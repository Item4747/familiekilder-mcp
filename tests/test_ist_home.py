from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from familiekilder import ist_oauth as ist_oauth_module
from familiekilder.core import (
    FamiliekilderError,
    ensure_private_layout,
    parse_iso,
    project_paths,
)
from familiekilder.ist_home import (
    IstHomeAuthError,
    IstHomeFatalConfigurationError,
    _credentials,
    _jwt_exp,
    access_token,
    fetch_snapshot,
    posts_in_window,
)

ALL_OK = {source: ("ok", None) for source in ("vigilo", "skolemelding", "spond", "hoopit", "ist-home")}


class FamiliekilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        ensure_private_layout(project_paths(self.root))

    def tearDown(self) -> None:
        self.temp.cleanup()


    def test_corrupt_session_error_does_not_retain_secret_json_context(self) -> None:
        credential = self.root / ".private" / "credentials" / "ist-home-session.json"
        credential.parent.mkdir(parents=True)
        credential.write_text('{"refresh_token":"private-secret",', encoding="utf-8")
        with self.assertRaises(FamiliekilderError) as raised:
            _credentials(self.root)
        self.assertNotIn("private-secret", str(raised.exception))
        self.assertIsNone(raised.exception.__context__)
        self.assertIsNone(raised.exception.__cause__)

    def test_access_token_requires_consent_and_rejects_symlinked_session(self) -> None:
        credential = self.root / ".private" / "credentials" / "ist-home-session.json"
        credential.parent.mkdir(parents=True, exist_ok=True)
        credential.write_text('{"access_token":"unconsented-private-token"}', encoding="utf-8")
        with self.assertRaises(IstHomeAuthError):
            access_token(self.root, now=2_000_000_000)

        credential.unlink()
        scope = (
            "openid offline_access notification miniforum profile "
            "skolid.full-access consent live-translate-service"
        )
        client = {
            "authority": ist_oauth_module.AUTHORITY,
            "client_id": ist_oauth_module.REDIRECT_URI.split(":", 1)[0],
            "client_secret": "synthetic-client-secret",
            "redirect_uri": ist_oauth_module.REDIRECT_URI,
            "scope": scope,
        }
        source = self.root / "client.json"
        source.write_text(json.dumps(client), encoding="utf-8")
        ist_oauth_module.configure_client(self.root, source)
        ist_oauth_module.record_consent(self.root, at=2_000_000_000)
        external = self.root / "external-ist-session.json"
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": 2_000_010_000}).encode()
        ).decode().rstrip("=")
        external.write_text(
            json.dumps(
                {
                    **client,
                    "access_token": f"header.{payload}.signature",
                    "refresh_token": "synthetic-refresh-token",
                    "issued_at": 2_000_000_000,
                    "expires_in": 3600,
                    "subject": "subject-1",
                }
            ),
            encoding="utf-8",
        )
        try:
            credential.symlink_to(external)
        except (NotImplementedError, OSError):
            self.skipTest("symbolske lenker støttes ikke i testmiljøet")
        with self.assertRaises(IstHomeAuthError):
            access_token(self.root, now=2_000_000_100)
        self.assertIn("synthetic-refresh-token", external.read_text())

    def test_structurally_invalid_expired_session_requires_login(self) -> None:
        scope = (
            "openid offline_access notification miniforum profile "
            "skolid.full-access consent live-translate-service"
        )
        client = {
            "authority": ist_oauth_module.AUTHORITY,
            "client_id": ist_oauth_module.REDIRECT_URI.split(":", 1)[0],
            "client_secret": "synthetic-client-secret",
            "redirect_uri": ist_oauth_module.REDIRECT_URI,
            "scope": scope,
        }
        source = self.root / "client.json"
        source.write_text(json.dumps(client), encoding="utf-8")
        ist_oauth_module.configure_client(self.root, source)
        ist_oauth_module.record_consent(self.root, at=2_000_000_000)
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": 1_999_999_000}).encode()
        ).decode().rstrip("=")
        session = {
            **client,
            "access_token": f"header.{payload}.signature",
            "refresh_token": "",
            "issued_at": 1_999_990_000,
            "expires_in": 3600,
            "subject": "subject-1",
        }
        path = ist_oauth_module.session_path(self.root)
        path.write_text(json.dumps(session), encoding="utf-8")

        with self.assertRaises(IstHomeAuthError) as raised:
            access_token(self.root, now=2_000_000_000)
        self.assertIsNone(raised.exception.__context__)


    def test_norwegian_timezone_dst_and_ambiguous_dates(self) -> None:
        self.assertEqual(parse_iso("2026-01-15T08:00:00", "event_start"), "2026-01-15T07:00:00+00:00")
        self.assertEqual(parse_iso("2026-07-15T08:00:00", "event_start"), "2026-07-15T06:00:00+00:00")
        with self.assertRaisesRegex(FamiliekilderError, "tvetydig lokal tid"):
            parse_iso("2026-10-25T02:30:00", "event_start")
        with self.assertRaisesRegex(FamiliekilderError, "tvetydig lokal tid|ikke-eksisterende lokal tid"):
            parse_iso("2026-03-29T02:30:00", "event_start")
        with self.assertRaisesRegex(FamiliekilderError, "tvetydig/ugyldig dato"):
            parse_iso("neste torsdag", "deadline")
        self.assertIsNone(parse_iso(None, "deadline"))


    def test_ist_posts_are_paginated_and_only_posts_are_requested(self) -> None:
        calls: list[dict | None] = []

        def fake_graphql(token: str, query: str, variables: dict | None = None) -> dict:
            self.assertEqual(token, "synthetic-token")
            calls.append(variables)
            if variables is None:
                self.assertIn("getContext", query)
                return {"getContext": {"serverTime": "2026-08-28T10:00:00", "children": [{"id": "child-1"}]}}
            cursor = variables["input"]["page"].get("cursor")
            if cursor is None:
                return {
                    "getBlogPosts": {
                        "nodes": [{"id": "post-1"}],
                        "page": {"endCursor": "next-page", "hasNextPage": True},
                        "minRetentionTime": 30,
                    }
                }
            return {
                "getBlogPosts": {
                    "nodes": [{"id": "post-2"}],
                    "page": {"endCursor": None, "hasNextPage": False},
                    "minRetentionTime": 30,
                }
            }

        with patch("familiekilder.ist_home.graphql", side_effect=fake_graphql):
            snapshot = fetch_snapshot("synthetic-token")
        self.assertEqual([post["id"] for post in snapshot["posts"]], ["post-1", "post-2"])
        self.assertEqual(calls[1]["input"]["childIds"], ["child-1"])
        self.assertEqual(calls[2]["input"]["page"]["cursor"], "next-page")


    def test_ist_window_keeps_new_and_recently_edited_posts_only(self) -> None:
        posts = [
            {"id": "old", "publishedAt": "2026-07-01T08:00:00Z", "editedAt": None},
            {"id": "edited", "publishedAt": "2026-07-01T08:00:00Z", "editedAt": "2026-08-25T08:00:00Z"},
            {"id": "new", "publishedAt": "2026-08-26T08:00:00Z", "editedAt": None},
            {"id": "missing-time", "publishedAt": None, "editedAt": None},
        ]
        selected = posts_in_window(posts, "2026-08-20T00:00:00+00:00")
        self.assertEqual([post["id"] for post in selected], ["edited", "new"])

    def test_ist_jwt_expiry_is_read_without_exposing_token(self) -> None:
        import base64

        payload = base64.urlsafe_b64encode(json.dumps({"exp": 1234567890}).encode()).decode().rstrip("=")
        self.assertEqual(_jwt_exp(f"header.{payload}.signature"), 1234567890)

    def test_fatal_refresh_marker_blocks_even_a_locally_unexpired_token(self) -> None:
        marker = self.root / ".private" / "credentials" / "ist-home-refresh-blocked.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.parent.chmod(0o700)
        marker.write_text(
            json.dumps(
                {
                    "version": 1,
                    "reason": "fatal-client-configuration",
                    "recorded_at": "2033-05-18T03:33:20+00:00",
                }
            ),
            encoding="utf-8",
        )
        marker.chmod(0o600)
        with self.assertRaises(IstHomeFatalConfigurationError):
            access_token(self.root, now=2_000_000_000)


if __name__ == "__main__":
    unittest.main()
