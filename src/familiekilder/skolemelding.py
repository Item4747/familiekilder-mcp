"""Bounded, read-only Skolemelding collection through the official app API."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import time
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .api_transport import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    ApiHTTPError,
    ApiTransport,
    ApiTransportError,
    DownloadResult,
)
from .skolemelding_auth import load_client_config as load_client_config
from .core import FamiliekilderError, html_to_text, parse_iso
from .private_state import (
    ensure_private_directory,
    remove_private_file,
)
from .skolemelding_auth import (
    PORTAL_BASE_URL,
    AuthTransport,
    ClientConfig,
    local_session_for_use,
    refresh_session,
)
from .source_adapter import SourceFatalConfigurationError, SourceLoginRequired, SourceUnavailable


SOURCE = "skolemelding"
PROFILE_ENDPOINT = f"{PORTAL_BASE_URL}/api/v1/mobilesettings"
CHILDREN_ENDPOINT = f"{PORTAL_BASE_URL}/api/MyProfile/GetChildInfo"
CHILD_TEACHERS_ENDPOINT = f"{PORTAL_BASE_URL}/api/MyChildsTeachers"
MESSAGE_SEARCH_ENDPOINT = f"{PORTAL_BASE_URL}/api/messagesearchmobile/"
MESSAGE_DETAIL_ENDPOINT = f"{PORTAL_BASE_URL}/api/message/GetMessageWithId"

PAGE_SIZE = 50
MAX_PAGES = 200
MAX_MESSAGES = 5_000
MAX_CHILDREN = 100
MAX_SEARCH_REQUESTS = 500
MAX_ATTACHMENT_BYTES = DEFAULT_MAX_DOWNLOAD_BYTES
MAX_RUN_DOWNLOAD_BYTES = 100 * 1024 * 1024


class _AttachmentAuthMode(Enum):
    PORTAL_BEARER = "portal-bearer"
    TOKEN_FREE = "token-free"


class CollectorTransport(Protocol):
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

    def download(
        self,
        url: str,
        target: Path | str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        allowed_content_types: set[str] | frozenset[str] | None = None,
    ) -> DownloadResult | Any: ...



@dataclass(frozen=True)
class _Child:
    search_id: str
    name: str | None


@dataclass
class DownloadBudget:
    limit: int = MAX_RUN_DOWNLOAD_BYTES
    used: int = 0
    failed_items: set[str] = dataclass_field(default_factory=set)

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def consume(self, amount: int) -> None:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise SourceUnavailable("Skolemelding-vedlegget oppga ugyldig størrelse")
        if self.used + amount > self.limit:
            raise SourceUnavailable("Skolemelding-vedlegg overskred samlet nedlastingsgrense")
        self.used += amount

    def record_failure(self, source_id: str) -> None:
        self.failed_items.add(source_id)


def default_transport(config: ClientConfig) -> ApiTransport:
    return ApiTransport(
        allowed_hosts={"portal.skoleplattform.no", *config.attachment_hosts},
        timeout=45,
        max_json_bytes=25 * 1024 * 1024,
        max_download_bytes=MAX_ATTACHMENT_BYTES,
        user_agent="familiekilder-skolemelding/0.2",
    )


class AuthorizedClient:
    def __init__(
        self,
        root: Path,
        transport: CollectorTransport,
        auth_transport: AuthTransport | None,
        config: ClientConfig,
    ) -> None:
        self.root = root
        self.transport = transport
        self.auth_transport = auth_transport
        self.app_version = config.app_version
        self.attachment_hosts = frozenset(config.attachment_hosts)
        self.attachment_path_prefixes = tuple(config.attachment_path_prefixes)
        self.session = local_session_for_use(root)
        self._refreshed = False
        expires_at = self.session.get("access_expires_at")
        if isinstance(expires_at, (int, float)) and float(expires_at) <= time.time() + 300:
            refresh_session(self.root, transport=self.auth_transport)
            self.session = local_session_for_use(self.root)
            self._refreshed = True

    @property
    def token(self) -> str:
        return str(self.session["access_token"])

    def _request(self, operation: Any) -> Any:
        try:
            return operation(self.token)
        except ApiHTTPError as exc:
            if exc.status_code == 403:
                raise SourceLoginRequired("Skolemelding krever ny innlogging") from None
            if exc.status_code != 401 or self._refreshed:
                if exc.status_code == 401:
                    raise SourceLoginRequired("Skolemelding krever ny innlogging") from None
                raise
        self._refreshed = True
        refresh_session(self.root, transport=self.auth_transport)
        self.session = local_session_for_use(self.root)
        try:
            return operation(self.token)
        except ApiHTTPError as exc:
            if exc.status_code in {401, 403}:
                raise SourceLoginRequired("Skolemelding krever ny innlogging") from None
            raise

    @staticmethod
    def _guard_api_url(url: str, *, method: str) -> None:
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            raise SourceUnavailable("Skolemelding-adapteren avviste en ugyldig API-rute") from None
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        allowed = {
            "GET": {
                PROFILE_ENDPOINT, CHILDREN_ENDPOINT, MESSAGE_DETAIL_ENDPOINT,
                CHILD_TEACHERS_ENDPOINT,
            },
            "POST": {MESSAGE_SEARCH_ENDPOINT},
        }
        if base not in allowed.get(method, set()):
            raise SourceUnavailable("Skolemelding-adapteren avviste en ikke-lesende API-rute")
        if parsed.fragment:
            raise SourceUnavailable("Skolemelding-adapteren avviste fragment i API-ruten")
        if base == CHILD_TEACHERS_ENDPOINT:
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if set(query) != {"schoolId", "childId"} or any(
                len(values) != 1 or not values[0].strip() for values in query.values()
            ):
                raise SourceUnavailable("Læreroppslaget krever ett barn og én skole")

    def _guard_attachment_url(self, url: str) -> _AttachmentAuthMode:
        # Attachment URLs are data-driven, so they get a separate narrow guard
        # in addition to ApiTransport's exact HTTPS host allowlist.
        try:
            parsed = urllib.parse.urlsplit(url)
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            raise SourceUnavailable("Skolemelding-adapteren avviste en ugyldig vedleggsrute") from None
        decoded_path = urllib.parse.unquote(parsed.path)
        if (
            parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or host not in self.attachment_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.path.startswith(self.attachment_path_prefixes)
            or not decoded_path.startswith(self.attachment_path_prefixes)
            or any(
                segment in {".", ".."} for segment in decoded_path.split("/")
            )
        ):
            raise SourceUnavailable("Skolemelding-adapteren avviste en ikke-lesende vedleggsrute")
        if host == "portal.skoleplattform.no":
            return _AttachmentAuthMode.PORTAL_BEARER
        return _AttachmentAuthMode.TOKEN_FREE

    def get_json(self, url: str) -> Any:
        self._guard_api_url(url, method="GET")
        from .sync_snapshot import memo_read

        return memo_read(
            getattr(self, 'root', None), SOURCE, ('GET', url),
            lambda: self._request(lambda token: self.transport.get_json(url, bearer_token=token)),
        )

    def post_json(self, url: str, payload: Mapping[str, Any]) -> Any:
        self._guard_api_url(url, method="POST")
        load = lambda: self._request(
            lambda token: self.transport.post_json(
                url, payload, bearer_token=token
            )
        )
        if url != MESSAGE_SEARCH_ENDPOINT:
            return load()
        from .sync_snapshot import memo_read

        return memo_read(getattr(self, 'root', None), SOURCE, ('POST', url, payload), load)

    def download(self, url: str, target: Path, *, max_bytes: int) -> Any:
        auth_mode = self._guard_attachment_url(url)
        if auth_mode is _AttachmentAuthMode.TOKEN_FREE:
            # Signed external attachment routes are deliberately credentialless.
            # In particular, their 401/403 responses must remain an item-local
            # attachment failure instead of rotating the portal OAuth session.
            return self.transport.download(
                url,
                target,
                max_bytes=max_bytes,
            )
        return self._request(
            lambda token: self.transport.download(
                url, target, bearer_token=token, max_bytes=max_bytes
            )
        )


def _pick(mapping: Mapping[str, Any] | None, *names: str) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return None


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []




def _safe_iso(value: Any, field: str) -> str | None:
    try:
        return parse_iso(value, field)
    except FamiliekilderError:
        return None


def _timestamp(mapping: Mapping[str, Any] | None, *, changed: bool = False) -> str | None:
    if changed:
        value = _pick(
            mapping,
            "ChangedAt",
            "changedAt",
            "UpdatedAt",
            "updatedAt",
            "ModifiedAt",
            "modifiedAt",
            "LastModified",
            "lastModified",
        )
        return _safe_iso(value, "changed_at")
    value = _pick(
        mapping,
        "Timestamp",
        "timestamp",
        "PublishedAt",
        "publishedAt",
        "CreatedAt",
        "createdAt",
        "SentAt",
        "sentAt",
    )
    return _safe_iso(value, "published_at")


def timestamp_values(mapping: Mapping[str, Any] | None) -> list[datetime]:
    if not isinstance(mapping, Mapping):
        return []
    values: list[datetime] = []
    for changed in (False, True):
        normalized = _timestamp(mapping, changed=changed)
        if normalized is not None:
            values.append(datetime.fromisoformat(normalized))
    for field in ("Threads", "threads", "Replies", "replies"):
        for nested in _as_list(mapping.get(field)):
            if isinstance(nested, Mapping):
                normalized = _timestamp(nested)
                if normalized is not None:
                    values.append(datetime.fromisoformat(normalized))
    return values


def message_in_window(
    summary: Mapping[str, Any], cutoff_at: str, detail: Mapping[str, Any] | None = None
) -> bool:
    cutoff = datetime.fromisoformat(cutoff_at)
    timestamps = timestamp_values(summary) + timestamp_values(detail)
    if not timestamps:
        # An unknown summary may be opened once so its detail can establish a
        # date. Unknown detail must never be persisted as arbitrary history.
        return detail is None
    return max(timestamps) >= cutoff


def _page_is_strictly_old(messages: list[Mapping[str, Any]], cutoff_at: str) -> bool:
    if not messages:
        return False
    cutoff = datetime.fromisoformat(cutoff_at)
    values = [timestamp_values(message) for message in messages]
    return all(timestamps and max(timestamps) < cutoff for timestamps in values)


def child_name(value: Mapping[str, Any]) -> str | None:
    direct = _pick(
        value,
        "Name",
        "name",
        "DisplayName",
        "displayName",
        "FullName",
        "fullName",
    )
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts = [
        _pick(value, "FirstName", "firstName", "GivenName", "givenName"),
        _pick(value, "LastName", "lastName", "FamilyName", "familyName"),
    ]
    name = " ".join(str(part).strip() for part in parts if isinstance(part, str) and part.strip())
    return name or None


def _child_record(value: Any) -> _Child | None:
    if not isinstance(value, Mapping):
        return None
    wrapper = value.get("dependant")
    if isinstance(wrapper, Mapping):
        value = wrapper
    domain_user = value.get("DomainUser") or value.get("domainUser")
    domain_mapping = domain_user if isinstance(domain_user, Mapping) else None
    search_value = _pick(
        domain_mapping,
        "UserName",
        "Username",
        "userName",
        "username",
    ) or _pick(
        value,
        "UserName",
        "Username",
        "userName",
        "username",
        "ChildId",
        "childId",
        "Id",
        "id",
    )
    if search_value in (None, ""):
        return None
    name = child_name(value) or (child_name(domain_mapping) if domain_mapping else None)
    return _Child(str(search_value), name)


def children(profile: Any, child_info: Any) -> list[_Child]:
    candidates: list[Any] = []
    for value in (profile, child_info):
        if isinstance(value, list):
            candidates.extend(value)
            continue
        if not isinstance(value, Mapping):
            continue
        for field in (
            "DependantUsers",
            "dependantUsers",
            "Children",
            "children",
            "Items",
            "items",
            "Result",
            "result",
        ):
            candidates.extend(_as_list(value.get(field)))
    result: list[_Child] = []
    seen: set[str] = set()
    for candidate in candidates:
        child = _child_record(candidate)
        if child is not None and child.search_id not in seen:
            seen.add(child.search_id)
            result.append(child)
            if len(result) > MAX_CHILDREN:
                raise SourceUnavailable("Skolemelding returnerte for mange barn")
    return result


def search_payload(page: int, child_id: str) -> dict[str, Any]:
    return {
        "Page": page,
        "PageSize": PAGE_SIZE,
        "SearchTerm": "",
        "SearchInbox": True,
        "FilterSender": [],
        "FilterUnreadMessages": False,
        "FilterFeedbackRequired": False,
        "FilterFeedbackIsQuestion": False,
        "FilterIsAbsentMessage": False,
        "SendHiddenMessages": False,
        "OnBehalfOf": child_id,
    }


def search_result(value: Any) -> tuple[list[Mapping[str, Any]], int | None]:
    if not isinstance(value, Mapping):
        raise SourceUnavailable("Skolemelding returnerte ugyldig meldingsliste")
    container: Mapping[str, Any] = value
    if isinstance(value.get("data"), Mapping):
        container = value["data"]
    raw_messages = _pick(container, "Result", "result", "Messages", "messages", "Items", "items")
    if raw_messages is None:
        raw_messages = []
    if not isinstance(raw_messages, list) or any(
        not isinstance(message, Mapping) for message in raw_messages
    ):
        raise SourceUnavailable("Skolemelding returnerte ugyldig meldingsliste")
    total_value = _pick(container, "TotalMatching", "totalMatching", "Total", "total")
    total: int | None = None
    if isinstance(total_value, int) and not isinstance(total_value, bool) and total_value >= 0:
        total = total_value
    return list(raw_messages), total


def _message_id(message: Mapping[str, Any]) -> str | None:
    value = _pick(message, "Id", "id", "MessageId", "messageId")
    return str(value) if value not in (None, "") else None


def _stable_message_id(
    summary: Mapping[str, Any], detail: Mapping[str, Any] | None = None
) -> str | None:
    """Keep child-scoped search identities lossless until live evidence says otherwise."""

    message_id = _message_id(detail or {}) or _message_id(summary)
    if message_id is None:
        return None
    child_id = _pick(detail, "OnBehalfOf", "onBehalfOf", "ChildId", "childId") or _pick(
        summary, "OnBehalfOf", "onBehalfOf", "ChildId", "childId"
    )
    if child_id in (None, ""):
        return message_id
    return f"{message_id}@{urllib.parse.quote(str(child_id), safe='')}"


def _thread_id(message: Mapping[str, Any]) -> str | None:
    value = _pick(message, "ThreadId", "threadId")
    if value not in (None, ""):
        return str(value)
    for field in ("Threads", "threads", "Replies", "replies"):
        for thread in _as_list(message.get(field)):
            if isinstance(thread, Mapping):
                value = _pick(thread, "ThreadId", "threadId", "Id", "id")
                if value not in (None, ""):
                    return str(value)
    return None


def _bool_query(value: Any) -> str:
    return "true" if bool(value) else "false"


def detail_url(summary: Mapping[str, Any]) -> str:
    message_id = _message_id(summary)
    if message_id is None:
        raise SourceUnavailable("Skolemelding-meldingen mangler stabil ID")
    parameters: list[tuple[str, str]] = [
        ("messageId", message_id),
        ("threadId", _thread_id(summary) or ""),
        (
            "isReplyAllowed",
            _bool_query(_pick(summary, "IsReplyAllowed", "isReplyAllowed")),
        ),
    ]
    on_behalf = _pick(summary, "OnBehalfOf", "onBehalfOf")
    if on_behalf not in (None, ""):
        parameters.append(("onBehalfOf", str(on_behalf)))
    favourite = _pick(summary, "IsFavourite", "isFavourite")
    if favourite is not None:
        parameters.append(("isFavourite", _bool_query(favourite)))
    historic = _pick(summary, "IsHistoric", "isHistoric")
    if historic is not None:
        parameters.append(("isHistoric", _bool_query(historic)))
    return f"{MESSAGE_DETAIL_ENDPOINT}?{urllib.parse.urlencode(parameters)}"


def detail(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceUnavailable("Skolemelding returnerte ugyldig meldingstråd")
    candidate = _pick(value, "message", "Message", "data")
    if isinstance(candidate, Mapping):
        return candidate
    return value


def _absolute_attachment_url(value: str) -> str:
    url = urllib.parse.urljoin(f"{PORTAL_BASE_URL}/", value)
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise SourceUnavailable("Skolemelding-vedlegget har ugyldig URL") from None
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(
            segment in {".", ".."}
            for segment in urllib.parse.unquote(parsed.path).split("/")
        )
    ):
        raise SourceUnavailable("Skolemelding-vedlegget har ugyldig URL")
    return url


def _attachment_values(message: Mapping[str, Any]) -> Iterable[tuple[str, str | None]]:
    for field in ("Attachments", "attachments", "Files", "files"):
        for attachment in _as_list(message.get(field)):
            if isinstance(attachment, str):
                yield attachment, None
            elif isinstance(attachment, Mapping):
                url = _pick(
                    attachment,
                    "Url",
                    "url",
                    "FileUrl",
                    "fileUrl",
                    "DownloadUrl",
                    "downloadUrl",
                )
                name = _pick(attachment, "FileName", "fileName", "Name", "name")
                if isinstance(url, str):
                    yield url, str(name) if name not in (None, "") else None
    for field in ("Images", "images"):
        for image in _as_list(message.get(field)):
            if isinstance(image, str):
                yield image, None
            elif isinstance(image, Mapping):
                url = _pick(image, "ImageUrl", "imageUrl", "Url", "url")
                name = _pick(image, "FileName", "fileName", "Name", "name")
                if isinstance(url, str):
                    yield url, str(name) if name not in (None, "") else None


def attachment_specs(detail: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    """Return raw attachment candidates; validate each URL at download time."""

    result: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    messages = [detail]
    for field in ("Replies", "replies", "Threads", "threads"):
        messages.extend(
            value for value in _as_list(detail.get(field)) if isinstance(value, Mapping)
        )
    for message in messages:
        for raw_url, name in _attachment_values(message):
            if raw_url not in seen:
                seen.add(raw_url)
                result.append((raw_url, name))
    return result


def _safe_filename(url: str, supplied: str | None, ordinal: int) -> str:
    candidate = supplied or Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
    candidate = candidate.replace("/", "_").replace("\\", "_").strip(" .")
    candidate = re.sub(r"[^0-9A-Za-z._() -]+", "_", candidate)
    if not candidate or candidate in {".", ".."}:
        candidate = f"vedlegg-{ordinal}"
    if len(candidate) > 160:
        suffix = Path(candidate).suffix[:10]
        candidate = f"{candidate[:140]}{suffix}"
    return candidate


def download_attachments(
    client: AuthorizedClient,
    incoming: Path,
    source_id: str,
    detail: Mapping[str, Any],
    budget: DownloadBudget,
) -> list[dict[str, Any]]:
    try:
        specs = attachment_specs(detail)
    except (ApiTransportError, FamiliekilderError, OSError, ValueError):
        budget.record_failure(source_id)
        return []
    if not specs:
        return []
    message_dir = incoming / hashlib.sha256(source_id.encode()).hexdigest()[:32]
    try:
        ensure_private_directory(message_dir)
    except FamiliekilderError:
        budget.record_failure(source_id)
        return []
    attachments: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for ordinal, (raw_url, supplied_name) in enumerate(specs, start=1):
        if budget.remaining <= 0:
            budget.record_failure(source_id)
            break
        target: Path | None = None
        try:
            url = _absolute_attachment_url(raw_url)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            filename = _safe_filename(url, supplied_name, ordinal)
            url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
            target = message_dir / f"{ordinal:03d}-{url_hash}-{filename}"
            result = client.download(
                url,
                target,
                max_bytes=min(MAX_ATTACHMENT_BYTES, budget.remaining),
            )
            if not target.is_file():
                raise SourceUnavailable("Skolemelding-vedlegget ble ikke lagret")
            byte_size = (
                result.byte_size
                if isinstance(result, DownloadResult)
                else int(target.stat().st_size)
            )
            budget.consume(byte_size)
            content_type = result.content_type if isinstance(result, DownloadResult) else None
            mime_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            attachments.append(
                {
                    "path": str(target),
                    "filename": filename,
                    "mime_type": mime_type,
                    "summary": "Vedlegg til Skolemelding-tråd",
                }
            )
        except (SourceLoginRequired, SourceFatalConfigurationError):
            raise
        except (ApiTransportError, FamiliekilderError, OSError, ValueError):
            if target is not None:
                try:
                    remove_private_file(target)
                except FamiliekilderError:
                    pass
            budget.record_failure(source_id)
    return attachments


def _child_for_message(
    summary: Mapping[str, Any], detail: Mapping[str, Any], child_names: Mapping[str, str | None]
) -> str | None:
    identifier = _pick(
        detail,
        "OnBehalfOf",
        "onBehalfOf",
        "ChildId",
        "childId",
    ) or _pick(summary, "OnBehalfOf", "onBehalfOf", "ChildId", "childId")
    if identifier not in (None, ""):
        key = str(identifier)
        if child_names.get(key):
            return child_names[key]
    name = _pick(detail, "ChildName", "childName", "DependantName", "dependantName")
    return str(name).strip() if isinstance(name, str) and name.strip() else None


def _thread_body(detail: Mapping[str, Any], summary: Mapping[str, Any]) -> str:
    main = html_to_text(
        _pick(detail, "Body", "body", "MessageText", "messageText", "Content", "content")
    ) or html_to_text(_pick(summary, "Body", "body", "MessageText", "messageText"))
    parts = [main or "(Meldingen har ingen tekst)"]
    replies = _as_list(detail.get("Replies") or detail.get("replies"))
    for reply in replies:
        if not isinstance(reply, Mapping):
            continue
        body = html_to_text(
            _pick(reply, "MessageText", "messageText", "Body", "body", "Content", "content")
        )
        if not body:
            continue
        sender = _pick(reply, "SenderName", "senderName", "From", "from")
        timestamp = _timestamp(reply)
        heading = " — ".join(
            str(value).strip()
            for value in (timestamp, sender)
            if isinstance(value, str) and value.strip()
        )
        parts.append(f"{heading}:\n{body}" if heading else body)
    return "\n\n---\n\n".join(parts)


def normalize_message(
    summary: Mapping[str, Any],
    detail: Mapping[str, Any],
    *,
    child_names: Mapping[str, str | None],
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    source_id = _stable_message_id(summary, detail)
    if source_id is None:
        raise SourceUnavailable("Skolemelding-meldingen mangler stabil ID")
    thread_id = _thread_id(detail) or _thread_id(summary) or source_id
    title = html_to_text(
        _pick(
            detail,
            "Header",
            "header",
            "MessageTitle",
            "messageTitle",
            "Title",
            "title",
            "Subject",
            "subject",
        )
    ) or html_to_text(_pick(summary, "Header", "header", "Title", "title"))
    sender = _pick(detail, "From", "from", "SenderName", "senderName") or _pick(
        summary, "From", "from", "SenderName", "senderName"
    )
    published_at = _timestamp(detail) or _timestamp(summary)
    changed_at = _timestamp(detail, changed=True) or _timestamp(summary, changed=True)
    action_required = None
    if bool(
        _pick(
            detail,
            "IsFeedbackRequired",
            "isFeedbackRequired",
            "FeedbackRequired",
            "feedbackRequired",
        )
        or _pick(summary, "IsFeedbackRequired", "isFeedbackRequired")
    ):
        action_required = "Bekreftelse eller tilbakemelding etterspørres"
    is_absence = bool(
        _pick(detail, "IsAbsentMessage", "isAbsentMessage")
        or _pick(summary, "IsAbsentMessage", "isAbsentMessage")
    )
    raw_timestamp = _pick(detail, "Timestamp", "timestamp") or _pick(
        summary, "Timestamp", "timestamp"
    )
    date_note = None
    if raw_timestamp not in (None, "") and published_at is None:
        date_note = f"Original tidsangivelse: {raw_timestamp}"
    return {
        "source": SOURCE,
        "source_id": source_id,
        "thread_id": thread_id,
        "child": _child_for_message(summary, detail, child_names),
        "sender": str(sender).strip() if isinstance(sender, str) and sender.strip() else None,
        "published_at": published_at,
        "changed_at": changed_at,
        "title": title or "Skolemelding",
        "body": _thread_body(detail, summary),
        "type": "absence" if is_absence else "message",
        "deadline": None,
        "event_start": None,
        "event_end": None,
        "location": None,
        "action_required": action_required,
        "source_url": None,
        "topic": "Skolemelding",
        "date_note": date_note,
        "attachments": attachments,
    }


def fetch_context(client: AuthorizedClient, app_version: str) -> tuple[Any, Any]:
    profile_url = f"{PROFILE_ENDPOINT}?{urllib.parse.urlencode({'appVersion': app_version})}"
    try:
        profile = client.get_json(profile_url)
        child_info = client.get_json(CHILDREN_ENDPOINT)
    except SourceLoginRequired:
        raise
    except (ApiTransportError, OSError):
        raise SourceUnavailable("Skolemelding-profilen kunne ikke hentes") from None
    if not isinstance(profile, (Mapping, list)) or not isinstance(
        child_info, (Mapping, list)
    ):
        raise SourceUnavailable("Skolemelding returnerte ugyldig profilinformasjon")
    return profile, child_info


def fetch_summaries(
    client: AuthorizedClient, children: list[_Child], cutoff_at: str
) -> list[Mapping[str, Any]]:
    selected: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    requests = 0
    search_children = children or [_Child("", None)]
    for child in search_children:
        fetched_for_child = 0
        for page in range(1, MAX_PAGES + 1):
            requests += 1
            if requests > MAX_SEARCH_REQUESTS:
                raise SourceUnavailable(
                    "Skolemelding overskred samlet grense for meldingssider"
                )
            try:
                response = client.post_json(
                    MESSAGE_SEARCH_ENDPOINT, search_payload(page, child.search_id)
                )
                messages, total = search_result(response)
            except SourceLoginRequired:
                raise
            except ApiTransportError:
                raise SourceUnavailable(
                    "Skolemelding-meldingslisten kunne ikke hentes"
                ) from None
            for raw_message in messages:
                message = dict(raw_message)
                message.setdefault("OnBehalfOf", child.search_id)
                message_id = _message_id(message)
                if message_id is None:
                    continue
                stable_key = f"{message_id}\0{message.get('OnBehalfOf', '')}"
                if stable_key in seen:
                    continue
                seen.add(stable_key)
                if message_in_window(message, cutoff_at):
                    selected.append(message)
                    if len(selected) > MAX_MESSAGES:
                        raise SourceUnavailable("Skolemelding returnerte for mange meldinger")
            fetched_for_child += len(messages)
            if _page_is_strictly_old(messages, cutoff_at):
                break
            if not messages or len(messages) < PAGE_SIZE:
                break
            if total is not None and fetched_for_child >= total:
                break
        else:
            raise SourceUnavailable("Skolemelding returnerte for mange meldingssider")
    return selected


__all__ = [
    "CHILDREN_ENDPOINT",
    "MESSAGE_DETAIL_ENDPOINT",
    "MESSAGE_SEARCH_ENDPOINT",
    "PROFILE_ENDPOINT",
    "message_in_window",
    "normalize_message",
    "AuthorizedClient",
    "DownloadBudget",
    "attachment_specs",
    "child_name",
    "children",
    "default_transport",
    "detail",
    "detail_url",
    "download_attachments",
    "fetch_context",
    "fetch_summaries",
    "search_payload",
    "search_result",
    "timestamp_values",
]
