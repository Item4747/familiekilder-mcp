import unittest
from unittest.mock import patch
from familiekilder import browser_bootstrap as b


class UnreachableDiagnosticsTests(unittest.TestCase):
    def test_redacts_everything_except_fixed_kind_scheme_and_host(self):
        event = {'method': 'Page.frameNavigated', 'params': {'frame': {'url': 'chrome-error://chromewebdata/', 'unreachableUrl': 'https://user:secret@blocked.example/path?code=secret#private'}}}
        with self.assertLogs('familiekilder.browser_bootstrap', level='WARNING') as logged:
            b._report_unreachable(event, 'com.cgi.oslomelding.foresatte://oauth')
        text = '\n'.join(logged.output)
        self.assertIn('auth_navigation_unreachable', text)
        self.assertIn('skolemelding', text)
        self.assertIn('https', text)
        self.assertIn('blocked.example', text)
        for secret in ('user', 'secret', '/path', 'code=', '#private'):
            self.assertNotIn(secret, text)

    def test_ordinary_event_and_unsafe_scheme_do_not_emit(self):
        with patch.object(b.logging.getLogger(b.__name__), 'warning') as log:
            b._report_unreachable({'method': 'Page.frameNavigated', 'params': {'frame': {'url': 'https://example.test/'}}}, 'com.cgi.oslomelding.foresatte://oauth')
            b._report_unreachable({'method': 'Page.frameNavigated', 'params': {'frame': {'unreachableUrl': 'javascript:secret'}}}, 'com.cgi.oslomelding.foresatte://oauth')
            log.assert_not_called()

    def test_logging_failure_never_propagates(self):
        event = {'method': 'Page.frameNavigated', 'params': {'frame': {'unreachableUrl': 'https://blocked.example/'}}}
        with patch.object(b.logging.getLogger(b.__name__), 'warning', side_effect=OSError('private')):
            b._report_unreachable(event, 'com.cgi.oslomelding.foresatte://oauth')

    def test_long_query_and_custom_scheme_remain_sanitized(self):
        for url, expected_scheme, expected_host in (
            ('https://blocked.example/path?token=' + 'secret' * 2000, 'https', 'blocked.example'),
            ('bankid:///?secret=private', 'bankid', 'none'),
            ('intent://example.test/path#Intent;secret=private;end', 'intent', 'example.test'),
        ):
            with self.subTest(scheme=expected_scheme):
                event = {'method': 'Page.frameNavigated', 'params': {'frame': {'unreachableUrl': url}}}
                with self.assertLogs('familiekilder.browser_bootstrap', level='WARNING') as captured:
                    b._report_unreachable(event, 'com.cgi.oslomelding.foresatte://oauth')
                message = captured.output[0]
                self.assertIn('scheme=' + expected_scheme, message)
                self.assertIn('host=' + expected_host, message)
                for secret in ('secret', 'private', '/path', 'token='):
                    self.assertNotIn(secret, message)

    def test_expected_native_callback_and_malformed_input_are_ignored(self):
        redirect = 'bankid://return/path'
        with patch.object(b.logging.getLogger(b.__name__), 'warning') as log:
            for url in (redirect + '?code=' + 'private' * 2000, None, {'token': 'private'}):
                b._report_unreachable({'method': 'Page.frameNavigated', 'params': {'frame': {'unreachableUrl': url}}}, redirect)
            log.assert_not_called()
