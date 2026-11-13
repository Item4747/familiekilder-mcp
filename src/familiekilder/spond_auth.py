"""Private rotating session state for the read-only Spond web API adapter.

Spond's first-party web client keeps an access/refresh token pair and rotates
both values with ``POST /core/v1/auth2/login/refresh``.  This module mirrors
that narrow boundary.  It never accepts passwords or browser cookies, never
prints secret-bearing values, and only imports a user-operated token export
from an owned ``0600`` file below a literal ``.private`` directory.

An import is staged with one read-only profile request before it replaces an
existing session.  Refresh is deliberately single-attempt: an ambiguous result
leaves a durable marker so a potentially consumed rotating token is not reused.
"""

from __future__ import annotations

from . import auth_lifecycle as lifecycle
from .api_transport import ApiRequestNotSent

import hmac
import ipaddress
import math
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .api_transport import ApiHTTPError, ApiTransport, ApiTransportError
from .auth_common import is_plain_text, utc_iso
from .core import FamiliekilderError
from .private_lock import private_source_lock
from .private_state import (
    PrivateStateError,
    atomic_write_private_json,
    credential_path,
    read_private_json,
    remove_private_file,
)
from .source_adapter import (
    SourceFatalConfigurationError,
    SourceLoginRequired,
    SourceUnavailable,
)


SOURCE = "spond"
API_ROOT = "https://api.spond.com/core/v1"
PROFILE_ENDPOINT = f"{API_ROOT}/profile"
REFRESH_ENDPOINT = f"{API_ROOT}/auth2/login/refresh"

CLIENT_FILE = "spond-provider.json"
SESSION_FILE = "spond-session.json"
REFRESH_BLOCK_FILE = "spond-refresh-blocked.json"
COOLDOWN_FILE = "spond-cooldown.json"
PACING_FILE = "spond-pacing.json"
MAX_PRIVATE_JSON_BYTES = 1024 * 1024
REFRESH_SKEW_SECONDS = 60
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 60 * 60
MIN_RATE_LIMIT_COOLDOWN_SECONDS = 5 * 60
DEFAULT_SERVER_ERROR_COOLDOWN_SECONDS = 15 * 60
MAX_COOLDOWN_SECONDS = 366 * 24 * 60 * 60
MIN_REQUEST_INTERVAL_SECONDS = 2.0

_REFRESH_REASONS = frozenset({"refresh-in-flight", "refresh-rejected"})
_COOLDOWN_REASONS = frozenset({"rate-limited", "provider-error"})
_CLIENT_FIELDS = frozenset({"api_root", "app_version", "attachment_hosts"})


class SpondAuthError(FamiliekilderError):
    """A sanitized local or Spond authentication failure."""


class SpondReauthenticationRequired(SourceLoginRequired, SpondAuthError):
    """The local Spond grant is missing, rejected, expired, or unsafe."""


class SpondCooldownActive(SourceUnavailable, SpondAuthError):
    """Provider-directed or defensive network cooldown is still active."""


