"""IST OAuth wire protocol, callback binding and strict token validation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import importlib
import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, TypeVar

from .api_transport import ApiRequestNotSent, definitely_not_sent
from .auth_common import (
    base64url,
    has_invalid_percent_encoding,
    is_plain_text,
    random_urlsafe,
    safe_call,
)
from .auth_lifecycle import NOT_BEFORE_LEEWAY_SECONDS
from .core import FamiliekilderError
from .live_traffic import paced_request
from .network_usage import http_attempt

ISSUER = "https://skolid.se"
AUTHORITY = ISSUER
# Norway's discovery publishes this authorization endpoint with the same
# Swedish issuer, token endpoints and signing keys. Keep the OAuth transaction
# intact while opening the Norwegian country flow directly.
AUTHORIZATION_ENDPOINT = "https://skoleid.no/connect/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/connect/token"
USERINFO_ENDPOINT = f"{ISSUER}/connect/userinfo"
REVOCATION_ENDPOINT = f"{ISSUER}/connect/revocation"
JWKS_URI = f"{ISSUER}/.well-known/openid-configuration/jwks"
IST_GRAPHQL_ENDPOINT = "https://miniforum-api.home.ist.com/v2/school?env=prod-home&version=2.14.0&locale=nb"
REDIRECT_URI = "com.ist.home.miniforum-school://authcode"
ACCESS_TOKEN_AUDIENCE = "miniforum"
EXPECTED_SCOPES = frozenset(
    {
        "openid",
        "offline_access",
        "notification",
        "miniforum",
        "profile",
        "skolid.full-access",
        "consent",
        "live-translate-service",
    }
)
CALLBACK_LIFETIME_SECONDS = 10 * 60
# The IST Home readers already allow 45 seconds for the same GraphQL endpoint
# (ist_home._graphql). The login check uses the same bound instead of SkolID's 30.
CONTEXT_TIMEOUT_SECONDS = 45.0
MAX_JSON_BYTES = 1024 * 1024
MINIMAL_CONTEXT_QUERY = "query VerifyContext { getContext { serverTime } }"
# SkolID rejected the provisioned app registration itself; another attempt
# with it cannot succeed.
REGISTRATION_REJECTED = (
    "SkolID avviste IST-appregistreringen; ikke prøv igjen før registreringen er rettet"
)
_PINNED_NETWORK_URLS = frozenset(
    {
        TOKEN_ENDPOINT,
        USERINFO_ENDPOINT,
        REVOCATION_ENDPOINT,
        JWKS_URI,
        IST_GRAPHQL_ENDPOINT,
    }
)
_T = TypeVar("_T")


class OAuthError(FamiliekilderError):
    """A deliberately sanitized OAuth error suitable for terminal output."""


class OAuthFatalConfigurationError(OAuthError):
    """SkolID rejected immutable app registration details; stop using them."""


class OAuthReauthenticationRequired(OAuthError):
    """The refresh grant was rejected and the user must log in again."""


class OAuthNoAnswer(OAuthError):
    """The request may have been sent, but no complete HTTP answer arrived.

    A timeout or a broken connection. It is never a rejection: the provider
    did not say anything about the request.
    """


class _OAuthHTTPError(OAuthError):
    """Sanitized HTTP failure retaining only status and whitelisted OAuth code."""

    def __init__(
        self, status_code: int, oauth_error: str | None = None,
        *, retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__("Det fastlåste OAuth-endepunktet avviste forespørselen")
        self.status_code = status_code
        self.oauth_error = oauth_error
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class ClientConfig:
    """Validated IST client metadata; ``client_secret`` is redacted from repr."""

    authority: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    scope: str

    def as_private_json(self) -> dict[str, str]:
        """Return the complete value for private on-disk storage only."""

        return {
            "authority": self.authority,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "scope": self.scope,
        }


@dataclass(repr=False)
class LoginTransaction:
    """Single-use, in-memory login state.

    The object must not be serialized or persisted.  ``begin_login`` returns it
    separately from the printable authorization URL.
    """

    config: ClientConfig = field(repr=False)
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)
    created_at: float
    _consumed: bool = field(default=False, init=False, repr=False)

    @property
    def consumed(self) -> bool:
        return self._consumed

    def __repr__(self) -> str:
        return f"LoginTransaction(created_at={self.created_at!r}, consumed={self._consumed!r})"


class OAuthTransport(Protocol):
    """Injectable transport used by the high-level OAuth operations."""

    def post_form(
        self, url: str, fields: Mapping[str, str], *, expect_json: bool = True
    ) -> Mapping[str, Any] | None: ...

    def get_json(self, url: str, *, bearer_token: str | None = None) -> Mapping[str, Any]: ...

    def post_json(
        self, url: str, payload: Mapping[str, Any], *, bearer_token: str | None = None
    ) -> Mapping[str, Any]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class UrllibOAuthTransport:
    """Minimal HTTP transport with redirects disabled and response-size limits."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_bytes: int = MAX_JSON_BYTES,
        context_timeout: float = CONTEXT_TIMEOUT_SECONDS,
    ) -> None:
        self.timeout = timeout
        self.context_timeout = context_timeout
        self.max_bytes = max_bytes
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )

    @paced_request
    def _request(
        self,
        url: str,
        *,
        data: bytes | None,
        headers: Mapping[str, str],
        method: str,
        expect_json: bool,
    ) -> Mapping[str, Any] | None:
        if url not in _PINNED_NETWORK_URLS:
            raise OAuthError("OAuth-klienten nektet et ukjent endepunkt")
        request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
        timeout = (
            max(self.timeout, self.context_timeout)
            if url == IST_GRAPHQL_ENDPOINT
            else self.timeout
        )
        failed = False
        http_status: int | None = None
        retry_after = None
        oauth_error: str | None = None
        raw = b""
        started = time.monotonic()
        try:
            http_attempt()
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(self.max_bytes + 1)
        except urllib.error.HTTPError as exc:
            from .api_transport import _retry_after_seconds

            http_status = int(exc.code)
            retry_after = _retry_after_seconds(exc.headers.get("Retry-After"))
            try:
                raw = exc.read(self.max_bytes + 1)
            except OSError:
                raw = b""
            finally:
                exc.close()
            if len(raw) <= self.max_bytes:
                try:
                    error_value = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    error_value = None
                if isinstance(error_value, dict) and isinstance(error_value.get("error"), str):
                    candidate = error_value["error"]
                    if candidate in {
                        "invalid_client",
                        "unauthorized_client",
                        "invalid_scope",
                        "invalid_request",
                        "invalid_grant",
                    }:
                        oauth_error = candidate
        except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
            if definitely_not_sent(exc):
                raise ApiRequestNotSent("Forespørselen ble ikke sendt") from None
            # The caller only learns that it failed. The host is pinned and public;
            # the error classes tell a timeout from a refused or broken connection.
            # urllib wraps failures while connecting or sending in URLError
            # (reason=TimeoutError: connecting or sending timed out); a bare
            # TimeoutError means the request was sent and no answer came in time.
            reason = getattr(exc, "reason", None)
            logging.getLogger(__name__).warning(
                "ist_oauth_request_failed host=%s error=%s reason=%s"
                " elapsed_ms=%d timeout_s=%g",
                urllib.parse.urlsplit(url).hostname,
                type(exc).__name__,
                type(reason).__name__ if reason is not None else "-",
                round((time.monotonic() - started) * 1000),
                timeout,
            )
            failed = True
        if http_status is not None:
            raise _OAuthHTTPError(
                http_status, oauth_error, retry_after_seconds=retry_after
            )
        if failed:
            raise OAuthNoAnswer("Det fastlåste OAuth-endepunktet kunne ikke bekreftes")
        if len(raw) > self.max_bytes:
            raise OAuthError("OAuth-endepunktet returnerte for mye data")
        if not expect_json:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict):
            raise OAuthError("OAuth-endepunktet returnerte ugyldige data")
        return value

    def post_form(
        self, url: str, fields: Mapping[str, str], *, expect_json: bool = True
    ) -> Mapping[str, Any] | None:
        return self._request(
            url,
            data=urllib.parse.urlencode(fields).encode("ascii"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "familiekilder-ist-oauth/0.1",
            },
            method="POST",
            expect_json=expect_json,
        )

    def get_json(self, url: str, *, bearer_token: str | None = None) -> Mapping[str, Any]:
        headers = {"Accept": "application/json", "User-Agent": "familiekilder-ist-oauth/0.1"}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        value = self._request(url, data=None, headers=headers, method="GET", expect_json=True)
        assert value is not None
        return value

    def post_json(
        self, url: str, payload: Mapping[str, Any], *, bearer_token: str | None = None
    ) -> Mapping[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "familiekilder-ist-oauth/0.1",
        }
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        value = self._request(url, data=data, headers=headers, method="POST", expect_json=True)
        assert value is not None
        return value


