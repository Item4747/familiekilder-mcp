from __future__ import annotations

import json
import http.client
import stat
import tempfile
import threading
import unittest
import urllib.parse
from datetime import UTC, datetime
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from familiekilder.api_transport import (
    MAX_RETRY_AFTER_SECONDS,
    ApiHTTPError,
    ApiTransport,
    ApiTransportError,
    _PinnedHTTPSConnection,
    _retry_after_seconds,
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/json":
            payload = {
                "ok": True,
                "authenticated": self.headers.get("Authorization") == "Bearer private-token",
            }
            self._send(200, json.dumps(payload).encode())
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/json")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/error":
            self._send(
                401,
                b'{"error":"invalid_grant","detail":"private response body"}',
            )
        elif self.path == "/rate-limit":
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.send_header("Retry-After", "120")
            self.end_headers()
            self.wfile.write(b"{}")
        elif self.path == "/large":
            self._send(200, b'{"padding":"0123456789"}')
        elif self.path == "/truncated-json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "200")
            self.end_headers()
            self.wfile.write(b'{"private":"partial-secret')
        elif self.path == "/truncated-error":
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "200")
            self.end_headers()
            self.wfile.write(b'{"private":"partial-error-secret')
        elif self.path == "/download":
            self._send(200, b"attachment-bytes", "text/plain; charset=utf-8")
        elif self.path == "/truncated-download":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "200")
            self.end_headers()
            self.wfile.write(b"partial-download-secret")
        elif self.path == "/oversized-download":
            self._send(200, b"too-large")
        else:
            self._send(404, b"{}")

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        if self.path == "/form":
            fields = urllib.parse.parse_qs(body.decode("ascii"))
            self._send(200, json.dumps({"field": fields["field"][0]}).encode())
        elif self.path == "/post-json":
            self._send(200, json.dumps({"received": json.loads(body)}).encode())
        elif self.path == "/post-empty":
            self._send(204, b"")
        else:
            self._send(404, b"{}")

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path == "/delete-empty":
            self._send(204, b"")
        else:
            self._send(200, json.dumps({"authenticated": self.headers.get("Authorization") == "Bearer private-token", "body_length": self.headers.get("Content-Length", "0")}).encode())

    def log_message(self, _format: str, *_args) -> None:
        return


