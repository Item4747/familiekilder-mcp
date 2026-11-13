"""Native ID-porten bootstrap and rotating portal session for Skolemelding.

The module mirrors the official Android app's two-stage authentication flow:
ID-porten Authorization Code + PKCE is exchanged for a short-lived OIDC access
token, that token is exchanged for a one-time portal auth code, and the auth
code bootstraps the portal access/refresh token pair.  PIN and biometrics are
deliberately not part of this protocol; they are only UI locks in the app.

No function prints or logs.  Printable result mappings never contain tokens,
auth codes, user names, device identifiers, states, nonces, or PKCE verifiers.
``local_session_for_use`` is the sole secret-bearing integration boundary for
the read-only collector.
"""

from __future__ import annotations

from . import auth_lifecycle as lifecycle
from .api_transport import ApiRequestNotSent

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import platform
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .api_transport import ApiHTTPError, ApiTransport, ApiTransportError
from .auth_common import base64url, is_plain_text, random_urlsafe, utc_iso
from .core import FamiliekilderError
from .private_lock import private_source_lock
from .private_state import (
    PrivateStateError,
    atomic_write_private_json,
    credential_path,
    read_private_bytes,
    read_private_json,
    remove_private_file,
)
from .source_adapter import SourceFatalConfigurationError, SourceLoginRequired, SourceUnavailable


SOURCE = "skolemelding"
ISSUER = "https://idporten.no"
DISCOVERY_ENDPOINT = f"{ISSUER}/.well-known/openid-configuration"
REDIRECT_URI = "com.cgi.oslomelding.foresatte://oauth"
PORTAL_BASE_URL = "https://portal.skoleplattform.no"
APP_TOKEN_ENDPOINT = f"{PORTAL_BASE_URL}/api/exchange-app-token"
PORTAL_TOKEN_ENDPOINT = f"{PORTAL_BASE_URL}/token"
DEVICE_INFO_ENDPOINT = f"{PORTAL_BASE_URL}/api/deviceinfo/save"

CLIENT_FILE = "skolemelding-client.json"
SESSION_FILE = "skolemelding-session.json"
DEVICE_FILE = "skolemelding-device.json"
CONSENT_FILE = "skolemelding-oauth-consent.json"
REFRESH_BLOCK_FILE = "skolemelding-refresh-blocked.json"
CONSENT_VERSION = "unofficial-skolemelding-idporten-v1"
CALLBACK_LIFETIME_SECONDS = 10 * 60
MAX_PRIVATE_JSON_BYTES = 1024 * 1024
EXPECTED_SCOPES = frozenset({"openid", "profile"})

_SAFE_OAUTH_ERRORS = frozenset(
    {
        "invalid_client",
        "unauthorized_client",
        "invalid_scope",
        "invalid_request",
        "invalid_grant",
        "invalid_refresh_token",
    }
)
_FATAL_OAUTH_ERRORS = frozenset(
    {"invalid_client", "unauthorized_client", "invalid_scope", "invalid_request"}
)


class SkolemeldingAuthError(FamiliekilderError):
    """Sanitized local or protocol failure."""


class SkolemeldingReauthenticationRequired(SourceLoginRequired, SkolemeldingAuthError):
    """The portal grant cannot safely be used without a new ID-porten login."""


class SkolemeldingRefreshUncertain(SourceUnavailable, SkolemeldingAuthError):
    """Refresh outcome is unknown, which is not proof of an expired grant."""


class SkolemeldingFatalConfigurationError(
    SourceFatalConfigurationError, SkolemeldingAuthError
):
    """The provider rejected immutable client metadata."""


class AuthTransport(Protocol):
    def get_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any: ...

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
        allow_empty: bool = False,
    ) -> Any: ...

    def post_form(
        self,
        url: str,
        fields: Mapping[str, str],
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class ClientConfig:
    oidc_client_id: str = field(repr=False)
    portal_client_id: str = field(repr=False)
    app_version: str
    issuer: str = ISSUER
    redirect_uri: str = REDIRECT_URI
    scopes: tuple[str, ...] = tuple(sorted(EXPECTED_SCOPES))
    authorization_endpoint: str | None = None
    oidc_token_endpoint: str | None = None
    attachment_hosts: tuple[str, ...] = ()
    attachment_path_prefixes: tuple[str, ...] = ()

    def as_private_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "oidc_client_id": self.oidc_client_id,
            "portal_client_id": self.portal_client_id,
            "app_version": self.app_version,
            "issuer": self.issuer,
            "redirect_uri": self.redirect_uri,
            "scopes": list(self.scopes),
            "attachment_hosts": list(self.attachment_hosts),
            "attachment_path_prefixes": list(self.attachment_path_prefixes),
        }
        if self.authorization_endpoint is not None:
            result["authorization_endpoint"] = self.authorization_endpoint
        if self.oidc_token_endpoint is not None:
            result["oidc_token_endpoint"] = self.oidc_token_endpoint
        return result


@dataclass(repr=False)
class LoginTransaction:
    """Single-use login state; it must remain in memory and out of logs."""

    config: ClientConfig = field(repr=False)
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)
    device_unique_id: str = field(repr=False)
    authorization_endpoint: str = field(repr=False)
    oidc_token_endpoint: str = field(repr=False)
    created_at: float
    _consumed: bool = field(default=False, init=False, repr=False)

    @property
    def consumed(self) -> bool:
        return self._consumed

    def __repr__(self) -> str:
        return (
            "LoginTransaction("
            f"created_at={self.created_at!r}, consumed={self._consumed!r})"
        )


def client_config_path(root: Path | str | None) -> Path:
    return credential_path(root, CLIENT_FILE)


def session_path(root: Path | str | None) -> Path:
    return credential_path(root, SESSION_FILE)


