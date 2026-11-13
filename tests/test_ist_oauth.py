from __future__ import annotations

import json
import os
import stat
import unittest
import urllib.parse
from unittest.mock import patch

from familiekilder import ist_oauth as oauth_module
from familiekilder.ist_oauth import (
    AUTHORIZATION_ENDPOINT,
    AUTHORITY,
    CONSENT_VERSION,
    EXPECTED_SCOPES,
    IST_GRAPHQL_ENDPOINT,
    JWKS_URI,
    REDIRECT_URI,
    REVOCATION_ENDPOINT,
    TOKEN_ENDPOINT,
    USERINFO_ENDPOINT,
    _OAuthHTTPError,
    ClientConfig,
    OAuthContextNoAnswer,
    OAuthError,
    OAuthFatalConfigurationError,
    OAuthNoAnswer,
    OAuthReauthenticationRequired,
    _pyjwt_token_validator,
    auth_status,
    begin_login,
    client_config_path,
    configure_client,
    consent_status,
    export_mac_client_config,
    import_session,
    login_with_callback,
    logout,
    pkce_challenge,
    record_consent,
    refresh_block_path,
    refresh_session,
    session_path,
)
from familiekilder_testing.ist_oauth import (
    IstOAuthFixture,
    SCOPE,
    TEST_CLIENT_ID,
    _client,
    _session,
    failed_refresh_marker_cleanup,
    FakeTransport,
)


