from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from familiekilder import browser_bootstrap
from familiekilder.browser_bootstrap import (
    BrowserBootstrapError,
    HOOPIT_LOGIN_URL,
    HOOPIT_ORIGIN,
    SPOND_LOGIN_URL,
    SPOND_ORIGIN,
    bootstrap_hoopit,
    bootstrap_source,
    bootstrap_spond,
)
from familiekilder.hoopit_auth import (
    API_ROOT as HOOPIT_API_ROOT,
    FIREBASE_PROJECT_ID,
    client_config_path as hoopit_client_config_path,
)
from familiekilder.private_state import atomic_write_private_json
from familiekilder.spond_auth import (
    API_ROOT as SPOND_API_ROOT,
    client_config_path as spond_client_config_path,
)


SPOND_ACCESS = "spond-access-secret"
SPOND_REFRESH = "spond-refresh-secret"
HOOPIT_REFRESH = "hoopit-refresh-secret"
FIREBASE_KEY = "AIza" + "x" * 36


def _firebase_token(project_id: str = FIREBASE_PROJECT_ID) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "aud": project_id,
                    "iss": f"https://securetoken.google.com/{project_id}",
                    "sub": "fixture-user",
                },
                separators=(",", ":"),
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.fixture-signature"


class FakeCdp:
    def __init__(
        self,
        origin: str,
        *,
        runtime_value: Any = None,
        matching_targets: int = 1,
        indexed_entries: list[Mapping[str, Any]] | None = None,
        has_more: bool = False,
        record_value: Any = None,
        events: list[Mapping[str, Any]] | None = None,
    ) -> None:
        self.origin = origin
        self.runtime_value = runtime_value
        self.matching_targets = matching_targets
        self.indexed_entries = indexed_entries
        self.has_more = has_more
        self.record_value = record_value
        self.events = list(events or [])
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 20.0,
    ) -> Mapping[str, Any]:
        copied = dict(params or {})
        self.calls.append((method, copied, session_id))
        if method == "Target.getTargets":
            targets = [
                {
                    "type": "page",
                    "url": f"{self.origin}/signed-in/{index}",
                    "targetId": f"target-{index}",
                }
                for index in range(self.matching_targets)
            ]
            targets.append(
                {
                    "type": "page",
                    "url": "brave://newtab",
                    "targetId": "irrelevant",
                }
            )
            return {"targetInfos": targets}
        if method == "Target.attachToTarget":
            return {"sessionId": "session-1"}
        if method == "Runtime.evaluate":
            return {"result": {"type": "object", "value": self.runtime_value}}
        if method == "IndexedDB.enable":
            return {}
        if method in {"Network.enable", "Page.enable", "Target.setDiscoverTargets"}:
            return {}
        if method == "IndexedDB.requestData":
            return {
                "objectStoreDataEntries": list(self.indexed_entries or []),
                "hasMore": self.has_more,
            }
        if method == "Runtime.callFunctionOn":
            return {"result": {"type": "object", "value": self.record_value}}
        raise AssertionError(f"unexpected fake CDP method: {method}")

    def wait_for_event(
        self, methods: frozenset[str], *, timeout: float
    ) -> Mapping[str, Any] | None:
        while self.events:
            event = self.events.pop(0)
            if event.get("method") in methods:
                return event
        return None


class FakeBrowser:
    def __init__(self, cdp: FakeCdp, profile: Path, external: Path | None = None) -> None:
        self.cdp = cdp
        self.profile = profile
        self.closed = False
        nested = profile / "Bootstrap" / "IndexedDB"
        nested.mkdir(parents=True)
        (nested / "browser-state").write_bytes(b"not-a-secret")
        if external is not None:
            (profile / "outside-link").symlink_to(external)

    def close(self) -> None:
        self.closed = True


class BrowserBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _configure_hoopit(self) -> None:
        atomic_write_private_json(
            hoopit_client_config_path(self.root),
            {
                "api_root": HOOPIT_API_ROOT,
                "firebase_api_key": FIREBASE_KEY,
                "firebase_project_id": FIREBASE_PROJECT_ID,
                "app_version": "Web test",
                "attachment_hosts": [],
            },
        )

    def _configure_spond(self) -> None:
        atomic_write_private_json(
            spond_client_config_path(self.root),
            {
                "api_root": SPOND_API_ROOT,
                "app_version": "4.62.0",
                "attachment_hosts": [],
            },
        )

    def _assert_bootstrap_empty(self) -> None:
        base = self.root / ".private" / "browser-bootstrap"
        self.assertTrue(base.is_dir())
        self.assertEqual(list(base.iterdir()), [])
        self.assertEqual(stat.S_IMODE(base.stat().st_mode), 0o700)

    def test_cdp_framer_handles_partial_and_multiple_nul_frames(self) -> None:
        framer = browser_bootstrap._CdpFramer()
        first = browser_bootstrap._CdpFramer.encode({"id": 1, "result": {}})
        second = browser_bootstrap._CdpFramer.encode(
            {"method": "Target.targetInfoChanged", "params": {}}
        )

        self.assertEqual(framer.feed(first[:5]), [])
        self.assertEqual(
            framer.feed(first[5:] + second),
            [
                {"id": 1, "result": {}},
                {"method": "Target.targetInfoChanged", "params": {}},
            ],
        )

    def test_pipe_client_reads_thousands_of_fragments_without_recursion(self):
        message = browser_bootstrap._CdpFramer.encode({'id': 1, 'result': {'value': 'x' * 2000}})
        client = browser_bootstrap._PipeCdpClient(10, 11)
        with (
            patch.object(browser_bootstrap.select, 'select', return_value=([11], [], [])),
            patch.object(
                browser_bootstrap.os, 'read', side_effect=[bytes([byte]) for byte in message]
            ),
            patch.object(browser_bootstrap.time, 'monotonic', return_value=1),
        ):
            result = client._next_message(2)
        self.assertEqual(result['result']['value'], 'x' * 2000)

    def test_pipe_client_ignores_events_and_matches_one_response(self) -> None:
        request_read, client_write = os.pipe()
        client_read, response_write = os.pipe()
        client = browser_bootstrap._PipeCdpClient(client_write, client_read)

        def server() -> None:
            request = bytearray()
            while b"\0" not in request:
                request.extend(os.read(request_read, 4096))
            decoded = json.loads(bytes(request).split(b"\0", 1)[0])
            event = browser_bootstrap._CdpFramer.encode(
                {"method": "Target.targetCreated", "params": {}}
            )
            response = browser_bootstrap._CdpFramer.encode(
                {"id": decoded["id"], "result": {"ok": True}}
            )
            os.write(response_write, event + response[:3])
            os.write(response_write, response[3:])
            os.close(request_read)
            os.close(response_write)

        worker = threading.Thread(target=server)
        worker.start()
        try:
            self.assertEqual(client.call("Browser.getVersion"), {"ok": True})
        finally:
            client.close()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_brave_arguments_use_private_pipe_and_disable_persistent_features(self) -> None:
        executable = Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
        profile = self.root / ".private" / "browser-bootstrap" / f"run-{'a' * 32}"

        arguments = browser_bootstrap._brave_arguments(executable, profile, SPOND_LOGIN_URL)

        self.assertIn("--remote-debugging-pipe", arguments)
        self.assertFalse(
            any(argument.startswith("--remote-debugging-port") for argument in arguments)
        )
        self.assertIn(f"--user-data-dir={profile}", arguments)
        self.assertIn("--profile-directory=Bootstrap", arguments)
        self.assertIn("--disable-sync", arguments)
        self.assertIn("--disable-save-password-bubble", arguments)
        self.assertIn("--disable-password-generation", arguments)
        self.assertTrue(
            any(
                argument.startswith("--disable-features=")
                and "PasswordManagerEnabled" in argument
                and "BraveSync" in argument
                for argument in arguments
            )
        )
        self.assertFalse(any("password-store" in argument for argument in arguments))
        self.assertEqual(arguments[-1], SPOND_LOGIN_URL)

    def test_hosted_browser_uses_app_mode_without_an_address_bar(self) -> None:
        with patch.dict('os.environ', {'FAMILIEKILDER_HOSTED': '1'}):
            arguments = browser_bootstrap._brave_arguments(
                Path('/opt/brave'), self.root, SPOND_LOGIN_URL
            )
        self.assertIn('--app=' + SPOND_LOGIN_URL, arguments)
        self.assertIn('--window-size=1280,800', arguments)
        self.assertIn('--window-position=0,0', arguments)
        self.assertFalse(any(arg.startswith('--force-device-scale-factor') for arg in arguments))
        self.assertNotIn('--start-maximized', arguments)
        self.assertNotIn('--new-window', arguments)
        self.assertNotIn('--no-sandbox', arguments)
        self.assertIn('--remote-debugging-pipe', arguments)

    def test_hosted_window_size_rejects_arbitrary_values_without_affecting_local_browser(
        self,
    ) -> None:
        for value in ('', '99999,99999', '420,840', '500x840', 'private-value'):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {
                        'FAMILIEKILDER_HOSTED': '1',
                        'FAMILIEKILDER_HOSTED_BROWSER_SIZE': value,
                    },
                ),
            ):
                with self.assertRaisesRegex(BrowserBootstrapError, 'Ugyldig størrelse'):
                    browser_bootstrap._brave_arguments(
                        Path('/opt/brave'), self.root, SPOND_LOGIN_URL
                    )
                os.environ['FAMILIEKILDER_HOSTED'] = '0'
                arguments = browser_bootstrap._brave_arguments(
                    Path('/opt/brave'), self.root, SPOND_LOGIN_URL
                )
                self.assertIn('--new-window', arguments)
                self.assertEqual(arguments[-1], SPOND_LOGIN_URL)
                self.assertFalse(any(arg.startswith('--window-') for arg in arguments))

    def test_oauth_callback_is_captured_without_persisting_profile(self) -> None:
        callback = "app://ch-parent-android.vigilo.no?code=secret&state=private"
        cdp = FakeCdp(
            "https://login.example",
            events=[
                {
                    "method": "Page.frameRequestedNavigation",
                    "params": {"url": "https://irrelevant.example/"},
                },
                {
                    "method": "Page.frameRequestedNavigation",
                    "params": {"url": callback},
                },
            ],
        )
        browsers: list[FakeBrowser] = []

        def factory(profile: Path, start_url: str, brave_path: Any) -> FakeBrowser:
            self.assertEqual(start_url, "https://login.example/start")
            browser = FakeBrowser(cdp, profile)
            browsers.append(browser)
            return browser

        captured = browser_bootstrap.capture_oauth_callback(
            self.root,
            authorization_url="https://login.example/start",
            expected_redirect_uri="app://ch-parent-android.vigilo.no",
            browser_factory=factory,
        )

        self.assertEqual(captured, callback)
        self.assertTrue(browsers[0].closed)
        self._assert_bootstrap_empty()

    def test_cancel_oauth_closes_browser_and_removes_temporary_profile(self) -> None:
        browsers = []

        def factory(profile, start_url, brave_path):
            browser = FakeBrowser(FakeCdp("https://login.example"), profile)
            browsers.append(browser)
            return browser

        with self.assertRaises(browser_bootstrap.BrowserBootstrapError):
            browser_bootstrap.capture_oauth_callback(
                self.root,
                authorization_url="https://login.example/start",
                expected_redirect_uri="app://ch-parent-android.vigilo.no",
                browser_factory=factory,
                cancelled=lambda: True,
            )
        self.assertTrue(browsers[0].closed)
        self._assert_bootstrap_empty()

    def test_blocked_navigation_diagnostic_does_not_change_callback(self) -> None:
        callback = 'com.cgi.oslomelding.foresatte://oauth?code=fixture'
        cdp = FakeCdp(
            'https://login.example',
            events=[
                {
                    'method': 'Page.frameNavigated',
                    'params': {
                        'frame': {
                            'url': 'chrome-error://chromewebdata/',
                            'unreachableUrl': 'https://example.test/private?state=fixture',
                        }
                    },
                },
                {'method': 'Page.frameRequestedNavigation', 'params': {'url': callback}},
            ],
        )

        def factory(profile, start_url, brave_path):
            return FakeBrowser(cdp, profile)

        with self.assertLogs('familiekilder.browser_bootstrap', level='WARNING') as captured:
            result = browser_bootstrap.capture_oauth_callback(
                self.root,
                authorization_url='https://login.example/start',
                expected_redirect_uri='com.cgi.oslomelding.foresatte://oauth',
                browser_factory=factory,
            )
        self.assertEqual(result, callback)
        self.assertIn('host=example.test', captured.output[0])
        self.assertNotIn('fixture', captured.output[0])
        self._assert_bootstrap_empty()

    def test_oauth_callback_is_captured_from_https_redirect_response(self) -> None:
        callback = "app://ch-parent-android.vigilo.no?code=secret&state=private"
        cdp = FakeCdp(
            "https://login.example",
            events=[
                {
                    "method": "Network.responseReceived",
                    "params": {
                        "response": {
                            "url": "https://auth.example/signin-idporten",
                            "headers": {"Location": callback},
                        }
                    },
                }
            ],
        )
        browsers: list[FakeBrowser] = []

        def factory(profile: Path, start_url: str, brave_path: Any) -> FakeBrowser:
            browser = FakeBrowser(cdp, profile)
            browsers.append(browser)
            return browser

        captured = browser_bootstrap.capture_oauth_callback(
            self.root,
            authorization_url="https://login.example/start",
            expected_redirect_uri="app://ch-parent-android.vigilo.no",
            browser_factory=factory,
        )

        self.assertEqual(captured, callback)
        self.assertIn(
            ("Network.enable", {}, "session-1"),
            cdp.calls,
        )
        self.assertTrue(browsers[0].closed)
        self._assert_bootstrap_empty()

    def test_oauth_callback_rejects_ambiguous_location_headers(self) -> None:
        self.assertIsNone(
            browser_bootstrap._header_location(
                {
                    "Location": "app://ch-parent-android.vigilo.no?code=one",
                    "location": "app://ch-parent-android.vigilo.no?code=two",
                }
            )
        )

    def test_oauth_callback_rejects_wrong_redirect_without_exposing_it(self) -> None:
        self.assertFalse(
            browser_bootstrap._matches_callback(
                "app://wrong.example?code=secret",
                "app://ch-parent-android.vigilo.no",
            )
        )

    def test_oauth_callback_accepts_provider_root_slash_but_no_other_path(self) -> None:
        registered = "app://ch-parent-android.vigilo.no"
        self.assertTrue(
            browser_bootstrap._matches_callback(
                f"{registered}/?code=one&session_state=two&state=three", registered
            )
        )
        self.assertFalse(
            browser_bootstrap._matches_callback(f"{registered}/other?code=one", registered)
        )
        self.assertFalse(
            browser_bootstrap._matches_callback(
                "app://ch-parent-android.vigilo.no/?code=one",
                "app://ch-parent-android.vigilo.no/expected",
            )
        )

    def test_launcher_maps_devtools_pipe_to_child_fd_three_and_four(self) -> None:
        profile = browser_bootstrap._create_run_directory(self.root)
        child = r"""
import json
import os

pending = bytearray()
while True:
    chunk = os.read(3, 4096)
    if not chunk:
        break
    pending.extend(chunk)
    while b'\0' in pending:
        raw, _, remainder = pending.partition(b'\0')
        pending = bytearray(remainder)
        request = json.loads(raw)
        response = {'id': request['id'], 'result': {'pipe_ok': True}}
        os.write(4, json.dumps(response, separators=(',', ':')).encode() + b'\0')
        if request['method'] == 'Browser.close':
            raise SystemExit(0)
"""
        with (
            patch.object(
                browser_bootstrap,
                "_official_brave_path",
                return_value=Path(sys.executable),
            ),
            patch.object(
                browser_bootstrap,
                "_brave_arguments",
                return_value=[sys.executable, "-c", child],
            ),
        ):
            handle = browser_bootstrap._launch_brave(profile, SPOND_LOGIN_URL, None)
            try:
                self.assertEqual(handle.cdp.call("Browser.getVersion"), {"pipe_ok": True})
            finally:
                handle.close()
                browser_bootstrap._cleanup_run_directory(self.root, profile)

        self._assert_bootstrap_empty()

    def test_malformed_cdp_frame_does_not_echo_secret(self) -> None:
        secret = "do-not-echo-this-token"
        framer = browser_bootstrap._CdpFramer()

        with self.assertRaises(BrowserBootstrapError) as raised:
            framer.feed(f'{{"token":"{secret}",}}\0'.encode())

        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, repr(raised.exception))

    def test_spond_bootstrap_reads_only_exact_keys_closes_then_imports_and_cleans(self) -> None:
        self._configure_spond()
        cdp = FakeCdp(
            SPOND_ORIGIN,
            runtime_value={
                "token": SPOND_ACCESS,
                "tokenExpiration": "2033-05-18T04:33:20+00:00",
                "refreshToken": SPOND_REFRESH,
                "refreshTokenExpiration": "2033-05-19T04:33:20+00:00",
            },
        )
        external = self.root / "must-survive"
        external.write_text("outside", encoding="utf-8")
        holder: dict[str, Any] = {}

        def factory(profile: Path, url: str, brave_path: Path | str | None) -> FakeBrowser:
            self.assertEqual(url, SPOND_LOGIN_URL)
            self.assertIsNone(brave_path)
            self.assertEqual(
                profile.parent,
                (self.root / ".private" / "browser-bootstrap").resolve(),
            )
            self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o700)
            browser = FakeBrowser(cdp, profile, external)
            holder["browser"] = browser
            return browser

        def importer(
            root: Path,
            session_file: Path,
            *,
            replace_existing: bool,
        ) -> Mapping[str, Any]:
            browser = holder["browser"]
            self.assertTrue(browser.closed)
            self.assertEqual(root, self.root)
            self.assertTrue(replace_existing)
            self.assertEqual(stat.S_IMODE(session_file.stat().st_mode), 0o600)
            imported = json.loads(session_file.read_text(encoding="utf-8"))
            self.assertEqual(
                imported,
                {
                    "token": SPOND_ACCESS,
                    "tokenExpiration": "2033-05-18T04:33:20+00:00",
                    "refreshToken": SPOND_REFRESH,
                    "refreshTokenExpiration": "2033-05-19T04:33:20+00:00",
                },
            )
            return {
                "configured": True,
                "authenticated": True,
                "remote_verified": True,
                "expires_at": "2033-05-18T04:33:20+00:00",
                "refresh_expires_at": "2033-05-19T04:33:20+00:00",
                "attachment_hosts": 0,
                "refresh_marker_cleanup_pending": False,
                "unexpected_secret": SPOND_ACCESS,
            }

        prompts: list[str] = []
        result = bootstrap_spond(
            self.root,
            replace_existing=True,
            wait_for_user=prompts.append,
            browser_factory=factory,
            importer=importer,
        )

        self.assertEqual(result["source"], "spond")
        self.assertNotIn("unexpected_secret", result)
        self.assertNotIn(SPOND_ACCESS, repr(result))
        self.assertEqual(len(prompts), 1)
        self.assertTrue(external.exists())
        self._assert_bootstrap_empty()
        evaluate = next(call for call in cdp.calls if call[0] == "Runtime.evaluate")
        expression = evaluate[1]["expression"]
        for key in browser_bootstrap._SPOND_STORAGE_KEYS:
            self.assertIn(f'"{key}"', expression)
        self.assertNotIn("Object.keys", expression)
        self.assertNotIn("cookie", expression.lower())
        self.assertEqual(evaluate[2], "session-1")

    def test_spond_bootstrap_rejects_missing_or_malformed_profile_before_browser(self) -> None:
        calls: list[Path] = []

        def factory(profile: Path, url: str, path: Path | str | None) -> FakeBrowser:
            calls.append(profile)
            raise AssertionError("browser must not start")

        with self.assertRaisesRegex(BrowserBootstrapError, "providerprofilen"):
            bootstrap_spond(
                self.root,
                wait_for_user=lambda _prompt: None,
                browser_factory=factory,
                importer=lambda *args, **kwargs: {},
            )
        self.assertEqual(calls, [])
        self.assertFalse((self.root / ".private" / "browser-bootstrap").exists())

        atomic_write_private_json(
            spond_client_config_path(self.root),
            {
                "api_root": "https://attacker.invalid/core/v1",
                "app_version": "4.62.0",
                "attachment_hosts": [],
            },
        )
        with self.assertRaisesRegex(BrowserBootstrapError, "providerprofilen"):
            bootstrap_spond(
                self.root,
                wait_for_user=lambda _prompt: None,
                browser_factory=factory,
                importer=lambda *args, **kwargs: {},
            )
        self.assertEqual(calls, [])
        self.assertFalse((self.root / ".private" / "browser-bootstrap").exists())

    def test_spond_unknown_layout_fails_closed_redacts_and_cleans(self) -> None:
        self._configure_spond()
        secret = "layout-secret-must-not-escape"
        cdp = FakeCdp(
            SPOND_ORIGIN,
            runtime_value={
                "token": secret,
                "tokenExpiration": "2033-05-18T04:33:20+00:00",
                "refreshToken": SPOND_REFRESH,
                "refreshTokenExpiration": "2033-05-19T04:33:20+00:00",
                "password": "must-never-be-read",
            },
        )
        holder: dict[str, FakeBrowser] = {}

        def factory(profile: Path, url: str, brave_path: Path | str | None) -> FakeBrowser:
            browser = FakeBrowser(cdp, profile)
            holder["browser"] = browser
            return browser

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(BrowserBootstrapError) as raised:
                bootstrap_spond(
                    self.root,
                    wait_for_user=lambda prompt: None,
                    browser_factory=factory,
                    importer=lambda *args, **kwargs: {},
                )

        self.assertTrue(holder["browser"].closed)
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, repr(raised.exception))
        self.assertNotIn(secret, output.getvalue())
        self._assert_bootstrap_empty()

    def test_spond_duplicate_matching_page_fails_before_storage_read(self) -> None:
        self._configure_spond()
        cdp = FakeCdp(SPOND_ORIGIN, matching_targets=2)

        with self.assertRaises(BrowserBootstrapError):
            bootstrap_spond(
                self.root,
                wait_for_user=lambda prompt: None,
                browser_factory=lambda profile, url, path: FakeBrowser(cdp, profile),
                importer=lambda *args, **kwargs: {},
            )

        self.assertEqual([call[0] for call in cdp.calls], ["Target.getTargets"])
        self._assert_bootstrap_empty()

    def test_hoopit_bootstrap_reads_one_exact_provider_bound_record(self) -> None:
        self._configure_hoopit()
        id_token = _firebase_token()
        record_key = f"firebase:authUser:{FIREBASE_KEY}:[DEFAULT]"
        cdp = FakeCdp(
            HOOPIT_ORIGIN,
            indexed_entries=[
                {
                    "key": {"type": "string", "value": record_key},
                    "primaryKey": {"type": "string", "value": record_key},
                    "value": {"type": "object", "objectId": "record-object-1"},
                }
            ],
            record_value={
                "apiKey": FIREBASE_KEY,
                "accessToken": id_token,
                "refreshToken": HOOPIT_REFRESH,
            },
        )
        holder: dict[str, Any] = {}

        def factory(profile: Path, url: str, brave_path: Path | str | None) -> FakeBrowser:
            self.assertEqual(url, HOOPIT_LOGIN_URL)
            browser = FakeBrowser(cdp, profile)
            holder["browser"] = browser
            return browser

        def importer(
            root: Path,
            session_file: Path,
            *,
            replace_existing: bool,
        ) -> Mapping[str, Any]:
            self.assertTrue(holder["browser"].closed)
            self.assertFalse(replace_existing)
            self.assertEqual(stat.S_IMODE(session_file.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(session_file.read_text(encoding="utf-8")),
                {"id_token": id_token, "refresh_token": HOOPIT_REFRESH},
            )
            return {
                "configured": True,
                "authenticated": True,
                "firebase_verified": True,
                "expires_at": "2033-05-18T04:33:20+00:00",
                "attachment_hosts": 0,
                "id_token": id_token,
            }

        result = bootstrap_hoopit(
            self.root,
            wait_for_user=lambda prompt: None,
            browser_factory=factory,
            importer=importer,
        )

        self.assertEqual(
            result,
            {
                "source": "hoopit",
                "attachment_hosts": 0,
                "authenticated": True,
                "configured": True,
                "expires_at": "2033-05-18T04:33:20+00:00",
                "firebase_verified": True,
            },
        )
        self.assertNotIn(id_token, repr(result))
        request = next(call for call in cdp.calls if call[0] == "IndexedDB.requestData")
        self.assertEqual(request[1]["securityOrigin"], HOOPIT_ORIGIN)
        self.assertEqual(request[1]["databaseName"], "firebaseLocalStorageDb")
        self.assertEqual(request[1]["objectStoreName"], "firebaseLocalStorage")
        self.assertEqual(request[1]["pageSize"], 2)
        self.assertEqual(request[1]["keyRange"]["lower"]["string"], record_key)
        function_call = next(call for call in cdp.calls if call[0] == "Runtime.callFunctionOn")
        declaration = function_call[1]["functionDeclaration"]
        self.assertNotIn("Object.keys", declaration)
        self.assertNotIn("cookie", declaration.lower())
        self.assertNotIn("password", declaration.lower())
        self._assert_bootstrap_empty()

    def test_hoopit_duplicate_record_and_wrong_project_fail_closed(self) -> None:
        self._configure_hoopit()
        key = f"firebase:authUser:{FIREBASE_KEY}:[DEFAULT]"
        entry = {
            "key": {"value": key},
            "primaryKey": {"value": key},
            "value": {"objectId": "record"},
        }
        duplicate = FakeCdp(
            HOOPIT_ORIGIN,
            indexed_entries=[entry, entry],
            record_value={},
        )
        with self.assertRaises(BrowserBootstrapError):
            bootstrap_hoopit(
                self.root,
                wait_for_user=lambda prompt: None,
                browser_factory=lambda profile, url, path: FakeBrowser(duplicate, profile),
                importer=lambda *args, **kwargs: {},
            )
        self._assert_bootstrap_empty()

        wrong_token = _firebase_token("wrong-project")
        wrong_project = FakeCdp(
            HOOPIT_ORIGIN,
            indexed_entries=[entry],
            record_value={
                "apiKey": FIREBASE_KEY,
                "accessToken": wrong_token,
                "refreshToken": HOOPIT_REFRESH,
            },
        )
        with self.assertRaises(BrowserBootstrapError) as raised:
            bootstrap_hoopit(
                self.root,
                wait_for_user=lambda prompt: None,
                browser_factory=lambda profile, url, path: FakeBrowser(wrong_project, profile),
                importer=lambda *args, **kwargs: {},
            )
        self.assertNotIn(wrong_token, str(raised.exception))
        self.assertNotIn(HOOPIT_REFRESH, repr(raised.exception))
        self._assert_bootstrap_empty()

    def test_import_failure_is_redacted_and_profile_is_removed(self) -> None:
        self._configure_spond()
        cdp = FakeCdp(
            SPOND_ORIGIN,
            runtime_value={
                "token": SPOND_ACCESS,
                "tokenExpiration": "2033-05-18T04:33:20+00:00",
                "refreshToken": SPOND_REFRESH,
                "refreshTokenExpiration": "2033-05-19T04:33:20+00:00",
            },
        )

        def failing_importer(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
            raise RuntimeError(f"provider echoed {SPOND_ACCESS}")

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(BrowserBootstrapError) as raised:
                bootstrap_spond(
                    self.root,
                    wait_for_user=lambda prompt: None,
                    browser_factory=lambda profile, url, path: FakeBrowser(cdp, profile),
                    importer=failing_importer,
                )

        self.assertNotIn(SPOND_ACCESS, str(raised.exception))
        self.assertNotIn(SPOND_ACCESS, repr(raised.exception))
        self.assertNotIn(SPOND_ACCESS, output.getvalue())
        self._assert_bootstrap_empty()

    def test_cleanup_refuses_unmarked_or_out_of_scope_directory(self) -> None:
        run_path = browser_bootstrap._create_run_directory(self.root)
        (run_path / browser_bootstrap._OWNER_MARKER).unlink()
        with self.assertRaises(BrowserBootstrapError):
            browser_bootstrap._cleanup_run_directory(self.root, run_path)
        self.assertTrue(run_path.exists())

        outside = self.root / ".private" / f"run-{'a' * 32}"
        with self.assertRaises(BrowserBootstrapError):
            browser_bootstrap._cleanup_run_directory(self.root, outside)

    def test_dispatch_rejects_unknown_source_without_creating_private_state(self) -> None:
        with self.assertRaises(BrowserBootstrapError):
            bootstrap_source(self.root, "unknown")
        self.assertFalse((self.root / ".private").exists())


if __name__ == "__main__":
    unittest.main()
