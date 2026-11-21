"""Bounded reads from Hoopit's external API with a personal access token.

Hoopit documents the API at https://api.hoopit.io/external/docs/. The adapter
uses four member endpoints and nothing else:

- ``GET me/posts/``: posts the user or their children may read, newest first.
- ``GET posts/{post_id}/comments/``: a post's comments and replies, oldest first.
- ``GET me/occurrences/?from_date&to_date``: occurrences in a window with the
  family's answers.
- ``PATCH events/{event_id}/occurrences/{occurrence_id}/responses/{user_id}/``:
  an answer, only from ``live_hoopit.set_participation``.

Every endpoint is versioned on its own. ``ENDPOINTS`` pins the version each
call asks for with the ``Version`` header. A ``Deprecated-Version`` or
``Invalid-Version`` answer is stored in ``hoopit-api-version.json`` and shown
as ``api_version_warning`` in the status, so a coming change is never silent.

Traffic: requests are serial and at least one second apart. Hoopit allows
5,000 requests an hour per token, and one per second stays below that even
without a pause. A 429 is never retried in the same run: its ``Retry-After``
is stored and the run stops. Network failures and 5xx answers also store a
pause. The package's shared budget (``request_budget``) applies on top.

The API has no update time for posts or occurrences, so the adapter keeps a
small private list of the occurrence versions it has seen
(``hoopit-occurrence-versions.json``: IDs, hashes and times only). An
occurrence's ``changed_at`` is when this installation first saw its current
version.
"""

from __future__ import annotations

import logging
import math
import random
import re
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .api_transport import ApiHTTPError, ApiTransport, ApiTransportError
from .core import OSLO, project_paths
from .hoopit_auth import (  # noqa: F401 - HoopitScopeMissing is part of this API
    API_HOST,
    HoopitAccess,
    HoopitRemoteUnavailable,
    HoopitScopeMissing,
    HoopitTokenRejected,
    READ_SCOPES,
    RESPOND_SCOPE,
    SCOPE_LABELS,
    TOKEN_REJECTED_MESSAGE,
    api_version_path,
    mark_scope_missing,
    mark_token_rejected,
    rate_state_path,
    scope_missing_message,
    token_for_use,
)
from .private_lock import private_source_lock
from .private_state import (
    PrivateStateError,
    atomic_write_private_json,
    credential_path,
    read_private_json,
)
from .source_adapter import SourceBudgetExceeded, SourceUnavailable


SOURCE = "hoopit"
# Links for the user open the Hoopit web app.
APP_WEB_ROOT = "https://app.hoopit.io"
ORIGIN = f"https://{API_HOST}"

# name: (path pattern, pinned version, scope the call needs)
ENDPOINTS: dict[str, tuple[str, int, str]] = {
    "posts": ("/external/me/posts/", 1, "posts:read"),
    "comments": ("/external/posts/{post_id}/comments/", 1, "posts:read"),
    "occurrences": ("/external/me/occurrences/", 1, "events:read"),
    "response": (
        "/external/events/{event_id}/occurrences/{occurrence_id}/responses/{user_id}/",
        1,
        RESPOND_SCOPE,
    ),
}
# The lists whose 403 means that the token lacks the endpoint's scope.
SCOPE_ENDPOINTS = frozenset({"posts", "occurrences"})
_ENDPOINT_LABELS = {
    "posts": "innlegg",
    "comments": "kommentarer",
    "occurrences": "aktiviteter",
    "response": "svar på aktiviteter",
}

MAX_PAGES_PER_FEED = 20
MAX_COMMENT_PAGES = 10
MAX_REQUESTS = 300
MAX_POSTS = 250
MAX_EVENTS = 400
MAX_COMMENTS_PER_THREAD = 250
# One collection found more posts or events than MAX_POSTS/MAX_EVENTS allow.
TOO_MANY_ITEMS = "Hoopit returnerte flere elementer enn innhentingsgrensen"
MAX_JSON_BYTES = 25 * 1024 * 1024
MIN_REQUEST_INTERVAL_SECONDS = 1.0
NETWORK_COOLDOWN_SECONDS = 5 * 60
SERVER_COOLDOWN_BASE_SECONDS = 15 * 60
SERVER_COOLDOWN_MAX_SECONDS = 6 * 60 * 60
RATE_STATE_VERSION = 1
# Posts older than the window still get their comments checked this far back,
# because a new comment on an older post is new content.
COMMENT_LOOKBACK = timedelta(days=14)
# Occurrences are read from the window start to this far ahead of now.
EVENT_HORIZON = timedelta(days=120)
# Without a day in the reference, an occurrence is looked up in this window.
LOOKUP_PAST = timedelta(days=60)
LOOKUP_FUTURE = timedelta(days=240)
# me/occurrences answers 400 when from_date and to_date are more than 366 days
# apart, so a longer window is read in parts of at most this length.
MAX_OCCURRENCE_WINDOW = timedelta(days=365)
VERSIONS_FILE = "hoopit-occurrence-versions.json"
VERSIONS_SCHEMA = 1
MAX_VERSIONS = 3000
VERSION_RETENTION = timedelta(days=180)

