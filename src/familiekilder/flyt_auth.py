"""Visma Connect login and local session for Flyt Foresatt (barnehage and school).

The flow mirrors the official Flyt Foresatt app: the parent chooses a
municipality, logs in through Visma Connect (which hands over to ID-porten)
with Authorization Code + PKCE, and the app then opens a guardian session with
``POST v2/session``. Every guardian call carries the Visma Connect bearer token
and the municipality's ``customerName`` as ``Customer-Key``.

No function prints or logs. Printable results never contain tokens, codes,
states, verifiers, device IDs or the account subject. ``local_session_for_use``
is the only function that returns secrets, and only to the read client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from . import auth_lifecycle as lifecycle
from .api_transport import ApiHTTPError, ApiRequestNotSent, ApiTransport, ApiTransportError
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


SOURCE = "flyt"
ISSUER = "https://connect.visma.com"
CONNECT_BASE_URL = f"{ISSUER}/connect"
AUTHORIZATION_ENDPOINT = f"{CONNECT_BASE_URL}/authorize"
TOKEN_ENDPOINT = f"{CONNECT_BASE_URL}/token"
CLIENT_ID = "min-skole-guardian-mobile-app"
REDIRECT_URI = "com.visma.vfsparentapp://redirect"
SCOPES = (
    "openid",
    "email",
    "profile",
    "min-skole-guardian-api:all",
    "offline_access",
    "nnin",
    "flyt-skole-guardian-api:all",
    "flyt-guardian-web-api:all",
)
ACR_VALUES = "urn:idp:id-porten"
API_HOST = "api.guardian.mobile.skole.visma.no"
API_BASE_URL = f"https://{API_HOST}"
MUNICIPALITIES_ENDPOINT = f"{API_BASE_URL}/v1/municipalities"
SESSION_ENDPOINT = f"{API_BASE_URL}/v2/session"
# The app sends ``osType`` with its default value; the enum is not documented.
OS_TYPE = 1
ACCEPT_LANGUAGE = "nb-NO"

CLIENT_FILE = "flyt-client.json"
SESSION_FILE = "flyt-session.json"
DEVICE_FILE = "flyt-device.json"
CONSENT_FILE = "flyt-oauth-consent.json"
REFRESH_BLOCK_FILE = "flyt-refresh-blocked.json"
CONSENT_VERSION = "unofficial-flyt-visma-connect-v1"
CALLBACK_LIFETIME_SECONDS = 10 * 60
MAX_PRIVATE_JSON_BYTES = 1024 * 1024
MAX_MUNICIPALITIES = 2000

_ORGANIZATION_NUMBER = re.compile(r"[0-9]{9}")
_KEY = re.compile(r"[A-Za-z0-9._-]{1,128}")
_SAFE_OAUTH_ERRORS = frozenset(
    {
        "invalid_client",
        "unauthorized_client",
        "invalid_scope",
        "invalid_request",
        "invalid_grant",
    }
)
_FATAL_OAUTH_ERRORS = frozenset(
    {"invalid_client", "unauthorized_client", "invalid_scope", "invalid_request"}
)


class FlytAuthError(FamiliekilderError):
    """Sanitized local or protocol failure."""


class FlytReauthenticationRequired(SourceLoginRequired, FlytAuthError):
    """The grant cannot be used without a new Visma Connect login."""


class FlytRefreshUncertain(SourceUnavailable, FlytAuthError):
    """The refresh outcome is unknown, which is not proof of an expired grant."""


class FlytFatalConfigurationError(SourceFatalConfigurationError, FlytAuthError):
    """Visma Connect rejected the app registration itself."""


class AuthTransport(Protocol):
    def get_json(self, url: str, **kwargs: Any) -> Any: ...

    def post_json(self, url: str, payload: Mapping[str, Any], **kwargs: Any) -> Any: ...

    def post_form(self, url: str, fields: Mapping[str, str], **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class Municipality:
    """One entry of the public municipality list."""

    organization_number: str
    customer_name: str
    display_name: str
    client_context: str

    def as_dict(self) -> dict[str, str]:
        return {
            "organization_number": self.organization_number,
            "customer_name": self.customer_name,
            "display_name": self.display_name,
            "client_context": self.client_context,
        }


@dataclass(frozen=True)
class ClientConfig:
    municipality: Municipality
    client_id: str = CLIENT_ID
    redirect_uri: str = REDIRECT_URI

    @property
    def customer_key(self) -> str:
        return self.municipality.customer_name

    @property
    def on_behalf_of(self) -> str:
        return f"vismaflytskole-{self.municipality.organization_number}"

    def as_private_json(self) -> dict[str, Any]:
        return {
            **self.municipality.as_dict(),
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
        }


@dataclass(repr=False)
class LoginTransaction:
    """Single-use login state; it stays in memory and out of logs."""

    config: ClientConfig = field(repr=False)
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)
    device_unique_id: str = field(repr=False)
    created_at: float
    _consumed: bool = field(default=False, init=False, repr=False)

    @property
    def consumed(self) -> bool:
        return self._consumed

    def __repr__(self) -> str:
        return f"LoginTransaction(created_at={self.created_at!r}, consumed={self._consumed!r})"


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


def default_transport() -> ApiTransport:
    return ApiTransport(
        allowed_urls={TOKEN_ENDPOINT, MUNICIPALITIES_ENDPOINT, SESSION_ENDPOINT},
        safe_error_codes=_SAFE_OAUTH_ERRORS,
        max_json_bytes=4 * 1024 * 1024,
        user_agent="familiekilder-flyt/0.1",
    )


def guardian_headers(customer_key: str, session_id: str | None = None) -> dict[str, str]:
    """Headers the app adds to every guardian API call besides the bearer token."""

    headers = {"Customer-Key": customer_key, "Accept-Language": ACCEPT_LANGUAGE}
    if session_id:
        headers["Session-Id"] = session_id
    return headers


def _text(value: Any, field_name: str, *, maximum: int = 8192) -> str:
    if not is_plain_text(value, maximum=maximum):
        raise FlytAuthError(f"{field_name} har ugyldig format")
    return value


def _token(value: Any, field_name: str) -> str:
    if not is_plain_text(value, maximum=64 * 1024, allow_space=False):
        raise FlytAuthError(f"{field_name} har ugyldig format")
    return value


def _key(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise FlytAuthError(f"{field_name} har ugyldig format")
    return value


def _number(value: Any, message: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise FlytAuthError(message)
    return float(value)


# Municipalities ------------------------------------------------------------


def parse_municipality(value: Any) -> Municipality:
    """Validate one entry from ``v1/municipalities`` or a stored client file."""

    if not isinstance(value, Mapping):
        raise FlytAuthError("Kommuneoppføringen har ugyldig format")
    number = value.get("organizationNumber", value.get("organization_number"))
    if not isinstance(number, str) or not _ORGANIZATION_NUMBER.fullmatch(number):
        raise FlytAuthError("Kommunens organisasjonsnummer har ugyldig format")
    customer = _key(value.get("customerName", value.get("customer_name")), "Kommunens kundenavn")
    display = _text(
        value.get("displayName", value.get("display_name")), "Kommunenavnet", maximum=200
    )
    context = value.get("vismaConnectClientContext", value.get("client_context"))
    # Newer app versions read the context from the list; older ones built it
    # from the organization number, which differs for some municipalities.
    context = _key(context, "Kommunens innloggingskontekst") if context else f"VismaConnect{number}"
    return Municipality(number, customer, display, context)


def parse_municipalities(value: Any) -> list[Municipality]:
    if not isinstance(value, list) or len(value) > MAX_MUNICIPALITIES:
        raise FlytAuthError("Kommunelisten fra Flyt har ugyldig format")
    result = []
    for entry in value:
        try:
            result.append(parse_municipality(entry))
        except FlytAuthError:
            continue
    if not result:
        raise FlytAuthError("Kommunelisten fra Flyt var tom")
    return sorted(result, key=lambda item: (item.display_name.casefold(), item.organization_number))


def list_municipalities(*, transport: AuthTransport | None = None) -> list[dict[str, str]]:
    """The public list a host UI can show; one unauthenticated read."""

    return [item.as_dict() for item in _fetch_municipalities(transport)]


def _fetch_municipalities(transport: AuthTransport | None) -> list[Municipality]:
    client = transport or default_transport()
    try:
        value = client.get_json(
            MUNICIPALITIES_ENDPOINT, headers={"Accept-Language": ACCEPT_LANGUAGE}
        )
    except FlytAuthError:
        raise
    except Exception:
        raise SourceUnavailable("Kommunelisten fra Flyt kunne ikke hentes") from None
    return parse_municipalities(value)


def _comparable(value: str) -> str:
    text = " ".join(value.casefold().split())
    return text.removesuffix(" kommune")


def select_municipality(municipalities: list[Municipality], query: str) -> Municipality:
    """Pick one municipality by organization number or name, or explain why not."""

    if not is_plain_text(query, maximum=200) or not query.strip():
        raise FlytAuthError("Oppgi kommunens navn eller organisasjonsnummer")
    wanted = query.strip()
    if _ORGANIZATION_NUMBER.fullmatch(wanted):
        matches = [m for m in municipalities if m.organization_number == wanted]
    else:
        target = _comparable(wanted)
        matches = [
            m
            for m in municipalities
            if target in {_comparable(m.display_name), _comparable(m.customer_name)}
        ]
        if not matches:
            matches = [m for m in municipalities if target in _comparable(m.display_name)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FlytAuthError(f"Fant ingen kommune i Flyt som passer «{wanted}»")
    names = ", ".join(f"{m.display_name} ({m.organization_number})" for m in matches[:8])
    more = " med flere" if len(matches) > 8 else ""
    raise FlytAuthError(f"Flere kommuner passer: {names}{more}. Bruk organisasjonsnummeret.")


# Client configuration -----------------------------------------------------


def _parse_client_config(value: Any) -> ClientConfig:
    if not isinstance(value, Mapping):
        raise FlytAuthError("Flyt-klientkonfigurasjonen har ugyldig format")
    if (
        value.get("client_id", CLIENT_ID) != CLIENT_ID
        or value.get("redirect_uri", REDIRECT_URI) != REDIRECT_URI
    ):
        raise FlytAuthError("Flyt-klientkonfigurasjonen bruker feil klient eller redirect-URI")
    return ClientConfig(parse_municipality(value))


def load_client_config(root: Path | str | None) -> ClientConfig:
    try:
        value = read_private_json(
            client_config_path(root),
            max_bytes=MAX_PRIVATE_JSON_BYTES,
            missing_message="Flyt-klientkonfigurasjonen mangler",
        )
    except PrivateStateError:
        raise FlytAuthError(
            "Flyt er ikke satt opp; velg kommune med «auth flyt configure --client-file»"
        ) from None
    return _parse_client_config(value)


def _store_client_config(root: Path | str | None, config: ClientConfig) -> dict[str, Any]:
    with private_source_lock(root, SOURCE):
        if session_path(root).exists():
            current = load_client_config(root)
            if current.as_private_json() != config.as_private_json():
                raise FlytAuthError("Logg ut av Flyt før du bytter kommune")
        atomic_write_private_json(client_config_path(root), config.as_private_json())
        marker = _read_refresh_block(root)
        if marker is not None and marker.get("reason") == "fatal-client-configuration":
            remove_private_file(refresh_block_path(root))
    return {
        "configured": True,
        "municipality": config.municipality.display_name,
        "organization_number": config.municipality.organization_number,
        "redirect_uri": config.redirect_uri,
    }


def configure_client(
    root: Path | str | None, client_file: Path | str, *, transport: AuthTransport | None = None
) -> dict[str, Any]:
    """Import a private JSON file that names the municipality.

    The file holds either ``{"municipality": "<name or organization number>"}``,
    which is looked up in the public list, or one full entry from that list.
    """

    try:
        raw = read_private_bytes(
            Path(client_file).expanduser(),
            max_bytes=MAX_PRIVATE_JSON_BYTES,
            missing_message="Fant ikke klientkonfigurasjonsfilen",
        )
        value = json.loads(raw)
    except (PrivateStateError, UnicodeDecodeError, json.JSONDecodeError):
        raise FlytAuthError("Klientkonfigurasjonsfilen mangler eller har ugyldig format") from None
    if isinstance(value, Mapping) and set(value) == {"municipality"}:
        return configure_municipality(root, value["municipality"], transport=transport)
    return _store_client_config(root, _parse_client_config(value))


def configure_municipality(
    root: Path | str | None, query: str, *, transport: AuthTransport | None = None
) -> dict[str, Any]:
    """Choose the municipality by organization number or name and store it."""

    municipality = select_municipality(_fetch_municipalities(transport), query)
    return _store_client_config(root, ClientConfig(municipality))


# Consent and device -------------------------------------------------------


def consent_status(root: Path | str | None) -> dict[str, Any]:
    try:
        value = read_private_json(
            consent_path(root), max_bytes=4096, missing_message="Samtykkemarkøren mangler"
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
    root: Path | str | None, *, version: str = CONSENT_VERSION, at: float | None = None
) -> dict[str, Any]:
    if version != CONSENT_VERSION:
        raise FlytAuthError("Ukjent samtykkeversjon")
    recorded_at = utc_iso(
        _number(time.time() if at is None else at, "Ugyldig tidspunkt for samtykke")
    )
    with private_source_lock(root, SOURCE):
        atomic_write_private_json(
            consent_path(root), {"version": version, "recorded_at": recorded_at}
        )
    return {"recorded": True, "current": True, "version": version, "recorded_at": recorded_at}


def _random(random_bytes: Callable[[int], bytes]) -> str:
    return random_urlsafe(random_bytes, FlytAuthError)


def pkce_challenge(code_verifier: str) -> str:
    if not isinstance(code_verifier, str) or not 43 <= len(code_verifier) <= 128:
        raise FlytAuthError("PKCE-verifier har ugyldig format")
    return base64url(hashlib.sha256(code_verifier.encode("ascii")).digest())


def _device_id(root: Path | str | None, random_bytes: Callable[[int], bytes]) -> str:
    """Create the stable device ID once; the app sends its installation ID."""

    target = device_path(root)
    if not target.exists() and not target.is_symlink():
        identifier = _random(random_bytes)[:22]
        atomic_write_private_json(target, {"device_unique_id": identifier})
        return identifier
    return _read_device_id(root)


def _read_device_id(root: Path | str | None) -> str:
    try:
        value = read_private_json(
            device_path(root), max_bytes=4096, missing_message="Enhets-ID mangler"
        )
    except PrivateStateError:
        raise FlytAuthError("Flyt-enhets-ID-en kan ikke leses sikkert") from None
    if not isinstance(value, Mapping):
        raise FlytAuthError("Flyt-enhets-ID-en er ugyldig")
    return _key(value.get("device_unique_id"), "Flyt-enhets-ID")


# Login --------------------------------------------------------------------


def begin_login(
    root: Path | str | None,
    *,
    now: float | None = None,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> tuple[str, LoginTransaction]:
    """Return the Visma Connect URL and the secret-bearing transaction."""

    if not consent_status(root)["current"]:
        raise FlytAuthError("Eksplisitt samtykke må registreres før Flyt-innlogging")
    marker = _read_refresh_block(root)
    if marker is not None and marker.get("reason") == "fatal-client-configuration":
        raise FlytFatalConfigurationError("Visma Connect har avvist Flyt-appregistreringen")
    config = load_client_config(root)
    created_at = time.monotonic() if now is None else now
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or not math.isfinite(created_at)
    ):
        raise FlytAuthError("Ugyldig tidspunkt for innlogging")
    with private_source_lock(root, SOURCE):
        device_id = _device_id(root, random_bytes)
    transaction = LoginTransaction(
        config=config,
        state=_random(random_bytes),
        nonce=_random(random_bytes),
        code_verifier=_random(random_bytes),
        device_unique_id=device_id,
        created_at=float(created_at),
    )
    parameters = [
        ("client_id", config.client_id),
        ("redirect_uri", config.redirect_uri),
        ("response_mode", "query"),
        ("response_type", "code"),
        ("scope", " ".join(SCOPES)),
        ("client_context", config.municipality.client_context),
        ("acr_values", ACR_VALUES),
        ("onbehalfof", config.on_behalf_of),
        ("nonce", transaction.nonce),
        ("state", transaction.state),
        ("code_challenge_method", "S256"),
        ("code_challenge", pkce_challenge(transaction.code_verifier)),
        ("prompt", "login"),
        ("max_age", "0"),
    ]
    return f"{AUTHORIZATION_ENDPOINT}?{urllib.parse.urlencode(parameters)}", transaction


def _consume_callback(
    transaction: LoginTransaction, callback_url: str, *, now: float | None
) -> str:
    if not isinstance(transaction, LoginTransaction):
        raise FlytAuthError("OAuth-transaksjonen har ugyldig format")
    if transaction._consumed:
        raise FlytAuthError("OAuth-callbacken er allerede brukt")
    current = time.monotonic() if now is None else now
    if (
        isinstance(current, bool)
        or not isinstance(current, (int, float))
        or not math.isfinite(current)
        or float(current) < transaction.created_at
        or float(current) - transaction.created_at > CALLBACK_LIFETIME_SECONDS
    ):
        raise FlytAuthError("OAuth-callbacken er utløpt; start ny innlogging")
    if not isinstance(callback_url, str) or not callback_url or len(callback_url) > 8192:
        raise FlytAuthError("OAuth-callbacken har ugyldig format")
    try:
        parsed = urllib.parse.urlsplit(callback_url)
        expected = urllib.parse.urlsplit(transaction.config.redirect_uri)
        pairs = urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=12,
        )
    except (UnicodeError, ValueError):
        raise FlytAuthError("OAuth-callbacken har ugyldige parametre") from None
    # A browser or app may add a root slash to the authority-only redirect.
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or ("" if parsed.path == "/" else parsed.path)
        != ("" if expected.path == "/" else expected.path)
        or parsed.fragment
    ):
        raise FlytAuthError("OAuth-callbacken bruker feil redirect-URI")
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)) or "state" not in keys:
        raise FlytAuthError("OAuth-callbacken har ugyldige parametre")
    values = dict(pairs)
    if not hmac.compare_digest(values["state"].encode(), transaction.state.encode()):
        raise FlytAuthError("OAuth-callbackens state samsvarer ikke")
    transaction._consumed = True
    if values.get("error"):
        raise FlytAuthError("Visma Connect avviste eller avbrøt innloggingen")
    issuer = values.get("iss")
    if issuer is not None and not hmac.compare_digest(issuer.encode(), ISSUER.encode()):
        raise FlytAuthError("OAuth-callbackens issuer samsvarer ikke")
    if not values.get("code") or set(keys) - {"state", "code", "session_state", "iss", "scope"}:
        raise FlytAuthError("OAuth-callbacken mangler en gyldig kode")
    return _text(values["code"], "Authorization-koden")


def _claims(token: Any) -> Mapping[str, Any]:
    """Unverified JWT claims, used only for identity binding and expiry hints."""

    if not isinstance(token, str) or token.count(".") != 2 or len(token) > 64 * 1024:
        return {}
    try:
        encoded = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, TypeError, UnicodeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _subject(tokens: Mapping[str, Any]) -> str | None:
    for name in ("id_token", "access_token"):
        sub = _claims(tokens.get(name)).get("sub")
        if is_plain_text(sub, maximum=512):
            # Only a digest is stored: it binds the cache to the account
            # without keeping the provider's account identifier.
            return hashlib.sha256(f"{SOURCE}:{sub}".encode()).hexdigest()
    return None


def _check_login_id_token(value: Mapping[str, Any], nonce: str) -> None:
    """Bind the login's ID token to this app and to this login attempt.

    The claims are read without checking the signature (the token comes
    straight from the token endpoint over TLS); issuer, audience and nonce
    still refuse a token issued for another client or another login.
    """

    claims = _claims(value.get("id_token"))
    if not claims:
        raise FlytReauthenticationRequired(
            "Visma Connect sendte ikke et gyldig ID-token; start innloggingen på nytt"
        )
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or not hmac.compare_digest(issuer.encode(), ISSUER.encode()):
        raise FlytReauthenticationRequired(
            "ID-tokenet kom ikke fra Visma Connect; start innloggingen på nytt"
        )
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if not isinstance(audiences, list) or CLIENT_ID not in audiences:
        raise FlytReauthenticationRequired(
            "ID-tokenet var ikke laget for Flyt-appen; start innloggingen på nytt"
        )
    returned = claims.get("nonce")
    if not isinstance(returned, str) or not hmac.compare_digest(
        returned.encode(), nonce.encode()
    ):
        raise FlytReauthenticationRequired(
            "ID-tokenet hørte til en annen innlogging; start innloggingen på nytt"
        )


def _validated_tokens(value: Any, *, previous_refresh: str | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FlytAuthError("Visma Connect returnerte en ugyldig tokenrespons")
    token_type = value.get("token_type", "Bearer")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise FlytAuthError("Visma Connect returnerte en ukjent tokentype")
    refresh = value.get("refresh_token") or previous_refresh
    result: dict[str, Any] = {
        "access_token": _token(value.get("access_token"), "Access-tokenet"),
        "refresh_token": _token(refresh, "Refresh-tokenet"),
    }
    expires_in = value.get("expires_in")
    if (
        not isinstance(expires_in, bool)
        and isinstance(expires_in, (int, float))
        and math.isfinite(expires_in)
        and 0 < expires_in <= 31_536_000
    ):
        result["expires_in"] = float(expires_in)
    expiry = _claims(result["access_token"]).get("exp")
    if (
        not isinstance(expiry, bool)
        and isinstance(expiry, (int, float))
        and math.isfinite(expiry)
        and expiry > 0
    ):
        result["jwt_exp"] = float(expiry)
    subject = _subject(value)
    if subject is not None:
        result["subject"] = subject
    return result


def _session_json(
    tokens: Mapping[str, Any],
    base: Mapping[str, Any],
    *,
    at: float,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "subject": base["subject"],
        "device_unique_id": base["device_unique_id"],
        "customer_key": base["customer_key"],
        "updated_at": float(at),
    }
    for name in ("session_id", "kindergarten_status", "school_status"):
        if base.get(name) is not None:
            value[name] = base[name]
    expiries = []
    if isinstance(tokens.get("expires_in"), float):
        expiries.append(float(at) + tokens["expires_in"])
    if isinstance(tokens.get("jwt_exp"), float):
        expiries.append(tokens["jwt_exp"])
    if expiries:
        value["access_expires_at"] = min(expiries)
    if transaction_id is not None:
        value["refresh_transaction_id"] = transaction_id
    return value


def _guardian_session(value: Any) -> dict[str, Any]:
    """Read the ``v2/session`` reply: per-system status and an optional ID."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise FlytAuthError("Flyt svarte med en ugyldig økt")
    result: dict[str, Any] = {}
    for name, key in (("kindergarten", "kindergarten_status"), ("school", "school_status")):
        status = value.get(name)
        if isinstance(status, int) and not isinstance(status, bool) and 0 <= status <= 100:
            result[key] = status
    # Live replies (November 2026) hold only ``{"school": 3, "kindergarten": 1}``
    # and no session ID in body or headers; reads work without ``Session-Id``.
    # Keep an ID if a later version starts sending one.
    for name in ("sessionId", "session_id", "id"):
        candidate = value.get(name)
        if isinstance(candidate, str) and _KEY.fullmatch(candidate):
            result["session_id"] = candidate
            break
    return result


