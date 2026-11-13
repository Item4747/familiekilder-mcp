"""Helpers shared by the provider auth modules, and the contract they follow.

Provider auth modules (``vigilo_auth``, ``skolemelding_auth``, ``ist_oauth``,
``spond_auth``, ``hoopit_auth``) are named by the ``auth`` field of their
entry in ``sources.REGISTRY``, loaded on first use and called duck-typed.
A new module provides:

``session_path(root)``, ``client_config_path(root)``
    Paths from ``private_state.credential_path(root, "<source>-....json")``.
    ``auth_cli`` treats an existing session file as "logged in";
    ``auth_lifecycle`` reads ``<source>-session.json`` and the refresh marker
    ``<source>-refresh-blocked.json`` (Hoopit: ``-refresh-state``) directly.
``configure_client(root, client_file) -> dict``
    Validate provider metadata read through the private-file boundary (owner,
    0600, no symlink), write it with ``atomic_write_private_json`` under
    ``private_source_lock``, refuse to change the auth identity while a session
    exists, and return a summary without secrets.
``load_client_config(root) -> ClientConfig``
    Read and revalidate the stored metadata; raise the module error if missing.
``consent_status(root)`` / ``record_consent(root, *, version, at)``
    Only for unofficial browser OAuth. Both return ``recorded``, ``current``,
    ``version`` and ``recorded_at``; login must refuse until ``current``.
``begin_login(root) -> (authorization_url, transaction)`` and
``login_with_callback(root, transaction, callback_url, *, replace_existing)``,
or ``import_session(root, source, *, replace_existing)``
    Create the first session. Never overwrite one unless ``replace_existing``.
``local_session_for_use(root) -> dict``
    Offline: read and validate the stored session, raise a
    ``SourceLoginRequired`` subclass when login is needed and a
    ``SourceUnavailable`` subclass while a refresh outcome is uncertain. Hoopit
    exposes ``session_for_use`` instead; Vigilo and Spond add
    ``access_token_for_use(root) -> (token, refreshed)``.
``refresh_session(root, *, at=None) -> dict``
    Rotate under the source lock. Write the refresh marker before a rotating
    request and clear it only after the new session is committed, so a lost
    response blocks reuse of the old grant. Record events via ``auth_lifecycle``.
    ``auth_lifecycle.maintenance`` calls it with ``at=``.
``auth_status(root) -> dict``
    Offline and secret-free; includes ``authenticated``.
``logout(root) -> dict``
    Remove the local session and refresh marker under the source lock, keep
    consent and client config, and return a secret-free summary. The keys
    differ per provider; callers only print them.

All user-facing errors are Norwegian and come from the module's own error
class. Exceptions from injected transports or validators must not be chained
onto them (``raise ... from None``), since they may carry tokens.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from .private_state import PrivateStateError, read_private_json

_T = TypeVar("_T")
_HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")


def is_plain_text(value: Any, *, maximum: int, allow_space: bool = True) -> bool:
    """A non-empty string within ``maximum`` without control characters."""

    floor = 32 if allow_space else 33
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= maximum
        and not any(ord(character) < floor or ord(character) == 127 for character in value)
    )


def normalize_public_host(value: Any, *, strict: bool = True) -> str | None:
    """Return a lower-case public DNS name or global IP, or ``None``.

    ``strict=False`` keeps Vigilo's older, looser rules (it accepts ``*``,
    spaces and ``_``). New modules use the default.
    """

    if not is_plain_text(value, maximum=253):
        return None
    host = value.lower().rstrip(".")
    if "/" in host or ":" in host or host.startswith(".") or (strict and "*" in host):
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if host == "localhost" or host.endswith(".localhost"):
        return None
    if strict and (".." in host or not _HOSTNAME.fullmatch(host)):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    return host if address.is_global else None


def read_private_mapping(
    path: Path,
    missing_message: str,
    *,
    max_bytes: int,
    error: type[Exception],
    invalid_message: str,
) -> Mapping[str, Any]:
    try:
        value = read_private_json(path, max_bytes=max_bytes, missing_message=missing_message)
    except PrivateStateError as exc:
        raise error(str(exc)) from None
    if not isinstance(value, Mapping):
        raise error(invalid_message)
    return value


def canonical_sha256(value: Any, *, ensure_ascii: bool = True) -> str:
    """SHA-256 of compact, key-sorted JSON. Stored fingerprints depend on it."""

    encoded = json.dumps(
        value, ensure_ascii=ensure_ascii, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fingerprint_matches(fingerprint: object, candidates: Iterable[str]) -> bool:
    if not isinstance(fingerprint, str) or not fingerprint.isascii():
        return False
    encoded = fingerprint.encode("ascii")
    return any(
        hmac.compare_digest(encoded, candidate.encode("ascii")) for candidate in candidates
    )


def base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def random_urlsafe(random_bytes: Callable[[int], bytes], error: type[Exception]) -> str:
    """32 random bytes as unpadded base64url, for OAuth state and PKCE."""

    value = random_bytes(32)
    if not isinstance(value, bytes) or len(value) != 32:
        raise error("Sikker tilfeldig generator returnerte ugyldige data")
    return base64url(value)


def has_invalid_percent_encoding(value: str) -> bool:
    hexdigits = frozenset("0123456789abcdefABCDEF")
    return any(
        character == "%"
        and (
            index + 2 >= len(value)
            or value[index + 1] not in hexdigits
            or value[index + 2] not in hexdigits
        )
        for index, character in enumerate(value)
    )


def safe_call(message: str, operation: Callable[[], _T], error: type[Exception]) -> _T:
    """Run an injected operation; any failure becomes ``error(message)``."""

    failed = False
    result: Any = None
    try:
        result = operation()
    except Exception:
        failed = True
    if failed:
        # Raised outside the except suite so an injected exception carrying a
        # token is not kept as ``__context__``.
        raise error(message) from None
    return result


def utc_iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat(timespec="seconds")
