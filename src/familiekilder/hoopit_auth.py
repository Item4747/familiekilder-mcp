"""Private renewable Firebase session for the read-only Hoopit API adapter.

Hoopit's first-party clients authenticate API reads with a Firebase ID token
using ``Authorization: JWT <token>``.  The corresponding Firebase refresh
token is long-lived and can mint a new short-lived ID token through Google's
documented Secure Token endpoint.  This module imports that existing token
pair from an owned ``0600`` file below a literal ``.private`` directory and
then maintains an independent local copy, just like the other source adapters.

No function accepts a Hoopit password, PIN, cookie jar or browser profile.
Secret-bearing values are never returned by status/import/refresh operations
and are written only below the literal ``.private`` tree.
"""

from __future__ import annotations

from . import auth_lifecycle as lifecycle

import base64
import hmac
import json
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol

import jwt
from cryptography import x509
from jwt import InvalidTokenError

from .api_transport import ApiHTTPError, ApiTransport, ApiTransportError
from .auth_common import (
    canonical_sha256,
    fingerprint_matches,
    is_plain_text,
    normalize_public_host,
    read_private_mapping,
    utc_iso,
)
from .core import FamiliekilderError, project_paths
from .private_lock import private_source_lock
from .private_state import (
    PrivateStateError,
    atomic_write_private_json,
    credential_path,
    read_private_json,
    remove_private_file,
)
from .source_adapter import SourceLoginRequired, SourceUnavailable


SOURCE = "hoopit"
API_ROOT = "https://api.hoopit.io"
FIREBASE_PROJECT_ID = "hoopit-30d5c"
SECURE_TOKEN_ROOT = "https://securetoken.googleapis.com/v1/token"
FIREBASE_CERTS_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/"
    "securetoken@system.gserviceaccount.com"
)

CLIENT_FILE = "hoopit-client.json"
SESSION_FILE = "hoopit-session.json"
REFRESH_STATE_FILE = "hoopit-refresh-state.json"
RATE_STATE_FILE = "hoopit-rate-limit.json"
DIRECT_ACCESS_PROOF_FILE = "hoopit-direct-access-proof.json"
DIRECT_ACCESS_REJECTION_FILE = "hoopit-direct-access-rejection.json"
MAX_PRIVATE_JSON_BYTES = 256 * 1024
REFRESH_SKEW_SECONDS = 10 * 60
DIRECT_ACCESS_RETRY_SECONDS = 15 * 60
MAX_FIREBASE_TOKEN_LIFETIME_SECONDS = 2 * 60 * 60