@lifecycle.operation(SOURCE, "login")
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
    """Exchange the code, open the guardian session and store it."""

    config = load_client_config(root)
    if config.as_private_json() != transaction.config.as_private_json():
        raise FlytAuthError("Flyt-kommunen ble endret; start innloggingen på nytt")
    if session_path(root).exists() and not replace_existing and not transaction.consumed:
        raise FlytAuthError("En lokal Flyt-økt finnes allerede; bekreft at den skal erstattes")
    code = _consume_callback(transaction, callback_url, now=now)
    client = lifecycle.observe_transport(
        root, SOURCE, transport or default_transport(), at=wall_time
    )
    stage = "token"
    try:
        reply = client.post_form(
            TOKEN_ENDPOINT,
            {
                "client_id": config.client_id,
                "redirect_uri": config.redirect_uri,
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": transaction.code_verifier,
            },
        )
        tokens = _validated_tokens(reply)
        if "subject" not in tokens:
            raise FlytAuthError("Visma Connect returnerte ingen kontoidentitet")
        _check_login_id_token(reply, transaction.nonce)
        stage = "session"
        guardian = _guardian_session(
            client.post_json(
                SESSION_ENDPOINT,
                {"deviceId": transaction.device_unique_id, "osType": OS_TYPE},
                bearer_token=tokens["access_token"],
                headers=guardian_headers(config.customer_key),
                allow_empty=True,
            )
        )
    except FlytAuthError:
        raise
    except Exception as exc:
        if stage == "session":
            if isinstance(exc, ApiHTTPError) and exc.status_code in {401, 403}:
                raise FlytReauthenticationRequired(
                    "Flyt godtok ikke innloggingen for denne kommunen"
                ) from None
            raise FlytAuthError(
                "Flyt-økten kunne ikke opprettes; den nye innloggingen er ikke lagret"
            ) from None
        if _oauth_error(exc) in _FATAL_OAUTH_ERRORS:
            with private_source_lock(root, SOURCE):
                _mark_refresh_blocked(
                    root,
                    "fatal-client-configuration",
                    at=time.time() if wall_time is None else wall_time,
                )
            raise FlytFatalConfigurationError(
                "Visma Connect har avvist Flyt-appregistreringen"
            ) from None
        raise FlytAuthError(
            "Innloggingens tokenutveksling fikk et ukjent resultat; start på nytt"
        ) from None
    timestamp = _number(
        time.time() if wall_time is None else wall_time, "Ugyldig tidspunkt for tokenlagring"
    )
    base = {
        "subject": tokens["subject"],
        "device_unique_id": transaction.device_unique_id,
        "customer_key": config.customer_key,
        **guardian,
    }
    stored = _session_json(tokens, base, at=timestamp)
    with private_source_lock(root, SOURCE, wait=True):
        if not hmac.compare_digest(
            _read_device_id(root).encode(), transaction.device_unique_id.encode()
        ):
            raise FlytAuthError("Flyt-enhets-ID-en ble endret; start innloggingen på nytt")
        target = session_path(root)
        if (target.exists() or target.is_symlink()) and not replace_existing:
            raise FlytAuthError("En lokal Flyt-økt finnes allerede; bekreft at den skal erstattes")
        atomic_write_private_json(target, stored, overwrite=replace_existing)
        remove_private_file(refresh_block_path(root))
    result: dict[str, Any] = {
        "authenticated": True,
        "provider": "visma-connect",
        "municipality": config.municipality.display_name,
        "guardian_session_created": True,
        "kindergarten_status": guardian.get("kindergarten_status"),
        "school_status": guardian.get("school_status"),
    }
    if "access_expires_at" in stored:
        result["expires_at"] = utc_iso(stored["access_expires_at"])
    return result