class SpondProviderConfigurationRequired(
    SourceFatalConfigurationError, SpondAuthError
):
    """The signed Spond provider profile is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class ClientConfig:
    """Verified, non-user-specific Spond runtime metadata."""

    api_root: str
    app_version: str
    attachment_hosts: tuple[str, ...]

    def as_private_json(self) -> dict[str, Any]:
        return {
            "api_root": self.api_root,
            "app_version": self.app_version,
            "attachment_hosts": list(self.attachment_hosts),
        }


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
    ) -> Any: ...


def client_config_path(root: Path | str | None) -> Path:
    return credential_path(root, CLIENT_FILE)


def session_path(root: Path | str | None) -> Path:
    return credential_path(root, SESSION_FILE)


def refresh_block_path(root: Path | str | None) -> Path:
    return credential_path(root, REFRESH_BLOCK_FILE)


def cooldown_path(root: Path | str | None) -> Path:
    return credential_path(root, COOLDOWN_FILE)


def pacing_path(root: Path | str | None) -> Path:
    return credential_path(root, PACING_FILE)


def provider_user_agent(config: ClientConfig) -> str:
    """Represent the signed app release consistently on Spond requests."""

    return f"familiekilder-spond/0.2 (Spond {config.app_version})"


def _default_transport(config: ClientConfig) -> ApiTransport:
    return ApiTransport(
        allowed_urls={
            f"{config.api_root}/profile",
            f"{config.api_root}/auth2/login/refresh",
        },
        timeout=30,
        max_json_bytes=MAX_PRIVATE_JSON_BYTES,
        user_agent=provider_user_agent(config),
    )


def _timestamp(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise SpondAuthError(f"Spond-økten har ugyldig {field_name}")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str) and value and len(value) <= 128:
        try:
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                raise ValueError
            result = parsed.timestamp()
        except (OverflowError, ValueError):
            try:
                result = float(value)
            except ValueError:
                raise SpondAuthError(
                    f"Spond-økten har ugyldig {field_name}"
                ) from None
    else:
        raise SpondAuthError(f"Spond-økten har ugyldig {field_name}")
    if not math.isfinite(result) or result <= 0:
        raise SpondAuthError(f"Spond-økten har ugyldig {field_name}")
    return result


def _current_time(value: float | None, label: str) -> float:
    result = time.time() if value is None else value
    if (
        isinstance(result, bool)
        or not isinstance(result, (int, float))
        or not math.isfinite(float(result))
        or float(result) <= 0
    ):
        raise SpondAuthError(f"Ugyldig tidspunkt for {label}")
    return float(result)


def _token(value: Any, field_name: str) -> str:
    if not is_plain_text(value, maximum=64 * 1024, allow_space=False):
        raise SpondAuthError(f"Spond-økten har ugyldig {field_name}")
    return value


def _provider_text(value: Any, label: str, *, maximum: int) -> str:
    if not is_plain_text(value, maximum=maximum):
        raise SpondAuthError(f"Spond-providerprofilen har ugyldig {label}")
    return value


def _attachment_host(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 253
        or "/" in value
        or ":" in value
        or "*" in value
        or value.startswith(".")
    ):
        raise SpondAuthError("Spond-providerprofilen har ugyldig vedleggsvert")
    try:
        normalized = value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        raise SpondAuthError(
            "Spond-providerprofilen har ugyldig vedleggsvert"
        ) from None
    if (
        not normalized
        or normalized == "localhost"
        or normalized.endswith(".localhost")
        or ".." in normalized
        or re.fullmatch(
            r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", normalized
        )
        is None
        or any(len(label) > 63 for label in normalized.split("."))
    ):
        raise SpondAuthError("Spond-providerprofilen har ugyldig vedleggsvert")
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise SpondAuthError("Spond-providerprofilen har ugyldig vedleggsvert")
    return normalized


def _attachment_hosts(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SpondAuthError(
            "Spond-providerprofilen har ugyldig vedleggsvertsliste"
        )
    if len(value) > 20:
        raise SpondAuthError("Spond-providerprofilen har for mange vedleggsverter")
    result = sorted(_attachment_host(item) for item in value)
    if len(result) != len(set(result)):
        raise SpondAuthError(
            "Spond-providerprofilen har dupliserte vedleggsverter"
        )
    return result


def _parse_client_config(value: Mapping[str, Any]) -> ClientConfig:
    if set(value) != _CLIENT_FIELDS:
        raise SpondAuthError(
            "Spond-providerprofilen har ukjente eller manglende felt"
        )
    api_root = _provider_text(value.get("api_root"), "API-rot", maximum=200)
    if api_root != API_ROOT:
        raise SpondAuthError(
            "Spond-providerprofilen er ikke bundet til det fastlåste produksjons-API-et"
        )
    return ClientConfig(
        api_root=api_root,
        app_version=_provider_text(
            value.get("app_version"), "appversjon", maximum=100
        ),
        attachment_hosts=tuple(_attachment_hosts(value.get("attachment_hosts"))),
    )


def load_client_config(root: Path | str | None) -> ClientConfig:
    """Read the provisioned profile through the hardened private-file boundary."""

    try:
        value = read_private_json(
            client_config_path(root),
            max_bytes=MAX_PRIVATE_JSON_BYTES,
            missing_message="Spond-providerprofilen mangler",
        )
    except PrivateStateError:
        raise SpondProviderConfigurationRequired(
            "Spond-providerprofilen mangler eller kan ikke leses sikkert"
        ) from None
    if not isinstance(value, Mapping):
        raise SpondProviderConfigurationRequired(
            "Spond-providerprofilen har ugyldig format"
        )
    try:
        return _parse_client_config(value)
    except SpondAuthError:
        raise SpondProviderConfigurationRequired(
            "Spond-providerprofilen er ugyldig"
        ) from None


def configure_client(
    root: Path | str | None, client_file: Path | str
) -> dict[str, Any]:
    """Privately import strict Spond provider metadata for recovery use.

    The provider schema has only one immutable field, and it is pinned to the
    exact production API root by ``_parse_client_config``.  Consequently an
    app-version or attachment-host update never needs to inspect, migrate, or
    rewrite the independently rotating user session.
    """

    source = Path(client_file).expanduser()
    try:
        value = read_private_json(
            source,
            max_bytes=MAX_PRIVATE_JSON_BYTES,
            missing_message="Fant ikke Spond-providerprofilfilen",
        )
    except PrivateStateError:
        raise SpondAuthError(
            "Spond-providerprofilfilen mangler eller kan ikke leses sikkert"
        ) from None
    if not isinstance(value, Mapping):
        raise SpondAuthError("Spond-providerprofilfilen har ugyldig format")
    config = _parse_client_config(value)

    with private_source_lock(root, SOURCE):
        try:
            atomic_write_private_json(
                client_config_path(root), config.as_private_json()
            )
        except PrivateStateError as exc:
            raise SpondAuthError(str(exc)) from None
    return {
        "configured": True,
        "api_root_validated": True,
        "app_version_validated": True,
        "attachment_hosts": len(config.attachment_hosts),
    }


def _session_value(value: Any, *, stored: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SpondAuthError("Spond-økten har ugyldig format")
    import_fields = {
        "access_token",
        "access_expires_at",
        "refresh_token",
        "refresh_expires_at",
        "token",
        "tokenExpiration",
        "refreshToken",
        "refreshTokenExpiration",
    }
    internal_fields = {
        # Accepted only to keep already-created sessions readable.  Provider
        # allowlists are loaded separately and this legacy copy is ignored.
        "attachment_hosts",
        "profile_id",
        "verified_at",
        "session_transaction_id",
        "updated_at",
    }
    allowed = import_fields | internal_fields if stored else import_fields
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise SpondAuthError("Spond-økten inneholder ukjente felt")
    canonical_present = any(
        field in value
        for field in (
            "access_token",
            "access_expires_at",
            "refresh_token",
            "refresh_expires_at",
        )
    )
    browser_present = any(
        field in value
        for field in (
            "token",
            "tokenExpiration",
            "refreshToken",
            "refreshTokenExpiration",
        )
    )
    if canonical_present == browser_present:
        raise SpondAuthError(
            "Spond-økten må bruke enten kanoniske felt eller localStorage-felt"
        )
    if canonical_present:
        required = {
            "access_token",
            "access_expires_at",
            "refresh_token",
            "refresh_expires_at",
        }
        if not required.issubset(value):
            raise SpondAuthError("Spond-økten mangler obligatoriske tokenfelt")
        access_token = value.get("access_token")
        access_expiry = value.get("access_expires_at")
        refresh_token = value.get("refresh_token")
        refresh_expiry = value.get("refresh_expires_at")
    else:
        required = {
            "token",
            "tokenExpiration",
            "refreshToken",
            "refreshTokenExpiration",
        }
        if not required.issubset(value):
            raise SpondAuthError("Spond-økten mangler obligatoriske localStorage-felt")
        access_token = value.get("token")
        access_expiry = value.get("tokenExpiration")
        refresh_token = value.get("refreshToken")
        refresh_expiry = value.get("refreshTokenExpiration")
    result: dict[str, Any] = {
        "access_token": _token(access_token, "access-token"),
        "access_expires_at": _timestamp(access_expiry, "access-utløpstid"),
        "refresh_token": _token(refresh_token, "refresh-token"),
        "refresh_expires_at": _timestamp(refresh_expiry, "refresh-utløpstid"),
    }
    if stored and "attachment_hosts" in value:
        # Validate legacy state so malformed stored JSON still fails closed,
        # but never use it as an attachment authority.
        _attachment_hosts(value.get("attachment_hosts"))
    if result["refresh_expires_at"] < result["access_expires_at"]:
        raise SpondAuthError("Spond-øktens refresh-token utløper før access-tokenet")
    profile_id = value.get("profile_id") if stored else None
    if profile_id is not None:
        result["profile_id"] = _token(profile_id, "profil-ID")
    verified_at = value.get("verified_at") if stored else None
    if verified_at is not None:
        result["verified_at"] = _timestamp(verified_at, "verifiseringstid")
    transaction_id = value.get("session_transaction_id") if stored else None
    if transaction_id is not None:
        result["session_transaction_id"] = _token(
            transaction_id, "sesjonstransaksjon"
        )
    updated_at = value.get("updated_at") if stored else None
    if updated_at is not None:
        result["updated_at"] = _timestamp(updated_at, "oppdateringstid")
    return result


def _read_session(root: Path | str | None) -> dict[str, Any]:
    try:
        value = read_private_json(
            session_path(root),
            max_bytes=MAX_PRIVATE_JSON_BYTES,
            missing_message="Spond-økten mangler",
        )
    except PrivateStateError:
        raise SpondReauthenticationRequired(
            "Spond-økten mangler eller kan ikke leses sikkert"
        ) from None
    try:
        return _session_value(value, stored=True)
    except SpondAuthError:
        raise SpondReauthenticationRequired("Spond-økten er ugyldig") from None


def _read_refresh_marker(root: Path | str | None) -> Mapping[str, Any] | None:
    target = refresh_block_path(root)
    if not target.exists() and not target.is_symlink():
        return None
    try:
        value = read_private_json(
            target,
            max_bytes=4096,
            missing_message="Spond-refreshmarkøren mangler",
        )
    except PrivateStateError:
        return {"reason": "invalid-marker"}
    if (
        not isinstance(value, Mapping)
        or any(
            not isinstance(key, str)
            or key not in {"reason", "recorded_at", "transaction_id"}
            for key in value
        )
        or value.get("reason") not in _REFRESH_REASONS
        or not isinstance(value.get("recorded_at"), str)
    ):
        return {"reason": "invalid-marker"}
    transaction_id = value.get("transaction_id")
    if transaction_id is not None and (
        not isinstance(transaction_id, str)
        or not transaction_id
        or len(transaction_id) > 128
    ):
        return {"reason": "invalid-marker"}
    return {
        "reason": value["reason"],
        "recorded_at": value["recorded_at"],
        **({"transaction_id": transaction_id} if transaction_id is not None else {}),
    }


def _write_refresh_marker(
    root: Path | str | None,
    reason: str,
    *,
    at: float,
    transaction_id: str | None = None,
) -> None:
    if reason not in _REFRESH_REASONS:
        raise SpondAuthError("Spond-refreshmarkøren har ukjent årsak")
    value: dict[str, Any] = {
        "reason": reason,
        "recorded_at": utc_iso(at),
    }
    if transaction_id is not None:
        value["transaction_id"] = transaction_id
    try:
        atomic_write_private_json(refresh_block_path(root), value)
    except PrivateStateError as exc:
        raise SpondAuthError(str(exc)) from None


def _marker_completed(
    marker: Mapping[str, Any], session: Mapping[str, Any]
) -> bool:
    return (
        marker.get("reason") == "refresh-in-flight"
        and isinstance(marker.get("transaction_id"), str)
        and isinstance(session.get("session_transaction_id"), str)
        and hmac.compare_digest(
            str(marker["transaction_id"]).encode(),
            str(session["session_transaction_id"]).encode(),
        )
    )


def _transaction_id() -> str:
    return secrets.token_urlsafe(32)


def _read_cooldown(root: Path | str | None) -> Mapping[str, Any] | None:
    target = cooldown_path(root)
    if not target.exists() and not target.is_symlink():
        return None
    try:
        value = read_private_json(
            target,
            max_bytes=4096,
            missing_message="Spond-cooldown mangler",
        )
    except PrivateStateError:
        return {"reason": "invalid-cooldown"}
    if (
        not isinstance(value, Mapping)
        or any(
            not isinstance(key, str) or key not in {"reason", "until", "status_code"}
            for key in value
        )
    ):
        return {"reason": "invalid-cooldown"}
    reason = value.get("reason")
    until = value.get("until")
    status_code = value.get("status_code")
    if (
        reason not in _COOLDOWN_REASONS
        or isinstance(until, bool)
        or not isinstance(until, (int, float))
        or not math.isfinite(float(until))
        or float(until) <= 0
        or isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not (
            (reason == "rate-limited" and status_code == 429)
            or (reason == "provider-error" and 500 <= status_code <= 599)
        )
    ):
        return {"reason": "invalid-cooldown"}
    return {"reason": reason, "until": float(until), "status_code": status_code}


def _read_last_request(root: Path | str | None) -> float | None:
    target = pacing_path(root)
    if not target.exists() and not target.is_symlink():
        return None
    try:
        value = read_private_json(
            target,
            max_bytes=4096,
            missing_message="Spond-pacingtilstanden mangler",
        )
    except PrivateStateError:
        raise SpondAuthError("Spond-pacingtilstanden kan ikke leses sikkert") from None
    if (
        not isinstance(value, Mapping)
        or set(value) != {"last_request_at"}
        or isinstance(value.get("last_request_at"), bool)
        or not isinstance(value.get("last_request_at"), (int, float))
        or not math.isfinite(float(value["last_request_at"]))
        or float(value["last_request_at"]) <= 0
    ):
        raise SpondAuthError("Spond-pacingtilstanden er ugyldig")
    return float(value["last_request_at"])


def pace_network_request(
    root: Path | str | None,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    minimum_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
) -> None:
    """Durably reserve one serial request slot across processes."""

    if (
        isinstance(minimum_interval, bool)
        or not isinstance(minimum_interval, (int, float))
        or not math.isfinite(float(minimum_interval))
        or float(minimum_interval) < MIN_REQUEST_INTERVAL_SECONDS
    ):
        raise SpondAuthError("Spond-pacingintervallet er ugyldig")
    with private_source_lock(root, SOURCE):
        current = _current_time(clock(), "Spond-pacing")
        last = _read_last_request(root)
        delay = 0.0 if last is None else max(0.0, last + float(minimum_interval) - current)
        if delay > 0:
            sleep(delay)
            after_sleep = _current_time(clock(), "Spond-pacing")
            if after_sleep + 0.001 < current + delay:
                raise SpondAuthError(
                    "Spond-pacingens ventefunksjon flyttet ikke klokken"
                )
            current = after_sleep
        try:
            atomic_write_private_json(
                pacing_path(root), {"last_request_at": current}
            )
        except PrivateStateError as exc:
            raise SpondAuthError(str(exc)) from None


def cooldown_status(
    root: Path | str | None, *, at: float | None = None
) -> dict[str, Any]:
    """Return provider cooldown metadata without creating or mutating state."""

    current = _current_time(at, "Spond-cooldownstatus")
    marker = _read_cooldown(root)
    invalid = marker is not None and not isinstance(marker.get("until"), (int, float))
    until = float(marker["until"]) if marker is not None and not invalid else None
    active = invalid or (until is not None and until > current)
    return {
        "cooldown_active": active,
        "cooldown_until": utc_iso(until) if until is not None else None,
        "cooldown_reason": marker.get("reason") if marker is not None else None,
    }


def assert_network_allowed(
    root: Path | str | None, *, at: float | None = None
) -> None:
    status = cooldown_status(root, at=at)
    if status["cooldown_active"]:
        raise SpondCooldownActive("Spond-kilden er i leverandørpålagt pause")


def record_cooldown(
    root: Path | str | None,
    status_code: int,
    *,
    retry_after_seconds: int | None = None,
    at: float | None = None,
) -> dict[str, Any]:
    """Persist a no-retry boundary after a 429 or transient provider failure."""

    current = _current_time(at, "Spond-cooldown")
    if status_code == 429:
        if (
            isinstance(retry_after_seconds, bool)
            or (
                retry_after_seconds is not None
                and (not isinstance(retry_after_seconds, int) or retry_after_seconds < 0)
            )
        ):
            retry_after_seconds = None
        requested = (
            DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
            if retry_after_seconds is None
            else retry_after_seconds
        )
        delay = min(
            MAX_COOLDOWN_SECONDS,
            max(MIN_RATE_LIMIT_COOLDOWN_SECONDS, requested),
        )
        reason = "rate-limited"
    elif 500 <= status_code <= 599:
        delay = DEFAULT_SERVER_ERROR_COOLDOWN_SECONDS
        reason = "provider-error"
    else:
        raise SpondAuthError("Cooldown kan bare registreres for 429 eller 5xx")
    until = current + delay
    try:
        with private_source_lock(root, SOURCE):
            previous = _read_cooldown(root)
            if previous is not None and isinstance(previous.get("until"), (int, float)):
                until = max(until, float(previous["until"]))
            atomic_write_private_json(
                cooldown_path(root),
                {"reason": reason, "until": until, "status_code": status_code},
            )
    except PrivateStateError as exc:
        raise SpondAuthError(str(exc)) from None
    return {
        "cooldown_active": True,
        "cooldown_until": utc_iso(until),
        "cooldown_reason": reason,
    }


def _profile_id(value: Any) -> str:
    if not isinstance(value, Mapping):
        raise SpondAuthError("Spond returnerte ugyldig profilinformasjon")
    return _token(value.get("id"), "profil-ID")


def _handle_nonrotating_http_error(
    root: Path | str | None, exc: ApiHTTPError, *, at: float
) -> None:
    if exc.status_code == 429:
        record_cooldown(
            root,
            429,
            retry_after_seconds=exc.retry_after_seconds,
            at=at,
        )
        raise SpondCooldownActive("Spond avviste videre trafikk midlertidig") from None
    if 500 <= exc.status_code <= 599:
        record_cooldown(root, exc.status_code, at=at)
        raise SpondCooldownActive("Spond er midlertidig utilgjengelig") from None
    if exc.status_code in {401, 403}:
        raise SpondReauthenticationRequired("Spond-tokenet ble avvist") from None
    raise SpondAuthError("Spond avviste den lesende profilkontrollen") from None


@lifecycle.operation('spond', 'import')
def import_session(
    root: Path | str | None,
    session_file: Path | str,
    *,
    replace_existing: bool = False,
    transport: AuthTransport | None = None,
    at: float | None = None,
    pacing_clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    minimum_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Stage and atomically import a token export; never accept browser cookies."""

    current = _current_time(at, "Spond-import")
    # Provider metadata must be present and safe before a secret-bearing import
    # file is opened.  It is read again under the source lock below so a bundle
    # update cannot race the remote probe.
    load_client_config(root)
    try:
        raw = read_private_json(
            Path(session_file).expanduser(),
            max_bytes=MAX_PRIVATE_JSON_BYTES,
            missing_message="Fant ikke Spond-tokeneksporten",
        )
    except PrivateStateError:
        raise SpondAuthError(
            "Spond-tokeneksporten mangler eller kan ikke leses sikkert"
        ) from None
    candidate = _session_value(raw, stored=False)
    if candidate["refresh_expires_at"] <= current:
        raise SpondReauthenticationRequired("Spond-refresh-tokenet er allerede utløpt")
    target = session_path(root)
    with private_source_lock(root, SOURCE):
        config = load_client_config(root)
        present = target.exists() or target.is_symlink()
        if present and not replace_existing:
            raise SpondAuthError(
                "Spond-økten finnes allerede; eksplisitt erstatning kreves"
            )
        if candidate["access_expires_at"] <= current:
            raise SpondReauthenticationRequired(
                "Eksporter en fersk Spond-økt før fjernvalidering"
            )
        assert_network_allowed(root, at=current)
        pace_network_request(
            root,
            clock=pacing_clock,
            sleep=sleep,
            minimum_interval=minimum_interval,
        )
        client = transport or _default_transport(config)
        try:
            profile_id = _profile_id(
                client.get_json(
                    f"{config.api_root}/profile",
                    bearer_token=str(candidate["access_token"]),
                )
            )
        except ApiHTTPError as exc:
            _handle_nonrotating_http_error(root, exc, at=current)
        except (ApiTransportError, OSError):
            record_cooldown(root, 503, at=current)
            raise SpondAuthError(
                "Spond-økten kunne ikke fjernvalideres; gammel økt er beholdt"
            ) from None
        # Import is a non-rotating operation.  Do not create an in-flight
        # marker: atomic_write_private_json keeps the old session intact until
        # the replacement is fully committed.
        candidate["updated_at"] = current
        candidate["profile_id"] = profile_id
        candidate["verified_at"] = current
        try:
            atomic_write_private_json(target, candidate)
        except PrivateStateError as exc:
            raise SpondAuthError(str(exc)) from None
        cleanup_pending = False
        if _read_refresh_marker(root) is not None:
            try:
                remove_private_file(refresh_block_path(root))
            except PrivateStateError:
                cleanup_pending = True
    return {
        "configured": True,
        "authenticated": True,
        "remote_verified": True,
        "expires_at": utc_iso(float(candidate["access_expires_at"])),
        "refresh_expires_at": utc_iso(float(candidate["refresh_expires_at"])),
        "attachment_hosts": len(config.attachment_hosts),
        "refresh_marker_cleanup_pending": cleanup_pending,
    }