def _required_text(value: Any, label: str, *, max_length: int = 65536) -> str:
    if not is_plain_text(value, maximum=max_length):
        raise OAuthError(f"OAuth-konfigurasjonen har ugyldig {label}")
    return value


def _normalize_scope(value: Any) -> str:
    if isinstance(value, str):
        scopes = value.split()
    elif isinstance(value, (list, tuple)) and all(isinstance(scope, str) for scope in value):
        scopes = list(value)
    else:
        raise OAuthError("OAuth-konfigurasjonen har ugyldig scope")
    if len(scopes) != len(set(scopes)) or set(scopes) != EXPECTED_SCOPES:
        raise OAuthError("OAuth-konfigurasjonen har ikke IST Home sitt fastlåste scope-sett")
    return " ".join(scopes)


def _parse_client_config(value: Mapping[str, Any], *, app_keys: bool = False) -> ClientConfig:
    if app_keys:
        authority = value.get("authority")
        client_id = value.get("clientId")
        client_secret = value.get("clientSecret")
        redirect_uri = value.get("redirectUri")
        scope = value.get("scope")
    else:
        authority = value.get("authority")
        client_id = value.get("client_id")
        client_secret = value.get("client_secret")
        redirect_uri = value.get("redirect_uri")
        scope = value.get("scope")

    normalized = ClientConfig(
        authority=_required_text(authority, "authority", max_length=200),
        client_id=_required_text(client_id, "client-ID", max_length=200),
        client_secret=_required_text(client_secret, "klienthemmelighet", max_length=4096),
        redirect_uri=_required_text(redirect_uri, "redirect-URI", max_length=500),
        scope=_normalize_scope(scope),
    )
    if normalized.authority != AUTHORITY:
        raise OAuthError("OAuth-authority er ikke den fastlåste SkolID-utstederen")
    if normalized.redirect_uri != REDIRECT_URI:
        raise OAuthError("OAuth redirect-URI samsvarer ikke med IST Home")
    expected_client_id = urllib.parse.urlsplit(REDIRECT_URI).scheme
    if normalized.client_id != expected_client_id:
        raise OAuthError("OAuth-klient-ID samsvarer ikke med IST Home-redirecten")
    return normalized


