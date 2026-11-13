"""Bounded, read-only collection from Hoopit's first-party app API.

The adapter only issues allowlisted GET requests observed in the official web
application.  It never calls reaction, view, notification-access, attendance,
or other mutation routes, and it deliberately has no Stream Chat integration.
The imported Firebase ID token is sent only to the exact Hoopit API origin;
attachment downloads are unauthenticated and constrained to configured hosts.

The traffic policy is intentionally conservative: requests are serial,
at least two seconds apart, and bounded by page/request/download budgets.  A
429 is never retried during the same run.  Retry-After is persisted and the run
stops immediately.  Network failures and 5xx responses likewise persist a
cooldown and are not retried in-process.
"""

from __future__ import annotations

import ipaddress
import json
import math
import random
import re
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .api_transport import ApiHTTPError, ApiTransport, ApiTransportError, DownloadResult
from .hoopit_auth import _require_direct_access_proof as _require_direct_access_proof
from .core import project_paths
from .hoopit_auth import (
    API_ROOT,
    HoopitAccess,
    HoopitReadAccessUnverified,
    HoopitReadAccessDenied,
    HoopitRemoteUnavailable,
    RefreshTransport,
    TokenVerificationTransport,
    _invalidate_direct_access_proof,
    rate_state_path,
    _record_direct_access_proof,
    _record_direct_access_rejection,
    _direct_access_rejected,
    refresh_session,
    session_for_use,
)
from .private_lock import private_source_lock
from .private_state import (
    PrivateStateError,
    atomic_write_private_json,
    read_private_json,
)
from .source_adapter import SourceBudgetExceeded, SourceLoginRequired, SourceUnavailable


SOURCE = "hoopit"
ADAPTER_GENERATION = "hoopit-app-api-v1"
APP_WEB_ROOT = "https://app.hoopit.io"

# The official client sends pagination_count as a boolean, not a page size.
# The live post feed returns 10 rows per page. Keep a separate, bounded page
# allowance; the shared 80-request budget still covers auth, threads and files.
MAX_PAGES_PER_FEED = 20
MAX_COMMENT_PAGES = 5
MAX_REQUESTS = 80
MAX_POSTS = 250
MAX_EVENTS = 250
# One collection found more posts or events than MAX_POSTS/MAX_EVENTS allow.
TOO_MANY_ITEMS = "Hoopit returnerte flere elementer enn innhentingsgrensen"
MAX_NOTIFICATIONS = 500
MAX_COMMENTS_PER_THREAD = 250
MAX_JSON_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MIN_REQUEST_INTERVAL_SECONDS = 2.0
NETWORK_COOLDOWN_SECONDS = 5 * 60
SERVER_COOLDOWN_BASE_SECONDS = 15 * 60
SERVER_COOLDOWN_MAX_SECONDS = 6 * 60 * 60
RATE_STATE_VERSION = 1

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_PAGE_PATHS = {
    "/app/users/current/notifications/": 10,
    "/app/users/current/posts/": 5,
    "/app/users/current/events/": 2,
}
_PATH_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"/app/messaging/contacts/groups/\Z"), 1),
    (
        re.compile(
            r"/app/messaging/contacts/groups/[1-9][0-9]{0,19}/(?:players|guardians|admins)/\Z"
        ),
        1,
    ),
    (re.compile(r"/app/users/current/\Z"), 6),
    (re.compile(r"/app/users/current/tokens/\Z"), 1),
    (re.compile(r"/app/users/current/children/\Z"), 1),
    (re.compile(r"/app/events/[A-Za-z0-9_-]{1,128}/occurrences/[A-Za-z0-9_-]{1,128}/my/responses/\Z"), 1),
    (re.compile(r"/app/posts/[A-Za-z0-9_-]{1,128}/\Z"), 6),
    (re.compile(r"/app/posts/[A-Za-z0-9_-]{1,128}/comments/\Z"), 2),
    (
        re.compile(
            r"/app/posts/[A-Za-z0-9_-]{1,128}/comments/[A-Za-z0-9_-]{1,128}/replies/\Z"
        ),
        1,
    ),
    (
        re.compile(
            r"/app/events/[A-Za-z0-9_-]{1,128}/occurrences/[A-Za-z0-9_-]{1,128}/\Z"
        ),
        12,
    ),
    (
        re.compile(
            r"/app/events/[A-Za-z0-9_-]{1,128}/occurrences/[A-Za-z0-9_-]{1,128}/posts/\Z"
        ),
        3,
    ),
    (
        re.compile(
            r"/app/events/[A-Za-z0-9_-]{1,128}/occurrences/[A-Za-z0-9_-]{1,128}/comments/\Z"
        ),
        1,
    ),
    (
        re.compile(
            r"/app/events/[A-Za-z0-9_-]{1,128}/occurrences/[A-Za-z0-9_-]{1,128}/comments/[A-Za-z0-9_-]{1,128}/replies/\Z"
        ),
        1,
    ),
)