def local_session_for_use(
    root: Path | str | None, *, at: float | None = None
) -> dict[str, Any]:
    """Return validated secret-bearing state solely to the collector/auth layer."""

    config = load_client_config(root)
    session = _read_session(root)
    marker = _read_refresh_marker(root)
    if marker is not None:
        if _marker_completed(marker, session):
            try:
                remove_private_file(refresh_block_path(root))
            except PrivateStateError:
                pass
        else:
            raise SpondReauthenticationRequired(
                "En tidligere Spond-refresh fikk ukjent eller avvist resultat"
            )
    current = _current_time(at, "Spond-øktbruk")
    if session["refresh_expires_at"] <= current:
        raise SpondReauthenticationRequired("Spond-refresh-tokenet er utløpt")
    # Operational metadata is projected into memory only.  Updating a signed
    # provider bundle therefore takes effect immediately without rewriting the
    # independently rotating token line.
    session["api_root"] = config.api_root
    session["app_version"] = config.app_version
    session["attachment_hosts"] = list(config.attachment_hosts)
    return session


def _rotated_tokens(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SpondReauthenticationRequired("Spond-refresh returnerte ugyldige data")
    access = value.get("accessToken")
    refresh = value.get("refreshToken")
    if not isinstance(access, Mapping) or not isinstance(refresh, Mapping):
        raise SpondReauthenticationRequired("Spond-refresh returnerte ugyldige data")
    return {
        "access_token": _token(access.get("token"), "access-token"),
        "access_expires_at": _timestamp(
            access.get("expiration"), "access-utløpstid"
        ),
        "refresh_token": _token(refresh.get("token"), "refresh-token"),
        "refresh_expires_at": _timestamp(
            refresh.get("expiration"), "refresh-utløpstid"
        ),
    }


@lifecycle.operation('spond', 'refresh')
def refresh_session(
    root: Path | str | None,
    *,
    transport: AuthTransport | None = None,
    at: float | None = None,
    before_request: Callable[[], None] | None = None,
    pacing_clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    minimum_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Rotate once, never retrying an ambiguous or rate-limited request."""

    current = _current_time(at, "Spond-refresh")
    with private_source_lock(root, SOURCE):
        config = load_client_config(root)
        session = _read_session(root)
        session['api_root'] = config.api_root
        session['app_version'] = config.app_version
        session['attachment_hosts'] = list(config.attachment_hosts)
        marker = _read_refresh_marker(root) or {}
        saved = lifecycle.saved_response(root, SOURCE, session, marker.get('transaction_id')) if marker.get('reason') == 'refresh-in-flight' else None
        if saved is None:
            session = local_session_for_use(root, at=current)
        requested_at = current
        if saved is not None:
            response = saved['response']
            transaction_id = marker['transaction_id']
            current = saved['received_at']
            lifecycle.record(root, SOURCE, 'response_recovered', at=requested_at)
        else:
            assert_network_allowed(root, at=current)
            if before_request is not None:
                before_request()
            else:
                pace_network_request(
                    root,
                    clock=pacing_clock,
                    sleep=sleep,
                    minimum_interval=minimum_interval,
                )
            transaction_id = _transaction_id()
            _write_refresh_marker(
                root,
                "refresh-in-flight",
                at=current,
                transaction_id=transaction_id,
            )
            client = lifecycle.observe_transport(root, SOURCE, transport or _default_transport(config), at=current, capture_response=False)
            try:
                response = client.post_json(
                    f"{config.api_root}/auth2/login/refresh",
                    {"value": str(session["refresh_token"])},
                )
            except ApiRequestNotSent:
                remove_private_file(refresh_block_path(root))
                raise SourceUnavailable('Spond-fornyelsen ble ikke sendt; prøv ved neste runde') from None
            except ApiHTTPError as exc:
                if exc.status_code == 429:
                    record_cooldown(
                        root,
                        429,
                        retry_after_seconds=exc.retry_after_seconds,
                        at=current,
                    )
                    try:
                        remove_private_file(refresh_block_path(root))
                    except PrivateStateError:
                        pass
                    raise SpondCooldownActive(
                        "Spond avviste videre trafikk midlertidig"
                    ) from None
                if 500 <= exc.status_code <= 599:
                    record_cooldown(root, exc.status_code, at=current)
                    raise SpondReauthenticationRequired(
                        "Spond-refresh fikk et ukjent resultat"
                    ) from None
                _write_refresh_marker(root, "refresh-rejected", at=current)
                raise SpondReauthenticationRequired("Spond-refresh-tokenet ble avvist") from None
            except (ApiTransportError, OSError):
                # The request may or may not have reached the provider.  Preserve
                # the in-flight marker for the rotating grant and also prevent a
                # new process from immediately adding more traffic.
                record_cooldown(root, 503, at=current)
                raise SpondReauthenticationRequired(
                    "Spond-refresh fikk et ukjent resultat"
                ) from None
            lifecycle.save_response(root, SOURCE, session, transaction_id, response, at=current)
        try:
            tokens = _rotated_tokens(response)
        except SpondAuthError:
            # Keep the in-flight marker: the server may already have consumed
            # the rotating token even though its response was unusable.
            raise SpondReauthenticationRequired(
                "Spond-refresh returnerte ugyldige data"
            ) from None
        if (
            tokens["access_expires_at"] <= current + REFRESH_SKEW_SECONDS
            or tokens["refresh_expires_at"] <= current + REFRESH_SKEW_SECONDS
        ):
            # Never commit a rotated pair that would cause an immediate second
            # refresh.  The in-flight marker remains because the old rotating
            # grant may already have been consumed.
            raise SpondReauthenticationRequired(
                "Spond-refresh returnerte allerede utløpte token"
            )
        if tokens["refresh_expires_at"] < tokens["access_expires_at"]:
            raise SpondReauthenticationRequired(
                "Spond-refresh returnerte inkonsistente utløpstider"
            )
        value = {
            **tokens,
            "session_transaction_id": transaction_id,
            "updated_at": current,
        }
        profile_binding_preserved = False
        if isinstance(session.get("profile_id"), str):
            value["profile_id"] = session["profile_id"]
            if isinstance(session.get("verified_at"), (int, float)):
                # A successful rotation continues the already profile-probed
                # grant lineage; dropping this binding would make the offline
                # status reads falsely demand a destructive re-import.
                value["verified_at"] = session["verified_at"]
                profile_binding_preserved = True
        try:
            atomic_write_private_json(session_path(root), value)
        except PrivateStateError:
            raise SpondReauthenticationRequired(
                "Den roterte Spond-økten kunne ikke lagres sikkert"
            ) from None
        lifecycle.clear_response(root, SOURCE)
        cleanup_pending = False
        try:
            remove_private_file(refresh_block_path(root))
        except PrivateStateError:
            cleanup_pending = True
    if saved is not None and tokens['access_expires_at'] <= requested_at + REFRESH_SKEW_SECONDS:
        return refresh_session(root, transport=transport, at=requested_at, before_request=before_request,
                               pacing_clock=pacing_clock, sleep=sleep, minimum_interval=minimum_interval)
    return {
        "refreshed": True,
        "refresh_token_rotated": not hmac.compare_digest(
            str(session["refresh_token"]).encode(),
            str(tokens["refresh_token"]).encode(),
        ),
        "expires_at": utc_iso(float(tokens["access_expires_at"])),
        "remote_verified": profile_binding_preserved,
        "refresh_marker_cleanup_pending": cleanup_pending,
    }


def access_token_for_use(
    root: Path | str | None,
    *,
    now: float | None = None,
    transport: AuthTransport | None = None,
    before_request: Callable[[], None] | None = None,
) -> tuple[str, bool]:
    """Return an access token, consuming at most one refresh operation."""

    current = _current_time(now, "Spond-tokenbruk")
    session = local_session_for_use(root, at=current)
    refreshed = False
    if float(session["access_expires_at"]) <= current + REFRESH_SKEW_SECONDS:
        refresh_session(
            root,
            transport=transport,
            at=current,
            before_request=before_request,
        )
        session = local_session_for_use(root, at=current)
        refreshed = True
    return str(session["access_token"]), refreshed


def access_token(
    root: Path | str | None,
    *,
    now: float | None = None,
    transport: AuthTransport | None = None,
    before_request: Callable[[], None] | None = None,
) -> str:
    return access_token_for_use(
        root,
        now=now,
        transport=transport,
        before_request=before_request,
    )[0]


def auth_status(
    root: Path | str | None, *, at: float | None = None
) -> dict[str, Any]:
    """Return identity-free, offline status without creating private state."""

    current = _current_time(at, "Spond-status")
    target = session_path(root)
    session_present = target.is_file() and not target.is_symlink()
    configured = False
    authenticated = False
    remote_verified = False
    access_expired: bool | None = None
    access_expires_at: str | None = None
    refresh_expires_at: str | None = None
    session: dict[str, Any] | None = None
    try:
        load_client_config(root)
        configured = True
    except SpondAuthError:
        pass
    if configured:
        try:
            session = _read_session(root)
        except SpondReauthenticationRequired:
            pass
    marker = _read_refresh_marker(root)
    marker_completed = (
        session is not None and marker is not None and _marker_completed(marker, session)
    )
    blocked = marker is not None and not marker_completed
    reason = marker.get("reason") if blocked and marker is not None else None
    if session is not None:
        access_expired = float(session["access_expires_at"]) <= current
        access_expires_at = utc_iso(float(session["access_expires_at"]))
        refresh_expires_at = utc_iso(float(session["refresh_expires_at"]))
        remote_verified = isinstance(session.get("profile_id"), str) and isinstance(
            session.get("verified_at"), (int, float)
        )
        authenticated = (
            remote_verified
            and not blocked
            and float(session["refresh_expires_at"]) > current
        )
    cooldown = cooldown_status(root, at=current)
    return {
        "configured": configured,
        "authenticated": authenticated,
        "remote_verified": remote_verified,
        "session_present": session_present,
        "expires_at": access_expires_at,
        "access_expires_at": access_expires_at,
        "access_expired": access_expired,
        "refresh_expires_at": refresh_expires_at,
        "refresh_blocked": blocked,
        "refresh_block_reason": reason,
        **cooldown,
    }


@lifecycle.operation('spond', 'logout')
def logout(root: Path | str | None) -> dict[str, Any]:
    """Remove only local Spond grant state; never perform a remote write."""

    with private_source_lock(root, SOURCE, wait=True):
        try:
            removed = remove_private_file(session_path(root))
            marker_removed = remove_private_file(refresh_block_path(root))
        except PrivateStateError as exc:
            raise SpondAuthError(str(exc)) from None
    return {
        "authenticated": False,
        "session_removed": removed,
        "refresh_marker_removed": marker_removed,
    }
