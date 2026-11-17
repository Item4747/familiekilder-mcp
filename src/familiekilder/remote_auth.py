"""Built-in OAuth 2.1 authorization server for one owner, in front of HTTP mode.

The MCP SDK provides the protocol endpoints (metadata, registration, authorize,
token, revoke), PKCE S256 and the bearer check. This module supplies the
storage and the one decision that is ours: an authorization code is issued
only after someone types the owner code on the approval page.

- Clients register themselves (RFC 7591), as Claude and ChatGPT do. A
  registration alone grants nothing.
- Redirect addresses are limited to the hosts of known assistants and
  loopback, and must match exactly what the client registered.
- Owner-code attempts are limited across restarts, and only one slow check
  runs at a time.
- Access and refresh tokens are stored as SHA-256 hashes only. Refresh tokens
  rotate; reuse of a rotated token revokes the whole chain.
- Nothing here logs codes, tokens or request parameters.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .core import FamiliekilderError, project_paths
from .hosting import HostSettings
from .private_lock import private_named_lock, register_lock_names
from .private_state import atomic_write_private_json, read_private_json

SCOPE = "familiekilder"
OFFLINE_SCOPE = "offline_access"
VALID_SCOPES = [SCOPE, OFFLINE_SCOPE]
APPROVAL_PATH = "/godkjenn"

ACCESS_TOKEN_SECONDS = 60 * 60
REFRESH_TOKEN_SECONDS = 90 * 24 * 60 * 60
AUTHORIZATION_CODE_SECONDS = 5 * 60
PENDING_REQUEST_SECONDS = 10 * 60
MAX_CLIENTS = 50
MAX_PENDING_REQUESTS = 100
MAX_CLIENT_METADATA_BYTES = 16 * 1024
MAX_OAUTH_STORE_BYTES = 8 * 1024 * 1024

# Failed owner codes: at most 5 per 15 minutes and 20 per day, for the whole
# host. Kept on disk so a restart does not reset the count.
ATTEMPT_LIMITS = ((15 * 60, 5), (24 * 60 * 60, 20))

# Redirect hosts of the assistants this is made for, from their official
# documentation (Claude: claude.ai; ChatGPT: chatgpt.com). Loopback covers
# Claude Code and local test clients. Others can be added with
# FAMILIEKILDER_REDIRECT_HOSTS.
DEFAULT_REDIRECT_HOSTS = ("claude.ai", "claude.com", "chatgpt.com")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

register_lock_names("remote-oauth", "remote-attempts")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def remote_directory(root: Path) -> Path:
    return project_paths(root).private / "remote"


class OwnerCodeLimited(FamiliekilderError):
    """Too many wrong owner codes; the gate is closed for a while."""

    def __init__(self, retry_after: int):
        super().__init__("For mange feil forsøk. Vent litt og prøv igjen.")
        self.retry_after = retry_after


class OwnerCodeGate:
    """Checks the owner code with a persistent limit on failed attempts."""

    def __init__(self, root: Path, settings: HostSettings, *, clock: Callable[[], float] = time.time):
        self.root = root
        self.settings = settings
        self.clock = clock
        self._path = remote_directory(root) / "owner-attempts.json"
        # One slow scrypt check at a time keeps memory bounded on a small host.
        self._check_lock = threading.Lock()

    def _failures(self) -> list[float]:
        if not self._path.exists():
            return []
        try:
            value = read_private_json(self._path)
            failures = [float(item) for item in value.get("failures", [])]
        except (FamiliekilderError, TypeError, ValueError):
            # An unreadable counter must not open the gate: count it as full.
            return [self.clock()] * ATTEMPT_LIMITS[-1][1]
        return failures

    def _retry_after(self, failures: list[float], now: float) -> int:
        wait = 0.0
        for window, limit in ATTEMPT_LIMITS:
            recent = sorted(at for at in failures if at > now - window)
            if len(recent) >= limit:
                wait = max(wait, recent[-limit] + window - now)
        return int(wait) + 1 if wait > 0 else 0

    def check(self, code: str) -> bool:
        """True for the right code. Raises OwnerCodeLimited when closed."""

        with self._check_lock, private_named_lock(self.root, "remote-attempts", wait=True, timeout=30):
            now = self.clock()
            failures = [at for at in self._failures() if at > now - ATTEMPT_LIMITS[-1][0]]
            retry_after = self._retry_after(failures, now)
            if retry_after:
                raise OwnerCodeLimited(retry_after)
            if self.settings.owner_code.matches(code):
                if failures:
                    atomic_write_private_json(self._path, {"version": 1, "failures": []})
                return True
            failures.append(now)
            atomic_write_private_json(self._path, {"version": 1, "failures": failures})
            return False

    async def check_async(self, code: str) -> bool:
        return await anyio.to_thread.run_sync(self.check, code)


def redirect_hosts(extra: str | None) -> tuple[str, ...]:
    hosts = list(DEFAULT_REDIRECT_HOSTS)
    for item in (extra or "").split(","):
        item = item.strip().lower()
        if item:
            hosts.append(item)
    return tuple(hosts)


def allowed_redirect(uri: str, hosts: tuple[str, ...]) -> bool:
    parts = urlsplit(uri)
    host = (parts.hostname or "").lower()
    if parts.fragment or parts.username or parts.password:
        return False
    if parts.scheme == "http":
        return host in LOOPBACK_HOSTS
    return parts.scheme == "https" and host in hosts


class OAuthStore:
    """Clients and hashed tokens in ``.private/remote/oauth.json``.

    Read and written under a file lock on every call, so ``remote revoke`` in
    another process takes effect at the next request.
    """

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time):
        self.root = root
        self.clock = clock
        self.path = remote_directory(root) / "oauth.json"

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "clients": {}, "access": {}, "refresh": {}}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        value = dict(read_private_json(self.path, max_bytes=MAX_OAUTH_STORE_BYTES))
        if value.get("version") != 1:
            raise FamiliekilderError("Ukjent format på OAuth-lageret")
        for key in ("clients", "access", "refresh"):
            if not isinstance(value.get(key), dict):
                raise FamiliekilderError("Ugyldig OAuth-lager")
        return value

    def _save(self, value: dict[str, Any]) -> None:
        now = self.clock()
        for kind in ("access", "refresh"):
            value[kind] = {
                key: entry
                for key, entry in value[kind].items()
                if entry.get("expires_at", 0) > now
            }
        # Match atomic_write_private_json's exact encoding, including newline.
        # Refuse before replacing the last readable store, for every writer.
        serialized = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(serialized) > MAX_OAUTH_STORE_BYTES:
            raise FamiliekilderError("OAuth-lageret er fullt; ingen endring er lagret")
        atomic_write_private_json(self.path, value)

    def transaction(self, change: Callable[[dict[str, Any]], Any]) -> Any:
        with private_named_lock(self.root, "remote-oauth", wait=True, timeout=30):
            value = self._load()
            result = change(value)
            self._save(value)
            return result

    def read(self) -> dict[str, Any]:
        with private_named_lock(self.root, "remote-oauth", wait=True, timeout=30):
            return self._load()

    # Clients

    def add_client(self, client: OAuthClientInformationFull) -> None:
        metadata = client.model_dump(mode="json", exclude_none=True)
        if (
            len(json.dumps(metadata, ensure_ascii=False).encode()) > MAX_CLIENT_METADATA_BYTES
            or len(client.client_name or "") > 256
            or len(client.redirect_uris or []) > 10
        ):
            raise RegistrationError("invalid_client_metadata", "Client metadata is too large")

        def change(value):
            clients = value["clients"]
            if len(clients) >= MAX_CLIENTS:
                in_use = {entry["client_id"] for entry in value["refresh"].values()}
                unused = sorted(
                    (entry["registered_at"], client_id)
                    for client_id, entry in clients.items()
                    if client_id not in in_use
                )
                if not unused:
                    raise RegistrationError(
                        "invalid_client_metadata", "Too many registered clients"
                    )
                clients.pop(unused[0][1])
            clients[client.client_id] = {
                "registered_at": self.clock(),
                "info": metadata,
            }

        self.transaction(change)

    def client(self, client_id: str) -> OAuthClientInformationFull | None:
        entry = self.read()["clients"].get(client_id)
        if not entry:
            return None
        return OAuthClientInformationFull.model_validate(entry["info"])

    # Tokens

    def issue(
        self,
        client_id: str,
        scopes: list[str],
        resource: str,
        family: str | None = None,
        *,
        replaces: str | None = None,
    ) -> OAuthToken:
        access = "fka_" + secrets.token_urlsafe(32)
        refresh = "fkr_" + secrets.token_urlsafe(32)
        now = self.clock()
        family = family or secrets.token_hex(16)

        def change(value):
            if replaces is not None:
                old = value["refresh"].get(replaces)
                if old is None or old.get("rotated_at") is not None:
                    raise TokenError("invalid_grant", "refresh token is no longer valid")
                old["rotated_at"] = now
            common = {"client_id": client_id, "scopes": scopes, "resource": resource, "family": family}
            value["access"][_hash(access)] = {**common, "expires_at": now + ACCESS_TOKEN_SECONDS}
            value["refresh"][_hash(refresh)] = {**common, "expires_at": now + REFRESH_TOKEN_SECONDS}

        self.transaction(change)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    def access(self, token: str) -> dict[str, Any] | None:
        entry = self.read()["access"].get(_hash(token))
        if not entry or entry["expires_at"] <= self.clock():
            return None
        return entry

    def refresh(self, token: str) -> dict[str, Any] | None:
        key = _hash(token)

        def change(value):
            entry = value["refresh"].get(key)
            if not entry or entry["expires_at"] <= self.clock():
                return None
            if entry.get("rotated_at") is not None:
                # A rotated token came back: someone else may hold the chain.
                self._drop_family(value, entry["family"])
                return None
            return {**entry, "hash": key}

        return self.transaction(change)

    @staticmethod
    def _drop_family(value: dict[str, Any], family: str) -> None:
        for kind in ("access", "refresh"):
            value[kind] = {k: v for k, v in value[kind].items() if v.get("family") != family}

    def revoke_family(self, family: str) -> None:
        self.transaction(lambda value: self._drop_family(value, family))

    def revoke(self, *, client_id: str | None = None) -> int:
        """Revoke every token, or those of one client (and forget that client)."""

        def change(value):
            count = 0
            for kind in ("access", "refresh"):
                kept = {
                    key: entry
                    for key, entry in value[kind].items()
                    if client_id is not None and entry["client_id"] != client_id
                }
                count += len(value[kind]) - len(kept)
                value[kind] = kept
            if client_id is not None:
                value["clients"].pop(client_id, None)
            return count

        return self.transaction(change)

    def summary(self) -> list[dict[str, Any]]:
        """Clients and their active chains, without any secret or hash."""

        value = self.read()
        now = self.clock()
        result = []
        for client_id, entry in value["clients"].items():
            chains = {
                token["family"]
                for token in value["refresh"].values()
                if token["client_id"] == client_id
                and token.get("rotated_at") is None
                and token["expires_at"] > now
            }
            info = entry.get("info", {})
            result.append(
                {
                    "client_id": client_id,
                    "name": info.get("client_name"),
                    "redirect_uris": info.get("redirect_uris", []),
                    "registered_at": int(entry["registered_at"]),
                    "active_connections": len(chains),
                }
            )
        return result


class PendingRequest:
    def __init__(self, client: OAuthClientInformationFull, params: AuthorizationParams, expires_at: float):
        self.client = client
        self.params = params
        self.expires_at = expires_at


class OwnerAuthorizationProvider:
    """`OAuthAuthorizationServerProvider` for one owner, with an approval page."""

    def __init__(
        self,
        root: Path,
        settings: HostSettings,
        *,
        extra_redirect_hosts: str | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.root = root
        self.settings = settings
        self.clock = clock
        self.store = OAuthStore(root, clock=clock)
        self.gate = OwnerCodeGate(root, settings, clock=clock)
        self.redirect_hosts = redirect_hosts(extra_redirect_hosts)
        # Short-lived and in memory only: a restart just means trying again.
        self._pending: dict[str, PendingRequest] = {}
        # Request IDs already approved or refused, until they would have
        # expired, so a second submit can say so instead of "expired".
        self._answered: dict[str, float] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._lock = threading.Lock()

    # Registration

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(uri) for uri in client_info.redirect_uris or []]
        if not uris or not all(allowed_redirect(uri, self.redirect_hosts) for uri in uris):
            raise RegistrationError(
                "invalid_redirect_uri",
                "Redirect URIs must be loopback or belong to a supported assistant",
            )
        # One permission exists; give every client both scopes so a later
        # request for offline_access is not refused.
        client_info.scope = " ".join(VALID_SCOPES)
        self.store.add_client(client_info)

    # Authorization

    def _prune(self) -> None:
        now = self.clock()
        self._pending = {k: v for k, v in self._pending.items() if v.expires_at > now}
        self._answered = {k: v for k, v in self._answered.items() if v > now}
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > now}

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        # The SDK compares redirect_uri with the registered list; require the
        # exact string as well, and the allowed hosts in case they changed.
        registered = {str(uri) for uri in client.redirect_uris or []}
        if str(params.redirect_uri) not in registered or not allowed_redirect(
            str(params.redirect_uri), self.redirect_hosts
        ):
            raise AuthorizeError("invalid_request", "redirect_uri is not registered")
        if params.resource is not None and params.resource.rstrip("/") != self.settings.resource_url:
            raise AuthorizeError("invalid_target", "Unknown resource")
        with self._lock:
            self._prune()
            while len(self._pending) >= MAX_PENDING_REQUESTS:
                oldest = min(self._pending, key=lambda key: self._pending[key].expires_at)
                self._pending.pop(oldest)
            request_id = secrets.token_urlsafe(32)
            self._pending[request_id] = PendingRequest(
                client, params, self.clock() + PENDING_REQUEST_SECONDS
            )
        return f"{self.settings.issuer_url}{APPROVAL_PATH}?foresporsel={request_id}"

    def pending(self, request_id: str) -> PendingRequest | None:
        with self._lock:
            self._prune()
            return self._pending.get(request_id)

    def answered(self, request_id: str) -> bool:
        with self._lock:
            self._prune()
            return request_id in self._answered

    def _finish(self, request_id: str, *, approved: bool) -> str | None:
        with self._lock:
            pending = self._pending.pop(request_id, None)
            if pending is None or pending.expires_at <= self.clock():
                return None
            while len(self._answered) >= MAX_PENDING_REQUESTS:
                oldest = min(self._answered, key=self._answered.__getitem__)
                self._answered.pop(oldest)
            self._answered[request_id] = pending.expires_at
            params = pending.params
            if not approved:
                return construct_redirect_uri(
                    str(params.redirect_uri),
                    error="access_denied",
                    state=params.state,
                    iss=self.settings.issuer_url,
                )
            scopes = [SCOPE]
            if params.scopes and OFFLINE_SCOPE in params.scopes:
                scopes.append(OFFLINE_SCOPE)
            code = secrets.token_urlsafe(32)
            self._codes[code] = AuthorizationCode(
                code=code,
                scopes=scopes,
                expires_at=self.clock() + AUTHORIZATION_CODE_SECONDS,
                client_id=pending.client.client_id,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=self.settings.resource_url,
                subject="owner",
            )
            return construct_redirect_uri(
                str(params.redirect_uri), code=code, state=params.state, iss=self.settings.issuer_url
            )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with self._lock:
            self._prune()
            code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with self._lock:
            # One use only, even if two requests race.
            if self._codes.pop(authorization_code.code, None) is None:
                raise TokenError("invalid_grant", "authorization code was already used")
        return self.store.issue(
            client.client_id, authorization_code.scopes, self.settings.resource_url
        )

    # Refresh and access

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        entry = self.store.refresh(refresh_token)
        if entry is None or entry["client_id"] != client.client_id:
            return None
        return _StoredRefresh(
            token=refresh_token,
            client_id=entry["client_id"],
            scopes=entry["scopes"],
            expires_at=int(entry["expires_at"]),
            resource=entry["resource"],
            subject="owner",
            family=entry["family"],
            hash=entry["hash"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        stored = refresh_token if isinstance(refresh_token, _StoredRefresh) else None
        if stored is None:
            raise TokenError("invalid_grant", "unknown refresh token")
        granted = [scope for scope in stored.scopes if scope in scopes] or stored.scopes
        if SCOPE not in granted:
            granted = [SCOPE, *granted]
        return self.store.issue(
            client.client_id, granted, stored.resource or self.settings.resource_url,
            stored.family, replaces=stored.hash,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        entry = self.store.access(token)
        if entry is None:
            return None
        return _StoredAccess(
            token=token,
            client_id=entry["client_id"],
            scopes=entry["scopes"],
            expires_at=int(entry["expires_at"]),
            resource=entry["resource"],
            subject="owner",
            family=entry["family"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        family = getattr(token, "family", None)
        if family:
            self.store.revoke_family(family)

    async def exchange_identity_assertion(self, client, params):  # pragma: no cover - unused grant
        raise TokenError("unsupported_grant_type", "Not supported")

    # Approval page

    def _gone(self, request_id: str) -> Response:
        """The page for a request that is no longer pending."""

        if self.answered(request_id):
            return _page(_ANSWERED, status_code=409)
        return _page(_EXPIRED, status_code=400)

    async def approval_page(self, request: Request) -> Response:
        request_id = request.query_params.get("foresporsel", "")
        pending = self.pending(request_id)
        if pending is None:
            return self._gone(request_id)
        return _form_page(request_id, pending, error=None)

    async def approval_submit(self, request: Request) -> Response:
        form = await request.form(max_files=0, max_fields=10)
        request_id = str(form.get("foresporsel", ""))
        pending = self.pending(request_id)
        if pending is None:
            return self._gone(request_id)
        if form.get("valg") == "avvis":
            target = self._finish(request_id, approved=False)
            return _redirect(target) if target else self._gone(request_id)
        code = str(form.get("eierkode", ""))
        try:
            correct = await self.gate.check_async(code)
        except OwnerCodeLimited as exc:
            return _form_page(
                request_id,
                pending,
                error=str(exc),
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )
        if not correct:
            return _form_page(
                request_id, pending, error="Feil eierkode. Prøv igjen.", status_code=401
            )
        target = self._finish(request_id, approved=True)
        return _redirect(target) if target else self._gone(request_id)


class _StoredRefresh(RefreshToken):
    family: str
    hash: str


class _StoredAccess(AccessToken):
    family: str


_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

# One DNS label (or one part of a dotted IPv4 address), as CSP's host-source
# grammar allows it. Hostnames from urlsplit are already lower case.
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def redirect_origin(uri: str) -> str | None:
    """The origin of a redirect URI as a CSP source, or None.

    Built from the parsed scheme, host and port only, so no path, query or
    other text from the URI can reach the header. Anything that is not a plain
    http or https origin with a DNS or IPv4 host gives None. That includes
    IPv6 literals such as [::1]: CSP source expressions cannot name them, and
    Chromium ignores one if it is sent, so such a redirect stays blocked in
    the browser. Loopback clients use 127.0.0.1 or localhost instead.
    """

    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        return None
    if not host or len(host) > 253:
        return None
    if not all(_HOST_LABEL.match(label) for label in host.split(".")):
        return None
    if port is None:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def _content_security_policy(form_origin: str | None) -> str:
    """Strict policy; a form page may also submit to the client's origin.

    Chromium checks form-action on every redirect that follows a form
    submission. The approval form answers with a 303 to the client's
    redirect URI, so that origin must be allowed on the page with the form,
    or the browser stops on the form and the assistant never gets the code.
    """

    form_action = "'self'" if form_origin is None else f"'self' {form_origin}"
    return (
        "default-src 'none'; style-src 'unsafe-inline'; "
        f"form-action {form_action}; frame-ancestors 'none'"
    )


def _page(
    body: str,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    form_origin: str | None = None,
) -> HTMLResponse:
    document = f"""<!doctype html>
