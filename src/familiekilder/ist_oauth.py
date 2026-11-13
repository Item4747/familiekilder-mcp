"""IST OAuth lifecycle: consent, private sessions, rotation, recovery and logout."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import stat
import time
import urllib.parse
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Mapping

from . import auth_lifecycle as lifecycle
from .api_transport import ApiRequestNotSent
from .auth_common import base64url, utc_iso
from .core import project_paths
from .ist_oauth_protocol import (
    _T,
    _consume_callback,
    _decode_jwt_payload_unverified,
    _numeric,
    _OAuthHTTPError,
    _parse_client_config,
    _pyjwt_access_token_validator,
    _pyjwt_token_validator,
    _random_urlsafe,
    _safe_call,
    _safe_token,
    _validate_access_claims,
    _validate_id_claims,
    _validated_refresh_response,
    _validated_subject,
    _validated_token_response,
)
from .ist_oauth_protocol import (
    ACCESS_TOKEN_AUDIENCE as ACCESS_TOKEN_AUDIENCE,
)
from .ist_oauth_protocol import (
    AUTHORITY as AUTHORITY,
)
from .ist_oauth_protocol import (
    AUTHORIZATION_ENDPOINT as AUTHORIZATION_ENDPOINT,
)
from .ist_oauth_protocol import (
    CALLBACK_LIFETIME_SECONDS as CALLBACK_LIFETIME_SECONDS,
)
from .ist_oauth_protocol import (
    EXPECTED_SCOPES as EXPECTED_SCOPES,
)
from .ist_oauth_protocol import (
    ISSUER as ISSUER,
)
from .ist_oauth_protocol import (
    REGISTRATION_REJECTED as REGISTRATION_REJECTED,
)
from .ist_oauth_protocol import (
    IST_GRAPHQL_ENDPOINT as IST_GRAPHQL_ENDPOINT,
)
from .ist_oauth_protocol import (
    JWKS_URI as JWKS_URI,
)
from .ist_oauth_protocol import (
    MAX_JSON_BYTES as MAX_JSON_BYTES,
)
from .ist_oauth_protocol import (
    MINIMAL_CONTEXT_QUERY as MINIMAL_CONTEXT_QUERY,
)
from .ist_oauth_protocol import (
    REDIRECT_URI as REDIRECT_URI,
)
from .ist_oauth_protocol import (
    REVOCATION_ENDPOINT as REVOCATION_ENDPOINT,
)
from .ist_oauth_protocol import (
    TOKEN_ENDPOINT as TOKEN_ENDPOINT,
)
from .ist_oauth_protocol import (
    USERINFO_ENDPOINT as USERINFO_ENDPOINT,
)
from .ist_oauth_protocol import (
    AccessTokenValidator as AccessTokenValidator,
)
from .ist_oauth_protocol import (
    ClientConfig as ClientConfig,
)
from .ist_oauth_protocol import (
    LoginTransaction as LoginTransaction,
)
from .ist_oauth_protocol import (
    OAuthError as OAuthError,
)
from .ist_oauth_protocol import (
    OAuthFatalConfigurationError as OAuthFatalConfigurationError,
)
from .ist_oauth_protocol import (
    OAuthNoAnswer as OAuthNoAnswer,
)
from .ist_oauth_protocol import (
    OAuthReauthenticationRequired as OAuthReauthenticationRequired,
)
from .ist_oauth_protocol import (
    OAuthTransport as OAuthTransport,
)
from .ist_oauth_protocol import (
    TokenValidator as TokenValidator,
)
from .ist_oauth_protocol import (
    UrllibOAuthTransport as UrllibOAuthTransport,
)
from .ist_oauth_protocol import (
    pkce_challenge as pkce_challenge,
)
from .private_lock import private_source_lock
from .private_state import (
    PrivateStateError,
    atomic_write_private_json,
    fsync_directory,
    read_private_bytes,
    validate_private_parent_tree,
    write_client_export_json,
)

CLIENT_FILE = "ist-home-client.json"
SESSION_FILE = "ist-home-session.json"
CONSENT_FILE = "ist-home-oauth-consent.json"
REFRESH_BLOCK_FILE = "ist-home-refresh-blocked.json"
REFRESH_TRANSACTION_FIELD = "refresh_transaction_id"
REFRESH_RECOVERY_MARKER_FIELD = "refresh_recovery_marker_sha256"
CONSENT_VERSION = "unofficial-ist-oauth-v1"
# getContext is a read. When IST gives no answer at all, ask once more before
# giving up; any answer that rejects the token ends the check at once.
CONTEXT_ATTEMPTS = 2
CONTEXT_RETRY_DELAY_SECONDS = 2.0
# A check that answers this slowly is logged even when it succeeds.
SLOW_CONTEXT_MS = 10_000

_log = logging.getLogger(__name__)


class OAuthContextNoAnswer(OAuthError):
    """IST Home never answered the context check, so no session was saved.

    ``login_step_message`` is fixed copy for the person logging in; the
    exception's own text is never shown.
    """

    login_step_message = (
        "IST Home svarte ikke da innloggingen skulle kontrolleres, så den er ikke lagret. "
        "Prøv igjen om litt."
    )


def _credential_mutation(
    function: Callable[..., _T] | None = None,
    *,
    wait: bool = False,
) -> Any:
    """Serialize all IST credential mutations with collection/refresh work."""

    def decorate(operation: Callable[..., _T]) -> Callable[..., _T]:
        @wraps(operation)
        def locked(root: Path | str | None, *args: Any, **kwargs: Any) -> _T:
            with private_source_lock(root, "ist-home", wait=wait):
                return operation(root, *args, **kwargs)

        return locked

    if function is None:
        return decorate
    return decorate(function)


def _credential_dir(root: Path | str | None) -> Path:
    return project_paths(root).private / "credentials"


def client_config_path(root: Path | str | None) -> Path:
    """Return the private client configuration path."""

    return _credential_dir(root) / CLIENT_FILE


def session_path(root: Path | str | None) -> Path:
    """Return the legacy-compatible private session path."""

    return _credential_dir(root) / SESSION_FILE


def consent_path(root: Path | str | None) -> Path:
    return _credential_dir(root) / CONSENT_FILE


def refresh_block_path(root: Path | str | None) -> Path:
    return _credential_dir(root) / REFRESH_BLOCK_FILE


def _validated_refresh_transaction_id(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 43:
        raise OAuthError("Refresh-transaksjonen har ugyldig format")
    allowed = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    if any(character not in allowed for character in value):
        raise OAuthError("Refresh-transaksjonen har ugyldig format")
    return value


def _validated_marker_fingerprint(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OAuthError("Refresh-recoverymarkøren har ugyldig format")
    return value


def _new_refresh_transaction_id() -> str:
    return _validated_refresh_transaction_id(base64url(secrets.token_bytes(32)))


def _mark_refresh_blocked(
    root: Path | str | None,
    reason: str,
    *,
    at: float,
    transaction_id: str | None = None,
) -> None:
    """Persist non-secret terminal state after a possibly rotating refresh call."""

    recorded_at = utc_iso(at)
    value: dict[str, Any] = {
        "version": 1,
        "reason": reason,
        "recorded_at": recorded_at,
        # Make every marker incarnation unique.  A recovery fingerprint stored
        # in a session can therefore never authorize a later marker that merely
        # has the same reason and second-resolution timestamp.
        "marker_id": _new_refresh_transaction_id(),
    }
    if transaction_id is not None:
        value["transaction_id"] = _validated_refresh_transaction_id(transaction_id)
    _atomic_write_json(
        refresh_block_path(root),
        value,
        overwrite=True,
    )


def _read_bounded_private_file(path: Path, *, missing_message: str) -> bytes:
    if any(parent.name == ".private" for parent in (path, *path.parents)):
        try:
            return read_private_bytes(
                path,
                max_bytes=MAX_JSON_BYTES,
                missing_message=missing_message,
            )
        except PrivateStateError as exc:
            raise OAuthError(str(exc)) from None
    if path.is_symlink():
        raise OAuthError("En privat OAuth-fil kan ikke være en symbolsk lenke")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_JSON_BYTES + 1)
    except OSError:
        raise OAuthError(missing_message) from None
    if len(raw) > MAX_JSON_BYTES:
        raise OAuthError("OAuth-filen er større enn tillatt")
    return raw


def _refresh_block_state(
    root: Path | str | None,
) -> tuple[str | None, str | None, str | None]:
    path = refresh_block_path(root)
    if not path.exists() and not path.is_symlink():
        return None, None, None
    try:
        raw = _read_bounded_private_file(path, missing_message="Refresh-sperren mangler")
    except OAuthError:
        return "unknown", None, None
    fingerprint = hashlib.sha256(raw).hexdigest()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict):
        return "unknown", None, fingerprint
    reason = value.get("reason")
    if reason in {
        "refresh-in-flight",
        "refresh-completed",
        "invalid-grant",
        "fatal-client-configuration",
    }:
        transaction_id: str | None = None
        if value.get("transaction_id") is not None:
            try:
                transaction_id = _validated_refresh_transaction_id(value.get("transaction_id"))
            except OAuthError:
                transaction_id = None
        return str(reason), transaction_id, fingerprint
    return "unknown", None, fingerprint


def _refresh_block_reason(root: Path | str | None) -> str | None:
    return _refresh_block_state(root)[0]


def _clear_refresh_block(root: Path | str | None) -> None:
    path = refresh_block_path(root)
    try:
        if not validate_private_parent_tree(path):
            return
        existed = path.exists() or path.is_symlink()
        path.unlink(missing_ok=True)
        if existed:
            fsync_directory(path.parent)
    except PrivateStateError as exc:
        raise OAuthError(str(exc)) from None
    except OSError:
        raise OAuthError("Refresh-sperren kunne ikke fjernes fra privat lagring") from None


def _finish_refresh_marker(
    root: Path | str | None,
    *,
    at: float,
    transaction_id: str | None = None,
) -> bool:
    """Make a committed session usable even if marker cleanup later fails.

    The completed marker contains no secrets.  It is durably written only after
    the fully validated session has been committed, so readers can distinguish
    harmless local cleanup debt from an in-flight/unknown refresh outcome.
    ``True`` means the marker was removed; ``False`` means cleanup remains.
    """

    marker = refresh_block_path(root)
    if not marker.exists() and not marker.is_symlink():
        return True
    try:
        _mark_refresh_blocked(
            root,
            "refresh-completed",
            at=at,
            transaction_id=transaction_id,
        )
    except (OAuthError, OSError):
        # The committed session carries the same transaction ID.  Keeping the
        # old in-flight marker is therefore recoverable and must not invalidate
        # a fully verified rotated grant.
        pass
    try:
        _clear_refresh_block(root)
    except OAuthError:
        return False
    return True


def _discard_recovery_fingerprint(root: Path | str | None, session: Mapping[str, Any]) -> bool:
    """Remove one-shot recovery authority after its marker was deleted."""

    if REFRESH_RECOVERY_MARKER_FIELD not in session:
        return True
    cleaned = dict(session)
    cleaned.pop(REFRESH_RECOVERY_MARKER_FIELD, None)
    try:
        _atomic_write_json(
            session_path(root),
            cleaned,
            overwrite=True,
        )
    except (OSError, OAuthError):
        # The committed session remains valid.  Fresh random marker IDs make a
        # stale fingerprint harmless until the next successful session write.
        return False
    return True


def _atomic_write_json(path: Path, value: Mapping[str, Any], *, overwrite: bool) -> None:
    try:
        atomic_write_private_json(path, value, overwrite=overwrite)
    except PrivateStateError as exc:
        raise OAuthError(str(exc)) from None


def _reap_session_temporaries(root: Path | str | None) -> int:
    """Delete only abandoned regular temp files for the canonical session.

    Callers hold the shared IST credential lock, so a matching file cannot
    belong to a live cooperating writer.  Symlinks, foreign-owned files and
    every non-exact filename are left untouched.
    """

    target = session_path(root)
    parent = target.parent
    prefix = f".{target.name}."
    suffix = ".tmp"
    removed = 0
    failed = False
    try:
        entries = os.scandir(parent)
    except FileNotFoundError:
        return 0
    except OSError:
        raise OAuthError("Midlertidige IST Home-øktfiler kunne ikke kontrolleres") from None
    with entries:
        for entry in entries:
            if not entry.name.startswith(prefix) or not entry.name.endswith(suffix):
                continue
            stem = entry.name[len(prefix) : -len(suffix)]
            if not stem or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in stem
            ):
                continue
            try:
                file_stat = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                failed = True
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            if hasattr(os, "geteuid") and file_stat.st_uid != os.geteuid():
                continue
            try:
                os.unlink(entry.path)
            except FileNotFoundError:
                continue
            except OSError:
                failed = True
            else:
                removed += 1
    if removed:
        try:
            fsync_directory(parent)
        except (OSError, PrivateStateError):
            failed = True
    if failed:
        raise OAuthError("Midlertidige IST Home-øktfiler kunne ikke ryddes sikkert")
    return removed


def _read_json_file(path: Path, *, missing_message: str) -> Mapping[str, Any]:
    raw = _read_bounded_private_file(path, missing_message=missing_message)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict):
        raise OAuthError("OAuth-filen har ugyldig format")
    return value


def _same_client_config(left: ClientConfig, right: ClientConfig) -> bool:
    return (
        left.authority == right.authority
        and left.client_id == right.client_id
        and hmac.compare_digest(
            left.client_secret.encode("utf-8"), right.client_secret.encode("utf-8")
        )
        and left.redirect_uri == right.redirect_uri
        and set(left.scope.split()) == set(right.scope.split())
    )


def load_client_config(root: Path | str | None) -> ClientConfig:
    """Load and revalidate the private configuration (contains a secret)."""

    value = _read_json_file(
        client_config_path(root), missing_message="IST Home-klientkonfigurasjonen mangler"
    )
    return _parse_client_config(value)


@_credential_mutation
def configure_client(root: Path | str | None, client_file: Path | str) -> dict[str, Any]:
    """Validate and atomically import a client file into ``.private``.

    The returned metadata deliberately omits the client secret and scope string.
    """

    source = Path(client_file).expanduser().resolve()
    value = _read_json_file(source, missing_message="Fant ikke klientkonfigurasjonsfilen")
    config = _parse_client_config(value)
    existing_session_path = session_path(root)
    if existing_session_path.exists():
        try:
            _existing_session, session_config = _parse_session(
                _read_json_file(existing_session_path, missing_message="IST Home-økten mangler")
            )
        except OAuthError:
            raise OAuthError(
                "Klientkonfigurasjonen kan ikke endres mens en lokal IST Home-økt finnes"
            ) from None
        if not _same_client_config(config, session_config):
            raise OAuthError("Logg ut før IST Home-klientkonfigurasjonen endres")
    _atomic_write_json(client_config_path(root), config.as_private_json(), overwrite=True)
    if _refresh_block_reason(root) == "fatal-client-configuration":
        _clear_refresh_block(root)
    return {
        "configured": True,
        "authority": config.authority,
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope_validated": True,
    }


def _default_mac_async_storage() -> Path:
    return (
        Path.home()
        / "Library/Containers/com.ist.home.miniforum/Data/Library/Application Support"
        / "com.ist.home.miniforum/RCTAsyncLocalStorage_V1"
    )


def export_mac_client_config(
    destination: Path | str, *, async_storage: Path | str | None = None
) -> dict[str, Any]:
    """Export only validated client metadata from the official Mac app.

    The explicit destination is created with mode ``0600`` and is never
    overwritten.  User tokens and session state are not copied to the result.
    """

    storage = Path(async_storage) if async_storage is not None else _default_mac_async_storage()
    manifest = _read_json_file(
        storage / "manifest.json", missing_message="Fant ikke IST Home-appdata"
    )
    encoded_sessions = manifest.get("_skolid_sdk_sessions")
    if isinstance(encoded_sessions, str):
        try:
            sessions = json.loads(encoded_sessions)
        except json.JSONDecodeError:
            sessions = None
    elif encoded_sessions is None:
        backing_file = storage / hashlib.md5(b"_skolid_sdk_sessions").hexdigest()
        sessions = _read_json_file(backing_file, missing_message="Fant ikke IST Home-appøktene")
    else:
        sessions = None
    if not isinstance(sessions, dict):
        raise OAuthError("Fant ingen brukbar IST Home-klientkonfigurasjon")

    candidates: list[tuple[float, ClientConfig]] = []
    for session in sessions.values():
        if not isinstance(session, dict) or not isinstance(session.get("config"), dict):
            continue
        try:
            config = _parse_client_config(session["config"], app_keys=True)
        except OAuthError:
            continue
        state = session.get("state")
        issued_at = state.get("issued_at", 0) if isinstance(state, dict) else 0
        try:
            ranking = float(issued_at)
        except (TypeError, ValueError):
            ranking = 0.0
        if not math.isfinite(ranking):
            ranking = 0.0
        candidates.append((ranking, config))
    if not candidates:
        raise OAuthError("Fant ingen brukbar IST Home-klientkonfigurasjon")
    config = max(candidates, key=lambda candidate: candidate[0])[1]
    target = Path(destination).expanduser().resolve()
    try:
        write_client_export_json(target, config.as_private_json())
    except PrivateStateError as exc:
        raise OAuthError(str(exc)) from None
    return {
        "exported": True,
        "destination": str(target),
        "client_id": config.client_id,
        "scope_validated": True,
    }


def consent_status(root: Path | str | None) -> dict[str, Any]:
    """Return identity-free, offline consent status."""

    path = consent_path(root)
    try:
        value = _read_json_file(path, missing_message="Samtykkemarkøren mangler")
    except OAuthError:
        return {"recorded": False, "current": False, "version": None, "recorded_at": None}
    version = value.get("version") if isinstance(value.get("version"), str) else None
    recorded_at = value.get("recorded_at") if isinstance(value.get("recorded_at"), str) else None
    current = version == CONSENT_VERSION and recorded_at is not None
    return {
        "recorded": version is not None and recorded_at is not None,
        "current": current,
        "version": version,
        "recorded_at": recorded_at,
    }


def record_consent(
    root: Path | str | None, *, version: str = CONSENT_VERSION, at: float | None = None
) -> dict[str, Any]:
    """Record the CLI-confirmed first-use consent without identity data."""

    if version != CONSENT_VERSION:
        raise OAuthError("Ukjent samtykkeversjon")
    timestamp = time.time() if at is None else at
    if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp <= 0:
        raise OAuthError("Ugyldig tidspunkt for samtykke")
    recorded_at = utc_iso(timestamp)
    _atomic_write_json(
        consent_path(root),
        {"version": version, "recorded_at": recorded_at},
        overwrite=True,
    )
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
    """Create a printable authorization URL and a separate in-memory transaction."""

    if not consent_status(root)["current"]:
        raise OAuthError("Eksplisitt samtykke må registreres før uoffisiell IST-innlogging")
    if _refresh_block_reason(root) == "fatal-client-configuration":
        raise OAuthFatalConfigurationError(
            "SkolID avviste IST-appregistreringen; provisioner klientkonfigurasjonen på nytt"
        )
    config = load_client_config(root)
    state = _random_urlsafe(random_bytes)
    nonce = _random_urlsafe(random_bytes)
    verifier = _random_urlsafe(random_bytes)
    created_at = time.monotonic() if now is None else now
    if not isinstance(created_at, (int, float)) or not math.isfinite(created_at):
        raise OAuthError("Ugyldig tidspunkt for innlogging")
    transaction = LoginTransaction(config, state, nonce, verifier, float(created_at))
    parameters = [
        ("client_id", config.client_id),
        ("redirect_uri", config.redirect_uri),
        ("response_type", "code"),
        ("scope", config.scope),
        ("state", state),
        ("nonce", nonce),
        ("code_challenge", pkce_challenge(verifier)),
        ("code_challenge_method", "S256"),
        ("prompt", "login"),
    ]
    return f"{AUTHORIZATION_ENDPOINT}?{urllib.parse.urlencode(parameters)}", transaction


def _session_json(
    tokens: Mapping[str, Any],
    config: ClientConfig,
    issued_at: float,
    subject: str,
) -> dict[str, Any]:
    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "client_secret": config.client_secret,
        "client_id": config.client_id,
        "scope": config.scope,
        "authority": config.authority,
        "redirect_uri": config.redirect_uri,
        "issued_at": issued_at,
        "expires_in": tokens["expires_in"],
        "subject": _validated_subject(subject),
    }


def _verify_userinfo_and_context(
    client: OAuthTransport, access_token: str, expected_subject: str
) -> None:
    userinfo = _safe_call(
        "SkolID userinfo kunne ikke bekreftes; økten ble ikke lagret",
        lambda: client.get_json(USERINFO_ENDPOINT, bearer_token=access_token),
    )
    if not isinstance(userinfo, Mapping) or userinfo.get("sub") != expected_subject:
        raise OAuthError("SkolID userinfo samsvarer ikke med den verifiserte økten")
    _verify_context(client, access_token)


def _context_outcome(client: OAuthTransport, access_token: str) -> str:
    """One getContext call, reduced to a fixed outcome name.

    No exception, response or token leaves this function. Only ``answered``
    confirms the session; every HTTP status, GraphQL error and unexpected
    shape is an answer that rejects it.
    """

    try:
        context = client.post_json(
            IST_GRAPHQL_ENDPOINT,
            {"query": MINIMAL_CONTEXT_QUERY, "variables": {}},
            bearer_token=access_token,
        )
    except _OAuthHTTPError as exc:
        return f"http_{int(exc.status_code)}"
    except (OAuthNoAnswer, ApiRequestNotSent):
        return "no_answer"
    except Exception:
        return "failed"
    if isinstance(context, Mapping) and context.get("errors"):
        return "graphql_errors"
    if (
        not isinstance(context, Mapping)
        or not isinstance(context.get("data"), Mapping)
        or not isinstance(context["data"].get("getContext"), Mapping)
    ):
        return "unexpected_data"
    return "answered"


def _verify_context(client: OAuthTransport, access_token: str) -> None:
    """Confirm that IST Home accepts the token before anything is saved.

    Only "no answer" (timeout, broken connection, not sent) is asked again;
    a rejection is final. IST Home has been seen to leave this call
    unanswered for over 30 seconds, twice in a row, while SkolID answered at
    once.
    """

    for attempt in range(1, CONTEXT_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(CONTEXT_RETRY_DELAY_SECONDS)
        started = time.monotonic()
        outcome = _context_outcome(client, access_token)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if outcome != "answered" or attempt > 1 or elapsed_ms >= SLOW_CONTEXT_MS:
            _log.warning(
                "ist_context_check attempt=%d outcome=%s elapsed_ms=%d",
                attempt,
                outcome,
                elapsed_ms,
            )
        if outcome == "answered":
            return
        if outcome != "no_answer":
            raise OAuthError("IST Home-konteksten kunne ikke bekreftes; økten ble ikke lagret")
    raise OAuthContextNoAnswer("IST Home svarte ikke på kontekstkontrollen; økten ble ikke lagret")


@_credential_mutation
@lifecycle.operation("ist-home", "login")
def login_with_callback(
    root: Path | str | None,
    transaction: LoginTransaction,
    callback_url: str,
    *,
    transport: OAuthTransport | None = None,
    token_validator: TokenValidator | None = None,
    access_token_validator: AccessTokenValidator | None = None,
    now: float | None = None,
    wall_time: float | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Complete one login and atomically replace the session after all checks.

    Token exchange is attempted exactly once.  A matched callback consumes the
    transaction before the request, so an unknown result requires a new login.
    """

    _reap_session_temporaries(root)
    current_config = load_client_config(root)
    if not _same_client_config(current_config, transaction.config):
        raise OAuthError("IST-klientkonfigurasjonen ble endret; start innloggingen på nytt")
    if not transaction.consumed and session_path(root).exists() and not replace_existing:
        raise OAuthError(
            "En lokal IST Home-økt finnes allerede; bekreft uttrykkelig at den skal erstattes"
        )
    try:
        code = _consume_callback(transaction, callback_url, now=now)
    except OAuthFatalConfigurationError:
        _mark_refresh_blocked(
            root,
            "fatal-client-configuration",
            at=time.time(),
        )
        raise
    client = transport or UrllibOAuthTransport()
    client = lifecycle.observe_transport(root, "ist-home", client, at=wall_time)
    token_value: Mapping[str, Any] | None = None
    token_failed = False
    fatal_configuration = False
    try:
        token_value = client.post_form(
            TOKEN_ENDPOINT,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": transaction.config.redirect_uri,
                "client_id": transaction.config.client_id,
                "client_secret": transaction.config.client_secret,
                "code_verifier": transaction.code_verifier,
                "scope": transaction.config.scope,
            },
        )
    except _OAuthHTTPError as exc:
        fatal_configuration = exc.oauth_error in {
            "invalid_client",
            "unauthorized_client",
            "invalid_scope",
            "invalid_request",
        }
        token_failed = not fatal_configuration
    except Exception:
        token_failed = True
    if fatal_configuration:
        _mark_refresh_blocked(
            root,
            "fatal-client-configuration",
            at=time.time(),
        )
        raise OAuthFatalConfigurationError(REGISTRATION_REJECTED)
    if token_failed:
        raise OAuthError("Tokenutvekslingen fikk et ukjent resultat; start ny innlogging")
    if not isinstance(token_value, Mapping):
        raise OAuthError("SkolID returnerte en ugyldig tokenrespons")
    tokens = _validated_token_response(token_value, transaction.config)

    jwks = _safe_call(
        "SkolID-nøklene kunne ikke hentes; økten ble ikke lagret",
        lambda: client.get_json(JWKS_URI),
    )
    if not isinstance(jwks, Mapping):
        raise OAuthError("SkolID returnerte ugyldige signeringsnøkler")
    validation_time = time.time() if wall_time is None else wall_time
    if not isinstance(validation_time, (int, float)) or not math.isfinite(validation_time):
        raise OAuthError("Ugyldig tidspunkt for tokenvalidering")
    validator = token_validator or _pyjwt_token_validator
    claims = _safe_call(
        "ID-tokenet kunne ikke verifiseres; økten ble ikke lagret",
        lambda: validator(
            tokens["id_token"],
            jwks,
            transaction.config,
            transaction.nonce,
            float(validation_time),
        ),
    )
    if not isinstance(claims, Mapping):
        raise OAuthError("ID-tokenet kunne ikke verifiseres; økten ble ikke lagret")
    claims = _validate_id_claims(
        claims,
        client_id=transaction.config.client_id,
        nonce=transaction.nonce,
        now=float(validation_time),
    )

    id_subject = _validated_subject(claims["sub"])
    access_validator = access_token_validator or _pyjwt_access_token_validator
    access_claims = _safe_call(
        "Access-tokenet kunne ikke verifiseres; økten ble ikke lagret",
        lambda: access_validator(
            tokens["access_token"], jwks, transaction.config, float(validation_time)
        ),
    )
    if not isinstance(access_claims, Mapping):
        raise OAuthError("Access-tokenet kunne ikke verifiseres; økten ble ikke lagret")
    access_claims = _validate_access_claims(
        access_claims,
        config=transaction.config,
        now=float(validation_time),
    )
    access_subject = _validated_subject(access_claims["sub"])
    if not hmac.compare_digest(access_subject.encode("utf-8"), id_subject.encode("utf-8")):
        raise OAuthError("ID-tokenet og access-tokenet tilhører ikke samme bruker")

    _verify_userinfo_and_context(client, tokens["access_token"], id_subject)

    _marker_reason, _marker_transaction, recovery_fingerprint = _refresh_block_state(root)
    session = _session_json(
        tokens,
        transaction.config,
        float(validation_time),
        id_subject,
    )
    if recovery_fingerprint is not None:
        session[REFRESH_RECOVERY_MARKER_FIELD] = recovery_fingerprint
    try:
        _atomic_write_json(session_path(root), session, overwrite=True)
    except OSError:
        raise OAuthError("Den verifiserte IST Home-økten kunne ikke lagres") from None
    marker_cleared = _finish_refresh_marker(
        root,
        at=float(validation_time),
    )
    recovery_metadata_cleared = (
        _discard_recovery_fingerprint(root, session) if marker_cleared else True
    )
    expires_at = float(validation_time) + float(tokens["expires_in"])
    result = {
        "authenticated": True,
        "expires_at": utc_iso(expires_at),
        "id_token_verified": True,
        "access_token_verified": True,
        "userinfo_verified": True,
        "context_verified": True,
    }
    if not marker_cleared:
        result["refresh_marker_cleanup_pending"] = True
    if not recovery_metadata_cleared:
        result["recovery_metadata_cleanup_pending"] = True
    return result


