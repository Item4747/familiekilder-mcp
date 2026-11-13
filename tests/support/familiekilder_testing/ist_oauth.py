"""Fakes for IST Home's OAuth login: client configuration, sessions and a transport."""

from __future__ import annotations
import base64
import json
import tempfile
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from familiekilder import ist_oauth as oauth_module
from familiekilder.ist_oauth import (
    begin_login,
    configure_client,
    consent_status,
    import_session,
    record_consent,
    AUTHORITY,
    IST_GRAPHQL_ENDPOINT,
    JWKS_URI,
    REDIRECT_URI,
    REVOCATION_ENDPOINT,
    USERINFO_ENDPOINT,
    OAuthError,
)


SCOPE = "openid offline_access notification miniforum profile skolid.full-access consent live-translate-service"
TEST_CLIENT_ID = urllib.parse.urlsplit(REDIRECT_URI).scheme


def _client(*, scope: str = SCOPE, secret: str = "synthetic-client-secret") -> dict[str, object]:
    return {
        "authority": AUTHORITY,
        "client_id": TEST_CLIENT_ID,
        "client_secret": secret,
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
    }


def _jwt_with_exp(expiry: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.synthetic-signature"


def _session(*, issued_at: int = 2_000_000_000, expires_in: int = 3600) -> dict[str, object]:
    return {
        **_client(),
        "access_token": _jwt_with_exp(issued_at + expires_in),
        "refresh_token": "synthetic-refresh-token",
        "issued_at": issued_at,
        "expires_in": expires_in,
    }


@contextmanager
def failed_refresh_marker_cleanup():
    """Fail completion and cleanup while allowing in-flight marker writes."""
    original_mark = oauth_module._mark_refresh_blocked

    def fail_completed(root, reason, *, at, transaction_id=None):
        if reason == "refresh-completed":
            raise OSError("marker commit fault")
        return original_mark(root, reason, at=at, transaction_id=transaction_id)

    with (
        patch.object(oauth_module, "_mark_refresh_blocked", side_effect=fail_completed),
        patch.object(
            oauth_module, "_clear_refresh_block", side_effect=OAuthError("marker clear fault")
        ),
    ):
        yield


class FakeTransport:
    def __init__(self, *, fail_token: str | None = None, userinfo_sub: str = "subject-1") -> None:
        self.fail_token = fail_token
        self.userinfo_sub = userinfo_sub
        self.calls: list[tuple[object, ...]] = []
        self.revocation_fail = False

    def post_form(self, url, fields, *, expect_json=True):
        self.calls.append(("post_form", url, dict(fields), expect_json))
        if url == REVOCATION_ENDPOINT:
            if self.revocation_fail:
                raise RuntimeError(str(fields.get("token")))
            return None
        if self.fail_token is not None:
            raise RuntimeError(self.fail_token)
        return {
            "access_token": _jwt_with_exp(2_000_010_000),
            "refresh_token": "new-synthetic-refresh-token",
            "id_token": "synthetic.id.token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": SCOPE,
        }

    def get_json(self, url, *, bearer_token=None):
        self.calls.append(("get_json", url, bearer_token))
        if url == JWKS_URI:
            return {"keys": [{"kid": "key-1", "kty": "RSA", "use": "sig", "alg": "RS256"}]}
        if url == USERINFO_ENDPOINT:
            return {"sub": self.userinfo_sub, "name": "must-not-be-returned"}
        raise AssertionError("unexpected URL")

    def post_json(self, url, payload, *, bearer_token=None):
        self.calls.append(("post_json", url, dict(payload), bearer_token))
        if url != IST_GRAPHQL_ENDPOINT:
            raise AssertionError("unexpected URL")
        return {"data": {"getContext": {"serverTime": "2026-08-28T12:00:00Z"}}}


class IstOAuthFixture:
    """A temporary folder with an IST client file, and helpers to log in or import a session."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.client_source = self.root / "client-source.json"
        self.client_source.write_text(json.dumps(_client()), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def configure_and_consent(self) -> None:
        configure_client(self.root, self.client_source)
        record_consent(self.root, at=2_000_000_000)

    def login_start(self, *, now: float = 100.0):
        self.configure_and_consent()
        values = iter((b"s" * 32, b"n" * 32, b"v" * 32))
        return begin_login(self.root, now=now, random_bytes=lambda _length: next(values))

    def callback(self, url: str, code: str = "synthetic-code") -> str:
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        return f"{REDIRECT_URI}?code={code}&state={urllib.parse.quote(state)}&iss={urllib.parse.quote(AUTHORITY, safe='')}"

    def claims_validator(self, _token, _jwks, config, nonce, now):
        self.assertEqual(config.client_id, TEST_CLIENT_ID)
        self.assertEqual(now, 2_000_000_000)
        return {
            "iss": AUTHORITY,
            "aud": TEST_CLIENT_ID,
            "azp": TEST_CLIENT_ID,
            "sub": "subject-1",
            "nonce": nonce,
            "exp": 2_000_000_600,
        }

    def access_claims_validator(self, _token, _jwks, config, now):
        self.assertEqual(config.client_id, TEST_CLIENT_ID)
        return {
            "iss": AUTHORITY,
            "aud": "miniforum",
            "client_id": TEST_CLIENT_ID,
            "scope": ["openid", "miniforum"],
            "sub": "subject-1",
            "exp": now + 600,
        }

    def import_synthetic(self, payload, *, replace_existing=False, at=2_000_000_000):
        if not consent_status(self.root)["current"]:
            record_consent(self.root, at=at)
        return import_session(
            self.root,
            payload,
            transport=FakeTransport(),
            access_token_validator=self.access_claims_validator,
            at=at,
            replace_existing=replace_existing,
        )
