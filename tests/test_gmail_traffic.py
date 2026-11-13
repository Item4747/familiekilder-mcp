"""Provider-visible retry, pacing and restart behavior, without real waiting."""

import tempfile
import time
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from familiekilder.gmail import Client, SCOPE
from familiekilder.gmail_traffic import GmailDeferred, MIN_INTERVAL
from familiekilder.private_state import atomic_write_private_json
from familiekilder.private_lock import private_source_lock, PrivateLockBusy
from familiekilder.source_adapter import SourceLoginRequired, SourceUnavailable
from familiekilder.live_readers import get_live
from familiekilder_testing.gmail import mail


def limited(reason='userRateLimitExceeded', *, headers=None):
    return httpx.Response(
        403,
        headers=headers or {},
        json={'error': {'message': 'secret email/token', 'errors': [{'reason': reason}]}},
    )


class GmailTrafficTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.now = Path(tmp.name), time.time()
        self.requests, self.waits = [], []

    def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay

    def client(self, respond):
        def handler(request):
            self.requests.append((self.now, request.method))
            return respond(request)

        client = Client(
            self.root,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            clock=lambda: self.now,
            sleep=self.sleep,
            jitter=lambda: 0,
        )
        self.addCleanup(client.close)
        if not client.path.exists():
            atomic_write_private_json(
                client.path,
                {
                    'access_token': 'fixture',
                    'refresh_token': 'fixture',
                    'scope': SCOPE,
                    'client_id': 'fixture',
                    'client_secret': 'fixture',
                    'expires_at': self.now + 3600,
                },
            )
        return client

    def test_successive_clients_share_pacing_and_store_no_email(self):
        one = self.client(lambda _: httpx.Response(200, json={'secret': 'email'}))
        one.get('/messages', q='private search')
        two = self.client(lambda _: httpx.Response(200, json={}))
        two.get('/messages/secret-mail-id')
        self.assertGreaterEqual(self.requests[1][0] - self.requests[0][0], MIN_INTERVAL)
        persisted = two.traffic.path.read_text()
        for private in ('secret', 'private search', 'fixture', '/messages'):
            self.assertNotIn(private, persisted)

    def test_parallel_message_reads_wait_and_share_pacing(self):
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()

        def respond(request):
            if request.url.path.endswith('/first'):
                entered.set()
                if not release.wait(3):
                    raise AssertionError('First read was not released')
            return httpx.Response(200, json=mail(ident=request.url.path.rsplit('/', 1)[1]))

        # Exercise both the provider-level lock and nested Client.get lock.
        one, two = self.client(respond), self.client(respond)

        def second():
            second_started.set()
            return get_live(self.root, 'gmail', {'kind': 'message', 'id': 'second'})

        with patch('familiekilder.gmail.Client', side_effect=[one, two]):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(
                    get_live, self.root, 'gmail', {'kind': 'message', 'id': 'first'}
                )
                try:
                    self.assertTrue(entered.wait(3))
                    following = pool.submit(second)
                    self.assertTrue(second_started.wait(3))
                    # A fail-fast implementation completes with PrivateLockBusy here.
                    with self.assertRaises(TimeoutError):
                        following.result(timeout=0.1)
                    self.assertEqual(len(self.requests), 1)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=3)['source_id'], 'message:first')
                self.assertEqual(following.result(timeout=3)['source_id'], 'message:second')
        self.assertEqual([m for _, m in self.requests], ['GET', 'GET'])
        self.assertGreaterEqual(self.requests[1][0] - self.requests[0][0], MIN_INTERVAL)

    def test_parallel_clients_serialize_without_an_outer_provider_lock(self):
        entered, release = threading.Event(), threading.Event()

        def respond(request):
            entered.set()
            if not release.wait(3):
                raise AssertionError('First read was not released')
            return httpx.Response(200, json={'first': True})

        one = self.client(respond)
        two = self.client(lambda _: httpx.Response(200, json={'second': True}))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(one.get, '/profile')
            try:
                self.assertTrue(entered.wait(3))
                following = pool.submit(two.get, '/profile')
                with self.assertRaises(TimeoutError):
                    following.result(timeout=0.1)
                self.assertEqual(len(self.requests), 1)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=3), {'first': True})
            self.assertEqual(following.result(timeout=3), {'second': True})
        self.assertGreaterEqual(self.requests[1][0] - self.requests[0][0], MIN_INTERVAL)

    def test_busy_read_times_out_without_network_or_traffic_reservation(self):
        client = self.client(lambda _: self.fail('Unexpected network request'))
        with private_source_lock(self.root, 'gmail'):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with patch('familiekilder.gmail.READ_LOCK_TIMEOUT', 0.03):
                    pending = pool.submit(client.get, '/messages/blocked')
                    with self.assertRaisesRegex(
                        PrivateLockBusy, 'Tidsavbrudd.*ingen forespørsel sendt'
                    ):
                        pending.result(timeout=3)
                # provider imports the shared constant when each call starts.
                with patch('familiekilder.gmail_traffic.READ_LOCK_TIMEOUT', 0.03):
                    pending = pool.submit(
                        get_live, self.root, 'gmail', {'kind': 'message', 'id': 'blocked'}
                    )
                    with self.assertRaisesRegex(PrivateLockBusy, 'Tidsavbrudd'):
                        pending.result(timeout=3)
        self.assertEqual(self.requests, [])
        self.assertFalse(client.traffic.path.exists())

    def test_transient_quota_recovers_without_refreshing_credentials(self):
        client = self.client(
            lambda _: (
                limited() if len(self.requests) == 1 else httpx.Response(200, json={'ok': True})
            )
        )
        before = client.path.read_bytes()
        self.assertEqual(client.get('/messages'), {'ok': True})
        self.assertEqual([m for _, m in self.requests], ['GET', 'GET'])
        self.assertGreaterEqual(self.requests[1][0] - self.requests[0][0], 1)
        self.assertEqual(client.path.read_bytes(), before)
        self.assertEqual(client.traffic.state()['interval'], 1)
        self.assertIsNone(client.traffic.state()['blocked_until'])

    def test_retry_after_seconds_is_honored_before_success(self):
        client = self.client(
            lambda _: (
                limited(headers={'Retry-After': '3'})
                if len(self.requests) == 1
                else httpx.Response(200, json={})
            )
        )
        client.get('/messages')
        self.assertGreaterEqual(self.requests[1][0] - self.requests[0][0], 3)

    def test_long_retry_after_http_date_survives_client_restart(self):
        after = format_datetime(datetime.fromtimestamp(self.now + 120, UTC), usegmt=True)
        client = self.client(
            lambda _: httpx.Response(429, headers={'Retry-After': after}, text='secret')
        )
        with self.assertRaises(GmailDeferred) as error:
            client.get('/messages')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(error.exception.reason, 'rate_limit')
        self.assertNotIn('secret', str(error.exception))
        replacement = self.client(lambda _: httpx.Response(200, json={'ok': True}))
        with self.assertRaises(GmailDeferred):
            replacement.get('/profile')
        self.assertEqual(len(self.requests), 1)
        self.now = client.traffic.state()['blocked_until'] + 1
        self.assertEqual(replacement.get('/profile'), {'ok': True})

    def test_exhausted_retries_pause_all_clients_then_resume_and_recover_speed(self):
        client = self.client(lambda _: limited('rateLimitExceeded'))
        with self.assertRaises(GmailDeferred):
            client.get('/messages')
        self.assertEqual(len(self.requests), 3)
        state = client.traffic.state()
        self.assertGreaterEqual(state['blocked_until'] - self.now, 60)
        self.assertLess(sum(self.waits), 10)
        replacement = self.client(lambda _: httpx.Response(200, json={}))
        with self.assertRaises(GmailDeferred):
            replacement.get('/messages')
        self.assertEqual(len(self.requests), 3)
        self.now = state['blocked_until'] + 1
        for _ in range(20):
            replacement.get('/profile')
        self.assertLess(replacement.traffic.state()['interval'], state['interval'])
        self.assertIsNone(replacement.traffic.state()['reason'])

    def test_daily_quota_is_not_retried_or_misreported_as_login(self):
        client = self.client(lambda _: limited('dailyLimitExceeded'))
        with self.assertRaises(GmailDeferred) as error:
            client.get('/messages')
        self.assertEqual(error.exception.reason, 'daily_quota')
        self.assertEqual(len(self.requests), 1)
        self.assertGreaterEqual(client.traffic.state()['blocked_until'] - self.now, 3600)

    def test_permission_errors_do_not_retry_or_create_quota_pause(self):
        client = self.client(lambda _: limited('insufficientPermissions'))
        with self.assertRaises(SourceLoginRequired):
            client.get('/messages')
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(client.traffic.state()['blocked_until'])

    def test_backend_error_recovers_with_bounded_backoff(self):
        client = self.client(
            lambda _: (
                httpx.Response(503, text='secret')
                if len(self.requests) == 1
                else httpx.Response(200, json={})
            )
        )
        client.get('/messages')
        self.assertEqual(len(self.requests), 2)
        self.assertGreaterEqual(self.requests[1][0] - self.requests[0][0], 1)
        self.assertEqual(client.traffic.state()['interval'], MIN_INTERVAL)

    def test_invalid_state_prevents_network_request(self):
        client = self.client(lambda _: self.fail('Unexpected network request'))
        state = client.traffic.state()
        state['interval'] = float('nan')
        atomic_write_private_json(client.traffic.path, state)
        with self.assertRaises(SourceUnavailable):
            client.get('/messages')
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
