import json
import queue
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from familiekilder.android_callback import CallbackCapture
from familiekilder.android_passwords import AndroidPasswordSession, _SocketCdp
from familiekilder.core import FamiliekilderError


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.queue = queue.Queue(maxsize=1)
        self.capture = CallbackCapture('app://ch-parent-android.vigilo.no', 'state-test', self.queue)
        self.capture.session_id = 'login-session'
        self.callback = 'app://ch-parent-android.vigilo.no?code=test-code&state=state-test'

    def event(self, method, params, session='login-session'):
        self.capture.event({'method': method, 'params': params, 'sessionId': session})

    def test_blocked_redirect_captured_from_extra_headers(self):
        self.event('Network.responseReceivedExtraInfo', {'headers': {'Location': self.callback}})
        self.assertEqual(self.queue.get_nowait(), self.callback)

    def test_redirect_and_navigation_variants(self):
        for method, params in [
            ('Network.requestWillBeSent', {'redirectResponse': {'headers': {'location': self.callback}}}),
            ('Network.requestWillBeSent', {'request': {'url': self.callback}}),
            ('Page.frameRequestedNavigation', {'url': self.callback}),
        ]:
            with self.subTest(method=method):
                self.event(method, params)
                self.assertEqual(self.queue.get_nowait(), self.callback)

    def test_other_tabs_and_sessions_ignored(self):
        for session in [None, 'unrelated-session']:
            self.event('Network.responseReceivedExtraInfo', {'headers': {'location': self.callback}}, session)
        self.assertTrue(self.queue.empty())

    def test_callback_requires_exact_address_and_single_state(self):
        invalid = [
            self.callback.replace('state-test', 'wrong'),
            self.callback + '&state=state-test',
            self.callback + '&code=other',
            self.callback + '&error=access_denied',
            self.callback + '#fragment',
            self.callback.replace('.no?', '.no/other?'),
            self.callback.replace('.no?', '.no:80?'),
            self.callback.replace('.no?', '.no:invalid?'),
            self.callback.replace('app://', 'https://'),
            self.callback.replace('app://', 'app://user@'),
            self.callback.replace('.no?', '.no.attacker.test?'),
            self.callback.replace('test-code', ''),
            self.callback + '\n',
            None,
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertFalse(self.capture.accept(value))
        self.assertTrue(self.queue.empty())

    def test_skolemelding_callback_and_error_are_supported(self):
        capture = CallbackCapture('com.cgi.oslomelding.foresatte://oauth', 'state-test', self.queue)
        for query in ['code=code', 'error=access_denied']:
            value = 'com.cgi.oslomelding.foresatte://oauth?' + query + '&state=state-test'
            self.assertTrue(capture.accept(value))
            self.assertEqual(self.queue.get_nowait(), value)

    def test_duplicate_receiver_and_cdp_callback_does_not_overwrite(self):
        self.assertTrue(self.capture.accept(self.callback))
        self.event('Network.responseReceivedExtraInfo', {'headers': {'location': self.callback}})
        self.assertEqual(self.queue.qsize(), 1)

    def test_android_authority_only_uri_can_include_root_slash(self):
        callback = self.callback.replace('.no?', '.no/?')
        self.assertTrue(self.capture.accept(callback))
        self.assertEqual(self.queue.get_nowait(), callback)

    def test_events_processed_while_waiting_for_command_response(self):
        socket = Mock()
        socket.recv.side_effect = [
            json.dumps({'method': 'Network.responseReceivedExtraInfo', 'sessionId': 'login-session',
                        'params': {'headers': {'location': self.callback}}}),
            json.dumps({'id': 1, 'result': {'ok': True}}),
        ]
        cdp = _SocketCdp(socket, self.capture.event)
        self.assertEqual(cdp.call('Page.navigate'), {'ok': True})
        self.assertEqual(self.queue.get_nowait(), self.callback)

    def test_idle_poll_handles_callback_without_command(self):
        socket = Mock()
        socket.recv.side_effect = [json.dumps({
            'method': 'Page.frameRequestedNavigation', 'sessionId': 'login-session',
            'params': {'url': self.callback},
        }), TimeoutError()]
        _SocketCdp(socket, self.capture.event).poll()
        self.assertEqual(self.queue.get_nowait(), self.callback)

    def test_socket_errors_do_not_expose_private_values(self):
        socket = Mock()
        socket.recv.side_effect = ValueError(self.callback)
        for action in ['call', 'poll']:
            with self.assertRaises(FamiliekilderError) as caught:
                cdp = _SocketCdp(socket)
                cdp.call('Page.navigate') if action == 'call' else cdp.poll()
            self.assertNotIn('test-code', str(caught.exception))

    def test_callback_listener_precedes_navigation_without_saved_password(self):
        cdp = Mock()
        cdp.call.side_effect = [
            {'targetId': 'fresh-tab'}, {'sessionId': 'fresh-session'}, {}, {}, {}, {},
        ]
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = json.dumps({
            'webSocketDebuggerUrl': 'ws://127.0.0.1:12345/devtools/browser/test'
        }).encode()
        with (
            patch('familiekilder.password_autofill.configured', return_value=False),
            patch('familiekilder.android_oauth._adb', return_value='12345') as adb,
            patch('http.client.HTTPConnection', return_value=connection),
            patch('websocket.create_connection'),
            patch('familiekilder.android_passwords._SocketCdp', return_value=cdp),
        ):
            session = AndroidPasswordSession('/owner', Path('/adb'), 'device')
            self.assertTrue(session.start('https://auth.test/authorize', callback_capture=self.capture))
            self.assertEqual(self.capture.session_id, 'fresh-session')
            self.assertEqual([c.args[0] for c in cdp.call.call_args_list], [
                'Target.createTarget', 'Target.attachToTarget', 'Network.enable', 'Page.enable', 'Page.navigate',
            ])
            self.assertEqual(cdp.call.call_args_list[0].args[1], {'url': 'about:blank'})
            self.assertEqual(session.autofill.page_ids, {'fresh-tab'})
            session.close()
            adb.assert_any_call(Path('/adb'), 'device', 'forward', '--remove', 'tcp:12345')
            self.assertEqual(cdp.call.call_args.args, ('Target.closeTarget', {'targetId': 'fresh-tab'}))



class AndroidCallbackHttpTests(unittest.TestCase):
    def test_receiver_rejects_stale_or_malformed_callback_without_poisoning_queue(self):
        import http.client
        import http.server
        import threading
        from familiekilder.android_oauth import _CallbackHandler

        callbacks = queue.Queue(maxsize=1)
        capture = CallbackCapture('app://ch-parent-android.vigilo.no', 'fresh-state', callbacks)

        class Handler(_CallbackHandler):
            callback_capture = capture
            callback_queue = callbacks

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for callback, expected in [
                ('app://ch-parent-android.vigilo.no:invalid?code=code&state=fresh-state', 400),
                ('app://ch-parent-android.vigilo.no?code=code&state=old-state', 400),
                ('app://ch-parent-android.vigilo.no/?code=code&state=fresh-state', 204),
            ]:
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                try:
                    connection.request('POST', '/v1/oauth-callback', body=callback)
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                finally:
                    connection.close()
            self.assertEqual(callbacks.qsize(), 1)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_android_receiver_is_exclusive_between_source_logins(self):
        import tempfile
        import threading
        from familiekilder.android_oauth import _android_login_lock
        from familiekilder.private_lock import PrivateLockBusy

        result = []
        def attempt():
            try:
                with _android_login_lock():
                    result.append('unexpected-entry')
            except PrivateLockBusy:
                result.append('busy')
        with tempfile.TemporaryDirectory() as directory, patch('pathlib.Path.home', return_value=Path(directory)):
            with _android_login_lock():
                worker = threading.Thread(target=attempt)
                worker.start()
                worker.join(timeout=2)
                self.assertEqual(result, ['busy'])
            with _android_login_lock():
                pass  # Cancellation/exit releases the shared receiver lease.


if __name__ == '__main__':
    unittest.main()