# Stored session -----------------------------------------------------------


def _read_session(root: Path | str | None) -> dict[str, Any]:
    try:
        value = read_private_json(
            session_path(root),
            max_bytes=MAX_PRIVATE_JSON_BYTES,
            missing_message="Flyt-økten mangler",
        )
    except PrivateStateError:
        raise FlytReauthenticationRequired("Flyt-økten mangler eller er ugyldig") from None
    if not isinstance(value, Mapping):
        raise FlytReauthenticationRequired("Flyt-økten er ugyldig")
    try:
        session: dict[str, Any] = {
            "access_token": _token(value.get("access_token"), "Access-tokenet"),
            "refresh_token": _token(value.get("refresh_token"), "Refresh-tokenet"),
            "subject": _key(value.get("subject"), "Kontoidentiteten"),
            "device_unique_id": _key(value.get("device_unique_id"), "Flyt-enhets-ID"),
            "customer_key": _key(value.get("customer_key"), "Kundenøkkelen"),
            "updated_at": _number(value.get("updated_at"), "Ugyldig tidspunkt"),
        }
        if value.get("session_id") is not None:
            session["session_id"] = _key(value.get("session_id"), "Økt-ID-en")
        if value.get("access_expires_at") is not None:
            session["access_expires_at"] = _number(
                value.get("access_expires_at"), "Ugyldig utløpstid"
            )
        if value.get("refresh_transaction_id") is not None:
            session["refresh_transaction_id"] = _key(
                value.get("refresh_transaction_id"), "Refresh-transaksjonen"
            )
        for name in ("kindergarten_status", "school_status"):
            if isinstance(value.get(name), int) and not isinstance(value.get(name), bool):
                session[name] = value[name]
    except FlytAuthError:
        raise FlytReauthenticationRequired("Flyt-økten er ugyldig") from None
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
    if not isinstance(value, Mapping) or not isinstance(value.get("reason"), str):
        return {"reason": "invalid-marker"}
    return value