def pkce_challenge(code_verifier: str) -> str:
    """Return the RFC 7636 S256 challenge for a verifier."""

    if not isinstance(code_verifier, str) or not 43 <= len(code_verifier) <= 128:
        raise OAuthError("PKCE-verifier har ugyldig format")
    allowed = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    if any(character not in allowed for character in code_verifier):
        raise OAuthError("PKCE-verifier har ugyldig format")
    return base64url(hashlib.sha256(code_verifier.encode("ascii")).digest())


def _random_urlsafe(random_bytes: Callable[[int], bytes]) -> str:
    return random_urlsafe(random_bytes, OAuthError)


def _is_generated_base64url(value: str) -> bool:
    allowed = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    return len(value) == 43 and all(character in allowed for character in value)


def _consume_callback(transaction: LoginTransaction, callback_url: str, *, now: float | None) -> str:
    if not isinstance(transaction, LoginTransaction):
        raise OAuthError("OAuth-transaksjonen har ugyldig format")
    if transaction._consumed:
        raise OAuthError("OAuth-callbacken er allerede brukt")
    current = time.monotonic() if now is None else now
    if not isinstance(current, (int, float)) or not math.isfinite(current):
        raise OAuthError("Ugyldig tidspunkt for OAuth-callback")
    elapsed = float(current) - transaction.created_at
    if elapsed < 0 or elapsed > CALLBACK_LIFETIME_SECONDS:
        raise OAuthError("OAuth-callbacken er utløpt; start ny innlogging")
    if not isinstance(callback_url, str) or not callback_url or len(callback_url) > 8192:
        raise OAuthError("OAuth-callbacken har ugyldig format")
    if any(ord(character) < 32 or ord(character) == 127 for character in callback_url):
        raise OAuthError("OAuth-callbacken inneholder kontrolltegn")
    if has_invalid_percent_encoding(callback_url):
        raise OAuthError("OAuth-callbacken har ugyldig koding")
    try:
        parsed = urllib.parse.urlsplit(callback_url)
    except ValueError:
        raise OAuthError("OAuth-callbacken har ugyldig format") from None
    expected = urllib.parse.urlsplit(REDIRECT_URI)
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or parsed.path != expected.path
        or parsed.fragment
    ):
        raise OAuthError("OAuth-callbacken bruker feil redirect-URI")
    try:
        pairs = urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=12,
        )
    except (ValueError, UnicodeError):
        raise OAuthError("OAuth-callbacken har ugyldige parametre") from None
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise OAuthError("OAuth-callbacken har duplikate parametre")
    allowed = {"code", "state", "session_state", "iss", "error", "error_description", "error_uri"}
    # RFC 6749 section 4.1.2 requires clients to ignore unrecognized response
    # parameters. Discard provider extensions without storing or forwarding
    # them; all redirect, state, issuer and code checks still apply below.
    parameters = {key: value for key, value in pairs if key in allowed}
    has_code = "code" in parameters
    has_error = "error" in parameters
    if has_code == has_error:
        raise OAuthError("OAuth-callbacken må inneholde nøyaktig én respons")
    if ("error_description" in parameters or "error_uri" in parameters) and not has_error:
        raise OAuthError("OAuth-callbacken har ugyldige feilparametre")
    returned_state = parameters.get("state")
    if (
        not isinstance(returned_state, str)
        or not _is_generated_base64url(returned_state)
        or not hmac.compare_digest(
            returned_state.encode("ascii"), transaction.state.encode("ascii")
        )
    ):
        raise OAuthError("OAuth-callbackens state samsvarer ikke")
    returned_issuer = parameters.get("iss")
    if returned_issuer is not None and returned_issuer != ISSUER:
        raise OAuthError("OAuth-callbackens issuer samsvarer ikke")

    if "error" in parameters:
        # A state-matched provider error is terminal even though no token call
        # follows.  A malformed local callback remains retryable.
        transaction._consumed = True
        if parameters["error"] in {
            "invalid_client",
            "unauthorized_client",
            "invalid_scope",
            "invalid_request",
        }:
            raise OAuthFatalConfigurationError(REGISTRATION_REJECTED)
        raise OAuthError("SkolID fullførte ikke autorisasjonen; start ny innlogging")
    code = parameters.get("code")
    if (
        not isinstance(code, str)
        or not code
        or len(code) > 4096
        or any(not 33 <= ord(character) <= 126 for character in code)
    ):
        raise OAuthError("OAuth-callbacken mangler en gyldig autorisasjonskode")
    # Consume immediately before the sole token request.
    transaction._consumed = True
    return code