<html lang="nb">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Familiekilder – godkjenning</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 32rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
label {{ display: block; margin-top: 1rem; font-weight: 600; }}
input[type=password] {{ width: 100%; font-size: 1.1rem; padding: .5rem; box-sizing: border-box; }}
button {{ margin-top: 1rem; margin-right: .5rem; font-size: 1rem; padding: .5rem 1rem; }}
.feil {{ color: #a00; font-weight: 600; }}
.adresse {{ font-family: ui-monospace, monospace; word-break: break-all; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""
    security = {
        **_SECURITY_HEADERS,
        "Content-Security-Policy": _content_security_policy(form_origin),
    }
    return HTMLResponse(document, status_code=status_code, headers={**security, **(headers or {})})


def _form_page(
    request_id: str,
    pending: PendingRequest,
    *,
    error: str | None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    return _page(
        _approval_form(request_id, pending, error=error),
        status_code=status_code,
        headers=headers,
        form_origin=redirect_origin(str(pending.params.redirect_uri)),
    )


def _redirect(target: str) -> Response:
    return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})


_EXPIRED = """<h1>Forespørselen er utløpt</h1>
<p>Start tilkoblingen på nytt fra assistenten.</p>"""

_ANSWERED = """<h1>Forespørselen er allerede besvart</h1>
<p>Denne godkjenningssiden er allerede brukt. Gå tilbake til assistenten. Hvis den ikke
ble koblet til, starter du tilkoblingen på nytt derfra.</p>"""


def _approval_form(request_id: str, pending: PendingRequest, *, error: str | None) -> str:
    name = pending.client.client_name or "Ukjent app"
    target = urlsplit(str(pending.params.redirect_uri)).netloc
    message = f'<p class="feil" role="alert">{html.escape(error)}</p>' if error else ""
    return f"""<h1>Gi tilgang til familiens kilder?</h1>
<p><strong>{html.escape(name)}</strong> ber om å lese beskjeder og aktiviteter fra
kildene du har koblet til. Tilgangen gjelder bare lesing.</p>
<p>Svaret sendes til <span class="adresse">{html.escape(target)}</span>.
Godkjenn bare hvis du nettopp startet tilkoblingen selv.</p>
{message}
<form method="post" action="{APPROVAL_PATH}">
<input type="hidden" name="foresporsel" value="{html.escape(request_id)}">
<label for="eierkode">Eierkode</label>
<input id="eierkode" name="eierkode" type="password" autocomplete="off" autocapitalize="none" spellcheck="false" required>
<button type="submit" name="valg" value="godkjenn">Godkjenn</button>
<button type="submit" name="valg" value="avvis" formnovalidate>Avvis</button>
</form>
<p>Eierkoden fikk du da verten ble satt opp. Ingen andre trenger den.</p>"""


def revoke_command(root: Path, *, client_id: str | None) -> dict[str, Any]:
    count = OAuthStore(root).revoke(client_id=client_id)
    return {"revoked_tokens": count, "client_id": client_id}


def clients_command(root: Path) -> dict[str, Any]:
    return {"clients": OAuthStore(root).summary()}