def _mark_refresh_blocked(root, reason, *, at, transaction_id=None, diagnostic=None):
    value: dict[str, Any] = {"reason": reason, "recorded_at": utc_iso(float(at))}
    if transaction_id is not None:
        value["transaction_id"] = transaction_id
    if diagnostic is not None:
        value["diagnostic"] = dict(diagnostic)
    atomic_write_private_json(refresh_block_path(root), value)


def _marker_completed(marker: Mapping[str, Any], session: Mapping[str, Any]) -> bool:
    return (
        marker.get("reason") in {"refresh-in-flight", "refresh-response-received"}
        and isinstance(marker.get("transaction_id"), str)
        and isinstance(session.get("refresh_transaction_id"), str)
        and hmac.compare_digest(
            str(marker["transaction_id"]).encode(), str(session["refresh_transaction_id"]).encode()
        )
    )


def _oauth_error(exc: Exception) -> str | None:
    value = getattr(exc, "error_code", None)
    return value if isinstance(value, str) and value in _SAFE_OAUTH_ERRORS else None


def _session_binding(session: Mapping[str, Any]) -> str:
    material = [session[key] for key in ("refresh_token", "subject", "device_unique_id")]
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()


def _commit_refresh(root, session, value, *, at, transaction_id) -> tuple[dict[str, Any], bool]:
    tokens = _validated_tokens(value, previous_refresh=session["refresh_token"])
    if "subject" in tokens and not hmac.compare_digest(
        tokens["subject"].encode(), session["subject"].encode()
    ):
        raise FlytReauthenticationRequired("Flyt-fornyelsen byttet konto; logg inn på nytt")
    stored = _session_json(tokens, session, at=at, transaction_id=transaction_id)
    atomic_write_private_json(session_path(root), stored)
    rotated = not hmac.compare_digest(
        session["refresh_token"].encode(), tokens["refresh_token"].encode()
    )
    return stored, rotated