def _safe_call(message: str, operation: Callable[[], _T]) -> _T:
    return safe_call(message, operation, OAuthError)


def _safe_token(value: Any, label: str) -> str:
    if not is_plain_text(value, maximum=131072, allow_space=False):
        raise OAuthError(f"SkolID returnerte ugyldig {label}")
    return value


def _numeric(value: Any, label: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise OAuthError(f"OAuth-økten har ugyldig {label}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise OAuthError(f"OAuth-økten har ugyldig {label}") from None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise OAuthError(f"OAuth-økten har ugyldig {label}")
    return number


def _validated_subject(value: Any) -> str:
    if not is_plain_text(value, maximum=4096):
        raise OAuthError("OAuth-tokenet har ugyldig subject")
    return value


def _decode_jwt_payload_unverified(token: str) -> Mapping[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3 or not parts[1]:
            raise ValueError
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict):
        raise OAuthError("IST Home access-tokenet har ugyldig format")
    _numeric(value.get("exp"), "access-token expiry", minimum=1, maximum=10**11)
    return value


def _validate_id_claims(
    claims: Mapping[str, Any], *, client_id: str, nonce: str, now: float
) -> Mapping[str, Any]:
    if claims.get("iss") != ISSUER:
        raise OAuthError("ID-tokenets issuer samsvarer ikke")
    audience = claims.get("aud")
    if isinstance(audience, str):
        audiences = [audience]
    elif isinstance(audience, list) and all(isinstance(item, str) for item in audience):
        audiences = audience
    else:
        raise OAuthError("ID-tokenets audience har ugyldig format")
    if client_id not in audiences:
        raise OAuthError("ID-tokenets audience samsvarer ikke")
    azp = claims.get("azp")
    if (len(audiences) > 1 or azp is not None) and azp != client_id:
        raise OAuthError("ID-tokenets autoriserte part samsvarer ikke")
    returned_nonce = claims.get("nonce")
    if (
        not isinstance(returned_nonce, str)
        or not hmac.compare_digest(
            returned_nonce.encode("utf-8"), nonce.encode("utf-8")
        )
    ):
        raise OAuthError("ID-tokenets nonce samsvarer ikke")
    _validated_subject(claims.get("sub"))
    expires_at = _numeric(claims.get("exp"), "ID-token expiry", minimum=1, maximum=10**11)
    if expires_at <= now:
        raise OAuthError("ID-tokenet er utløpt")
    if "nbf" in claims:
        not_before = _numeric(claims["nbf"], "ID-token not-before", minimum=0, maximum=10**11)
        if not_before > now + NOT_BEFORE_LEEWAY_SECONDS:
            raise OAuthError("ID-tokenet er ikke gyldig ennå")
    return claims


def _pyjwt_token_validator(
    token: str,
    jwks: Mapping[str, Any],
    config: ClientConfig,
    nonce: str,
    now: float,
) -> Mapping[str, Any]:
    try:
        jwt = importlib.import_module("jwt")
        header = jwt.get_unverified_header(token)
        if not isinstance(header, dict) or header.get("alg") != "RS256":
            raise ValueError
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise ValueError
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            raise ValueError
        candidates = [
            key
            for key in keys
            if isinstance(key, dict)
            and key.get("kid") == kid
            and key.get("kty") == "RSA"
            and key.get("use", "sig") == "sig"
            and key.get("alg", "RS256") == "RS256"
        ]
        if len(candidates) != 1:
            raise ValueError
        public_key = jwt.PyJWK.from_dict(candidates[0]).key
        claims = jwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            audience=config.client_id,
            issuer=ISSUER,
            leeway=NOT_BEFORE_LEEWAY_SECONDS,
            options={
                "require": ["exp", "iss", "aud", "sub", "nonce"],
                "verify_exp": False,
                "verify_nbf": False,
            },
        )
    except Exception:
        claims = None
    if not isinstance(claims, dict):
        raise OAuthError("ID-tokenet kunne ikke verifiseres med SkolID JWKS")
    return _validate_id_claims(claims, client_id=config.client_id, nonce=nonce, now=now)


TokenValidator = Callable[
    [str, Mapping[str, Any], ClientConfig, str, float], Mapping[str, Any]
]


def _validate_access_claims(
    claims: Mapping[str, Any], *, config: ClientConfig, now: float
) -> Mapping[str, Any]:
    if claims.get("iss") != ISSUER:
        raise OAuthError("Access-tokenets issuer samsvarer ikke")
    audience = claims.get("aud")
    if isinstance(audience, str):
        audiences = [audience]
    elif isinstance(audience, list) and all(isinstance(item, str) for item in audience):
        audiences = audience
    else:
        raise OAuthError("Access-tokenets audience har ugyldig format")
    if ACCESS_TOKEN_AUDIENCE not in audiences:
        raise OAuthError("Access-tokenets audience samsvarer ikke med IST Home")
    if claims.get("client_id") != config.client_id:
        raise OAuthError("Access-tokenets klient-ID samsvarer ikke med IST Home")
    scopes = claims.get("scope")
    if isinstance(scopes, str):
        scope_set = set(scopes.split())
    elif isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes):
        scope_set = set(scopes)
    else:
        raise OAuthError("Access-tokenets scope har ugyldig format")
    if ACCESS_TOKEN_AUDIENCE not in scope_set:
        raise OAuthError("Access-tokenet mangler IST Home-scope")
    _validated_subject(claims.get("sub"))
    expires_at = _numeric(claims.get("exp"), "access-token expiry", minimum=1, maximum=10**11)
    if expires_at <= now:
        raise OAuthError("Access-tokenet er utløpt")
    if "nbf" in claims:
        not_before = _numeric(
            claims["nbf"], "access-token not-before", minimum=0, maximum=10**11
        )
        if not_before > now + NOT_BEFORE_LEEWAY_SECONDS:
            raise OAuthError("Access-tokenet er ikke gyldig ennå")
    return claims


