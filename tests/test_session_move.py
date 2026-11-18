"""Session moves use fictional files and isolated loopback servers only."""

import asyncio
import base64
import json
import os
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from familiekilder import auth_lifecycle as life, gmail, session_move as move, source_server
from familiekilder.core import FamiliekilderError
from familiekilder.hosting import host_settings
from familiekilder.live_config import Config
from familiekilder.private_state import atomic_write_private_json, credential_path, read_private_json
from familiekilder.remote_http import build_http_app
from familiekilder.source_server import ReadOnlyRuntime
from familiekilder_testing import remote as fake


class RecordingToolError(source_server.ToolError):
    """A tool error that keeps the traceback of the exception it replaces."""

    causes: list[str] = []

    def __init__(self, *args):
        RecordingToolError.causes.append(traceback.format_exc())
        super().__init__(*args)


class MoveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.mac = Path(directory.name) / 'mac'
        self.host = Path(directory.name) / 'host'
        self.port = fake.free_port()
        environment = patch.dict(os.environ, fake.host_environment(self.port))
        environment.start()
        self.addCleanup(environment.stop)
        self.platform = patch('sys.platform', 'linux')
        self.platform.start()
        self.addCleanup(self.platform.stop)
        Config(self.mac, {'spond': frozenset(), 'gmail': frozenset()}).save()
        for source in ('spond', 'gmail'):
            atomic_write_private_json(credential_path(self.mac, move.session_filename(source)), {
                'access_token': 'fictional-access', 'refresh_token': 'fictional-refresh',
                'expires_at': 0, 'scope': gmail.SCOPE,
                'client_id': 'fictional-client', 'client_secret': 'fictional-secret',
            })
            life.bind_owner(self.mac, source)
        atomic_write_private_json(credential_path(self.mac, 'spond-client.json'), {'client': 'fictional'})
        atomic_write_private_json(credential_path(self.mac, 'spond-refresh-response.json'), {'response': 'fictional-journal'})
        config = Config.load(self.host)
        item = {'source': 'spond', 'source_id': 'post:fictional', 'title': 'Oppdiktet',
                'body': 'Oppdiktet tekst', 'reference': {'kind': 'post', 'id': 'fictional'}, 'attachments': []}
        self.runtime = ReadOnlyRuntime(config, reader=lambda *_: ([item], True))
        self.addCleanup(self.runtime.close)
        self.built = build_http_app(config, host_settings(), runtime=self.runtime)
        context = fake.running(self.built.app, self.port)
        self.base = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def test_round_trip_preserves_session_client_journal_and_activates_live_catalog(self):
        async def catalog():
            async with fake.session(self.base) as session:
                self.assertEqual([t.name for t in (await session.list_tools()).tools], ['family_connections'])
                result = await asyncio.to_thread(move.move_to, self.mac, self.base, fake.OWNER_CODE)
                self.assertEqual(result['moved'], ['gmail', 'spond'])
                names = [t.name for t in (await session.list_tools()).tools]
                self.assertIn('spond_list_items', names)
                self.assertIn('gmail_search', names)
                with patch.object(source_server, 'ToolError', RecordingToolError):
                    answer = await session.call_tool('spond_list_items', {})
                text = answer.content[0].text
                # The tool hides the cause of an error; show what it caught.
                self.assertTrue(text.startswith('{'), text + '\n' + ''.join(RecordingToolError.causes))
                self.assertEqual(json.loads(text)['items'][0]['title'], 'Oppdiktet')
        asyncio.run(catalog())
        for source in ('spond', 'gmail'):
            self.assertFalse(credential_path(self.mac, move.session_filename(source)).exists())
            self.assertTrue(move.moved_sources(self.mac)[source]['confirmed'])
            life.require_owner(self.host, source)
            self.assertEqual(Config.load(self.host).services[source], frozenset())
            with self.assertRaisesRegex(FamiliekilderError, 'flyttet'):
                life.require_not_moved(self.mac, source)
        self.assertEqual(read_private_json(credential_path(self.host, 'spond-client.json')), {'client': 'fictional'})
        self.assertEqual(read_private_json(credential_path(self.host, 'spond-refresh-response.json')), {'response': 'fictional-journal'})

    def test_interruption_after_import_before_local_deletion_blocks_gmail_and_refresh(self):
        original = move._post
        def post(client, url, code, body):
            response = original(client, url, code, body)
            if url.endswith(move.IMPORT_PATH):
                self.assertEqual(response.status_code, 200)
                raise KeyboardInterrupt
            return response
        with patch.object(move, '_post', post), self.assertRaises(KeyboardInterrupt):
            move.move_to(self.mac, self.base, fake.OWNER_CODE)
        self.assertTrue(credential_path(self.mac, 'gmail.json').exists())
        self.assertIn('gmail', move.moved_sources(self.mac))
        calls = []
        client = gmail.Client(self.mac, client=httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))))
        self.addCleanup(client.close)
        with self.assertRaisesRegex(FamiliekilderError, 'flyttet'):
            client.token(force=True)
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(FamiliekilderError, 'flyttet'):
            life.require_not_moved(self.mac, 'spond')
        with patch.dict(os.environ, {'FAMILIEKILDER_INSTALLATION_ID': 'different-installation-id-0000000000'}):
            copied = gmail.Client(self.host, client=client.http)
            with self.assertRaisesRegex(FamiliekilderError, 'annen'):
                copied.token(force=True)
        with patch.dict(os.environ, {'FAMILIEKILDER_INSTALLATION_ID': ''}):
            with self.assertRaises(FamiliekilderError):
                gmail.Client(self.host, client=client.http).token(force=True)
        self.assertEqual(calls, [])

    def test_wrong_host_modified_package_and_replay_are_refused(self):
        receiver = move.MoveReceiver(self.host, host_settings(), self.built.provider.gate)
        public = receiver.create_key()
        payload = {'format': move.PAYLOAD_FORMAT, 'version': 1, 'host': public['host'],
                   'sources': {'gmail': {'files': {'gmail.json': base64.urlsafe_b64encode(b'{}').decode()}}}}
        package = move.seal(payload, key_id=public['key_id'], public_key=move._unb64(public['public_key']), host=public['host'])
        key = receiver.take_key(public['key_id'])
        with self.assertRaises(move.MoveError):
            move.open_package(package, key, key_id=public['key_id'], host='other-host')
        altered = json.loads(package)
        ciphertext = bytearray(move._unb64(altered['ciphertext']))
        ciphertext[-1] ^= 1
        altered['ciphertext'] = move._b64(ciphertext)
        with self.assertRaises(move.MoveError):
            move.open_package(json.dumps(altered).encode(), key, key_id=public['key_id'], host=public['host'])
        self.assertIsNone(receiver.take_key(public['key_id']))

    def test_import_crash_after_session_write_keeps_destination_ownership(self):
        package = {'sources': {'gmail': {'gmail.json': credential_path(self.mac, 'gmail.json').read_bytes()}}}
        original = move.atomic_write_private_bytes
        def crash(path, data):
            self.assertTrue(credential_path(self.host, 'gmail-auth-owner.json').exists())
            original(path, data)
            raise KeyboardInterrupt
        with patch.object(move, 'atomic_write_private_bytes', crash), self.assertRaises(KeyboardInterrupt):
            move.import_sources(self.host, package)
        self.assertTrue(credential_path(self.host, 'gmail.json').exists())
        life.require_owner(self.host, 'gmail')
        with patch.dict(os.environ, {'FAMILIEKILDER_INSTALLATION_ID': 'different-installation-id-0000000000'}):
            with self.assertRaisesRegex(FamiliekilderError, 'annen'):
                life.require_owner(self.host, 'gmail')

    def test_hosted_ownerless_session_is_never_adopted_even_without_host_secrets(self):
        move.MoveReceiver(self.host, host_settings(), self.built.provider.gate).create_key()
        for source in ('gmail', 'spond'):
            atomic_write_private_json(credential_path(self.host, move.session_filename(source)), {'refresh_token': 'fictional'})
            for identity in (fake.INSTALLATION_ID, ''):
                with patch.dict(os.environ, {'FAMILIEKILDER_INSTALLATION_ID': identity}):
                    with self.assertRaisesRegex(FamiliekilderError, 'mangler eierbinding'):
                        life.require_owner(self.host, source)
            self.assertFalse(credential_path(self.host, f'{source}-auth-owner.json').exists())

    def test_uncertain_network_outcome_leaves_local_sources_blocked(self):
        original = move._post
        def post(client, url, code, body):
            if url.endswith(move.IMPORT_PATH):
                original(client, url, code, body)
                raise httpx.ReadTimeout('fictional lost response')
            return original(client, url, code, body)
        with patch.object(move, '_post', post), self.assertRaises(move.MoveUncertain):
            move.move_to(self.mac, self.base, fake.OWNER_CODE)
        self.assertFalse(credential_path(self.mac, 'gmail.json').exists())
        self.assertFalse(move.moved_sources(self.mac)['gmail']['confirmed'])


if __name__ == '__main__':
    unittest.main()
