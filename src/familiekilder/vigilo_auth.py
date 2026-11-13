"""Private OAuth state for the unofficial, read-only Vigilo parent adapter.

The official Android client uses an authorization-code grant once and then
rotates a refresh token.  PIN and biometrics only unlock that local state.  We
mirror the protocol boundary without automating ID-porten, and keep all client
metadata and user tokens below ``.private/credentials``.

Nothing in this module prints or logs.  Secret-bearing objects redact their
``repr`` and public result dictionaries contain status metadata only.
"""

from __future__ import annotations

from . import auth_lifecycle as lifecycle
from .api_transport import ApiRequestNotSent

import base64
import hmac
import re
import importlib
import math
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .api_transport import ApiHTTPError, ApiTransport, ApiTransportError
from .auth_common import (
    base64url,
    canonical_sha256,
    fingerprint_matches,
    has_invalid_percent_encoding,
    is_plain_text,
    normalize_public_host,
    random_urlsafe,
    read_private_mapping,
    safe_call,
    utc_iso,
)
from .core import FamiliekilderError
from .private_lock import private_source_lock
from .private_state import (
    PrivateStateError,
    atomic_write_private_json,
    credential_path,
    remove_private_file,
)
from .source_adapter import SourceFatalConfigurationError, SourceLoginRequired, SourceUnavailable


SOURCE = "vigilo"
ISSUER = "https://auth.prod.vigilo-oas.no"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/connect/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/connect/token"
USERINFO_ENDPOINT = f"{ISSUER}/connect/userinfo"
REVOCATION_ENDPOINT = f"{ISSUER}/connect/revocation"
JWKS_URI = f"{ISSUER}/.well-known/jwks"
API_ROOT = "https://api-gw-parent-app.prod.vigilo-oas.no/"
REDIRECT_URI = "app://ch-parent-android.vigilo.no"
EXPECTED_SCOPES = frozenset({"openid", "vigiloprofile", "offline_access"})

CLIENT_FILE = "vigilo-client.json"
SESSION_FILE = "vigilo-session.json"
CONSENT_FILE = "vigilo-oauth-consent.json"
REFRESH_BLOCK_FILE = "vigilo-refresh-blocked.json"
CONSENT_VERSION = "unofficial-vigilo-oauth-v1"
# A login waits for the user to finish ID-porten and approve BankID on the
# phone. Keep the in-memory state alive for a bounded window long enough that
# a slow approval does not discard an otherwise valid callback.
CALLBACK_LIFETIME_SECONDS = 30 * 60
MAX_JSON_BYTES = 1024 * 1024
REFRESH_SKEW_SECONDS = 60
_SAFE_OAUTH_ERRORS = frozenset(
    {
        "invalid_client",
        "unauthorized_client",
        "invalid_scope",
        "invalid_request",
        "invalid_grant",
    }
)


class VigiloOAuthError(FamiliekilderError):
    """Sanitized local or remote OAuth failure."""


class VigiloReauthenticationRequired(SourceLoginRequired, VigiloOAuthError):
    """The grant is absent, rejected, or unsafe to reuse."""


class VigiloFatalConfigurationError(SourceFatalConfigurationError, VigiloOAuthError):
    """The provider rejected immutable mobile-client registration details."""


@dataclass(frozen=True)
class ClientConfig:
    authority: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    scope: str
    api_root: str
    app_version: str
    attachment_hosts: tuple[str, ...] = ()

    def as_private_json(self) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "scope": self.scope,
            "api_root": self.api_root,
            "app_version": self.app_version,
            "attachment_hosts": list(self.attachment_hosts),
        }


@dataclass(repr=False)
class LoginTransaction:
    config: ClientConfig = field(repr=False)
    state: str = field(repr=False)
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


