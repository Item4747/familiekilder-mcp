"""Traffic controls persist across processes without storing request data."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from familiekilder import (
    live_traffic as traffic,
    ist_home,
    ist_oauth,
    live_readers,
    vigilo,
)
from familiekilder.api_transport import ApiHTTPError, ApiRequestNotSent, ApiTransport
from familiekilder.private_state import atomic_write_private_json


class Clock:
    def __init__(self):
        self.value = 10000.0

    def __call__(self):
        return self.value

    def sleep(self, delay):
        self.value += delay


class TrafficTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.clock = Clock()
        self.pacer = traffic.Traffic(
            self.root, "ist-home", clock=self.clock, sleep=self.clock.sleep
        )
        self.calls = []

    def scope(self):
        return patch.object(traffic, "Traffic", return_value=self.pacer)

    def request(self, error=None):
        @traffic.paced_request
        def send():
            self.calls.append(self.clock())
            if error:
                raise error
            return "ok"

        return send()

    def test_spacing_persists_across_scopes_and_instances(self):
        with self.scope(), traffic.traffic_scope(self.root, "ist-home"):
            self.request()
            self.request()
        later = traffic.Traffic(
            self.root, "ist-home", clock=self.clock, sleep=self.clock.sleep
        )
        with (
            patch.object(traffic, "Traffic", return_value=later),
            traffic.traffic_scope(self.root, "ist-home"),
        ):
            self.request()
        self.assertEqual(self.calls, [10000, 10000.25, 10000.5])
        self.assertEqual(
            set(json.loads(later.path.read_text())),
            {"version", "last_request_at", "cooldown_until"},
        )
        self.assertEqual(later.path.stat().st_mode & 0o777, 0o600)

    def test_429_blocks_next_process_without_network_or_sleep(self):
        with self.scope(), traffic.traffic_scope(self.root, "ist-home"):
            with self.assertRaises(ApiHTTPError):
                self.request(ApiHTTPError(429, retry_after_seconds=3600))
        code = """
import sys
from familiekilder.live_traffic import traffic_scope, paced_request
from familiekilder.api_transport import ApiRequestNotSent
@paced_request
def request(): raise AssertionError('Network must not be called')
try:
    with traffic_scope(sys.argv[1], 'ist-home'): request()
except ApiRequestNotSent:
    print('blocked-before-network')
