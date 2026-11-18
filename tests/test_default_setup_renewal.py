"""A newer built-in client setup replaces one the wizard stored, and nothing else.

Every setup here is invented; the shipped defaults are never read.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from familiekilder_testing.private import private_again, readable_by_others

from familiekilder import client_defaults, hoopit_auth, skolemelding_auth, spond_auth, vigilo_auth
from familiekilder.client_defaults import (
    MARK_SCHEMA,
    RenewalRefused,
    mark_path,
    refresh_stored_defaults,
    renewal_pending,
    store_default,
)
from familiekilder.connection_status import auth_module, connection_state
from familiekilder.private_lock import PrivateLockBusy, private_source_lock
from familiekilder.private_state import (
    PrivateStateError,
    atomic_write_private_bytes,
    atomic_write_private_json,
    read_private_json,
)
from familiekilder.provider_bundle import provider_state_path
from familiekilder.setup_service import SetupError, SetupService
from familiekilder.skolemelding_auth import REDIRECT_URI as SKOLEMELDING_REDIRECT_URI
from familiekilder import source_cli
from familiekilder_testing.ist_oauth import _client as ist_client
from familiekilder_testing.spond_auth import _canonical_session
from familiekilder_testing.vigilo_auth import FakeAuthTransport
from familiekilder_testing.vigilo_auth import _config as vigilo_config


def old_defaults() -> dict[str, dict]:
    return {
        "vigilo": vigilo_config(),
        "ist-home": ist_client(),
        "spond": {
            "api_root": spond_auth.API_ROOT,
            "app_version": "fictional-spond-1.0",
            "attachment_hosts": ["files.example.test"],
        },
        "hoopit": {
            "api_root": hoopit_auth.API_ROOT,
            "firebase_api_key": "AIza" + "x" * 36,
            "firebase_project_id": hoopit_auth.FIREBASE_PROJECT_ID,
            "app_version": "Web fictional-hoopit-1.0",
            "attachment_hosts": ["files.example.test"],
        },
        "skolemelding": {
            "oidc_client_id": "fictional-oidc-client",
            "portal_client_id": "fictional-portal-client",
            "app_version": "fictional-skolemelding-1.0",
            "issuer": "https://idporten.no",
            "redirect_uri": SKOLEMELDING_REDIRECT_URI,
            "scopes": ["openid", "profile"],
            "attachment_hosts": ["portal.skoleplattform.no"],
            "attachment_path_prefixes": ["/api/files/"],
        },
    }


def new_defaults() -> dict[str, dict]:
    """The next package: a newer app version, and a new secret where IST has no version."""

    value = old_defaults()
    for source, config in value.items():
        if source == "ist-home":
            config["client_secret"] = "fictional-rotated-client-secret"
        else:
            config["app_version"] = config["app_version"].replace("1.0", "2.0")
    value["vigilo"]["client_id"] = "fictional-rotated-client.ANDROID"
    return value


def strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


def secret_strings() -> set[str]:
    # The source ids themselves may appear; values from a setup may not.
    found = set()
    for defaults in (old_defaults(), new_defaults()):
        for config in defaults.values():
            found.update(text for text in strings(config) if len(text) >= 8)
    return found


def shipping(defaults):
    """Make the package ship these built-in setups."""

    return patch.object(client_defaults, "_defaults", return_value=defaults)


class RenewalCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.root = self.temporary / "data"
        self.root.mkdir()

    def setup_path(self, source):
        return auth_module(source).client_config_path(self.root)

    def stored(self, source):
        return auth_module(source).load_client_config(self.root).as_private_json()

    def store_from_wizard(self, source, config=None):
        config = old_defaults()[source] if config is None else config
        store_default(self.root, source, config)

    def configure_own(self, source, config):
        client_file = self.temporary / ".private" / f"own-{source}.json"
        atomic_write_private_bytes(client_file, json.dumps(config).encode())
        auth_module(source).configure_client(self.root, client_file)

    def assert_same_setup(self, source, config):
        stored = self.stored(source)
        for key, value in config.items():
            expected = sorted(value) if isinstance(value, list) else value
            actual = sorted(stored[key]) if isinstance(stored[key], list) else stored[key]
            self.assertEqual(actual, expected, key)


class RenewalTests(RenewalCase):
    def test_newer_default_replaces_a_marked_setup_for_every_source(self):
        for source in old_defaults():
            with self.subTest(source=source):
                self.store_from_wizard(source)
                with shipping(new_defaults()):
                    self.assertTrue(renewal_pending(self.root, source))
                    self.assertEqual(refresh_stored_defaults(self.root), [source])
                    self.assert_same_setup(source, new_defaults()[source])
                    self.assertFalse(renewal_pending(self.root, source))
                    # Renewed once; the next start has nothing to do.
                    self.assertEqual(refresh_stored_defaults(self.root), [])
                mark = read_private_json(mark_path(self.root, source))
                self.assertEqual(
                    mark["setup_sha256"],
                    hashlib.sha256(self.setup_path(source).read_bytes()).hexdigest(),
                )
                self.assertEqual(
                    mark["default_sha256"],
                    client_defaults.default_fingerprint(new_defaults()[source]),
                )

    def test_same_default_changes_nothing(self):
        self.store_from_wizard("spond")
        before = self.setup_path("spond").stat()
        with shipping(old_defaults()):
            self.assertEqual(refresh_stored_defaults(self.root), [])
        after = self.setup_path("spond").stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_the_users_own_setup_stays(self):
        for source, config in old_defaults().items():
            with self.subTest(source=source):
                self.configure_own(source, config)
                before = self.setup_path(source).read_bytes()
                with shipping(new_defaults()):
                    self.assertFalse(renewal_pending(self.root, source))
                    self.assertEqual(refresh_stored_defaults(self.root), [])
                self.assertEqual(self.setup_path(source).read_bytes(), before)
                self.assertFalse(mark_path(self.root, source).exists())

    def test_a_setup_imported_after_the_wizard_stays(self):
        self.store_from_wizard("spond")
        own = {**old_defaults()["spond"], "app_version": "fictional-own-9.9"}
        self.configure_own("spond", own)
        before = self.setup_path("spond").read_bytes()
        with shipping(new_defaults()):
            self.assertEqual(refresh_stored_defaults(self.root), [])
        self.assertEqual(self.setup_path("spond").read_bytes(), before)
        self.assertEqual(self.stored("spond")["app_version"], "fictional-own-9.9")

    def test_a_setup_edited_by_hand_stays(self):
        self.store_from_wizard("hoopit")
        path = self.setup_path("hoopit")
        edited = path.read_bytes().rstrip(b"\n") + b" \n"
        atomic_write_private_bytes(path, edited)
        with shipping(new_defaults()):
            self.assertEqual(refresh_stored_defaults(self.root), [])
        self.assertEqual(path.read_bytes(), edited)

    def test_a_withdrawn_default_leaves_the_stored_setup(self):
        self.store_from_wizard("vigilo")
        before = self.setup_path("vigilo").read_bytes()
        mark_before = mark_path(self.root, "vigilo").read_bytes()
        withdrawn = {
            source: config for source, config in new_defaults().items() if source != "vigilo"
        }
        with shipping(withdrawn):
            self.assertFalse(renewal_pending(self.root, "vigilo"))
            self.assertEqual(refresh_stored_defaults(self.root), [])
        self.assertEqual(self.setup_path("vigilo").read_bytes(), before)
        self.assertEqual(mark_path(self.root, "vigilo").read_bytes(), mark_before)

    def test_an_unreadable_setup_stays(self):
        self.store_from_wizard("skolemelding")
        path = self.setup_path("skolemelding")
        before = path.read_bytes()
        readable_by_others(path)
        with shipping(new_defaults()):
            self.assertEqual(refresh_stored_defaults(self.root), [])
        private_again(path)
        self.assertEqual(path.read_bytes(), before)

    def test_a_missing_setup_is_not_recreated(self):
        self.store_from_wizard("spond")
        self.setup_path("spond").unlink()
        with shipping(new_defaults()):
            self.assertEqual(refresh_stored_defaults(self.root), [])
        self.assertFalse(self.setup_path("spond").exists())

    def test_an_invalid_mark_counts_as_no_mark(self):
        self.store_from_wizard("spond")
        before = self.setup_path("spond").read_bytes()
        for mark in (
            b"{not json",
            b'{"schema": "other"}',
            json.dumps(
                {
                    "schema": MARK_SCHEMA,
                    "setup_sha256": "0" * 64,
                    "default_sha256": "short",
                }
            ).encode(),
        ):
            with self.subTest(mark=mark):
                atomic_write_private_bytes(mark_path(self.root, "spond"), mark)
                with shipping(new_defaults()):
                    self.assertEqual(refresh_stored_defaults(self.root), [])
                self.assertEqual(self.setup_path("spond").read_bytes(), before)

    def test_a_folder_managed_by_a_provider_bundle_is_left_alone(self):
        self.store_from_wizard("spond")
        before = self.setup_path("spond").read_bytes()
        atomic_write_private_bytes(provider_state_path(self.root), b"{}\n")
        with shipping(new_defaults()):
            self.assertEqual(refresh_stored_defaults(self.root), [])
        self.assertEqual(self.setup_path("spond").read_bytes(), before)

    def test_without_marks_nothing_is_created(self):
        root = self.temporary / "empty"
        root.mkdir()
        with shipping(new_defaults()):
            self.assertEqual(refresh_stored_defaults(root), [])
        self.assertEqual(list(root.iterdir()), [])

    def test_a_broken_defaults_file_does_not_raise(self):
        self.store_from_wizard("spond")
        before = self.setup_path("spond").read_bytes()
        with (
            patch.object(client_defaults, "_defaults", side_effect=ValueError("broken")),
            self.assertLogs("familiekilder.client_defaults", "WARNING"),
        ):
            self.assertEqual(refresh_stored_defaults(self.root), [])
        self.assertEqual(self.setup_path("spond").read_bytes(), before)


class SessionTests(RenewalCase):
    def store_spond_session(self):
        atomic_write_private_json(
            spond_auth.session_path(self.root), _canonical_session(stored=True)
        )

    def store_vigilo_session(self):
        config = vigilo_auth.load_client_config(self.root)
        atomic_write_private_json(
            vigilo_auth.session_path(self.root),
            {
                "access_token": "fictional-access-token",
                "refresh_token": "fictional-refresh-token",
                "expires_at": 2_000_000_000,
                "client_fingerprint": vigilo_auth._config_fingerprint(config),
            },
        )

    def test_a_new_app_version_keeps_the_login(self):
        self.store_from_wizard("spond")
        self.store_spond_session()
        session = spond_auth.session_path(self.root).read_bytes()
        self.assertEqual(connection_state(spond_auth.auth_status(self.root)), "connected")
        with shipping(new_defaults()):
            self.assertEqual(refresh_stored_defaults(self.root), ["spond"])
        self.assertEqual(spond_auth.session_path(self.root).read_bytes(), session)
        self.assertEqual(self.stored("spond")["app_version"], "fictional-spond-2.0")
        self.assertEqual(connection_state(spond_auth.auth_status(self.root)), "connected")

    def test_a_refused_new_identity_keeps_setup_login_and_mark_until_logout(self):
        self.store_from_wizard("vigilo")
        self.store_vigilo_session()
        session = vigilo_auth.session_path(self.root).read_bytes()
        setup = self.setup_path("vigilo").read_bytes()
        mark = mark_path(self.root, "vigilo").read_bytes()
        state = connection_state(vigilo_auth.auth_status(self.root))
        stderr = io.StringIO()
        with (
            shipping(new_defaults()),
            self.assertLogs("familiekilder.client_defaults", "WARNING") as logs,
            contextlib.redirect_stdout(stderr),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(refresh_stored_defaults(self.root), [])
            # Still pending, so the next start tries again.
            self.assertTrue(renewal_pending(self.root, "vigilo"))
        self.assertEqual(vigilo_auth.session_path(self.root).read_bytes(), session)
        self.assertEqual(self.setup_path("vigilo").read_bytes(), setup)
        self.assertEqual(mark_path(self.root, "vigilo").read_bytes(), mark)
        self.assertEqual(connection_state(vigilo_auth.auth_status(self.root)), state)
        printed = "\n".join(logs.output) + stderr.getvalue()
        for value in secret_strings():
            self.assertNotIn(value, printed)

        # After the user logs out, the next start takes the new setup and
        # the status asks for a new login the normal way.
        vigilo_auth.logout(self.root, transport=FakeAuthTransport([]))
        with shipping(new_defaults()):
            self.assertEqual(refresh_stored_defaults(self.root), ["vigilo"])
        self.assertEqual(self.stored("vigilo")["client_id"], "fictional-rotated-client.ANDROID")
        self.assertEqual(connection_state(vigilo_auth.auth_status(self.root)), "login_required")

    def test_skolemelding_refuses_a_new_identity_but_takes_a_new_version(self):
        self.store_from_wizard("skolemelding")
        session_file = skolemelding_auth.session_path(self.root)
        atomic_write_private_json(session_file, {"refresh_token": "fictional-refresh-token"})
        session = session_file.read_bytes()
        setup = self.setup_path("skolemelding").read_bytes()
        rotated = new_defaults()
        rotated["skolemelding"]["oidc_client_id"] = "fictional-rotated-oidc-client"
        with (
            shipping(rotated),
            self.assertLogs("familiekilder.client_defaults", "WARNING") as logs,
        ):
            self.assertEqual(refresh_stored_defaults(self.root), [])
        self.assertIn("innlogging", "\n".join(logs.output))
        self.assertEqual(self.setup_path("skolemelding").read_bytes(), setup)
        self.assertEqual(session_file.read_bytes(), session)
        with shipping(new_defaults()):
            self.assertEqual(refresh_stored_defaults(self.root), ["skolemelding"])
        self.assertEqual(session_file.read_bytes(), session)
        self.assertEqual(self.stored("skolemelding")["app_version"], "fictional-skolemelding-2.0")


class BusyLockTests(RenewalCase):
    def hold_lock(self, source):
        """Hold a source lock in another thread, as another process would."""

        held = threading.Event()
        release = threading.Event()

        def hold():
            with private_source_lock(self.root, source, wait=True, timeout=5):
                held.set()
                release.wait(30)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(5))

        def stop():
            release.set()
            thread.join(timeout=5)

        self.addCleanup(stop)
        return stop

    def test_nothing_pending_never_waits_for_the_lock_or_warns(self):
        self.store_from_wizard("spond")
        self.hold_lock("spond")
        started = time.monotonic()
        with shipping(old_defaults()), self.assertNoLogs("familiekilder.client_defaults"):
            self.assertEqual(refresh_stored_defaults(self.root), [])
        self.assertLess(time.monotonic() - started, 1)

    def test_a_pending_renewal_on_a_busy_source_waits_briefly_and_does_not_warn(self):
        self.store_from_wizard("spond")
        release = self.hold_lock("spond")
        started = time.monotonic()
        with (
            shipping(new_defaults()),
            self.assertLogs("familiekilder.client_defaults", "INFO") as logs,
        ):
            self.assertEqual(refresh_stored_defaults(self.root), [])
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, client_defaults.LOCK_TIMEOUT_SECONDS + 1)
        self.assertEqual([record.levelname for record in logs.records], ["INFO"])
        self.assertIn("opptatt", logs.output[0])
        release()
        with shipping(new_defaults()):
            self.assertEqual(refresh_stored_defaults(self.root), ["spond"])

    def test_the_wizard_does_not_mark_a_setup_imported_meanwhile(self):
        own = {**old_defaults()["spond"], "app_version": "fictional-own-9.9"}
        self.configure_own("spond", own)
        stored = store_default(self.root, "spond", old_defaults()["spond"])
        self.assertFalse(stored)
        self.assertFalse(mark_path(self.root, "spond").exists())
        self.assertEqual(self.stored("spond")["app_version"], "fictional-own-9.9")


class ConcurrencyTests(RenewalCase):
    def test_threads_renew_once(self):
        self.store_from_wizard("spond")
        results = []
        barrier = threading.Barrier(8)

        def renew():
            barrier.wait()
            results.append(refresh_stored_defaults(self.root))

        with shipping(new_defaults()):
            threads = [threading.Thread(target=renew) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertFalse(renewal_pending(self.root, "spond"))
        self.assertEqual(sorted(results), [[]] * 7 + [["spond"]])
        self.assertEqual(self.stored("spond")["app_version"], "fictional-spond-2.0")

    def test_processes_renew_once(self):
        self.store_from_wizard("spond")
        script = (
            "import json, sys\n"
            "from unittest.mock import patch\n"
            "from familiekilder import client_defaults\n"
            "defaults = json.loads(sys.argv[2])\n"
            "with patch.object(client_defaults, '_defaults', return_value=defaults):\n"
            "    print(json.dumps(client_defaults.refresh_stored_defaults(sys.argv[1])))\n"
        )
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.root), json.dumps(new_defaults())],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            for _ in range(4)
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(sorted(results), [[]] * 3 + [["spond"]])
        self.assertEqual(self.stored("spond")["app_version"], "fictional-spond-2.0")


class WizardTests(RenewalCase):
    def test_wizard_import_writes_the_mark_without_values(self):
        service = SetupService(self.root, home=self.temporary)
        with shipping(old_defaults()), patch.object(service, "_login"):
            for source in old_defaults():
                with self.subTest(source=source):
                    service.start("login", source, consent=True)
                    service.worker.join(timeout=5)
                    self.assertEqual(service.job.phase, "done")
                    mark_bytes = mark_path(self.root, source).read_bytes()
                    mark = json.loads(mark_bytes)
                    self.assertEqual(set(mark), {"schema", "setup_sha256", "default_sha256"})
                    self.assertEqual(
                        mark["setup_sha256"],
                        hashlib.sha256(self.setup_path(source).read_bytes()).hexdigest(),
                    )
                    for value in secret_strings():
                        self.assertNotIn(value.encode(), mark_bytes)
                    if os.name != "nt":
                        self.assertEqual(mark_path(self.root, source).stat().st_mode & 0o777, 0o600)

    def test_relogin_with_a_session_logs_out_and_takes_the_refused_setup(self):
        self.store_from_wizard("vigilo")
        SessionTests.store_vigilo_session(self)
        service = SetupService(self.root, home=self.temporary)
        real_logout = vigilo_auth.logout

        def logout(root):
            return real_logout(root, transport=FakeAuthTransport([]))

        with (
            shipping(new_defaults()),
            patch.object(vigilo_auth, "logout", side_effect=logout) as logged_out,
            patch.object(service, "_login") as login,
            self.assertLogs("familiekilder.client_defaults", "WARNING"),
        ):
            # The wizard opening could not renew it, because of the session.
            self.assertEqual(refresh_stored_defaults(self.root), [])
            service.start("login", "vigilo", consent=True, replace=True)
            service.worker.join(timeout=5)
        self.assertEqual(service.job.phase, "done", service.job.message)
        logged_out.assert_called_once()
        login.assert_called_once()
        self.assertEqual(self.stored("vigilo")["client_id"], "fictional-rotated-client.ANDROID")

    def test_relogin_without_pending_renewal_does_not_log_out(self):
        self.store_from_wizard("spond")
        SessionTests.store_spond_session(self)
        session = spond_auth.session_path(self.root).read_bytes()
        service = SetupService(self.root, home=self.temporary)
        with (
            shipping(old_defaults()),
            patch.object(spond_auth, "logout") as logged_out,
            patch.object(service, "_login"),
        ):
            service.start("login", "spond", consent=True, replace=True)
            service.worker.join(timeout=5)
        self.assertEqual(service.job.phase, "done")
        logged_out.assert_not_called()
        self.assertEqual(spond_auth.session_path(self.root).read_bytes(), session)


class WizardReloginTests(RenewalCase):
    """Re-login in the wizard while a refused renewal waits for the session to go."""

    def setUp(self):
        super().setUp()
        self.store_from_wizard("vigilo")
        SessionTests.store_vigilo_session(self)
        self.session = vigilo_auth.session_path(self.root).read_bytes()
        self.setup = self.setup_path("vigilo").read_bytes()
        self.service = SetupService(self.root, home=self.temporary)
        real_logout = vigilo_auth.logout

        def logout(root):
            return real_logout(root, transport=FakeAuthTransport([]))

        for patcher in (
            shipping(new_defaults()),
            patch.object(vigilo_auth, "logout", side_effect=logout),
        ):
            mock = patcher.start()
            self.addCleanup(patcher.stop)
        self.logout = mock

    def relogin(self, login=None):
        with patch.object(self.service, "_login", side_effect=login) as login_mock:
            self.service.start("login", "vigilo", consent=True, replace=True)
            self.service.worker.join(timeout=10)
        return login_mock

    def test_state_says_which_logged_in_service_has_a_refused_renewal(self):
        rows = {row["id"]: row for row in self.service.state()["services"]}
        self.assertIs(rows["vigilo"]["renewal_pending"], True)
        self.assertIs(rows["spond"]["renewal_pending"], False)
        page = Path(client_defaults.__file__).parent / "data/setup"
        self.assertIn('id="renewal-warning"', (page / "index.html").read_text(encoding="utf-8"))
        self.assertIn("renewal_pending", (page / "setup.js").read_text(encoding="utf-8"))

    def test_cancel_after_the_logout_says_the_user_is_logged_out(self):
        def cancelled(job, **_):
            job.cancel.set()
            raise SetupError("Innloggingen ble avbrutt.")

        self.relogin(cancelled)
        job = self.service.job
        self.assertEqual(job.phase, "cancelled")
        self.assertIn("logget ut av Vigilo", job.message)
        self.logout.assert_called_once()
        self.assertFalse(vigilo_auth.session_path(self.root).exists())
        self.assertEqual(self.stored("vigilo")["client_id"], "fictional-rotated-client.ANDROID")

    def test_failed_login_after_the_logout_says_the_user_is_logged_out(self):
        self.relogin(RuntimeError("fictional failure"))
        self.assertEqual(self.service.job.phase, "error")
        self.assertIn("logget ut av Vigilo", self.service.job.message)

    def test_swap_that_still_fails_after_the_logout_stops_before_the_login(self):
        with patch.object(client_defaults, "_write_default", side_effect=RuntimeError("fictional")):
            login = self.relogin()
        job = self.service.job
        self.assertEqual(job.phase, "error")
        self.assertIn("Du er logget ut av Vigilo", job.message)
        self.assertIn("kunne ikke lagres", job.message)
        login.assert_not_called()
        self.logout.assert_called_once()
        self.assertEqual(self.setup_path("vigilo").read_bytes(), self.setup)

    def test_cancel_before_the_logout_keeps_the_login(self):
        def refused(*_args, **_kwargs):
            self.service.job.cancel.set()
            raise RenewalRefused

        with patch("familiekilder.setup_service.refresh_stored_default", side_effect=refused):
            login = self.relogin()
        self.assertEqual(self.service.job.phase, "cancelled")
        self.assertEqual(self.service.job.message, "Steget er avbrutt.")
        login.assert_not_called()
        self.logout.assert_not_called()
        self.assertEqual(vigilo_auth.session_path(self.root).read_bytes(), self.session)

    def test_a_busy_source_fails_the_job_without_changes(self):
        def busy(*_args, **_kwargs):
            raise PrivateLockBusy("fictional")

        with patch("familiekilder.private_lock.private_source_lock", side_effect=busy):
            login = self.relogin()
        self.assertEqual(self.service.job.phase, "error")
        self.assertIn("En annen Vigilo-handling pågår", self.service.job.message)
        login.assert_not_called()
        self.logout.assert_not_called()
        self.assertEqual(vigilo_auth.session_path(self.root).read_bytes(), self.session)
        self.assertEqual(self.setup_path("vigilo").read_bytes(), self.setup)

    def test_other_failures_do_not_log_out(self):
        failure = PrivateStateError("fictional")
        with patch.object(client_defaults, "_write_default", side_effect=failure):
            login = self.relogin()
        self.assertEqual(self.service.job.phase, "done", self.service.job.message)
        login.assert_called_once()
        self.logout.assert_not_called()
        self.assertEqual(vigilo_auth.session_path(self.root).read_bytes(), self.session)
        self.assertEqual(self.setup_path("vigilo").read_bytes(), self.setup)


class StartTests(RenewalCase):
    def test_mcp_server_start_renews_before_serving(self):
        self.store_from_wizard("spond")
        build_server = MagicMock(return_value=(MagicMock(), MagicMock()))
        seen = []
        build_server.side_effect = lambda *args, **kwargs: (
            seen.append(self.stored("spond")["app_version"]) or (MagicMock(), MagicMock())
        )
        with (
            shipping(new_defaults()),
            patch.object(source_cli, "source_server_api", return_value=(build_server, None)),
            patch.object(source_cli, "serve_stdio", return_value=0) as serve,
        ):
            self.assertEqual(source_cli.main(["--root", str(self.root), "mcp", "serve"]), 0)
        serve.assert_called_once()
        self.assertEqual(seen, ["fictional-spond-2.0"])

    def test_mcp_server_starts_when_renewal_fails(self):
        self.store_from_wizard("spond")
        stdout = io.StringIO()
        with (
            patch.object(client_defaults, "_defaults", side_effect=ValueError("broken")),
            patch.object(
                source_cli,
                "source_server_api",
                return_value=(MagicMock(return_value=(MagicMock(), MagicMock())), None),
            ),
            patch.object(source_cli, "serve_stdio", return_value=0) as serve,
            contextlib.redirect_stdout(stdout),
            self.assertLogs("familiekilder.client_defaults", "WARNING"),
        ):
            self.assertEqual(source_cli.main(["--root", str(self.root), "mcp", "serve"]), 0)
        serve.assert_called_once()
        # stdout is the MCP channel; nothing may be written there.
        self.assertEqual(stdout.getvalue(), "")

    def test_wizard_start_renews_before_the_page_opens(self):
        self.store_from_wizard("spond")
        seen = []

        def server(root, **_):
            seen.append(self.stored("spond")["app_version"])
            return contextlib.nullcontext(MagicMock(origin="http://127.0.0.1:1"))

        with (
            shipping(new_defaults()),
            patch("familiekilder.setup_web.SetupServer", side_effect=server),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(source_cli.main(["--root", str(self.root), "setup", "--no-open"]), 0)
        self.assertEqual(seen, ["fictional-spond-2.0"])

    def test_wizard_starts_when_renewal_fails(self):
        with (
            patch(
                "familiekilder.setup_web.refresh_stored_defaults", wraps=refresh_stored_defaults
            ) as renew,
            patch.object(
                client_defaults, "refresh_stored_default", side_effect=RuntimeError("broken")
            ),
            patch.object(client_defaults, "_present", return_value=True),
            patch(
                "familiekilder.setup_web.SetupServer",
                return_value=contextlib.nullcontext(MagicMock(origin="http://127.0.0.1:1")),
            ) as server,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertLogs("familiekilder.client_defaults", "WARNING"),
        ):
            self.assertEqual(source_cli.main(["--root", str(self.root), "setup", "--no-open"]), 0)
        renew.assert_called_once()
        server.assert_called_once()


if __name__ == "__main__":
    unittest.main()