class OAuthTransport(Protocol):
    def post_form(
        self,
        url: str,
        fields: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any: ...

    def get_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any: ...


AccessTokenValidator = Callable[
    [str, Mapping[str, Any], ClientConfig, float], Mapping[str, Any]
]


def client_config_path(root: Path | str | None) -> Path:
    return credential_path(root, CLIENT_FILE)


def session_path(root: Path | str | None) -> Path:
    """Return the canonical private Vigilo session path."""

    return credential_path(root, SESSION_FILE)


def consent_path(root: Path | str | None) -> Path:
    return credential_path(root, CONSENT_FILE)


def refresh_block_path(root: Path | str | None) -> Path:
    return credential_path(root, REFRESH_BLOCK_FILE)


def _required_text(value: Any, label: str, *, max_length: int = 4096) -> str:
    if not is_plain_text(value, maximum=max_length):
        raise VigiloOAuthError(f"Vigilo-konfigurasjonen har ugyldig {label}")
    return value


def _normalize_scope(value: Any) -> str:
    if isinstance(value, str):
        scopes = value.split()
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        scopes = list(value)
    else:
        raise VigiloOAuthError("Vigilo-konfigurasjonen har ugyldig scope")
    if len(scopes) != len(set(scopes)) or set(scopes) != EXPECTED_SCOPES:
        raise VigiloOAuthError(
            "Vigilo-konfigurasjonen mangler det fastlåste app-scope-settet"
        )
    return " ".join(scopes)


def _normalize_host(value: Any) -> str:
    host = normalize_public_host(value, strict=False)
    if host is None:
        raise VigiloOAuthError("Vigilo-konfigurasjonen har ugyldig vedleggsvert")
    return host


def _parse_client_config(value: Mapping[str, Any]) -> ClientConfig:
    raw_hosts = value.get("attachment_hosts", [])
    if not isinstance(raw_hosts, list) or not all(
        isinstance(host, str) for host in raw_hosts
    ):
        raise VigiloOAuthError("Vigilo-konfigurasjonen har ugyldig vedleggsvertsliste")
    attachment_hosts = tuple(sorted({_normalize_host(host) for host in raw_hosts}))
    config = ClientConfig(
        authority=_required_text(value.get("authority"), "authority", max_length=200),
        client_id=_required_text(value.get("client_id"), "client-ID", max_length=300),
        client_secret=_required_text(
            value.get("client_secret"), "klienthemmelighet", max_length=4096
        ),
        redirect_uri=_required_text(
            value.get("redirect_uri"), "redirect-URI", max_length=500
        ),
        scope=_normalize_scope(value.get("scope")),
        api_root=_required_text(value.get("api_root", API_ROOT), "API-rot", max_length=500),
        app_version=_required_text(value.get("app_version"), "appversjon", max_length=100),
        attachment_hosts=attachment_hosts,
    )
    if config.authority != ISSUER:
        raise VigiloOAuthError("Vigilo-authority er ikke produksjonsutstederen")
    if config.redirect_uri != REDIRECT_URI:
        raise VigiloOAuthError("Vigilo redirect-URI samsvarer ikke med foreldreappen")
    if config.api_root != API_ROOT:
        raise VigiloOAuthError("Vigilo API-roten er ikke det fastlåste produksjons-API-et")
    if not config.client_id.endswith(".ANDROID"):
        raise VigiloOAuthError("Vigilo client-ID samsvarer ikke med Android-klienttypen")
    if not config.app_version.startswith("Android "):
        raise VigiloOAuthError("Vigilo appversjonen samsvarer ikke med Android-klienten")
    return config


def _read_mapping(path: Path, missing_message: str) -> Mapping[str, Any]:
    return read_private_mapping(
        path,
        missing_message,
        max_bytes=MAX_JSON_BYTES,
        error=VigiloOAuthError,
        invalid_message="Den private Vigilo-filen har ugyldig format",
    )


def load_client_config(root: Path | str | None) -> ClientConfig:
    return _parse_client_config(
        _read_mapping(
            client_config_path(root), "Vigilo-klientkonfigurasjonen mangler"
        )
    )


def _auth_identity_json(config: ClientConfig) -> dict[str, str]:
    """Return only fields that bind an OAuth grant to one app registration."""

    return {
        "authority": config.authority,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "redirect_uri": config.redirect_uri,
        "scope": config.scope,
        "api_root": config.api_root,
    }


def _config_fingerprint(config: ClientConfig) -> str:
    """Fingerprint immutable auth identity, not operational app metadata."""

    return canonical_sha256(_auth_identity_json(config))


def _legacy_config_fingerprint(config: ClientConfig) -> str:
    return canonical_sha256(config.as_private_json())


def _fingerprint_matches_config(fingerprint: object, config: ClientConfig) -> bool:
    return fingerprint_matches(
        fingerprint, (_config_fingerprint(config), _legacy_config_fingerprint(config))
    )


def _same_config(left: ClientConfig, right: ClientConfig) -> bool:
    return hmac.compare_digest(
        _config_fingerprint(left).encode("ascii"),
        _config_fingerprint(right).encode("ascii"),
    )


def configure_client(
    root: Path | str | None, client_file: Path | str
) -> dict[str, Any]:
    """Import validated app metadata without returning the client secret."""

    # Read the exact supplied name so the shared O_NOFOLLOW boundary can reject
    # a symlink instead of silently following it through Path.resolve().
    source = Path(client_file).expanduser()
    value = _read_mapping(source, "Fant ikke Vigilo-klientkonfigurasjonsfilen")
    config = _parse_client_config(value)
    with private_source_lock(root, SOURCE):
        target_session = session_path(root)
        if target_session.exists() or target_session.is_symlink():
            try:
                current_config = load_client_config(root)
                current_session = _read_mapping(target_session, "Vigilo-økten mangler")
                current_fingerprint = current_session.get("client_fingerprint")
            except VigiloOAuthError:
                raise VigiloOAuthError(
                    "Vigilo-klientkonfigurasjonen kan ikke oppdateres mens økten er ugyldig"
                ) from None
            if not _fingerprint_matches_config(current_fingerprint, current_config):
                raise VigiloOAuthError(
                    "Vigilo-øktens eksisterende klientbinding er ugyldig"
                )
            if not _same_config(current_config, config):
                raise VigiloOAuthError(
                    "Logg ut før Vigilos OAuth-identitet endres"
                )
            # Sessions created before the profile split bound app_version and
            # attachment_hosts too. Migrate that fingerprint before writing
            # mutable metadata so a crash leaves either state internally valid.
            if not hmac.compare_digest(
                str(current_fingerprint).encode("ascii", "ignore"),
                _config_fingerprint(current_config).encode("ascii"),
            ):
                migrated_session = dict(current_session)
                migrated_session["client_fingerprint"] = _config_fingerprint(
                    current_config
                )
                atomic_write_private_json(target_session, migrated_session)
        try:
            atomic_write_private_json(client_config_path(root), config.as_private_json())
            if _marker_reason(root) == "fatal-client-configuration":
                remove_private_file(refresh_block_path(root))
        except PrivateStateError as exc:
            raise VigiloOAuthError(str(exc)) from None
    return {
        "configured": True,
        "authority": config.authority,
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope_validated": True,
        "api_root_validated": True,
        "app_version_validated": True,
        "attachment_hosts": len(config.attachment_hosts),
    }


def consent_status(root: Path | str | None) -> dict[str, Any]:
    try:
        value = _read_mapping(consent_path(root), "Samtykkemarkøren mangler")
    except VigiloOAuthError:
        return {
            "recorded": False,
            "current": False,
            "version": None,
            "recorded_at": None,
        }
    version = value.get("version") if isinstance(value.get("version"), str) else None
    recorded_at = (
        value.get("recorded_at")
        if isinstance(value.get("recorded_at"), str)
        else None
    )
    return {
        "recorded": version is not None and recorded_at is not None,
        "current": version == CONSENT_VERSION and recorded_at is not None,
        "version": version,
        "recorded_at": recorded_at,
    }


def record_consent(
    root: Path | str | None,
    *,
    version: str = CONSENT_VERSION,
    at: float | None = None,
) -> dict[str, Any]:
    if version != CONSENT_VERSION:
        raise VigiloOAuthError("Ukjent Vigilo-samtykkeversjon")
    timestamp = time.time() if at is None else at
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
        or timestamp <= 0
    ):
        raise VigiloOAuthError("Ugyldig tidspunkt for Vigilo-samtykke")
    recorded_at = utc_iso(timestamp)
    with private_source_lock(root, SOURCE):
        try:
            atomic_write_private_json(
                consent_path(root),
                {"version": version, "recorded_at": recorded_at},
            )
        except PrivateStateError as exc:
            raise VigiloOAuthError(str(exc)) from None
    return {
        "recorded": True,
        "current": True,
        "version": version,
        "recorded_at": recorded_at,
    }