"""
        # Use a real future timestamp for the separate interpreter's wall clock.
        import time

        state = json.loads(self.pacer.path.read_text())
        state["cooldown_until"] = time.time() + 3600
        atomic_write_private_json(self.pacer.path, state)
        result = subprocess.run(
            [sys.executable, "-c", code, str(self.root)],
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "blocked-before-network")

    def test_other_source_and_root_are_independent_and_scope_restored(self):
        with self.scope(), traffic.traffic_scope(self.root, "ist-home"):
            with self.assertRaises(ApiHTTPError):
                self.request(ApiHTTPError(429))
            for source in ("spond", "hoopit"):
                with traffic.traffic_scope(self.root, source):
                    self.assertEqual(self.request(), "ok")
            with self.assertRaises(ApiRequestNotSent):
                self.request()
        self.assertEqual(self.request(), "ok")
        for source, root in (("vigilo", self.root), ("ist-home", self.root / "other")):
            root.mkdir(exist_ok=True)
            with traffic.traffic_scope(root, source):
                self.assertEqual(self.request(), "ok")

    def test_nested_scope_keeps_request_budget(self):
        self.pacer.used = traffic.MAX_REQUESTS - 1
        with self.scope(), traffic.traffic_scope(self.root, "ist-home"):
            self.request()
            with traffic.traffic_scope(self.root, "ist-home"):
                with self.assertRaises(ApiRequestNotSent):
                    self.request()
        self.assertEqual(len(self.calls), 1)

    def test_expired_cooldown_allows_one_request_without_replaying_write(self):
        with self.scope(), traffic.traffic_scope(self.root, "ist-home"):
            with self.assertRaises(ApiHTTPError):
                self.request(ApiHTTPError(429, retry_after_seconds=60))
            with self.assertRaises(ApiRequestNotSent):
                self.request()
            self.assertEqual(len(self.calls), 1)
            self.clock.value += 61
            self.assertEqual(self.request(), "ok")
        self.assertIsNone(json.loads(self.pacer.path.read_text())["cooldown_until"])

    def test_503_retry_after_and_success_cannot_shorten_cooldown(self):
        with self.scope(), traffic.traffic_scope(self.root, "ist-home"):
            with self.assertRaises(ApiHTTPError):
                self.request(ApiHTTPError(503, retry_after_seconds=7200))
            self.pacer.rejected(429, 60)
            self.assertEqual(
                json.loads(self.pacer.path.read_text())["cooldown_until"],
                self.clock() + 7200,
            )
            with self.assertRaises(ApiRequestNotSent):
                self.request()
        self.assertEqual(len(self.calls), 1)

    def test_bad_state_or_clock_stops_before_network(self):
        with self.scope(), traffic.traffic_scope(self.root, "ist-home"):
            for state in (
                {"version": 1},
                {"version": 1, "last_request_at": float("nan"), "cooldown_until": None},
            ):
                atomic_write_private_json(self.pacer.path, state)
                with self.assertRaises(ApiRequestNotSent):
                    self.request()
            atomic_write_private_json(
                self.pacer.path,
                {
                    "version": 1,
                    "last_request_at": self.clock() + 100,
                    "cooldown_until": None,
                },
            )
            with self.assertRaises(ApiRequestNotSent):
                self.request()
        self.assertEqual(self.calls, [])

    def test_http_error_does_not_automatically_retry_mutation(self):
        with self.scope(), traffic.traffic_scope(self.root, "ist-home"):
            with self.assertRaises(ApiHTTPError):
                self.request(ApiHTTPError(500))
        self.assertEqual(len(self.calls), 1)

    def test_api_transport_captures_429_for_auth_writes_and_reads(self):
        transport = ApiTransport(allowed_urls={"https://synthetic.test/token"})
        transport._opener = SimpleNamespace(
            open=lambda *a, **kw: (_ for _ in ()).throw(
                HTTPError(
                    "https://synthetic.test/token",
                    429,
                    "limited",
                    {"Retry-After": "7200"},
                    io.BytesIO(b"{}"),
                )
            )
        )
        with self.scope(), traffic.traffic_scope(self.root, "ist-home"):
            with self.assertRaises(ApiHTTPError):
                transport.post_form(
                    "https://synthetic.test/token", {"synthetic": "body"}
                )
            with self.assertRaises(ApiRequestNotSent):
                transport.get_json("https://synthetic.test/token")
        self.assertEqual(
            json.loads(self.pacer.path.read_text())["cooldown_until"],
            self.clock() + 7200,
        )
        self.assertNotIn("synthetic", self.pacer.path.read_text())

    def test_ist_graphql_preserves_retry_after(self):
        def error(*a, **kw):
            raise HTTPError(
                ist_home.ENDPOINT,
                429,
                "limited",
                {"Retry-After": "3600"},
                io.BytesIO(b"{}"),
            )

        with (
            self.scope(),
            traffic.traffic_scope(self.root, "ist-home"),
            patch.object(
                ist_home.urllib.request,
                "build_opener",
                return_value=SimpleNamespace(open=error),
            ),
        ):
            with self.assertRaises(ApiHTTPError):
                ist_home.graphql("synthetic", "query {}")
            with self.assertRaises(ApiRequestNotSent):
                ist_home.graphql("synthetic", "query {}")
        self.assertEqual(
            json.loads(self.pacer.path.read_text())["cooldown_until"],
            self.clock() + 3600,
        )

    def test_ist_oauth_preserves_retry_after_and_no_sent_classification(self):
        client = ist_oauth.UrllibOAuthTransport()
        calls = []

        def error(*a, **kw):
            calls.append(1)
            raise HTTPError(
                ist_oauth.TOKEN_ENDPOINT,
                429,
                "limited",
                {"Retry-After": "3600"},
                io.BytesIO(b"{}"),
            )

        client._opener = SimpleNamespace(open=error)
        with self.scope(), traffic.traffic_scope(self.root, "ist-home"):
            with self.assertRaises(ist_oauth._OAuthHTTPError):
                client.post_form(ist_oauth.TOKEN_ENDPOINT, {})
            with self.assertRaises(ApiRequestNotSent) as caught:
                client.post_form(ist_oauth.TOKEN_ENDPOINT, {})
        self.assertIs(caught.exception.request_sent, False)
        self.assertEqual(calls, [1])

    def test_ist_attachment_429_uses_same_cooldown_and_cleans_target(self):
        connection = SimpleNamespace(close=lambda: None)
        response = SimpleNamespace(status=429, headers={"Retry-After": "60"})
        target = self.root / ".private/tmp/file.pdf"
        with (
            self.scope(),
            traffic.traffic_scope(self.root, "ist-home"),
            patch.object(
                ist_home, "_open_pinned_https", return_value=(connection, response)
            ),
        ):
            with self.assertRaises(ApiHTTPError):
                ist_home.download("https://synthetic.test/file", target)
            with self.assertRaises(ApiRequestNotSent):
                self.request()
        self.assertFalse(target.exists())

    def test_provider_establishes_scope_before_authentication(self):
        marker = []

        def auth(root):
            marker.append(traffic._ACTIVE.get().source)
            raise ApiRequestNotSent("synthetic stop")

        with patch.object(vigilo, "access_token_for_use", side_effect=auth):
            with self.assertRaises(ApiRequestNotSent):
                with live_readers.provider(self.root, "vigilo"):
                    pass
        self.assertEqual(marker, ["vigilo"])
        self.assertIsNone(traffic._ACTIVE.get())
