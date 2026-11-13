"""Offline adapter boundaries reuse reads before pacing, never credentials."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from familiekilder import gmail, hoopit, ist_home, skolemelding, spond
from familiekilder import live_hoopit_chat, live_spond_chat, live_vigilo_reader
from familiekilder.api_transport import ApiHTTPError
from familiekilder.private_state import atomic_write_private_json
from familiekilder.sync_snapshot import SyncSnapshot, snapshot_scope, suspend_snapshot, suspended


class SyncSnapshotAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for source in ('spond', 'skolemelding', 'hoopit', 'gmail', 'ist-home', 'vigilo'):
            filename = 'gmail.json' if source == 'gmail' else source + '-session.json'
            atomic_write_private_json(self.root / '.private/credentials' / filename,
                                      {'subject': 'synthetic-' + source})
        self.snapshot = SyncSnapshot('synthetic-job')

    def test_spond_metadata_reuse_crosses_client_instances_and_keeps_tokens_live(self):
        clients = [object.__new__(spond.AuthorizedClient) for _ in range(2)]
        for client in clients:
            client.root = self.root
            client._get_json = Mock(return_value={'id': 'parent'})
        with snapshot_scope(self.snapshot, self.root):
            first = clients[0].get_json(spond.PROFILE_ENDPOINT)
            first['id'] = 'caller mutation'
            self.assertEqual(clients[1].get_json(spond.PROFILE_ENDPOINT), {'id': 'parent'})
            token_url = spond.API_ROOT + '/group/group-1/filesToken'
            clients[1].get_json(token_url)
            clients[1].get_json(token_url)
        clients[0]._get_json.assert_called_once_with(spond.PROFILE_ENDPOINT)
        self.assertEqual(clients[1]._get_json.call_count, 2)

    def test_skolemelding_get_and_exact_search_payload_reuse(self):
        client = object.__new__(skolemelding.AuthorizedClient)
        client.root = self.root
        client.transport = SimpleNamespace(get_json=Mock(return_value={'profile': []}),
                                           post_json=Mock(return_value={'Result': []}))
        client._request = lambda callback: callback('synthetic-token')
        with snapshot_scope(self.snapshot, self.root):
            for _ in range(2):
                client.get_json(skolemelding.PROFILE_ENDPOINT)
                client.post_json(skolemelding.MESSAGE_SEARCH_ENDPOINT, {'Page': 1})
            client.post_json(skolemelding.MESSAGE_SEARCH_ENDPOINT, {'Page': 2})
        self.assertEqual(client.transport.get_json.call_count, 1)
        self.assertEqual(client.transport.post_json.call_count, 2)

    def test_hoopit_cached_read_skips_live_loader_but_token_route_never_does(self):
        client = object.__new__(hoopit.AuthorizedClient)
        client.root, client.access = self.root, SimpleNamespace(app_version='fixture')
        client._get = Mock(return_value={'results': []})
        with snapshot_scope(self.snapshot, self.root):
            for _ in range(2):
                client.get('/app/users/current/posts/', page=1)
                client.get('/app/users/current/tokens/')
        self.assertEqual(client._get.call_count, 3)

    def test_gmail_exact_path_and_params_reuse_before_live_loader(self):
        client = object.__new__(gmail.Client)
        client.root, client._get = self.root, Mock(return_value={'id': 'message-1'})
        with snapshot_scope(self.snapshot, self.root):
            client.get('/messages/message-1', format='full')
            client.get('/messages/message-1', format='full')
            client.get('/messages/message-1', format='metadata')
        self.assertEqual(client._get.call_count, 2)

    def test_vigilo_reuses_reads_before_transport_and_write_scope_bypasses(self):
        transport = SimpleNamespace(get_json=Mock(return_value={'messages': []}))
        client = live_vigilo_reader.RefreshingClient(self.root, SimpleNamespace(), transport, 'token', False)
        with snapshot_scope(self.snapshot, self.root):
            client.get_json('https://fictional.invalid/messages', bearer_token='old', headers={'Version': '1'})
            client.get_json('https://fictional.invalid/messages', bearer_token='rotated', headers={'Version': '1'})
            self.assertEqual(transport.get_json.call_count, 1)
            client.get_json('https://fictional.invalid/messages', bearer_token='rotated', headers={'Version': '2'})
            with suspend_snapshot():
                client.get_json('https://fictional.invalid/messages', bearer_token='rotated', headers={'Version': '1'})
        self.assertEqual(transport.get_json.call_count, 3)

    def test_ist_queries_reuse_but_mutations_and_suspended_queries_are_live(self):
        with patch.object(ist_home, '_graphql', return_value={'getContext': {}}) as live:
            with snapshot_scope(self.snapshot, self.root):
                ist_home.graphql('access-1', ist_home.CONTEXT_QUERY)
                ist_home.graphql('rotated-access', ist_home.CONTEXT_QUERY)
                for _ in range(2):
                    ist_home.graphql('access-1', 'mutation Example { example }')
                with suspend_snapshot():
                    ist_home.graphql('access-1', ist_home.CONTEXT_QUERY)
        self.assertEqual(live.call_count, 4)

    def test_chat_reads_reuse_across_instances_without_bootstrap_or_pacing(self):
        for module, source in ((live_spond_chat, 'spond'), (live_hoopit_chat, 'hoopit')):
            with self.subTest(source=source):
                clients = [object.__new__(module.Client) for _ in range(2)]
                for client in clients:
                    client.core = SimpleNamespace(root=self.root)
                    client._get_live = Mock(return_value={'messages': []})
                with snapshot_scope(self.snapshot, self.root):
                    clients[0]._get('/chats')
                    clients[1]._get('/chats')
                clients[0]._get_live.assert_called_once()
                clients[1]._get_live.assert_not_called()

    def test_spond_find_read_reuses_exact_recipients(self):
        client = object.__new__(live_spond_chat.Client)
        client.core = SimpleNamespace(root=self.root)
        client._find_live = Mock(return_value={'id': 'chat-1'})
        with snapshot_scope(self.snapshot, self.root):
            client.find('group-1', ['person-1'])
            client.find('group-1', ['person-1'])
            client.find('group-1', ['person-2'])
        self.assertEqual(client._find_live.call_count, 2)

    def test_spond_chat_401_recovery_suspends_snapshot_for_profile_refresh(self):
        transport = SimpleNamespace(
            get_json=Mock(side_effect=[ApiHTTPError(401), {'messages': []}]),
            post_json=Mock(return_value={'url': live_spond_chat.CHAT_ROOT, 'auth': 'renewed'}),
        )
        recovery = Mock(side_effect=lambda url: self.assertTrue(suspended()))
        core = SimpleNamespace(root=self.root, token='synthetic', transport=transport,
                               get_json=recovery, _before_request=lambda: None,
                               _record_provider_stop=lambda exc: None)
        client = live_spond_chat.Client(spond, core)
        client.auth = 'expired'
        with snapshot_scope(self.snapshot, self.root):
            self.assertEqual(client._get('/chats'), {'messages': []})
        recovery.assert_called_once_with(spond.PROFILE_ENDPOINT)
        self.assertEqual(transport.get_json.call_count, 2)
