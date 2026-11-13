from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from familiekilder.setup_service import SetupService
from familiekilder.live_config import Config
from familiekilder.setup_web import SetupServer
from familiekilder.sources import REGISTRY


class SetupHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data"
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.service = SetupService(self.root, home=self.home)
        self.server = SetupServer(self.root, service=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, *, method="GET", value=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        request_headers = {"X-Setup-Token": self.server.token}
        body = None
        if value is not None:
            body = json.dumps(value)
            request_headers.update({"Content-Type": "application/json", "Origin": self.server.origin})
        request_headers.update(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_page_and_state_are_local_and_do_not_disclose_credentials(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, _, body = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["services"]), len(REGISTRY))
        self.assertNotIn(b"client_secret", body)
        self.assertFalse(self.root.exists())

    def test_dns_rebinding_cross_origin_and_missing_token_are_rejected(self):
        for headers in (
            {"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"},
            {"X-Setup-Token": "wrong"}, {"Sec-Fetch-Site": "cross-site"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/state", headers=headers)[0], 403)
                self.assertEqual(self.request("/api/connect", method="POST", value={"client": "codex"}, headers=headers)[0], 403)
        self.assertFalse((self.home / ".codex").exists())

    def test_client_registration_needs_explicit_valid_post(self):
        self.assertEqual(self.request("/api/connect?client=codex")[0], 404)
        self.assertFalse((self.home / ".codex").exists())
        status, _, _ = self.request("/api/connect", method="POST", value={"client": "codex"})
        self.assertEqual(status, 200)
        self.assertTrue((self.home / ".codex/config.toml").exists())

    def test_upload_failure_is_sanitized_and_temporary_file_removed(self):
        with patch("familiekilder.setup_service.auth_module") as module:
            module.return_value.configure_client.side_effect = ValueError("private-client-content")
            status, _, body = self.request("/api/client-file", method="POST", value={"source": "spond", "content": '{"fictional": true}'})
        self.assertEqual(status, 400)
        self.assertNotIn(b"private-client-content", body)
        self.assertEqual(list((self.root / ".private/setup-imports").iterdir()), [])

    def test_login_requires_consent_and_existing_session_confirmation(self):
        with patch("familiekilder.setup_service.auth_module") as module:
            module.return_value.auth_status.return_value = {"configured": True}
            module.return_value.session_path.return_value.exists.return_value = True
            self.assertEqual(self.request("/api/start", method="POST", value={"action": "login", "source": "spond"})[0], 400)
            self.assertEqual(self.request("/api/start", method="POST", value={"action": "login", "source": "spond", "consent": True})[0], 400)
        self.assertIsNone(self.service.job)

    def test_spond_prepares_its_public_web_configuration_without_a_client_file(self):
        from familiekilder.spond_auth import load_client_config

        with patch.object(self.service, "_login"):
            self.service.start("login", "spond", consent=True)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "done")
        config = load_client_config(self.root)
        self.assertEqual(config.app_version, "web")
        self.assertEqual(config.api_root, "https://api.spond.com/core/v1")
        self.assertEqual(Config.load(self.root).services, {"spond": frozenset()})

    def test_successful_login_enables_reads_only(self):
        with (
            patch("familiekilder.setup_service.auth_module") as module,
            patch.object(self.service, "_login"),
        ):
            module.return_value.auth_status.return_value = {"configured": True}
            module.return_value.session_path.return_value.exists.return_value = False
            status, _, _ = self.request("/api/start", method="POST", value={"action": "login", "source": "spond", "consent": True})
            self.assertEqual(status, 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "done")
        self.assertEqual(Config.load(self.root).services, {"spond": frozenset()})

    def test_cancelling_waiting_login_does_not_enable_source(self):
        def wait(job, **_):
            job.phase = "waiting"
            job.cancel.wait(3)

        with patch("familiekilder.setup_service.auth_module") as module, patch.object(self.service, "_login", side_effect=wait):
            module.return_value.auth_status.return_value = {"configured": True}
            module.return_value.session_path.return_value.exists.return_value = False
            self.service.start("login", "spond", consent=True)
            self.service.signal(self.service.job.id, cancel=True)
            self.service.worker.join(timeout=4)
        self.assertEqual(self.service.job.phase, "cancelled")
        self.assertEqual(Config.load(self.root).services, {})


if __name__ == "__main__":
    unittest.main()
