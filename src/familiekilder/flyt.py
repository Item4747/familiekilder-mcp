"""Bounded, read-only Flyt Foresatt collection through the app's guardian API.

Only barnehage (``vfk/``) content is read so far: announcements, diary posts,
the child's message thread, calendar events, absences and check-in/check-out.
School (``vfs/``) routes can be added as more entries in ``READ_ROUTES`` and
more fetchers.

Every request passes ``guard_route``: an exact method and path pattern per read.
Many reads are ``POST .../search``, so a verb is never enough. Paths that mark
content as seen, send messages, register absence or answer consents match no
pattern and are refused before any network access.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .api_transport import ApiHTTPError, ApiTransport, ApiTransportError, DownloadResult
from .core import FamiliekilderError, html_to_text, parse_iso
from .flyt_auth import (
    API_BASE_URL,
    API_HOST,
    AuthTransport,
    guardian_headers,
    load_client_config,
    local_session_for_use,
    refresh_session,
)
from .source_adapter import SourceLoginRequired, SourceUnavailable


SOURCE = "flyt"
PAGE_SIZE = 25
# The app pages check-in/check-out with 50 rows.
ATTENDANCE_PAGE_SIZE = 50
MAX_PAGES = 20
MAX_ITEMS_PER_STREAM = 500
MAX_CHILDREN = 20
CALENDAR_DAYS_AHEAD = 60
MAX_CALENDAR_SPAN_DAYS = 400
MAX_CALENDAR_DAYS = 62
MAX_EVENT_DETAILS = 200
MAX_DIARY_DETAILS = 200
COUNTS_CHUNK_DAYS = 31
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_RUN_DOWNLOAD_BYTES = 100 * 1024 * 1024
# Diary photos are signed CloudFront links. The app fetches them without its
# bearer token; only the guardian API host gets the token.
MEDIA_HOST = "scdn.barnehage.visma.no"
LOCAL_ZONE = ZoneInfo("Europe/Oslo")

_ID = r"[A-Za-z0-9._~-]{1,256}"
# (method, path pattern, allowed query keys). Nothing else reaches the network.
READ_ROUTES: tuple[tuple[str, re.Pattern[str], frozenset[str]], ...] = tuple(
    (method, re.compile(pattern), frozenset(query))
    for method, pattern, query in (
        ("GET", r"v1/children", ()),
        ("POST", rf"vfk/v1/children/{_ID}/announcements/search", ()),
        ("POST", rf"vfk/v1/children/{_ID}/diary-posts/search", ()),
        ("GET", rf"vfk/v1/children/{_ID}/diary-posts/{_ID}", ()),
        ("GET", rf"vfk/v1/children/{_ID}/messages", ("Count", "FromMessageId")),
        ("POST", rf"vfk/v1/children/{_ID}/calendar/events/counts", ()),
        ("POST", rf"vfk/v1/children/{_ID}/calendar/events/search", ()),
        ("POST", rf"vfk/v2/children/{_ID}/calendar/events/{_ID}", ()),
        ("POST", rf"vfk/v1/children/{_ID}/absences/search", ()),
        ("POST", rf"vfk/v1/children/{_ID}/attendances/search", ()),
    )
)
# Never sent, whatever the pattern above would say: mark-as-seen and writes.
_FORBIDDEN_SEGMENTS = frozenset({"seen", "consents", "register-device", "unregister-device"})


class FlytForbidden(SourceUnavailable):
    """Flyt refused this one read; other children and streams may still work."""


def _identifier(value: Any) -> str | None:
    from .live_readers import identifier

    try:
        return identifier(value)
    except FamiliekilderError:
        return None


def guard_route(method: str, path: str, query: Mapping[str, str] | None = None) -> str:
    """Return the path when it is one of the listed reads, else refuse."""

    segments = path.split("/")
    if (
        not isinstance(path, str)
        or path.startswith("/")
        or any(segment in {"", ".", ".."} for segment in segments)
        or _FORBIDDEN_SEGMENTS & set(segments)
    ):
        raise SourceUnavailable("Flyt-klienten nektet et kall som ikke er en lesing")
    for allowed_method, pattern, keys in READ_ROUTES:
        if method == allowed_method and pattern.fullmatch(path):
            if set(query or {}) - keys:
                break
            return path
    raise SourceUnavailable("Flyt-klienten nektet et kall som ikke er en lesing")


def guard_attachment(url: str) -> str:
    """Attachments come from the guardian API host or the signed photo host."""

    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (ValueError, AttributeError):
        raise SourceUnavailable("Vedlegget i Flyt har en ugyldig adresse") from None
    path = urllib.parse.unquote(parsed.path)
    segments = path.split("/")
    if (
        parsed.scheme != "https"
        or host not in (API_HOST, MEDIA_HOST)
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(segment in {".", ".."} for segment in segments)
        or _FORBIDDEN_SEGMENTS & set(segments)
    ):
        raise SourceUnavailable("Vedlegget i Flyt ligger på en adresse som ikke er godkjent")
    return url


def default_transport() -> ApiTransport:
    return ApiTransport(
        allowed_hosts={API_HOST, MEDIA_HOST},
        timeout=45,
        max_json_bytes=10 * 1024 * 1024,
        max_download_bytes=MAX_ATTACHMENT_BYTES,
        user_agent="familiekilder-flyt/0.1",
    )


@dataclass
class DownloadBudget:
    limit: int = MAX_RUN_DOWNLOAD_BYTES
    used: int = 0

    def consume(self, amount: int) -> None:
        if amount < 0 or self.used + amount > self.limit:
            raise SourceUnavailable("Flyt-vedlegg overskred samlet nedlastingsgrense")
        self.used += amount


class Client:
    """Guardian API reads with one refresh on 401 and the app's headers."""

    def __init__(
        self,
        root: Path,
        transport: Any | None = None,
        auth_transport: AuthTransport | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = root
        self.transport = transport or default_transport()
        self.auth_transport = auth_transport
        self.clock = clock
        self.customer_key = load_client_config(root).customer_key
        self.session = local_session_for_use(root)
        self._refreshed = False
        expires_at = self.session.get("access_expires_at")
        if isinstance(expires_at, float) and expires_at <= clock() + 300:
            self._refresh()

    def _refresh(self) -> None:
        self._refreshed = True
        refresh_session(self.root, transport=self.auth_transport)
        self.session = local_session_for_use(self.root)

    def _headers(self, *, photo: bool = False) -> dict[str, str]:
        # The app's photo interceptor sends no Session-Id.
        return guardian_headers(
            self.customer_key, None if photo else self.session.get("session_id")
        )

    def _authorized(self, operation: Callable[[str], Any]) -> Any:
        for attempt in (0, 1):
            try:
                return operation(str(self.session["access_token"]))
            except ApiHTTPError as exc:
                if exc.status_code == 401 and attempt == 0 and not self._refreshed:
                    self._refresh()
                    continue
                if exc.status_code == 401:
                    raise SourceLoginRequired("Flyt krever ny innlogging") from None
                if exc.status_code == 403:
                    raise FlytForbidden("Flyt nektet tilgang til dette innholdet") from None
                if exc.status_code == 429:
                    raise SourceUnavailable("Flyt ber om en pause før neste oppslag") from None
                raise SourceUnavailable(
                    f"Flyt svarte ikke som forventet (HTTP {exc.status_code})"
                ) from None
            except ApiTransportError:
                raise SourceUnavailable(
                    "Flyt kunne ikke nås eller svarte med ugyldige data"
                ) from None
        raise SourceLoginRequired("Flyt krever ny innlogging")

    def read(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        query: Mapping[str, str] | None = None,
    ) -> Any:
        guard_route(method, path, query)
        url = f"{API_BASE_URL}/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(sorted(query.items()))
        if method == "GET":

            def load() -> Any:
                return self._authorized(
                    lambda token: self.transport.get_json(
                        url, bearer_token=token, headers=self._headers()
                    )
                )
        else:
            body = dict(payload or {})

            def load() -> Any:
                return self._authorized(
                    lambda token: self.transport.post_json(
                        url, body, bearer_token=token, headers=self._headers()
                    )
                )

        from .sync_snapshot import memo_read

        return memo_read(self.root, SOURCE, (method, url, payload), load)

    def download(self, url: str, target: Path, budget: DownloadBudget) -> DownloadResult:
        guard_attachment(url)
        limit = min(MAX_ATTACHMENT_BYTES, budget.limit - budget.used)
        if urllib.parse.urlsplit(url).hostname.lower() == MEDIA_HOST:
            # The signature in the link is the authorization; no token leaves
            # for the photo host. An expired link needs a fresh read.
            try:
                result = self.transport.download(url, target, max_bytes=limit)
            except ApiHTTPError as exc:
                raise SourceUnavailable(
                    f"Bildet i Flyt kunne ikke hentes (HTTP {exc.status_code}); "
                    "les innlegget på nytt"
                ) from None
            except ApiTransportError:
                raise SourceUnavailable("Bildet i Flyt kunne ikke hentes") from None
        else:
            result = self._authorized(
                lambda token: self.transport.download(
                    url,
                    target,
                    bearer_token=token,
                    headers=self._headers(photo=True),
                    max_bytes=limit,
                )
            )
        budget.consume(int(getattr(result, "byte_size", 0) or 0))
        return result


# Children -----------------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    """One barnehage placement; its ID is the path parameter for vfk reads."""

    placement_id: str
    child_id: str | None
    name: str | None
    kindergarten: str | None = field(default=None, compare=False)


def _name(person: Any) -> str | None:
    if not isinstance(person, Mapping):
        return None
    parts = [person.get("firstName"), person.get("lastName")]
    return (
        " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip()) or None
    )


def placements(client: Client) -> tuple[list[Placement], bool]:
    rows = client.read("GET", "v1/children")
    if not isinstance(rows, list):
        raise SourceUnavailable("Flyt returnerte en ugyldig barneliste")
    result: dict[str, Placement] = {}
    for child in rows:
        data = child.get("kindergartenData") if isinstance(child, Mapping) else None
        if not isinstance(data, Mapping):
            continue
        # ``placementId`` is the current placement; ``placements`` lists every
        # placement of the child, the current one included.
        extra = data.get("placements")
        for entry in [data, *(extra if isinstance(extra, list) else [])]:
            if not isinstance(entry, Mapping):
                continue
            placement_id = _identifier(entry.get("placementId"))
            if placement_id is None or placement_id in result:
                continue
            kindergarten = entry.get("kindergartenName")
            result[placement_id] = Placement(
                placement_id,
                _identifier(data.get("childId")),
                _name(child),
                kindergarten if isinstance(kindergarten, str) else None,
            )
    ordered = list(result.values())
    return ordered[:MAX_CHILDREN], len(ordered) <= MAX_CHILDREN


# Dates --------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    try:
        return parse_iso(value, "tidspunkt")
    except FamiliekilderError:
        return None


def _instant(value: Any) -> datetime | None:
    normalized = _iso(value)
    return datetime.fromisoformat(normalized) if normalized else None


def _api_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day_start(day: date) -> str:
    return f"{day.isoformat()}T00:00:00Z"


def occurrence_token(start: str) -> str:
    """A URL-safe occurrence ID: the event's start in compact UTC."""

    moment = _instant(start)
    if moment is None:
        raise SourceUnavailable("Kalenderhendelsen i Flyt mangler starttid")
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def occurrence_days(token: str) -> list[date]:
    """The days whose event search can hold the occurrence: local day first.

    An all-day event in Norwegian time starts at 23:00 or 22:00 UTC the day
    before, and the counts may list it under its local day. Searching only
    the UTC day would then miss an item the list found.
    """

    try:
        start = datetime.strptime(token, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        raise FamiliekilderError("Ugyldig forekomst i Flyt-referansen") from None
    local, utc = start.astimezone(LOCAL_ZONE).date(), start.date()
    return [local] if local == utc else [local, utc]


def _older_page(
    page: list[Any], since: datetime | None, stamp: Callable[[Any], datetime | None]
) -> bool:
    """True when a newest-first page ends before ``since``: stop paging."""

    if since is None or not page:
        return False
    stamps = [stamp(row) for row in page]
    if any(value is None for value in stamps):
        return False
    descending = all(left >= right for left, right in zip(stamps, stamps[1:]))
    return descending and stamps[-1] < since


# Paged reads --------------------------------------------------------------


def _search(
    client: Client,
    path: str,
    body: Mapping[str, Any],
    *,
    since: datetime | None = None,
    stamp: Callable[[Any], datetime | None] = lambda row: None,
    page_size: int = PAGE_SIZE,
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    skip = 0
    for _page in range(MAX_PAGES):
        reply = client.read("POST", path, {**body, "paging": {"skip": skip, "take": page_size}})
        if not isinstance(reply, Mapping) or not isinstance(reply.get("resultList"), list):
            raise SourceUnavailable("Flyt returnerte en ugyldig side")
        page = [row for row in reply["resultList"] if isinstance(row, Mapping)]
        rows.extend(page)
        if len(rows) > MAX_ITEMS_PER_STREAM:
            return rows[:MAX_ITEMS_PER_STREAM], False
        total = reply.get("totalCount")
        skip += len(reply["resultList"])
        # The announcement search answers ``totalCount: 0`` next to real rows,
        # so the total only ends paging when it covers what was already read.
        trusted_total = (
            isinstance(total, int) and not isinstance(total, bool) and total >= len(rows)
        )
        if (
            len(reply["resultList"]) < page_size
            or (trusted_total and skip >= total)
            or _older_page(page, since, stamp)
        ):
            return rows, True
    return rows, False


def announcements(client: Client, placement: Placement, since: datetime | None = None):
    return _search(
        client,
        f"vfk/v1/children/{placement.placement_id}/announcements/search",
        {"sort": [{"orderBy": 1, "orderType": 1}]},
        since=since,
        stamp=lambda row: _instant(row.get("lastEditedDate") or row.get("startDate")),
    )


def diary_post(client: Client, placement: Placement, post_id: str) -> dict[str, Any]:
    """One diary post with every photo; the search shows only the first."""

    reply = client.read("GET", f"vfk/v1/children/{placement.placement_id}/diary-posts/{post_id}")
    if not isinstance(reply, Mapping) or reply.get("id") != post_id:
        raise SourceUnavailable("Flyt returnerte et ugyldig dagbokinnlegg")
    return dict(reply)


def diary_posts(client: Client, placement: Placement, since: datetime | None = None):
    rows, complete = _search(
        client,
        f"vfk/v1/children/{placement.placement_id}/diary-posts/search",
        {},
        since=since,
        stamp=lambda row: _instant(row.get("createdDate")),
    )
    details = 0
    result = []
    for row in rows:
        created = _instant(row.get("createdDate"))
        wanted = (
            row.get("attachments")
            and _identifier(row.get("id"))
            and (since is None or created is None or created >= since)
        )
        if wanted and details >= MAX_DIARY_DETAILS:
            row = {**row, "_detail_missing": True}
            complete = False
        elif wanted:
            details += 1
            try:
                # The detail replaces the search row, so the list builds the
                # post from the same reply as `find` and both hash alike.
                row = diary_post(client, placement, row["id"])
            except FlytForbidden:
                row = {**row, "_detail_missing": True}
                complete = False
        result.append(row)
    return result, complete


def attendances(client: Client, placement: Placement, since: datetime | None = None):
    """Check-in and check-out per day, newest first."""

    return _search(
        client,
        f"vfk/v1/children/{placement.placement_id}/attendances/search",
        {},
        since=since,
        stamp=lambda row: _instant(row.get("checkInDateTime")),
        page_size=ATTENDANCE_PAGE_SIZE,
    )


def absences(client: Client, placement: Placement, since: datetime | None = None):
    return _search(
        client,
        f"vfk/v1/children/{placement.placement_id}/absences/search",
        {"includeModifiedBy": True},
    )


def messages(client: Client, placement: Placement, since: datetime | None = None):
    """The child's thread, newest first; older pages via ``FromMessageId``."""

    path = f"vfk/v1/children/{placement.placement_id}/messages"
    rows: dict[str, dict[str, Any]] = {}
    query = {"Count": str(PAGE_SIZE)}
    for _page in range(MAX_PAGES):
        page = client.read("GET", path, query=query)
        if not isinstance(page, list):
            raise SourceUnavailable("Flyt returnerte en ugyldig meldingsliste")
        fresh = [
            row
            for row in page
            if isinstance(row, Mapping) and _identifier(row.get("id")) and row["id"] not in rows
        ]
        for row in fresh:
            rows[row["id"]] = dict(row)
        if len(rows) > MAX_ITEMS_PER_STREAM:
            return list(rows.values())[:MAX_ITEMS_PER_STREAM], False
        if len(page) < PAGE_SIZE:
            return list(rows.values()), True
        if not fresh:
            return list(rows.values()), False
        dated = [(row, _instant(row.get("createdDate"))) for row in fresh]
        if any(moment is None for _row, moment in dated):
            return list(rows.values()), False
        oldest_row, oldest = min(dated, key=lambda pair: pair[1])
        if since is not None and oldest < since:
            return list(rows.values()), True
        query = {"Count": str(PAGE_SIZE), "FromMessageId": oldest_row["id"]}
    return list(rows.values()), False


def calendar_days(
    client: Client, placement: Placement, first: date, last: date
) -> tuple[list[date], bool]:
    """Days with events, from the counts read in month-sized ranges.

    The counts leave out the day of ``to``, so each range ends at midnight
    after its last day.
    """

    path = f"vfk/v1/children/{placement.placement_id}/calendar/events/counts"
    child_ids = [placement.child_id or placement.placement_id]
    days: set[date] = set()
    start = first
    while start <= last:
        end = min(last, start + timedelta(days=COUNTS_CHUNK_DAYS - 1))
        reply = client.read(
            "POST",
            path,
            {
                "childIds": child_ids,
                "dateRange": {
                    "from": _day_start(start),
                    "to": _day_start(end + timedelta(days=1)),
                },
            },
        )
        counts = reply.get("counts") if isinstance(reply, Mapping) else None
        if not isinstance(counts, Mapping):
            raise SourceUnavailable("Flyt returnerte en ugyldig kalenderoversikt")
        for key, value in counts.items():
            try:
                day = date.fromisoformat(str(key)[:10])
            except ValueError:
                continue
            events = value.get("events") if isinstance(value, Mapping) else None
            if (
                isinstance(events, int)
                and not isinstance(events, bool)
                and events > 0
                and first <= day <= last
            ):
                days.add(day)
        start = end + timedelta(days=1)
    ordered = sorted(days)
    return ordered[:MAX_CALENDAR_DAYS], len(ordered) <= MAX_CALENDAR_DAYS


def events_on(client: Client, placement: Placement, day: date) -> list[dict[str, Any]]:
    reply = client.read(
        "POST",
        f"vfk/v1/children/{placement.placement_id}/calendar/events/search",
        {
            "statusDate": _day_start(day),
            "childIds": [placement.child_id or placement.placement_id],
        },
    )
    if not isinstance(reply, list):
        raise SourceUnavailable("Flyt returnerte en ugyldig kalenderliste")
    return [dict(row) for row in reply if isinstance(row, Mapping) and _identifier(row.get("id"))]


def event_detail(client: Client, placement: Placement, event: Mapping[str, Any]) -> dict[str, Any]:
    reply = client.read(
        "POST",
        f"vfk/v2/children/{placement.placement_id}/calendar/events/{event['id']}",
        {
            "occurrenceStartDate": _api_time(_instant(event.get("startDate"))),
            "occurrenceEndDate": _api_time(
                _instant(event.get("endDate")) or _instant(event.get("startDate"))
            ),
        },
    )
    if not isinstance(reply, Mapping) or reply.get("id") != event["id"]:
        raise SourceUnavailable("Flyt returnerte en ugyldig kalenderhendelse")
    return dict(reply)


def calendar_events(client: Client, placement: Placement, first: date, last: date):
    days, complete = calendar_days(client, placement, first, last)
    found: dict[tuple[str, str], dict[str, Any]] = {}
    details = 0
    for day in days:
        for event in events_on(client, placement, day):
            if _instant(event.get("startDate")) is None:
                complete = False
                continue
            key = (event["id"], occurrence_token(event["startDate"]))
            if key in found:
                continue
            if details >= MAX_EVENT_DETAILS:
                complete = False
                continue
            details += 1
            try:
                event = {**event, **event_detail(client, placement, event)}
            except FlytForbidden:
                event = {**event, "_detail_missing": True}
                complete = False
            found[key] = event
    return list(found.values()), complete


# Normalization ------------------------------------------------------------


def _base(kind: str, placement: Placement, raw_id: str, *, topic: str) -> dict[str, Any]:
    source_id = f"{kind}:{placement.placement_id}:{raw_id}"
    return {
        "source": SOURCE,
        "source_id": source_id,
        "thread_id": source_id,
        "child": placement.name,
        "sender": None,
        "topic": topic,
        "published_at": None,
        "changed_at": None,
        "title": "",
        "body": "",
        "type": "announcement",
        "deadline": None,
        "event_start": None,
        "event_end": None,
        "location": None,
        "action_required": None,
        "source_url": None,
        "date_note": None,
        "attachments": [],
    }


def _attachments(rows: Any) -> list[dict[str, Any]]:
    from .live_readers import attachment_metadata

    result = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, Mapping) and isinstance(row.get("url"), str):
            result.append(
                attachment_metadata(
                    {"id": row.get("id"), "fileName": row.get("fileName"), "url": row["url"]}
                )
            )
    return result


def normalize_announcement(row: Mapping[str, Any], placement: Placement) -> dict[str, Any]:
    item = _base("announcement", placement, row["id"], topic="Beskjed")
    item.update(
        published_at=_iso(row.get("startDate")),
        changed_at=_iso(row.get("lastEditedDate")),
        title=row.get("title") if isinstance(row.get("title"), str) and row["title"] else "Beskjed",
        body=html_to_text(row.get("content")) or "",
        attachments=_attachments(row.get("attachments")),
    )
    stop = _iso(row.get("stopDate"))
    if stop:
        item["date_note"] = f"Vises til {stop[:10]}"
    if row.get("hasAttachments") is True and not item["attachments"]:
        item["content_complete"] = False
    return item


def normalize_diary_post(row: Mapping[str, Any], placement: Placement) -> dict[str, Any]:
    from .live_readers import attachment_metadata

    item = _base("diary", placement, row["id"], topic="Dagbok")
    pictures = []
    for number, picture in enumerate(row.get("attachments") or [], 1):
        url = (
            picture.get("previewUrl") or picture.get("thumbnailUrl")
            if isinstance(picture, Mapping)
            else None
        )
        if isinstance(url, str):
            pictures.append(
                attachment_metadata(
                    {
                        "id": picture.get("id") or picture.get("attachmentFileId"),
                        "url": url,
                        "name": f"bilde-{number}.jpg",
                    }
                )
            )
    item.update(
        published_at=_iso(row.get("createdDate")),
        sender=_name(row.get("createdBy")),
        title=row.get("title") if isinstance(row.get("title"), str) and row["title"] else "Dagbok",
        body=html_to_text(row.get("content")) or "",
        attachments=pictures,
    )
    if row.get("_detail_missing"):
        item["content_complete"] = False
    return item


def normalize_message(row: Mapping[str, Any], placement: Placement) -> dict[str, Any]:
    item = _base("message", placement, row["id"], topic="Melding")
    sender = _name(row.get("sender"))
    item.update(
        thread_id=f"messages:{placement.placement_id}",
        type="message",
        sender=sender,
        published_at=_iso(row.get("createdDate")),
        title=f"Melding fra {sender}" if sender else "Melding",
        body=html_to_text(row.get("message")) or "",
    )
    return item


def normalize_event(row: Mapping[str, Any], placement: Placement) -> dict[str, Any]:
    occurrence = occurrence_token(row["startDate"])
    item = _base("event", placement, f"{row['id']}:{occurrence}", topic="Kalender")
    item.update(
        type="event",
        title=row.get("title")
        if isinstance(row.get("title"), str) and row["title"]
        else "Hendelse",
        body=html_to_text(row.get("description")) or "",
        event_start=_iso(row.get("startDate")),
        event_end=_iso(row.get("endDate")),
        date_note="Hele dagen" if row.get("isAllDay") is True else None,
        attachments=_attachments(row.get("attachments")),
    )
    if row.get("_detail_missing"):
        item["content_complete"] = False
    return item


def normalize_absence(row: Mapping[str, Any], placement: Placement) -> dict[str, Any]:
    item = _base("absence", placement, row["id"], topic="Fravær")
    start, stop = _iso(row.get("startDate")), _iso(row.get("stopDate"))
    period = " – ".join(value[:10] for value in (start, stop) if value)
    item.update(
        type="absence",
        sender=_name(row.get("modifiedBy")),
        published_at=_iso(row.get("createdDate")),
        title=f"Fravær {period}".strip(),
        body=html_to_text(row.get("comment")) or "",
        event_start=start,
        event_end=stop,
    )
    return item


def _clock(moment: datetime | None) -> str | None:
    return moment.astimezone(LOCAL_ZONE).strftime("%H:%M") if moment else None


def normalize_attendance(row: Mapping[str, Any], placement: Placement) -> dict[str, Any]:
    item = _base("attendance", placement, row["id"], topic="Inn-/utsjekk")
    check_in, check_out = _instant(row.get("checkInDateTime")), _instant(row.get("checkOutDateTime"))
    day = (check_in or check_out).astimezone(LOCAL_ZONE) if check_in or check_out else None
    parts = []
    if check_in:
        parts.append(f"Sjekket inn {_clock(check_in)}.")
    if check_out:
        parts.append(f"Sjekket ut {_clock(check_out)}.")
    item.update(
        type="attendance",
        published_at=_iso(row.get("checkInDateTime")),
        changed_at=_iso(row.get("checkOutDateTime")),
        title=f"Inn-/utsjekk {day:%d.%m.%Y}" if day else "Inn-/utsjekk",
        body=" ".join(parts),
        event_start=_iso(row.get("checkInDateTime")),
        event_end=_iso(row.get("checkOutDateTime")),
    )
    return item


KINDS = {
    "announcement": (announcements, normalize_announcement),
    "diary": (diary_posts, normalize_diary_post),
    "message": (messages, normalize_message),
    "absence": (absences, normalize_absence),
    "attendance": (attendances, normalize_attendance),
}


def in_window(item: Mapping[str, Any], since: datetime) -> bool:
    stamps = [
        _instant(item.get(name))
        for name in ("changed_at", "published_at", "event_end", "event_start")
    ]
    known = [stamp for stamp in stamps if stamp is not None]
    return not known or max(known) >= since


def collect(
    client: Client, since: datetime, *, today: date | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """Every barnehage stream for every placement, within fixed limits."""

    children, complete = placements(client)
    items: list[dict[str, Any]] = []
    failure: SourceUnavailable | None = None
    first = since.astimezone(UTC).date()
    today = today or datetime.now(UTC).date()
    last = today + timedelta(days=CALENDAR_DAYS_AHEAD)
    first = max(first, last - timedelta(days=MAX_CALENDAR_SPAN_DAYS))
    for placement in children:
        streams: list[
            tuple[Callable[[], tuple[list[Any], bool]], Callable[..., dict[str, Any]]]
        ] = [
            (lambda fetch=fetch, placement=placement: fetch(client, placement, since), normalize)
            for fetch, normalize in KINDS.values()
        ]
        streams.append(
            (
                lambda placement=placement: calendar_events(client, placement, first, last),
                normalize_event,
            )
        )
        for fetch, normalize in streams:
            try:
                rows, stream_complete = fetch()
            except SourceLoginRequired:
                raise
            except SourceUnavailable as exc:
                failure = failure or exc
                complete = False
                continue
            complete = complete and stream_complete
            for row in rows:
                if not _identifier(row.get("id")) or row.get("isHidden") is True:
                    continue
                item = normalize(row, placement)
                if in_window(item, since):
                    items.append(item)
    if failure is not None and not items:
        raise failure
    return items, complete


def find(
    client: Client, placement: Placement, kind: str, raw_id: str, occurrence: str | None = None
) -> dict[str, Any]:
    """Read one item again through the same bounded list reads."""

    if kind == "event":
        for day in occurrence_days(occurrence or ""):
            for event in events_on(client, placement, day):
                if (
                    event["id"] == raw_id
                    and _instant(event.get("startDate"))
                    and occurrence_token(event["startDate"]) == occurrence
                ):
                    return normalize_event(
                        {**event, **event_detail(client, placement, event)}, placement
                    )
        raise SourceUnavailable("Kalenderhendelsen finnes ikke lenger i Flyt")
    if kind not in KINDS:
        raise FamiliekilderError("Ukjent Flyt-referansetype")
    if kind == "diary":
        # One detail read instead of the whole diary with every photo list.
        # A post with photos is listed from this same detail. A post without
        # photos is listed from its search row; live replies (November 2026)
        # had the same id, title, content, createdDate and createdBy in both,
        # and the post has no photos to differ in.
        return normalize_diary_post(diary_post(client, placement, raw_id), placement)
    fetch, normalize = KINDS[kind]
    rows, _complete = fetch(client, placement, None)
    for row in rows:
        if row.get("id") == raw_id:
            return normalize(row, placement)
    raise SourceUnavailable(
        "Innholdet finnes ikke lenger i Flyt eller ligger utenfor grensen for oppslag"
    )


def placement_for(client: Client, placement_id: str) -> Placement:
    children, _complete = placements(client)
    for placement in children:
        if placement.placement_id == placement_id:
            return placement
    raise SourceUnavailable("Barnet finnes ikke lenger i Flyt")


def unique(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for item in items:
        if item["source_id"] not in seen:
            seen.add(item["source_id"])
            result.append(item)
    return result