def _parse_session(value: Mapping[str, Any]) -> tuple[dict[str, Any], ClientConfig]:
    config = _parse_client_config(value)
    access_token = _safe_token(value.get("access_token"), "access-token")
    refresh_token = _safe_token(value.get("refresh_token"), "refresh-token")
    issued_at = _numeric(value.get("issued_at"), "issued_at", minimum=1, maximum=10**11)
    expires_in = _numeric(value.get("expires_in"), "expires_in", minimum=1, maximum=31_536_000)
    _decode_jwt_payload_unverified(access_token)
    parsed_session = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "client_secret": config.client_secret,
        "client_id": config.client_id,
        "scope": config.scope,
        "authority": config.authority,
        "redirect_uri": config.redirect_uri,
        "issued_at": issued_at,
        "expires_in": expires_in,
    }
    if value.get("subject") is not None:
        parsed_session["subject"] = _validated_subject(value.get("subject"))
    if value.get(REFRESH_TRANSACTION_FIELD) is not None:
        parsed_session[REFRESH_TRANSACTION_FIELD] = _validated_refresh_transaction_id(
            value.get(REFRESH_TRANSACTION_FIELD)
        )
    if value.get(REFRESH_RECOVERY_MARKER_FIELD) is not None:
        parsed_session[REFRESH_RECOVERY_MARKER_FIELD] = _validated_marker_fingerprint(
            value.get(REFRESH_RECOVERY_MARKER_FIELD)
        )
    return parsed_session, config


