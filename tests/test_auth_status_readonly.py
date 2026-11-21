from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from familiekilder import (
    hoopit_auth,
    ist_oauth,
    skolemelding_auth,
    spond_auth,
    vigilo_auth,
)


class AuthStatusReadonlyTests(unittest.TestCase):
    def test_pristine_status_calls_do_not_create_private_state(self) -> None:
        providers = {
            vigilo_auth: {
                "configured",
                "consent_recorded",
                "authenticated",
                "session_present",
                "access_expires_at",
                "expires_at",
                "access_expired",
                "tenant_bound",
                "refresh_blocked",
                "refresh_block_reason",
            },
            skolemelding_auth: {
                "configured",
                "consent_recorded",
                "session_present",
                "authenticated",
                "expires_at",
                "refresh_blocked",
                "refresh_block_reason",
                "device_id_present",
                "refresh_response_saved",
                "refresh_diagnostic",
            },
            spond_auth: {
                "configured",
                "authenticated",
                "remote_verified",
                "session_present",
                "expires_at",
                "access_expires_at",
                "access_expired",
                "refresh_expires_at",
                "refresh_blocked",
                "refresh_block_reason",
                "cooldown_active",
                "cooldown_until",
                "cooldown_reason",
            },
            ist_oauth: {
                "configured",
                "consent_recorded",
                "authenticated",
                "access_expires_at",
                "access_expired",
                "refresh_blocked",
                "refresh_block_reason",
            },
        }
        for provider, expected_fields in providers.items():
            with self.subTest(provider=provider.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                before = list(root.iterdir())
                status = provider.auth_status(root)
                after = list(root.iterdir())
                self.assertEqual(before, [])
                self.assertEqual(after, [])
                self.assertEqual(set(status), expected_fields)
                self.assertFalse(status["configured"])
                self.assertFalse(status["authenticated"])

    def test_hoopit_status_needs_no_setup_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = hoopit_auth.auth_status(root)
            self.assertEqual(list(root.iterdir()), [])
            self.assertTrue(status["configured"])
            self.assertFalse(status["authenticated"])
            self.assertFalse(status["session_present"])
            self.assertEqual(
                status["required_scopes"], ["posts:read", "events:read", "events:respond"]
            )


if __name__ == "__main__":
    unittest.main()
