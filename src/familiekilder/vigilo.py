"""Read-only Vigilo parent API collector.

Only GET endpoints observed in the official parent app are used.  In
particular, this module never calls the viewing/read-status or reply endpoints,
so collection cannot mark messages as read or mutate provider state.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol

from .api_transport import (
    ApiHTTPError,
    ApiTransport,
    ApiTransportError,
    DownloadResult,
)
from .vigilo_auth import access_token_for_use as access_token_for_use, local_session_for_use as local_session_for_use, refresh_session as refresh_session
from .core import FamiliekilderError, html_to_text_unescaped
from .private_state import (
    PrivateStateError,
    remove_private_file,
)
from .source_adapter import SourceUnavailable
from .vigilo_auth import (
    API_ROOT,
)


SOURCE = "vigilo"
PAGE_SIZE = 50
MAX_PAGES = 200
MAX_CHILDREN = 100
MAX_THREADS = 5_000
MAX_NEWS_ITEMS = 5_000
MAX_MESSAGES_PER_THREAD = 5_000
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_RUN_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_JSON_BYTES = 25 * 1024 * 1024


class VigiloApi(Protocol):
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



class DownloadBudget:
    def __init__(self, limit: int = MAX_RUN_DOWNLOAD_BYTES) -> None:
        self.limit = limit
        self.used = 0
        self.failed_items: set[str] = set()

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def record(self, amount: int) -> None:
        if amount < 0 or amount > self.remaining:
            raise FamiliekilderError(
                "Vigilo-vedlegg overskred samlet nedlastingsgrense"
            )
        self.used += amount

    def record_failure(self, stable_id: str) -> None:
        self.failed_items.add(stable_id)


def api_transport(session: Mapping[str, Any]) -> ApiTransport:
    api_host = urllib.parse.urlsplit(str(session.get("api_root") or API_ROOT)).hostname
    configured_hosts = session.get("attachment_hosts")
    attachment_hosts = (
        {str(host) for host in configured_hosts if isinstance(host, str)}
        if isinstance(configured_hosts, list)
        else set()
    )
    if api_host:
        attachment_hosts.add(api_host)
    return ApiTransport(
        allowed_hosts=attachment_hosts,
        timeout=45,
        max_json_bytes=MAX_JSON_BYTES,
        max_download_bytes=MAX_ATTACHMENT_BYTES,
        user_agent="familiekilder-vigilo/0.2",
    )


def _url(path: str, query: list[tuple[str, str]] | None = None) -> str:
    allowed_prefixes = (
        "api/children",
        "api/message-threads",
        "api/messages/threads/",
        "api/news-feed",
        "api/posts/",
    )
    if not path.startswith(allowed_prefixes):
        raise FamiliekilderError("Vigilo-adapteren nektet et ukjent endepunkt")
    target = urllib.parse.urljoin(API_ROOT, path)
    if query:
        target += "?" + urllib.parse.urlencode(query, doseq=True)
    return target


def get(
    client: VigiloApi,
    token: str,
    app_version: str,
    path: str,
    query: list[tuple[str, str]] | None = None,
) -> Any:
    return client.get_json(
        _url(path, query),
        bearer_token=token,
        headers={"appVersion": app_version},
    )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceUnavailable(f"Vigilo returnerte ugyldig {label}")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise SourceUnavailable(f"Vigilo returnerte ugyldig {label}")
    return value


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _identifier(value: Any, label: str) -> str:
    result = _text(value)
    if (
        result is None
        or len(result) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in result)
    ):
        raise SourceUnavailable(f"Vigilo returnerte ugyldig {label}")
    return result


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _item_timestamp(value: Mapping[str, Any]) -> datetime | None:
    candidates = [
        _parse_time(value.get(field))
        for field in (
            "editedAt",
            "updatedAt",
            "changedAt",
            "postedTime",
            "createdAt",
            "timeCreated",
            "posted",
        )
    ]
    present = [candidate for candidate in candidates if candidate is not None]
    return max(present) if present else None


def _in_window(value: Mapping[str, Any], cutoff: datetime) -> bool:
    timestamp = _item_timestamp(value)
    # Unknown age must never turn a bounded onboarding into arbitrary history.
    return timestamp is not None and timestamp >= cutoff


def _thread_in_window(summary: Mapping[str, Any], cutoff: datetime) -> bool:
    last_message = summary.get("lastMessage")
    return _in_window(summary, cutoff) or (
        isinstance(last_message, Mapping) and _in_window(last_message, cutoff)
    )



def _person_name(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    parts = [_text(value.get(field)) for field in ("firstName", "lastName")]
    return " ".join(part for part in parts if part) or _text(value.get("alias"))


def _safe_filename(value: Any, fallback: str) -> str:
    name = Path(str(value or "")).name.strip() or fallback
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name[:180] or fallback


def _mime_type(value: Any, filename: str) -> str:
    if isinstance(value, str) and "/" in value:
        return value.lower()
    if isinstance(value, Mapping):
        for field in ("mimeType", "value", "type"):
            candidate = value.get(field)
            if isinstance(candidate, str) and "/" in candidate:
                return candidate.lower()
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def fetch_children(
    client: VigiloApi, token: str, app_version: str
) -> list[dict[str, Any]]:
    response = _mapping(
        get(client, token, app_version, "api/children"), "barneliste"
    )
    values = _list(response.get("items"), "barneliste")
    if len(values) > MAX_CHILDREN:
        raise SourceUnavailable("Vigilo returnerte for mange barn")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        child = _mapping(value, "barn")
        child_id = _identifier(child.get("childId"), "barn-ID")
        if child_id in seen:
            continue
        seen.add(child_id)
        result.append(
            {
                "childId": child_id,
                "firstName": _text(child.get("firstName")),
                "lastName": _text(child.get("lastName")),
            }
        )
    return result


def child_name(child: Mapping[str, Any]) -> str:
    parts = [_text(child.get(field)) for field in ("firstName", "lastName")]
    return " ".join(part for part in parts if part) or "Ukjent barn"


def thread_pages(
    client: VigiloApi,
    token: str,
    app_version: str,
    child_id: str,
    cutoff: datetime,
) -> Iterator[Mapping[str, Any]]:
    page_id: int | None = None
    seen_pages: set[int] = set()
    for _page in range(MAX_PAGES):
        query = [
            ("childIds", child_id),
            ("fromDate", cutoff.date().isoformat()),
            ("toDate", datetime.now(UTC).date().isoformat()),
            ("pageSize", str(PAGE_SIZE)),
            ("unreadMessageThreadsCountOnly", "false"),
            ("includeAfterSchoolMessages", "true"),
        ]
        if page_id is not None:
            query.append(("pageId", str(page_id)))
        response = _mapping(
            get(client, token, app_version, "api/message-threads", query),
            "meldingstrådliste",
        )
        values = _list(response.get("messageThreads"), "meldingstrådliste")
        for value in values:
            yield _mapping(value, "meldingstråd")
        if len(values) < PAGE_SIZE:
            return
        returned_page = response.get("pageId")
        if isinstance(returned_page, bool) or not isinstance(returned_page, int):
            raise SourceUnavailable("Vigilo returnerte ugyldig sideinndeling")
        next_page = returned_page + 1
        if next_page in seen_pages:
            raise SourceUnavailable("Vigilo returnerte gjentatt side")
        seen_pages.add(next_page)
        page_id = next_page
    raise SourceUnavailable("Vigilo returnerte for mange meldingssider")


def full_thread(
    client: VigiloApi,
    token: str,
    app_version: str,
    thread_id: str,
    child_id: str,
) -> dict[str, Any]:
    page_id: int | None = None
    messages: list[dict[str, Any]] = []
    seen_messages: set[str] = set()
    result: dict[str, Any] | None = None
    for _page in range(MAX_PAGES):
        query = [
            ("childPersonId", child_id),
            ("pageSize", str(PAGE_SIZE)),
        ]
        if page_id is not None:
            query.append(("pageId", str(page_id)))
        response = _mapping(
            get(
                client,
                token,
                app_version,
                "api/messages/threads/" + urllib.parse.quote(thread_id, safe=""),
                query,
            ),
            "meldingstråd",
        )
        if result is None:
            result = dict(response)
        page_messages = _list(response.get("messages"), "meldingsliste")
        for raw in page_messages:
            message = _mapping(raw, "melding")
            message_id = _identifier(message.get("id"), "melding-ID")
            if message_id in seen_messages:
                continue
            seen_messages.add(message_id)
            messages.append(dict(message))
            if len(messages) > MAX_MESSAGES_PER_THREAD:
                raise SourceUnavailable("Vigilo returnerte for mange meldinger i én tråd")
        if len(page_messages) < PAGE_SIZE:
            break
        returned_page = response.get("pageId")
        if isinstance(returned_page, bool) or not isinstance(returned_page, int):
            raise SourceUnavailable("Vigilo returnerte ugyldig meldingsside")
        page_id = returned_page + 1
    else:
        raise SourceUnavailable("Vigilo returnerte for mange sider i meldingstråden")
    if result is None:
        raise SourceUnavailable("Vigilo returnerte en tom meldingstråd")
    result["messages"] = sorted(
        messages,
        key=lambda message: _parse_time(message.get("timeCreated"))
        or datetime.min.replace(tzinfo=UTC),
    )
    return result


def news_pages(
    client: VigiloApi,
    token: str,
    app_version: str,
    child_id: str,
    cutoff: datetime,
) -> Iterator[Mapping[str, Any]]:
    # The official Android repository never omits lastDate: its initial page
    # uses tomorrow, and subsequent pages use the oldest returned date.
    cursor = (datetime.now().astimezone().date() + timedelta(days=1)).isoformat()
    seen_cursors: set[str] = set()
    for _page in range(MAX_PAGES):
        query = [
            ("childId", child_id),
            ("lastDate", cursor),
            ("minPageSize", str(PAGE_SIZE)),
        ]
        response = get(client, token, app_version, "api/news-feed", query)
        values = _list(response, "nyhetsliste")
        mappings = [_mapping(value, "nyhet") for value in values]
        for item in mappings:
            if _in_window(item, cutoff) and str(item.get("type", "")).lower() == "post":
                yield item
        if len(values) < PAGE_SIZE:
            return
        timestamps = [
            timestamp for item in mappings if (timestamp := _item_timestamp(item)) is not None
        ]
        if not timestamps or min(timestamps) < cutoff:
            return
        next_cursor = min(timestamps).date().isoformat()
        if next_cursor in seen_cursors:
            raise SourceUnavailable("Vigilo returnerte gjentatt nyhetsside")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise SourceUnavailable("Vigilo returnerte for mange nyhetssider")


def fetch_snapshot(
    token: str,
    cutoff_at: str,
    *,
    transport: VigiloApi,
    app_version: str,
) -> dict[str, Any]:
    cutoff = _parse_time(cutoff_at)
    if cutoff is None:
        raise FamiliekilderError("Vigilo-vinduet har ugyldig cutoff")
    children = fetch_children(transport, token, app_version)
    threads: dict[str, dict[str, Any]] = {}
    for child in children:
        child_id = _identifier(child.get("childId"), "barn-ID")
        for summary in thread_pages(
            transport, token, app_version, child_id, cutoff
        ):
            if not _thread_in_window(summary, cutoff):
                continue
            thread_id = _identifier(summary.get("threadUid"), "tråd-ID")
            entry = threads.get(thread_id)
            if entry is None:
                if len(threads) >= MAX_THREADS:
                    raise SourceUnavailable("Vigilo returnerte for mange meldingstråder")
                entry = {
                    "summary": dict(summary),
                    "child_ids": [],
                }
                threads[thread_id] = entry
            if child_id not in entry["child_ids"]:
                entry["child_ids"].append(child_id)
    for thread_id, entry in threads.items():
        entry["detail"] = full_thread(
            transport,
            token,
            app_version,
            thread_id,
            str(entry["child_ids"][0]),
        )

    news: dict[str, dict[str, Any]] = {}
    for child in children:
        child_id = _identifier(child.get("childId"), "barn-ID")
        for summary in news_pages(
            transport, token, app_version, child_id, cutoff
        ):
            post_id = _identifier(summary.get("id"), "nyhets-ID")
            entry = news.get(post_id)
            if entry is None:
                if len(news) >= MAX_NEWS_ITEMS:
                    raise SourceUnavailable("Vigilo returnerte for mange nyheter")
                detail = _mapping(
                    get(
                        transport,
                        token,
                        app_version,
                        "api/posts/" + urllib.parse.quote(post_id, safe=""),
                    ),
                    "nyhetsdetalj",
                )
                entry = {
                    "summary": dict(summary),
                    "detail": dict(detail),
                    "child_ids": [],
                }
                news[post_id] = entry
            if child_id not in entry["child_ids"]:
                entry["child_ids"].append(child_id)
    return {
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "children": children,
        "message_threads": list(threads.values()),
        "news": list(news.values()),
    }


def _attachment_specs(item: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    values = item.get("attachments")
    if isinstance(values, list):
        for value in values:
            if isinstance(value, Mapping):
                yield value


def download_attachments(
    specs: Iterator[Mapping[str, Any]],
    *,
    client: VigiloApi,
    token: str,
    app_version: str,
    incoming: Path,
    stable_id: str,
    budget: DownloadBudget,
) -> list[dict[str, Any]]:
    if incoming is None:
        from .live_readers import attachment_metadata
        attachments = []
        seen_specs = {}
        for value in specs:
            entry = attachment_metadata(value)
            ident = entry["attachment_id"]
            if seen_specs.get(ident) == entry:
                continue
            seen_specs[ident] = entry
            attachments.append(entry)
        return attachments

    item_dir = incoming / hashlib.sha256(stable_id.encode("utf-8")).hexdigest()[:32]
    attachments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ordinal, spec in enumerate(specs, start=1):
        target: Path | None = None
        try:
            if budget.remaining <= 0:
                budget.record_failure(stable_id)
                break
            url_value = _text(spec.get("url"))
            if url_value is None:
                continue
            target_url = urllib.parse.urljoin(API_ROOT, url_value)
            parsed = urllib.parse.urlsplit(target_url)
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
            api_host = urllib.parse.urlsplit(API_ROOT).hostname
            if (
                parsed.scheme != "https"
                or parsed.port not in (None, 443)
                or not host
                or host == api_host
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                # No production capture establishes a safe attachment prefix on
                # the authenticated API host. Fail closed instead of allowing a
                # data-driven URL to turn into an arbitrary bearer-authenticated
                # GET. External attachment hosts are separately pinned in config.
                raise SourceUnavailable(
                    "Vigilo-vedlegget peker ikke til en fastlåst ekstern vedleggsvert"
                )
            attachment_id = _text(spec.get("id")) or target_url
            if attachment_id in seen:
                continue
            seen.add(attachment_id)
            filename = _safe_filename(spec.get("name"), f"vedlegg-{ordinal}")
            target = item_dir / f"{ordinal:03d}-{filename}"
            mime_type = _mime_type(spec.get("mimeType"), filename)
            result = client.download(
                target_url,
                target,
                bearer_token=None,
                headers=None,
                max_bytes=min(MAX_ATTACHMENT_BYTES, budget.remaining),
            )
            budget.record(result.byte_size)
            attachments.append(
                {
                    "path": str(target),
                    "filename": filename,
                    "mime_type": result.content_type or mime_type,
                    "summary": "Vedlegg fra Vigilo",
                }
            )
        except (
            ApiHTTPError,
            ApiTransportError,
            FamiliekilderError,
            OSError,
            UnicodeError,
            ValueError,
        ):
            # External signed-link, allowlist and size failures are item-level
            # partial results. Preserve the readable message and any sibling
            # attachments; they are not evidence that OAuth should be rotated.
            if target is not None:
                try:
                    remove_private_file(target)
                except PrivateStateError:
                    pass
            budget.record_failure(stable_id)
    return attachments


def _children_label(
    ids: list[str], child_names: Mapping[str, str]
) -> str | None:
    names = [child_names[child_id] for child_id in ids if child_id in child_names]
    return ", ".join(dict.fromkeys(names)) or None


def normalize_thread(
    entry: Mapping[str, Any],
    child_names: Mapping[str, str],
    *,
    client: VigiloApi,
    token: str,
    app_version: str,
    incoming: Path,
    budget: DownloadBudget,
) -> dict[str, Any]:
    summary = _mapping(entry.get("summary"), "meldingstrådsammendrag")
    detail = _mapping(entry.get("detail"), "meldingstråddetalj")
    thread_id = _identifier(
        detail.get("threadUid") or summary.get("threadUid"), "tråd-ID"
    )
    raw_messages = _list(detail.get("messages"), "meldingsliste")
    messages = [
        _mapping(message, "melding")
        for message in raw_messages
        if isinstance(message, Mapping) and message.get("isDeleted") is not True
    ]
    body_parts: list[str] = []
    attachment_specs: list[Mapping[str, Any]] = []
    for message in messages:
        sender = _person_name(message.get("sender")) or "Ukjent avsender"
        when = _text(message.get("timeCreated")) or "Ukjent tidspunkt"
        message_body = html_to_text_unescaped(message.get("body")) or "(Ingen tekst)"
        body_parts.append(f"[{when}] {sender}:\n{message_body}")
        attachment_specs.extend(_attachment_specs(message))
    last_message = summary.get("lastMessage")
    if isinstance(last_message, Mapping) and not messages:
        sender = _person_name(last_message.get("sender")) or _person_name(
            summary.get("sender")
        )
        body_parts.append(
            f"[{_text(last_message.get('timeCreated')) or 'Ukjent tidspunkt'}] "
            f"{sender or 'Ukjent avsender'}:\n"
            f"{html_to_text_unescaped(last_message.get('body')) or '(Ingen tekst)'}"
        )
        attachment_specs.extend(_attachment_specs(last_message))
    attachments = download_attachments(
        iter(attachment_specs),
        client=client,
        token=token,
        app_version=app_version,
        incoming=incoming,
        stable_id="message-thread:" + thread_id,
        budget=budget,
    )
    timestamps = [
        timestamp
        for value in (summary, detail, *messages)
        if (timestamp := _item_timestamp(value)) is not None
    ]
    published_at = _text(detail.get("createdAt"))
    if published_at is None and timestamps:
        published_at = min(timestamps).isoformat()
    changed_at = max(timestamps).isoformat() if timestamps else published_at
    child_ids = [
        str(value) for value in entry.get("child_ids", []) if isinstance(value, str)
    ]
    return {
        "source": SOURCE,
        "source_id": "message-thread:" + thread_id,
        "thread_id": thread_id,
        "child": _children_label(child_ids, child_names),
        "sender": _person_name(detail.get("sender"))
        or _person_name(summary.get("sender")),
        "published_at": published_at,
        "changed_at": changed_at,
        "title": _text(detail.get("title"))
        or _text(summary.get("title"))
        or "Vigilo-melding",
        "body": "\n\n".join(body_parts) or "(Meldingstråden har ingen tekst)",
        "type": "message",
        "deadline": None,
        "event_start": None,
        "event_end": None,
        "location": None,
        "action_required": None,
        "source_url": None,
        "topic": "Vigilo meldinger",
        "date_note": None,
        "attachments": attachments,
    }


def _event_value(date_value: Any, time_value: Any) -> str | None:
    date_text = _text(date_value)
    if date_text is None:
        return None
    time_text = _text(time_value)
    return f"{date_text}T{time_text}" if time_text else date_text


def normalize_news(
    entry: Mapping[str, Any],
    child_names: Mapping[str, str],
    *,
    client: VigiloApi,
    token: str,
    app_version: str,
    incoming: Path,
    budget: DownloadBudget,
) -> dict[str, Any]:
    summary = _mapping(entry.get("summary"), "nyhetssammendrag")
    detail = _mapping(entry.get("detail"), "nyhetsdetalj")
    post_id = _identifier(detail.get("id") or summary.get("id"), "nyhets-ID")
    detail_child_ids = detail.get("childIds")
    child_ids = (
        [str(value) for value in detail_child_ids if isinstance(value, str)]
        if isinstance(detail_child_ids, list)
        else [
            str(value)
            for value in entry.get("child_ids", [])
            if isinstance(value, str)
        ]
    )
    specs = list(_attachment_specs(detail)) or list(_attachment_specs(summary))
    attachments = download_attachments(
        iter(specs),
        client=client,
        token=token,
        app_version=app_version,
        incoming=incoming,
        stable_id="news:" + post_id,
        budget=budget,
    )
    published_at = _text(detail.get("posted")) or _text(summary.get("postedTime"))
    changed_at = (
        _text(detail.get("editedAt"))
        or _text(summary.get("editedAt"))
        or published_at
    )
    sender = _person_name(detail.get("author")) or _person_name(summary.get("postedBy"))
    return {
        "source": SOURCE,
        "source_id": "news:" + post_id,
        "thread_id": "news:" + post_id,
        "child": _children_label(child_ids, child_names),
        "sender": sender,
        "published_at": published_at,
        "changed_at": changed_at,
        "title": _text(detail.get("title"))
        or _text(summary.get("title"))
        or "Vigilo-nyhet",
        "body": html_to_text_unescaped(detail.get("text"))
        or html_to_text_unescaped(summary.get("text"))
        or "(Nyheten har ingen tekst)",
        "type": "announcement",
        "deadline": None,
        "event_start": _event_value(summary.get("startDate"), summary.get("startTime")),
        "event_end": _event_value(summary.get("endDate"), summary.get("endTime")),
        "location": _text(summary.get("location")),
        "action_required": None,
        "source_url": None,
        "topic": "Vigilo nyheter",
        "date_note": None,
        "attachments": attachments,
    }