def _session_completes_refresh_marker(
    session: Mapping[str, Any],
    reason: str | None,
    transaction_id: str | None,
    marker_fingerprint: str | None,
) -> bool:
    if reason == "refresh-completed":
        return True
    session_transaction = session.get(REFRESH_TRANSACTION_FIELD)
    if (
        reason == "refresh-in-flight"
        and transaction_id is not None
        and isinstance(session_transaction, str)
        and hmac.compare_digest(session_transaction.encode("ascii"), transaction_id.encode("ascii"))
    ):
        return True
    recovery_fingerprint = session.get(REFRESH_RECOVERY_MARKER_FIELD)
    return (
        marker_fingerprint is not None
        and isinstance(recovery_fingerprint, str)
        and hmac.compare_digest(
            recovery_fingerprint.encode("ascii"), marker_fingerprint.encode("ascii")
        )
    )


def _read_validated_local_session(root: Path | str | None) -> dict[str, Any]:
    """Read one locally validated session through the hardened file boundary."""

    session, embedded_config = _parse_session(
        _read_json_file(session_path(root), missing_message="IST Home-økten mangler")
    )
    configured = load_client_config(root)
    if not _same_client_config(embedded_config, configured):
        raise OAuthError("IST Home-øktens klientkonfigurasjon samsvarer ikke")
    if not isinstance(session.get("subject"), str):
        raise OAuthReauthenticationRequired(
            "Legacy-økten mangler verifisert brukerbinding; importer økten eller logg inn på nytt"
        )
    return session