_CLIENT_FIELDS = frozenset(
    {
        "api_root",
        "firebase_api_key",
        "firebase_project_id",
        "app_version",
        "attachment_hosts",
    }
)
_IMPORT_FIELDS = frozenset({"id_token", "refresh_token"})
_SESSION_FIELDS_V1 = frozenset(
    {
        "version",
        "id_token",
        "refresh_token",
        "subject",
        "issued_at",
        "expires_at",
        "client_fingerprint",
        "token_verified_at",
        "signing_key_id",
    }
)
_SESSION_FIELDS_V2 = _SESSION_FIELDS_V1 | {"session_binding"}
_DIRECT_ACCESS_PROOF_FIELDS = frozenset(
    {
        "version",
        "verified_at",
        "auth_identity_fingerprint",
        "app_release_fingerprint",
        "session_identity_fingerprint",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SESSION_BINDING = re.compile(r"[0-9a-f]{64}\Z")


class HoopitAuthError(FamiliekilderError):
    """Sanitized Hoopit authentication or local-state failure."""


class HoopitReauthenticationRequired(SourceLoginRequired, HoopitAuthError):
    """The imported Firebase session is absent, rejected or invalid."""


class HoopitReadAccessUnverified(SourceUnavailable):
    """The app must verify API access using the already imported session."""

    reason = 'setup_verification'


class HoopitReadAccessDenied(HoopitReadAccessUnverified):
    """A verified Firebase session was refused by the provider's read API."""

    reason = 'access_denied'

    def __init__(self, message, *, retry_at=None):
        self.retry_at = retry_at
        super().__init__(message)


class HoopitRemoteUnavailable(SourceUnavailable):
    """Preserve a remote failure's safe category without its response body."""

    def __init__(self, message, *, reason, retry_after_seconds=None):
        self.reason = reason
        self.retry_at = ((datetime.now(UTC) + timedelta(seconds=retry_after_seconds)).isoformat()
                         if retry_after_seconds is not None else None)
        super().__init__(message)


@dataclass(frozen=True)
class ClientConfig:
    api_root: str
    firebase_api_key: str = field(repr=False)
    firebase_project_id: str = FIREBASE_PROJECT_ID
    app_version: str = ""
    attachment_hosts: tuple[str, ...] = ()

    def as_private_json(self) -> dict[str, Any]:
        return {
            "api_root": self.api_root,
            "firebase_api_key": self.firebase_api_key,
            "firebase_project_id": self.firebase_project_id,
            "app_version": self.app_version,
            "attachment_hosts": list(self.attachment_hosts),
        }


@dataclass(frozen=True)
class SessionCredential:
    id_token: str = field(repr=False)
    refresh_token: str = field(repr=False)


@dataclass(frozen=True)
class HoopitAccess:
    id_token: str = field(repr=False)
    app_version: str
    expires_at: float
    attachment_hosts: tuple[str, ...]


@dataclass(frozen=True)
class DirectAccessProofBinding:
    """Opaque, secret-free binding used only to validate the local live gate."""

    auth_identity_fingerprint: str = field(repr=False)
    app_release_fingerprint: str = field(repr=False)
    session_identity_fingerprint: str = field(repr=False)


class RefreshTransport(Protocol):
    def post_form(
        self,
        url: str,
        fields: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any: ...


class TokenVerificationTransport(Protocol):
    def get_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any: ...


class RateLimitController(Protocol):
    def before_request(self) -> None: ...

    def record_success(self) -> None: ...

    def record_http_failure(
        self, status_code: int, retry_after: int | None = None
    ) -> float | None: ...

    def record_network_failure(self) -> None: ...


@dataclass(frozen=True)
class _VerifiedIdToken:
    token: str = field(repr=False)
    subject: str
    issued_at: float
    expires_at: float
    signing_key_id: str


def client_config_path(root: Path | str | None) -> Path:
    return credential_path(root, CLIENT_FILE)


def session_path(root: Path | str | None) -> Path:
    return credential_path(root, SESSION_FILE)


def refresh_state_path(root: Path | str | None) -> Path:
    return credential_path(root, REFRESH_STATE_FILE)


def rate_state_path(root: Path | str | None) -> Path:
    return project_paths(root).private / "state" / RATE_STATE_FILE


def direct_access_proof_path(root: Path | str | None) -> Path:
    return credential_path(root, DIRECT_ACCESS_PROOF_FILE)


def _direct_access_rejection_path(root):
    return credential_path(root, DIRECT_ACCESS_REJECTION_FILE)


def _required_text(value: Any, label: str, *, maximum: int = 4096) -> str:
    if not is_plain_text(value, maximum=maximum):
        raise HoopitAuthError(f"Hoopit-konfigurasjonen har ugyldig {label}")
    return value


def _safe_secret(value: Any, label: str) -> str:
    if not is_plain_text(value, maximum=128 * 1024, allow_space=False):
        raise HoopitAuthError(f"Hoopit-økten har ugyldig {label}")
    return value


def _number(value: Any, label: str, *, minimum: float = 0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise HoopitAuthError(f"Hoopit-økten har ugyldig {label}")
    return float(value)


def _expires_in(value: Any) -> float:
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    return _number(value, "expires_in", minimum=1)


def _normalize_host(value: Any) -> str:
    host = normalize_public_host(value)
    if host is None:
        raise HoopitAuthError("Hoopit-konfigurasjonen har ugyldig vedleggsvert")
    return host


def _parse_client_config(value: Mapping[str, Any]) -> ClientConfig:
    if set(value) != _CLIENT_FIELDS:
        raise HoopitAuthError(
            "Hoopit-klientkonfigurasjonen har ukjente eller manglende felt"
        )
    hosts = value.get("attachment_hosts")
    if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
        raise HoopitAuthError("Hoopit-konfigurasjonen har ugyldig vedleggsvertsliste")
    if len(hosts) > 20:
        raise HoopitAuthError("Hoopit-konfigurasjonen har for mange vedleggsverter")
    config = ClientConfig(
        api_root=_required_text(value.get("api_root"), "API-rot", maximum=200),
        firebase_api_key=_required_text(
            value.get("firebase_api_key"), "Firebase API-nøkkel", maximum=200
        ),
        firebase_project_id=_required_text(
            value.get("firebase_project_id"), "Firebase-prosjekt", maximum=100
        ),
        app_version=_required_text(value.get("app_version"), "appversjon", maximum=100),
        attachment_hosts=tuple(sorted({_normalize_host(host) for host in hosts})),
    )
    if config.api_root != API_ROOT:
        raise HoopitAuthError("Hoopit API-roten er ikke det fastlåste produksjons-API-et")
    if config.firebase_project_id != FIREBASE_PROJECT_ID:
        raise HoopitAuthError("Hoopit Firebase-prosjektet er ikke fastlåst produksjon")
    if not re.fullmatch(r"AIza[A-Za-z0-9_-]{26,196}", config.firebase_api_key):
        raise HoopitAuthError("Hoopit Firebase API-nøkkelen har ugyldig format")
    return config


def _read_mapping(path: Path, missing_message: str) -> Mapping[str, Any]:
    return read_private_mapping(
        path,
        missing_message,
        max_bytes=MAX_PRIVATE_JSON_BYTES,
        error=HoopitAuthError,
        invalid_message="Den private Hoopit-filen har ugyldig format",
    )


def load_client_config(root: Path | str | None) -> ClientConfig:
    return _parse_client_config(
        _read_mapping(client_config_path(root), "Hoopit-klientkonfigurasjonen mangler")
    )


def _auth_identity_json(config: ClientConfig) -> dict[str, str]:
    return {
        "api_root": config.api_root,
        "firebase_api_key": config.firebase_api_key,
        "firebase_project_id": config.firebase_project_id,
    }


def _config_fingerprint(config: ClientConfig) -> str:
    """Bind the grant to Firebase/app identity, not mutable app metadata."""

    return canonical_sha256(_auth_identity_json(config), ensure_ascii=False)


def _provider_release_context(root: Path | str | None) -> Mapping[str, Any] | None:
    """Read the validated bundle generation/provenance when provisioned that way."""

    try:
        from .provider_bundle import _load_state, _transaction_directory_exists

        if _transaction_directory_exists(root):
            raise HoopitAuthError("En providerprofiltransaksjon er ikke ferdigstilt")
        state = _load_state(root)
    except Exception:
        raise HoopitAuthError("Hoopit-providerreleasen kunne ikke valideres") from None
    if state is None:
        return None
    try:
        return {
            "generation": state["generation"],
            "provenance": state["sources"][SOURCE]["provenance"],
        }
    except (KeyError, TypeError):
        raise HoopitAuthError("Hoopit-providerreleasen kunne ikke valideres") from None


def _app_release_fingerprint(
    root: Path | str | None, config: ClientConfig
) -> str:
    return canonical_sha256(
        {
            "app_version": config.app_version,
            "provider_release": _provider_release_context(root),
        },
        ensure_ascii=False,
    )


def _session_identity_fingerprint(subject: str, session_binding: str) -> str:
    """Bind a proof to one imported session without storing a user identifier."""

    return canonical_sha256(
        {"subject": subject, "session_binding": session_binding}, ensure_ascii=False
    )


def _direct_access_binding(
    root: Path | str | None,
    config: ClientConfig,
    session: Mapping[str, Any],
) -> DirectAccessProofBinding:
    session_binding = session.get("session_binding")
    if not isinstance(session_binding, str) or not _SESSION_BINDING.fullmatch(
        session_binding
    ):
        raise HoopitAuthError("Hoopit-økten mangler en gyldig lokal sesjonsbinding")
    subject = session.get("subject")
    if not isinstance(subject, str) or not subject:
        raise HoopitAuthError("Hoopit-øktens brukerbinding er ugyldig")
    return DirectAccessProofBinding(
        auth_identity_fingerprint=_config_fingerprint(config),
        app_release_fingerprint=_app_release_fingerprint(root, config),
        session_identity_fingerprint=_session_identity_fingerprint(
            subject, session_binding
        ),
    )


def _legacy_config_fingerprint(config: ClientConfig) -> str:
    return canonical_sha256(config.as_private_json(), ensure_ascii=False)


def _fingerprint_matches_config(fingerprint: object, config: ClientConfig) -> bool:
    return fingerprint_matches(
        fingerprint, (_config_fingerprint(config), _legacy_config_fingerprint(config))
    )


def configure_client(root: Path | str | None, client_file: Path | str) -> dict[str, Any]:
    """Validate and privately import public metadata from the official client."""

    source = Path(client_file).expanduser()
    config = _parse_client_config(
        _read_mapping(source, "Fant ikke Hoopit-klientkonfigurasjonsfilen")
    )
    with private_source_lock(root, SOURCE):
        previous_config: ClientConfig | None = None
        existing_config_path = client_config_path(root)
        if existing_config_path.exists() or existing_config_path.is_symlink():
            try:
                previous_config = load_client_config(root)
            except HoopitAuthError:
                previous_config = None
        target = session_path(root)
        if target.exists() or target.is_symlink():
            current = _read_mapping(target, "Hoopit-økten mangler")
            fingerprint = current.get("client_fingerprint")
            try:
                current_config = load_client_config(root)
            except HoopitAuthError:
                raise HoopitAuthError(
                    "Hoopit-klientkonfigurasjonen kan ikke oppdateres mens økten er ugyldig"
                ) from None
            if not _fingerprint_matches_config(fingerprint, current_config):
                raise HoopitAuthError("Hoopit-øktens eksisterende klientbinding er ugyldig")
            if not hmac.compare_digest(
                _config_fingerprint(current_config).encode("ascii"),
                _config_fingerprint(config).encode("ascii"),
            ):
                raise HoopitAuthError(
                    "Fjern den lokale Hoopit-økten før auth-identiteten endres"
                )
            if not hmac.compare_digest(
                str(fingerprint).encode("ascii", "ignore"),
                _config_fingerprint(current_config).encode("ascii"),
            ):
                migrated_session = dict(current)
                migrated_session["client_fingerprint"] = _config_fingerprint(
                    current_config
                )
                atomic_write_private_json(target, migrated_session)
        try:
            atomic_write_private_json(client_config_path(root), config.as_private_json())
            if previous_config is None or (
                previous_config.app_version != config.app_version
                or not fingerprint_matches(
                    _config_fingerprint(previous_config), (_config_fingerprint(config),)
                )
            ):
                remove_private_file(direct_access_proof_path(root))
        except PrivateStateError as exc:
            raise HoopitAuthError(str(exc)) from None
    return {
        "configured": True,
        "api_root_validated": True,
        "firebase_project_validated": True,
        "app_version_validated": True,
        "attachment_hosts": len(config.attachment_hosts),
    }


def _decode_claims(token: str) -> Mapping[str, Any]:
    token = _safe_secret(token, "Firebase ID-token")
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise HoopitAuthError("Hoopit Firebase ID-tokenet har ugyldig format")
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise HoopitAuthError("Hoopit Firebase ID-tokenet har ugyldig format") from None
    if not isinstance(value, Mapping):
        raise HoopitAuthError("Hoopit Firebase ID-tokenet har ugyldig format")
    return value


def _validated_claims(
    token: str, *, at: float, require_current: bool
) -> tuple[str, float, float]:
    claims = _decode_claims(token)
    subject = _required_text(claims.get("sub"), "Firebase subject", maximum=512)
    if claims.get("iss") != f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}":
        raise HoopitAuthError("Hoopit Firebase-tokenet har feil utsteder")
    if claims.get("aud") != FIREBASE_PROJECT_ID:
        raise HoopitAuthError("Hoopit Firebase-tokenet har feil mottaker")
    issued_at = _number(claims.get("iat"), "Firebase issued_at", minimum=1)
    expires_at = _number(claims.get("exp"), "Firebase expires_at", minimum=1)
    if issued_at > at + 60 or (require_current and expires_at <= at):
        raise HoopitAuthError("Hoopit Firebase-tokenet er utløpt eller ikke gyldig ennå")
    if expires_at <= issued_at or expires_at - issued_at > MAX_FIREBASE_TOKEN_LIFETIME_SECONDS:
        raise HoopitAuthError("Hoopit Firebase-tokenet har ugyldig levetid")
    return subject, issued_at, expires_at


def _verified_id_token(
    token: str, certificates: Mapping[str, Any], *, at: float
) -> _VerifiedIdToken:
    safe_token = _safe_secret(token, "Firebase ID-token")
    try:
        header = jwt.get_unverified_header(safe_token)
    except InvalidTokenError:
        raise HoopitAuthError("Hoopit Firebase ID-tokenet har ugyldig header") from None
    if not isinstance(header, Mapping) or header.get("alg") != "RS256":
        raise HoopitAuthError("Hoopit Firebase ID-tokenet bruker feil signaturalgoritme")
    key_id = header.get("kid")
    if not is_plain_text(key_id, maximum=256, allow_space=False):
        raise HoopitAuthError("Hoopit Firebase ID-tokenet mangler gyldig signing key-ID")
    certificate = certificates.get(key_id)
    if not isinstance(certificate, str) or not certificate.startswith(
        "-----BEGIN CERTIFICATE-----"
    ):
        raise HoopitAuthError("Hoopit Firebase-signaturnøkkelen er ukjent")
    try:
        public_key = x509.load_pem_x509_certificate(certificate.encode("ascii")).public_key()
        claims = jwt.decode(
            safe_token,
            public_key,
            algorithms=["RS256"],
            audience=FIREBASE_PROJECT_ID,
            issuer=f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}",
            options={
                "verify_exp": False,
                "verify_iat": False,
                "require": ["sub", "iat", "exp", "iss", "aud"],
            },
        )
    except (InvalidTokenError, ValueError, TypeError, UnicodeError):
        raise HoopitAuthError("Hoopit Firebase ID-tokenets signatur er ugyldig") from None
    if not isinstance(claims, Mapping):
        raise HoopitAuthError("Hoopit Firebase ID-tokenets claims er ugyldige")
    subject, issued_at, expires_at = _validated_claims(
        safe_token, at=at, require_current=True
    )
    if claims.get("sub") != subject:
        raise HoopitAuthError("Hoopit Firebase-tokenets brukerbinding er ugyldig")
    return _VerifiedIdToken(
        token=safe_token,
        subject=subject,
        issued_at=issued_at,
        expires_at=expires_at,
        signing_key_id=key_id,
    )


def _default_rate_limiter(root: Path | str | None) -> RateLimitController:
    from .hoopit import PersistentRateLimiter

    return PersistentRateLimiter(root)


def _default_verification_transport() -> ApiTransport:
    return ApiTransport(
        allowed_urls={FIREBASE_CERTS_URL},
        timeout=45,
        max_json_bytes=MAX_PRIVATE_JSON_BYTES,
        user_agent="familiekilder-hoopit-auth/0.2",
    )


def _record_remote_failure(
    limiter: RateLimitController, exc: Exception, *, operation: str
) -> None:
    if isinstance(exc, ApiHTTPError):
        limiter.record_http_failure(exc.status_code, exc.retry_after_seconds)
        if exc.status_code == 429:
            raise HoopitRemoteUnavailable(
                f"Hoopit {operation} ble ratebegrenset og stoppet uten retry",
                reason='rate_limit', retry_after_seconds=exc.retry_after_seconds,
            ) from None
        if 500 <= exc.status_code <= 599:
            raise HoopitRemoteUnavailable(
                f"Hoopit {operation} fikk serverfeil og stoppet uten retry",
                reason='backend_error', retry_after_seconds=exc.retry_after_seconds,
            ) from None
        raise HoopitAuthError(f"Hoopit {operation} ble avvist") from None
    limiter.record_network_failure()
    raise HoopitRemoteUnavailable(
        f"Hoopit {operation} kunne ikke nås og stoppet uten retry", reason='transient',
    ) from None


def _fetch_certificates(
    transport: TokenVerificationTransport, limiter: RateLimitController
) -> Mapping[str, Any]:
    limiter.before_request()
    try:
        response = transport.get_json(FIREBASE_CERTS_URL)
    except (ApiHTTPError, ApiTransportError, OSError, TimeoutError) as exc:
        _record_remote_failure(limiter, exc, operation="signaturnøkkelhenting")
        raise AssertionError("unreachable")
    except Exception as exc:
        _record_remote_failure(limiter, exc, operation="signaturnøkkelhenting")
        raise AssertionError("unreachable")
    limiter.record_success()
    if (
        not isinstance(response, Mapping)
        or not response
        or len(response) > 20
        or any(
            not isinstance(key, str)
            or not isinstance(certificate, str)
            or not certificate.startswith("-----BEGIN CERTIFICATE-----")
            for key, certificate in response.items()
        )
    ):
        raise HoopitAuthError("Firebase returnerte ugyldige signeringssertifikater")
    return response


def _session_value(
    config: ClientConfig,
    credential: SessionCredential,
    verified: _VerifiedIdToken,
    *,
    at: float,
    session_binding: str,
) -> dict[str, Any]:
    id_token = _safe_secret(credential.id_token, "Firebase ID-token")
    refresh_token = _safe_secret(credential.refresh_token, "Firebase refresh-token")
    if not hmac.compare_digest(id_token.encode(), verified.token.encode()):
        raise HoopitAuthError("Hoopit Firebase-tokenverifikasjonen samsvarer ikke")
    if not isinstance(session_binding, str) or not _SESSION_BINDING.fullmatch(
        session_binding
    ):
        raise HoopitAuthError("Hoopit-økten har ugyldig lokal sesjonsbinding")
    return {
        "version": 2,
        "id_token": id_token,
        "refresh_token": refresh_token,
        "subject": verified.subject,
        "issued_at": verified.issued_at,
        "expires_at": verified.expires_at,
        "client_fingerprint": _config_fingerprint(config),
        "token_verified_at": at,
        "signing_key_id": verified.signing_key_id,
        "session_binding": session_binding,
    }


def _parse_session(
    config: ClientConfig, value: Mapping[str, Any], *, at: float
) -> dict[str, Any]:
    version = value.get("version")
    if not (
        (version == 1 and set(value) == _SESSION_FIELDS_V1)
        or (version == 2 and set(value) == _SESSION_FIELDS_V2)
    ):
        raise HoopitAuthError("Hoopit-økten har ugyldig format")
    id_token = _safe_secret(value.get("id_token"), "Firebase ID-token")
    subject, issued_at, expires_at = _validated_claims(
        id_token, at=at, require_current=False
    )
    if value.get("subject") != subject:
        raise HoopitAuthError("Hoopit-øktens brukerbinding samsvarer ikke")
    stored_issued = _number(value.get("issued_at"), "issued_at", minimum=1)
    stored_expiry = _number(value.get("expires_at"), "expires_at", minimum=1)
    if abs(stored_issued - issued_at) > 1 or abs(stored_expiry - expires_at) > 1:
        raise HoopitAuthError("Hoopit-øktens tidsbinding samsvarer ikke")
    fingerprint = value.get("client_fingerprint")
    if not _fingerprint_matches_config(fingerprint, config):
        raise HoopitAuthError("Hoopit-øktens klientbinding samsvarer ikke")
    signing_key_id = _required_text(
        value.get("signing_key_id"), "Firebase signing key-ID", maximum=256
    )
    try:
        header = jwt.get_unverified_header(id_token)
    except InvalidTokenError:
        raise HoopitAuthError("Hoopit Firebase ID-tokenet har ugyldig header") from None
    if (
        not isinstance(header, Mapping)
        or header.get("alg") != "RS256"
        or header.get("kid") != signing_key_id
    ):
        raise HoopitAuthError("Hoopit-øktens signaturnøkkelbinding samsvarer ikke")
    token_verified_at = _number(
        value.get("token_verified_at"), "token_verified_at", minimum=1
    )
    if token_verified_at > at + 60:
        raise HoopitAuthError("Hoopit-øktens verifikasjonstid er ugyldig")
    parsed = {
        "id_token": id_token,
        "refresh_token": _safe_secret(value.get("refresh_token"), "Firebase refresh-token"),
        "subject": subject,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "client_fingerprint": fingerprint,
        "token_verified_at": token_verified_at,
        "signing_key_id": signing_key_id,
    }
    if version == 2:
        session_binding = value.get("session_binding")
        if not isinstance(session_binding, str) or not _SESSION_BINDING.fullmatch(
            session_binding
        ):
            raise HoopitAuthError("Hoopit-økten har ugyldig lokal sesjonsbinding")
        parsed["session_binding"] = session_binding
    return parsed


def _read_session(
    root: Path | str | None, *, at: float
) -> tuple[ClientConfig, dict[str, Any]]:
    config = load_client_config(root)
    value = _read_mapping(session_path(root), "Hoopit-økten mangler")
    return config, _parse_session(config, value, at=at)


def _now(value: float | None) -> float:
    result = time.time() if value is None else value
    if (
        isinstance(result, bool)
        or not isinstance(result, (int, float))
        or not math.isfinite(float(result))
        or float(result) <= 0
    ):
        raise HoopitAuthError("Tidspunktet for Hoopit-økten er ugyldig")
    return float(result)


def _parse_proof_time(value: Any, *, at: float) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise HoopitAuthError("Hoopit live-gate-beviset har ugyldig tidspunkt")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HoopitAuthError("Hoopit live-gate-beviset har ugyldig tidspunkt") from None
    if parsed.tzinfo is None or parsed.timestamp() > at + 60:
        raise HoopitAuthError("Hoopit live-gate-beviset har ugyldig tidspunkt")
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _direct_access_proof_status(
    root: Path | str | None,
    config: ClientConfig,
    session: Mapping[str, Any],
    *,
    at: float,
) -> dict[str, Any]:
    path = direct_access_proof_path(root)
    if not path.exists() and not path.is_symlink():
        return {
            "direct_access_proof_present": False,
            "direct_access_verified": False,
            "direct_access_verified_at": None,
            "direct_access_proof_state": "missing",
        }
    try:
        value = _read_mapping(path, "Hoopit live-gate-beviset mangler")
        if set(value) != _DIRECT_ACCESS_PROOF_FIELDS or value.get("version") != 1:
            raise HoopitAuthError("Hoopit live-gate-beviset har ugyldig format")
        verified_at = _parse_proof_time(value.get("verified_at"), at=at)
        fingerprints: dict[str, str] = {}
        for name in (
            "auth_identity_fingerprint",
            "app_release_fingerprint",
            "session_identity_fingerprint",
        ):
            candidate = value.get(name)
            if not isinstance(candidate, str) or not _SHA256.fullmatch(candidate):
                raise HoopitAuthError("Hoopit live-gate-beviset har ugyldig format")
            fingerprints[name] = candidate
        current = _direct_access_binding(root, config, session)
        matches = all(
            fingerprint_matches(fingerprints[name], (getattr(current, name),))
            for name in fingerprints
        )
    except (HoopitAuthError, PrivateStateError, OSError, OverflowError, ValueError):
        return {
            "direct_access_proof_present": True,
            "direct_access_verified": False,
            "direct_access_verified_at": None,
            "direct_access_proof_state": "invalid",
        }
    return {
        "direct_access_proof_present": True,
        "direct_access_verified": matches,
        "direct_access_verified_at": verified_at if matches else None,
        "direct_access_proof_state": "verified" if matches else "mismatch",
    }


def _record_direct_access_proof(
    root: Path | str | None,
    access: HoopitAccess,
    *,
    at: float | None = None,
) -> dict[str, Any]:
    """Persist a local proof only when the just-read access is still current."""

    instant = _now(at)
    with private_source_lock(root, SOURCE):
        config, session = _read_session(root, at=instant)
        if not hmac.compare_digest(
            access.id_token.encode(), str(session["id_token"]).encode()
        ) or not hmac.compare_digest(
            access.app_version.encode(), config.app_version.encode()
        ):
            raise HoopitAuthError(
                "Hoopit-økten eller appreleasen ble endret under live-gaten"
            )
        binding = _direct_access_binding(root, config, session)
        verified_at = utc_iso(instant)
        value = {
            "version": 1,
            "verified_at": verified_at,
            "auth_identity_fingerprint": binding.auth_identity_fingerprint,
            "app_release_fingerprint": binding.app_release_fingerprint,
            "session_identity_fingerprint": binding.session_identity_fingerprint,
        }
        try:
            atomic_write_private_json(direct_access_proof_path(root), value)
            remove_private_file(_direct_access_rejection_path(root))
        except PrivateStateError as exc:
            raise HoopitAuthError(str(exc)) from None
    return {"recorded": True, "verified_at": verified_at}


def _record_direct_access_rejection(root, access, *, at=None):
    """Remember a denied API gate without conflating it with Firebase login."""
    instant = _now(at)
    with private_source_lock(root, SOURCE):
        config, session = _read_session(root, at=instant)
        if not hmac.compare_digest(access.id_token.encode(), str(session['id_token']).encode()) or not hmac.compare_digest(access.app_version.encode(), config.app_version.encode()):
            raise HoopitReadAccessUnverified('Hoopit-økten eller appreleasen ble endret under verifiseringen.')
        binding = _direct_access_binding(root, config, session)
        atomic_write_private_json(_direct_access_rejection_path(root), {
            'version': 1,
            'rejected_at': utc_iso(instant),
            'auth_identity_fingerprint': binding.auth_identity_fingerprint,
            'app_release_fingerprint': binding.app_release_fingerprint,
            'session_identity_fingerprint': binding.session_identity_fingerprint,
        })
    return utc_iso(instant + DIRECT_ACCESS_RETRY_SECONDS)


def _direct_access_rejected(root, *, at=None):
    """Do not blindly repeat a denied gate for the same app and imported grant."""
    with private_source_lock(root, SOURCE):
        target = _direct_access_rejection_path(root)
        if not target.exists() and not target.is_symlink():
            return None
        try:
            config, session = _read_session(root, at=_now(at))
            binding = _direct_access_binding(root, config, session)
            value = read_private_json(target)
            fields = {'auth_identity_fingerprint', 'app_release_fingerprint',
                      'session_identity_fingerprint'}
            if not isinstance(value, Mapping) or set(value) != fields | {'version', 'rejected_at'} or value['version'] != 1:
                raise HoopitAuthError('Ugyldig status for Hoopit-lesetilgang')
            if any(not isinstance(value[k], str) or not _SHA256.fullmatch(value[k]) for k in fields):
                raise HoopitAuthError('Ugyldig binding for Hoopit-lesetilgang')
            rejected_at = datetime.fromisoformat(_parse_proof_time(value['rejected_at'], at=_now(at))).timestamp()
            retry_at = rejected_at + DIRECT_ACCESS_RETRY_SECONDS
            return (utc_iso(retry_at) if retry_at > _now(at)
                    and all(fingerprint_matches(value[k], (getattr(binding, k),)) for k in fields) else None)
        except (HoopitAuthError, PrivateStateError, OSError, ValueError):
            raise HoopitReadAccessUnverified('Appens lagrede Hoopit-verifisering kunne ikke leses.') from None


def _invalidate_direct_access_proof(root: Path | str | None) -> bool:
    """Remove a prior success before or after a newly observed auth failure."""

    with private_source_lock(root, SOURCE):
        try:
            return remove_private_file(direct_access_proof_path(root))
        except PrivateStateError as exc:
            raise HoopitAuthError(str(exc)) from None


def _require_direct_access_proof(
    root: Path | str | None, *, at: float | None = None
) -> None:
    """Fail closed before collection unless the current local binding passed live."""

    instant = _now(at)
    with private_source_lock(root, SOURCE):
        try:
            config, session = _read_session(root, at=instant)
        except HoopitAuthError:
            raise HoopitReauthenticationRequired(
                "Hoopit-økten mangler eller er ugyldig"
            ) from None
        proof = _direct_access_proof_status(root, config, session, at=instant)
        if not proof["direct_access_verified"]:
            raise HoopitReadAccessUnverified(
                "Appens lesetilgang til Hoopit må verifiseres med den lagrede økten."
            )


def _import_credential(value: Mapping[str, Any]) -> SessionCredential:
    if set(value) != _IMPORT_FIELDS:
        raise HoopitAuthError(
            "Hoopit-tokeneksporten må bare inneholde id_token og refresh_token"
        )
    return SessionCredential(
        _safe_secret(value.get("id_token"), "Firebase ID-token"),
        _safe_secret(value.get("refresh_token"), "Firebase refresh-token"),
    )


@lifecycle.operation('hoopit', 'import')
def import_session(
    root: Path | str | None,
    session_file: Path | str,
    *,
    replace_existing: bool = False,
    verification_transport: TokenVerificationTransport | None = None,
    rate_limiter: RateLimitController | None = None,
    at: float | None = None,
) -> dict[str, Any]:
    """Verify and atomically import an existing first-party Firebase token pair."""

    instant = _now(at)
    source = Path(session_file).expanduser()
    credential = _import_credential(
        _read_mapping(source, "Fant ikke Hoopit-tokeneksporten")
    )
    with private_source_lock(root, SOURCE):
        config = load_client_config(root)
        target = session_path(root)
        if (target.exists() or target.is_symlink()) and not replace_existing:
            raise HoopitAuthError(
                "Hoopit-økten finnes allerede; eksplisitt erstatning kreves"
            )
        limiter = rate_limiter or _default_rate_limiter(root)
        certificates = _fetch_certificates(
            verification_transport or _default_verification_transport(), limiter
        )
        verified = _verified_id_token(credential.id_token, certificates, at=instant)
        value = _session_value(
            config,
            credential,
            verified,
            at=instant,
            session_binding=secrets.token_hex(32),
        )
        try:
            atomic_write_private_json(target, value)
            remove_private_file(refresh_state_path(root))
            remove_private_file(direct_access_proof_path(root))
            remove_private_file(_direct_access_rejection_path(root))
        except PrivateStateError as exc:
            raise HoopitAuthError(str(exc)) from None
    return {
        "configured": True,
        "authenticated": True,
        "firebase_verified": True,
        "expires_at": utc_iso(verified.expires_at),
        "attachment_hosts": len(config.attachment_hosts),
    }


def _refresh_rejected(root: Path | str | None) -> bool:
    path = refresh_state_path(root)
    if not path.exists() and not path.is_symlink():
        return False
    try:
        value = _read_mapping(path, "Hoopit-refreshmarkøren mangler")
    except HoopitAuthError:
        return True
    return set(value) == {"reason", "recorded_at"} and value.get("reason") == "refresh-rejected"


def _mark_refresh_rejected(root: Path | str | None, *, at: float) -> None:
    try:
        atomic_write_private_json(
            refresh_state_path(root),
            {"reason": "refresh-rejected", "recorded_at": utc_iso(at)},
        )
        remove_private_file(direct_access_proof_path(root))
    except PrivateStateError as exc:
        raise HoopitAuthError(str(exc)) from None


def _default_refresh_transport(config: ClientConfig) -> ApiTransport:
    return ApiTransport(
        allowed_urls={f"{SECURE_TOKEN_ROOT}?key={config.firebase_api_key}"},
        timeout=45,
        max_json_bytes=MAX_PRIVATE_JSON_BYTES,
        user_agent="familiekilder-hoopit-auth/0.2",
    )


@lifecycle.operation('hoopit', 'refresh')
def refresh_session(
    root: Path | str | None,
    *,
    transport: RefreshTransport | None = None,
    verification_transport: TokenVerificationTransport | None = None,
    rate_limiter: RateLimitController | None = None,
    at: float | None = None,
) -> dict[str, Any]:
    """Refresh once without an in-process retry or a second provider attempt."""

    instant = _now(at)
    with private_source_lock(root, SOURCE):
        try:
            config, session = _read_session(root, at=instant)
        except HoopitAuthError:
            raise HoopitReauthenticationRequired(
                "Hoopit-økten mangler eller er ugyldig"
            ) from None
        if _refresh_rejected(root):
            raise HoopitReauthenticationRequired(
                "Hoopit-refresh-tokenet er tidligere avvist"
            )
        journal = lifecycle._read(lifecycle._path(root, SOURCE, 'refresh-response'))
        saved = lifecycle.saved_response(root, SOURCE, session, journal.get('transaction_id'))
        requested_at = instant
        limiter = rate_limiter or _default_rate_limiter(root)
        certificates = _fetch_certificates(
            verification_transport or _default_verification_transport(), limiter
        )
        if saved is not None:
            response = saved['response']
            instant = saved['received_at']
            lifecycle.record(root, SOURCE, 'response_recovered', at=requested_at)
        else:
            limiter.before_request()
            try:
                response = lifecycle.observe_transport(root, SOURCE, transport or _default_refresh_transport(config), at=instant, capture_response=False).post_form(
                    f"{SECURE_TOKEN_ROOT}?key={config.firebase_api_key}",
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": str(session["refresh_token"]),
                    },
                )
            except ApiHTTPError as exc:
                if exc.status_code in {400, 401, 403}:
                    limiter.record_http_failure(exc.status_code, exc.retry_after_seconds)
                    _mark_refresh_rejected(root, at=instant)
                    raise HoopitReauthenticationRequired(
                        "Hoopit avviste den lagrede refresh-økten"
                    ) from None
                _record_remote_failure(limiter, exc, operation='refresh')
            except (ApiTransportError, OSError, TimeoutError) as exc:
                _record_remote_failure(limiter, exc, operation='refresh')
            except Exception as exc:
                _record_remote_failure(limiter, exc, operation='refresh')
            limiter.record_success()
            if not isinstance(response, Mapping):
                raise SourceUnavailable("Hoopit-refresh returnerte ugyldig svar")
            lifecycle.save_response(root, SOURCE, session, secrets.token_hex(32), response, at=instant)
        try:
            id_token = _safe_secret(response.get("id_token"), "Firebase ID-token")
            refresh_token = _safe_secret(
                response.get("refresh_token"), "Firebase refresh-token"
            )
            verified = _verified_id_token(id_token, certificates, at=instant)
            expires_in = _expires_in(response.get("expires_in"))
            response_subject = response.get("user_id")
            if verified.subject != session["subject"] or response_subject not in {
                None,
                verified.subject,
            }:
                raise HoopitAuthError("Hoopit-refresh endret brukerbinding")
            if abs(verified.expires_at - (instant + expires_in)) > 120:
                raise HoopitAuthError("Hoopit-refresh har inkonsistent utløpstid")
            value = _session_value(
                config,
                SessionCredential(id_token, refresh_token),
                verified,
                at=instant,
                session_binding=str(
                    session.get("session_binding") or secrets.token_hex(32)
                ),
            )
            atomic_write_private_json(session_path(root), value)
        except (HoopitAuthError, PrivateStateError):
            raise SourceUnavailable(
                "Hoopit-refresh kunne ikke valideres; gammel økt er beholdt"
            ) from None
        lifecycle.clear_response(root, SOURCE)
        if saved is not None and verified.expires_at <= requested_at + REFRESH_SKEW_SECONDS:
            return refresh_session(root, transport=transport, verification_transport=verification_transport,
                                   rate_limiter=limiter, at=requested_at)
        return {
            "authenticated": True,
            "firebase_verified": True,
            "expires_at": utc_iso(verified.expires_at),
            "refresh_token_rotated": not hmac.compare_digest(
                str(session["refresh_token"]).encode(), refresh_token.encode()
            ),
        }


def session_for_use(
    root: Path | str | None,
    *,
    transport: RefreshTransport | None = None,
    verification_transport: TokenVerificationTransport | None = None,
    rate_limiter: RateLimitController | None = None,
    at: float | None = None,
) -> HoopitAccess:
    """Return a current ID token, refreshing the imported grant when needed."""

    instant = _now(at)
    with private_source_lock(root, SOURCE):
        try:
            config, session = _read_session(root, at=instant)
        except HoopitAuthError:
            raise HoopitReauthenticationRequired(
                "Hoopit-økten mangler eller er ugyldig"
            ) from None
        if _refresh_rejected(root):
            raise HoopitReauthenticationRequired(
                "Hoopit-refresh-tokenet er tidligere avvist"
            )
        if float(session["expires_at"]) <= instant + REFRESH_SKEW_SECONDS:
            refresh_session(
                root,
                transport=transport,
                verification_transport=verification_transport,
                rate_limiter=rate_limiter,
                at=instant,
            )
            config, session = _read_session(root, at=instant)
        return HoopitAccess(
            id_token=str(session["id_token"]),
            app_version=config.app_version,
            expires_at=float(session["expires_at"]),
            attachment_hosts=config.attachment_hosts,
        )


def _read_cooldown(root: Path | str | None, at: float) -> str | None:
    path = rate_state_path(root)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        value = read_private_json(path, max_bytes=64 * 1024)
    except PrivateStateError:
        raise HoopitAuthError("Hoopit cooldown-tilstanden kunne ikke leses") from None
    if not isinstance(value, Mapping):
        raise HoopitAuthError("Hoopit cooldown-tilstanden har ugyldig format")
    raw = value.get("cooldown_until")
    if raw is None:
        return None
    expiry = _number(raw, "cooldown", minimum=0)
    return utc_iso(expiry) if expiry > at else None


def auth_status(root: Path | str | None, *, at: float | None = None) -> dict[str, Any]:
    """Return a fixed-shape, secret-free and strictly offline status snapshot."""

    instant = _now(at)
    result: dict[str, Any] = {
        "configured": False,
        "authenticated": False,
        "firebase_verified": False,
        "session_present": False,
        "expires_at": None,
        "access_expires_at": None,
        "access_expired": None,
        "refresh_blocked": False,
        "refresh_block_reason": None,
        "direct_access_proof_present": False,
        "direct_access_verified": False,
        "direct_access_verified_at": None,
        "direct_access_proof_state": "missing",
        "cooldown_until": None,
        "status_unavailable": False,
    }
    try:
        config_path = client_config_path(root)
        configured = config_path.exists() or config_path.is_symlink()
        if configured:
            load_client_config(root)
            result["configured"] = True
        target = session_path(root)
        result["session_present"] = target.exists() or target.is_symlink()
        proof_target = direct_access_proof_path(root)
        result["direct_access_proof_present"] = (
            proof_target.exists() or proof_target.is_symlink()
        )
        if result["session_present"] and configured:
            config, session = _read_session(root, at=instant)
            expiry = float(session["expires_at"])
            result["expires_at"] = utc_iso(expiry)
            result["access_expires_at"] = result["expires_at"]
            result["access_expired"] = expiry <= instant
            blocked = _refresh_rejected(root)
            result["refresh_blocked"] = blocked
            result["refresh_block_reason"] = "refresh-rejected" if blocked else None
            result["firebase_verified"] = True
            result["authenticated"] = not blocked
            result.update(
                _direct_access_proof_status(root, config, session, at=instant)
            )
        result["cooldown_until"] = _read_cooldown(root, instant)
    except (
        HoopitAuthError,
        PrivateStateError,
        OSError,
        OverflowError,
        ValueError,
    ):
        result["authenticated"] = False
        result["firebase_verified"] = False
        result["status_unavailable"] = True
    return result


@lifecycle.operation('hoopit', 'logout')
def logout(root: Path | str | None) -> dict[str, Any]:
    """Remove local Hoopit credentials only; do not mutate the provider session."""

    with private_source_lock(root, SOURCE):
        try:
            removed_session = remove_private_file(session_path(root))
            removed_marker = remove_private_file(refresh_state_path(root))
            removed_proof = remove_private_file(direct_access_proof_path(root))
            remove_private_file(_direct_access_rejection_path(root))
        except PrivateStateError as exc:
            raise HoopitAuthError(str(exc)) from None
    return {
        "authenticated": False,
        "session_removed": removed_session,
        "refresh_state_removed": removed_marker,
        "direct_access_proof_removed": removed_proof,
        "remote_revocation_attempted": False,
    }


__all__ = [
    "API_ROOT",
    "ClientConfig",
    "DirectAccessProofBinding",
    "FIREBASE_CERTS_URL",
    "FIREBASE_PROJECT_ID",
    "HoopitAccess",
    "HoopitAuthError",
    "HoopitReauthenticationRequired",
    "RateLimitController",
    "RefreshTransport",
    "SECURE_TOKEN_ROOT",
    "SessionCredential",
    "TokenVerificationTransport",
    "auth_status",
    "client_config_path",
    "configure_client",
    "direct_access_proof_path",
    "import_session",
    "load_client_config",
    "logout",
    "rate_state_path",
    "refresh_session",
    "refresh_state_path",
    "session_for_use",
    "session_path",
]