class ApiTransportTests(unittest.TestCase):
    def test_http_observation_counts_downloads_errors_and_no_validation_rejections(self):
        from familiekilder.network_usage import observe_http
        calls = []
        transport = self.transport()
        with tempfile.TemporaryDirectory() as directory, observe_http(lambda: calls.append(True)):
            transport.get_json(self.base + '/json')
            with self.assertRaises(ApiHTTPError):
                transport.get_json(self.base + '/error')
            target = Path(directory) / '.private' / 'attachment.txt'
            transport.download(self.base + '/download', target)
            self.assertEqual(target.read_bytes(), b'attachment-bytes')
            with self.assertRaises(ApiTransportError):
                transport.get_json(self.base + '/not-allowed')
        self.assertEqual(len(calls), 3)
        with observe_http(lambda: (_ for _ in ()).throw(RuntimeError('Observer unavailable'))):
            self.assertTrue(transport.get_json(self.base + '/json')['ok'])

    def test_bodyless_delete_auth_empty_receipt_and_allowlist(self):
        url = self.base + "/delete-empty"
        transport = ApiTransport(allowed_urls={url, self.base + "/delete-json"})
        self.assertIsNone(transport.delete_json(url, allow_empty=True))
        with self.assertRaises(ApiTransportError):
            transport.delete_json(url)
        self.assertEqual(transport.delete_json(self.base + "/delete-json", bearer_token="private-token"), {"authenticated": True, "body_length": "0"})
        with self.assertRaises(ApiTransportError):
            transport.delete_json(self.base + "/not-allowed", allow_empty=True)

    def test_explicit_json_array_preserves_object_guard_and_url_allowlist(self):
        transport = self.transport()
        url = self.base + "/post-json"
        self.assertEqual(transport.post_json_array(url, ["one", "two"]), {"received": ["one", "two"]})
        with self.assertRaises(ApiTransportError):
            transport.post_json(url, ["one"])
        with self.assertRaises(ApiTransportError):
            transport.post_json_array(url, {"one": 1})
        with self.assertRaises(ApiTransportError):
            transport.post_json_array(self.base + "/not-allowed", ["one"])

    def test_empty_write_receipt_requires_explicit_opt_in(self):
        transport = ApiTransport(allowed_urls={self.base + "/post-empty"})
        self.assertIsNone(transport.post_json(self.base + "/post-empty", {}, allow_empty=True))
        with self.assertRaises(ApiTransportError):
            transport.post_json(self.base + "/post-empty", {})

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def transport(self, *, max_json_bytes: int = 1024) -> ApiTransport:
        paths = (
            "/json",
            "/form",
            "/post-json",
            "/redirect",
            "/error",
            "/rate-limit",
            "/large",
            "/truncated-json",
            "/truncated-error",
            "/download",
            "/truncated-download",
            "/oversized-download",
        )
        return ApiTransport(
            allowed_urls={self.base + path for path in paths},
            safe_error_codes={"invalid_grant"},
            max_json_bytes=max_json_bytes,
            max_download_bytes=64,
        )

    def test_json_form_bearer_and_exact_allowlist(self) -> None:
        transport = self.transport()
        self.assertEqual(
            transport.get_json(self.base + "/json", bearer_token="private-token"),
            {"ok": True, "authenticated": True},
        )
        self.assertEqual(
            transport.post_form(self.base + "/form", {"field": "value"}),
            {"field": "value"},
        )
        self.assertEqual(
            transport.post_json(self.base + "/post-json", {"key": "value"}),
            {"received": {"key": "value"}},
        )
        with self.assertRaisesRegex(ApiTransportError, "ukjent endepunkt"):
            transport.get_json(self.base + "/not-allowed")

    def test_https_host_allowlist_accepts_paths_but_not_ports_or_http(self) -> None:
        transport = ApiTransport(allowed_hosts={"files.example"})
        self.assertEqual(
            transport._validate_url("https://files.example/path?id=1"),
            "https://files.example/path?id=1",
        )
        for denied in (
            "http://files.example/path",
            "https://files.example:444/path",
            "https://other.example/path",
            "https://user@files.example/path",
        ):
            with self.subTest(denied=denied):
                with self.assertRaises(ApiTransportError):
                    transport._validate_url(denied)

    def test_host_allowlist_rejects_local_and_private_dns_targets(self) -> None:
        with self.assertRaises(ApiTransportError):
            ApiTransport(allowed_hosts={"localhost"})
        with self.assertRaises(ApiTransportError):
            ApiTransport(allowed_hosts={"127.0.0.1"})

        transport = ApiTransport(allowed_hosts={"files.example"})
        with (
            patch(
                "familiekilder.api_transport.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("10.0.0.5", 443))],
            ),
            patch.object(
                transport._opener,
                "open",
                side_effect=AssertionError("private target must not be opened"),
            ),
        ):
            with self.assertRaisesRegex(ApiTransportError, "ikke offentlig"):
                transport.get_json("https://files.example/attachment")

    def test_host_allowlist_pins_the_validated_public_address(self) -> None:
        transport = ApiTransport(allowed_hosts={"files.example"})
        created: list[tuple[str, str, float]] = []

        class Response:
            status = 200
            headers = Message()

            def read(self, _amount: int | None = None) -> bytes:
                return b'{"ok":true}'

            def close(self) -> None:
                return None

        class Connection:
            def __init__(self, hostname: str, address: str, *, timeout: float) -> None:
                created.append((hostname, address, timeout))

            def connect(self) -> None:
                return None

            def request(self, method: str, path: str, *, body=None, headers=None) -> None:
                self.request_value = (method, path, body, headers)

            def getresponse(self) -> Response:
                return Response()

            def close(self) -> None:
                return None

        with (
            patch(
                "familiekilder.api_transport.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
            ),
            patch(
                "familiekilder.api_transport._PinnedHTTPSConnection",
                Connection,
            ),
        ):
            self.assertEqual(
                transport.get_json("https://files.example/attachment?id=1"),
                {"ok": True},
            )
        self.assertEqual(
            created,
            [("files.example", "93.184.216.34", transport.timeout)],
        )

        connection = _PinnedHTTPSConnection(
            "files.example", "93.184.216.34", timeout=7
        )
        plain_socket = Mock()
        wrapped_socket = Mock()
        context = Mock()
        context.wrap_socket.return_value = wrapped_socket
        connection._context = context
        with patch(
            "familiekilder.api_transport.socket.create_connection",
            return_value=plain_socket,
        ) as create_connection:
            connection.connect()
        create_connection.assert_called_once_with(
            ("93.184.216.34", 443), 7, None
        )
        context.wrap_socket.assert_called_once_with(
            plain_socket, server_hostname="files.example"
        )
        self.assertIs(connection.sock, wrapped_socket)

    def test_redirects_and_oversized_json_fail_closed(self) -> None:
        transport = self.transport(max_json_bytes=8)
        with self.assertRaises(ApiHTTPError) as redirected:
            transport.get_json(self.base + "/redirect")
        self.assertEqual(redirected.exception.status_code, 302)
        with self.assertRaisesRegex(ApiTransportError, "for mye"):
            transport.get_json(self.base + "/large")

    def test_download_redirect_never_forwards_bearer(self) -> None:
        original = _Handler.do_GET
        requests = []
        def record(handler):
            requests.append((handler.path, handler.headers.get("Authorization")))
            return original(handler)
        with tempfile.TemporaryDirectory() as directory, patch.object(_Handler, "do_GET", record):
            with self.assertRaises(ApiHTTPError) as error:
                self.transport().download(self.base + "/redirect", Path(directory) / ".private" / "file.pdf",
                                          bearer_token="fictional-token")
        self.assertEqual(error.exception.status_code, 302)
        self.assertEqual(requests, [("/redirect", "Bearer fictional-token")])

    def test_progress_reports_only_fully_read_valid_successes(self):
        from familiekilder.api_transport import completed_calls
        observer = Mock()
        with completed_calls(observer):
            self.transport().get_json(self.base + '/json')
            observer.assert_called_once_with()
            for endpoint in ('/error', '/redirect', '/truncated-json'):
                with self.subTest(endpoint=endpoint), self.assertRaises(ApiTransportError):
                    self.transport().get_json(self.base + endpoint)
                observer.assert_called_once_with()
            with self.assertRaises(ApiTransportError):
                self.transport(max_json_bytes=8).get_json(self.base + '/large')
            observer.assert_called_once_with()
        self.transport().get_json(self.base + '/json')
        observer.assert_called_once_with()

    def test_truncated_responses_are_sanitized_transport_failures(self) -> None:
        transport = self.transport()
        with self.assertRaises(ApiTransportError) as truncated:
            transport.get_json(self.base + "/truncated-json")
        self.assertNotIsInstance(truncated.exception, http.client.IncompleteRead)
        self.assertNotIn("partial-secret", repr(truncated.exception))

        with self.assertRaises(ApiHTTPError) as error:
            transport.get_json(self.base + "/truncated-error")
        self.assertEqual(error.exception.status_code, 503)
        self.assertIsNone(error.exception.error_code)
        self.assertNotIn("partial-error-secret", repr(error.exception))

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / ".private" / "truncated.bin"
            with self.assertRaises(ApiTransportError) as download:
                transport.download(self.base + "/truncated-download", target, max_bytes=1000)
            self.assertNotIn("partial-download-secret", repr(download.exception))
            self.assertFalse(target.exists())

    def test_environment_proxy_is_ignored_and_header_injection_is_rejected(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                },
                clear=False,
            ),
            patch(
                "familiekilder.api_transport.urllib.request.getproxies",
                side_effect=AssertionError("environment proxy must not be read"),
            ),
        ):
            transport = self.transport()
        self.assertEqual(transport.get_json(self.base + "/json")["ok"], True)

        secret = "private-header-value"
        with self.assertRaises(ApiTransportError) as raised:
            transport.get_json(
                self.base + "/json",
                headers={"X-Test": f"valid-prefix\n{secret}"},
            )
        self.assertNotIn(secret, str(raised.exception))
        with self.assertRaises(ApiTransportError):
            transport.get_json(
                self.base + "/json",
                headers={"Host": "other.example"},
            )

    def test_http_error_exposes_only_allowlisted_code(self) -> None:
        transport = self.transport()
        with self.assertRaises(ApiHTTPError) as raised:
            transport.get_json(self.base + "/error")
        error = raised.exception
        self.assertEqual(error.status_code, 401)
        self.assertEqual(error.error_code, "invalid_grant")
        self.assertNotIn("private response body", str(error))
        self.assertNotIn("private response body", repr(error))

        bounded = self.transport(max_json_bytes=8)
        with self.assertRaises(ApiHTTPError) as oversized:
            bounded.get_json(self.base + "/error")
        self.assertIsNone(oversized.exception.error_code)

        with self.assertRaises(ApiHTTPError) as limited:
            transport.get_json(self.base + "/rate-limit")
        self.assertEqual(limited.exception.status_code, 429)
        self.assertEqual(limited.exception.retry_after_seconds, 120)
        self.assertNotIn("Retry-After", str(limited.exception))

    def test_retry_after_date_is_sanitized_bounded_metadata(self) -> None:
        now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        self.assertEqual(
            _retry_after_seconds("Mon, 31 Aug 2026 12:02:00 GMT", now=now),
            120,
        )
        self.assertEqual(
            _retry_after_seconds(str(MAX_RETRY_AFTER_SECONDS + 1)),
            MAX_RETRY_AFTER_SECONDS,
        )
        self.assertIsNone(_retry_after_seconds("private-header-value"))

    def test_download_is_atomic_private_bounded_and_hashed(self) -> None:
        transport = self.transport()
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary) / ".private"
            target = private / "incoming" / "run" / "attachment.txt"
            result = transport.download(
                self.base + "/download",
                target,
                allowed_content_types={"text/plain"},
            )
            self.assertEqual(target.read_bytes(), b"attachment-bytes")
            self.assertEqual(result.byte_size, len(b"attachment-bytes"))
            self.assertEqual(len(result.sha256), 64)
            self.assertEqual(result.content_type, "text/plain")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

            denied = private / "incoming" / "run" / "denied.bin"
            with self.assertRaisesRegex(ApiTransportError, "overskred"):
                transport.download(
                    self.base + "/oversized-download",
                    denied,
                    max_bytes=3,
                )
            self.assertFalse(denied.exists())
            self.assertEqual(list(denied.parent.glob(f".{denied.name}.*.tmp")), [])


    def test_truncated_download_preserves_previous_file_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / ".private" / "attachment.bin"
            self.transport().download(self.base + "/download", target)
            previous = target.read_bytes()
            with self.assertRaisesRegex(ApiTransportError, "fullstendig"):
                self.transport().download(self.base + "/truncated-download", target, max_bytes=1000)
            self.assertEqual(target.read_bytes(), previous)
            self.assertEqual(list(target.parent.glob("*.tmp")), [])


