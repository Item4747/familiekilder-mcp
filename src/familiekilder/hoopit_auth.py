"""Personal access token for Hoopit's external API.

The user creates a personal access token at ``TOKEN_PAGE_URL`` with the scopes
``posts:read``, ``events:read`` and ``events:respond``. Hoopit's page shows no
scope ids, only the checkbox labels in ``SCOPE_LABELS``, so everything the user
reads names the labels. Hoopit shows the token once. This module checks its format, lets ``hoopit.verify_token`` read one page
of posts and one page of occurrences with it, and stores it below the literal
``.private`` tree. The token is sent only as ``Authorization: Bearer`` to the
exact Hoopit API host. It is never printed, logged or returned.

There is no refresh: a token works until it expires or the user withdraws it.
A 401 from Hoopit marks the token as rejected; a 403 on a member list marks the
scope that list needs as missing. Both stay until the user connects a new token.

The API does not say whose token it is, so the session keeps a random
``account_scope`` from the first connection. Replacing the token keeps it;
logging out ends it. ``read_cache.account_scope`` uses it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import auth_lifecycle as lifecycle
from .auth_common import utc_iso
from .core import FamiliekilderError
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
API_HOST = "api.hoopit.io"
API_ROOT = f"https://{API_HOST}/external"
TOKEN_PAGE_URL = "https://id.hoopit.io/security/access-tokens/"
# The scopes the user ticks when creating the token. Only the two read scopes
# can be checked without a write; events:respond is needed for answering.
READ_SCOPES = ("posts:read", "events:read")
RESPOND_SCOPE = "events:respond"
REQUIRED_SCOPES = (*READ_SCOPES, RESPOND_SCOPE)
# Hoopit's checkbox for each scope under «Dine» on the token page, in the
# page's order. The boxes under «Som administrator» are not needed.
SCOPE_LABELS = {
    "events:read": "Se aktivitetene og invitasjonene dine, og barnas",
    "events:respond": "Svare på invitasjoner for deg selv og barna dine",
    "posts:read": "Se innlegg og kommentarer i gruppene dine",
}
TOKEN_CHECKBOXES = tuple(SCOPE_LABELS.values())

SESSION_FILE = "hoopit-session.json"
TOKEN_STATE_FILE = "hoopit-token-state.json"
RATE_STATE_FILE = "hoopit-rate-limit.json"
API_VERSION_FILE = "hoopit-api-version.json"

SESSION_VERSION = 1
SESSION_KIND = "personal_access_token"
TOKEN_STATE_VERSION = 1
MAX_PRIVATE_JSON_BYTES = 64 * 1024

_NEW_TOKEN = re.compile(r"hoopit_pat_[A-Za-z0-9]{40}\Z")
# Hoopit: tokens made before the hoopit_pat_ prefix work until they expire.
_OLD_TOKEN = re.compile(r"hoopit_(?!pat_)[A-Za-z0-9_-]{20,200}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SCOPES = frozenset(REQUIRED_SCOPES)
_SESSION_FIELDS = frozenset(
    {"version", "kind", "token", "account_scope", "connected_at", "verified_at", "scopes_verified"}
)

def checkbox_list(labels=TOKEN_CHECKBOXES) -> str:
    """The labels quoted and joined as Norwegian prose: «a», «b» og «c»."""

    quoted = [f"«{label}»" for label in labels]
    return quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " og " + quoted[-1]


# The steps on Hoopit's token page, in its own words.
TOKEN_STEPS = (
    f"Åpne {TOKEN_PAGE_URL} og logg inn i Hoopit.",
    "Skriv «familiekilder» i «Navn».",
    f"Under «Dine» krysser du av for {checkbox_list()}. "
    "Boksene under «Som administrator» trengs ikke.",
    "Velg hvor lenge tokenet skal virke i «Utløper om». Når det har utløpt, lager du et nytt.",
    "Trykk «Opprett et token», og kopier tokenet. Det vises bare én gang.",
)

NEW_TOKEN_HINT = (
    f"Lag et nytt token på {TOKEN_PAGE_URL}. Under «Dine» krysser du av for "
    f"{checkbox_list()}. Koble deretter til Hoopit på nytt."
)
TOKEN_REJECTED_MESSAGE = (
    "Hoopit avviste tokenet. Det er utløpt eller trukket tilbake. " + NEW_TOKEN_HINT
)
NOT_CONNECTED_MESSAGE = (
    f"Hoopit er ikke koblet til. Koble til med et personlig tilgangstoken fra {TOKEN_PAGE_URL}."
)
UNREADABLE_SESSION_MESSAGE = (
    "Det lagrede Hoopit-tokenet kan ikke leses. Koble til på nytt med et token og velg "
    "å erstatte det som er lagret, eller logg ut av Hoopit."
)


class HoopitAuthError(FamiliekilderError):
    """Sanitized Hoopit token or local-state failure."""


class HoopitTokenFormatError(HoopitAuthError):
    """The pasted text is not a Hoopit personal access token."""


class HoopitReauthenticationRequired(SourceLoginRequired, HoopitAuthError):
    """No usable token: none stored, unreadable, rejected or missing a scope."""


class HoopitTokenRejected(HoopitReauthenticationRequired):
    """Hoopit answered 401: the token has expired or was withdrawn."""

    reason = "token_rejected"


class HoopitScopeMissing(HoopitReauthenticationRequired):
    """Hoopit answered 403 on a member list that needs one of the token's scopes."""

    reason = "scope_missing"

    def __init__(self, message: str, *, scope: str) -> None:
        self.scope = scope
        super().__init__(message)