class OAuthRefreshUncertain(OAuthError):
    pass


def local_session_for_use(root: Path | str | None) -> dict[str, Any]:
    """Return the secret-bearing local session only when it is safe to use.

    This is an internal integration boundary for the IST collector.  It is
    deliberately stricter than ``auth_status``: consent, canonical client
    configuration, verified subject binding and refresh terminal state must all
    be valid before a token can leave this module.
    """

    block_reason, block_transaction, block_fingerprint = _refresh_block_state(root)
    if not consent_status(root)["current"]:
        if block_reason == "fatal-client-configuration":
            raise OAuthFatalConfigurationError(REGISTRATION_REJECTED)
        raise OAuthReauthenticationRequired(
            "Eksplisitt samtykke mangler; importer økten eller logg inn på nytt"
        )
    try:
        session = _read_validated_local_session(root)
    except OAuthReauthenticationRequired:
        raise
    except OAuthError:
        raise OAuthReauthenticationRequired(
            "IST Home-økten er ugyldig; importer økten eller logg inn på nytt"
        ) from None
    if block_reason is not None and not _session_completes_refresh_marker(
        session, block_reason, block_transaction, block_fingerprint
    ):
        if block_reason == "fatal-client-configuration":
            raise OAuthFatalConfigurationError(REGISTRATION_REJECTED)
        if block_reason == 'invalid-grant':
            raise OAuthReauthenticationRequired('IST-refresh-tokenet er avvist')
        raise OAuthRefreshUncertain("En tidligere refresh må avklares")
    return session


