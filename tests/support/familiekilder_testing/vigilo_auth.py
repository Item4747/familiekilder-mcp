"""Fakes for Vigilo's login: a client configuration, token claims and an auth transport."""

from __future__ import annotations
import json
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from familiekilder.vigilo_auth import (
    begin_login,
    configure_client,
    login_with_callback,
    record_consent,
    API_ROOT,
    ISSUER,
    JWKS_URI,
    REDIRECT_URI,
    REVOCATION_ENDPOINT,
    USERINFO_ENDPOINT,
)


SECRET = "test-only-client-secret"


def _config() -> dict[str, Any]:
    return {
        "authority": ISSUER,
        "client_id": "fixture-client.ANDROID",
        "client_secret": SECRET,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid vigiloprofile offline_access",
        "api_root": API_ROOT,
        "app_version": "Android 9.9.9-test",
        "attachment_hosts": ["files.example.test"],
    }


def _claims(*, exp: float = 2_000_000_000, tenant: str = "tenant-1") -> dict[str, Any]:
    return {
        "iss": ISSUER,
        "aud": "fixture-provider-resource",
        "client_id": "fixture-client.ANDROID",
        "scope": "openid vigiloprofile offline_access",
        "sub": "subject-1",
        "personId": "person-1",
        "tenantId": tenant,
        "exp": exp,
    }


def _validator(
    _token: str,
    _jwks: Mapping[str, Any],
    _config_value: Any,
    now: float,
) -> Mapping[str, Any]:
    return _claims(exp=now + 3600)


class FakeAuthTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.posts: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.gets: list[str] = []

    def post_form(
        self,
        url: str,
        fields: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.posts.append((url, dict(fields), dict(headers or {})))
        if url == REVOCATION_ENDPOINT:
            return {}
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def get_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.gets.append(url)
        if url == JWKS_URI:
            return {"keys": []}
        if url == USERINFO_ENDPOINT:
            return {"sub": "subject-1"}
        if url.startswith(API_ROOT + "api/tenants/"):
            return {"id": "tenant-1", "name": "Test skole"}
        raise AssertionError(url)


class VigiloAuthFixture:
    """A temporary project folder with helpers to configure Vigilo and log in."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp_path = Path(self.temporary.name)
        self.root = self.tmp_path / "project"
        self.root.mkdir()

    def configure(self) -> None:
        private = self.root / ".private"
        private.mkdir(mode=0o700, exist_ok=True)
        private.chmod(0o700)
        bootstrap = private / "bootstrap"
        bootstrap.mkdir(mode=0o700, parents=True, exist_ok=True)
        bootstrap.chmod(0o700)
        source = bootstrap / "vigilo-client.json"
        source.write_text(json.dumps(_config()), encoding="utf-8")
        source.chmod(0o600)
        result = configure_client(self.root, source)
        self.assertTrue(result["configured"])
        self.assertNotIn(SECRET, repr(result))
        record_consent(self.root, at=1_700_000_000)

    def login(
        self,
        transport: FakeAuthTransport,
        *,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        url, transaction = begin_login(
            self.root,
            now=10,
            random_bytes=lambda size: b"x" * size,
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["scope"], ["openid vigiloprofile offline_access"])
        self.assertNotIn("code_challenge", query)
        self.assertNotIn("code_challenge_method", query)
        self.assertNotIn("nonce", query)
        callback = (
            REDIRECT_URI
            + "?code=fixture-code&state="
            + urllib.parse.quote(transaction.state)
            + "&iss="
            + urllib.parse.quote(ISSUER, safe="")
        )
        return login_with_callback(
            self.root,
            transaction,
            callback,
            transport=transport,
            access_token_validator=_validator,
            now=11,
            wall_time=1_800_000_000,
            replace_existing=replace_existing,
        )