def begin_login(
    root: Path | str | None,
    *,
    now: float | None = None,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> tuple[str, LoginTransaction]:
    if not consent_status(root)["current"]:
        raise VigiloOAuthError(
            "Eksplisitt samtykke må registreres før uoffisiell Vigilo-innlogging"
        )
    if _marker_reason(root) == "fatal-client-configuration":
        raise VigiloFatalConfigurationError(
            "Vigilo-appregistreringen er avvist; importer klientmetadata på nytt"
        )
    config = load_client_config(root)
    state = random_urlsafe(random_bytes, VigiloOAuthError)
    created_at = time.monotonic() if now is None else now
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or not math.isfinite(created_at)
    ):
        raise VigiloOAuthError("Ugyldig tidspunkt for Vigilo-innlogging")
    transaction = LoginTransaction(config, state, float(created_at))
    # This legacy confidential Android registration does not use PKCE. Mirror
    # the verified app request exactly, except for OAuth state, which the
    # provider echoes and lets the local receiver bind the callback to this
    # one in-memory transaction.
    parameters = [
        ("client_id", config.client_id),
        ("redirect_uri", config.redirect_uri),
        ("response_type", "code"),
        ("scope", config.scope),
        ("state", state),
        ("prompt", "login"),
        ("display", "touch"),
    ]
    return (
        f"{AUTHORIZATION_ENDPOINT}?{urllib.parse.urlencode(parameters)}",
        transaction,
    )


def _consume_callback(
    transaction: LoginTransaction, callback_url: str, *, now: float | None
) -> str:
    if not isinstance(transaction, LoginTransaction):
        raise VigiloOAuthError("Vigilo OAuth-transaksjonen har ugyldig format")
    if transaction._consumed:
        raise VigiloOAuthError("Vigilo OAuth-callbacken er allerede brukt")
    current = time.monotonic() if now is None else now
    if (
        isinstance(current, bool)
        or not isinstance(current, (int, float))
        or not math.isfinite(current)
        or float(current) < transaction.created_at
        or float(current) - transaction.created_at > CALLBACK_LIFETIME_SECONDS
    ):
        raise VigiloOAuthError("Vigilo OAuth-callbacken er utløpt")
    if not is_plain_text(callback_url, maximum=8192) or has_invalid_percent_encoding(
        callback_url
    ):
        raise VigiloOAuthError("Vigilo OAuth-callbacken har ugyldig format")
    try:
        parsed = urllib.parse.urlsplit(callback_url)
        expected = urllib.parse.urlsplit(transaction.config.redirect_uri)
    except ValueError:
        raise VigiloOAuthError("Vigilo OAuth-callbacken har ugyldig format") from None
    # The provider may return an authority-only custom URI with a root slash
    # even though the registered redirect omits it.  Both forms identify the
    # same app-scheme origin; no other path is accepted.
    callback_path = "" if parsed.path == "/" else parsed.path
    expected_path = "" if expected.path == "/" else expected.path
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or callback_path != expected_path
        or parsed.fragment
    ):
        raise VigiloOAuthError("Vigilo OAuth-callbacken bruker feil redirect-URI")
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
        raise VigiloOAuthError("Vigilo OAuth-callbacken har ugyldige parametre") from None
    keys = [key for key, _value in pairs]
    allowed = {
        "code",
        "state",
        "session_state",
        "iss",
        "error",
        "error_description",
        "error_uri",
    }
    if len(keys) != len(set(keys)) or any(key not in allowed for key in keys):
        raise VigiloOAuthError("Vigilo OAuth-callbacken har ugyldige parametre")
    parameters = dict(pairs)
    if ("code" in parameters) == ("error" in parameters):
        raise VigiloOAuthError("Vigilo OAuth-callbacken har ikke én entydig respons")
    returned_state = parameters.get("state")
    if (
        not isinstance(returned_state, str)
        or not hmac.compare_digest(
            returned_state.encode("utf-8"), transaction.state.encode("utf-8")
        )
    ):
        raise VigiloOAuthError("Vigilo OAuth-callbackens state samsvarer ikke")
    returned_issuer = parameters.get("iss")
    if returned_issuer is not None and returned_issuer != ISSUER:
        raise VigiloOAuthError("Vigilo OAuth-callbackens issuer samsvarer ikke")
    if "error" in parameters:
        transaction._consumed = True
        if parameters["error"] in {
            "invalid_client",
            "unauthorized_client",
            "invalid_scope",
            "invalid_request",
        }:
            raise VigiloFatalConfigurationError(
                "Vigilo avviste appregistreringen"
            )
        raise VigiloOAuthError("Vigilo fullførte ikke autorisasjonen")
    code = parameters.get("code")
    if (
        not isinstance(code, str)
        or not code
        or len(code) > 4096
        or any(not 33 <= ord(character) <= 126 for character in code)
    ):
        raise VigiloOAuthError("Vigilo-callbacken mangler en gyldig kode")
    transaction._consumed = True
    return code


def _basic_auth(config: ClientConfig) -> str:
    value = f"{config.client_id}:{config.client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(value).decode("ascii")