def _pyjwt_access_token_validator(
    token: str,
    jwks: Mapping[str, Any],
    config: ClientConfig,
    now: float,
) -> Mapping[str, Any]:
    """Verify a legacy imported access token with pinned SkolID JWKS policy."""

    try:
        jwt = importlib.import_module("jwt")
        header = jwt.get_unverified_header(token)
        if not isinstance(header, dict) or header.get("alg") != "RS256":
            raise ValueError
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise ValueError
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            raise ValueError
        candidates = [
            key
            for key in keys
            if isinstance(key, dict)
            and key.get("kid") == kid
            and key.get("kty") == "RSA"
            and key.get("use", "sig") == "sig"
            and key.get("alg", "RS256") == "RS256"
        ]
        if len(candidates) != 1:
            raise ValueError
        public_key = jwt.PyJWK.from_dict(candidates[0]).key
        claims = jwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            issuer=ISSUER,
            leeway=NOT_BEFORE_LEEWAY_SECONDS,
            options={
                "require": ["exp", "iss", "aud", "sub", "client_id", "scope"],
                "verify_aud": False,
                "verify_exp": False,
                "verify_nbf": False,
            },
        )
    except Exception:
        claims = None
    if not isinstance(claims, dict):
        raise OAuthError("Access-tokenet kunne ikke verifiseres med SkolID JWKS")
    return _validate_access_claims(claims, config=config, now=now)