def device_path(root: Path | str | None) -> Path:
    return credential_path(root, DEVICE_FILE)


def consent_path(root: Path | str | None) -> Path:
    return credential_path(root, CONSENT_FILE)


def refresh_block_path(root: Path | str | None) -> Path:
    return credential_path(root, REFRESH_BLOCK_FILE)


def _default_transport(
    *, allowed_idporten_urls: tuple[str, ...] = ()
) -> ApiTransport:
    verified_idporten_urls = {
        _official_idporten_endpoint(url, "ID-porten-endepunkt")
        for url in allowed_idporten_urls
    }
    return ApiTransport(
        allowed_urls={
            DISCOVERY_ENDPOINT,
            APP_TOKEN_ENDPOINT,
            PORTAL_TOKEN_ENDPOINT,
            DEVICE_INFO_ENDPOINT,
            *verified_idporten_urls,
        },
        safe_error_codes=_SAFE_OAUTH_ERRORS,
        user_agent="familiekilder-skolemelding/0.2",
    )


def _nonempty_text(
    value: Any, field_name: str, *, maximum: int = 8192
) -> str:
    if not is_plain_text(value, maximum=maximum):
        raise SkolemeldingAuthError(f"{field_name} har ugyldig format")
    return value


def _token(value: Any, field_name: str) -> str:
    return _nonempty_text(value, field_name, maximum=64 * 1024)


def _official_idporten_endpoint(value: Any, field_name: str) -> str:
    endpoint = _nonempty_text(value, field_name)
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise SkolemeldingAuthError(f"{field_name} har ugyldig format") from None
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not (host == "idporten.no" or host.endswith(".idporten.no"))
    ):
        raise SkolemeldingAuthError(f"{field_name} peker utenfor ID-porten")
    return endpoint