class HoopitRemoteUnavailable(SourceUnavailable):
    """Preserve a remote failure's safe category without its response body."""

    def __init__(self, message, *, reason, retry_after_seconds=None):
        from datetime import UTC, datetime, timedelta

        self.reason = reason
        self.retry_at = (
            (datetime.now(UTC) + timedelta(seconds=retry_after_seconds)).isoformat()
            if retry_after_seconds is not None
            else None
        )
        super().__init__(message)


@dataclass(frozen=True)
class HoopitAccess:
    token: str = field(repr=False)
    account_scope: str = field(repr=False)


def session_path(root: Path | str | None) -> Path:
    return credential_path(root, SESSION_FILE)


def token_state_path(root: Path | str | None) -> Path:
    return credential_path(root, TOKEN_STATE_FILE)


def rate_state_path(root: Path | str | None) -> Path:
    return credential_path(root, RATE_STATE_FILE)


def api_version_path(root: Path | str | None) -> Path:
    return credential_path(root, API_VERSION_FILE)


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _now(value: float | None) -> float:
    return time.time() if value is None else float(value)


def scope_missing_message(scope: str) -> str:
    label = SCOPE_LABELS.get(scope, scope)
    return (
        f"Tokenet mangler «{label}». Lag et nytt token på {TOKEN_PAGE_URL} med den "
        "avkrysset under «Dine»."
    )


def validate_token_format(value: Any) -> str:
    """Return the token without surrounding whitespace, or raise a clear error.

    The error text never contains the value.
    """

    if not isinstance(value, str):
        raise HoopitTokenFormatError("Lim inn tokenet fra Hoopit.")
    token = value.strip()
    if not token:
        raise HoopitTokenFormatError("Lim inn tokenet fra Hoopit.")
    if _NEW_TOKEN.fullmatch(token) or _OLD_TOKEN.fullmatch(token):
        return token
    if token.startswith("hoopit_pat_"):
        raise HoopitTokenFormatError(
            "Tokenet er ikke helt. Et Hoopit-token er «hoopit_pat_» fulgt av 40 bokstaver "
            "og tall. Kopier hele tokenet på nytt."
        )
    raise HoopitTokenFormatError(
        "Dette er ikke et Hoopit-token. Et token begynner med «hoopit_pat_» og lages på "
        f"{TOKEN_PAGE_URL}."
    )


def _read_mapping(path: Path) -> Mapping[str, Any] | None:
    if not _present(path):
        return None
    try:
        value = read_private_json(path, max_bytes=MAX_PRIVATE_JSON_BYTES)
    except PrivateStateError:
        raise HoopitAuthError("Hoopit-tilstanden kunne ikke leses") from None
    if not isinstance(value, Mapping):
        raise HoopitAuthError("Hoopit-tilstanden har ugyldig format")
    return value


def _valid_session(value: Mapping[str, Any]) -> bool:
    token, scope, scopes = (
        value.get("token"),
        value.get("account_scope"),
        value.get("scopes_verified"),
    )
    return (
        set(value) == _SESSION_FIELDS
        and value.get("version") == SESSION_VERSION
        and value.get("kind") == SESSION_KIND
        and isinstance(token, str)
        and bool(_NEW_TOKEN.fullmatch(token) or _OLD_TOKEN.fullmatch(token))
        and isinstance(scope, str)
        and bool(_SHA256.fullmatch(scope))
        and isinstance(scopes, list)
        and all(item in _SCOPES for item in scopes)
        and isinstance(value.get("connected_at"), str)
        and isinstance(value.get("verified_at"), str)
    )