def _same_session_grant(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    for field_name in ("access_token", "refresh_token", "client_secret"):
        if not hmac.compare_digest(
            str(left.get(field_name, "")).encode("utf-8"),
            str(right.get(field_name, "")).encode("utf-8"),
        ):
            return False
    return all(
        left.get(field_name) == right.get(field_name)
        for field_name in (
            "client_id",
            "scope",
            "authority",
            "redirect_uri",
            "issued_at",
            "expires_in",
        )
    )


def _mapping_from_payload(payload: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    if not isinstance(payload, (str, bytes)) or len(payload) > MAX_JSON_BYTES:
        raise OAuthError("IST Home-økten har ugyldig format")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict):
        raise OAuthError("IST Home-økten har ugyldig format")
    return value


def _complete_refresh(
    root: Path | str | None,
    session: Mapping[str, Any],
    config: ClientConfig,
    token_value: Mapping[str, Any],
    client: OAuthTransport,
    validator: AccessTokenValidator,
    validation_time: float,
    expected_subject: str,
    refresh_transaction_id: str,
    *,
    verify_remote: bool = True,
) -> dict[str, Any]:
    tokens = _validated_refresh_response(token_value, session, config)
    jwks = _safe_call(
        "SkolID-nøklene kunne ikke hentes; den gamle økten er beholdt",
        lambda: client.get_json(JWKS_URI),
    )
    if not isinstance(jwks, Mapping):
        raise OAuthError("SkolID returnerte ugyldige signeringsnøkler")
    claims = _safe_call(
        "Det fornyede access-tokenet kunne ikke verifiseres; den gamle økten er beholdt",
        lambda: validator(tokens["access_token"], jwks, config, validation_time),
    )
    if not isinstance(claims, Mapping):
        raise OAuthError("Det fornyede access-tokenet kunne ikke verifiseres")
    claims = _validate_access_claims(claims, config=config, now=validation_time)
    refreshed_subject = _validated_subject(claims["sub"])
    if not hmac.compare_digest(refreshed_subject.encode("utf-8"), expected_subject.encode("utf-8")):
        raise OAuthReauthenticationRequired(
            "Den fornyede IST-økten tilhører en annen bruker; logg inn på nytt"
        )
    if verify_remote:
        _verify_userinfo_and_context(client, tokens["access_token"], expected_subject)

    replacement = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "client_secret": config.client_secret,
        "client_id": config.client_id,
        "scope": config.scope,
        "authority": config.authority,
        "redirect_uri": config.redirect_uri,
        "issued_at": validation_time,
        "expires_in": tokens["expires_in"],
        "subject": expected_subject,
        REFRESH_TRANSACTION_FIELD: refresh_transaction_id,
    }
    try:
        _atomic_write_json(session_path(root), replacement, overwrite=True)
    except OSError:
        raise OAuthError("Den verifiserte, fornyede IST Home-økten kunne ikke lagres") from None
    marker_cleared = _finish_refresh_marker(
        root,
        at=validation_time,
        transaction_id=refresh_transaction_id,
    )
    expiry = _numeric(claims.get("exp"), "access-token expiry", minimum=1, maximum=10**11)
    return {
        "refreshed": True,
        "refresh_token_rotated": not hmac.compare_digest(
            str(session["refresh_token"]).encode("utf-8"),
            str(tokens["refresh_token"]).encode("utf-8"),
        ),
        "access_expires_at": utc_iso(expiry),
        "access_token_verified": True,
        "userinfo_verified": verify_remote,
        "context_verified": verify_remote,
        "refresh_marker_cleanup_pending": not marker_cleared,
    }


