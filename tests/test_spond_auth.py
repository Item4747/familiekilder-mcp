from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from familiekilder import spond_auth
from familiekilder.api_transport import ApiHTTPError, ApiTransportError
from familiekilder.private_state import (
    PrivateStateError,
    atomic_write_private_json,
    read_private_json,
)
from familiekilder.spond_auth import (
    API_ROOT,
    PROFILE_ENDPOINT,
    REFRESH_ENDPOINT,
    SpondAuthError,
    SpondCooldownActive,
    SpondReauthenticationRequired,
    auth_status,
    client_config_path,
    configure_client,
    cooldown_path,
    import_session,
    load_client_config,
    local_session_for_use,
    logout,
    pace_network_request,
    pacing_path,
    refresh_block_path,
    refresh_session,
    session_path,
)
from familiekilder_testing.spond_auth import (
    SpondAuthFixture,
    NOW,
    TEN_DAYS,
    FakeClock,
    FakeAuthTransport,
    _canonical_session,
)


class SpondAuthTests(SpondAuthFixture, unittest.TestCase):
    def _write_import(self, value: Mapping[str, Any], name: str = "session.json") -> Path:
        self._store_provider(attachment_hosts=["files.example.test"])
        directory = self.root / ".private" / "imports"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        (self.root / ".private").chmod(0o700)
        directory.chmod(0o700)
        target = directory / name
        target.write_text(json.dumps(dict(value)), encoding="utf-8")
        target.chmod(0o600)
        return target

    def _write_provider_import(
        self, value: Mapping[str, Any], name: str = "spond-provider.json"
    ) -> Path:
        target = self.root / ".private" / "bootstrap" / name
        atomic_write_private_json(target, dict(value))
        return target

    def test_configure_client_imports_strict_private_profile_atomically(self) -> None:
        source = self._write_provider_import(
            {
                "api_root": API_ROOT,
                "app_version": "4.62.0",
                "attachment_hosts": ["FILES.EXAMPLE.TEST."],
            }
        )

        result = configure_client(self.root, source)

        self.assertEqual(
            result,
            {
                "configured": True,
                "api_root_validated": True,
                "app_version_validated": True,
                "attachment_hosts": 1,
            },
        )
        self.assertNotIn("api.spond.com", json.dumps(result))
        config = load_client_config(self.root)
        self.assertEqual(config.api_root, API_ROOT)
        self.assertEqual(config.app_version, "4.62.0")
        self.assertEqual(config.attachment_hosts, ("files.example.test",))
        target = client_config_path(self.root)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)

    def test_configure_client_mutable_update_does_not_touch_rotating_session(self) -> None:
        self._store_session()
        session_before = session_path(self.root).read_bytes()
        session_metadata = session_path(self.root).stat()
        source = self._write_provider_import(
            {
                "api_root": API_ROOT,
                "app_version": "4.63.0",
                "attachment_hosts": ["new-files.example.test"],
            },
            "updated-spond-provider.json",
        )

        result = configure_client(self.root, source)

        self.assertTrue(result["configured"])
        self.assertEqual(session_path(self.root).read_bytes(), session_before)
        self.assertEqual(session_path(self.root).stat().st_ino, session_metadata.st_ino)
        runtime = local_session_for_use(self.root, at=NOW)
        self.assertEqual(runtime["app_version"], "4.63.0")
        self.assertEqual(runtime["attachment_hosts"], ["new-files.example.test"])

    def test_configure_client_refuses_unsafe_input_and_target_without_replacement(self) -> None:
        self._store_provider(app_version="4.62.0")
        installed_before = client_config_path(self.root).read_bytes()
        outside = self.root / "outside-provider.json"
        outside.write_text(
            json.dumps(
                {
                    "api_root": API_ROOT,
                    "app_version": "4.63.0",
                    "attachment_hosts": [],
                }
            ),
            encoding="utf-8",
        )
        outside.chmod(0o600)
        with self.assertRaises(SpondAuthError):
            configure_client(self.root, outside)

        safe_source = self._write_provider_import(
            {
                "api_root": API_ROOT,
                "app_version": "4.63.0",
                "attachment_hosts": [],
            },
            "safe-source.json",
        )
        broad = self._write_provider_import(
            {
                "api_root": API_ROOT,
                "app_version": "4.63.0",
                "attachment_hosts": [],
            },
            "broad-source.json",
        )
        broad.chmod(0o644)
        with self.assertRaises(SpondAuthError):
            configure_client(self.root, broad)

        linked = safe_source.with_name("linked-source.json")
        linked.symlink_to(safe_source)
        with self.assertRaises(SpondAuthError):
            configure_client(self.root, linked)
        for ordinal, invalid in enumerate(
            (
                {
                    "api_root": "https://attacker.invalid/core/v1",
                    "app_version": "4.63.0",
                    "attachment_hosts": [],
                },
                {
                    "api_root": API_ROOT,
                    "app_version": "4.63.0",
                    "attachment_hosts": [],
                    "unknown": "field",
                },
                {
                    "api_root": API_ROOT,
                    "app_version": "4.63.0",
                    "attachment_hosts": ["files.example.test", "FILES.EXAMPLE.TEST."],
                },
            )
        ):
            invalid_source = self._write_provider_import(
                invalid, f"invalid-{ordinal}.json"
            )
            with self.assertRaises(SpondAuthError):
                configure_client(self.root, invalid_source)
        self.assertEqual(client_config_path(self.root).read_bytes(), installed_before)

        target = client_config_path(self.root)
        target.unlink()
        protected = self.root / "protected-target.json"
        protected.write_text("protected", encoding="utf-8")
        target.symlink_to(protected)
        with self.assertRaises(SpondAuthError):
            configure_client(self.root, safe_source)
        self.assertEqual(protected.read_text(encoding="utf-8"), "protected")

    def test_import_accepts_canonical_and_localstorage_exports_after_one_probe(self) -> None:
        for ordinal, exported in enumerate(
            (
                _canonical_session(),
                {
                    "token": "browser-access-secret",
                    "tokenExpiration": "2033-05-18T04:33:20+00:00",
                    "refreshToken": "browser-refresh-secret",
                    "refreshTokenExpiration": "2033-05-19T04:33:20+00:00",
                },
            )
        ):
            root = self.root / f"case-{ordinal}"
            root.mkdir()
            self.root = root
            transport = FakeAuthTransport()
            result = import_session(
                root,
                self._write_import(exported),
                transport=transport,
                at=NOW,
                pacing_clock=FakeClock(),
                sleep=lambda _seconds: None,
            )

            self.assertEqual(transport.gets[0][0], PROFILE_ENDPOINT)
            self.assertEqual(len(transport.gets), 1)
            self.assertTrue(result["authenticated"])
            self.assertTrue(result["remote_verified"])
            self.assertNotIn("access_token", result)
            self.assertNotIn("refresh_token", result)
            stored = read_private_json(session_path(root))
            self.assertEqual(stored["profile_id"], "profile-1")
            self.assertNotIn("attachment_hosts", stored)
            self.assertEqual(
                local_session_for_use(root, at=NOW)["attachment_hosts"],
                ["files.example.test"],
            )
            self.assertEqual(session_path(root).stat().st_mode & 0o777, 0o600)

    def test_mutable_provider_update_is_live_without_rewriting_rotating_session(self) -> None:
        self._store_session()
        before = session_path(self.root).read_bytes()

        self._store_provider(
            app_version="4.63.0",
            attachment_hosts=["new-files.example.test"],
        )
        runtime = local_session_for_use(self.root, at=NOW)

        self.assertEqual(runtime["api_root"], API_ROOT)
        self.assertEqual(runtime["app_version"], "4.63.0")
        self.assertEqual(runtime["attachment_hosts"], ["new-files.example.test"])
        self.assertEqual(session_path(self.root).read_bytes(), before)
        self.assertEqual(session_path(self.root).stat().st_mode & 0o777, 0o600)
        self.assertEqual(client_config_path(self.root).stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            client_config_path(self.root).parent.stat().st_mode & 0o777, 0o700
        )

    def test_malformed_or_symlinked_provider_profile_fails_closed(self) -> None:
        self._store_session()
        session_before = session_path(self.root).read_bytes()
        atomic_write_private_json(
            client_config_path(self.root),
            {
                "api_root": "https://attacker.invalid/core/v1",
                "app_version": "4.63.0",
                "attachment_hosts": [],
            },
        )
        with self.assertRaises(SpondAuthError):
            load_client_config(self.root)
        with self.assertRaises(SpondAuthError):
            local_session_for_use(self.root, at=NOW)
        self.assertEqual(session_path(self.root).read_bytes(), session_before)

        target = client_config_path(self.root)
        target.unlink()
        outside = self.root / "outside-provider.json"
        outside.write_text(
            json.dumps(
                {
                    "api_root": API_ROOT,
                    "app_version": "4.63.0",
                    "attachment_hosts": [],
                }
            ),
            encoding="utf-8",
        )
        outside.chmod(0o600)
        target.symlink_to(outside)
        with self.assertRaises(SpondAuthError):
            load_client_config(self.root)
        self.assertEqual(session_path(self.root).read_bytes(), session_before)

    def test_import_requires_private_owned_file_and_rejects_internal_fields(self) -> None:
        outside = self.root / "session.json"
        outside.write_text(json.dumps(_canonical_session()), encoding="utf-8")
        outside.chmod(0o600)
        with self.assertRaises(SpondAuthError):
            import_session(self.root, outside, transport=FakeAuthTransport(), at=NOW)

        for forbidden in (
            "profile_id",
            "verified_at",
            "session_transaction_id",
            "updated_at",
            "unknown",
        ):
            value = _canonical_session()
            value[forbidden] = "attacker-controlled"
            transport = FakeAuthTransport()
            with self.assertRaises(SpondAuthError):
                import_session(
                    self.root,
                    self._write_import(value, f"{forbidden}.json"),
                    transport=transport,
                    at=NOW,
                )
            self.assertEqual(transport.gets, [])

    def test_existing_session_needs_explicit_replace_before_any_network(self) -> None:
        self._store_session()
        transport = FakeAuthTransport()

        with self.assertRaises(SpondAuthError):
            import_session(
                self.root,
                self._write_import(
                    _canonical_session(
                        access_token="new-access-secret",
                        refresh_token="new-refresh-secret",
                    )
                ),
                transport=transport,
                at=NOW,
            )

        self.assertEqual(transport.gets, [])
        self.assertEqual(read_private_json(session_path(self.root))["access_token"], "old-access-secret")

    def test_failed_replace_probe_preserves_old_session_without_refresh_marker(self) -> None:
        self._store_session()
        old = dict(read_private_json(session_path(self.root)))
        transport = FakeAuthTransport(profile=ApiHTTPError(401))

        with self.assertRaises(SpondReauthenticationRequired):
            import_session(
                self.root,
                self._write_import(
                    _canonical_session(
                        access_token="new-access-secret",
                        refresh_token="new-refresh-secret",
                    )
                ),
                replace_existing=True,
                transport=transport,
                at=NOW,
                pacing_clock=FakeClock(),
            )

        self.assertEqual(len(transport.gets), 1)
        self.assertEqual(read_private_json(session_path(self.root)), old)
        self.assertFalse(refresh_block_path(self.root).exists())
        self.assertEqual(local_session_for_use(self.root, at=NOW)["access_token"], "old-access-secret")

    def test_atomic_replace_failure_preserves_old_session_and_does_not_block_it(self) -> None:
        self._store_session()
        old = dict(read_private_json(session_path(self.root)))
        original_write = spond_auth.atomic_write_private_json

        def fail_session_write(path: Path | str, value: Any, **kwargs: Any) -> None:
            if Path(path) == session_path(self.root):
                raise PrivateStateError("simulated safe failure")
            original_write(path, value, **kwargs)

        with patch.object(
            spond_auth, "atomic_write_private_json", side_effect=fail_session_write
        ):
            with self.assertRaises(SpondAuthError):
                import_session(
                    self.root,
                    self._write_import(
                        _canonical_session(
                            access_token="new-access-secret",
                            refresh_token="new-refresh-secret",
                        )
                    ),
                    replace_existing=True,
                    transport=FakeAuthTransport(),
                    at=NOW,
                    pacing_clock=FakeClock(),
                )

        self.assertEqual(read_private_json(session_path(self.root)), old)
        self.assertFalse(refresh_block_path(self.root).exists())
        self.assertEqual(local_session_for_use(self.root, at=NOW)["access_token"], "old-access-secret")

    def test_pacing_is_persistent_and_cannot_be_bypassed(self) -> None:
        first = FakeClock(NOW)
        pace_network_request(self.root, clock=first, sleep=first.sleep)
        second = FakeClock(NOW + 1)
        pace_network_request(self.root, clock=second, sleep=second.sleep)

        self.assertEqual(second.sleeps, [1.0])
        self.assertEqual(read_private_json(pacing_path(self.root))["last_request_at"], NOW + 2)
        with self.assertRaises(SpondAuthError):
            pace_network_request(
                self.root,
                clock=FakeClock(NOW + 3),
                sleep=lambda _seconds: None,
                minimum_interval=0,
            )
        with self.assertRaises(SpondAuthError):
            pace_network_request(
                self.root,
                clock=FakeClock(NOW + 1),
                sleep=lambda _seconds: None,
            )

    def test_import_429_persists_full_retry_after_and_never_retries(self) -> None:
        transport = FakeAuthTransport(
            profile=ApiHTTPError(429, retry_after_seconds=TEN_DAYS)
        )
        with self.assertRaises(SpondCooldownActive):
            import_session(
                self.root,
                self._write_import(_canonical_session()),
                transport=transport,
                at=NOW,
                pacing_clock=FakeClock(),
            )

        self.assertEqual(len(transport.gets), 1)
        cooldown = read_private_json(cooldown_path(self.root))
        self.assertEqual(cooldown["until"], NOW + TEN_DAYS)
        self.assertFalse(session_path(self.root).exists())

    def test_refresh_rotates_once_and_never_returns_secrets(self) -> None:
        self._store_session()
        transport = FakeAuthTransport()
        result = refresh_session(
            self.root,
            transport=transport,
            at=NOW,
            pacing_clock=FakeClock(),
        )

        self.assertEqual(len(transport.posts), 1)
        self.assertEqual(transport.posts[0][0], REFRESH_ENDPOINT)
        self.assertEqual(transport.posts[0][1], {"value": "old-refresh-secret"})
        self.assertTrue(result["refresh_token_rotated"])
        self.assertNotIn("access_token", result)
        self.assertNotIn("refresh_token", result)
        self.assertTrue(result["remote_verified"])
        stored = read_private_json(session_path(self.root))
        self.assertEqual(stored["access_token"], "rotated-access-secret")
        self.assertEqual(stored["verified_at"], NOW - 10)
        self.assertFalse(refresh_block_path(self.root).exists())
        status = auth_status(self.root, at=NOW)
        self.assertTrue(status["authenticated"])
        self.assertTrue(status["remote_verified"])

    def test_refresh_never_commits_expired_or_immediately_expiring_rotated_tokens(self) -> None:
        self._store_session()
        old = dict(read_private_json(session_path(self.root)))
        transport = FakeAuthTransport(
            refresh={
                "accessToken": {
                    "token": "unusable-access-secret",
                    "expiration": NOW + 30,
                },
                "refreshToken": {
                    "token": "unusable-refresh-secret",
                    "expiration": NOW + 30,
                },
            }
        )

        with self.assertRaises(SpondReauthenticationRequired):
            refresh_session(
                self.root,
                transport=transport,
                at=NOW,
                pacing_clock=FakeClock(),
            )

        self.assertEqual(len(transport.posts), 1)
        self.assertEqual(read_private_json(session_path(self.root)), old)
        self.assertTrue(refresh_block_path(self.root).exists())

    def test_refresh_429_stops_once_and_persists_full_cooldown(self) -> None:
        self._store_session()
        transport = FakeAuthTransport(
            refresh=ApiHTTPError(429, retry_after_seconds=TEN_DAYS)
        )

        with self.assertRaises(SpondCooldownActive):
            refresh_session(
                self.root,
                transport=transport,
                at=NOW,
                pacing_clock=FakeClock(),
            )

        self.assertEqual(len(transport.posts), 1)
        self.assertFalse(refresh_block_path(self.root).exists())
        self.assertEqual(read_private_json(cooldown_path(self.root))["until"], NOW + TEN_DAYS)

    def test_refresh_5xx_and_network_failures_stop_once_block_grant_and_cool_down(self) -> None:
        for ordinal, failure in enumerate((ApiHTTPError(503), ApiTransportError("secret-network-detail"))):
            root = self.root / f"refresh-{ordinal}"
            root.mkdir()
            self.root = root
            self._store_session()
            transport = FakeAuthTransport(refresh=failure)

            with self.assertRaises(SpondReauthenticationRequired):
                refresh_session(
                    root,
                    transport=transport,
                    at=NOW,
                    pacing_clock=FakeClock(),
                )

            self.assertEqual(len(transport.posts), 1)
            self.assertTrue(refresh_block_path(root).exists())
            self.assertEqual(read_private_json(cooldown_path(root))["until"], NOW + 900)
            with self.assertRaises(SpondReauthenticationRequired):
                local_session_for_use(root, at=NOW)

    def test_corrupt_private_reasons_are_fixed_and_never_echoed(self) -> None:
        self._store_session()
        secret = "attacker-private-secret"
        atomic_write_private_json(
            refresh_block_path(self.root),
            {"reason": secret, "recorded_at": "2033-01-01T00:00:00+00:00"},
        )
        atomic_write_private_json(
            cooldown_path(self.root),
            {"reason": secret, "until": NOW + 60, "status_code": 429},
        )

        status = auth_status(self.root, at=NOW)
        rendered = json.dumps(status, sort_keys=True)
        self.assertNotIn(secret, rendered)
        self.assertEqual(status["refresh_block_reason"], "invalid-marker")
        self.assertEqual(status["cooldown_reason"], "invalid-cooldown")
        self.assertTrue(status["cooldown_active"])

    def test_injected_time_controls_session_expiry_and_status_has_no_secrets(self) -> None:
        self._store_session(
            _canonical_session(
                access_token="do-not-print-access",
                refresh_token="do-not-print-refresh",
                access_expires_at=NOW + 50,
                refresh_expires_at=NOW + 100,
                stored=True,
            )
        )

        with self.assertRaises(SpondReauthenticationRequired):
            local_session_for_use(self.root, at=NOW + 101)
        rendered = json.dumps(auth_status(self.root, at=NOW), sort_keys=True)
        self.assertNotIn("do-not-print-access", rendered)
        self.assertNotIn("do-not-print-refresh", rendered)

    def test_logout_is_local_only(self) -> None:
        self._store_session()
        atomic_write_private_json(
            refresh_block_path(self.root),
            {
                "reason": "refresh-rejected",
                "recorded_at": "2033-05-18T03:33:20+00:00",
            },
        )

        result = logout(self.root)

        self.assertFalse(result["authenticated"])
        self.assertTrue(result["session_removed"])
        self.assertTrue(result["refresh_marker_removed"])
        self.assertFalse(session_path(self.root).exists())


if __name__ == "__main__":
    unittest.main()