def _session_state(root: Path | str | None) -> tuple[str, Mapping[str, Any] | None]:
    """("missing" | "token" | "unreadable", the stored mapping when readable)."""

    try:
        value = _read_mapping(session_path(root))
    except HoopitAuthError:
        return "unreadable", None
    if value is None:
        return "missing", None
    return ("token", dict(value)) if _valid_session(value) else ("unreadable", value)


def _read_token_state(root: Path | str | None) -> dict[str, Any] | None:
    value = _read_mapping(token_state_path(root))
    if value is None:
        return None
    if value.get("version") != TOKEN_STATE_VERSION or value.get("reason") not in {
        "token_rejected",
        "scope_missing",
    }:
        raise HoopitAuthError("Hoopit-tokentilstanden har ugyldig format")
    if value["reason"] == "scope_missing" and value.get("scope") not in _SCOPES:
        raise HoopitAuthError("Hoopit-tokentilstanden har ugyldig format")
    return dict(value)


def _account_scope(stored: Mapping[str, Any] | None) -> str:
    """The stored binding, also from an unreadable session; else a new one."""

    scope = stored.get("account_scope") if isinstance(stored, Mapping) else None
    if isinstance(scope, str) and _SHA256.fullmatch(scope):
        return scope
    return hashlib.sha256(
        b"familiekilder-hoopit-token-account\0" + secrets.token_bytes(32)
    ).hexdigest()


@lifecycle.operation("hoopit", "import")
def connect_token(
    root: Path | str | None,
    token: Any,
    *,
    replace_existing: bool = False,
    transport: Any = None,
    rate_limiter: Any = None,
    at: float | None = None,
) -> dict[str, Any]:
    """Check a pasted token with Hoopit and store it; returns a secret-free summary.

    The format is checked first, then ``hoopit.verify_token`` reads one page of
    posts and one page of occurrences. Nothing is stored unless both succeed.
    ``replace_existing`` replaces a stored token, also an unreadable one.
    """

    value = validate_token_format(token)
    instant = _now(at)
    with private_source_lock(root, SOURCE):
        state, stored = _session_state(root)
        if state == "unreadable" and not replace_existing:
            raise HoopitAuthError(UNREADABLE_SESSION_MESSAGE)
        if state == "token" and not replace_existing:
            raise HoopitAuthError(
                "Hoopit er allerede koblet til med et token. Velg å erstatte det for å bruke et nytt."
            )
        from .hoopit import verify_token

        verified = verify_token(
            root, value, transport=transport, rate_limiter=rate_limiter, at=instant
        )
        stamp = utc_iso(instant)
        session = {
            "version": SESSION_VERSION,
            "kind": SESSION_KIND,
            "token": value,
            "account_scope": _account_scope(stored),
            "connected_at": stamp,
            "verified_at": stamp,
            "scopes_verified": list(verified["scopes_verified"]),
        }
        try:
            atomic_write_private_json(session_path(root), session)
            remove_private_file(token_state_path(root))
        except PrivateStateError as exc:
            raise HoopitAuthError(str(exc)) from None
    return {
        "authenticated": True,
        "connected_at": stamp,
        "scopes_verified": list(verified["scopes_verified"]),
        "scope_not_checked": RESPOND_SCOPE,
        "posts_on_first_page": verified["posts"],
        "occurrences_on_first_page": verified["occurrences"],
    }


def token_for_use(root: Path | str | None) -> HoopitAccess:
    """Return the stored token for one read run, or explain what the user must do."""

    with private_source_lock(root, SOURCE):
        lifecycle.require_not_moved(root, SOURCE)
        state, session = _session_state(root)
        if state == "missing":
            raise HoopitReauthenticationRequired(NOT_CONNECTED_MESSAGE)
        try:
            rejection = _read_token_state(root)
        except HoopitAuthError:
            state = "unreadable"
        if state == "unreadable":
            raise HoopitReauthenticationRequired(UNREADABLE_SESSION_MESSAGE)
        if rejection is not None:
            if rejection["reason"] == "token_rejected":
                raise HoopitTokenRejected(TOKEN_REJECTED_MESSAGE)
            scope = rejection["scope"]
            raise HoopitScopeMissing(scope_missing_message(scope), scope=scope)
        lifecycle.require_owner(root, SOURCE)
        return HoopitAccess(
            token=str(session["token"]), account_scope=str(session["account_scope"])
        )


def mark_token_rejected(root: Path | str | None, *, at: float | None = None) -> None:
    _write_token_state(root, {"reason": "token_rejected"}, at=at)


def mark_scope_missing(root: Path | str | None, scope: str, *, at: float | None = None) -> None:
    if scope not in _SCOPES:
        raise HoopitAuthError("Ukjent Hoopit-tilgang")
    _write_token_state(root, {"reason": "scope_missing", "scope": scope}, at=at)