class SharedDownloadTests(unittest.TestCase):
    def test_ist_uses_complete_atomic_download_and_preserves_aggregate_budget(self):
        import io
        from familiekilder import ist_home
        from familiekilder.core import FamiliekilderError
        for declared, body, budget_size, succeeds in ((3, b'abc', 10, True),
                (10, b'abc', 20, False), (3, b'abc', 2, False)):
            with self.subTest(declared=declared, budget=budget_size), tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary) / '.private' / 'attachment'
                target.parent.mkdir(mode=0o700)
                target.write_bytes(b'previous')
                target.chmod(0o600)
                response = Mock(status=200, headers={'Content-Length': str(declared)})
                response.read.side_effect = io.BytesIO(body).read
                connection = Mock()
                budget = ist_home.DownloadBudget(budget_size)
                with patch.object(ist_home, '_open_pinned_https', return_value=(connection, response)):
                    if succeeds:
                        ist_home.download('https://synthetic.test/file', target, budget)
                        self.assertEqual(target.read_bytes(), body)
                        self.assertEqual(budget.used, len(body))
                    else:
                        with self.assertRaises(FamiliekilderError):
                            ist_home.download('https://synthetic.test/file', target, budget)
                        self.assertEqual(target.read_bytes(), b'previous')
                connection.close.assert_called_once()
                response.close.assert_called()
                self.assertEqual(list(target.parent.glob('*.tmp')), [])

    def test_ist_keeps_https_public_address_and_no_redirect_policy(self):
        from familiekilder import ist_home
        from familiekilder.core import FamiliekilderError
        for url in ('http://example.test/file', 'https://user@example.test/file',
                    'https://example.test:8443/file', 'https://example.test/file#fragment'):
            with self.subTest(url=url), patch.object(ist_home.socket, 'getaddrinfo') as resolve:
                with self.assertRaises(FamiliekilderError):
                    ist_home._validated_public_https_target(url)
                resolve.assert_not_called()
        with patch.object(ist_home.socket, 'getaddrinfo', return_value=[
                (2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(FamiliekilderError):
                ist_home._validated_public_https_target('https://example.test/file')
        with tempfile.TemporaryDirectory() as temporary:
            connection = Mock()
            response = Mock(status=302, headers={'Location': 'https://other.example/file'})
            with patch.object(ist_home, '_open_pinned_https', return_value=(connection, response)) as opened:
                with self.assertRaises(ApiHTTPError) as error:
                    ist_home.download('https://example.test/file', Path(temporary) / '.private' / 'file')
            self.assertEqual(error.exception.status_code, 302)
            opened.assert_called_once()
            response.read.assert_not_called()
            connection.close.assert_called_once()

    def test_unknown_length_is_allowed_but_size_limit_still_applies(self):
        import io
        from familiekilder.api_transport import save_download_response
        for limit in (2, 3):
            with tempfile.TemporaryDirectory() as temporary:
                response = Mock(headers={})
                response.read.side_effect = io.BytesIO(b'abc').read
                target = Path(temporary) / '.private' / 'attachment'
                if limit == 3:
                    result = save_download_response(response, target, max_bytes=limit)
                    self.assertEqual(result.byte_size, 3)
                else:
                    with self.assertRaises(ApiTransportError):
                        save_download_response(response, target, max_bytes=limit)
                    self.assertFalse(target.exists())
                response.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