RESPONSE_STATES = {
    "not_answered": "unanswered",
    "accepted": "accepted",
    "declined": "declined",
    "waiting_list": "waitlisted",
    "maybe": "maybe",
}
RESPONSE_LABELS = {
    "not_answered": "har ikke svart",
    "accepted": "kommer",
    "declined": "kommer ikke",
    "waiting_list": "på venteliste",
    "maybe": "kanskje",
}
EVENT_TYPES = {
    "default": "Aktivitet",
    "match": "Kamp",
    "meeting": "Møte",
    "dugnad": "Dugnad",
    "training": "Trening",
    "competition": "Konkurranse",
}
DECLINE_REASONS = ("illness", "injury", "other_training", "other_reason")

_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_HEADER_VALUE = re.compile(r"[ -~]{1,64}\Z")
_log = logging.getLogger(__name__)


class HoopitTransport(Protocol):
    def get_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> Any: ...

    def patch_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> Any: ...


class HoopitCooldownActive(SourceUnavailable):
    """A previous remote throttle/failure forbids traffic in this run."""

    def __init__(self, cooldown_until: float, reason: str) -> None:
        self.cooldown_until = cooldown_until
        self.reason = reason
        super().__init__("Hoopit er i en lagret pause etter forrige API-forsøk")


class HoopitRateLimited(HoopitCooldownActive):
    """The current request received 429 and must end without retry."""


class RequestBudgetExceeded(SourceBudgetExceeded):
    """A hard request/page/item budget was reached."""


class HoopitRunStopped(HoopitRemoteUnavailable):
    """A global cooldown/network/server condition forbids more requests."""


class HoopitItemUnavailable(SourceUnavailable):
    """One resource failed without imposing a global cooldown."""