@_credential_mutation
@lifecycle.operation('ist-home', 'refresh')
def refresh_session(
    root: Path | str | None,
    *,
    transport: OAuthTransport | None = None,
    access_token_validator: AccessTokenValidator | None = None,
    at: float | None = None,
) -> dict[str, Any]:
    """Refresh once, fully validate, then atomically replace the local session.

    A non-secret marker is written before the network request.  If the process
    loses the result, the provider rejects the grant, or post-response
    verification fails, future automatic refresh attempts remain blocked until
    an explicit successful login/import replaces the grant.
    """

    _reap_session_temporaries(root)
    block_reason, block_transaction, block_fingerprint = _refresh_block_state(root)
    if not consent_status(root)["current"]:
        if block_reason == "fatal-client-configuration":
            raise OAuthFatalConfigurationError(REGISTRATION_REJECTED)
        raise OAuthError("Eksplisitt samtykke må registreres før IST-økten fornyes")
    session = _read_validated_local_session(root)
    config = load_client_config(root)
    saved = (
        lifecycle.saved_response(root, 'ist-home', session, block_transaction)
        if block_reason == 'refresh-in-flight'
        else None
    )
    if (
        saved is None
        and block_reason is not None
        and not _session_completes_refresh_marker(
            session, block_reason, block_transaction, block_fingerprint
        )
    ):
        if block_reason == "fatal-client-configuration":
            raise OAuthFatalConfigurationError(REGISTRATION_REJECTED)
        if block_reason == 'invalid-grant':
            raise OAuthReauthenticationRequired('IST-refresh-tokenet er avvist')
        raise OAuthRefreshUncertain("En tidligere refresh må avklares")
    expected_subject = session.get("subject")
    if not isinstance(expected_subject, str):
        raise OAuthReauthenticationRequired(
            "Legacy-økten mangler verifisert brukerbinding; importer økten eller logg inn på nytt"
        )
    validation_time = time.time() if at is None else at
    if (
        not isinstance(validation_time, (int, float))
        or not math.isfinite(validation_time)
        or validation_time <= 0
    ):
        raise OAuthError("Ugyldig tidspunkt for tokenfornyelse")
    validation_time = float(validation_time)
    requested_at = validation_time

    if saved is not None:
        token_value = saved['response']
        validation_time = lifecycle.recovery_validation_time(
            token_value, saved['received_at'], requested_at
        )
        refresh_transaction_id = block_transaction
        client = lifecycle.observe_transport(
            root,
            'ist-home',
            transport or UrllibOAuthTransport(),
            at=validation_time,
            capture_response=False,
        )
        lifecycle.record(
            root, 'ist-home', 'response_recovered', at=time.time() if at is None else at
        )
    else:
        # Mark before the request: a refresh token may rotate even if the client
        # times out, is interrupted, or crashes before receiving/persisting it.
        refresh_transaction_id = _new_refresh_transaction_id()
        _mark_refresh_blocked(
            root,
            "refresh-in-flight",
            at=validation_time,
            transaction_id=refresh_transaction_id,
        )
        client = lifecycle.observe_transport(
            root,
            'ist-home',
            transport or UrllibOAuthTransport(),
            at=validation_time,
            capture_response=False,
        )
        token_value: Mapping[str, Any] | None = None
        rejected_error: str | None = None
        request_failed = False
        try:
            token_value = client.post_form(
                TOKEN_ENDPOINT,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": str(session["refresh_token"]),
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "scope": config.scope,
                },
            )
        except ApiRequestNotSent:
            _clear_refresh_block(root)
            raise OAuthRefreshUncertain(
                'Fornyelsesforespørselen ble ikke sendt; kan forsøkes ved neste runde'
            ) from None
        except _OAuthHTTPError as exc:
            rejected_error = exc.oauth_error
        except Exception:
            request_failed = True

        if rejected_error in {
            "invalid_client",
            "unauthorized_client",
            "invalid_scope",
            "invalid_request",
        }:
            _mark_refresh_blocked(
                root,
                "fatal-client-configuration",
                at=validation_time,
            )
            raise OAuthFatalConfigurationError(REGISTRATION_REJECTED)
        if rejected_error == 'invalid_grant':
            _mark_refresh_blocked(root, 'invalid-grant', at=validation_time)
            raise OAuthReauthenticationRequired('IST-refresh-tokenet er avvist')
        if rejected_error is not None or request_failed:
            raise OAuthRefreshUncertain(
                "Tokenfornyelsen har ukjent utfall; ikke gjenta forespørselen"
            )
        if not isinstance(token_value, Mapping):
            raise OAuthReauthenticationRequired(
                "Tokenfornyelsen returnerte ugyldige data; start ny innlogging"
            )

        # nbf/iat may be later than the request's start. Validate against the
        # receipt time, not against the clock from before the token was issued.
        validation_time = time.time() if at is None else float(at)
        lifecycle.save_response(
            root, 'ist-home', session, refresh_transaction_id, token_value, at=validation_time
        )

    validator = access_token_validator or _pyjwt_access_token_validator
    try:
        result = _complete_refresh(
            root,
            session,
            config,
            token_value,
            client,
            validator,
            validation_time,
            expected_subject,
            refresh_transaction_id,
            verify_remote=saved is None or requested_at <= saved['received_at'] + 60,
        )
        lifecycle.clear_response(root, 'ist-home')
        if (
            saved is not None
            and datetime.fromisoformat(result['access_expires_at']).timestamp() <= requested_at + 60
        ):
            return refresh_session(
                root, transport=transport, access_token_validator=access_token_validator, at=at
            )
        return result
    except OAuthReauthenticationRequired:
        raise
    except OAuthError:
        raise OAuthRefreshUncertain(
            "Tokenfornyelsen kunne ikke fullvalideres; svaret er lagret for gjenoppretting"
        ) from None