class HoopitTransport(Protocol):
    def get_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any: ...

    def download(
        self,
        url: str,
        target: Path | str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        allowed_content_types: set[str] | frozenset[str] | None = None,
    ) -> DownloadResult: ...


class HoopitCooldownActive(SourceUnavailable):
    """A previous remote throttle/failure forbids traffic in this run."""

    def __init__(self, cooldown_until: float, reason: str) -> None:
        self.cooldown_until = cooldown_until
        self.reason = reason
        super().__init__("Hoopit er i lagret cooldown etter forrige API-forsøk")


class HoopitRateLimited(HoopitCooldownActive):
    """The current request received 429 and must end without retry."""


class RequestBudgetExceeded(SourceBudgetExceeded):
    """A hard request/page/item budget was reached."""


class HoopitRunStopped(HoopitRemoteUnavailable):
    """A global cooldown/network/server condition forbids more requests."""


class HoopitItemUnavailable(SourceUnavailable):
    """One optional resource failed without imposing a global cooldown."""


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
        payload["recorded_at"] = datetime.fromtimestamp(
            _clock_value(self.clock), UTC
        ).isoformat(timespec="seconds")
        try:
            atomic_write_private_json(self.path, payload)
        except PrivateStateError:
            raise SourceUnavailable("Hoopit-frekvenstilstanden kunne ikke lagres") from None

    def before_request(self) -> None:
        """Wait only for serial pacing; active cooldown raises before traffic."""

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
                        raise SourceUnavailable("Hoopit-pacing kunne ikke vente trygt") from None
                    now = _clock_value(self.clock)
                    if now + 0.001 < float(last) + self.minimum_interval:
                        raise SourceUnavailable("Hoopit-pacing-klokken gikk ikke fremover")
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
            # Never clear a still-active 429 cooldown as a side effect of a
            # preceding successful request finishing out of order.
            now = _clock_value(self.clock)
            cooldown = state.get("cooldown_until")
            if not isinstance(cooldown, (int, float)) or float(cooldown) <= now:
                state["cooldown_until"] = None
                state["reason"] = None
                state["retry_after_seconds"] = None
            self._write(state)

    def record_http_failure(
        self, status_code: int, retry_after: int | None = None
    ) -> float | None:
        """Persist a stop condition.  The return value is never a retry signal."""

        with private_source_lock(self.root, SOURCE):
            state = self._read()
            now = _clock_value(self.clock)
            if status_code == 429:
                if (
                    isinstance(retry_after, bool)
                    or retry_after is not None
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
                return None
            self._write(state)
        return None

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
    """Count auth and data traffic against one total run budget."""

    def __init__(self, limiter: PersistentRateLimiter, budget: RequestBudget) -> None:
        self.limiter = limiter
        self.budget = budget

    def before_request(self) -> None:
        self.budget.consume()
        self.limiter.before_request()

    def record_success(self) -> None:
        self.limiter.record_success()

    def record_http_failure(
        self, status_code: int, retry_after: int | None = None
    ) -> float | None:
        return self.limiter.record_http_failure(status_code, retry_after)

    def record_network_failure(self) -> None:
        self.limiter.record_network_failure()


def _identifier(value: Any, label: str) -> str:
    text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if not _SAFE_ID.fullmatch(text):
        raise SourceUnavailable(f"Hoopit returnerte ugyldig {label}")
    return text


def _api_url(path: str, *, page: int | None = None) -> tuple[str, int]:
    version = _PAGE_PATHS.get(path)
    if version is None:
        for pattern, candidate in _PATH_PATTERNS:
            if pattern.fullmatch(path):
                version = candidate
                break
    if version is None:
        raise SourceUnavailable("Hoopit-adapteren nektet et ukjent endepunkt")
    query = ""
    if page is not None:
        if not 1 <= page <= MAX_PAGES_PER_FEED:
            raise SourceUnavailable("Hoopit-adapteren nektet et ugyldig sidetall")
        query = "?" + urllib.parse.urlencode(
            {
                "page": page,
                "pagination_count": (
                    "true"
                    if path.startswith("/app/messaging/contacts/groups/")
                    else "false"
                ),
            }
        )
    return f"{API_ROOT}{path}{query}", version


def default_transport(access: HoopitAccess) -> ApiTransport:
    return ApiTransport(
        allowed_hosts={"api.hoopit.io", *access.attachment_hosts},
        timeout=45,
        max_json_bytes=MAX_JSON_BYTES,
        max_download_bytes=MAX_ATTACHMENT_BYTES,
        user_agent="familiekilder-hoopit/0.2",
    )


class AuthorizedClient:
    def __init__(
        self,
        access: HoopitAccess,
        transport: HoopitTransport,
        limiter: Any,
    ) -> None:
        self.access = access
        self.transport = transport
        self.limiter = limiter

    def _headers(self, version: int) -> dict[str, str]:
        return {
            "Authorization": f"JWT {self.access.id_token}",
            "App-Version": self.access.app_version,
            "Accept": f"application/problem+json; version={version};",
            "Accept-Language": "nb-NO",
            "Cache-Control": "no-cache",
        }

    def _begin(self) -> None:
        self.limiter.before_request()

    def _failure(self, exc: Exception, *, authenticated_origin: bool) -> None:
        if isinstance(exc, ApiHTTPError):
            self.limiter.record_http_failure(
                exc.status_code, exc.retry_after_seconds
            )
            if authenticated_origin and exc.status_code in {401, 403}:
                raise SourceLoginRequired("Hoopit krever en ny Firebase-økt") from None
            if exc.status_code == 429:
                until = (time.time() + exc.retry_after_seconds if exc.retry_after_seconds is not None else 0)
                raise HoopitRateLimited(until, "http-429") from None
            if 500 <= exc.status_code <= 599:
                raise HoopitRunStopped(
                    "Hoopit API hadde en serverfeil; kjøringen ble stoppet uten retry",
                    reason='backend_error', retry_after_seconds=exc.retry_after_seconds,
                ) from None
            raise HoopitItemUnavailable(
                "Hoopit API avviste ett lesende element"
            ) from None
        self.limiter.record_network_failure()
        raise HoopitRunStopped(
            "Hoopit API kunne ikke nås; kjøringen ble stoppet uten retry", reason='transient',
        ) from None

    def get(self, path: str, *, page: int | None = None) -> Any:
        url, version = _api_url(path, page=page)
        if urllib.parse.urlsplit(url).path.rstrip('/') == '/app/users/current/tokens':
            return self._get(path, page=page)
        from .sync_snapshot import memo_read

        return memo_read(
            getattr(self, 'root', None), SOURCE,
            ('GET', url, version, self.access.app_version),
            lambda: self._get(path, page=page),
        )

    def _get(self, path: str, *, page: int | None = None) -> Any:
        url, version = _api_url(path, page=page)
        self._begin()
        try:
            result = self.transport.get_json(
                url, bearer_token=None, headers=self._headers(version)
            )
        except (ApiHTTPError, ApiTransportError, OSError, TimeoutError) as exc:
            self._failure(exc, authenticated_origin=True)
            raise AssertionError("unreachable")
        self.limiter.record_success()
        return result

    @staticmethod
    def _attachment_url(url: Any, allowed_hosts: tuple[str, ...]) -> str:
        if not isinstance(url, str) or not url or len(url) > 8192:
            raise HoopitItemUnavailable("Hoopit returnerte en ugyldig vedleggs-URL")
        try:
            parsed = urllib.parse.urlsplit(url)
            hostname = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
            decoded_path = urllib.parse.unquote(parsed.path)
        except (UnicodeError, ValueError):
            raise HoopitItemUnavailable("Hoopit returnerte en ugyldig vedleggs-URL") from None
        if (
            parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or hostname not in set(allowed_hosts)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(segment in {".", ".."} for segment in decoded_path.split("/"))
        ):
            raise HoopitItemUnavailable("Hoopit-adapteren nektet en ukjent vedleggsvert")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise HoopitItemUnavailable("Hoopit-adapteren nektet en privat vedleggsadresse")
        return url

    def download(self, url: Any, target: Path, *, max_bytes: int) -> DownloadResult:
        target_url = self._attachment_url(url, self.access.attachment_hosts)
        self._begin()
        try:
            # Authentication is deliberately absent here.  The
            # transport host allowlist is the second line of SSRF protection.
            result = self.transport.download(
                target_url,
                target,
                bearer_token=None,
                headers=None,
                max_bytes=max_bytes,
            )
        except (ApiHTTPError, ApiTransportError, OSError, TimeoutError) as exc:
            self._failure(exc, authenticated_origin=False)
            raise AssertionError("unreachable")
        if not isinstance(result, DownloadResult):
            raise SourceUnavailable("Hoopit-vedlegget returnerte ugyldig metadata")
        self.limiter.record_success()
        return result


def require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceUnavailable(f"Hoopit returnerte ugyldig {label}")
    return value


def _page(value: Any, label: str) -> tuple[list[Mapping[str, Any]], bool]:
    mapping = require_mapping(value, label)
    results = mapping.get("results")
    has_next = mapping.get("has_next")
    if not isinstance(results, list) or not isinstance(has_next, bool):
        raise SourceUnavailable(f"Hoopit returnerte ugyldig {label}")
    if any(not isinstance(item, Mapping) for item in results):
        raise SourceUnavailable(f"Hoopit returnerte ugyldig {label}")
    return [dict(item) for item in results], has_next


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


def latest_time(value: Mapping[str, Any], fields: tuple[str, ...]) -> datetime | None:
    parsed = [_parse_time(value.get(field)) for field in fields]
    present = [candidate for candidate in parsed if candidate is not None]
    return max(present) if present else None


def iter_feed(
    client: AuthorizedClient,
    path: str,
    *,
    label: str,
    maximum_pages: int,
) -> Iterator[Mapping[str, Any]]:
    seen_pages: set[tuple[str, ...]] = set()
    for page_number in range(1, maximum_pages + 1):
        values, has_next = _page(client.get(path, page=page_number), label)
        fingerprint = tuple(_identifier(value.get("id"), label) for value in values)
        if values and fingerprint in seen_pages:
            raise SourceUnavailable("Hoopit gjentok en side; innhentingen er ufullstendig")
        seen_pages.add(fingerprint)
        yield from values
        if not has_next:
            return
        if not values:
            raise SourceUnavailable("Hoopit returnerte en tom side med flere sider igjen")
        # The public app contract does not document ordering by the maximum of
        # created/updated timestamps.  Do not infer that one old page proves
        # all later pages are old; continue until has_next=false or the cap.
    raise RequestBudgetExceeded(f"Hoopit returnerte for mange sider med {label}")


def _fetch_pages(
    client: AuthorizedClient,
    path: str,
    *,
    label: str,
    maximum_pages: int,
) -> list[Mapping[str, Any]]:
    # A thread is not complete until all its comment/reply pages are read.
    return list(iter_feed(client, path, label=label, maximum_pages=maximum_pages))


def _notification_refs(notification: Mapping[str, Any]) -> tuple[str | None, tuple[str, str] | None]:
    data = notification.get("data")
    if not isinstance(data, Mapping):
        return None, None
    raw_post = _pick(data, "EXTRA_POST_ID", "post_id")
    raw_event = _pick(data, "EXTRA_EVENT_ID", "event_id")
    raw_occurrence = _pick(data, "EXTRA_OCCURRENCE_ID", "occurrence_id")
    post_id = None
    if raw_post not in (None, ""):
        try:
            post_id = _identifier(raw_post, "post-ID")
        except SourceUnavailable:
            post_id = None
    event_ref = None
    if raw_event not in (None, "") and raw_occurrence not in (None, ""):
        try:
            event_ref = (
                _identifier(raw_event, "event-ID"),
                _identifier(raw_occurrence, "forekomst-ID"),
            )
        except SourceUnavailable:
            event_ref = None
    return post_id, event_ref


def _comment_pages(client: AuthorizedClient, path: str, label: str) -> list[Mapping[str, Any]]:
    comments = _fetch_pages(
        client,
        path,
        label=label,
        maximum_pages=MAX_COMMENT_PAGES,
    )
    if len(comments) > MAX_COMMENTS_PER_THREAD:
        raise RequestBudgetExceeded("Hoopit returnerte for mange kommentarer")
    return comments


def _with_replies(
    client: AuthorizedClient,
    comments: list[Mapping[str, Any]],
    reply_path: Callable[[str], str],
) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for comment in comments:
        enriched = dict(comment)
        reply_count = comment.get("reply_count")
        if (
            not isinstance(reply_count, int)
            or isinstance(reply_count, bool)
            or reply_count < 0
        ):
            raise SourceUnavailable(
                "Hoopit-kommentaren mangler et pålitelig antall svar"
            )
        if reply_count > 0:
            comment_id = _identifier(comment.get("id"), "kommentar-ID")
            replies = _comment_pages(
                client, reply_path(comment_id), "kommentarsvar"
            )
            if len(replies) != reply_count:
                raise SourceUnavailable(
                    "Hoopit returnerte ikke alle oppgitte kommentarsvar"
                )
            enriched["_replies"] = replies
        result.append(enriched)
    return result


def post_comments(client: AuthorizedClient, post: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    count = post.get("comment_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise SourceUnavailable(
            "Hoopit-posten mangler et pålitelig antall kommentarer"
        )
    if count == 0:
        return []
    post_id = _identifier(post.get("id"), "post-ID")
    comments = _comment_pages(
        client, f"/app/posts/{post_id}/comments/", "postkommentarer"
    )
    complete = _with_replies(
        client,
        comments,
        lambda comment_id: f"/app/posts/{post_id}/comments/{comment_id}/replies/",
    )
    total = len(complete) + sum(
        len(comment.get("_replies", []))
        for comment in complete
        if isinstance(comment.get("_replies", []), list)
    )
    if total != count:
        raise SourceUnavailable("Hoopit returnerte ikke alle oppgitte kommentarer")
    return complete


def event_comments(
    client: AuthorizedClient, event_id: str, occurrence_id: str
) -> list[Mapping[str, Any]]:
    path = f"/app/events/{event_id}/occurrences/{occurrence_id}/comments/"
    comments = _comment_pages(client, path, "aktivitetskommentarer")
    return _with_replies(
        client,
        comments,
        lambda comment_id: (
            f"/app/events/{event_id}/occurrences/{occurrence_id}/comments/"
            f"{comment_id}/replies/"
        ),
    )


def fetch_snapshot(
    client: AuthorizedClient,
    cutoff_at: str,
    *,
    on_record: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Fetch one bounded read snapshot; every network operation is a GET."""

    cutoff = _parse_time(cutoff_at)
    if cutoff is None:
        raise SourceUnavailable("Hoopit-vinduet har ugyldig starttid")

    notification_feed = iter_feed(
        client,
        "/app/users/current/notifications/",
        label="varsler",
        maximum_pages=MAX_PAGES_PER_FEED,
    )
    selected_notifications: list[Mapping[str, Any]] = []
    for value in notification_feed:
        timestamp = latest_time(value, ("timestamp", "created_at"))
        if timestamp is None:
            raise SourceUnavailable("Hoopit-varslet mangler en pålitelig tidsangivelse")
        if timestamp >= cutoff:
            if len(selected_notifications) >= MAX_NOTIFICATIONS:
                raise RequestBudgetExceeded("Hoopit returnerte for mange varsler")
            selected_notifications.append(value)
            if on_record is not None:
                on_record("notifications", value)
    notifications = selected_notifications

    post_records: list[dict[str, Any]] = []
    processed_posts: set[str] = set()

    def record_post(post: Mapping[str, Any]) -> None:
        post_id = _identifier(post.get("id"), "post-ID")
        if post_id in processed_posts:
            return
        if len(processed_posts) >= MAX_POSTS:
            raise RequestBudgetExceeded("Hoopit returnerte for mange poster")
        parent_time = latest_time(post, ("updated_at", "created_at"))
        if parent_time is None:
            raise SourceUnavailable("Hoopit-posten mangler en pålitelig tidsangivelse")
        comments = post_comments(client, post)
        comment_time = _thread_latest_time(comments)
        processed_posts.add(post_id)
        if max(t for t in (parent_time, comment_time) if t is not None) < cutoff:
            return
        record = {"post": dict(post), "comments": comments}
        post_records.append(record)
        if on_record is not None:
            on_record("posts", record)

    posts = iter_feed(
        client,
        "/app/users/current/posts/",
        label="poster",
        maximum_pages=MAX_PAGES_PER_FEED,
    )
    post_map: dict[str, dict[str, Any]] = {}
    for value in posts:
        timestamp = latest_time(value, ("updated_at", "created_at"))
        if timestamp is None:
            raise SourceUnavailable("Hoopit-posten mangler en pålitelig tidsangivelse")
        if timestamp < cutoff:
            continue
        post_map[_identifier(value.get("id"), "post-ID")] = dict(value)
        record_post(value)

    event_summaries = _fetch_pages(
        client,
        "/app/users/current/events/",
        label="aktiviteter",
        maximum_pages=MAX_PAGES_PER_FEED,
    )
    event_refs: dict[tuple[str, str], Mapping[str, Any]] = {}
    for summary in event_summaries:
        event_id = _identifier(_pick(summary, "event_id", "event"), "event-ID")
        occurrence_id = _identifier(summary.get("id"), "forekomst-ID")
        end = latest_time(
            summary, ("updated_at", "last_synchronized_at", "end", "datetime")
        )
        if end is None:
            raise SourceUnavailable("Hoopit-aktiviteten mangler en pålitelig tidsangivelse")
        if end < cutoff:
            continue
        event_refs[(event_id, occurrence_id)] = summary

    for notification in notifications:
        post_id, event_ref = _notification_refs(notification)
        if post_id is not None:
            if post_id not in post_map:
                detail = require_mapping(client.get(f"/app/posts/{post_id}/"), "postdetalj")
                if _identifier(detail.get("id"), "post-ID") != post_id:
                    raise SourceUnavailable("Hoopit-postdetaljen samsvarer ikke med varslet")
                timestamp = latest_time(detail, ("updated_at", "created_at"))
                if timestamp is None:
                    raise SourceUnavailable(
                        "Hoopit-postdetaljen mangler en pålitelig tidsangivelse"
                    )
                # The notification authorizes one bounded thread probe.  Its
                # type is not trusted as proof that content changed; a later
                # filter requires a recent parent/comment/reply timestamp.
                post_map[post_id] = dict(detail)
                record_post(detail)
        if event_ref is not None:
            event_refs.setdefault(event_ref, {})

    if len(post_map) > MAX_POSTS or len(event_refs) > MAX_EVENTS:
        raise RequestBudgetExceeded(TOO_MANY_ITEMS)

    event_records: list[dict[str, Any]] = []
    for (event_id, occurrence_id), summary in event_refs.items():
        detail = require_mapping(
            client.get(f"/app/events/{event_id}/occurrences/{occurrence_id}/"),
            "aktivitetsdetalj",
        )
        returned_occurrence = _identifier(detail.get("id"), "forekomst-ID")
        raw_returned_event = _pick(detail, "event", "event_id")
        if isinstance(raw_returned_event, Mapping):
            raw_returned_event = raw_returned_event.get("id")
        returned_event = _identifier(raw_returned_event, "event-ID")
        if (returned_event, returned_occurrence) != (event_id, occurrence_id):
            raise SourceUnavailable("Hoopit-aktivitetsdetaljen samsvarer ikke")
        event_time = latest_time(
            detail, ("updated_at", "last_synchronized_at", "end", "start")
        )
        if event_time is None:
            raise SourceUnavailable(
                "Hoopit-aktivitetsdetaljen mangler en pålitelig tidsangivelse"
            )
        comments = event_comments(client, event_id, occurrence_id)
        comment_time = _thread_latest_time(comments)
        event_posts = _fetch_pages(
            client,
            f"/app/events/{event_id}/occurrences/{occurrence_id}/posts/",
            label="aktivitetsposter",
            maximum_pages=MAX_COMMENT_PAGES,
        )
        for raw_post in event_posts:
            post_id = _identifier(raw_post.get("id"), "post-ID")
            timestamp = latest_time(raw_post, ("updated_at", "created_at"))
            if timestamp is None:
                raise SourceUnavailable(
                    "Hoopit-aktivitetsposten mangler en pålitelig tidsangivelse"
                )
            if timestamp >= cutoff:
                post_map.setdefault(post_id, dict(raw_post))
                record_post(post_map[post_id])
        if max(
            candidate
            for candidate in (event_time, comment_time)
            if candidate is not None
        ) < cutoff:
            # A reaction or other recent notification must not turn an old,
            # unchanged activity into a new version.  The notification itself
            # remains a first-class item.
            continue
        stored_detail = dict(detail)
        event_records.append(
            {
                "summary": dict(summary),
                "detail": stored_detail,
                "comments": comments,
                "post_ids": [
                    _identifier(value.get("id"), "post-ID")
                    for value in event_posts
                    if (latest_time(value, ("updated_at", "created_at")) or datetime.min.replace(tzinfo=UTC)) >= cutoff
                ],
            }
        )
        if on_record is not None:
            on_record("events", event_records[-1])

    if len(post_map) > MAX_POSTS:
        raise RequestBudgetExceeded("Hoopit returnerte for mange poster")
    return {
        "notifications": notifications,
        "posts": post_records,
        "events": event_records,
    }


def verify_direct_access(
    root: Path | str | None,
    *,
    transport: HoopitTransport | None = None,
    auth_transport: RefreshTransport | None = None,
    verification_transport: TokenVerificationTransport | None = None,
    rate_limiter: PersistentRateLimiter | None = None,
    at: float | None = None,
) -> dict[str, bool]:
    """Run the one-refresh/one-read gate without retrying either operation.

    Success proves that this installation can use a freshly refreshed Firebase
    token directly against the read API.  A 401/403 on the read remains
    inconclusive: it can be App Check or another provider-side access rule, so
    the caller fails closed instead of claiming a specific cause.
    """

    root_path = project_paths(root).root
    instant = time.time() if at is None else at
    limiter = rate_limiter or PersistentRateLimiter(root_path)
    _invalidate_direct_access_proof(root_path)
    refresh_session(
        root_path,
        transport=auth_transport,
        verification_transport=verification_transport,
        rate_limiter=limiter,
        at=instant,
    )
    access = session_for_use(root_path, rate_limiter=limiter, at=instant)
    client = AuthorizedClient(
        access,
        transport or default_transport(access),
        limiter,
    )
    try:
        response = client.get("/app/users/current/notifications/", page=1)
    except SourceLoginRequired:
        retry_at = _record_direct_access_rejection(root_path, access, at=instant)
        raise HoopitReadAccessDenied(
            "Hoopit avviste appens lesetilgang med et nyfornyet Firebase-token; "
            "mulig App Check eller annen leverandørtilgang kreves",
            retry_at=retry_at,
        ) from None
    _page(response, "varsler")
    _record_direct_access_proof(root_path, access, at=instant)
    return {
        "firebase_refresh_verified": True,
        "reading_api_verified": True,
        "app_check_required": False,
        "direct_access_proof_recorded": True,
    }


def ensure_direct_access(root):
    """Complete the missing app gate once, preserving its strict read proof."""
    with private_source_lock(root, SOURCE):
        try:
            _require_direct_access_proof(root)
            return
        except HoopitReadAccessUnverified:
            retry_at = _direct_access_rejected(root)
            if retry_at:
                raise HoopitReadAccessDenied(
                    'Hoopit avviste appens lesetilgang. En ny kontroll venter til etter pausen.',
                    retry_at=retry_at,
                ) from None
        verify_direct_access(root)
        # Verification must actually persist the current binding. A return value
        # or a partially successful request never substitutes for the proof.
        _require_direct_access_proof(root)


def _quill_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ""
    if isinstance(value, Mapping):
        value = value.get("ops")
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for operation in value:
        if isinstance(operation, Mapping) and isinstance(operation.get("insert"), str):
            parts.append(str(operation["insert"]))
    return "".join(parts).strip()


def _person_name(value: Any) -> str | None:
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, Mapping):
        return None
    direct = _pick(value, "name", "full_name", "display_name")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts = [
        _text(value.get("first_name")),
        _text(value.get("last_name")),
    ]
    return " ".join(part for part in parts if part) or None


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
        replies = raw.get("_replies")
        lines.extend(_comment_lines(replies, indent="  ↳ "))
    return lines


def _with_comments(body: str, comments: Any) -> str:
    lines = _comment_lines(comments)
    if not lines:
        return body or "Ingen tekst"
    return (body.strip() + "\n\nKommentarer:\n" + "\n".join(lines)).strip()


def _thread_latest_time(comments: Any) -> datetime | None:
    """Return the newest real comment/reply time, rejecting unknown shapes."""

    if not isinstance(comments, list):
        raise SourceUnavailable("Hoopit returnerte ugyldig kommentarliste")
    latest: datetime | None = None
    pending = list(comments)
    while pending:
        raw = pending.pop()
        if not isinstance(raw, Mapping):
            raise SourceUnavailable("Hoopit returnerte en ugyldig kommentar")
        timestamp = latest_time(raw, ("timestamp", "updated_at", "created_at"))
        if timestamp is None:
            raise SourceUnavailable(
                "Hoopit-kommentaren mangler en pålitelig tidsangivelse"
            )
        latest = timestamp if latest is None or timestamp > latest else latest
        replies = raw.get("_replies")
        if replies is not None:
            if not isinstance(replies, list):
                raise SourceUnavailable("Hoopit returnerte ugyldige kommentarsvar")
            pending.extend(replies)
    return latest


def _explicit_child(value: Mapping[str, Any]) -> str | None:
    candidates = [value.get("child"), value.get("managed_user")]
    related = value.get("related_users")
    if isinstance(related, list) and len(related) == 1:
        candidates.append(related[0])
    names = [_person_name(candidate) for candidate in candidates]
    present = [name for name in names if name]
    return present[0] if len(set(present)) == 1 else None


def _location(value: Any) -> str | None:
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, Mapping):
        return None
    return _text(_pick(value, "name", "address", "title"))


def attachment_rows(value: Any) -> list[Mapping[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise SourceUnavailable("Hoopit returnerte ugyldig vedleggsliste")
    return [item for item in value if isinstance(item, Mapping)]


def post_item(
    record: Mapping[str, Any],
    *,
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    post = require_mapping(record.get("post"), "post")
    post_id = _identifier(post.get("id"), "post-ID")
    body = _text(post.get("text")) or _quill_text(post.get("quill_delta")) or "Ingen tekst"
    group_name = _text(post.get("group_name"))
    event_name = _text(post.get("event_name"))
    sender = _text(post.get("poster_name")) or _person_name(post.get("poster"))
    changed_candidates = [
        _parse_time(_pick(post, "updated_at", "created_at")),
        _thread_latest_time(record.get("comments")),
    ]
    changed_present = [candidate for candidate in changed_candidates if candidate is not None]
    changed_at = max(changed_present).isoformat(timespec="seconds") if changed_present else None
    return {
        "source": SOURCE,
        "source_id": f"post:{post_id}",
        "thread_id": f"post:{post_id}",
        "child": _explicit_child(post),
        "sender": sender,
        "published_at": _iso(post.get("created_at")),
        "changed_at": changed_at,
        "title": _text(post.get("title")) or event_name or group_name or "Hoopit-post",
        "body": _with_comments(body, record.get("comments")),
        "type": "message",
        "deadline": None,
        "event_start": None,
        "event_end": None,
        "location": None,
        "action_required": None,
        "source_url": f"{APP_WEB_ROOT}/app/posts/{post_id}",
        "topic": event_name or group_name or "Hoopit",
        "date_note": None,
        "attachments": attachments,
    }


def event_item(
    record: Mapping[str, Any],
    *,
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = require_mapping(record.get("summary", {}), "aktivitetssammendrag")
    detail = require_mapping(record.get("detail"), "aktivitetsdetalj")
    event_raw = _pick(detail, "event", "event_id") or _pick(summary, "event_id", "event")
    if isinstance(event_raw, Mapping):
        event_raw = event_raw.get("id")
    event_id = _identifier(event_raw, "event-ID")
    occurrence_id = _identifier(detail.get("id"), "forekomst-ID")
    title = _text(detail.get("title")) or _text(summary.get("title")) or "Hoopit-aktivitet"
    description = _text(detail.get("description")) or "Ingen beskrivelse"
    cancelled = bool(detail.get("cancelled"))
    cancelled_reason = _text(detail.get("cancelled_reason"))
    if cancelled:
        description += "\n\nAvlyst" + (f": {cancelled_reason}" if cancelled_reason else "")
    response_deadline = _iso(detail.get("response_deadline"))
    status = str(_pick(summary, "multi_answer_status", "answer_status") or "").upper()
    action = "Svar på aktivitetsinvitasjonen" if "NOT" in status and "RESPOND" in status else None
    group = detail.get("group") if isinstance(detail.get("group"), Mapping) else summary.get("group")
    group_name = _person_name(group) or (
        _text(group.get("name")) if isinstance(group, Mapping) else _text(group)
    )
    changed_candidates = [
        _parse_time(_pick(detail, "updated_at", "last_synchronized_at")),
        _thread_latest_time(record.get("comments")),
    ]
    changed_present = [candidate for candidate in changed_candidates if candidate is not None]
    changed_at = max(changed_present).isoformat(timespec="seconds") if changed_present else None
    return {
        "source": SOURCE,
        "source_id": f"occurrence:{event_id}:{occurrence_id}",
        "thread_id": f"occurrence:{event_id}:{occurrence_id}",
        "child": _explicit_child(detail) or _explicit_child(summary),
        "sender": _person_name(_pick(detail, "organizer", "created_by")),
        "published_at": _iso(_pick(detail, "created_at", "start")),
        "changed_at": changed_at,
        "title": title,
        "body": _with_comments(description, record.get("comments")),
        "type": "event",
        "deadline": response_deadline[:10] if response_deadline else None,
        "event_start": _iso(_pick(detail, "start", "datetime")) or _iso(summary.get("datetime")),
        "event_end": _iso(detail.get("end")) or _iso(summary.get("end")),
        "location": _location(detail.get("location")) or _location(summary.get("location")),
        "action_required": action,
        "source_url": f"{APP_WEB_ROOT}/app/events/{event_id}/occurrences/{occurrence_id}",
        "topic": group_name or "Hoopit aktivitet",
        "date_note": f"Svarfrist: {response_deadline}" if response_deadline else None,
        "attachments": attachments,
    }


def notification_item(notification: Mapping[str, Any]) -> dict[str, Any]:
    notification_id = _identifier(notification.get("id"), "varsel-ID")
    data = notification.get("data") if isinstance(notification.get("data"), Mapping) else {}
    title = _text(data.get("title")) or "Hoopit-varsel"
    body = _text(data.get("body")) or title
    post_id, event_ref = _notification_refs(notification)
    if post_id:
        source_url = f"{APP_WEB_ROOT}/app/posts/{post_id}"
    elif event_ref:
        source_url = f"{APP_WEB_ROOT}/app/events/{event_ref[0]}/occurrences/{event_ref[1]}"
    else:
        source_url = None
    action = _text(data.get("action"))
    return {
        "source": SOURCE,
        "source_id": f"notification:{notification_id}",
        "thread_id": f"notification:{notification_id}",
        "child": _explicit_child(data),
        "sender": "Hoopit",
        "published_at": _iso(_pick(notification, "timestamp", "created_at")),
        "changed_at": None,
        "title": title,
        "body": body,
        "type": "message",
        "deadline": None,
        "event_start": None,
        "event_end": None,
        "location": None,
        "action_required": action,
        "source_url": source_url,
        "topic": _text(_pick(data, "EXTRA_GROUP_NAME", "group_name")) or "Hoopit-varsel",
        "date_note": None,
        "attachments": [],
    }


__all__ = [
    "ADAPTER_GENERATION",
    "MAX_REQUESTS",
    "MIN_REQUEST_INTERVAL_SECONDS",
    "PersistentRateLimiter",
    "RequestBudget",
    "RequestBudgetExceeded",
    "HoopitCooldownActive",
    "HoopitRateLimited",
    "fetch_snapshot",
    "verify_direct_access",
    "AuthorizedClient",
    "BudgetedRateLimiter",
    "attachment_rows",
    "default_transport",
    "event_comments",
    "event_item",
    "iter_feed",
    "latest_time",
    "notification_item",
    "post_comments",
    "post_item",
    "require_mapping",
]