def _default_transport(config: ClientConfig) -> ApiTransport:
    return ApiTransport(
        allowed_urls={TOKEN_ENDPOINT, USERINFO_ENDPOINT, REVOCATION_ENDPOINT, JWKS_URI},
        allowed_hosts={urllib.parse.urlsplit(config.api_root).hostname or ""},
        safe_error_codes=_SAFE_OAUTH_ERRORS,
        max_json_bytes=MAX_JSON_BYTES,
        user_agent="familiekilder-vigilo-auth/0.2",
    )


def _safe_token(value: Any, label: str) -> str:
    if not is_plain_text(value, maximum=131072, allow_space=False):
        raise VigiloOAuthError(f"Vigilo returnerte ugyldig {label}")
    return value


def _number(value: Any, label: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool):
        raise VigiloOAuthError(f"Vigilo-tokenet har ugyldig {label}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise VigiloOAuthError(f"Vigilo-tokenet har ugyldig {label}") from None
    if not math.isfinite(number) or number < minimum or number > 10**11:
        raise VigiloOAuthError(f"Vigilo-tokenet har ugyldig {label}")
    return number


def _required_claim(claims: Mapping[str, Any], name: str) -> str:
    value = claims.get(name)
    if not is_plain_text(value, maximum=4096):
        raise VigiloOAuthError(f"Vigilo-tokenet har ugyldig {name}")
    return value


def _validate_access_claims(
    claims: Mapping[str, Any], *, config: ClientConfig, now: float
) -> Mapping[str, Any]:
    if claims.get("iss") != ISSUER:
        raise VigiloOAuthError("Vigilo-tokenets issuer samsvarer ikke")
    audience = claims.get("aud")
    if isinstance(audience, str):
        audiences = [audience]
    elif isinstance(audience, list) and all(isinstance(item, str) for item in audience):
        audiences = audience
    else:
        raise VigiloOAuthError("Vigilo-tokenets audience har ugyldig format")
    if not audiences or any(
        not is_plain_text(value, maximum=4096) for value in audiences
    ):
        raise VigiloOAuthError("Vigilo-tokenets audience har ugyldig format")
    # `vigiloprofile` is the OAuth scope, not the resource audience. The live,
    # issuer-signed mobile token uses a different provider-defined audience.
    # Bind the token to this exact client and scope below, then prove that the
    # intended API accepts it through userinfo and the selected tenant endpoint.
    token_client = claims.get("client_id")
    if token_client != config.client_id:
        raise VigiloOAuthError("Vigilo-tokenets klientbinding samsvarer ikke")
    raw_scope = claims.get("scope")
    if isinstance(raw_scope, str):
        scopes = set(raw_scope.split())
    elif isinstance(raw_scope, list) and all(isinstance(item, str) for item in raw_scope):
        scopes = set(raw_scope)
    else:
        raise VigiloOAuthError("Vigilo-tokenets scope har ugyldig format")
    if "vigiloprofile" not in scopes:
        raise VigiloOAuthError("Vigilo-tokenet mangler profil-scope")
    _required_claim(claims, "sub")
    _required_claim(claims, "personId")
    _required_claim(claims, "tenantId")
    expiry = _number(claims.get("exp"), "expiry", minimum=1)
    if expiry <= now:
        raise VigiloOAuthError("Vigilo-tokenet er utløpt")
    not_before = _number(claims.get("nbf", 0), "not-before")
    if not_before > now + lifecycle.NOT_BEFORE_LEEWAY_SECONDS:
        raise VigiloOAuthError("Vigilo-tokenet er ikke gyldig ennå")
    return claims


def _pyjwt_access_token_validator(
    token: str,
    jwks: Mapping[str, Any],
    config: ClientConfig,
    now: float,
) -> Mapping[str, Any]:
    try:
        jwt = importlib.import_module("jwt")
        header = jwt.get_unverified_header(token)
    except Exception:
        raise VigiloOAuthError("Vigilo-tokenets JWT-header er ugyldig") from None
    kid = header.get("kid") if isinstance(header, dict) else None
    if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
        raise VigiloOAuthError("Vigilo-tokenets signaturalgoritme er ugyldig")
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        raise VigiloOAuthError("Vigilo-signaturnøklene har ugyldig format")
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
        raise VigiloOAuthError("Vigilo-tokenets signaturnøkkel samsvarer ikke")
    try:
        public_key = jwt.PyJWK.from_dict(candidates[0]).key
        claims = jwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            leeway=lifecycle.NOT_BEFORE_LEEWAY_SECONDS,
            options={
                "verify_iss": False,
                "verify_aud": False,
                "verify_exp": False,
                "verify_nbf": False,
            },
        )
    except Exception:
        raise VigiloOAuthError("Vigilo-tokenets signatur kunne ikke verifiseres") from None
    if not isinstance(claims, Mapping):
        raise VigiloOAuthError("Vigilo-tokenets claims har ugyldig format")
    return _validate_access_claims(claims, config=config, now=now)


def _validated_token_response(
    value: Mapping[str, Any], previous_refresh: str | None = None
) -> tuple[str, str]:
    access_token = _safe_token(value.get("access_token"), "access-token")
    refresh_value = value.get("refresh_token")
    if refresh_value is None and previous_refresh is not None:
        refresh_token = previous_refresh
    else:
        refresh_token = _safe_token(refresh_value, "refresh-token")
    return access_token, refresh_token


def _safe_call(message: str, operation: Callable[[], Any]) -> Any:
    return safe_call(message, operation, VigiloOAuthError)


def _verify_identity_and_tenant(
    client: OAuthTransport,
    config: ClientConfig,
    access_token: str,
    claims: Mapping[str, Any],
) -> str | None:
    userinfo = _safe_call(
        "Vigilo userinfo kunne ikke bekreftes",
        lambda: client.get_json(USERINFO_ENDPOINT, bearer_token=access_token),
    )
    if not isinstance(userinfo, Mapping) or userinfo.get("sub") != claims.get("sub"):
        raise VigiloOAuthError("Vigilo userinfo samsvarer ikke med tokenet")
    tenant_id = _required_claim(claims, "tenantId")
    tenant_url = config.api_root + "api/tenants/" + urllib.parse.quote(
        tenant_id, safe=""
    )
    tenant = _safe_call(
        "Vigilo-organisasjonen kunne ikke bekreftes",
        lambda: client.get_json(
            tenant_url,
            bearer_token=access_token,
            headers={"appVersion": config.app_version},
        ),
    )
    if not isinstance(tenant, Mapping) or tenant.get("id") != tenant_id:
        raise VigiloOAuthError("Vigilo-organisasjonen samsvarer ikke med tokenet")
    name = tenant.get("name")
    return name if isinstance(name, str) and name else None