def _attachment_hosts(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 20:
        raise SkolemeldingAuthError("Vedleggsvertlisten har ugyldig format")
    result: list[str] = []
    for raw_host in value:
        host = _nonempty_text(raw_host, "Vedleggsvert", maximum=253).lower().rstrip(".")
        if "/" in host or ":" in host or "*" in host or host.startswith("."):
            raise SkolemeldingAuthError("Vedleggsvertlisten har ugyldig format")
        try:
            host = host.encode("idna").decode("ascii")
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        except UnicodeError:
            raise SkolemeldingAuthError("Vedleggsvertlisten har ugyldig format") from None
        if address is not None and not address.is_global:
            raise SkolemeldingAuthError("Vedleggsvertlisten har ugyldig format")
        if (
            host == "localhost"
            or host.endswith(".localhost")
            or ".." in host
            or not all(
                part
                and len(part) <= 63
                and part[0].isalnum()
                and part[-1].isalnum()
                and all(character.isalnum() or character == "-" for character in part)
                for part in host.split(".")
            )
        ):
            raise SkolemeldingAuthError("Vedleggsvertlisten har ugyldig format")
        result.append(host)
    if len(result) != len(set(result)):
        raise SkolemeldingAuthError("Vedleggsvertlisten har duplikater")
    return tuple(sorted(result))


def _attachment_path_prefixes(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 20:
        raise SkolemeldingAuthError("Vedleggsprefikslisten har ugyldig format")
    result: list[str] = []
    for raw_prefix in value:
        prefix = _nonempty_text(raw_prefix, "Vedleggsprefiks", maximum=512)
        try:
            decoded = urllib.parse.unquote(prefix)
        except ValueError:
            raise SkolemeldingAuthError("Vedleggsprefikslisten har ugyldig format") from None
        if (
            not prefix.startswith("/")
            or not prefix.endswith("/")
            or prefix.startswith("//")
            or "?" in prefix
            or "#" in prefix
            or "%" in prefix
            or "\\" in prefix
            or decoded != prefix
            or any(segment in {".", ".."} for segment in prefix.split("/"))
        ):
            raise SkolemeldingAuthError("Vedleggsprefikslisten har ugyldig format")
        result.append(prefix)
    if len(result) != len(set(result)):
        raise SkolemeldingAuthError("Vedleggsprefikslisten har duplikater")
    return tuple(sorted(result))


def _parse_client_config(value: Any) -> ClientConfig:
    if not isinstance(value, Mapping):
        raise SkolemeldingAuthError("Skolemelding-klientkonfigurasjonen har ugyldig format")
    oidc_client_id = _nonempty_text(
        value.get("oidc_client_id") or value.get("client_id"), "OIDC client-id", maximum=512
    )
    portal_client_id = _nonempty_text(
        value.get("portal_client_id"), "Portal client-id", maximum=512
    )
    app_version = _nonempty_text(value.get("app_version"), "Appversjon", maximum=64)
    issuer = value.get("issuer", ISSUER)
    redirect_uri = value.get("redirect_uri", REDIRECT_URI)
    if issuer != ISSUER or redirect_uri != REDIRECT_URI:
        raise SkolemeldingAuthError(
            "Skolemelding-klientkonfigurasjonen bruker feil issuer eller redirect-URI"
        )
    raw_scopes = value.get("scopes", sorted(EXPECTED_SCOPES))
    if isinstance(raw_scopes, str):
        scopes = tuple(raw_scopes.split())
    elif isinstance(raw_scopes, list) and all(isinstance(item, str) for item in raw_scopes):
        scopes = tuple(raw_scopes)
    else:
        raise SkolemeldingAuthError("ID-porten-scopene har ugyldig format")
    if set(scopes) != EXPECTED_SCOPES or len(scopes) != len(EXPECTED_SCOPES):
        raise SkolemeldingAuthError("ID-porten-scopene avviker fra appflyten")
    authorization_endpoint = value.get("authorization_endpoint")
    token_endpoint = value.get("oidc_token_endpoint") or value.get("token_endpoint")
    if (authorization_endpoint is None) != (token_endpoint is None):
        raise SkolemeldingAuthError(
            "Begge oppdagede ID-porten-endepunktene må oppgis sammen"
        )
    if authorization_endpoint is not None:
        authorization_endpoint = _official_idporten_endpoint(
            authorization_endpoint, "Authorization-endepunktet"
        )
        token_endpoint = _official_idporten_endpoint(
            token_endpoint, "OIDC token-endepunktet"
        )
    attachment_hosts = _attachment_hosts(value.get("attachment_hosts"))
    attachment_path_prefixes = _attachment_path_prefixes(
        value.get("attachment_path_prefixes")
    )
    if bool(attachment_hosts) != bool(attachment_path_prefixes):
        raise SkolemeldingAuthError(
            "Vedleggsverter og path-prefikser må provisioneres sammen"
        )
    return ClientConfig(
        oidc_client_id=oidc_client_id,
        portal_client_id=portal_client_id,
        app_version=app_version,
        scopes=tuple(sorted(scopes)),
        authorization_endpoint=authorization_endpoint,
        oidc_token_endpoint=token_endpoint,
        attachment_hosts=attachment_hosts,
        attachment_path_prefixes=attachment_path_prefixes,
    )


def _read_import_file(path: Path | str) -> Mapping[str, Any]:
    source = Path(path).expanduser()
    try:
        # Client metadata is credential-like even though both OAuth clients are
        # public clients.  Reuse the same owner/regular-file/0600 boundary as
        # token state and do not follow the final symlink.
        raw = read_private_bytes(
            source,
            max_bytes=MAX_PRIVATE_JSON_BYTES,
            missing_message="Fant ikke klientkonfigurasjonsfilen",
        )
    except PrivateStateError:
        raise SkolemeldingAuthError(
            "Klientkonfigurasjonsfilen mangler eller kan ikke leses sikkert"
        ) from None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SkolemeldingAuthError("Klientkonfigurasjonsfilen har ugyldig format") from None
    if not isinstance(value, Mapping):
        raise SkolemeldingAuthError("Klientkonfigurasjonsfilen har ugyldig format")
    return value


def load_client_config(root: Path | str | None) -> ClientConfig:
    try:
        value = read_private_json(
            client_config_path(root),
            max_bytes=MAX_PRIVATE_JSON_BYTES,
            missing_message="Skolemelding-klientkonfigurasjonen mangler",
        )
    except PrivateStateError:
        raise SkolemeldingAuthError(
            "Skolemelding-klientkonfigurasjonen mangler eller er ugyldig"
        ) from None
    return _parse_client_config(value)


def _same_config(left: ClientConfig, right: ClientConfig) -> bool:
    """Compare fields that bind the OIDC/portal grant, excluding app version."""

    def identity(config: ClientConfig) -> dict[str, Any]:
        value = config.as_private_json()
        value.pop("app_version", None)
        value.pop("attachment_hosts", None)
        value.pop("attachment_path_prefixes", None)
        return value

    return hmac.compare_digest(
        json.dumps(identity(left), sort_keys=True, separators=(",", ":")).encode(),
        json.dumps(identity(right), sort_keys=True, separators=(",", ":")).encode(),
    )


def configure_client(
    root: Path | str | None, client_file: Path | str
) -> dict[str, Any]:
    """Validate and privately import non-user app metadata."""

    config = _parse_client_config(_read_import_file(client_file))
    with private_source_lock(root, SOURCE):
        if session_path(root).exists():
            current = load_client_config(root)
            if not _same_config(current, config):
                raise SkolemeldingAuthError(
                    "Logg ut før Skolemeldings auth-identitet endres"
                )
        atomic_write_private_json(client_config_path(root), config.as_private_json())
        marker = _read_refresh_block(root)
        if marker is not None and marker.get("reason") == "fatal-client-configuration":
            remove_private_file(refresh_block_path(root))
    return {
        "configured": True,
        "issuer": config.issuer,
        "redirect_uri": config.redirect_uri,
        "scopes_validated": True,
    }


def consent_status(root: Path | str | None) -> dict[str, Any]:
    try:
        value = read_private_json(
            consent_path(root),
            max_bytes=4096,
            missing_message="Samtykkemarkøren mangler",
        )
    except PrivateStateError:
        return {"recorded": False, "current": False, "version": None, "recorded_at": None}
    version = value.get("version") if isinstance(value, Mapping) else None
    recorded_at = value.get("recorded_at") if isinstance(value, Mapping) else None
    valid_time = isinstance(recorded_at, str) and bool(recorded_at)
    return {
        "recorded": isinstance(version, str) and valid_time,
        "current": version == CONSENT_VERSION and valid_time,
        "version": version if isinstance(version, str) else None,
        "recorded_at": recorded_at if valid_time else None,
    }


def record_consent(
    root: Path | str | None,
    *,
    version: str = CONSENT_VERSION,
    at: float | None = None,
) -> dict[str, Any]:
    if version != CONSENT_VERSION:
        raise SkolemeldingAuthError("Ukjent samtykkeversjon")
    timestamp = time.time() if at is None else at
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
        or timestamp <= 0
    ):
        raise SkolemeldingAuthError("Ugyldig tidspunkt for samtykke")
    recorded_at = utc_iso(timestamp)
    with private_source_lock(root, SOURCE):
        atomic_write_private_json(
            consent_path(root), {"version": version, "recorded_at": recorded_at}
        )
    return {
        "recorded": True,
        "current": True,
        "version": version,
        "recorded_at": recorded_at,
    }


def _random_urlsafe(random_bytes: Callable[[int], bytes]) -> str:
    return random_urlsafe(random_bytes, SkolemeldingAuthError)


def pkce_challenge(code_verifier: str) -> str:
    if not isinstance(code_verifier, str) or not 43 <= len(code_verifier) <= 128:
        raise SkolemeldingAuthError("PKCE-verifier har ugyldig format")
    return base64url(hashlib.sha256(code_verifier.encode("ascii")).digest())


def _device_id(
    root: Path | str | None,
    *,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> str:
    target = device_path(root)
    if not target.exists() and not target.is_symlink():
        identifier = f"fi-{_random_urlsafe(random_bytes)}"
        atomic_write_private_json(target, {"device_unique_id": identifier})
        return identifier
    return _read_device_id(root)


def _read_device_id(root: Path | str | None) -> str:
    """Read the stable device ID without ever creating or replacing it."""

    target = device_path(root)
    try:
        value = read_private_json(
            target, max_bytes=4096, missing_message="Enhets-ID mangler"
        )
    except PrivateStateError:
        raise SkolemeldingAuthError(
            "Skolemelding-enhets-ID-en kan ikke leses sikkert"
        ) from None
    if not isinstance(value, Mapping):
        raise SkolemeldingAuthError("Skolemelding-enhets-ID-en er ugyldig")
    return _nonempty_text(
        value.get("device_unique_id"), "Skolemelding-enhets-ID", maximum=256
    )


def _discover_endpoints(config: ClientConfig, transport: AuthTransport) -> tuple[str, str]:
    if config.authorization_endpoint and config.oidc_token_endpoint:
        return config.authorization_endpoint, config.oidc_token_endpoint
    try:
        value = transport.get_json(DISCOVERY_ENDPOINT)
    except Exception:
        raise SkolemeldingAuthError("ID-porten-konfigurasjonen kunne ikke hentes") from None
    if not isinstance(value, Mapping) or value.get("issuer") != ISSUER:
        raise SkolemeldingAuthError("ID-porten-konfigurasjonen har ugyldig issuer")
    return (
        _official_idporten_endpoint(
            value.get("authorization_endpoint"), "Authorization-endepunktet"
        ),
        _official_idporten_endpoint(value.get("token_endpoint"), "OIDC token-endepunktet"),
    )


def begin_login(
    root: Path | str | None,
    *,
    transport: AuthTransport | None = None,
    now: float | None = None,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> tuple[str, LoginTransaction]:
    """Create the browser URL and a separate, secret-bearing transaction."""

    if not consent_status(root)["current"]:
        raise SkolemeldingAuthError(
            "Eksplisitt samtykke må registreres før Skolemelding-innlogging"
        )
    marker = _read_refresh_block(root)
    if marker is not None and marker.get("reason") == "fatal-client-configuration":
        raise SkolemeldingFatalConfigurationError(
            "Skolemelding-appregistreringen er avvist"
        )
    config = load_client_config(root)
    client = transport or _default_transport()
    authorization_endpoint, token_endpoint = _discover_endpoints(config, client)
    created_at = time.monotonic() if now is None else now
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or not math.isfinite(created_at)
    ):
        raise SkolemeldingAuthError("Ugyldig tidspunkt for innlogging")
    with private_source_lock(root, SOURCE):
        device_id = _device_id(root, random_bytes=random_bytes)
    state = _random_urlsafe(random_bytes)
    nonce = _random_urlsafe(random_bytes)
    verifier = _random_urlsafe(random_bytes)
    transaction = LoginTransaction(
        config=config,
        state=state,
        nonce=nonce,
        code_verifier=verifier,
        device_unique_id=device_id,
        authorization_endpoint=authorization_endpoint,
        oidc_token_endpoint=token_endpoint,
        created_at=float(created_at),
    )
    parameters = [
        ("client_id", config.oidc_client_id),
        ("redirect_uri", config.redirect_uri),
        ("response_type", "code"),
        ("scope", " ".join(config.scopes)),
        ("state", state),
        ("nonce", nonce),
        ("code_challenge", pkce_challenge(verifier)),
        ("code_challenge_method", "S256"),
        ("prompt", "login"),
    ]
    return f"{authorization_endpoint}?{urllib.parse.urlencode(parameters)}", transaction


def _consume_callback(
    transaction: LoginTransaction, callback_url: str, *, now: float | None
) -> str:
    if not isinstance(transaction, LoginTransaction):
        raise SkolemeldingAuthError("OAuth-transaksjonen har ugyldig format")
    if transaction._consumed:
        raise SkolemeldingAuthError("OAuth-callbacken er allerede brukt")
    current = time.monotonic() if now is None else now
    if (
        isinstance(current, bool)
        or not isinstance(current, (int, float))
        or not math.isfinite(current)
        or float(current) < transaction.created_at
        or float(current) - transaction.created_at > CALLBACK_LIFETIME_SECONDS
    ):
        raise SkolemeldingAuthError("OAuth-callbacken er utløpt; start ny innlogging")
    if not isinstance(callback_url, str) or not callback_url or len(callback_url) > 8192:
        raise SkolemeldingAuthError("OAuth-callbacken har ugyldig format")
    try:
        parsed = urllib.parse.urlsplit(callback_url)
        expected = urllib.parse.urlsplit(transaction.config.redirect_uri)
    except ValueError:
        raise SkolemeldingAuthError("OAuth-callbacken har ugyldig format") from None
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or parsed.path != expected.path
        or parsed.fragment
    ):
        raise SkolemeldingAuthError("OAuth-callbacken bruker feil redirect-URI")
    try:
        pairs = urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=12,
        )
    except (UnicodeError, ValueError):
        raise SkolemeldingAuthError("OAuth-callbacken har ugyldige parametre") from None
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)) or "state" not in keys:
        raise SkolemeldingAuthError("OAuth-callbacken har ugyldige parametre")
    values = dict(pairs)
    state = values.get("state", "")
    if not hmac.compare_digest(state.encode(), transaction.state.encode()):
        raise SkolemeldingAuthError("OAuth-callbackens state samsvarer ikke")
    transaction._consumed = True
    if values.get("error"):
        raise SkolemeldingAuthError("ID-porten avviste eller avbrøt innloggingen")
    code = values.get("code")
    issuer = values.get("iss")
    if issuer is not None and not hmac.compare_digest(
        issuer.encode(), transaction.config.issuer.encode()
    ):
        raise SkolemeldingAuthError("OAuth-callbackens issuer samsvarer ikke")
    if not code or set(keys) - {"state", "code", "session_state", "iss"}:
        raise SkolemeldingAuthError("OAuth-callbacken mangler en gyldig kode")
    return _nonempty_text(code, "Authorization-koden", maximum=8192)