def _recover_refresh_response(root, marker, session):
    """Adopt a saved refresh reply that belongs to this exact session, without HTTP."""

    binding = marker.get("session_binding")
    if not isinstance(binding, str) or not hmac.compare_digest(
        binding.encode(), _session_binding(session).encode()
    ):
        raise FlytRefreshUncertain("Lagret fornyelsessvar tilhører ikke gjeldende Flyt-økt")
    try:
        received = _number(marker.get("received_at"), "Ugyldig tidspunkt")
        transaction = _key(marker.get("transaction_id"), "Refresh-transaksjonen")
        _commit_refresh(
            root, session, marker.get("response"), at=received, transaction_id=transaction
        )
    except FlytReauthenticationRequired:
        raise
    except FlytAuthError:
        raise FlytRefreshUncertain(
            "Lagret fornyelsessvar må avklares før Flyt-økten kan brukes"
        ) from None
    lifecycle.record(root, SOURCE, "response_recovered", value=marker.get("response"), at=received)
    return _read_session(root)


def local_session_for_use(root: Path | str | None) -> dict[str, Any]:
    with private_source_lock(root, SOURCE):
        return _local_session_for_use_locked(root)


def _local_session_for_use_locked(root: Path | str | None) -> dict[str, Any]:
    """Return the secret-bearing session only to the read client."""

    if not consent_status(root)["current"]:
        raise FlytReauthenticationRequired("Eksplisitt samtykke mangler for Flyt")
    try:
        config = load_client_config(root)
    except FlytAuthError:
        raise FlytReauthenticationRequired("Flyt er ikke satt opp med en kommune") from None
    session = _read_session(root)
    if not hmac.compare_digest(session["customer_key"].encode(), config.customer_key.encode()):
        raise FlytReauthenticationRequired(
            "Flyt-økten hører til en annen kommune; logg inn på nytt"
        )
    try:
        device = _read_device_id(root)
    except FlytAuthError:
        raise FlytReauthenticationRequired("Flyt-enhets-ID-en er ugyldig") from None
    if not hmac.compare_digest(device.encode(), session["device_unique_id"].encode()):
        raise FlytReauthenticationRequired("Flyt-øktens enhets-ID samsvarer ikke")
    marker = _read_refresh_block(root)
    if marker is not None:
        if marker.get("reason") == "refresh-response-received" and not _marker_completed(
            marker, session
        ):
            session = _recover_refresh_response(root, marker, session)
        if _marker_completed(marker, session):
            try:
                remove_private_file(refresh_block_path(root))
            except PrivateStateError:
                pass
        elif marker.get("reason") == "fatal-client-configuration":
            raise FlytFatalConfigurationError("Visma Connect har avvist Flyt-appregistreringen")
        elif marker.get("reason") == "invalid-grant":
            raise FlytReauthenticationRequired("Flyt-innloggingen er utløpt; logg inn på nytt")
        else:
            raise FlytRefreshUncertain(
                "Fornyelsen av Flyt-økten har ukjent utfall; økten er satt på pause"
            )
    return session