def _new_transaction_id() -> str:
    return base64url(secrets.token_bytes(32))


def _write_marker(
    root: Path | str | None,
    reason: str,
    *,
    at: float,
    transaction_id: str | None = None,
) -> None:
    value: dict[str, Any] = {
        "version": 1,
        "reason": reason,
        "recorded_at": utc_iso(at),
        "marker_id": _new_transaction_id(),
    }
    if transaction_id is not None:
        value["transaction_id"] = transaction_id
    try:
        atomic_write_private_json(refresh_block_path(root), value)
    except PrivateStateError as exc:
        raise VigiloOAuthError(str(exc)) from None


def _marker_state(root: Path | str | None) -> tuple[str | None, str | None]:
    path = refresh_block_path(root)
    if not path.exists() and not path.is_symlink():
        return None, None
    try:
        value = _read_mapping(path, "Vigilo-refreshmarkøren mangler")
    except VigiloOAuthError:
        return "unknown", None
    reason = value.get("reason")
    if reason not in {
        "refresh-in-flight",
        "refresh-completed",
        "invalid-grant",
        "fatal-client-configuration",
    }:
        return "unknown", None
    transaction_id = value.get("transaction_id")
    if not isinstance(transaction_id, str):
        transaction_id = None
    return str(reason), transaction_id


def _marker_reason(root: Path | str | None) -> str | None:
    return _marker_state(root)[0]


def _session_completes_marker(
    session: Mapping[str, Any], reason: str | None, transaction_id: str | None
) -> bool:
    current = session.get("refresh_transaction_id")
    return (
        reason in {"refresh-in-flight", "refresh-completed"}
        and isinstance(current, str)
        and isinstance(transaction_id, str)
        and hmac.compare_digest(current.encode("ascii"), transaction_id.encode("ascii"))
    )


def _parse_session(
    root: Path | str | None, value: Mapping[str, Any]
) -> dict[str, Any]:
    config = load_client_config(root)
    fingerprint = value.get("client_fingerprint")
    if not _fingerprint_matches_config(fingerprint, config):
        raise VigiloOAuthError("Vigilo-øktens klientbinding samsvarer ikke")
    session = {
        "access_token": _safe_token(value.get("access_token"), "access-token"),
        "refresh_token": _safe_token(value.get("refresh_token"), "refresh-token"),
        "subject": _required_text(value.get("subject"), "subject"),
        "person_id": _required_text(value.get("person_id"), "person-ID"),
        "tenant_id": _required_text(value.get("tenant_id"), "tenant-ID"),
        "issued_at": _number(value.get("issued_at"), "issued_at", minimum=1),
        "expires_at": _number(value.get("expires_at"), "expires_at", minimum=1),
        "client_fingerprint": fingerprint,
        "api_root": config.api_root,
        "app_version": config.app_version,
        "attachment_hosts": list(config.attachment_hosts),
    }
    if isinstance(value.get("tenant_name"), str):
        session["tenant_name"] = value["tenant_name"]
    if isinstance(value.get("refresh_transaction_id"), str):
        session["refresh_transaction_id"] = value["refresh_transaction_id"]
    if isinstance(value.get("native_handoff_id"), str):
        session["native_handoff_id"] = value["native_handoff_id"]
    return session


def _read_session(root: Path | str | None) -> dict[str, Any]:
    value = _read_mapping(session_path(root), "Vigilo-økten mangler")
    return _parse_session(root, value)


class VigiloRefreshUncertain(SourceUnavailable, VigiloOAuthError):
    pass


def local_session_for_use(root: Path | str | None) -> dict[str, Any]:
    if not consent_status(root)["current"]:
        raise VigiloReauthenticationRequired("Vigilo-samtykke mangler")
    reason, transaction_id = _marker_state(root)
    try:
        session = _read_session(root)
    except VigiloOAuthError:
        raise VigiloReauthenticationRequired(
            "Vigilo-økten mangler eller er ugyldig"
        ) from None
    if reason is not None and not _session_completes_marker(
        session, reason, transaction_id
    ):
        if reason == "fatal-client-configuration":
            raise VigiloFatalConfigurationError(
                "Vigilo-appregistreringen er avvist"
            )
        if reason == 'invalid-grant':
            raise VigiloReauthenticationRequired('Vigilo-refresh-tokenet er avvist')
        raise VigiloRefreshUncertain(
            "En tidligere Vigilo-refresh må avklares"
        )
    return session


def _validate_remote_token(
    client: OAuthTransport,
    config: ClientConfig,
    access_token: str,
    validator: AccessTokenValidator,
    validation_time: float,
    *, verify_remote: bool = True,
) -> tuple[Mapping[str, Any], str | None]:
    jwks = _safe_call(
        "Vigilo-signaturnøklene kunne ikke hentes",
        lambda: client.get_json(JWKS_URI),
    )
    if not isinstance(jwks, Mapping):
        raise VigiloOAuthError("Vigilo returnerte ugyldige signaturnøkler")
    validation_error: str | None = None
    claims: Any = None
    try:
        claims = validator(access_token, jwks, config, validation_time)
    except VigiloOAuthError as exc:
        # Only our own fixed, secret-free validation categories may cross this
        # boundary. Arbitrary injected exceptions remain fully redacted.
        validation_error = str(exc)
    except Exception:
        validation_error = "Vigilo-tokenet kunne ikke verifiseres"
    if validation_error is not None:
        # Raise outside the except suite so a hostile injected exception cannot
        # remain reachable through __context__.
        raise VigiloOAuthError(validation_error) from None
    if not isinstance(claims, Mapping):
        raise VigiloOAuthError("Vigilo-tokenet kunne ikke verifiseres")
    claims = _validate_access_claims(
        claims, config=config, now=validation_time
    )
    tenant_name = _verify_identity_and_tenant(
        client, config, access_token, claims
    ) if verify_remote else None
    return claims, tenant_name


