from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from familiekilder import flyt_auth
from familiekilder.client_defaults import default_client_config
from familiekilder.connection_status import auth_module
from familiekilder.setup_service import SetupError, SetupService
from familiekilder.live_config import Config
from familiekilder.private_state import atomic_write_private_bytes, ensure_private_directory
from familiekilder.setup_web import SetupServer
from familiekilder.source_adapter import SourceUnavailable
from familiekilder.sources import REGISTRY
from familiekilder_testing.flyt import FakeMunicipalityTransport

AUTOMATIC_SOURCES = [source for source in REGISTRY if default_client_config(source) is not None]


def default_strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from default_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from default_strings(item)
    elif isinstance(value, str):
        yield value


class SetupServerCase(unittest.TestCase):
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


class SetupHttpTests(SetupServerCase):
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

    def test_state_says_how_each_service_is_set_up_without_its_values(self):
        status, _, body = self.request("/api/state")
        self.assertEqual(status, 200)
        services = {row["id"]: row for row in json.loads(body)["services"]}
        # A source whose default is withdrawn is shown as unavailable.
        expected = {source: "unavailable" for source in services}
        expected.update({source: "automatic" for source in AUTOMATIC_SOURCES})
        expected.update({"flyt": "municipality", "gmail": "file"})
        self.assertEqual({source: row["setup"] for source, row in services.items()}, expected)
        self.assertIn("spond", AUTOMATIC_SOURCES)
        for row in services.values():
            self.assertNotIn("automatic_setup", row)
        text = body.decode()
        for source in AUTOMATIC_SOURCES:
            for value in default_strings(default_client_config(source)):
                if len(value) >= 12:
                    self.assertNotIn(value, text)

    def test_a_source_without_built_in_setup_is_unavailable_in_the_wizard(self):
        with patch("familiekilder.setup_service.default_client_config", return_value=None):
            rows = {row["id"]: row for row in self.service.state()["services"]}
            self.assertEqual(rows["vigilo"]["setup"], "unavailable")
            status, _, body = self.request(
                "/api/start", method="POST", value={"action": "login", "source": "vigilo", "consent": True})
        self.assertEqual(status, 400)
        self.assertIn("kan ikke settes opp i veiviseren", json.loads(body)["error"])
        self.assertIsNone(self.service.job)

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
            status, _, body = self.request("/api/client-file", method="POST", value={"source": "gmail", "content": '{"fictional": true}'})
        self.assertEqual(status, 400)
        self.assertNotIn(b"private-client-content", body)
        self.assertEqual(list((self.root / ".private/setup-imports").iterdir()), [])

    def test_client_file_is_only_for_services_set_up_with_a_file(self):
        content = json.dumps({"installed": {
            "client_id": "fictional-desktop.apps.googleusercontent.com", "client_secret": "fictional-secret",
        }})
        for source in [*AUTOMATIC_SOURCES, "flyt"]:
            with self.subTest(source=source):
                status, _, _ = self.request("/api/client-file", method="POST", value={"source": source, "content": content})
                self.assertEqual(status, 400)
                self.assertFalse(auth_module(source).auth_status(self.root)["configured"])
        status, _, _ = self.request("/api/client-file", method="POST", value={"source": "gmail", "content": content})
        self.assertEqual(status, 200)
        self.assertTrue(auth_module("gmail").auth_status(self.root)["configured"])

    def test_ist_home_app_import_is_no_longer_a_wizard_action(self):
        status, _, body = self.request("/api/start", method="POST", value={"action": "ist-client"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "Ukjent oppsettshandling.")
        self.assertIsNone(self.service.job)

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

    def test_login_without_setup_imports_the_built_in_setup(self):
        with patch.object(self.service, "_login") as login:
            for source in AUTOMATIC_SOURCES:
                with self.subTest(source=source):
                    module = auth_module(source)
                    self.assertFalse(module.auth_status(self.root)["configured"])
                    status, _, _ = self.request(
                        "/api/start", method="POST", value={"action": "login", "source": source, "consent": True})
                    self.assertEqual(status, 200)
                    self.service.worker.join(timeout=3)
                    self.assertEqual(self.service.job.phase, "done")
                    self.assertTrue(module.auth_status(self.root)["configured"])
                    stored = module.load_client_config(self.root).as_private_json()
                    for key, value in default_client_config(source).items():
                        expected = sorted(value) if isinstance(value, list) else value
                        actual = sorted(stored[key]) if isinstance(stored[key], list) else stored[key]
                        self.assertEqual(actual, expected, key)
        self.assertEqual(login.call_count, len(AUTOMATIC_SOURCES))
        self.assertEqual(list((self.root / ".private/setup-imports").iterdir()), [])

    def test_existing_setup_is_never_overwritten_by_the_built_in_setup(self):
        from familiekilder.spond_auth import load_client_config

        stored = {**default_client_config("spond"), "app_version": "fictional-9.9"}
        self.root.mkdir()
        client_file = Path(self.temporary.name) / ".private/spond-client.json"
        atomic_write_private_bytes(client_file, json.dumps(stored).encode())
        auth_module("spond").configure_client(self.root, client_file)
        with patch.object(self.service, "_login"):
            self.service.start("login", "spond", consent=True)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "done")
        self.assertEqual(load_client_config(self.root).app_version, "fictional-9.9")

    def test_unreadable_stored_setup_is_not_replaced_by_the_built_in_setup(self):
        module = auth_module("vigilo")
        path = module.client_config_path(self.root)
        self.root.mkdir()
        ensure_private_directory(self.root / ".private")
        ensure_private_directory(path.parent)
        atomic_write_private_bytes(path, b"{not json")
        self.assertFalse(module.auth_status(self.root)["configured"])
        with patch.object(self.service, "_login") as login:
            with self.assertRaisesRegex(SetupError, "kan ikke leses"):
                self.service.start("login", "vigilo", consent=True)
        login.assert_not_called()
        self.assertEqual(path.read_bytes(), b"{not json")

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


