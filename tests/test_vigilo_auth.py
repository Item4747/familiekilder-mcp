from __future__ import annotations

import importlib.util
import json
import unittest
import urllib.parse
from typing import Any, Mapping
from unittest import mock

from familiekilder import vigilo_auth
from familiekilder.api_transport import ApiHTTPError
from familiekilder.private_state import PrivateStateError
from familiekilder.source_adapter import SourceLoginRequired
from familiekilder.vigilo_auth import (
    REDIRECT_URI,
    TOKEN_ENDPOINT,
    VigiloOAuthError,
    _pyjwt_access_token_validator,
    auth_status,
    begin_login,
    configure_client,
    consent_status,
    load_client_config,
    local_session_for_use,
    login_with_callback,
    logout,
    refresh_session,
    session_path,
)
from familiekilder_testing.vigilo_auth import (
    VigiloAuthFixture,
    SECRET,
    _config,
    _claims,
    _validator,
    FakeAuthTransport,
)


class VigiloAuthTests(VigiloAuthFixture, unittest.TestCase):
    def test_config_fingerprint_rejects_unicode_without_breaking_legacy_identity(self):
        config = vigilo_auth._parse_client_config(_config())
        for fingerprint in (vigilo_auth._config_fingerprint(config),
                            vigilo_auth._legacy_config_fingerprint(config)):
            self.assertTrue(vigilo_auth._fingerprint_matches_config(fingerprint, config))
            for invalid in (None, '', fingerprint[:-1], fingerprint + 'ø',
                            fingerprint[:12] + 'ø' + fingerprint[12:]):
                with self.subTest(fingerprint=invalid):
                    self.assertFalse(vigilo_auth._fingerprint_matches_config(invalid, config))

    def test_config_consent_mobile_contract_and_secret_redaction(self) -> None:
        self.configure()
        config = load_client_config(self.root)
        self.assertEqual(config.app_version, "Android 9.9.9-test")
        self.assertNotIn(SECRET, repr(config))
        self.assertTrue(consent_status(self.root)["current"])

        transport = FakeAuthTransport(
            [{"access_token": "access-A", "refresh_token": "refresh-A"}]
        )
        result = self.login(transport)
        self.assertEqual(
            result,
            {
                "authenticated": True,
                "expires_at": "2027-01-15T09:00:00+00:00",
                "access_token_verified": True,
                "userinfo_verified": True,
                "tenant_verified": True,
            },
        )
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn("access-A", repr(result))
        self.assertNotIn("refresh-A", repr(result))
        token_call = transport.posts[0]
        self.assertEqual(token_call[0], TOKEN_ENDPOINT)
        self.assertEqual(
            set(token_call[1]), {"grant_type", "code", "redirect_uri"}
        )
        self.assertEqual(
            list(token_call[1]), ["code", "redirect_uri", "grant_type"]
        )
        self.assertNotIn("client_secret", token_call[1])
        self.assertTrue(token_call[2]["Authorization"].startswith("Basic "))
        self.assertEqual(session_path(self.root).stat().st_mode & 0o777, 0o600)
        self.assertEqual(session_path(self.root).parent.stat().st_mode & 0o777, 0o700)
        status = auth_status(self.root, at=1_800_000_100)
        self.assertTrue(status["session_present"])
        self.assertEqual(status["expires_at"], status["access_expires_at"])
        self.assertNotIn(SECRET, json.dumps(status))

    def test_config_import_rejects_symlink_and_broad_permissions(self) -> None:
        private = self.root / ".private"
        private.mkdir(mode=0o700, exist_ok=True)
        private.chmod(0o700)
        bootstrap = private / "bootstrap"
        bootstrap.mkdir(mode=0o700, parents=True, exist_ok=True)
        bootstrap.chmod(0o700)
        real = bootstrap / "real.json"
        real.write_text(json.dumps(_config()), encoding="utf-8")
        real.chmod(0o600)
        link = bootstrap / "link.json"
        link.symlink_to(real)
        with self.assertRaises(VigiloOAuthError):
            configure_client(self.root, link)

        broad = bootstrap / "broad.json"
        broad.write_text(json.dumps(_config()), encoding="utf-8")
        broad.chmod(0o644)
        with self.assertRaises(VigiloOAuthError):
            configure_client(self.root, broad)

        local_host = bootstrap / "local-host.json"
        local_host.write_text(
            json.dumps({**_config(), "attachment_hosts": ["localhost"]}),
            encoding="utf-8",
        )
        local_host.chmod(0o600)
        with self.assertRaises(VigiloOAuthError):
            configure_client(self.root, local_host)

    def test_mutable_profile_metadata_updates_without_reauthentication(self) -> None:
        self.configure()
        self.login(
            FakeAuthTransport(
                [{"access_token": "access-A", "refresh_token": "refresh-A"}]
            )
        )
        bootstrap = self.root / ".private" / "bootstrap"
        updated = bootstrap / "vigilo-client-updated.json"
        updated.write_text(
            json.dumps(
                {
                    **_config(),
                    "app_version": "Android 10.0.0-test",
                    "attachment_hosts": ["cdn.example.test"],
                }
            ),
            encoding="utf-8",
        )
        updated.chmod(0o600)

        configure_client(self.root, updated)

        session = local_session_for_use(self.root)
        self.assertEqual(session["app_version"], "Android 10.0.0-test")
        self.assertEqual(session["attachment_hosts"], ["cdn.example.test"])

        changed_identity = bootstrap / "vigilo-client-changed-identity.json"
        changed_identity.write_text(
            json.dumps({**_config(), "client_id": "different-client.ANDROID"}),
            encoding="utf-8",
        )
        changed_identity.chmod(0o600)
        with self.assertRaisesRegex(VigiloOAuthError, "OAuth-identitet"):
            configure_client(self.root, changed_identity)

    def test_callback_state_mismatch_is_retryable_and_never_posts(self) -> None:
        self.configure()
        _url, transaction = begin_login(
            self.root, now=10, random_bytes=lambda size: b"y" * size
        )
        transport = FakeAuthTransport([])
        with self.assertRaises(VigiloOAuthError):
            login_with_callback(
                self.root,
                transaction,
                REDIRECT_URI + "?code=fixture&state=wrong",
                transport=transport,
                access_token_validator=_validator,
                now=11,
            )
        self.assertFalse(transaction.consumed)
        self.assertEqual(transport.posts, [])

    def test_root_slash_callback_matches_authority_only_redirect(self) -> None:
        self.configure()
        _url, transaction = begin_login(
            self.root, now=10, random_bytes=lambda size: b"r" * size
        )
        callback = (
            REDIRECT_URI
            + "/?code=fixture-code&state="
            + urllib.parse.quote(transaction.state)
        )
        transport = FakeAuthTransport(
            [{"access_token": "access-A", "refresh_token": "refresh-A"}]
        )

        result = login_with_callback(
            self.root,
            transaction,
            callback,
            transport=transport,
            access_token_validator=_validator,
            now=11,
            wall_time=1_800_000_000,
        )

        self.assertTrue(result["authenticated"])

    def test_manual_callback_is_accepted_after_slow_bankid_approval(self) -> None:
        self.configure()
        _url, transaction = begin_login(
            self.root, now=10, random_bytes=lambda size: b"w" * size
        )
        callback = (
            REDIRECT_URI
            + "?code=fixture-code&state="
            + urllib.parse.quote(transaction.state)
        )
        transport = FakeAuthTransport(
            [{"access_token": "access-A", "refresh_token": "refresh-A"}]
        )

        result = login_with_callback(
            self.root,
            transaction,
            callback,
            transport=transport,
            access_token_validator=_validator,
            now=10 + (29 * 60),
            wall_time=1_800_000_000,
        )

        self.assertTrue(result["authenticated"])

    def test_injected_validation_failure_does_not_chain_or_print_token(self) -> None:
        self.configure()
        _url, transaction = begin_login(
            self.root, now=10, random_bytes=lambda size: b"z" * size
        )
        callback = REDIRECT_URI + "?code=fixture&state=" + transaction.state
        transport = FakeAuthTransport(
            [{"access_token": "access-secret", "refresh_token": "refresh-secret"}]
        )

        def unsafe_validator(*_args: Any) -> Mapping[str, Any]:
            raise RuntimeError("access-secret refresh-secret")

        with self.assertRaises(VigiloOAuthError) as raised:
            login_with_callback(
                self.root,
                transaction,
                callback,
                transport=transport,
                access_token_validator=unsafe_validator,
                now=11,
                wall_time=1_800_000_000,
            )
        self.assertNotIn("secret", str(raised.exception))
        self.assertIsNone(raised.exception.__context__)

    def test_refresh_rotates_and_preserves_identity_binding(self) -> None:
        self.configure()
        self.login(
            FakeAuthTransport(
                [{"access_token": "access-A", "refresh_token": "refresh-A"}]
            )
        )
        transport = FakeAuthTransport(
            [{"access_token": "access-B", "refresh_token": "refresh-B"}]
        )
        result = refresh_session(
            self.root,
            transport=transport,
            access_token_validator=_validator,
            at=1_800_000_100,
        )
        self.assertTrue(result["refresh_token_rotated"])
        session = local_session_for_use(self.root)
        self.assertEqual(session["access_token"], "access-B")
        self.assertEqual(session["refresh_token"], "refresh-B")
        self.assertEqual(session["subject"], "subject-1")
        self.assertEqual(session["tenant_id"], "tenant-1")
        self.assertFalse(auth_status(self.root)["refresh_blocked"])

    def test_committed_refresh_survives_marker_cleanup_failure(self) -> None:
        self.configure()
        self.login(
            FakeAuthTransport(
                [{"access_token": "access-A", "refresh_token": "refresh-A"}]
            )
        )
        transport = FakeAuthTransport(
            [{"access_token": "access-B", "refresh_token": "refresh-B"}]
        )
        with mock.patch.object(
            vigilo_auth,
            "remove_private_file",
            side_effect=PrivateStateError("fixture cleanup failure"),
        ):
            result = refresh_session(
                self.root,
                transport=transport,
                access_token_validator=_validator,
                at=1_800_000_100,
            )
        self.assertTrue(result["refresh_marker_cleanup_pending"])
        self.assertEqual(local_session_for_use(self.root)["access_token"], "access-B")
        self.assertFalse(auth_status(self.root)["refresh_blocked"])

    def test_rejected_or_unknown_refresh_is_durably_blocked(self) -> None:
        for failure in (
            ApiHTTPError(400, "invalid_grant"),
            RuntimeError("refresh-A secret"),
        ):
            with self.subTest(failure=type(failure).__name__):
                case_root = self.tmp_path / type(failure).__name__
                case_root.mkdir()
                original_root = self.root
                self.root = case_root
                try:
                    self.configure()
                    self.login(
                        FakeAuthTransport(
                            [
                                {
                                    "access_token": "access-A",
                                    "refresh_token": "refresh-A",
                                }
                            ]
                        )
                    )
                    transport = FakeAuthTransport([failure])
                    with self.assertRaises(SourceLoginRequired if isinstance(failure, ApiHTTPError) else vigilo_auth.VigiloRefreshUncertain) as raised:
                        refresh_session(
                            self.root,
                            transport=transport,
                            access_token_validator=_validator,
                            at=1_800_000_100,
                        )
                    self.assertNotIn("refresh-A", str(raised.exception))
                    self.assertTrue(auth_status(self.root)["refresh_blocked"])
                    with self.assertRaises(SourceLoginRequired if isinstance(failure, ApiHTTPError) else vigilo_auth.VigiloRefreshUncertain):
                        local_session_for_use(self.root)

                    second = FakeAuthTransport(
                        [
                            {
                                "access_token": "access-C",
                                "refresh_token": "refresh-C",
                            }
                        ]
                    )
                    with self.assertRaises(SourceLoginRequired if isinstance(failure, ApiHTTPError) else vigilo_auth.VigiloRefreshUncertain):
                        refresh_session(
                            self.root,
                            transport=second,
                            access_token_validator=_validator,
                            at=1_800_000_200,
                        )
                    self.assertEqual(second.posts, [])
                finally:
                    self.root = original_root

    @unittest.skipUnless(
        importlib.util.find_spec("jwt") is not None
        and importlib.util.find_spec("cryptography") is not None,
        "PyJWT/cryptography are not installed in this interpreter",
    )
    def test_signed_access_token_pins_issuer_client_scope_and_subject(self) -> None:
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from familiekilder.vigilo_auth import _parse_client_config

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_jwk = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key())
        )
        public_jwk.update({"kid": "fixture-key", "use": "sig", "alg": "RS256"})
        parsed_config = _parse_client_config(_config())
        claims = _claims(exp=1_800_001_000)
        token = jwt.encode(
            claims,
            private_key,
            algorithm="RS256",
            headers={"kid": "fixture-key"},
        )
        result = _pyjwt_access_token_validator(
            token, {"keys": [public_jwk]}, parsed_config, 1_800_000_000
        )
        self.assertEqual(result["sub"], "subject-1")

        wrong = jwt.encode(
            {**claims, "aud": ""},
            private_key,
            algorithm="RS256",
            headers={"kid": "fixture-key"},
        )
        with self.assertRaises(VigiloOAuthError):
            _pyjwt_access_token_validator(
                wrong, {"keys": [public_jwk]}, parsed_config, 1_800_000_000
            )

    def test_logout_removes_tokens_and_reports_only_aggregate_status(self) -> None:
        self.configure()
        self.login(
            FakeAuthTransport(
                [{"access_token": "access-A", "refresh_token": "refresh-A"}]
            )
        )
        transport = FakeAuthTransport([])
        result = logout(self.root, transport=transport)
        self.assertTrue(result["local_session_removed"])
        self.assertEqual(result["revocation_attempts"], 2)
        self.assertEqual(result["revocation_successes"], 2)
        self.assertNotIn("access-A", repr(result))
        self.assertNotIn("refresh-A", repr(result))
        self.assertFalse(session_path(self.root).exists())

    def test_logout_does_not_claim_revocation_for_an_unreadable_session(self) -> None:
        self.configure()
        target = session_path(self.root)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text("not-json", encoding="utf-8")
        target.chmod(0o600)

        result = logout(self.root, transport=FakeAuthTransport([]))

        self.assertTrue(result["local_session_removed"])
        self.assertEqual(result["revocation_attempts"], 0)
        self.assertFalse(result["remote_revocation_complete"])


if __name__ == "__main__":
    unittest.main()