@lifecycle.operation(SOURCE, "refresh")
def refresh_session(
    root: Path | str | None,
    *,
    transport: AuthTransport | None = None,
    at: float | None = None,
) -> dict[str, Any]:
    """Rotate the Visma Connect grant; block reuse after any unclear result."""

    timestamp = _number(time.time() if at is None else at, "Ugyldig tidspunkt for tokenfornyelse")
    with private_source_lock(root, SOURCE):
        session = local_session_for_use(root)
        config = load_client_config(root)
        transaction_id = _random(secrets.token_bytes)
        _mark_refresh_blocked(
            root, "refresh-in-flight", at=timestamp, transaction_id=transaction_id
        )
        client = lifecycle.observe_transport(
            root, SOURCE, transport or default_transport(), at=timestamp, capture_response=False
        )
        response_saved = False
        stage = "request"
        try:
            value = client.post_form(
                TOKEN_ENDPOINT,
                {
                    "client_id": config.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": session["refresh_token"],
                },
            )
            stage = "persist-response"
            # Keep the reply before replacing the session: a crash afterwards
            # must not lose a rotated refresh token.
            kept = (
                "access_token",
                "refresh_token",
                "id_token",
                "token_type",
                *lifecycle.TIME_FIELDS,
            )
            atomic_write_private_json(
                refresh_block_path(root),
                {
                    "reason": "refresh-response-received",
                    "transaction_id": transaction_id,
                    "received_at": timestamp,
                    "session_binding": _session_binding(session),
                    "response": {key: value[key] for key in kept if key in value}
                    if isinstance(value, Mapping)
                    else None,
                },
            )
            response_saved = True
            lifecycle.record(root, SOURCE, "response_saved", value=value, at=timestamp)
            stage = "validate-response"
            stored, rotated = _commit_refresh(
                root, session, value, at=timestamp, transaction_id=transaction_id
            )
        except Exception as exc:
            if isinstance(exc, FlytReauthenticationRequired):
                raise
            if isinstance(exc, ApiRequestNotSent):
                remove_private_file(refresh_block_path(root))
                raise FlytRefreshUncertain(
                    "Forespørselen ble ikke sendt; kan forsøkes ved neste runde"
                ) from None
            oauth_error = _oauth_error(exc)
            if oauth_error in _FATAL_OAUTH_ERRORS:
                _mark_refresh_blocked(root, "fatal-client-configuration", at=timestamp)
                raise FlytFatalConfigurationError(
                    "Visma Connect har avvist Flyt-appregistreringen"
                ) from None
            if oauth_error == "invalid_grant":
                _mark_refresh_blocked(
                    root,
                    "invalid-grant",
                    at=timestamp,
                    diagnostic={
                        "stage": "request",
                        "kind": "http",
                        "http_status": getattr(exc, "status_code", 400),
                    },
                )
                raise FlytReauthenticationRequired(
                    "Flyt-innloggingen er utløpt; logg inn på nytt"
                ) from None
            if not response_saved:
                diagnostic: dict[str, Any] = {
                    "stage": stage,
                    "kind": "http"
                    if isinstance(exc, ApiHTTPError)
                    else "transport"
                    if isinstance(exc, ApiTransportError)
                    else "local",
                }
                if isinstance(exc, ApiHTTPError):
                    diagnostic["http_status"] = exc.status_code
                _mark_refresh_blocked(
                    root,
                    "refresh-in-flight",
                    at=timestamp,
                    transaction_id=transaction_id,
                    diagnostic=diagnostic,
                )
            raise FlytRefreshUncertain(
                "Fornyelsen av Flyt-økten må avklares; økten er satt på pause"
            ) from None
        cleanup_pending = False
        try:
            remove_private_file(refresh_block_path(root))
        except PrivateStateError:
            cleanup_pending = True
    result: dict[str, Any] = {
        "refreshed": True,
        "refresh_token_rotated": rotated,
        "refresh_marker_cleanup_pending": cleanup_pending,
    }
    if "access_expires_at" in stored:
        result["expires_at"] = utc_iso(stored["access_expires_at"])
    return result