def _write_token_state(root, value, *, at=None) -> None:
    with private_source_lock(root, SOURCE):
        if _session_state(root)[0] != "token":
            return
        try:
            atomic_write_private_json(
                token_state_path(root),
                {"version": TOKEN_STATE_VERSION, **value, "recorded_at": utc_iso(_now(at))},
            )
        except PrivateStateError as exc:
            raise HoopitAuthError(str(exc)) from None


def refresh_session(root: Path | str | None, *, at: float | None = None) -> dict[str, Any]:
    """A personal access token is not refreshed."""

    return {"refreshed": False, "reason": "personal-access-token"}


def _version_warning(root: Path | str | None) -> str | None:
    try:
        value = _read_mapping(api_version_path(root))
    except HoopitAuthError:
        return None
    if not value:
        return None
    from .hoopit import version_warning_text

    return version_warning_text(value)


def _read_cooldown(root: Path | str | None, at: float) -> str | None:
    value = _read_mapping(rate_state_path(root))
    if value is None:
        return None
    raw = value.get("cooldown_until")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return utc_iso(float(raw)) if float(raw) > at else None


def auth_status(root: Path | str | None, *, at: float | None = None) -> dict[str, Any]:
    """Return a fixed-shape, secret-free and strictly offline status snapshot."""

    instant = _now(at)
    result: dict[str, Any] = {
        # No client setup is needed: the API and its host are fixed.
        "configured": True,
        "authenticated": False,
        "session_present": False,
        "login": "personal_access_token",
        "token_page": TOKEN_PAGE_URL,
        "required_scopes": list(REQUIRED_SCOPES),
        "token_checkboxes": list(TOKEN_CHECKBOXES),
        "connected_at": None,
        "verified_at": None,
        "scopes_verified": [],
        "refresh_blocked": False,
        "refresh_block_reason": None,
        "missing_scope": None,
        "detail": NOT_CONNECTED_MESSAGE,
        "api_version_warning": None,
        "cooldown_until": None,
        "status_unavailable": False,
    }
    try:
        state, session = _session_state(root)
        if state != "missing":
            result["session_present"] = True
        if state == "unreadable":
            result.update(
                refresh_blocked=True,
                refresh_block_reason="session-unreadable",
                detail=UNREADABLE_SESSION_MESSAGE,
            )
        elif state == "token":
            result.update(
                connected_at=session["connected_at"],
                verified_at=session["verified_at"],
                scopes_verified=list(session["scopes_verified"]),
                detail=None,
            )
            rejection = _read_token_state(root)
            if rejection is None:
                result["authenticated"] = True
            elif rejection["reason"] == "token_rejected":
                result.update(
                    refresh_blocked=True,
                    refresh_block_reason="token-rejected",
                    detail=TOKEN_REJECTED_MESSAGE,
                )
            else:
                result.update(
                    refresh_blocked=True,
                    refresh_block_reason="scope-missing",
                    missing_scope=rejection["scope"],
                    detail=scope_missing_message(rejection["scope"]),
                )
        result["api_version_warning"] = _version_warning(root)
        result["cooldown_until"] = _read_cooldown(root, instant)
    except (HoopitAuthError, PrivateStateError, OSError, ValueError, OverflowError):
        result["authenticated"] = False
        result["status_unavailable"] = True
    return result


@lifecycle.operation("hoopit", "logout")
def logout(root: Path | str | None) -> dict[str, Any]:
    """Remove the local token, also an unreadable one; it stays valid at Hoopit."""

    with private_source_lock(root, SOURCE):
        try:
            removed = remove_private_file(session_path(root))
            remove_private_file(token_state_path(root))
            remove_private_file(api_version_path(root))
        except PrivateStateError as exc:
            raise HoopitAuthError(str(exc)) from None
    return {
        "authenticated": False,
        "session_removed": removed,
        "remote_revocation_attempted": False,
        "detail": (
            f"Tokenet er slettet her. Det virker hos Hoopit til du sletter det på {TOKEN_PAGE_URL}."
        ),
    }


__all__ = [
    "API_HOST",
    "API_ROOT",
    "HoopitAccess",
    "HoopitAuthError",
    "HoopitReauthenticationRequired",
    "HoopitScopeMissing",
    "HoopitTokenFormatError",
    "HoopitTokenRejected",
    "READ_SCOPES",
    "REQUIRED_SCOPES",
    "RESPOND_SCOPE",
    "TOKEN_PAGE_URL",
    "auth_status",
    "connect_token",
    "logout",
    "mark_scope_missing",
    "mark_token_rejected",
    "rate_state_path",
    "refresh_session",
    "session_path",
    "token_for_use",
    "validate_token_format",
]