class IstOAuthTests(IstOAuthFixture, unittest.TestCase):
    def test_rfc_7636_s256_vector(self) -> None:
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self.assertEqual(pkce_challenge(verifier), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_config_is_pinned_and_written_privately(self) -> None:
        result = configure_client(self.root, self.client_source)
        self.assertTrue(result["configured"])
        self.assertNotIn("secret", json.dumps(result))
        path = client_config_path(self.root)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

        for field_name, wrong in (
            ("authority", "https://attacker.invalid"),
            ("client_id", "another-client"),
            ("redirect_uri", "https://attacker.invalid/callback"),
        ):
            value = _client()
            value[field_name] = wrong
            bad = self.root / f"bad-{field_name}.json"
            bad.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(OAuthError):
                configure_client(self.root, bad)

        wrong_scope = self.root / "bad-scope.json"
        wrong_scope.write_text(json.dumps(_client(scope="openid offline_access profile")), encoding="utf-8")
        with self.assertRaisesRegex(OAuthError, "scope-sett"):
            configure_client(self.root, wrong_scope)

    def test_failed_atomic_secret_write_removes_temporary_file(self) -> None:
        target = self.root / ".private" / "credentials" / "fault-session.json"
        secret = "refresh-secret-that-must-not-survive"

        with patch("familiekilder.private_state.os.fsync", side_effect=OSError("disk fault")):
            with self.assertRaises(OAuthError):
                oauth_module._atomic_write_json(
                    target,
                    {"refresh_token": secret},
                    overwrite=True,
                )

        self.assertFalse(target.exists())
        leftovers = list(target.parent.glob(f".{target.name}.*.tmp"))
        self.assertEqual(leftovers, [])
        self.assertFalse(any(secret.encode() in path.read_bytes() for path in target.parent.iterdir()))

    def test_scope_order_is_preserved_but_set_must_be_exact(self) -> None:
        reversed_scope = " ".join(reversed(SCOPE.split()))
        self.client_source.write_text(json.dumps(_client(scope=reversed_scope)), encoding="utf-8")
        self.configure_and_consent()
        url, transaction = begin_login(self.root, now=10, random_bytes=lambda _length: b"x" * 32)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["scope"], [reversed_scope])
        self.assertEqual(transaction.config.scope, reversed_scope)
        self.assertEqual(set(reversed_scope.split()), EXPECTED_SCOPES)

    def test_consent_is_required_and_identity_free(self) -> None:
        configure_client(self.root, self.client_source)
        with self.assertRaisesRegex(OAuthError, "samtykke"):
            begin_login(self.root, now=1)
        recorded = record_consent(self.root, at=2_000_000_000)
        self.assertEqual(recorded["version"], CONSENT_VERSION)
        self.assertTrue(consent_status(self.root)["current"])
        raw = (client_config_path(self.root).parent / "ist-home-oauth-consent.json").read_text()
        self.assertEqual(set(json.loads(raw)), {"version", "recorded_at"})

    def test_authorization_url_has_random_state_nonce_and_pkce(self) -> None:
        self.configure_and_consent()
        counter = 0

        def random_bytes(length):
            nonlocal counter
            counter += 1
            return bytes([counter]) * length

        url, transaction = begin_login(self.root, now=12.5, random_bytes=random_bytes)
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", AUTHORIZATION_ENDPOINT)
        self.assertEqual(AUTHORIZATION_ENDPOINT, "https://skoleid.no/connect/authorize")
        self.assertEqual(transaction.config.authority, "https://skolid.se")
        self.assertEqual(query["client_id"], [TEST_CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["prompt"], ["login"])
        self.assertNotEqual(query["state"], query["nonce"])
        self.assertEqual(query["code_challenge"], [pkce_challenge(transaction.code_verifier)])
        representation = repr(transaction)
        self.assertNotIn(transaction.state, representation)
        self.assertNotIn(transaction.nonce, representation)
        self.assertNotIn(transaction.code_verifier, representation)
        self.assertNotIn("synthetic-client-secret", repr(transaction.config))

    def test_strict_callback_rejects_timeout_redirect_duplicates_and_replay(self) -> None:
        url, transaction = self.login_start(now=100)
        callback = self.callback(url)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]

        with self.assertRaisesRegex(OAuthError, "utløpt"):
            login_with_callback(
                self.root, transaction, callback, transport=FakeTransport(), now=701
            )
        self.assertFalse(transaction.consumed)

        for bad in (
            f"com.ist.home.miniforum-school://wrong?code=x&state={state}",
            f"{REDIRECT_URI}/extra?code=x&state={state}",
            f"{REDIRECT_URI}?code=x&code=y&state={state}",
            f"{REDIRECT_URI}?code=x&state=wrong",
            f"{REDIRECT_URI}?code=x&state=wrong&unknown=value",
            f"{REDIRECT_URI}?code=x&state={state}&iss=https%3A%2F%2Fwrong.example&unknown=value",
            f"{REDIRECT_URI}?co\nde=x&state={state}",
            f"{REDIRECT_URI}?code=x&state=%C3%A9",
            f"{REDIRECT_URI}?code=%FF&state={state}",
            f"{REDIRECT_URI}?code=x&error=access_denied&state={state}",
            f"{REDIRECT_URI}?code=x&error_description=no&state={state}",
            f"{REDIRECT_URI}?code=æ&state={state}",
        ):
            with self.assertRaises(OAuthError):
                login_with_callback(self.root, transaction, bad, transport=FakeTransport(), now=101)
            self.assertFalse(transaction.consumed)

        with self.assertRaises(OAuthFatalConfigurationError):
            login_with_callback(
                self.root,
                transaction,
                f"{REDIRECT_URI}?error=invalid_client&state={state}",
                transport=FakeTransport(),
                now=101,
            )
        self.assertTrue(transaction.consumed)

        url, transaction = self.login_start(now=100)
        callback = self.callback(url)

        transport = FakeTransport()
        login_with_callback(
            self.root,
            transaction,
            callback,
            transport=transport,
            token_validator=self.claims_validator,
            access_token_validator=self.access_claims_validator,
            now=101,
            wall_time=2_000_000_000,
        )
        with self.assertRaisesRegex(OAuthError, "allerede brukt"):
            login_with_callback(self.root, transaction, callback, transport=transport, now=102)

    def test_callback_ignores_provider_extensions_without_forwarding_or_storing_them(self) -> None:
        url, transaction = self.login_start()
        transport = FakeTransport()
        result = login_with_callback(
            self.root,
            transaction,
            self.callback(url) + "&provider_extension=synthetic-extension-value",
            transport=transport,
            token_validator=self.claims_validator,
            access_token_validator=self.access_claims_validator,
            now=101,
            wall_time=2_000_000_000,
        )
        self.assertTrue(result["authenticated"])
        self.assertTrue(transaction.consumed)
        for payload in (repr(transport.calls), session_path(self.root).read_text()):
            self.assertNotIn("provider_extension", payload)
            self.assertNotIn("synthetic-extension-value", payload)

    def test_full_login_uses_client_secret_post_and_verifies_before_atomic_storage(self) -> None:
        url, transaction = self.login_start()
        transport = FakeTransport()
        result = login_with_callback(
            self.root,
            transaction,
            self.callback(url),
            transport=transport,
            token_validator=self.claims_validator,
            access_token_validator=self.access_claims_validator,
            now=101,
            wall_time=2_000_000_000,
        )
        self.assertEqual(
            result,
            {
                "authenticated": True,
                "expires_at": "2033-05-18T04:33:20+00:00",
                "id_token_verified": True,
                "access_token_verified": True,
                "userinfo_verified": True,
                "context_verified": True,
            },
        )
        token_call = transport.calls[0]
        self.assertEqual(token_call[0:2], ("post_form", TOKEN_ENDPOINT))
        form = token_call[2]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["client_secret"], "synthetic-client-secret")
        self.assertEqual(form["code_verifier"], transaction.code_verifier)
        self.assertEqual(form["scope"], SCOPE)
        self.assertEqual([call[1] for call in transport.calls[1:]], [JWKS_URI, USERINFO_ENDPOINT, IST_GRAPHQL_ENDPOINT])
        graphql_call = transport.calls[-1]
        self.assertIn("getContext", graphql_call[2]["query"])
        self.assertNotIn("children", graphql_call[2]["query"])

        stored_path = session_path(self.root)
        stored = json.loads(stored_path.read_text())
        self.assertEqual(
            set(stored),
            {
                "access_token",
                "refresh_token",
                "client_secret",
                "client_id",
                "scope",
                "authority",
                "redirect_uri",
                "issued_at",
                "expires_in",
                "subject",
            },
        )
        self.assertNotIn("id_token", stored)
        self.assertEqual(stat.S_IMODE(stored_path.stat().st_mode), 0o600)

    def context_login(self, transport: FakeTransport):
        url, transaction = self.login_start()
        with (
            patch.object(oauth_module, "CONTEXT_RETRY_DELAY_SECONDS", 0),
            self.assertLogs("familiekilder.ist_oauth", "WARNING") as logged,
        ):
            try:
                result = login_with_callback(
                    self.root,
                    transaction,
                    self.callback(url),
                    transport=transport,
                    token_validator=self.claims_validator,
                    access_token_validator=self.access_claims_validator,
                    now=101,
                    wall_time=2_000_000_000,
                )
            except OAuthError as exc:
                result = exc
        history = json.loads(
            (self.root / ".private/credentials/ist-home-auth-history.json").read_text()
        )
        events = [event["event"] for event in history["events"]]
        context_calls = [call for call in transport.calls if call[1] == IST_GRAPHQL_ENDPOINT]
        for line in logged.output:
            self.assertNotIn("synthetic", line)
        return result, events, len(context_calls), logged.output

    def test_context_check_without_answer_is_asked_once_more(self) -> None:
        # 10 Nov 2026: the hosted login got no answer to getContext in 30 s and
        # the verified SkolID tokens were thrown away.
        class SlowFirstContext(FakeTransport):
            def post_json(self, url, payload, *, bearer_token=None):
                super().post_json(url, payload, bearer_token=bearer_token)
                if len([call for call in self.calls if call[0] == "post_json"]) == 1:
                    raise OAuthNoAnswer("no answer")
                return {"data": {"getContext": {"serverTime": "2026-08-28T12:00:00Z"}}}

        result, events, calls, logged = self.context_login(SlowFirstContext())
        self.assertTrue(result["context_verified"])
        self.assertEqual(calls, 2)
        self.assertTrue(session_path(self.root).exists())
        self.assertEqual(events, ["response_received", "request_uncertain", "login_succeeded"])
        self.assertRegex(logged[0], r"ist_context_check attempt=1 outcome=no_answer elapsed_ms=\d+$")
        self.assertRegex(logged[1], r"ist_context_check attempt=2 outcome=answered elapsed_ms=\d+$")

    def test_context_check_that_never_answers_saves_nothing_and_says_so(self) -> None:
        class SilentContext(FakeTransport):
            def post_json(self, url, payload, *, bearer_token=None):
                super().post_json(url, payload, bearer_token=bearer_token)
                raise OAuthNoAnswer("no answer access_token=synthetic")

        result, events, calls, logged = self.context_login(SilentContext())
        self.assertIsInstance(result, OAuthContextNoAnswer)
        self.assertIsNone(result.__context__)
        self.assertIn("IST Home svarte ikke", OAuthContextNoAnswer.login_step_message)
        self.assertEqual(calls, oauth_module.CONTEXT_ATTEMPTS)
        self.assertFalse(session_path(self.root).exists())
        self.assertEqual(
            events,
            ["response_received", "request_uncertain", "request_uncertain", "login_failed"],
        )
        self.assertEqual(len(logged), 2)

    def test_context_rejection_is_final_and_never_saved(self) -> None:
        rejections = {
            "http_401": _OAuthHTTPError(401),
            "http_503": _OAuthHTTPError(503),
            "graphql_errors": {"errors": [{"message": "auth_error synthetic"}]},
            "unexpected_data": {"data": {"getContext": None}},
        }
        for outcome, answer in rejections.items():
            with self.subTest(outcome=outcome):
                self.tearDown()
                self.setUp()

                class Rejecting(FakeTransport):
                    def post_json(self, url, payload, *, bearer_token=None):
                        super().post_json(url, payload, bearer_token=bearer_token)
                        if isinstance(answer, Exception):
                            raise answer
                        return answer

                result, events, calls, logged = self.context_login(Rejecting())
                self.assertIsInstance(result, OAuthError)
                self.assertNotIsInstance(result, OAuthContextNoAnswer)
                self.assertFalse(hasattr(type(result), "login_step_message"))
                self.assertEqual(calls, 1)
                self.assertFalse(session_path(self.root).exists())
                self.assertEqual(events[-1], "login_failed")
                self.assertEqual(len(logged), 1)
                self.assertRegex(
                    logged[0], rf"ist_context_check attempt=1 outcome={outcome} elapsed_ms=\d+$"
                )

    def test_login_rejects_access_token_for_a_different_subject(self) -> None:
        url, transaction = self.login_start()

        def other_subject(_token, _jwks, config, now):
            claims = dict(self.access_claims_validator(_token, _jwks, config, now))
            claims["sub"] = "subject-2"
            return claims

        with self.assertRaisesRegex(OAuthError, "samme bruker"):
            login_with_callback(
                self.root,
                transaction,
                self.callback(url),
                transport=FakeTransport(),
                token_validator=self.claims_validator,
                access_token_validator=other_subject,
                now=101,
                wall_time=2_000_000_000,
            )
        self.assertFalse(session_path(self.root).exists())

    def test_failed_remote_verification_preserves_existing_session(self) -> None:
        self.import_synthetic(_session(issued_at=1_999_999_000))
        record_consent(self.root, at=2_000_000_000)
        before = session_path(self.root).read_bytes()
        values = iter((b"a" * 32, b"b" * 32, b"c" * 32))
        url, transaction = begin_login(self.root, now=100, random_bytes=lambda _length: next(values))
        with self.assertRaisesRegex(OAuthError, "userinfo"):
            login_with_callback(
                self.root,
                transaction,
                self.callback(url),
                transport=FakeTransport(userinfo_sub="other-subject"),
                token_validator=self.claims_validator,
                access_token_validator=self.access_claims_validator,
                now=101,
                wall_time=2_000_000_000,
                replace_existing=True,
            )
        self.assertEqual(session_path(self.root).read_bytes(), before)

    def test_login_recovery_survives_completed_marker_write_and_clear_failure(self) -> None:
        url, transaction = self.login_start()
        old_transaction = "x" * 43
        oauth_module._mark_refresh_blocked(
            self.root,
            "refresh-in-flight",
            at=2_000_000_000,
            transaction_id=old_transaction,
        )
        with failed_refresh_marker_cleanup():
            result = login_with_callback(
                self.root,
                transaction,
                self.callback(url),
                transport=FakeTransport(),
                token_validator=self.claims_validator,
                access_token_validator=self.access_claims_validator,
                now=101,
                wall_time=2_000_000_000,
            )

        self.assertTrue(result["authenticated"])
        self.assertTrue(result["refresh_marker_cleanup_pending"])
        self.assertFalse(auth_status(self.root, at=2_000_000_100)["refresh_blocked"])
        self.assertEqual(
            oauth_module.local_session_for_use(self.root)["subject"],
            "subject-1",
        )
        oauth_module._mark_refresh_blocked(
            self.root,
            "fatal-client-configuration",
            at=2_000_000_000,
        )
        with self.assertRaises(OAuthFatalConfigurationError):
            oauth_module.local_session_for_use(self.root)

    def test_existing_session_requires_explicit_login_replacement(self) -> None:
        self.import_synthetic(_session(issued_at=1_999_999_000))
        record_consent(self.root, at=2_000_000_000)
        values = iter((b"a" * 32, b"b" * 32, b"c" * 32))
        url, transaction = begin_login(self.root, now=100, random_bytes=lambda _length: next(values))
        transport = FakeTransport()
        with self.assertRaisesRegex(OAuthError, "uttrykkelig"):
            login_with_callback(
                self.root, transaction, self.callback(url), transport=transport, now=101
            )
        self.assertFalse(transaction.consumed)
        self.assertEqual(transport.calls, [])
        result = login_with_callback(
            self.root,
            transaction,
            self.callback(url),
            transport=transport,
            token_validator=self.claims_validator,
            access_token_validator=self.access_claims_validator,
            now=101,
            wall_time=2_000_000_000,
            replace_existing=True,
        )
        self.assertTrue(result["authenticated"])

    def test_unverified_import_writes_nothing_and_does_not_chain_secret(self) -> None:
        leaked = "private-validation-detail"

        def reject(_token, _jwks, _config, _now):
            raise RuntimeError(leaked)

        with self.assertRaises(OAuthError) as raised:
            import_session(
                self.root,
                _session(),
                transport=FakeTransport(),
                access_token_validator=reject,
                at=2_000_000_000,
            )
        self.assertFalse(client_config_path(self.root).exists())
        self.assertFalse(session_path(self.root).exists())
        self.assertNotIn(leaked, str(raised.exception))
        self.assertIsNone(raised.exception.__context__)

    def test_import_requires_explicit_consent_before_network_or_storage(self) -> None:
        transport = FakeTransport()
        with self.assertRaisesRegex(OAuthError, "samtykke"):
            import_session(
                self.root,
                _session(),
                transport=transport,
                access_token_validator=self.access_claims_validator,
                at=2_000_000_000,
            )
        self.assertEqual(transport.calls, [])
        self.assertFalse(client_config_path(self.root).exists())
        self.assertFalse(session_path(self.root).exists())

    def test_unknown_token_result_is_not_retried_and_does_not_chain_secret(self) -> None:
        url, transaction = self.login_start()
        leaked = "synthetic-code-or-secret-must-not-leak"
        transport = FakeTransport(fail_token=leaked)
        with self.assertRaises(OAuthError) as raised:
            login_with_callback(
                self.root, transaction, self.callback(url), transport=transport, now=101
            )
        self.assertTrue(transaction.consumed)
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn(leaked, str(raised.exception))
        self.assertIsNone(raised.exception.__context__)
        with self.assertRaisesRegex(OAuthError, "allerede brukt"):
            login_with_callback(self.root, transaction, self.callback(url), transport=transport, now=102)

    def test_import_session_is_validated_atomic_and_keeps_newer_local_session(self) -> None:
        first = self.import_synthetic(json.dumps(_session(issued_at=2_000_000_000)))
        self.assertTrue(first["stored"])
        before = session_path(self.root).read_bytes()
        older = self.import_synthetic(_session(issued_at=1_900_000_000))
        self.assertEqual(older["reason"], "newer-local-session")
        self.assertEqual(session_path(self.root).read_bytes(), before)
        self.assertEqual(stat.S_IMODE(session_path(self.root).stat().st_mode), 0o600)

        invalid = _session(issued_at=2_100_000_000)
        invalid["redirect_uri"] = "https://attacker.invalid/callback"
        with self.assertRaises(OAuthError):
            self.import_synthetic(invalid, replace_existing=True)
        self.assertEqual(session_path(self.root).read_bytes(), before)

        changed_client = self.root / "changed-client.json"
        changed_client.write_text(json.dumps(_client(secret="different-secret")), encoding="utf-8")
        with self.assertRaisesRegex(OAuthError, "Logg ut"):
            configure_client(self.root, changed_client)
        self.assertEqual(session_path(self.root).read_bytes(), before)

    def test_identical_import_is_still_remotely_validated_and_migrates_subject(self) -> None:
        payload = _session()
        self.import_synthetic(payload)
        stored = json.loads(session_path(self.root).read_text())
        stored.pop("subject")
        session_path(self.root).write_text(json.dumps(stored), encoding="utf-8")
        transport = FakeTransport()

        result = import_session(
            self.root,
            payload,
            transport=transport,
            access_token_validator=self.access_claims_validator,
            at=2_000_000_000,
        )

        self.assertFalse(result["stored"])
        self.assertEqual(result["reason"], "same-local-session")
        self.assertEqual(
            [call[1] for call in transport.calls],
            [JWKS_URI, USERINFO_ENDPOINT, IST_GRAPHQL_ENDPOINT],
        )
        self.assertEqual(json.loads(session_path(self.root).read_text())["subject"], "subject-1")

    def test_import_recovery_correlates_an_unknown_marker_before_commit(self) -> None:
        record_consent(self.root, at=2_000_000_000)
        marker = refresh_block_path(self.root)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            '{"version":1,"reason":"unknown-legacy-state","recorded_at":"now"}\n',
            encoding="utf-8",
        )
        marker.chmod(0o600)
        with failed_refresh_marker_cleanup():
            result = import_session(
                self.root,
                _session(),
                transport=FakeTransport(),
                access_token_validator=self.access_claims_validator,
                at=2_000_000_000,
            )

        self.assertTrue(result["stored"])
        self.assertTrue(result["refresh_marker_cleanup_pending"])
        self.assertFalse(auth_status(self.root, at=2_000_000_100)["refresh_blocked"])
        self.assertEqual(
            oauth_module.local_session_for_use(self.root)["subject"],
            "subject-1",
        )

    def test_identical_import_cannot_reopen_an_unknown_refresh_grant(self) -> None:
        payload = _session(issued_at=1_999_999_000)
        self.import_synthetic(payload)
        before = session_path(self.root).read_bytes()
        with self.assertRaises(oauth_module.OAuthRefreshUncertain):
            refresh_session(
                self.root,
                transport=FakeTransport(fail_token="unknown refresh outcome"),
                access_token_validator=self.access_claims_validator,
                at=2_000_000_000,
            )
        import_transport = FakeTransport()

        with self.assertRaisesRegex(OAuthReauthenticationRequired, "faktisk ny grant"):
            import_session(
                self.root,
                payload,
                transport=import_transport,
                access_token_validator=self.access_claims_validator,
                at=2_000_000_100,
            )

        self.assertEqual(import_transport.calls, [])
        self.assertEqual(session_path(self.root).read_bytes(), before)
        self.assertTrue(auth_status(self.root)["refresh_blocked"])

    def test_status_is_offline_and_contains_no_secrets(self) -> None:
        self.assertEqual(
            auth_status(self.root, at=2_000_000_000),
            {
                "configured": False,
                "consent_recorded": False,
                "authenticated": False,
                "access_expires_at": None,
                "access_expired": None,
                "refresh_blocked": False,
                "refresh_block_reason": None,
            },
        )
        self.import_synthetic(_session())
        record_consent(self.root, at=2_000_000_000)
        current = auth_status(self.root, at=2_000_000_100)
        self.assertTrue(current["configured"])
        self.assertTrue(current["authenticated"])
        self.assertFalse(current["access_expired"])
        self.assertNotIn("token", json.dumps(current))
        self.assertNotIn("secret", json.dumps(current))

    def test_refresh_rotates_only_after_full_validation_and_blocks_unknown_retry(self) -> None:
        self.import_synthetic(_session(issued_at=1_999_999_000))
        before = session_path(self.root).read_bytes()
        transport = FakeTransport()
        result = refresh_session(
            self.root,
            transport=transport,
            access_token_validator=self.access_claims_validator,
            at=2_000_000_000,
        )
        self.assertTrue(result["refreshed"])
        self.assertTrue(result["refresh_token_rotated"])
        self.assertFalse(refresh_block_path(self.root).exists())
        after = session_path(self.root).read_bytes()
        self.assertNotEqual(after, before)
        stored = json.loads(after)
        self.assertEqual(stored["refresh_token"], "new-synthetic-refresh-token")
        self.assertNotIn("new-synthetic-refresh-token", json.dumps(result))
        self.assertNotIn(str(stored["access_token"]), json.dumps(result))
        self.assertEqual(
            [call[1] for call in transport.calls],
            [TOKEN_ENDPOINT, JWKS_URI, USERINFO_ENDPOINT, IST_GRAPHQL_ENDPOINT],
        )

        before_failure = session_path(self.root).read_bytes()
        failing = FakeTransport(fail_token="private-refresh-detail")
        with self.assertRaises(oauth_module.OAuthRefreshUncertain) as raised:
            refresh_session(
                self.root,
                transport=failing,
                access_token_validator=self.access_claims_validator,
                at=2_000_000_100,
            )
        self.assertIsNone(raised.exception.__context__)
        self.assertNotIn("private-refresh-detail", str(raised.exception))
        self.assertEqual(session_path(self.root).read_bytes(), before_failure)
        self.assertTrue(refresh_block_path(self.root).exists())
        retry_transport = FakeTransport()
        with self.assertRaises(oauth_module.OAuthRefreshUncertain):
            refresh_session(
                self.root,
                transport=retry_transport,
                access_token_validator=self.access_claims_validator,
                at=2_000_000_200,
            )
        self.assertEqual(retry_transport.calls, [])

    def test_completed_refresh_remains_usable_when_marker_cleanup_fails(self) -> None:
        self.import_synthetic(_session(issued_at=1_999_999_000))
        transport = FakeTransport()

        with patch(
            "familiekilder.ist_oauth._clear_refresh_block",
            side_effect=OAuthError("private-cleanup-detail"),
        ):
            result = refresh_session(
                self.root,
                transport=transport,
                access_token_validator=self.access_claims_validator,
                at=2_000_000_000,
            )

        self.assertTrue(result["refreshed"])
        self.assertTrue(result["refresh_marker_cleanup_pending"])
        stored = json.loads(session_path(self.root).read_text())
        self.assertEqual(stored["refresh_token"], "new-synthetic-refresh-token")
        current = auth_status(self.root, at=2_000_000_100)
        self.assertFalse(current["refresh_blocked"])
        self.assertEqual(current["refresh_block_reason"], "refresh-completed")
        self.assertEqual(
            oauth_module.local_session_for_use(self.root)["access_token"],
            stored["access_token"],
        )
        self.assertEqual(
            len([call for call in transport.calls if call[1] == TOKEN_ENDPOINT]),
            1,
        )

    def test_committed_refresh_is_recognized_if_marker_commit_and_clear_fail(self) -> None:
        self.import_synthetic(_session(issued_at=1_999_999_000))
        transport = FakeTransport()
        with failed_refresh_marker_cleanup():
            result = refresh_session(
                self.root,
                transport=transport,
                access_token_validator=self.access_claims_validator,
                at=2_000_000_000,
            )

        self.assertTrue(result["refresh_marker_cleanup_pending"])
        raw_marker = json.loads(refresh_block_path(self.root).read_text())
        raw_session = json.loads(session_path(self.root).read_text())
        self.assertEqual(raw_marker["reason"], "refresh-in-flight")
        self.assertEqual(
            raw_marker["transaction_id"],
            raw_session[oauth_module.REFRESH_TRANSACTION_FIELD],
        )
        current = auth_status(self.root, at=2_000_000_100)
        self.assertFalse(current["refresh_blocked"])
        self.assertEqual(current["refresh_block_reason"], "refresh-completed")
        self.assertEqual(
            oauth_module.local_session_for_use(self.root)["access_token"],
            raw_session["access_token"],
        )

    def test_refresh_rejects_subject_switch_and_post_response_validation_failure(self) -> None:
        self.import_synthetic(_session(issued_at=1_999_999_000))
        before = session_path(self.root).read_bytes()

        def other_subject(_token, _jwks, config, now):
            claims = dict(self.access_claims_validator(_token, _jwks, config, now))
            claims["sub"] = "subject-2"
            return claims

        with self.assertRaises(OAuthReauthenticationRequired):
            refresh_session(
                self.root,
                transport=FakeTransport(userinfo_sub="subject-2"),
                access_token_validator=other_subject,
                at=2_000_000_000,
            )
        self.assertEqual(session_path(self.root).read_bytes(), before)
        self.assertTrue(refresh_block_path(self.root).exists())

        # Only a genuinely new grant can recover an unknown refresh outcome.
        recovered = _session(issued_at=2_000_000_001)
        recovered["refresh_token"] = "recovered-new-refresh-grant"
        self.import_synthetic(recovered, replace_existing=True)

        def reject_after_response(_token, _jwks, _config, _now):
            raise RuntimeError("private-post-response-detail")

        with self.assertRaises(oauth_module.OAuthRefreshUncertain) as raised:
            refresh_session(
                self.root,
                transport=FakeTransport(),
                access_token_validator=reject_after_response,
                at=2_000_000_100,
            )
        self.assertNotIn("private-post-response-detail", str(raised.exception))
        self.assertTrue(refresh_block_path(self.root).exists())

    def test_fatal_refresh_marker_persists_until_reprovisioning(self) -> None:
        self.import_synthetic(_session(issued_at=1_999_999_000))

        class FatalTransport(FakeTransport):
            def post_form(self, url, fields, *, expect_json=True):
                self.calls.append(("post_form", url, dict(fields), expect_json))
                raise oauth_module._OAuthHTTPError(400, "invalid_client")

        fatal = FatalTransport()
        with self.assertRaises(OAuthFatalConfigurationError):
            refresh_session(
                self.root,
                transport=fatal,
                access_token_validator=self.access_claims_validator,
                at=2_000_000_000,
            )
        current = auth_status(self.root, at=2_000_000_000)
        self.assertEqual(current["refresh_block_reason"], "fatal-client-configuration")

        untouched = FakeTransport()
        with self.assertRaises(OAuthFatalConfigurationError):
            refresh_session(
                self.root,
                transport=untouched,
                access_token_validator=self.access_claims_validator,
                at=2_000_000_100,
            )
        self.assertEqual(untouched.calls, [])

        configure_client(self.root, self.client_source)
        self.assertIsNone(auth_status(self.root)["refresh_block_reason"])

    def test_logout_deletes_local_session_even_when_revocation_is_unconfirmed(self) -> None:
        self.import_synthetic(_session())
        transport = FakeTransport()
        transport.revocation_fail = True
        result = logout(self.root, transport=transport)
        self.assertEqual(result["remote_revocation"], "unconfirmed")
        self.assertTrue(result["local_session_deleted"])
        self.assertFalse(session_path(self.root).exists())
        revocations = [call for call in transport.calls if call[1] == REVOCATION_ENDPOINT]
        self.assertEqual(len(revocations), 2)
        self.assertEqual(logout(self.root, transport=transport)["remote_revocation"], "not-needed")

    def test_logout_reaps_only_owned_regular_abandoned_session_temporaries(self) -> None:
        target = session_path(self.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        (self.root / ".private").chmod(0o700)
        target.parent.chmod(0o700)
        orphan = target.parent / f".{target.name}.crash123.tmp"
        orphan.write_text("crash-secret-refresh-token", encoding="utf-8")
        orphan.chmod(0o600)
        unrelated = target.parent / f".{target.name}.not-a-temp.backup"
        unrelated.write_text("keep", encoding="utf-8")
        unrelated.chmod(0o600)

        result = logout(self.root, transport=FakeTransport())

        self.assertEqual(result["orphaned_session_files_removed"], 1)
        self.assertTrue(result["orphan_cleanup_complete"])
        self.assertEqual(result["remote_revocation"], "unconfirmed")
        self.assertFalse(orphan.exists())
        self.assertEqual(unrelated.read_text(), "keep")

    def test_logout_deletes_local_session_before_uninterruptible_remote_work(self) -> None:
        self.import_synthetic(_session())
        with patch(
            "familiekilder.ist_oauth.UrllibOAuthTransport",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                logout(self.root)
        self.assertFalse(session_path(self.root).exists())

    def test_logout_continues_revocation_if_directory_fsync_cannot_confirm_unlink(self) -> None:
        self.import_synthetic(_session())
        transport = FakeTransport()

        real_fsync = os.fsync
        failed = False

        def fail_first_directory_sync(descriptor):
            nonlocal failed
            if not failed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                failed = True
                raise OSError("directory fsync fault")
            return real_fsync(descriptor)

        with patch(
            "familiekilder.private_state.os.fsync",
            side_effect=fail_first_directory_sync,
        ):
            result = logout(self.root, transport=transport)

        self.assertFalse(session_path(self.root).exists())
        self.assertFalse(result["local_session_delete_durable"])
        self.assertEqual(result["remote_revocation"], "confirmed")
        self.assertEqual(
            len([call for call in transport.calls if call[1] == REVOCATION_ENDPOINT]),
            2,
        )

    def test_logout_still_revokes_when_refresh_marker_cleanup_fails(self) -> None:
        self.import_synthetic(_session())
        transport = FakeTransport()
        with patch(
            "familiekilder.ist_oauth._clear_refresh_block",
            side_effect=OAuthError("private-cleanup-detail"),
        ):
            result = logout(self.root, transport=transport)
        self.assertFalse(result["refresh_block_cleared"])
        self.assertFalse(session_path(self.root).exists())
        self.assertEqual(
            len([call for call in transport.calls if call[1] == REVOCATION_ENDPOINT]),
            2,
        )

    def test_logout_prefers_canonical_client_config_over_valid_embedded_mutation(self) -> None:
        self.import_synthetic(_session())
        malformed = json.loads(session_path(self.root).read_text())
        malformed["client_secret"] = "wrong-but-syntactically-valid-secret"
        session_path(self.root).write_text(json.dumps(malformed), encoding="utf-8")
        transport = FakeTransport()

        result = logout(self.root, transport=transport)

        self.assertEqual(result["remote_revocation"], "confirmed")
        self.assertFalse(session_path(self.root).exists())
        revocations = [call for call in transport.calls if call[1] == REVOCATION_ENDPOINT]
        self.assertEqual(len(revocations), 2)
        self.assertTrue(
            all(call[2]["client_id"] == TEST_CLIENT_ID for call in revocations)
        )
        self.assertTrue(
            all(
                call[2]["client_secret"] == "synthetic-client-secret"
                for call in revocations
            )
        )

    def test_mac_export_contains_client_config_only_and_never_overwrites(self) -> None:
        storage = self.root / "async-storage"
        storage.mkdir()
        app_config = {
            "authority": AUTHORITY,
            "clientId": TEST_CLIENT_ID,
            "clientSecret": "synthetic-export-secret",
            "redirectUri": REDIRECT_URI,
            "scope": SCOPE,
        }
        sessions = {
            "older": {
                "config": app_config,
                "state": {
                    "issued_at": 100,
                    "access_token": "must-not-be-exported",
                    "refresh_token": "must-not-be-exported-either",
                },
            },
            "newer": {
                "config": app_config,
                "state": {"issued_at": 200, "access_token": "private-user-token"},
            },
        }
        (storage / "manifest.json").write_text(
            json.dumps({"_skolid_sdk_sessions": json.dumps(sessions)}), encoding="utf-8"
        )
        destination = self.root / "exports" / "ist-client.json"
        result = export_mac_client_config(destination, async_storage=storage)
        self.assertTrue(result["exported"])
        exported = json.loads(destination.read_text())
        self.assertEqual(exported, _client(secret="synthetic-export-secret"))
        raw = destination.read_text()
        self.assertNotIn("access_token", raw)
        self.assertNotIn("refresh_token", raw)
        self.assertNotIn("private-user-token", raw)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        with self.assertRaisesRegex(OAuthError, "finnes allerede"):
            export_mac_client_config(destination, async_storage=storage)
        self.assertEqual(json.loads(destination.read_text()), exported)

    def test_default_pyjwt_validator_pins_rs256_kid_issuer_audience_azp_exp_and_nonce(self) -> None:
        calls = {}

        class FakePyJWK:
            @classmethod
            def from_dict(cls, value):
                calls["jwk"] = value
                return type("Key", (), {"key": "synthetic-public-key"})()

        class FakeJwt:
            PyJWK = FakePyJWK

            @staticmethod
            def get_unverified_header(_token):
                return {"alg": "RS256", "kid": "key-1"}

            @staticmethod
            def decode(token, **kwargs):
                calls["decode"] = (token, kwargs)
                return {
                    "iss": AUTHORITY,
                    "aud": [TEST_CLIENT_ID, "secondary-audience"],
                    "azp": TEST_CLIENT_ID,
                    "sub": "subject-1",
                    "nonce": "expected-nonce",
                    "exp": 2_000_000_600,
                }

        config = ClientConfig(AUTHORITY, TEST_CLIENT_ID, "secret", REDIRECT_URI, SCOPE)
        jwks = {"keys": [{"kid": "key-1", "kty": "RSA", "use": "sig", "alg": "RS256"}]}
        with patch("familiekilder.ist_oauth_protocol.importlib.import_module", return_value=FakeJwt):
            claims = _pyjwt_token_validator(
                "synthetic.id.token", jwks, config, "expected-nonce", 2_000_000_000
            )
        self.assertEqual(claims["sub"], "subject-1")
        _token, decode_kwargs = calls["decode"]
        self.assertEqual(decode_kwargs["algorithms"], ["RS256"])
        self.assertEqual(decode_kwargs["audience"], TEST_CLIENT_ID)
        self.assertEqual(decode_kwargs["issuer"], AUTHORITY)

        class WrongAlgJwt(FakeJwt):
            @staticmethod
            def get_unverified_header(_token):
                return {"alg": "HS256", "kid": "key-1"}

        with patch("familiekilder.ist_oauth_protocol.importlib.import_module", return_value=WrongAlgJwt):
            with self.assertRaisesRegex(OAuthError, "JWKS"):
                _pyjwt_token_validator(
                    "synthetic.id.token", jwks, config, "expected-nonce", 2_000_000_000
                )


if __name__ == "__main__":
    unittest.main()