class HoopitRequestRefused(HoopitItemUnavailable):
    """Hoopit refused one request with a 4xx answer other than 401, 403 or 429."""

    def __init__(self, message: str, *, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(message)


def _clock_value(clock: Callable[[], float]) -> float:
    value = clock()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise SourceUnavailable("Hoopit-klokken returnerte ugyldig verdi")
    return float(value)


class PersistentRateLimiter:
    """Serial pacer and durable cooldown state for one Hoopit installation."""

    def __init__(
        self,
        root: Path | str | None,
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        minimum_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        if (
            isinstance(minimum_interval, bool)
            or not isinstance(minimum_interval, (int, float))
            or not math.isfinite(float(minimum_interval))
            or float(minimum_interval) < MIN_REQUEST_INTERVAL_SECONDS
        ):
            raise SourceUnavailable("Hoopit-frekvensgrensen er for lav")
        self.root = project_paths(root).root
        self.clock = clock
        self.sleeper = sleeper
        self.jitter = jitter
        self.minimum_interval = float(minimum_interval)

    @property
    def path(self) -> Path:
        return rate_state_path(self.root)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists() and not self.path.is_symlink():
            return {
                "version": RATE_STATE_VERSION,
                "last_request_at": None,
                "cooldown_until": None,
                "reason": None,
                "retry_after_seconds": None,
                "server_failure_count": 0,
            }
        try:
            raw = read_private_json(self.path, max_bytes=64 * 1024)
        except PrivateStateError:
            raise SourceUnavailable("Hoopit-frekvenstilstanden kunne ikke leses") from None
        if not isinstance(raw, Mapping) or raw.get("version") != RATE_STATE_VERSION:
            raise SourceUnavailable("Hoopit-frekvenstilstanden har ugyldig format")
        allowed = {
            "version",
            "last_request_at",
            "cooldown_until",
            "reason",
            "retry_after_seconds",
            "server_failure_count",
            "recorded_at",
        }
        if any(key not in allowed for key in raw):
            raise SourceUnavailable("Hoopit-frekvenstilstanden har ukjente felt")
        result = dict(raw)
        for key in ("last_request_at", "cooldown_until"):
            value = result.get(key)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise SourceUnavailable("Hoopit-frekvenstilstanden har ugyldig tid")
        count = result.get("server_failure_count", 0)
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 100:
            raise SourceUnavailable("Hoopit-frekvenstilstanden har ugyldig feilteller")
        return result

    def _write(self, value: Mapping[str, Any]) -> None:
        payload = dict(value)
        payload["version"] = RATE_STATE_VERSION
        payload["recorded_at"] = datetime.fromtimestamp(_clock_value(self.clock), UTC).isoformat(
            timespec="seconds"
        )
        try:
            atomic_write_private_json(self.path, payload)
        except PrivateStateError:
            raise SourceUnavailable("Hoopit-frekvenstilstanden kunne ikke lagres") from None

    def before_request(self) -> None:
        """Wait only for serial pacing; an active cooldown raises before traffic."""

        with private_source_lock(self.root, SOURCE):
            state = self._read()
            now = _clock_value(self.clock)
            cooldown = state.get("cooldown_until")
            if isinstance(cooldown, (int, float)) and float(cooldown) > now:
                raise HoopitCooldownActive(float(cooldown), str(state.get("reason") or "unknown"))
            last = state.get("last_request_at")
            if isinstance(last, (int, float)):
                delay = self.minimum_interval - (now - float(last))
                if delay > 0:
                    try:
                        self.sleeper(delay)
                    except Exception:
                        raise SourceUnavailable("Hoopit-pausen kunne ikke vente trygt") from None
                    now = _clock_value(self.clock)
                    if now + 0.001 < float(last) + self.minimum_interval:
                        raise SourceUnavailable("Hoopit-klokken gikk ikke fremover")
            state["last_request_at"] = now
            from .request_budget import reserve

            reserve(self.root, SOURCE, at=now)
            if isinstance(cooldown, (int, float)) and float(cooldown) <= now:
                state["cooldown_until"] = None
                state["reason"] = None
                state["retry_after_seconds"] = None
            self._write(state)

    def record_success(self) -> None:
        with private_source_lock(self.root, SOURCE):
            state = self._read()
            state["server_failure_count"] = 0
            now = _clock_value(self.clock)
            cooldown = state.get("cooldown_until")
            if not isinstance(cooldown, (int, float)) or float(cooldown) <= now:
                state["cooldown_until"] = None
                state["reason"] = None
                state["retry_after_seconds"] = None
            self._write(state)

    def record_http_failure(self, status_code: int, retry_after: int | None = None) -> None:
        """Persist a stop condition. Nothing here is a retry signal."""

        with private_source_lock(self.root, SOURCE):
            state = self._read()
            now = _clock_value(self.clock)
            if status_code == 429:
                if isinstance(retry_after, bool) or (
                    retry_after is not None
                    and (not isinstance(retry_after, int) or retry_after < 0)
                ):
                    retry_after = None
                delay = max(60, retry_after if retry_after is not None else 15 * 60)
                state.update(
                    {
                        "cooldown_until": now + delay,
                        "reason": "http-429",
                        "retry_after_seconds": retry_after,
                    }
                )
            elif 500 <= status_code <= 599:
                failures = min(int(state.get("server_failure_count", 0)) + 1, 10)
                try:
                    jitter = float(self.jitter(0, 5 * 60))
                except Exception:
                    jitter = 5 * 60
                if not math.isfinite(jitter) or jitter < 0:
                    jitter = 5 * 60
                delay = min(
                    SERVER_COOLDOWN_BASE_SECONDS * (2 ** (failures - 1)) + jitter,
                    SERVER_COOLDOWN_MAX_SECONDS,
                )
                state.update(
                    {
                        "server_failure_count": failures,
                        "cooldown_until": now + delay,
                        "reason": "http-5xx",
                        "retry_after_seconds": None,
                    }
                )
            else:
                return
            self._write(state)

    def record_network_failure(self) -> None:
        with private_source_lock(self.root, SOURCE):
            state = self._read()
            now = _clock_value(self.clock)
            state.update(
                {
                    "cooldown_until": now + NETWORK_COOLDOWN_SECONDS,
                    "reason": "network",
                    "retry_after_seconds": None,
                }
            )
            self._write(state)


@dataclass
class RequestBudget:
    maximum: int = MAX_REQUESTS
    used: int = 0

    def consume(self) -> None:
        if self.used >= self.maximum:
            raise RequestBudgetExceeded("Hoopit nådde den harde forespørselsgrensen")
        self.used += 1


class BudgetedRateLimiter:
    """Count all traffic of one run against one total budget."""

    def __init__(self, limiter: PersistentRateLimiter, budget: RequestBudget) -> None:
        self.limiter = limiter
        self.budget = budget

    def before_request(self) -> None:
        self.budget.consume()
        self.limiter.before_request()

    def record_success(self) -> None:
        self.limiter.record_success()

    def record_http_failure(self, status_code: int, retry_after: int | None = None) -> None:
        self.limiter.record_http_failure(status_code, retry_after)

    def record_network_failure(self) -> None:
        self.limiter.record_network_failure()


def default_transport() -> ApiTransport:
    return ApiTransport(allowed_hosts={API_HOST}, timeout=45, max_json_bytes=MAX_JSON_BYTES)


def identifier(value: Any, label: str) -> str:
    """A Hoopit ID: a positive integer, as JSON number or decimal text."""

    if type(value) is int and value > 0:
        text = str(value)
    elif isinstance(value, str) and value.isascii():
        text = value
    else:
        text = ""
    if not _ID.fullmatch(text):
        raise SourceUnavailable(f"Hoopit returnerte ugyldig {label}")
    return text


def _iso_query(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def endpoint_url(name: str, query: Mapping[str, Any] | None = None, **ids: str) -> str:
    """Build the URL of one allowlisted endpoint; IDs must be Hoopit IDs."""

    pattern, _version, _scope = ENDPOINTS[name]
    path = pattern.format(**{key: identifier(value, "ID") for key, value in ids.items()})
    suffix = ""
    if query:
        page = query.get("page")
        if page is not None and (type(page) is not int or not 1 <= page <= MAX_PAGES_PER_FEED):
            raise SourceUnavailable("Hoopit-adapteren nektet et ugyldig sidetall")
        suffix = "?" + urllib.parse.urlencode(query)
    return ORIGIN + path + suffix


def _version_header_value(value: Any) -> str | None:
    if isinstance(value, str) and _HEADER_VALUE.fullmatch(value.strip()):
        return value.strip()
    return None


def version_warning_text(state: Mapping[str, Any]) -> str | None:
    """The status text for stored version answers, or None."""

    endpoints = state.get("endpoints")
    if not isinstance(endpoints, Mapping):
        return None
    lines = []
    for name in sorted(endpoints):
        entry = endpoints[name]
        if name not in ENDPOINTS or not isinstance(entry, Mapping):
            continue
        label = _ENDPOINT_LABELS[name]
        requested = ENDPOINTS[name][1]
        if entry.get("invalid"):
            lines.append(
                f"Hoopit kjenner ikke versjon {requested} av API-et for {label}. "
                "Svarene kan ha endret form."
            )
        elif entry.get("deprecated"):
            lines.append(
                f"Hoopit har varslet at versjon {requested} av API-et for {label} fases ut."
            )
    if not lines:
        return None
    return " ".join(lines) + " Oppdater familiekilder-mcp eller meld fra til utviklerne."


class VersionWatch:
    """Store Deprecated-Version and Invalid-Version answers per endpoint."""

    def __init__(self, root: Path | str | None) -> None:
        self.root = root
        self.warned: set[str] = set()

    def observe(self, name: str, headers: Mapping[str, str] | None) -> None:
        if self.root is None:
            return
        lowered = {str(key).lower(): value for key, value in (headers or {}).items()}
        deprecated = _version_header_value(lowered.get("deprecated-version"))
        invalid = _version_header_value(lowered.get("invalid-version"))
        path = api_version_path(self.root)
        try:
            with private_source_lock(self.root, SOURCE):
                present = path.exists() or path.is_symlink()
                state = read_private_json(path, max_bytes=64 * 1024) if present else {}
                if not isinstance(state, Mapping):
                    state = {}
                endpoints = dict(state.get("endpoints") or {})
                if deprecated is None and invalid is None:
                    if name not in endpoints:
                        return
                    endpoints.pop(name)
                else:
                    entry = {
                        "requested": ENDPOINTS[name][1],
                        "deprecated": deprecated,
                        "invalid": invalid,
                        "seen_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    }
                    if (
                        endpoints.get(name, {}).get("deprecated") == deprecated
                        and endpoints.get(name, {}).get("invalid") == invalid
                    ):
                        return
                    endpoints[name] = entry
                atomic_write_private_json(path, {"version": 1, "endpoints": endpoints})
        except PrivateStateError:
            return
        if (deprecated or invalid) and name not in self.warned:
            self.warned.add(name)
            _log.warning(
                "Hoopit answered %s for the %s endpoint (requested version %d)",
                "Invalid-Version" if invalid else "Deprecated-Version",
                name,
                ENDPOINTS[name][1],
            )


class Client:
    """One read run's authorized client: pinned versions, pacing and error mapping."""

    def __init__(
        self,
        access: HoopitAccess,
        transport: HoopitTransport,
        limiter: Any,
        *,
        root: Path | str | None = None,
    ) -> None:
        self.access = access
        self.transport = transport
        self.limiter = limiter
        self.root = Path(root) if root is not None else None
        self.versions = VersionWatch(self.root)

    def _headers(self, name: str) -> dict[str, str]:
        return {"Version": str(ENDPOINTS[name][1]), "Accept-Language": "nb-NO"}

    def _begin(self) -> None:
        self.limiter.before_request()

    def _failure(self, name: str, exc: Exception) -> None:
        if isinstance(exc, ApiHTTPError):
            self.versions.observe(name, getattr(exc, "headers", None))
            self.limiter.record_http_failure(exc.status_code, exc.retry_after_seconds)
            if exc.status_code == 401:
                if self.root is not None:
                    mark_token_rejected(self.root)
                raise HoopitTokenRejected(TOKEN_REJECTED_MESSAGE) from None
            if exc.status_code == 403 and name in SCOPE_ENDPOINTS:
                # The member lists answer 403 only for a missing scope. A 403
                # on one post or one answer is about that item, not the token.
                scope = ENDPOINTS[name][2]
                if self.root is not None:
                    mark_scope_missing(self.root, scope)
                raise HoopitScopeMissing(scope_missing_message(scope), scope=scope) from None
            if exc.status_code == 403 and name == "response":
                raise HoopitRequestRefused(
                    "Hoopit avviste svaret (HTTP 403). Tokenet kan mangle "
                    f"«{SCOPE_LABELS['events:respond']}», eller Hoopit tillater ikke svar "
                    "for denne personen.",
                    status_code=403,
                ) from None
            if exc.status_code == 429:
                until = time.time() + (exc.retry_after_seconds or 15 * 60)
                raise HoopitRateLimited(until, "http-429") from None
            if 500 <= exc.status_code <= 599:
                raise HoopitRunStopped(
                    "Hoopit hadde en serverfeil; lesingen ble stoppet uten nytt forsøk",
                    reason="backend_error",
                    retry_after_seconds=exc.retry_after_seconds,
                ) from None
            raise HoopitRequestRefused(
                f"Hoopit avviste forespørselen (HTTP {exc.status_code})",
                status_code=exc.status_code,
            ) from None
        self.limiter.record_network_failure()
        raise HoopitRunStopped(
            "Hoopit kunne ikke nås; lesingen ble stoppet uten nytt forsøk", reason="transient"
        ) from None

    def get(self, name: str, query: Mapping[str, Any] | None = None, **ids: str) -> Any:
        url = endpoint_url(name, query, **ids)
        from .sync_snapshot import memo_read

        return memo_read(
            self.root, SOURCE, ("GET", url, ENDPOINTS[name][1]), lambda: self._get(name, url)
        )

    def _get(self, name: str, url: str) -> Any:
        self._begin()
        headers: dict[str, str] = {}
        try:
            result = self.transport.get_json(
                url,
                bearer_token=self.access.token,
                headers=self._headers(name),
                response_headers=headers,
            )
        except (ApiHTTPError, ApiTransportError, OSError, TimeoutError) as exc:
            self._failure(name, exc)
            raise AssertionError("unreachable")
        self.limiter.record_success()
        self.versions.observe(name, headers)
        return result

    def patch(self, name: str, payload: Mapping[str, Any], **ids: str) -> Any:
        url = endpoint_url(name, None, **ids)
        self._begin()
        headers: dict[str, str] = {}
        try:
            result = self.transport.patch_json(
                url,
                dict(payload),
                bearer_token=self.access.token,
                headers=self._headers(name),
                response_headers=headers,
            )
        except (ApiHTTPError, ApiTransportError, OSError, TimeoutError) as exc:
            self._failure(name, exc)
            raise AssertionError("unreachable")
        self.limiter.record_success()
        self.versions.observe(name, headers)
        return result


def require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceUnavailable(f"Hoopit returnerte ugyldig {label}")
    return value


def _page(value: Any, label: str) -> tuple[list[dict[str, Any]], bool]:
    mapping = require_mapping(value, label)
    results = mapping.get("results")
    has_next = mapping.get("has_next")
    if not isinstance(results, list) or not isinstance(has_next, bool):
        raise SourceUnavailable(f"Hoopit returnerte ugyldig {label}")
    if any(not isinstance(item, Mapping) for item in results):
        raise SourceUnavailable(f"Hoopit returnerte ugyldig {label}")
    return [dict(item) for item in results], has_next


def iter_pages(
    client: Client,
    name: str,
    *,
    label: str,
    maximum_pages: int,
    query: Mapping[str, Any] | None = None,
    **ids: str,
) -> Iterator[list[dict[str, Any]]]:
    """Yield one page of results at a time until ``has_next`` is false."""

    seen_pages: set[tuple[str, ...]] = set()
    for page_number in range(1, maximum_pages + 1):
        values, has_next = _page(
            client.get(name, {**(query or {}), "page": page_number}, **ids), label
        )
        fingerprint = tuple(identifier(value.get("id"), label) for value in values)
        if values and fingerprint in seen_pages:
            raise SourceUnavailable("Hoopit gjentok en side; lesingen er ufullstendig")
        seen_pages.add(fingerprint)
        yield values
        if not has_next:
            return
        if not values:
            raise SourceUnavailable("Hoopit returnerte en tom side med flere sider igjen")
    raise RequestBudgetExceeded(f"Hoopit returnerte for mange sider med {label}")


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _pick(mapping: Mapping[str, Any] | None, *names: str) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(UTC)


def _iso(value: Any) -> str | None:
    parsed = _parse_time(value)
    return parsed.isoformat(timespec="seconds") if parsed else None


def _person_name(value: Any) -> str | None:
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, Mapping):
        return None
    direct = _pick(value, "name", "full_name", "display_name")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts = [_text(value.get("first_name")), _text(value.get("last_name"))]
    return " ".join(part for part in parts if part) or None


def post_comments(client: Client, post_id: str) -> list[dict[str, Any]]:
    """All comments of one post as a two-level tree, in Hoopit's order.

    Hoopit returns replies in the same list with ``parent_id``. A reply is
    placed under the top-level comment it belongs to, in ``_replies``, so the
    text has comment lines with the replies indented below them.
    """

    rows: list[dict[str, Any]] = []
    for page in iter_pages(
        client,
        "comments",
        label="kommentarer",
        maximum_pages=MAX_COMMENT_PAGES,
        post_id=post_id,
    ):
        rows.extend(page)
        if len(rows) > MAX_COMMENTS_PER_THREAD:
            raise RequestBudgetExceeded("Hoopit returnerte for mange kommentarer")
    return comment_tree(rows)


def comment_tree(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        ident = identifier(row.get("id"), "kommentar-ID")
        if ident in by_id:
            raise SourceUnavailable("Hoopit returnerte dupliserte kommentarer")
        if _parse_time(row.get("created_at")) is None:
            raise SourceUnavailable("Hoopit-kommentaren mangler en pålitelig tidsangivelse")
        by_id[ident] = dict(row)

    def root_of(ident: str) -> str:
        seen = set()
        current = ident
        while True:
            parent = by_id[current].get("parent_id")
            if parent is None:
                return current
            parent_id = identifier(parent, "kommentar-ID")
            if parent_id not in by_id or parent_id in seen:
                # A missing parent (deleted, or on another page) keeps the
                # reply visible at the top level instead of dropping it.
                return current
            seen.add(current)
            current = parent_id

    roots: list[dict[str, Any]] = []
    for ident, row in by_id.items():
        top = root_of(ident)
        if top == ident:
            roots.append(row)
        else:
            by_id[top].setdefault("_replies", []).append(row)
    return roots


def _comment_sender(comment: Mapping[str, Any]) -> str | None:
    return _person_name(_pick(comment, "poster", "author", "user")) or _text(
        _pick(comment, "poster_name", "author_name")
    )


def _comment_text(comment: Mapping[str, Any]) -> str:
    return _text(_pick(comment, "comment", "text", "body")) or "Kommentar uten tekst"


def _comment_lines(comments: Any, *, indent: str = "- ") -> list[str]:
    if not isinstance(comments, list):
        return []
    lines: list[str] = []
    for raw in comments:
        if not isinstance(raw, Mapping):
            continue
        sender = _comment_sender(raw) or "Ukjent avsender"
        timestamp = _iso(_pick(raw, "timestamp", "created_at"))
        prefix = f"{sender} ({timestamp})" if timestamp else sender
        lines.append(f"{indent}{prefix}: {_comment_text(raw)}")
        lines.extend(_comment_lines(raw.get("_replies"), indent="  ↳ "))
    return lines


def _with_comments(body: str, comments: Any) -> str:
    lines = _comment_lines(comments)
    if not lines:
        return body or "Ingen tekst"
    return (body.strip() + "\n\nKommentarer:\n" + "\n".join(lines)).strip()


def _thread_latest_time(comments: Any) -> datetime | None:
    if not isinstance(comments, list):
        raise SourceUnavailable("Hoopit returnerte ugyldig kommentarliste")
    latest: datetime | None = None
    pending = list(comments)
    while pending:
        raw = pending.pop()
        if not isinstance(raw, Mapping):
            raise SourceUnavailable("Hoopit returnerte en ugyldig kommentar")
        stamp = _parse_time(_pick(raw, "created_at", "timestamp"))
        if stamp is None:
            raise SourceUnavailable("Hoopit-kommentaren mangler en pålitelig tidsangivelse")
        latest = stamp if latest is None or stamp > latest else latest
        replies = raw.get("_replies")
        if replies is not None:
            if not isinstance(replies, list):
                raise SourceUnavailable("Hoopit returnerte ugyldige kommentarsvar")
            pending.extend(replies)
    return latest


def fetch_posts(
    client: Client,
    cutoff: datetime,
    *,
    on_record: Callable[[str, Mapping[str, Any]], None] | None = None,
    records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Posts with activity since ``cutoff``: new posts and new comments.

    The list is newest first by creation time. Paging stops after a page whose
    posts are all older than the comment lookback. Records are appended to
    ``records`` as they are read, so a caller keeps them when a limit stops
    the read. A post whose comments Hoopit refuses is kept with
    ``comments_unavailable``.
    """

    floor = cutoff - COMMENT_LOOKBACK
    records = [] if records is None else records
    seen: set[str] = set()
    for page in iter_pages(client, "posts", label="innlegg", maximum_pages=MAX_PAGES_PER_FEED):
        all_old = bool(page)
        for post in page:
            post_id = identifier(post.get("id"), "innleggs-ID")
            created = _parse_time(post.get("created_at"))
            if created is None:
                raise SourceUnavailable("Hoopit-innlegget mangler en pålitelig tidsangivelse")
            if created >= floor:
                all_old = False
            if post_id in seen or created < floor:
                continue
            seen.add(post_id)
            if len(seen) > MAX_POSTS:
                raise RequestBudgetExceeded(TOO_MANY_ITEMS)
            try:
                comments, unavailable = post_comments(client, post_id), False
            except HoopitItemUnavailable:
                comments, unavailable = [], True
            latest = max(t for t in (created, _thread_latest_time(comments)) if t is not None)
            if latest < cutoff and not unavailable:
                continue
            record = {"post": post, "comments": comments}
            if unavailable:
                record["comments_unavailable"] = True
            records.append(record)
            if on_record is not None:
                on_record("posts", record)
        if all_old:
            break
    return records


def fetch_occurrences(
    client: Client,
    start: datetime,
    end: datetime,
    *,
    maximum_pages: int = MAX_PAGES_PER_FEED,
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if end < start:
        raise SourceUnavailable("Hoopit-vinduet har ugyldig sluttid")
    rows = [] if rows is None else rows
    seen: set[tuple[str, str]] = set()
    part_start = start
    while True:
        part_end = min(end, part_start + MAX_OCCURRENCE_WINDOW)
        query = {"from_date": _iso_query(part_start), "to_date": _iso_query(part_end)}
        for page in iter_pages(
            client, "occurrences", label="aktiviteter", maximum_pages=maximum_pages, query=query
        ):
            for row in page:
                key = (
                    identifier(row.get("event_id"), "aktivitets-ID"),
                    identifier(row.get("id"), "forekomst-ID"),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
                if len(rows) > MAX_EVENTS:
                    raise RequestBudgetExceeded(TOO_MANY_ITEMS)
        if part_end >= end:
            return rows
        part_start = part_end


def fetch_snapshot(
    client: Client,
    cutoff_at: str,
    *,
    now: datetime | None = None,
    on_record: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Fetch one bounded read snapshot; every network operation is a GET.

    A limit (pages, items or requests) keeps what was read and is named in
    ``limits``; the read is then partial, not failed. Other failures raise.
    """

    cutoff = _parse_time(cutoff_at)
    if cutoff is None:
        raise SourceUnavailable("Hoopit-vinduet har ugyldig starttid")
    current = now or datetime.now(UTC)
    posts: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    limits: list[str] = []
    try:
        fetch_posts(client, cutoff, on_record=on_record, records=posts)
    except RequestBudgetExceeded as exc:
        limits.append(f"Innlegg: {exc}")
    try:
        fetch_occurrences(client, cutoff, max(cutoff, current) + EVENT_HORIZON, rows=events)
    except RequestBudgetExceeded as exc:
        limits.append(f"Aktiviteter: {exc}")
    if any(record.get("comments_unavailable") for record in posts):
        limits.append("Hoopit ga ikke kommentarene til minst ett innlegg.")
    for text in limits:
        _log.warning("Hoopit read is partial: %s", text)
    if on_record is not None:
        for row in events:
            on_record("events", row)
    return {"posts": posts, "events": events, "limits": limits}


def find_occurrence(
    client: Client, event_id: str, occurrence_id: str, day: str | None = None
) -> dict[str, Any]:
    """The one occurrence with these IDs, from the member occurrence list.

    Members have no detail endpoint. With the occurrence's day the lookup is a
    few days wide; without it, a wide window is paged.
    """

    event_id = identifier(event_id, "aktivitets-ID")
    occurrence_id = identifier(occurrence_id, "forekomst-ID")
    if day is not None:
        try:
            date = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=OSLO)
        except (TypeError, ValueError):
            raise SourceUnavailable("Hoopit-referansen har ugyldig dag") from None
        start, end = date - timedelta(days=1), date + timedelta(days=2)
    else:
        current = datetime.now(UTC)
        start, end = current - LOOKUP_PAST, current + LOOKUP_FUTURE
    try:
        rows = fetch_occurrences(client, start, end)
    except RequestBudgetExceeded:
        rows = []
    for row in rows:
        if (
            identifier(row.get("event_id"), "aktivitets-ID") == event_id
            and identifier(row.get("id"), "forekomst-ID") == occurrence_id
        ):
            return row
    raise HoopitItemUnavailable(
        "Fant ikke aktiviteten i Hoopit. Den kan være slettet, eller familien er ikke invitert lenger."
    )


def verify_token(
    root: Path | str | None,
    token: str,
    *,
    transport: HoopitTransport | None = None,
    rate_limiter: Any = None,
    at: float | None = None,
) -> dict[str, Any]:
    """One page of posts and one page of occurrences with a new token, no retry.

    Called by ``hoopit_auth.connect_token`` before the token is stored. A 401
    means the token is expired or withdrawn; a 403 names the missing scope.
    """

    limiter = rate_limiter or BudgetedRateLimiter(PersistentRateLimiter(root), RequestBudget(4))
    # No root: a failure here must not mark a stored, working token as rejected.
    client = Client(
        HoopitAccess(token=token, account_scope="0" * 64), transport or default_transport(), limiter
    )
    client.versions = VersionWatch(root)
    posts, _ = _page(client._get("posts", endpoint_url("posts", {"page": 1})), "innlegg")
    instant = datetime.fromtimestamp(time.time() if at is None else at, UTC)
    occurrences, _ = _page(
        client._get(
            "occurrences",
            endpoint_url(
                "occurrences",
                {
                    "from_date": _iso_query(instant),
                    "to_date": _iso_query(instant + timedelta(days=14)),
                    "page": 1,
                },
            ),
        ),
        "aktiviteter",
    )
    return {
        "scopes_verified": list(READ_SCOPES),
        "posts": len(posts),
        "occurrences": len(occurrences),
    }


def _versions_path(root: Path | str | None) -> Path:
    return credential_path(root, VERSIONS_FILE)


def _read_versions(root: Path | str | None) -> dict[str, Any]:
    path = _versions_path(root)
    if not path.exists() and not path.is_symlink():
        return {}
    try:
        value = read_private_json(path)
    except PrivateStateError:
        return {}
    if not isinstance(value, Mapping) or value.get("version") != VERSIONS_SCHEMA:
        return {}
    entries = value.get("occurrences")
    return dict(entries) if isinstance(entries, Mapping) else {}


def observed_changed_at(
    root: Path | str | None, items: list[dict[str, Any]], *, now: datetime | None = None
) -> None:
    """Set ``changed_at`` of occurrence items to when this version was first seen.

    ``items`` are built without ``changed_at``. The version hash covers
    everything else in the item, so an unchanged occurrence keeps its time.
    """

    from .live_readers import content_hash

    current = (now or datetime.now(UTC)).astimezone(UTC)
    stamp = current.isoformat(timespec="seconds")
    if root is None:
        for item in items:
            item["changed_at"] = stamp
        return
    with private_source_lock(root, SOURCE):
        entries = _read_versions(root)
        changed = False
        for item in items:
            digest = content_hash(
                {key: value for key, value in item.items() if key != "changed_at"}
            )[:32]
            entry = entries.get(item["source_id"])
            if (
                isinstance(entry, Mapping)
                and entry.get("hash") == digest
                and isinstance(entry.get("first_seen"), str)
            ):
                item["changed_at"] = entry["first_seen"]
                if entry.get("last_seen") != stamp[:10]:
                    entries[item["source_id"]] = {**entry, "last_seen": stamp[:10]}
                    changed = True
                continue
            item["changed_at"] = stamp
            entries[item["source_id"]] = {
                "hash": digest,
                "first_seen": stamp,
                "last_seen": stamp[:10],
            }
            changed = True
        if not changed:
            return
        floor = (current - VERSION_RETENTION).date().isoformat()
        kept = {
            key: value
            for key, value in entries.items()
            if isinstance(value, Mapping) and str(value.get("last_seen", "")) >= floor
        }
        if len(kept) > MAX_VERSIONS:
            newest = sorted(
                kept.items(), key=lambda pair: str(pair[1].get("last_seen")), reverse=True
            )
            kept = dict(newest[:MAX_VERSIONS])
        try:
            atomic_write_private_json(
                _versions_path(root), {"version": VERSIONS_SCHEMA, "occurrences": kept}
            )
        except PrivateStateError:
            raise SourceUnavailable("Hoopit-aktivitetsoversikten kunne ikke lagres") from None


def post_item(record: Mapping[str, Any]) -> dict[str, Any]:
    """A post as a source item."""

    post = require_mapping(record.get("post"), "innlegg")
    post_id = identifier(post.get("id"), "innleggs-ID")
    body = _text(post.get("text")) or "Ingen tekst"
    group = post.get("group") if isinstance(post.get("group"), Mapping) else None
    group_name = _text(group.get("name")) if group else None
    sender = _person_name(post.get("author"))
    comments = record.get("comments") or []
    changed_candidates = [_parse_time(post.get("created_at")), _thread_latest_time(comments)]
    incomplete = record.get("comments_unavailable") is True
    present = [candidate for candidate in changed_candidates if candidate is not None]
    changed_at = max(present).isoformat(timespec="seconds") if present else None
    return {
        "source": SOURCE,
        "source_id": f"post:{post_id}",
        "thread_id": f"post:{post_id}",
        "child": None,
        "sender": sender,
        "published_at": _iso(post.get("created_at")),
        "changed_at": changed_at,
        "title": _text(post.get("title")) or group_name or "Hoopit-post",
        "body": _with_comments(body, comments),
        "type": "message",
        "deadline": None,
        "event_start": None,
        "event_end": None,
        "location": None,
        "action_required": None,
        "source_url": f"{APP_WEB_ROOT}/app/posts/{post_id}",
        "topic": group_name or "Hoopit",
        "date_note": None,
        "attachments": [],
        # Only when Hoopit refused the comments.
        **({"content_complete": False} if incomplete else {}),
    }


def post_day(post: Mapping[str, Any]) -> str | None:
    parsed = _parse_time(post.get("created_at"))
    return parsed.astimezone(OSLO).date().isoformat() if parsed else None


def _local_time(value: Any) -> str | None:
    parsed = _parse_time(value)
    return parsed.astimezone(OSLO).strftime("%H:%M") if parsed else None


def occurrence_day(value: Mapping[str, Any]) -> str | None:
    parsed = _parse_time(value.get("start"))
    return parsed.astimezone(OSLO).date().isoformat() if parsed else None


def response_rows(occurrence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The family's answers: person ID, name and status, in Hoopit's order."""

    raw = occurrence.get("responses")
    if not isinstance(raw, list):
        raise SourceUnavailable("Hoopit returnerte en ugyldig svarliste")
    result = []
    seen = set()
    for row in raw:
        if not isinstance(row, Mapping) or not isinstance(row.get("user"), Mapping):
            raise SourceUnavailable("Hoopit returnerte en ugyldig svarliste")
        person = identifier(row["user"].get("id"), "person-ID")
        if person in seen:
            raise SourceUnavailable("Hoopit returnerte samme person to ganger")
        seen.add(person)
        response = row.get("response")
        result.append(
            {
                "person_id": person,
                "name": _person_name(row["user"]) or "Ukjent person",
                "response": response if response in RESPONSE_STATES else None,
            }
        )
    return result


def event_item(occurrence: Mapping[str, Any]) -> dict[str, Any]:
    """An occurrence as a source item, without ``changed_at`` (see above)."""

    value = require_mapping(occurrence, "aktivitet")
    event_id = identifier(value.get("event_id"), "aktivitets-ID")
    occurrence_id = identifier(value.get("id"), "forekomst-ID")
    title = _text(value.get("title")) or "Hoopit-aktivitet"
    lines = [EVENT_TYPES.get(str(value.get("event_type")), "Aktivitet")]
    meetup = _local_time(value.get("meetup_time"))
    if meetup and _parse_time(value.get("meetup_time")) != _parse_time(value.get("start")):
        lines.append(f"Oppmøte: {meetup}")
    if value.get("cancelled") is True:
        lines.append("Avlyst")
    answers = response_rows(value)
    if answers:
        lines.append("Svar:")
        lines.extend(
            f"- {row['name']}: {RESPONSE_LABELS.get(row['response'] or '', 'ukjent svar')}"
            for row in answers
        )
    unanswered = any(row["response"] == "not_answered" for row in answers)
    group = value.get("group") if isinstance(value.get("group"), Mapping) else None
    group_name = _text(group.get("name")) if group else None
    item = {
        "source": SOURCE,
        "source_id": f"occurrence:{event_id}:{occurrence_id}",
        "thread_id": f"occurrence:{event_id}:{occurrence_id}",
        "child": None,
        "sender": None,
        "published_at": None,
        "title": title,
        "body": "\n".join(lines),
        "type": "event",
        "deadline": None,
        "event_start": _iso(value.get("start")),
        "event_end": _iso(value.get("end")),
        "location": _text(value.get("location")),
        "action_required": "Svar på aktivitetsinvitasjonen"
        if unanswered and value.get("cancelled") is not True
        else None,
        "source_url": f"{APP_WEB_ROOT}/app/events/{event_id}/occurrences/{occurrence_id}",
        "topic": group_name or "Hoopit aktivitet",
        "date_note": None,
        "attachments": [],
    }
    return item


__all__ = [
    "BudgetedRateLimiter",
    "Client",
    "ENDPOINTS",
    "HoopitCooldownActive",
    "HoopitRateLimited",
    "MAX_REQUESTS",
    "MIN_REQUEST_INTERVAL_SECONDS",
    "PersistentRateLimiter",
    "RequestBudget",
    "RequestBudgetExceeded",
    "default_transport",
    "event_item",
    "fetch_snapshot",
    "find_occurrence",
    "observed_changed_at",
    "post_comments",
    "post_item",
    "response_rows",
    "token_for_use",
    "verify_token",
    "version_warning_text",
]