AccessTokenValidator = Callable[
    [str, Mapping[str, Any], ClientConfig, float], Mapping[str, Any]
]


def _validated_token_response(value: Mapping[str, Any], config: ClientConfig) -> dict[str, Any]:
    access_token = _safe_token(value.get("access_token"), "access-token")
    refresh_token = _safe_token(value.get("refresh_token"), "refresh-token")
    id_token = _safe_token(value.get("id_token"), "ID-token")
    token_type = value.get("token_type")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise OAuthError("SkolID returnerte ugyldig token-type")
    expires_in = _numeric(value.get("expires_in"), "expires_in", minimum=1, maximum=31_536_000)
    returned_scope = value.get("scope")
    if returned_scope is not None and set(_normalize_scope(returned_scope).split()) != EXPECTED_SCOPES:
        raise OAuthError("SkolID returnerte et uventet scope")
    _decode_jwt_payload_unverified(access_token)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "expires_in": expires_in,
        "scope": config.scope,
    }


def _validated_refresh_response(
    value: Mapping[str, Any], session: Mapping[str, Any], config: ClientConfig
) -> dict[str, Any]:
    access_token = _safe_token(value.get("access_token"), "access-token")
    returned_refresh_token = value.get("refresh_token")
    if returned_refresh_token is None:
        refresh_token = str(session["refresh_token"])
    else:
        refresh_token = _safe_token(returned_refresh_token, "refresh-token")
    token_type = value.get("token_type")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise OAuthError("SkolID returnerte ugyldig token-type")
    expires_in = _numeric(value.get("expires_in"), "expires_in", minimum=1, maximum=31_536_000)
    returned_scope = value.get("scope")
    if returned_scope is not None:
        _normalize_scope(returned_scope)
    _decode_jwt_payload_unverified(access_token)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "scope": config.scope,
    }
