from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from familiekilder.api_transport import ApiHTTPError, ApiTransportError
from familiekilder.hoopit_auth import (
    API_ROOT,
    FIREBASE_PROJECT_ID,
    SECURE_TOKEN_ROOT,
    HoopitAuthError,
    HoopitReauthenticationRequired,
    HoopitReadAccessUnverified,
    HoopitReadAccessDenied,
    SessionCredential,
    _record_direct_access_proof,
    _require_direct_access_proof,
    _record_direct_access_rejection,
    _direct_access_rejected,
    _direct_access_rejection_path,
    DIRECT_ACCESS_RETRY_SECONDS,
    auth_status,
    client_config_path,
    configure_client,
    direct_access_proof_path,
    import_session,
    logout,
    refresh_session,
    refresh_state_path,
    session_for_use,
    session_path,
)
from familiekilder.private_state import read_private_json
from familiekilder.source_adapter import SourceUnavailable


NOW = 2_000_000_000.0
KID = "fixture-key-1"


def _keys() -> tuple[str, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "firebase-test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


CERTIFICATE, PRIVATE_PEM = _keys()


def _token(
    *,
    at: float = NOW,
    subject: str = "user-1",
    key: bytes = PRIVATE_PEM,
    kid: str = KID,
) -> str:
    return jwt.encode(
        {
            "iss": f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}",
            "aud": FIREBASE_PROJECT_ID,
            "sub": subject,
            "iat": int(at),
            "exp": int(at + 3600),
        },
        key,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _config() -> dict[str, Any]:
    return {
        "api_root": API_ROOT,
        "firebase_api_key": "AIza" + "x" * 36,
        "firebase_project_id": FIREBASE_PROJECT_ID,
        "app_version": "Web 2026.08-test",
        "attachment_hosts": ["files.example.test"],
    }


class FakeLimiter:
    def __init__(self) -> None:
        self.before = 0
        self.success = 0
        self.http: list[tuple[int, int | None]] = []
        self.network = 0

    def before_request(self) -> None:
        self.before += 1

    def record_success(self) -> None:
        self.success += 1

    def record_http_failure(self, status_code: int, retry_after: int | None = None) -> None:
        self.http.append((status_code, retry_after))

    def record_network_failure(self) -> None:
        self.network += 1


class FakeCertTransport:
    def __init__(self, value: Any = None) -> None:
        self.value = {KID: CERTIFICATE} if value is None else value
        self.calls: list[str] = []

    def get_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.calls.append(url)
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class FakeRefreshTransport:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def post_form(
        self,
        url: str,
        fields: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.calls.append((url, dict(fields), dict(headers or {})))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class HoopitAuthTests(unittest.TestCase):
    def test_config_fingerprint_rejects_unicode_without_breaking_legacy_identity(self):
        from familiekilder import hoopit_auth
        config = hoopit_auth._parse_client_config(_config())
        for fingerprint in (hoopit_auth._config_fingerprint(config),
                            hoopit_auth._legacy_config_fingerprint(config)):
            self.assertTrue(hoopit_auth._fingerprint_matches_config(fingerprint, config))
            for invalid in (None, '', fingerprint[:-1], fingerprint + 'ø',
                            fingerprint[:12] + 'ø' + fingerprint[12:]):
                with self.subTest(fingerprint=invalid):
                    self.assertFalse(hoopit_auth._fingerprint_matches_config(invalid, config))

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _private_file(self, name: str, value: Mapping[str, Any]) -> Path:
        directory = self.root / ".private" / "imports"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        (self.root / ".private").chmod(0o700)
        directory.chmod(0o700)
        path = directory / name
        path.write_text(json.dumps(dict(value)), encoding="utf-8")
        path.chmod(0o600)
        return path

    def _configure(self) -> None:
        result = configure_client(
            self.root, self._private_file("hoopit-client.json", _config())
        )
        self.assertTrue(result["configured"])
        self.assertNotIn("firebase_api_key", result)

    def _import(self, *, at: float = NOW, token: str | None = None) -> FakeLimiter:
        self._configure()
        limiter = FakeLimiter()
        result = import_session(
            self.root,
            self._private_file(
                "hoopit-session-export.json",
                {"id_token": token or _token(at=at), "refresh_token": "refresh-secret"},
            ),
            verification_transport=FakeCertTransport(),
            rate_limiter=limiter,
            at=at,
        )
        self.assertTrue(result["authenticated"])
        return limiter

    def test_configure_is_strict_private_and_has_no_app_check_contract(self) -> None:
        self._configure()
        self.assertEqual(os.stat(client_config_path(self.root)).st_mode & 0o777, 0o600)

        for change in (
            {"api_root": "https://evil.example"},
            {"firebase_project_id": "other"},
            {"attachment_hosts": ["127.0.0.1"]},
            {"app_check_provider": "external"},
        ):
            value = _config()
            value.update(change)
            with self.assertRaises(HoopitAuthError):
                configure_client(
                    self.root / str(change.get("firebase_project_id", "bad")),
                    self._private_file("changed.json", value),
                )

    def test_first_provider_read_verifies_saved_import_without_user_login(self):
        from familiekilder import hoopit, live_readers
        self._import()
        self.assertTrue(auth_status(self.root, at=NOW)['authenticated'])
        self.assertFalse(auth_status(self.root, at=NOW)['direct_access_verified'])
        class ReadTransport:
            calls = 0
            def get_json(inner, *args, **kwargs):
                inner.calls += 1
                return {'results': [], 'has_next': False}
        transport = ReadTransport()
        with (patch('time.time', return_value=NOW),
              patch.object(hoopit, 'refresh_session') as refresh,
              patch.object(hoopit, 'default_transport', return_value=transport),
              patch.object(hoopit, 'PersistentRateLimiter', return_value=FakeLimiter())):
            with live_readers.provider(self.root, 'hoopit'):
                pass
            with live_readers.provider(self.root, 'hoopit'):
                pass
        refresh.assert_called_once()
        self.assertEqual(transport.calls, 1)
        self.assertTrue(auth_status(self.root, at=NOW)['direct_access_verified'])

    def test_denied_verification_waits_then_allows_bounded_recheck(self):
        from familiekilder import hoopit
        self._import()
        class DeniedTransport:
            calls = 0
            def get_json(inner, *args, **kwargs):
                inner.calls += 1
                raise ApiHTTPError(403)
        transport = DeniedTransport()
        with (patch('time.time', return_value=NOW),
              patch.object(hoopit, 'refresh_session') as refresh,
              patch.object(hoopit, 'default_transport', return_value=transport),
              patch.object(hoopit, 'PersistentRateLimiter', return_value=FakeLimiter())):
            for _ in range(2):
                with self.assertRaises(HoopitReadAccessDenied) as caught:
                    hoopit.ensure_direct_access(self.root)
                self.assertTrue(caught.exception.retry_at)
            refresh.assert_called_once()
        self.assertEqual(transport.calls, 1)
        self.assertFalse(auth_status(self.root, at=NOW)['direct_access_verified'])
        self.assertTrue(auth_status(self.root, at=NOW)['authenticated'])
        later = NOW + DIRECT_ACCESS_RETRY_SECONDS + 1
        self.assertIsNone(_direct_access_rejected(self.root, at=later))
        with (patch('time.time', return_value=later),
              patch.object(hoopit, 'verify_direct_access') as verify):
            with self.assertRaises(HoopitReadAccessUnverified):
                hoopit.ensure_direct_access(self.root)
        verify.assert_called_once()  # No proof means no client despite a success return.

    def test_rejection_is_private_bound_and_malformed_state_fails_closed(self):
        self._import()
        _record_direct_access_rejection(self.root, session_for_use(self.root, at=NOW), at=NOW)
        target = _direct_access_rejection_path(self.root)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        text = target.read_text()
        for sensitive in ['user-1', 'refresh-secret', _token(), _config()['firebase_api_key']]:
            self.assertNotIn(sensitive, text)
        self.assertTrue(_direct_access_rejected(self.root, at=NOW))
        value = json.loads(text)
        value['session_identity_fingerprint'] = '0' * 64
        target.write_text(json.dumps(value))
        self.assertIsNone(_direct_access_rejected(self.root, at=NOW))
        target.write_text('{}')
        with self.assertRaises(HoopitReadAccessUnverified):
            _direct_access_rejected(self.root, at=NOW)

    def test_genuine_firebase_rejection_does_not_start_api_verification(self):
        from familiekilder import hoopit
        self._import()
        with (patch.object(hoopit, '_require_direct_access_proof', side_effect=HoopitReauthenticationRequired('rejected')),
              patch.object(hoopit, 'verify_direct_access') as verify):
            with self.assertRaises(HoopitReauthenticationRequired):
                hoopit.ensure_direct_access(self.root)
        verify.assert_not_called()

    def test_mutable_profile_metadata_updates_without_reimporting_firebase_grant(self) -> None:
        self._import()
        mutable = _config()
        mutable.update(
            {
                "app_version": "Web 2026.09-test",
                "attachment_hosts": ["cdn.example.test"],
            }
        )

        configure_client(
            self.root, self._private_file("hoopit-client-mutable.json", mutable)
        )

        access = session_for_use(self.root, at=NOW)
        self.assertEqual(access.app_version, "Web 2026.09-test")
        self.assertEqual(access.attachment_hosts, ("cdn.example.test",))

        changed_identity = _config()
        changed_identity["firebase_api_key"] = "AIza" + "y" * 36
        with self.assertRaisesRegex(HoopitAuthError, "auth-identiteten"):
            configure_client(
                self.root,
                self._private_file("hoopit-client-immutable.json", changed_identity),
            )

    def test_direct_access_proof_is_private_secret_free_and_currently_bound(self) -> None:
        self._import()
        access = session_for_use(self.root, at=NOW)

        result = _record_direct_access_proof(self.root, access, at=NOW)

        self.assertTrue(result["recorded"])
        proof_path = direct_access_proof_path(self.root)
        self.assertEqual(proof_path.stat().st_mode & 0o777, 0o600)
        proof = dict(read_private_json(proof_path))
        self.assertEqual(
            set(proof),
            {
                "version",
                "verified_at",
                "auth_identity_fingerprint",
                "app_release_fingerprint",
                "session_identity_fingerprint",
            },
        )
        rendered = json.dumps(proof, sort_keys=True)
        for sensitive in (
            "user-1",
            "refresh-secret",
            access.id_token,
            _config()["firebase_api_key"],
        ):
            self.assertNotIn(sensitive, rendered)
        status = auth_status(self.root, at=NOW)
        self.assertTrue(status["direct_access_proof_present"])
        self.assertTrue(status["direct_access_verified"])
        self.assertEqual(status["direct_access_proof_state"], "verified")
        self.assertNotIn("user-1", json.dumps(status, sort_keys=True))
        _require_direct_access_proof(self.root, at=NOW)

    def test_refresh_rotation_preserves_proof_but_session_replacement_removes_it(self) -> None:
        self._import()
        _record_direct_access_proof(
            self.root, session_for_use(self.root, at=NOW), at=NOW
        )
        refresh_at = NOW + 3601
        refresh_session(
            self.root,
            transport=FakeRefreshTransport(
                {
                    "id_token": _token(at=refresh_at),
                    "refresh_token": "rotated-refresh-secret",
                    "expires_in": "3600",
                    "user_id": "user-1",
                }
            ),
            verification_transport=FakeCertTransport(),
            rate_limiter=FakeLimiter(),
            at=refresh_at,
        )
        self.assertTrue(auth_status(self.root, at=refresh_at)["direct_access_verified"])

        import_session(
            self.root,
            self._private_file(
                "replacement-same-user.json",
                {
                    "id_token": _token(at=refresh_at),
                    "refresh_token": "replacement-refresh-secret",
                },
            ),
            replace_existing=True,
            verification_transport=FakeCertTransport(),
            rate_limiter=FakeLimiter(),
            at=refresh_at,
        )
        replaced = auth_status(self.root, at=refresh_at)
        self.assertFalse(replaced["direct_access_proof_present"])
        self.assertFalse(replaced["direct_access_verified"])
        with self.assertRaises(HoopitReadAccessUnverified):
            _require_direct_access_proof(self.root, at=refresh_at)

    def test_app_release_or_provider_release_change_invalidates_proof(self) -> None:
        self._import()
        access = session_for_use(self.root, at=NOW)
        with patch(
            "familiekilder.hoopit_auth._provider_release_context",
            return_value={"generation": 1, "provenance": {"release": "a"}},
        ):
            _record_direct_access_proof(self.root, access, at=NOW)
        with patch(
            "familiekilder.hoopit_auth._provider_release_context",
            return_value={"generation": 2, "provenance": {"release": "b"}},
        ):
            status = auth_status(self.root, at=NOW)
        self.assertFalse(status["direct_access_verified"])
        self.assertEqual(status["direct_access_proof_state"], "mismatch")

        mutable = _config()
        mutable["app_version"] = "Web 2026.09-test"
        configure_client(
            self.root, self._private_file("new-release.json", mutable)
        )
        changed = auth_status(self.root, at=NOW)
        self.assertFalse(changed["direct_access_proof_present"])
        self.assertFalse(changed["direct_access_verified"])

    def test_attachment_metadata_change_preserves_current_release_proof(self) -> None:
        self._import()
        _record_direct_access_proof(
            self.root, session_for_use(self.root, at=NOW), at=NOW
        )
        mutable = _config()
        mutable["attachment_hosts"] = ["new-files.example.test"]
        configure_client(
            self.root, self._private_file("new-attachments.json", mutable)
        )
        status = auth_status(self.root, at=NOW)
        self.assertTrue(status["direct_access_proof_present"])
        self.assertTrue(status["direct_access_verified"])

    def test_tampered_proof_and_logout_fail_closed_without_identity_output(self) -> None:
        self._import()
        _record_direct_access_proof(
            self.root, session_for_use(self.root, at=NOW), at=NOW
        )
        proof_path = direct_access_proof_path(self.root)
        proof = dict(read_private_json(proof_path))
        proof["session_identity_fingerprint"] = "0" * 64
        proof_path.write_text(json.dumps(proof), encoding="utf-8")
        proof_path.chmod(0o600)
        status = auth_status(self.root, at=NOW)
        self.assertFalse(status["direct_access_verified"])
        self.assertEqual(status["direct_access_proof_state"], "mismatch")
        with self.assertRaises(HoopitReadAccessUnverified):
            _require_direct_access_proof(self.root, at=NOW)

        result = logout(self.root)
        self.assertTrue(result["direct_access_proof_removed"])
        self.assertFalse(proof_path.exists())

    def test_import_reuses_exact_token_pair_after_signature_verification(self) -> None:
        limiter = self._import()

        self.assertEqual((limiter.before, limiter.success), (1, 1))
        stored = read_private_json(session_path(self.root))
        self.assertEqual(stored["refresh_token"], "refresh-secret")
        self.assertEqual(stored["subject"], "user-1")
        self.assertNotIn("app_check_token", stored)
        self.assertEqual(session_path(self.root).stat().st_mode & 0o777, 0o600)
        credential = SessionCredential("id-secret", "refresh-secret")
        self.assertNotIn("id-secret", repr(credential))
        self.assertNotIn("refresh-secret", repr(credential))

    def test_import_requires_private_exact_file_and_preserves_existing_session(self) -> None:
        self._configure()
        outside = self.root / "session.json"
        outside.write_text(
            json.dumps({"id_token": _token(), "refresh_token": "refresh-secret"}),
            encoding="utf-8",
        )
        outside.chmod(0o600)
        with self.assertRaises(HoopitAuthError):
            import_session(
                self.root,
                outside,
                verification_transport=FakeCertTransport(),
                rate_limiter=FakeLimiter(),
                at=NOW,
            )

        self._import()
        old = dict(read_private_json(session_path(self.root)))
        certs = FakeCertTransport()
        with self.assertRaises(HoopitAuthError):
            import_session(
                self.root,
                self._private_file(
                    "replacement.json",
                    {"id_token": _token(), "refresh_token": "new-refresh"},
                ),
                verification_transport=certs,
                rate_limiter=FakeLimiter(),
                at=NOW,
            )
        self.assertEqual(certs.calls, [])
        self.assertEqual(read_private_json(session_path(self.root)), old)

    def test_invalid_signature_and_expired_access_are_not_imported(self) -> None:
        self._configure()
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        bad_token = _token(
            key=other_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        for name, token, at in (
            ("bad", bad_token, NOW),
            ("expired", _token(), NOW + 3601),
        ):
            with self.subTest(name=name), self.assertRaises(HoopitAuthError):
                import_session(
                    self.root,
                    self._private_file(
                        f"{name}.json",
                        {"id_token": token, "refresh_token": "refresh-secret"},
                    ),
                    verification_transport=FakeCertTransport(),
                    rate_limiter=FakeLimiter(),
                    at=at,
                )
        self.assertFalse(session_path(self.root).exists())

    def test_expired_access_refreshes_without_app_check_header(self) -> None:
        self._import()
        refresh_at = NOW + 3601
        fresh = _token(at=refresh_at)
        transport = FakeRefreshTransport(
            {
                "id_token": fresh,
                "refresh_token": "rotated-refresh-secret",
                "expires_in": "3600",
                "user_id": "user-1",
            }
        )
        limiter = FakeLimiter()
        result = refresh_session(
            self.root,
            transport=transport,
            verification_transport=FakeCertTransport(),
            rate_limiter=limiter,
            at=refresh_at,
        )

        self.assertTrue(result["authenticated"])
        self.assertTrue(result["refresh_token_rotated"])
        self.assertEqual((limiter.before, limiter.success), (2, 2))
        url, fields, headers = transport.calls[0]
        self.assertTrue(url.startswith(SECURE_TOKEN_ROOT + "?key="))
        self.assertEqual(fields["grant_type"], "refresh_token")
        self.assertEqual(fields["refresh_token"], "refresh-secret")
        self.assertEqual(headers, {})
        access = session_for_use(self.root, at=refresh_at + 1)
        self.assertEqual(access.expires_at, refresh_at + 3600)

    def test_network_failure_stops_once_but_does_not_discard_reusable_grant(self) -> None:
        self._import()
        refresh_at = NOW + 3601
        failed = FakeRefreshTransport(ApiTransportError("secret response"))
        limiter = FakeLimiter()
        with self.assertRaises(SourceUnavailable):
            refresh_session(
                self.root,
                transport=failed,
                verification_transport=FakeCertTransport(),
                rate_limiter=limiter,
                at=refresh_at,
            )
        self.assertEqual(len(failed.calls), 1)
        self.assertEqual(limiter.network, 1)
        self.assertFalse(refresh_state_path(self.root).exists())

    def test_rejected_refresh_is_blocked_without_second_call(self) -> None:
        self._import()
        _record_direct_access_proof(
            self.root, session_for_use(self.root, at=NOW), at=NOW
        )
        refresh_at = NOW + 3601
        rejected = FakeRefreshTransport(ApiHTTPError(400))
        with self.assertRaises(HoopitReauthenticationRequired):
            refresh_session(
                self.root,
                transport=rejected,
                verification_transport=FakeCertTransport(),
                rate_limiter=FakeLimiter(),
                at=refresh_at,
            )
        second = FakeRefreshTransport({})
        with self.assertRaises(HoopitReauthenticationRequired):
            refresh_session(
                self.root,
                transport=second,
                verification_transport=FakeCertTransport(),
                rate_limiter=FakeLimiter(),
                at=refresh_at + 1,
            )
        self.assertEqual(second.calls, [])
        self.assertTrue(auth_status(self.root, at=refresh_at)["refresh_blocked"])
        self.assertFalse(direct_access_proof_path(self.root).exists())

    def test_expired_access_still_reports_a_renewable_authenticated_session(self) -> None:
        self._import()
        status = auth_status(self.root, at=NOW + 3601)
        rendered = json.dumps(status, sort_keys=True)
        self.assertTrue(status["authenticated"])
        self.assertTrue(status["access_expired"])
        self.assertNotIn("refresh-secret", rendered)

    def test_logout_is_local_only(self) -> None:
        self._import()
        result = logout(self.root)
        self.assertTrue(result["session_removed"])
        self.assertFalse(result["remote_revocation_attempted"])
        self.assertFalse(session_path(self.root).exists())


if __name__ == "__main__":
    unittest.main()