def _jwt_exp(value: str) -> float | None:
    try:
        encoded = value.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        expiry = float(payload["exp"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return expiry if math.isfinite(expiry) and expiry > 0 else None


def _validated_portal_tokens(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SkolemeldingAuthError("Portalen returnerte en ugyldig tokenrespons")
    result: dict[str, Any] = {
        "access_token": _token(value.get("access_token"), "Portal access-token"),
        "refresh_token": _token(value.get("refresh_token"), "Portal refresh-token"),
        "userName": _nonempty_text(value.get("userName"), "Portal-brukernavn", maximum=1024),
    }
    expires_in = value.get("expires_in")
    if (
        not isinstance(expires_in, bool)
        and isinstance(expires_in, (int, float))
        and math.isfinite(float(expires_in))
        and 0 < float(expires_in) <= 31_536_000
    ):
        result["expires_in"] = float(expires_in)
    jwt_expiry = _jwt_exp(result["access_token"])
    if jwt_expiry is not None:
        result["jwt_exp"] = jwt_expiry
    return result


def _session_json(
    tokens: Mapping[str, Any], device_id: str, *, at: float, transaction_id: str | None = None
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "userName": tokens["userName"],
        "device_unique_id": device_id,
        "updated_at": float(at),
    }
    expiry_candidates: list[float] = []
    if isinstance(tokens.get("expires_in"), (int, float)):
        expiry_candidates.append(float(at) + float(tokens["expires_in"]))
    if isinstance(tokens.get("jwt_exp"), (int, float)):
        expiry_candidates.append(float(tokens["jwt_exp"]))
    if expiry_candidates:
        value["access_expires_at"] = min(expiry_candidates)
    if transaction_id is not None:
        value["refresh_transaction_id"] = transaction_id
    return value


def _read_session(root: Path | str | None) -> dict[str, Any]:
    try:
        value = read_private_json(
            session_path(root),
            max_bytes=MAX_PRIVATE_JSON_BYTES,
            missing_message="Skolemelding-økten mangler",
        )
    except PrivateStateError:
        raise SkolemeldingReauthenticationRequired(
            "Skolemelding-økten mangler eller er ugyldig"
        ) from None
    if not isinstance(value, Mapping):
        raise SkolemeldingReauthenticationRequired("Skolemelding-økten er ugyldig")
    try:
        session = {
            **_validated_portal_tokens(value),
            "device_unique_id": _nonempty_text(
                value.get("device_unique_id"), "Skolemelding-enhets-ID", maximum=256
            ),
            "updated_at": float(value.get("updated_at")),
        }
    except (SkolemeldingAuthError, TypeError, ValueError):
        raise SkolemeldingReauthenticationRequired("Skolemelding-økten er ugyldig") from None
    transaction_id = value.get("refresh_transaction_id")
    if transaction_id is not None:
        session["refresh_transaction_id"] = _nonempty_text(
            transaction_id, "Refresh-transaksjonen", maximum=128
        )
    expires_at = value.get("access_expires_at")
    if expires_at is not None:
        try:
            numeric_expiry = float(expires_at)
        except (TypeError, ValueError):
            raise SkolemeldingReauthenticationRequired(
                "Skolemelding-økten har ugyldig utløpstid"
            ) from None
        if not math.isfinite(numeric_expiry) or numeric_expiry <= 0:
            raise SkolemeldingReauthenticationRequired(
                "Skolemelding-økten har ugyldig utløpstid"
            )
        session["access_expires_at"] = numeric_expiry
    return session


def _read_refresh_block(root: Path | str | None) -> Mapping[str, Any] | None:
    target = refresh_block_path(root)
    if not target.exists() and not target.is_symlink():
        return None
    try:
        value = read_private_json(
            target, max_bytes=MAX_PRIVATE_JSON_BYTES, missing_message="Refresh-markøren mangler"
        )
    except PrivateStateError:
        return {"reason": "invalid-marker"}
    if not isinstance(value, Mapping):
        return {"reason": "invalid-marker"}
    reason = value.get("reason")
    if not isinstance(reason, str):
        return {"reason": "invalid-marker"}
    return value


def _mark_refresh_blocked(
    root: Path | str | None,
    reason: str,
    *,
    at: float,
    transaction_id: str | None = None,
    diagnostic: Mapping[str, Any] | None = None,
) -> None:
    value: dict[str, Any] = {
        "reason": reason,
        "recorded_at": utc_iso(at),
    }
    if transaction_id is not None:
        value["transaction_id"] = transaction_id
    if diagnostic is not None:
        value["diagnostic"] = dict(diagnostic)
    atomic_write_private_json(refresh_block_path(root), value)


def _marker_completed_by_session(
    marker: Mapping[str, Any], session: Mapping[str, Any]
) -> bool:
    return (
        marker.get("reason") in {"refresh-in-flight", "refresh-response-received"}
        and isinstance(marker.get("transaction_id"), str)
        and isinstance(session.get("refresh_transaction_id"), str)
        and hmac.compare_digest(
            str(marker["transaction_id"]).encode(),
            str(session["refresh_transaction_id"]).encode(),
        )
    )


def _oauth_error(exc: Exception) -> str | None:
    for field_name in ("error_code", "oauth_error", "error"):
        value = getattr(exc, field_name, None)
        if isinstance(value, str) and value in _SAFE_OAUTH_ERRORS:
            return value
    return None


def _session_binding(session: Mapping[str, Any]) -> str:
    material = [session[key] for key in ("refresh_token", "userName", "device_unique_id")]
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()


def _recover_refresh_response(root, marker, session):
    """Recover only a response bound to this exact prior session, without HTTP."""
    binding = marker.get("session_binding")
    if not isinstance(binding, str) or not binding.isascii() or not hmac.compare_digest(binding, _session_binding(session)):
        raise SkolemeldingRefreshUncertain("Lagret fornyelsessvar tilhører ikke gjeldende økt")
    try:
        timestamp = float(marker["received_at"])
        if not math.isfinite(timestamp) or timestamp <= 0:
            raise ValueError()
        transaction = _nonempty_text(marker.get("transaction_id"), "Refresh-transaksjonen", maximum=128)
        tokens = _validated_portal_tokens(marker.get("response"))
    except (KeyError, ValueError, TypeError, SkolemeldingAuthError):
        raise SkolemeldingRefreshUncertain("Lagret fornyelsessvar må avklares før økten kan brukes") from None
    if not hmac.compare_digest(session["userName"].encode(), tokens["userName"].encode()):
        raise SkolemeldingReauthenticationRequired("Skolemelding-fornyelsen byttet portalidentitet")
    atomic_write_private_json(session_path(root), _session_json(
        tokens, session["device_unique_id"], at=timestamp, transaction_id=transaction))
    lifecycle.record(root, SOURCE, "response_recovered", value=marker["response"], at=timestamp)
    return _read_session(root)


@lifecycle.operation('skolemelding', 'login')
def login_with_callback(
    root: Path | str | None,
    transaction: LoginTransaction,
    callback_url: str,
    *,
    transport: AuthTransport | None = None,
    now: float | None = None,
    wall_time: float | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Complete OIDC, portal exchange and bootstrap without persisting OIDC tokens."""

    config = load_client_config(root)
    if not _same_config(config, transaction.config):
        raise SkolemeldingAuthError(
            "Skolemelding-klientkonfigurasjonen ble endret; start innloggingen på nytt"
        )
    if session_path(root).exists() and not replace_existing and not transaction.consumed:
        raise SkolemeldingAuthError(
            "En lokal Skolemelding-økt finnes allerede; bekreft at den skal erstattes"
        )
    code = _consume_callback(transaction, callback_url, now=now)
    client = transport or _default_transport(
        allowed_idporten_urls=(transaction.oidc_token_endpoint,)
    )
    client = lifecycle.observe_transport(root, 'skolemelding', client, at=wall_time)
    registering_device = False
    try:
        oidc_value = client.post_form(
            transaction.oidc_token_endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "client_id": config.oidc_client_id,
                "code_verifier": transaction.code_verifier,
            },
        )
        if not isinstance(oidc_value, Mapping):
            raise SkolemeldingAuthError("ID-porten returnerte en ugyldig tokenrespons")
        oidc_access_token = _token(oidc_value.get("access_token"), "ID-porten access-token")
        app_token_value = client.post_json(
            APP_TOKEN_ENDPOINT,
            {"accessToken": oidc_access_token, "provider": "idporten"},
        )
        if not isinstance(app_token_value, Mapping):
            raise SkolemeldingAuthError("Portalutvekslingen returnerte ugyldige data")
        auth_code = _nonempty_text(
            app_token_value.get("authCode"), "Portalens engangskode", maximum=8192
        )
        portal_value = client.post_form(
            PORTAL_TOKEN_ENDPOINT,
            {
                "grant_type": "password",
                "username": auth_code,
                "scope": transaction.device_unique_id,
                "client_id": config.portal_client_id,
                "app_version": config.app_version,
                # Vendor protocol marker: this is not a user password and the
                # module never accepts or stores a Skolemelding/ID-porten password.
                "password": "code",
            },
        )
        tokens = _validated_portal_tokens(portal_value)
        # The official app registers device metadata even without push
        # permission. The portal binds the device through the authenticated
        # token (whose initial grant carries scope=device_unique_id).
        # Report this client's actual OS; do not invent an Android version or
        # enroll for push, biometrics, or another device's credentials.
        registering_device = True
        client.post_json(
            DEVICE_INFO_ENDPOINT,
            {
                "OsVersion": f"{platform.system().lower()}|{platform.release()}",
                "AppVersion": config.app_version,
            },
            bearer_token=tokens["access_token"],
            allow_empty=True,
        )
    except SkolemeldingAuthError:
        raise
    except Exception as exc:
        if registering_device:
            raise SkolemeldingAuthError(
                "Skolemelding-enheten kunne ikke registreres; den nye økten er ikke aktivert"
            ) from None
        oauth_error = _oauth_error(exc)
        if oauth_error in _FATAL_OAUTH_ERRORS:
            timestamp = time.time() if wall_time is None else wall_time
            with private_source_lock(root, SOURCE):
                _mark_refresh_blocked(
                    root, "fatal-client-configuration", at=float(timestamp)
                )
            raise SkolemeldingFatalConfigurationError(
                "Skolemelding-appregistreringen er avvist"
            ) from None
        raise SkolemeldingAuthError(
            "Innloggingens tokenutveksling fikk et ukjent resultat; start på nytt"
        ) from None
    timestamp = time.time() if wall_time is None else wall_time
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
        or timestamp <= 0
    ):
        raise SkolemeldingAuthError("Ugyldig tidspunkt for tokenlagring")
    with private_source_lock(root, SOURCE, wait=True):
        current_device_id = _device_id(root)
        if not hmac.compare_digest(
            current_device_id.encode(), transaction.device_unique_id.encode()
        ):
            raise SkolemeldingAuthError(
                "Skolemelding-enhets-ID-en ble endret; start innloggingen på nytt"
            )
        target = session_path(root)
        if (target.exists() or target.is_symlink()) and not replace_existing:
            raise SkolemeldingAuthError(
                "En lokal Skolemelding-økt finnes allerede; bekreft at den skal erstattes"
            )
        try:
            atomic_write_private_json(
                target,
                _session_json(tokens, current_device_id, at=float(timestamp)),
                overwrite=replace_existing,
            )
        except PrivateStateError:
            if not replace_existing and (target.exists() or target.is_symlink()):
                raise SkolemeldingAuthError(
                    "En lokal Skolemelding-økt finnes allerede; "
                    "bekreft at den skal erstattes"
                ) from None
            raise
        remove_private_file(refresh_block_path(root))
    stored_session = _session_json(tokens, current_device_id, at=float(timestamp))
    expires_at = stored_session.get("access_expires_at")
    result = {
        "authenticated": True,
        "provider": "idporten",
        "portal_session_stored": True,
        "device_id_persisted": True,
        "device_registered": True,
    }
    if isinstance(expires_at, (int, float)):
        result["expires_at"] = utc_iso(float(expires_at))
    return result


def local_session_for_use(root: Path | str | None) -> dict[str, Any]:
    with private_source_lock(root, SOURCE):
        return _local_session_for_use_locked(root)


def _local_session_for_use_locked(root: Path | str | None) -> dict[str, Any]:
    """Return the secret-bearing portal session only for the collector."""

    if not consent_status(root)["current"]:
        raise SkolemeldingReauthenticationRequired(
            "Eksplisitt samtykke mangler for Skolemelding"
        )
    load_client_config(root)
    session = _read_session(root)
    try:
        # Only an explicit begin_login bootstrap may create a device ID. A
        # missing file during unattended collection must fail closed without
        # silently replacing the server-bound identity.
        device = _read_device_id(root)
    except SkolemeldingAuthError:
        raise SkolemeldingReauthenticationRequired(
            "Skolemelding-enhets-ID-en er ugyldig"
        ) from None
    if not hmac.compare_digest(device.encode(), session["device_unique_id"].encode()):
        raise SkolemeldingReauthenticationRequired(
            "Skolemelding-øktens enhets-ID samsvarer ikke"
        )
    marker = _read_refresh_block(root)
    if marker is not None:
        if marker.get("reason") == "refresh-response-received" and not _marker_completed_by_session(marker, session):
            session = _recover_refresh_response(root, marker, session)
        if _marker_completed_by_session(marker, session):
            try:
                remove_private_file(refresh_block_path(root))
            except PrivateStateError:
                pass
        elif marker.get("reason") == "fatal-client-configuration":
            raise SkolemeldingFatalConfigurationError(
                "Skolemelding-appregistreringen er avvist"
            )
        elif marker.get("reason") == "invalid-grant":
            raise SkolemeldingReauthenticationRequired(
                "Skolemelding-refresh-tokenet er avvist"
            )
        else:
            raise SkolemeldingRefreshUncertain(
                "Fornyelsen har ukjent utfall; økten er satt på pause for avklaring"
            )
    return session


@lifecycle.operation('skolemelding', 'refresh')
def refresh_session(
    root: Path | str | None,
    *,
    transport: AuthTransport | None = None,
    at: float | None = None,
) -> dict[str, Any]:
    """Rotate the portal tokens, blocking reuse after any ambiguous result."""

    timestamp = time.time() if at is None else at
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
        or timestamp <= 0
    ):
        raise SkolemeldingAuthError("Ugyldig tidspunkt for tokenfornyelse")
    timestamp = float(timestamp)
    with private_source_lock(root, SOURCE):
        session = local_session_for_use(root)
        config = load_client_config(root)
        transaction_id = _random_urlsafe(secrets.token_bytes)
        _mark_refresh_blocked(
            root,
            "refresh-in-flight",
            at=timestamp,
            transaction_id=transaction_id,
        )
        client = lifecycle.observe_transport(root, SOURCE, transport or _default_transport(), at=timestamp, capture_response=False)
        response_saved = False
        stage = "request"
        try:
            value = client.post_form(
                PORTAL_TOKEN_ENDPOINT,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": session["refresh_token"],
                    "client_id": config.portal_client_id,
                    "app_version": config.app_version,
                    "username": session["userName"],
                },
            )
            stage = "persist-response"
            # Retain the returned token privately before validation or replacing
            # the old session. A crash afterwards must not lose a rotated grant.
            atomic_write_private_json(refresh_block_path(root), {
                "reason": "refresh-response-received", "transaction_id": transaction_id,
                "received_at": timestamp, "session_binding": _session_binding(session),
                "response": {key: value[key] for key in ("access_token", "refresh_token", "userName", *lifecycle.TIME_FIELDS)
                             if key in value} if isinstance(value, Mapping) else None,
            })
            response_saved = True
            lifecycle.record(root, SOURCE, "response_saved", value=value, at=timestamp)
            stage = "validate-response"
            tokens = _validated_portal_tokens(value)
            if not hmac.compare_digest(
                session["userName"].encode(), tokens["userName"].encode()
            ):
                raise SkolemeldingReauthenticationRequired(
                    "Skolemelding-refreshen byttet portalidentitet; logg inn på nytt"
                )
        except Exception as exc:
            if isinstance(exc, SkolemeldingReauthenticationRequired):
                raise
            if isinstance(exc, ApiRequestNotSent):
                remove_private_file(refresh_block_path(root))
                raise SkolemeldingRefreshUncertain('Forespørselen ble ikke sendt; kan forsøkes ved neste runde') from None
            oauth_error = _oauth_error(exc)
            if oauth_error in _FATAL_OAUTH_ERRORS:
                _mark_refresh_blocked(
                    root, "fatal-client-configuration", at=timestamp
                )
                raise SkolemeldingFatalConfigurationError(
                    "Skolemelding-appregistreringen er avvist"
                ) from None
            if oauth_error in {"invalid_grant", "invalid_refresh_token"}:
                _mark_refresh_blocked(root, "invalid-grant", at=timestamp,
                                      diagnostic={"stage": "request", "kind": "http",
                                                  "http_status": getattr(exc, "status_code", 400)})
                raise SkolemeldingReauthenticationRequired(
                    "Skolemelding-refresh-tokenet er avvist; logg inn på nytt"
                ) from None
            if not response_saved:
                diagnostic = {"stage": stage, "kind": "http" if isinstance(exc, ApiHTTPError)
                              else "transport" if isinstance(exc, ApiTransportError) else "local"}
                if isinstance(exc, ApiHTTPError):
                    diagnostic["http_status"] = exc.status_code
                _mark_refresh_blocked(root, "refresh-in-flight", at=timestamp,
                                      transaction_id=transaction_id, diagnostic=diagnostic)
            # Unknown is not invalid_grant. Retain the barrier against replay,
            # but do not claim that a new login is known to be necessary.
            raise SkolemeldingRefreshUncertain(
                "Skolemelding-fornyelsen må avklares; økten er satt på pause"
            ) from None
        rotated = not hmac.compare_digest(
            session["refresh_token"].encode(), tokens["refresh_token"].encode()
        )
        atomic_write_private_json(
            session_path(root),
            _session_json(
                tokens,
                session["device_unique_id"],
                at=timestamp,
                transaction_id=transaction_id,
            ),
        )
        cleanup_pending = False
        try:
            remove_private_file(refresh_block_path(root))
        except PrivateStateError:
            cleanup_pending = True
    result = {
        "refreshed": True,
        "refresh_token_rotated": rotated,
        "refresh_marker_cleanup_pending": cleanup_pending,
    }
    expires_at = _session_json(
        tokens,
        session["device_unique_id"],
        at=timestamp,
        transaction_id=transaction_id,
    ).get("access_expires_at")
    if isinstance(expires_at, (int, float)):
        result["expires_at"] = utc_iso(float(expires_at))
    return result


def auth_status(root: Path | str | None) -> dict[str, Any]:
    """Return offline, identity-free status metadata."""

    configured = True
    try:
        load_client_config(root)
    except SkolemeldingAuthError:
        configured = False
    consent = consent_status(root)
    marker = _read_refresh_block(root)
    authenticated = False
    expires_at: str | None = None
    target_session = session_path(root)
    session_present = target_session.is_file() and not target_session.is_symlink()
    if configured and consent["current"]:
        try:
            # Status reads must not create a replacement device
            # ID or clear a recoverable marker.  Validate the same bindings as
            # local_session_for_use, but keep this path strictly read-only.
            session = _read_session(root)
            device = _read_device_id(root)
            if not hmac.compare_digest(
                device.encode(), session["device_unique_id"].encode()
            ):
                raise SkolemeldingAuthError(
                    "Skolemelding-øktens enhets-ID samsvarer ikke"
                )
            if marker is not None and not _marker_completed_by_session(marker, session):
                raise SkolemeldingAuthError("Skolemelding-refresh er blokkert")
        except (SkolemeldingAuthError, SourceLoginRequired):
            authenticated = False
        else:
            authenticated = True
            numeric_expiry = session.get("access_expires_at")
            if isinstance(numeric_expiry, (int, float)):
                expires_at = utc_iso(float(numeric_expiry))
    reason = marker.get("reason") if isinstance(marker, Mapping) else None
    if authenticated and marker is not None and _marker_completed_by_session(marker, session):
        reason = None
    return {
        "configured": configured,
        "consent_recorded": consent["current"],
        "session_present": session_present,
        "authenticated": authenticated,
        "expires_at": expires_at,
        "refresh_blocked": reason is not None,
        "refresh_block_reason": reason,
        "refresh_response_saved": bool(marker and marker.get("reason") == "refresh-response-received"),
        "refresh_diagnostic": {
            key: value for key, value in (marker.get("diagnostic", {}) if marker else {}).items()
            if (key == "stage" and value in ("request", "persist-response", "validate-response"))
            or (key == "kind" and value in ("http", "transport", "local"))
            or (key == "http_status" and type(value) is int and 100 <= value <= 599)
        } if marker and isinstance(marker.get("diagnostic", {}), dict) else {},
        "device_id_present": device_path(root).is_file()
        and not device_path(root).is_symlink(),
    }


@lifecycle.operation('skolemelding', 'logout')
def logout(root: Path | str | None) -> dict[str, Any]:
    """Remove portal tokens locally while retaining consent, config and device ID."""

    with private_source_lock(root, SOURCE, wait=True):
        removed = remove_private_file(session_path(root))
        marker_removed = remove_private_file(refresh_block_path(root))
    return {
        "logged_out": True,
        "local_session_removed": removed,
        "refresh_block_removed": marker_removed,
        "device_id_retained": device_path(root).is_file(),
    }


__all__ = [
    "SkolemeldingAuthError",
    "SkolemeldingReauthenticationRequired",
    "SkolemeldingFatalConfigurationError",
    "LoginTransaction",
    "auth_status",
    "begin_login",
    "configure_client",
    "consent_status",
    "load_client_config",
    "local_session_for_use",
    "login_with_callback",
    "logout",
    "pkce_challenge",
    "record_consent",
    "refresh_session",
    "session_path",
]