class FlytSetupTests(SetupServerCase):
    """Flyt asks for the municipality; the public list comes from a fake transport."""

    def setUp(self):
        super().setUp()
        self.held_lock = []
        self.transport = FakeMunicipalityTransport(
            on_call=lambda: self.held_lock.append(self.service.lock._is_owned()))
        transport_patch = patch.object(flyt_auth, "default_transport", return_value=self.transport)
        transport_patch.start()
        self.addCleanup(transport_patch.stop)

    def start_flyt(self, **fields):
        value = {"action": "login", "source": "flyt", "consent": True, **fields}
        return self.request("/api/start", method="POST", value=value)

    def test_login_without_municipality_is_rejected(self):
        for fields in ({}, {"municipality": ""}, {"municipality": "   "}):
            with self.subTest(fields=fields):
                status, _, body = self.start_flyt(**fields)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"], "Velg kommune før du logger inn.")
        self.assertIsNone(self.service.job)
        self.assertEqual(self.transport.calls, [])

    def test_invalid_municipality_values_are_rejected_before_any_lookup(self):
        for value in (912345678, ["912345678"], "9" * 201):
            with self.subTest(value=value):
                self.assertEqual(self.start_flyt(municipality=value)[0], 400)
        status, _, _ = self.request("/api/start", method="POST", value={
            "action": "login", "source": "spond", "consent": True, "municipality": "912345678"})
        self.assertEqual(status, 400)
        self.assertIsNone(self.service.job)
        self.assertEqual(self.transport.calls, [])

    def test_municipality_is_stored_in_the_worker_before_the_login(self):
        with patch.object(self.service, "_login") as login:
            login.side_effect = lambda job, **_: self.assertTrue(
                auth_module("flyt").auth_status(self.root)["configured"])
            status, _, _ = self.start_flyt(municipality="923456789")
            self.assertEqual(status, 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "done")
        self.assertEqual(login.call_count, 1)
        self.assertEqual(self.transport.threads, [self.service.worker])
        self.assertEqual(self.held_lock, [False])
        config = flyt_auth.load_client_config(self.root)
        self.assertEqual(config.municipality.organization_number, "923456789")
        self.assertEqual(Config.load(self.root).services, {"flyt": frozenset()})

    def test_unknown_municipality_gives_its_own_message(self):
        with patch.object(self.service, "_login") as login:
            self.assertEqual(self.start_flyt(municipality="999999999")[0], 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "error")
        self.assertIn("Fant ingen kommune", self.service.job.message)
        self.assertNotIn("Steget kunne ikke fullføres", self.service.job.message)
        login.assert_not_called()
        self.assertFalse(auth_module("flyt").auth_status(self.root)["configured"])
        self.assertEqual(Config.load(self.root).services, {})

    def test_unreachable_municipality_list_gives_its_own_message(self):
        self.transport.failures.append(SourceUnavailable("fictional outage"))
        with patch.object(self.service, "_login") as login:
            self.assertEqual(self.start_flyt(municipality="923456789")[0], 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "error")
        self.assertIn("Kommunelisten fra Flyt kunne ikke hentes", self.service.job.message)
        login.assert_not_called()

    def test_a_stored_municipality_is_kept(self):
        self.root.mkdir()
        flyt_auth.configure_municipality(self.root, "912345678", transport=self.transport)
        calls = len(self.transport.calls)
        with patch.object(self.service, "_login"):
            self.assertEqual(self.start_flyt()[0], 200)
            self.service.worker.join(timeout=3)
            self.assertEqual(self.service.job.phase, "done")
            self.assertEqual(self.start_flyt(municipality="923456789")[0], 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(len(self.transport.calls), calls)
        config = flyt_auth.load_client_config(self.root)
        self.assertEqual(config.municipality.organization_number, "912345678")

    def test_municipality_list_is_sorted_by_name_and_fetched_once(self):
        for _ in range(2):
            status, _, body = self.request("/api/flyt-municipalities")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"municipalities": [
                {"id": "923456789", "name": "Bergli kommune"},
                {"id": "912345678", "name": "Fjordvik kommune"},
                {"id": "934567890", "name": "Åsby kommune"},
            ]})
        self.assertEqual(len(self.transport.calls), 1)

    def test_failed_municipality_fetch_is_not_cached(self):
        self.transport.failures.append(SourceUnavailable("fictional outage"))
        status, _, body = self.request("/api/flyt-municipalities")
        self.assertEqual(status, 502)
        self.assertNotIn("fictional outage", json.loads(body)["error"])
        status, _, body = self.request("/api/flyt-municipalities")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["municipalities"]), 3)
        self.assertEqual(len(self.transport.calls), 2)

    def test_municipality_list_has_the_same_protection_as_the_state(self):
        for headers in (
            {"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"},
            {"X-Setup-Token": "wrong"}, {"Sec-Fetch-Site": "cross-site"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/flyt-municipalities", headers=headers)[0], 403)
        self.assertEqual(self.transport.calls, [])


if __name__ == "__main__":
    unittest.main()