@_credential_mutation
@lifecycle.operation("ist-home", "import")
def import_session(
    root: Path | str | None,
    payload: str | bytes | Mapping[str, Any],
    *,
    transport: OAuthTransport | None = None,
    access_token_validator: AccessTokenValidator | None = None,
    at: float | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Remotely verify and atomically import a legacy flat session.

    The access token must pass pinned RS256/JWKS, issuer, ``miniforum`` audience,
    client-ID, scope and expiry validation.  ``userinfo`` and a minimal
    ``getContext`` call must also succeed before any private file is changed.
    """

    if not consent_status(root)["current"]:
        raise OAuthError("Eksplisitt samtykke må registreres før en IST Home-økt importeres")
    _reap_session_temporaries(root)
    value = _mapping_from_payload(payload)
    session, config = _parse_session(value)
    config_path = client_config_path(root)
    if config_path.exists():
        existing_config = load_client_config(root)
        if not _same_client_config(existing_config, config):
            raise OAuthError("IST Home-øktens klientkonfigurasjon samsvarer ikke")

    target = session_path(root)
    current: dict[str, Any] | None = None
    existing_reason: str | None = None
    if target.exists():
        try:
            current, current_config = _parse_session(
                _read_json_file(target, missing_message="IST Home-økten mangler")
            )
        except OAuthError:
            if not replace_existing:
                raise OAuthError(
                    "En eksisterende IST Home-økt er ugyldig; bekreft uttrykkelig erstatning"
                ) from None
        else:
            if not _same_client_config(current_config, config):
                raise OAuthError("IST Home-øktens klientkonfigurasjon samsvarer ikke")
            if float(current["issued_at"]) > float(session["issued_at"]):
                existing_reason = "newer-local-session"
            elif _same_session_grant(current, session):
                existing_reason = "same-local-session"
            elif not replace_existing:
                raise OAuthError(
                    "En lokal IST Home-økt finnes allerede; bekreft uttrykkelig at den skal erstattes"
                )
        if current is None and not replace_existing:
            raise OAuthError(
                "En lokal IST Home-økt finnes allerede; bekreft uttrykkelig at den skal erstattes"
            )

    block_reason, block_transaction, block_fingerprint = _refresh_block_state(root)
    block_is_active = block_reason is not None and (
        current is None
        or not _session_completes_refresh_marker(
            current,
            block_reason,
            block_transaction,
            block_fingerprint,
        )
    )
    if (
        block_is_active
        and current is not None
        and hmac.compare_digest(
            str(current["refresh_token"]).encode("utf-8"),
            str(session["refresh_token"]).encode("utf-8"),
        )
    ):
        raise OAuthReauthenticationRequired(
            "Den importerte økten gjenbruker en refresh-grant med ukjent resultat; "
            "logg inn på nytt eller importer en faktisk ny grant"
        )

    validation_time = time.time() if at is None else at
    if not isinstance(validation_time, (int, float)) or not math.isfinite(validation_time):
        raise OAuthError("Ugyldig tidspunkt for sesjonsimport")
    client = transport or UrllibOAuthTransport()
    jwks = _safe_call(
        "SkolID-nøklene kunne ikke hentes; økten ble ikke importert",
        lambda: client.get_json(JWKS_URI),
    )
    if not isinstance(jwks, Mapping):
        raise OAuthError("SkolID returnerte ugyldige signeringsnøkler")
    validator = access_token_validator or _pyjwt_access_token_validator
    claims = _safe_call(
        "Access-tokenet kunne ikke verifiseres; økten ble ikke importert",
        lambda: validator(str(session["access_token"]), jwks, config, float(validation_time)),
    )
    if not isinstance(claims, Mapping):
        raise OAuthError("Access-tokenet kunne ikke verifiseres; økten ble ikke importert")
    claims = _validate_access_claims(claims, config=config, now=float(validation_time))
    verified_subject = _validated_subject(claims["sub"])
    _verify_userinfo_and_context(client, str(session["access_token"]), verified_subject)

    if existing_reason == "newer-local-session" and current is not None:
        expiry = _numeric(
            _decode_jwt_payload_unverified(str(current["access_token"])).get("exp"),
            "access-token expiry",
            minimum=1,
            maximum=10**11,
        )
        return {
            "stored": False,
            "reason": existing_reason,
            "expires_at": utc_iso(expiry),
        }

    if existing_reason == "same-local-session" and current is not None:
        current_subject = current.get("subject")
        if isinstance(current_subject, str) and not hmac.compare_digest(
            current_subject.encode("utf-8"), verified_subject.encode("utf-8")
        ):
            raise OAuthError("Den lokale IST Home-økten har en annen brukerbinding")

    _marker_reason, _marker_transaction, recovery_fingerprint = _refresh_block_state(root)
    verified_session = dict(session)
    verified_session.pop(REFRESH_TRANSACTION_FIELD, None)
    verified_session.pop(REFRESH_RECOVERY_MARKER_FIELD, None)
    verified_session["subject"] = verified_subject
    if recovery_fingerprint is not None:
        verified_session[REFRESH_RECOVERY_MARKER_FIELD] = recovery_fingerprint

    if not config_path.exists():
        _atomic_write_json(config_path, config.as_private_json(), overwrite=False)
    _atomic_write_json(target, verified_session, overwrite=True)
    marker_cleared = _finish_refresh_marker(
        root,
        at=float(validation_time),
    )
    recovery_metadata_cleared = (
        _discard_recovery_fingerprint(root, verified_session) if marker_cleared else True
    )
    expiry = _numeric(
        _decode_jwt_payload_unverified(str(session["access_token"])).get("exp"),
        "access-token expiry",
        minimum=1,
        maximum=10**11,
    )
    result = {
        "stored": existing_reason != "same-local-session",
        **({"reason": existing_reason} if existing_reason is not None else {}),
        "expires_at": utc_iso(expiry),
    }
    if not marker_cleared:
        result["refresh_marker_cleanup_pending"] = True
    if not recovery_metadata_cleared:
        result["recovery_metadata_cleanup_pending"] = True
    return result


def auth_status(root: Path | str | None, *, at: float | None = None) -> dict[str, Any]:
    """Return local authentication status without network access or secrets."""

    configured = False
    try:
        load_client_config(root)
        configured = True
    except OAuthError:
        pass

    consent = consent_status(root)
    authenticated = False
    session: dict[str, Any] | None = None
    expires_at: str | None = None
    expired: bool | None = None
    try:
        if not consent["current"]:
            raise OAuthError("Eksplisitt samtykke mangler")
        session = _read_validated_local_session(root)
        expiry = _numeric(
            _decode_jwt_payload_unverified(str(session["access_token"])).get("exp"),
            "access-token expiry",
            minimum=1,
            maximum=10**11,
        )
        current = time.time() if at is None else at
        if not isinstance(current, (int, float)) or not math.isfinite(current):
            raise OAuthError("Ugyldig statustidspunkt")
        authenticated = True
        expires_at = utc_iso(expiry)
        expired = expiry <= float(current)
    except OAuthError:
        pass
    block_reason, block_transaction, block_fingerprint = _refresh_block_state(root)
    effective_block_reason = block_reason
    if session is not None and _session_completes_refresh_marker(
        session, block_reason, block_transaction, block_fingerprint
    ):
        effective_block_reason = "refresh-completed"
    return {
        "configured": configured,
        "consent_recorded": consent["current"],
        "authenticated": authenticated,
        "access_expires_at": expires_at,
        "access_expired": expired,
        "refresh_blocked": effective_block_reason not in {None, "refresh-completed"},
        "refresh_block_reason": effective_block_reason,
    }


@_credential_mutation(wait=True)
@lifecycle.operation("ist-home", "logout")
def logout(root: Path | str | None, *, transport: OAuthTransport | None = None) -> dict[str, Any]:
    """Best-effort revoke tokens, then always delete the local session.

    Remote failure is reported only as aggregate status; server bodies and
    injected exception messages are never returned or chained.
    """

    target = session_path(root)
    raw: Mapping[str, Any] | None = None
    try:
        parent_exists = validate_private_parent_tree(target)
    except PrivateStateError as exc:
        raise OAuthError(str(exc)) from None
    if parent_exists and target.exists() and not target.is_symlink():
        try:
            raw = _read_json_file(target, missing_message="IST Home-økten mangler")
        except OAuthError:
            raw = None
    had_session = parent_exists and (target.exists() or target.is_symlink())
    try:
        if parent_exists:
            target.unlink(missing_ok=True)
    except OSError:
        raise OAuthError("Den lokale IST Home-økten kunne ikke slettes") from None
    local_session_delete_durable = True
    if had_session:
        try:
            fsync_directory(target.parent)
        except (OSError, PrivateStateError):
            # The name is gone in the running system; continue revocation and
            # report that crash durability of the unlink could not be proven.
            local_session_delete_durable = False
    orphaned_session_files_removed = 0
    orphan_cleanup_complete = True
    try:
        orphaned_session_files_removed = _reap_session_temporaries(root)
    except OAuthError:
        orphan_cleanup_complete = False
    refresh_block_cleared = True
    try:
        _clear_refresh_block(root)
    except OAuthError:
        refresh_block_cleared = False

    attempts = 0
    successes = 0
    if raw is not None:
        try:
            config = load_client_config(root)
        except OAuthError:
            try:
                config = _parse_client_config(raw)
            except OAuthError:
                config = None
        if config is not None:
            tokens_to_revoke = []
            for field_name, hint in (
                ("refresh_token", "refresh_token"),
                ("access_token", "access_token"),
            ):
                try:
                    token = _safe_token(raw.get(field_name), hint)
                except OAuthError:
                    continue
                tokens_to_revoke.append((token, hint))
            attempts = len(tokens_to_revoke)
            try:
                client = transport or UrllibOAuthTransport()
            except Exception:
                client = None
            for token, hint in tokens_to_revoke:
                if client is None:
                    continue
                failed = False
                try:
                    client.post_form(
                        REVOCATION_ENDPOINT,
                        {
                            "token": token,
                            "token_type_hint": hint,
                            "client_id": config.client_id,
                            "client_secret": config.client_secret,
                        },
                        expect_json=False,
                    )
                except Exception:
                    failed = True
                if not failed:
                    successes += 1

    if attempts == 0:
        remote = "not-attempted" if had_session else "not-needed"
    elif successes == attempts:
        remote = "confirmed"
    elif successes:
        remote = "partial"
    else:
        remote = "unconfirmed"
    if orphaned_session_files_removed or not orphan_cleanup_complete:
        # A crash temp may contain a fully validated rotated grant that was
        # fsynced before the canonical replace.  Deleting it locally is safe,
        # but remote revocation can no longer be proven from the old session.
        remote = "unconfirmed"
    return {
        "logged_out": True,
        "local_session_deleted": had_session,
        "local_session_delete_durable": local_session_delete_durable,
        "orphaned_session_files_removed": orphaned_session_files_removed,
        "orphan_cleanup_complete": orphan_cleanup_complete,
        "refresh_block_cleared": refresh_block_cleared,
        "remote_revocation": remote,
    }