def auth_status(root: Path | str | None) -> dict[str, Any]:
    """Offline, secret-free status. Reads files only; never creates them."""

    municipality = None
    try:
        config = load_client_config(root)
        configured = True
        municipality = config.municipality.display_name
    except FlytAuthError:
        configured = False
    consent = consent_status(root)
    marker = _read_refresh_block(root)
    target = session_path(root)
    session_present = target.is_file() and not target.is_symlink()
    authenticated = False
    expires_at = None
    session: Mapping[str, Any] = {}
    if configured and consent["current"] and session_present:
        try:
            session = _read_session(root)
            if not hmac.compare_digest(
                session["customer_key"].encode(), config.customer_key.encode()
            ):
                raise FlytAuthError("Annen kommune")
            if not hmac.compare_digest(
                _read_device_id(root).encode(), session["device_unique_id"].encode()
            ):
                raise FlytAuthError("Enhets-ID samsvarer ikke")
            if marker is not None and not _marker_completed(marker, session):
                raise FlytAuthError("Fornyelsen er blokkert")
        except (FlytAuthError, SourceLoginRequired):
            authenticated = False
        else:
            authenticated = True
            if "access_expires_at" in session:
                expires_at = utc_iso(session["access_expires_at"])
    reason = marker.get("reason") if isinstance(marker, Mapping) else None
    if authenticated and marker is not None and _marker_completed(marker, session):
        reason = None
    diagnostic = marker.get("diagnostic") if isinstance(marker, Mapping) else None
    return {
        "configured": configured,
        "municipality": municipality,
        "consent_recorded": consent["current"],
        "session_present": session_present,
        "authenticated": authenticated,
        "access_expired": bool(
            authenticated
            and session.get("access_expires_at") is not None
            and session["access_expires_at"] <= time.time()
        ),
        "expires_at": expires_at,
        "refresh_blocked": reason is not None,
        "refresh_block_reason": reason,
        "refresh_response_saved": bool(
            marker and marker.get("reason") == "refresh-response-received"
        ),
        "refresh_diagnostic": {
            key: value
            for key, value in diagnostic.items()
            if (key == "stage" and value in ("request", "persist-response", "validate-response"))
            or (key == "kind" and value in ("http", "transport", "local"))
            or (key == "http_status" and type(value) is int and 100 <= value <= 599)
        }
        if isinstance(diagnostic, dict)
        else {},
        "device_id_present": device_path(root).is_file() and not device_path(root).is_symlink(),
    }


@lifecycle.operation(SOURCE, "logout")
def logout(root: Path | str | None) -> dict[str, Any]:
    """Remove the local session; consent, municipality and device ID stay."""

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
    "ClientConfig",
    "FlytAuthError",
    "FlytFatalConfigurationError",
    "FlytReauthenticationRequired",
    "FlytRefreshUncertain",
    "LoginTransaction",
    "Municipality",
    "auth_status",
    "begin_login",
    "configure_client",
    "configure_municipality",
    "consent_status",
    "guardian_headers",
    "list_municipalities",
    "load_client_config",
    "local_session_for_use",
    "login_with_callback",
    "logout",
    "parse_municipalities",
    "pkce_challenge",
    "record_consent",
    "refresh_session",
    "select_municipality",
    "session_path",
]
