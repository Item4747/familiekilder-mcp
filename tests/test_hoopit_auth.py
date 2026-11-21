"""Hoopit personal access token: format, live check, storage, rejection, retired app login."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from familiekilder import hoopit
from familiekilder.api_transport import ApiHTTPError
from familiekilder.hoopit_auth import (
    HoopitAuthError,
    HoopitReauthenticationRequired,
    HoopitScopeMissing,
    HoopitTokenFormatError,
    HoopitTokenRejected,
    auth_status,
    connect_token,
    logout,
    mark_scope_missing,
    mark_token_rejected,
    session_path,
    token_for_use,
    validate_token_format,
)
from familiekilder.private_state import (
    atomic_write_private_json,
    read_private_json,
)
from familiekilder.read_cache import account_scope
from familiekilder_testing.hoopit import (
    OCCURRENCES,
    OTHER_TOKEN,
    POSTS,
    TOKEN,
    FakeTransport,
    NoopLimiter,
    page,
)

NOW = 2_000_000_000.0


def _ok_transport() -> FakeTransport:
    return FakeTransport({POSTS: page([]), OCCURRENCES: page([])})


class TokenFormatTests(unittest.TestCase):
    def test_new_and_older_tokens_are_accepted_and_trimmed(self):
        self.assertEqual(validate_token_format(f"  {TOKEN}\n"), TOKEN)
        older = "hoopit_" + "Gammel0Token" * 3
        self.assertEqual(validate_token_format(older), older)

    def test_wrong_text_gets_a_clear_message_without_the_value(self):
        cases = {
            "": "Lim inn tokenet",
            "hoopit_pat_kort": "ikke helt",
            "ghp_" + "x" * 36: "ikke et Hoopit-token",
            TOKEN + "x": "ikke helt",
        }
        for value, expected in cases.items():
            with (
                self.subTest(value=value[:12]),
                self.assertRaises(HoopitTokenFormatError) as raised,
            ):
                validate_token_format(value)
            self.assertIn(expected, str(raised.exception))
            if value:
                self.assertNotIn(value, str(raised.exception))


class ConnectTokenTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def connect(self, token=TOKEN, transport=None, **options):
        return connect_token(
            self.root,
            token,
            transport=transport or _ok_transport(),
            rate_limiter=NoopLimiter(),
            at=NOW,
            **options,
        )

    def test_a_working_token_is_checked_with_two_reads_and_stored_privately(self):
        transport = _ok_transport()
        result = self.connect(transport=transport)
        self.assertEqual(
            [FakeTransport.query(url).get("page") for url, _, _ in transport.gets], ["1", "1"]
        )
        self.assertEqual(
            [url.split("?")[0] for url, _, _ in transport.gets],
            [
                "https://api.hoopit.io/external/me/posts/",
                "https://api.hoopit.io/external/me/occurrences/",
            ],
        )
        self.assertTrue(all(bearer == TOKEN for _, bearer, _ in transport.gets))
        self.assertTrue(all(headers.get("Version") == "1" for _, _, headers in transport.gets))
        self.assertEqual(result["scopes_verified"], ["posts:read", "events:read"])
        self.assertEqual(result["scope_not_checked"], "events:respond")
        self.assertNotIn(TOKEN, json.dumps(result))
        stored = read_private_json(session_path(self.root))
        self.assertEqual(stored["token"], TOKEN)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(session_path(self.root).stat().st_mode), 0o600)
        status = auth_status(self.root)
        self.assertTrue(status["authenticated"])
        self.assertNotIn(TOKEN, json.dumps(status))
        self.assertEqual(token_for_use(self.root).token, TOKEN)

    def test_a_rejected_token_is_not_stored(self):
        transport = FakeTransport({POSTS: ApiHTTPError(401)})
        with self.assertRaises(HoopitTokenRejected) as raised:
            self.connect(transport=transport)
        self.assertIn("utløpt eller trukket tilbake", str(raised.exception))
        self.assertFalse(session_path(self.root).exists())

    def test_a_missing_scope_is_named_by_hoopits_checkbox(self):
        # Hoopit's token page shows the checkbox labels, never the scope ids.
        labels = {
            "posts:read": "Tokenet mangler «Se innlegg og kommentarer i gruppene dine».",
            "events:read": "Tokenet mangler «Se aktivitetene og invitasjonene dine, og barnas».",
        }
        for path, scope in ((POSTS, "posts:read"), (OCCURRENCES, "events:read")):
            values = {POSTS: page([]), OCCURRENCES: page([])}
            values[path] = ApiHTTPError(403)
            with self.subTest(scope=scope), self.assertRaises(HoopitScopeMissing) as raised:
                self.connect(transport=FakeTransport(values))
            self.assertEqual(raised.exception.scope, scope)
            self.assertTrue(str(raised.exception).startswith(labels[scope]))
            self.assertNotIn(scope, str(raised.exception))
            self.assertFalse(session_path(self.root).exists())

    def test_a_stored_token_is_replaced_only_when_asked_and_keeps_its_account(self):
        self.connect()
        scope = account_scope(self.root, "hoopit")
        with self.assertRaises(HoopitAuthError):
            self.connect(token=OTHER_TOKEN)
        self.connect(token=OTHER_TOKEN, replace_existing=True)
        self.assertEqual(token_for_use(self.root).token, OTHER_TOKEN)
        self.assertEqual(account_scope(self.root, "hoopit"), scope)

    def test_rejection_and_missing_scope_stay_until_a_new_token(self):
        self.connect()
        mark_token_rejected(self.root, at=NOW)
        status = auth_status(self.root)
        self.assertTrue(status["refresh_blocked"])
        self.assertEqual(status["refresh_block_reason"], "token-rejected")
        self.assertIn("id.hoopit.io", status["detail"])
        with self.assertRaises(HoopitTokenRejected):
            token_for_use(self.root)
        self.connect(token=OTHER_TOKEN, replace_existing=True)
        self.assertTrue(auth_status(self.root)["authenticated"])
        mark_scope_missing(self.root, "events:read", at=NOW)
        self.assertEqual(auth_status(self.root)["missing_scope"], "events:read")
        with self.assertRaises(HoopitScopeMissing):
            token_for_use(self.root)

    def test_logout_ends_the_account_binding(self):
        self.connect()
        scope = account_scope(self.root, "hoopit")
        result = logout(self.root)
        self.assertTrue(result["session_removed"])
        self.assertIn("id.hoopit.io", result["detail"])
        with self.assertRaises(HoopitReauthenticationRequired):
            token_for_use(self.root)
        self.connect()
        self.assertNotEqual(account_scope(self.root, "hoopit"), scope)


class UnreadableSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        connect_token(
            self.root, TOKEN, transport=_ok_transport(), rate_limiter=NoopLimiter(), at=NOW
        )
        stored = read_private_json(session_path(self.root))
        atomic_write_private_json(session_path(self.root), {**stored, "unexpected": True})

    def test_status_says_what_to_do_and_reads_stop(self):
        status = auth_status(self.root)
        self.assertFalse(status["status_unavailable"])
        self.assertTrue(status["session_present"])
        self.assertEqual(status["refresh_block_reason"], "session-unreadable")
        self.assertIn("erstatte", status["detail"])
        with self.assertRaises(HoopitReauthenticationRequired):
            token_for_use(self.root)

    def test_replacing_keeps_the_account_and_logout_removes_it(self):
        scope = read_private_json(session_path(self.root))["account_scope"]
        with self.assertRaises(HoopitAuthError):
            connect_token(
                self.root, OTHER_TOKEN, transport=_ok_transport(), rate_limiter=NoopLimiter()
            )
        connect_token(
            self.root,
            OTHER_TOKEN,
            replace_existing=True,
            transport=_ok_transport(),
            rate_limiter=NoopLimiter(),
        )
        self.assertEqual(token_for_use(self.root).token, OTHER_TOKEN)
        self.assertEqual(account_scope(self.root, "hoopit"), scope)
        session_path(self.root).write_text("ikke json", encoding="utf-8")
        self.assertTrue(logout(self.root)["session_removed"])
        self.assertFalse(session_path(self.root).exists())


class VerifyTokenTests(unittest.TestCase):
    def test_token_check_runs_through_the_pinned_client(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = _ok_transport()
            result = hoopit.verify_token(
                Path(directory), TOKEN, transport=transport, rate_limiter=NoopLimiter(), at=NOW
            )
        self.assertEqual(result["posts"], 0)
        query = FakeTransport.query(transport.gets[1][0])
        self.assertEqual(query["from_date"], "2033-05-18T03:33:20Z")
        self.assertEqual(query["to_date"], "2033-06-01T03:33:20Z")


class CommandLineAndWizardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fake_check(self):
        real = hoopit.verify_token

        def check(root, token, **options):
            options["transport"] = _ok_transport()
            options["rate_limiter"] = NoopLimiter()
            return real(root, token, **options)

        return patch.object(hoopit, "verify_token", check)

    def test_the_cli_reads_the_token_from_stdin_and_never_prints_it(self):
        from familiekilder import source_cli

        output = io.StringIO()
        with (
            self.fake_check(),
            patch.object(sys, "stdin", io.StringIO(TOKEN + "\n")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(source_cli.main(["--root", str(self.root), "configure", "hoopit"]), 0)
            self.assertEqual(
                source_cli.main(["--root", str(self.root), "auth", "hoopit", "token", "--stdin"]), 0
            )
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertTrue(auth_status(self.root)["authenticated"])
        with (
            patch.object(sys, "stdin", io.StringIO(OTHER_TOKEN)),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertNotEqual(
                source_cli.main(["--root", str(self.root), "auth", "hoopit", "token", "--stdin"]), 0
            )
        self.assertEqual(token_for_use(self.root).token, TOKEN)

    def test_the_wizard_checks_the_token_in_a_job_and_shows_only_safe_text(self):
        import time

        from familiekilder.setup_service import SetupError, SetupService

        service = SetupService(self.root, home=self.root / "home")
        self.addCleanup(service.close)
        with self.fake_check():
            service.connect_token("hoopit", TOKEN)
            deadline = time.monotonic() + 10
            while service.state()["job"]["phase"] == "working" and time.monotonic() < deadline:
                time.sleep(0.05)
        state = service.state()
        self.assertEqual(state["job"]["phase"], "done")
        self.assertNotIn(TOKEN, json.dumps(state))
        row = next(row for row in state["services"] if row["id"] == "hoopit")
        self.assertEqual((row["setup"], row["state"], row["enabled"]), ("token", "connected", True))
        with self.assertRaises(SetupError):
            service.connect_token("hoopit", OTHER_TOKEN)
        service.connect_token("hoopit", "ikke et token", replace=True)
        deadline = time.monotonic() + 10
        while service.state()["job"]["phase"] == "working" and time.monotonic() < deadline:
            time.sleep(0.05)
        job = service.state()["job"]
        self.assertEqual(job["phase"], "error")
        self.assertIn("ikke et Hoopit-token", job["message"])
        with self.assertRaises(SetupError):
            service.start("login", "hoopit", consent=True)


if __name__ == "__main__":
    unittest.main()
