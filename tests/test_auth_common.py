from __future__ import annotations

import unittest

from familiekilder import hoopit_auth, vigilo_auth
from familiekilder.auth_common import (
    base64url,
    canonical_sha256,
    fingerprint_matches,
    has_invalid_percent_encoding,
    is_plain_text,
    normalize_public_host,
    random_urlsafe,
    safe_call,
    utc_iso,
)


class Failure(Exception):
    pass


class StoredFingerprintTests(unittest.TestCase):
    """Sessions on disk store these digests; they were computed before the refactor."""

    def test_vigilo_fingerprints_are_unchanged(self):
        config = vigilo_auth._parse_client_config({
            "authority": vigilo_auth.ISSUER,
            "client_id": "fixture-client.ANDROID",
            "client_secret": "test-only-sécret",
            "redirect_uri": vigilo_auth.REDIRECT_URI,
            "scope": "openid vigiloprofile offline_access",
            "api_root": vigilo_auth.API_ROOT,
            "app_version": "Android 9.9.9-tést",
            "attachment_hosts": ["files.example.test", "bücher.example"],
        })
        self.assertEqual(
            vigilo_auth._config_fingerprint(config),
            "70a7daab989336802427a4ad9e8773c1ab4de6a631113da4cd7f2c0a14c3c424",
        )
        self.assertEqual(
            vigilo_auth._legacy_config_fingerprint(config),
            "19fa71b846ab5ac5a0437f663c9ad6778ee109e84a99aac245f4a5456ac1ffe8",
        )

    def test_hoopit_fingerprints_are_unchanged(self):
        config = hoopit_auth._parse_client_config({
            "api_root": hoopit_auth.API_ROOT,
            "firebase_api_key": "AIza" + "x" * 35,
            "firebase_project_id": hoopit_auth.FIREBASE_PROJECT_ID,
            "app_version": "Hoopit 1.2 «ø»",
            "attachment_hosts": ["cdn.example.test", "Bücher.Example."],
        })
        self.assertEqual(
            hoopit_auth._config_fingerprint(config),
            "c2a5dd80d23363afbdb491f698cab1a6eac273af4957d062cd4f0924762ac10d",
        )
        self.assertEqual(
            hoopit_auth._legacy_config_fingerprint(config),
            "c8b3282c0b5591c0144b41ce6fedfaeeea6d1a563b26d4065616c8235f0d7f4f",
        )
        self.assertEqual(
            hoopit_auth._session_identity_fingerprint("sübject", "a" * 64),
            "0e71433c575c3ee2c790e647510de82779b50ba39ba82ab4052ec4e29c2f56f1",
        )

    def test_ensure_ascii_changes_the_digest_only_for_non_ascii(self):
        self.assertEqual(canonical_sha256({"a": 1}), canonical_sha256({"a": 1}, ensure_ascii=False))
        self.assertNotEqual(canonical_sha256({"a": "ø"}), canonical_sha256({"a": "ø"}, ensure_ascii=False))


class HelperTests(unittest.TestCase):
    def test_plain_text(self):
        self.assertTrue(is_plain_text("a b", maximum=3))
        self.assertFalse(is_plain_text("a b", maximum=3, allow_space=False))
        for value in ("", "abcd", "a\tb", "a\x7f", None, 5):
            with self.subTest(value=value):
                self.assertFalse(is_plain_text(value, maximum=3))

    def test_public_host(self):
        cases = {
            "Bücher.de.": ("xn--bcher-kva.de", "xn--bcher-kva.de"),
            "8.8.8.8": ("8.8.8.8", "8.8.8.8"),
            "*.x.com": (None, "*.x.com"),
            "a_b.com": (None, "a_b.com"),
            "ok .com": (None, "ok .com"),
            "a..b": (None, None),
            "localhost": (None, None),
            "a.localhost": (None, None),
            "10.0.0.1": (None, None),
            "::1": (None, None),
            "x/y": (None, None),
            "": (None, None),
            None: (None, None),
        }
        for value, (strict, lax) in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_public_host(value), strict)
                self.assertEqual(normalize_public_host(value, strict=False), lax)

    def test_fingerprint_matches(self):
        digest = "ab" * 32
        self.assertTrue(fingerprint_matches(digest, ("x", digest)))
        for value in (None, "", digest[:-1], digest + "ø"):
            with self.subTest(value=value):
                self.assertFalse(fingerprint_matches(value, (digest,)))

    def test_random_urlsafe(self):
        self.assertEqual(random_urlsafe(lambda n: b"\xff" * n, Failure), base64url(b"\xff" * 32))
        for source in (lambda n: b"x", lambda n: "x" * n):
            with self.assertRaisesRegex(Failure, "Sikker tilfeldig generator"):
                random_urlsafe(source, Failure)

    def test_safe_call_drops_the_injected_exception(self):
        def leak():
            raise RuntimeError("token-123")

        with self.assertRaises(Failure) as caught:
            safe_call("bekreftelse feilet", leak, Failure)
        self.assertEqual(str(caught.exception), "bekreftelse feilet")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(safe_call("x", lambda: 7, Failure), 7)

    def test_percent_encoding_and_time(self):
        self.assertFalse(has_invalid_percent_encoding("a%20b"))
        self.assertTrue(has_invalid_percent_encoding("a%2"))
        self.assertTrue(has_invalid_percent_encoding("a%zz"))
        self.assertEqual(utc_iso(0), "1970-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