def _session_value(
    config: ClientConfig,
    access_token: str,
    refresh_token: str,
    claims: Mapping[str, Any],
    *,
    issued_at: float,
    tenant_name: str | None,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "subject": _required_claim(claims, "sub"),
        "person_id": _required_claim(claims, "personId"),
        "tenant_id": _required_claim(claims, "tenantId"),
        "issued_at": issued_at,
        "expires_at": _number(claims.get("exp"), "expiry", minimum=1),
        "client_fingerprint": _config_fingerprint(config),
    }
    if tenant_name is not None:
        value["tenant_name"] = tenant_name
    if transaction_id is not None:
        value["refresh_transaction_id"] = transaction_id
    return value


@lifecycle.operation('vigilo', 'login')
def login_with_callback(
    root: Path | str | None,
    transaction: LoginTransaction,
    callback_url: str,
    *,
    transport: OAuthTransport | None = None,
    access_token_validator: AccessTokenValidator | None = None,
    now: float | None = None,
    wall_time: float | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Exchange exactly one matched callback, validate, then persist."""

    with private_source_lock(root, SOURCE):
        current_config = load_client_config(root)
        if not _same_config(current_config, transaction.config):
            raise VigiloOAuthError(
                "Vigilo-klientkonfigurasjonen ble endret under innloggingen"
            )
        target = session_path(root)
        if (
            not transaction.consumed
            and (target.exists() or target.is_symlink())
            and not replace_existing
        ):
            raise VigiloOAuthError(
                "En Vigilo-økt finnes allerede; bekreft uttrykkelig erstatning"
            )
        code = _consume_callback(transaction, callback_url, now=now)
        client = transport or _default_transport(current_config)
        client = lifecycle.observe_transport(root, 'vigilo', client, at=wall_time)
        # Preserve the verified Retrofit field order from the Android release.
        fields = {
            "code": code,
            "redirect_uri": current_config.redirect_uri,
            "grant_type": "authorization_code",
        }
        try:
            response = client.post_form(
                TOKEN_ENDPOINT,
                fields,
                headers={"Authorization": _basic_auth(current_config)},
            )
        except ApiHTTPError as exc:
            if exc.error_code in {
                "invalid_client",
                "unauthorized_client",
                "invalid_scope",
                "invalid_request",
            }:
                _write_marker(
                    root, "fatal-client-configuration", at=time.time()
                )
                raise VigiloFatalConfigurationError(
                    "Vigilo avviste appregistreringen"
                ) from None
            if exc.error_code == "invalid_grant":
                raise VigiloOAuthError(
                    "Vigilo avviste autorisasjonskoden (invalid_grant)"
                ) from None
            raise VigiloOAuthError(
                f"Vigilo-tokenutvekslingen ble avvist (HTTP {exc.status_code})"
            ) from None
        except ApiTransportError:
            raise VigiloOAuthError(
                "Vigilo-tokenendepunktet kunne ikke nås eller tolkes"
            ) from None
        except Exception:
            raise VigiloOAuthError(
                "Vigilo-tokenutvekslingen fikk et ukjent resultat"
            ) from None
        if not isinstance(response, Mapping):
            raise VigiloOAuthError("Vigilo returnerte en ugyldig tokenrespons")
        access_token, refresh_token = _validated_token_response(response)
        validation_time = time.time() if wall_time is None else wall_time
        if (
            isinstance(validation_time, bool)
            or not isinstance(validation_time, (int, float))
            or not math.isfinite(validation_time)
            or validation_time <= 0
        ):
            raise VigiloOAuthError("Ugyldig tidspunkt for Vigilo-tokenvalidering")
        validator = access_token_validator or _pyjwt_access_token_validator
        claims, tenant_name = _validate_remote_token(
            client,
            current_config,
            access_token,
            validator,
            float(validation_time),
        )
        commit_transaction_id = _new_transaction_id()
        # Establish a durable correlation before replacing any older grant.
        # A crash after the session commit is then distinguishable from a lost
        # token response without storing additional secrets in the marker.
        _write_marker(
            root,
            "refresh-in-flight",
            at=float(validation_time),
            transaction_id=commit_transaction_id,
        )
        value = _session_value(
            current_config,
            access_token,
            refresh_token,
            claims,
            issued_at=float(validation_time),
            tenant_name=tenant_name,
            transaction_id=commit_transaction_id,
        )
        try:
            atomic_write_private_json(target, value)
        except PrivateStateError as exc:
            raise VigiloOAuthError(str(exc)) from None
        marker_cleared = False
        try:
            _write_marker(
                root,
                "refresh-completed",
                at=float(validation_time),
                transaction_id=commit_transaction_id,
            )
            marker_cleared = remove_private_file(refresh_block_path(root))
        except (VigiloOAuthError, PrivateStateError):
            # The session transaction ID also completes the earlier in-flight
            # marker, so cleanup debt cannot make a verified grant unsafe.
            marker_cleared = False
        result = {
            "authenticated": True,
            "expires_at": utc_iso(float(value["expires_at"])),
            "access_token_verified": True,
            "userinfo_verified": True,
            "tenant_verified": True,
        }
        if not marker_cleared:
            result["refresh_marker_cleanup_pending"] = True
        return result


@lifecycle.operation('vigilo', 'import')
def import_native_grant(root, payload, *, handoff_id, replace_existing=False,
                        transport=None, access_token_validator=None, wall_time=None):
    """Import a grant read from a stopped Vigilo app. No refresh POST, no raw result.

    The caller must stop the Android app before reading its database. The
    persisted handover ID makes a lost acknowledgement harmless, even after the
    provider has rotated the tokens.
    """
    if not isinstance(handoff_id, str) or not re.fullmatch(r"[a-f0-9]{32}", handoff_id):
        raise VigiloOAuthError("Ugyldig tokenoverlevering")
    if not consent_status(root)["current"]:
        raise VigiloOAuthError("Vigilo-samtykke mangler")
    target = session_path(root)
    if target.exists():
        existing = _read_mapping(target, "Vigilo-økten mangler")
        if existing.get("native_handoff_id") == handoff_id:
            return {"authenticated": True, "already_imported": True}
        if not replace_existing:
            raise VigiloOAuthError("Bekreft erstatning av eksisterende økt")
    config = load_client_config(root)
    access, refresh = _validated_token_response(payload)
    instant = time.time() if wall_time is None else wall_time
    claims, name = _validate_remote_token(transport or _default_transport(config), config, access,
        access_token_validator or _pyjwt_access_token_validator, instant)
    transaction = _new_transaction_id()
    value = _session_value(config, access, refresh, claims, issued_at=instant, tenant_name=name,
                           transaction_id=transaction)
    value["native_handoff_id"] = handoff_id
    # An existing grant is replaced only after all validation has passed.
    _write_marker(root, "refresh-in-flight", at=instant, transaction_id=transaction)
    atomic_write_private_json(target, value)
    remove_private_file(refresh_block_path(root), missing_ok=True)
    return {"authenticated": True, "access_token_verified": True,
            "userinfo_verified": True, "tenant_verified": True}


@lifecycle.operation('vigilo', 'refresh')
def refresh_session(
    root: Path | str | None,
    *,
    transport: OAuthTransport | None = None,
    access_token_validator: AccessTokenValidator | None = None,
    at: float | None = None,
) -> dict[str, Any]:
    """Rotate once; unknown outcomes remain durably blocked."""

    with private_source_lock(root, SOURCE):
        if not consent_status(root)["current"]:
            raise VigiloReauthenticationRequired("Vigilo-samtykke mangler")
        session = _read_session(root)
        reason, old_transaction = _marker_state(root)
        saved = lifecycle.saved_response(root, SOURCE, session, old_transaction) if reason == 'refresh-in-flight' else None
        if saved is None:
            session = local_session_for_use(root)
        config = load_client_config(root)
        validation_time = time.time() if at is None else at
        if (
            isinstance(validation_time, bool)
            or not isinstance(validation_time, (int, float))
            or not math.isfinite(validation_time)
            or validation_time <= 0
        ):
            raise VigiloOAuthError("Ugyldig tidspunkt for Vigilo-refresh")
        validation_time = float(validation_time)
        requested_at = validation_time
        if saved is not None:
            transaction_id = old_transaction
            response = saved['response']
            validation_time = lifecycle.recovery_validation_time(response, saved['received_at'], requested_at)
            client = lifecycle.observe_transport(root, SOURCE, transport or _default_transport(config), at=validation_time, capture_response=False)
            lifecycle.record(root, SOURCE, 'response_recovered', at=time.time() if at is None else at)
        else:
            transaction_id = _new_transaction_id()
            _write_marker(
                root,
                "refresh-in-flight",
                at=validation_time,
                transaction_id=transaction_id,
            )
            client = lifecycle.observe_transport(root, SOURCE, transport or _default_transport(config), at=validation_time, capture_response=False)
            try:
                response = client.post_form(
                    TOKEN_ENDPOINT,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": str(session["refresh_token"]),
                    },
                    headers={"Authorization": _basic_auth(config)},
                )
            except ApiRequestNotSent:
                remove_private_file(refresh_block_path(root))
                raise VigiloRefreshUncertain('Fornyelsesforespørselen ble ikke sendt; kan forsøkes ved neste runde') from None
            except ApiHTTPError as exc:
                if exc.error_code in {
                    "invalid_client",
                    "unauthorized_client",
                    "invalid_scope",
                    "invalid_request",
                }:
                    _write_marker(
                        root, "fatal-client-configuration", at=validation_time
                    )
                    raise VigiloFatalConfigurationError(
                        "Vigilo avviste appregistreringen"
                    ) from None
                if exc.error_code == 'invalid_grant':
                    _write_marker(root, 'invalid-grant', at=validation_time)
                    raise VigiloReauthenticationRequired('Vigilo-refresh-tokenet er avvist') from None
                raise VigiloRefreshUncertain('Vigilo-fornyelsen har ukjent utfall') from None
            except Exception:
                raise VigiloRefreshUncertain(
                    "Vigilo-refresh fikk ukjent resultat"
                ) from None
            if not isinstance(response, Mapping):
                raise VigiloReauthenticationRequired(
                    "Vigilo-refresh returnerte ugyldige data"
                )
            validation_time = time.time() if at is None else float(at)
            lifecycle.save_response(root, SOURCE, session, transaction_id, response, at=validation_time)
        access_token, refresh_token = _validated_token_response(
            response, str(session["refresh_token"])
        )
        validator = access_token_validator or _pyjwt_access_token_validator
        try:
            claims, tenant_name = _validate_remote_token(
                client,
                config,
                access_token,
                validator,
                validation_time,
                verify_remote=saved is None or requested_at <= saved['received_at'] + 60,
            )
            if not hmac.compare_digest(
                _required_claim(claims, "sub").encode("utf-8"),
                str(session["subject"]).encode("utf-8"),
            ) or not hmac.compare_digest(
                _required_claim(claims, "personId").encode("utf-8"),
                str(session["person_id"]).encode("utf-8"),
            ) or not hmac.compare_digest(
                _required_claim(claims, "tenantId").encode("utf-8"),
                str(session["tenant_id"]).encode("utf-8"),
            ):
                raise VigiloReauthenticationRequired(
                    "Den fornyede Vigilo-økten har en annen identitetsbinding"
                )
            value = _session_value(
                config,
                access_token,
                refresh_token,
                claims,
                issued_at=validation_time,
                tenant_name=tenant_name,
                transaction_id=transaction_id,
            )
            if session.get("native_handoff_id"):
                value["native_handoff_id"] = session["native_handoff_id"]
            atomic_write_private_json(session_path(root), value)
        except VigiloReauthenticationRequired:
            raise
        except (VigiloOAuthError, PrivateStateError):
            raise VigiloRefreshUncertain(
                "Vigilo-refresh kunne ikke fullvalideres; svaret er lagret"
            ) from None
        lifecycle.clear_response(root, SOURCE)
        marker_cleared = False
        try:
            _write_marker(
                root,
                "refresh-completed",
                at=validation_time,
                transaction_id=transaction_id,
            )
            marker_cleared = remove_private_file(refresh_block_path(root))
        except (VigiloOAuthError, PrivateStateError):
            # A matching in-flight marker plus the committed transaction ID is
            # sufficient recovery evidence; cleanup can be retried later.
            marker_cleared = False
        if saved is not None and float(value['expires_at']) <= requested_at + 60:
            return refresh_session(root, transport=transport, access_token_validator=access_token_validator, at=at)
        return {
            "refreshed": True,
            "refresh_token_rotated": not hmac.compare_digest(
                str(session["refresh_token"]).encode("utf-8"),
                refresh_token.encode("utf-8"),
            ),
            "access_expires_at": utc_iso(float(value["expires_at"])),
            "access_token_verified": True,
            "userinfo_verified": saved is None or requested_at <= saved['received_at'] + 60,
            "tenant_verified": saved is None or requested_at <= saved['received_at'] + 60,
            "refresh_marker_cleanup_pending": not marker_cleared,
        }


def auth_status(
    root: Path | str | None, *, at: float | None = None
) -> dict[str, Any]:
    configured = False
    try:
        load_client_config(root)
        configured = True
    except VigiloOAuthError:
        pass
    consent = consent_status(root)
    authenticated = False
    expires_at: str | None = None
    expired: bool | None = None
    tenant_bound = False
    session: dict[str, Any] | None = None
    try:
        session = _read_session(root)
        current = time.time() if at is None else at
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(current)
        ):
            raise VigiloOAuthError("Ugyldig tidspunkt for Vigilo-status")
        expiry = float(session["expires_at"])
        authenticated = consent["current"]
        expires_at = utc_iso(expiry)
        expired = expiry <= float(current)
        tenant_bound = bool(session.get("tenant_id"))
    except VigiloOAuthError:
        pass
    reason, transaction_id = _marker_state(root)
    effective_reason = reason
    if session is not None and _session_completes_marker(
        session, reason, transaction_id
    ):
        effective_reason = "refresh-completed"
    return {
        "configured": configured,
        "consent_recorded": consent["current"],
        "authenticated": authenticated,
        "session_present": session is not None,
        "access_expires_at": expires_at,
        "expires_at": expires_at,
        "access_expired": expired,
        "tenant_bound": tenant_bound,
        "refresh_blocked": effective_reason not in {None, "refresh-completed"},
        "refresh_block_reason": effective_reason,
    }


@lifecycle.operation('vigilo', 'logout')
def logout(
    root: Path | str | None, *, transport: OAuthTransport | None = None
) -> dict[str, Any]:
    """Delete local grant first, then best-effort revoke without leaking bodies."""

    with private_source_lock(root, SOURCE, wait=True):
        raw: Mapping[str, Any] | None = None
        target = session_path(root)
        if target.exists() and not target.is_symlink():
            try:
                raw = _read_mapping(target, "Vigilo-økten mangler")
            except VigiloOAuthError:
                raw = None
        try:
            had_session = remove_private_file(target)
            remove_private_file(refresh_block_path(root))
        except PrivateStateError as exc:
            raise VigiloOAuthError(str(exc)) from None
        attempts = 0
        successes = 0
        config: ClientConfig | None = None
        try:
            config = load_client_config(root)
        except VigiloOAuthError:
            pass
        if raw is not None and config is not None:
            client = transport or _default_transport(config)
            for field_name, hint in (
                ("refresh_token", "refresh_token"),
                ("access_token", "access_token"),
            ):
                try:
                    token = _safe_token(raw.get(field_name), field_name)
                except VigiloOAuthError:
                    continue
                attempts += 1
                try:
                    client.post_form(
                        REVOCATION_ENDPOINT,
                        {"token": token, "token_type_hint": hint},
                        headers={"Authorization": _basic_auth(config)},
                    )
                except Exception:
                    continue
                successes += 1
        return {
            "local_session_removed": had_session,
            "revocation_attempts": attempts,
            "revocation_successes": successes,
            "remote_revocation_complete": (not had_session)
            or (attempts > 0 and attempts == successes),
        }


def access_token_for_use(
    root: Path | str | None,
    *,
    now: float | None = None,
    transport: OAuthTransport | None = None,
    access_token_validator: AccessTokenValidator | None = None,
) -> tuple[str, bool]:
    """Return the token and whether this call already consumed the refresh budget."""

    session = local_session_for_use(root)
    refreshed = False
    current = time.time() if now is None else now
    if (
        isinstance(current, bool)
        or not isinstance(current, (int, float))
        or not math.isfinite(current)
    ):
        raise VigiloOAuthError("Ugyldig tidspunkt for Vigilo-tokenbruk")
    if float(session["expires_at"]) <= float(current) + REFRESH_SKEW_SECONDS:
        refresh_session(
            root,
            transport=transport,
            access_token_validator=access_token_validator,
            at=now,
        )
        session = local_session_for_use(root)
        refreshed = True
    return str(session["access_token"]), refreshed


def access_token(
    root: Path | str | None,
    *,
    now: float | None = None,
    transport: OAuthTransport | None = None,
    access_token_validator: AccessTokenValidator | None = None,
) -> str:
    """Backward-compatible token-only accessor."""

    return access_token_for_use(
        root,
        now=now,
        transport=transport,
        access_token_validator=access_token_validator,
    )[0]
