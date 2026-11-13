"""Bounded, read-only collection from Spond's first-party web API.

The adapter follows the routes used by the current web client for posts,
events (called ``sponds`` by the API), comments, activity summaries and group
attachments.  It never calls chat, seen/read, RSVP, reaction, notification
update or other mutation routes.  Apart from rotating authentication in
``spond_auth``, every provider request is a GET.

Requests are serialized and paced.  A 429 is never retried in the same run;
its sanitized Retry-After value is persisted as a provider cooldown.  A 5xx
response is not retried either; a defensive cooldown is persisted instead.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

from .api_transport import (
    ApiContentRejected,
    DEFAULT_MAX_DOWNLOAD_BYTES,
    ApiHTTPError,
    ApiTransport,
    ApiTransportError,
    DownloadResult,
)
from .spond_auth import PROFILE_ENDPOINT as PROFILE_ENDPOINT
from .core import FamiliekilderError, html_to_text
from .source_adapter import SourceLoginRequired, SourceUnavailable
from .spond_auth import (
    API_ROOT,
    AuthTransport,
    ClientConfig,
    SpondCooldownActive,
    access_token_for_use,
    assert_network_allowed,
    load_client_config,
    local_session_for_use,
    pace_network_request,
    provider_user_agent,
    record_cooldown,
    refresh_session,
)


SOURCE = "spond"
BASE_API_URL = "https://api.spond.com"
GROUPS_ENDPOINT = f"{API_ROOT}/groups"
POSTS_ENDPOINT = f"{API_ROOT}/posts"
# The current first-party web client uses the collection route for its event
# feed.  The legacy byCreatedTime route remains in an older Angular factory,
# but the production API returns 404 for it.
EVENTS_BY_CREATED_ENDPOINT = f"{API_ROOT}/sponds"
ACTIVITIES_ENDPOINT = f"{API_ROOT}/activities/summary"

# The first-party web client's ``belongsToCurrentContext`` intentionally treats
# CLUB_PAYMENT records returned by the PAYMENT stream as belonging to that
# stream.  A second, undocumented CLUB_PAYMENT request would duplicate traffic.
POST_TYPES = ("PLAIN", "POLL", "PAYMENT")
PAGE_SIZE = 20
MAX_PAGES_PER_STREAM = 25
MAX_REQUESTS_PER_RUN = 120
MAX_NOTIFICATION_DETAILS = 100
MAX_NOTIFICATIONS = 500
MAX_COMMENTS_PER_ENTITY = 1_000
MAX_COMMENT_DEPTH = 20
MIN_REQUEST_INTERVAL_SECONDS = 2.0
MAX_ATTACHMENT_BYTES = DEFAULT_MAX_DOWNLOAD_BYTES
MAX_ATTACHMENTS_PER_ENTITY = 100
MAX_ATTACHMENT_HOSTS = 20

_CORE_ID_ROUTE = re.compile(
    r"/core/v1/(?:posts|sponds)/[A-Za-z0-9._~-]{1,512}\Z"
)
_COMMENTS_ROUTE = re.compile(
    r"/core/v1/(?:posts|sponds)/[A-Za-z0-9._~-]{1,512}/comments\Z"
)
_GROUP_TOKEN_ROUTE = re.compile(
    r"/core/v1/group/[A-Za-z0-9._~-]{1,512}/filesToken\Z"
)
_GROUP_STORAGE_ROUTE = re.compile(
    r"/storage/upload/group/[A-Za-z0-9._~-]{1,512}\Z"
)
_IMAGE_STORAGE_ROUTE = re.compile(
    r"/images/([0-9a-f]{3})/([0-9a-f]{32})\.(jpeg|png)\Z"
)
# Token-free post media observed and downloaded on 2026-10-15. This is
# deliberately separate from the authenticated group-file resolver above.
_POST_UPLOAD_ROUTE = re.compile(
    r"/storage/upload/([A-F0-9]{32})/[0-9]{10}_[A-F0-9]{8}/file_[0-9a-f]{32}\.png\Z"
)


class CollectorTransport(Protocol):
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
    ) -> DownloadResult | Any: ...



@dataclass
class RequestBudget:
    limit: int = MAX_REQUESTS_PER_RUN
    used: int = 0

    def consume(self) -> None:
        if self.used >= self.limit:
            raise SourceUnavailable("Spond overskred den faste forespørselsgrensen")
        self.used += 1


# Observed from the authenticated group-file resolver on 2026-09-30. This is
# allowed only for resolver output, not arbitrary source-supplied media URLs.
GROUP_FILE_STORAGE_HOST = 'spond-secure-attachments.s3.eu-west-1.amazonaws.com'
# Fresh post attachment metadata and a token-free GET verified this exact image
# route on 2026-10-07. Other spond.com paths and image formats are not admitted.
IMAGE_STORAGE_HOST = 'spond.com'


def _default_transport(config: ClientConfig) -> ApiTransport:
    hosts = {"api.spond.com", GROUP_FILE_STORAGE_HOST, IMAGE_STORAGE_HOST, *config.attachment_hosts}
    return ApiTransport(
        allowed_hosts=hosts,
        timeout=45,
        max_json_bytes=25 * 1024 * 1024,
        max_download_bytes=MAX_ATTACHMENT_BYTES,
        user_agent=provider_user_agent(config),
    )


def _normalized_host(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise SourceUnavailable("Spond-adapteren avviste en ugyldig URL") from None
    return host


def _query_mapping(parsed: urllib.parse.SplitResult) -> dict[str, str]:
    try:
        pairs = urllib.parse.parse_qsl(
            parsed.query, keep_blank_values=True, strict_parsing=True
        )
    except ValueError:
        raise SourceUnavailable("Spond-adapteren avviste ugyldige URL-parametere") from None
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise SourceUnavailable("Spond-adapteren avviste dupliserte URL-parametere")
    return dict(pairs)


def _safe_id_cursor(value: str | None) -> bool:
    return (
        value is None
        or re.fullmatch(r"[A-Za-z0-9._~-]{1,512}", value) is not None
    )


def _safe_time_cursor(value: str | None) -> bool:
    return value is None or (
        0 < len(value) <= 128 and parse_time(value) is not None
    )


def _guard_api_get(url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        raise SourceUnavailable("Spond-adapteren avviste en ugyldig API-rute") from None
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or _normalized_host(url) != "api.spond.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or urllib.parse.unquote(parsed.path) != parsed.path
    ):
        raise SourceUnavailable("Spond-adapteren avviste en ukjent API-rute")
    query = _query_mapping(parsed)
    if _COMMENTS_ROUTE.fullmatch(parsed.path) and not query:
        return
    if parsed.path in {"/core/v1/profile", "/core/v1/groups"}:
        if query:
            raise SourceUnavailable("Spond-adapteren avviste ukjente API-parametere")
        return
    if parsed.path == "/core/v1/posts":
        required = {
            "type",
            "includeComments",
            "includeReadStatus",
            "includeSeenCount",
            "max",
        }
        if (
            not required.issubset(query)
            or not set(query).issubset(required | {"prevId", "maxTimestamp"})
            or query["type"] not in POST_TYPES
            or query["includeComments"] != "true"
            or query["includeReadStatus"] != "false"
            or query["includeSeenCount"] != "false"
            or query["max"] != str(PAGE_SIZE)
            or not _safe_id_cursor(query.get("prevId"))
            or not _safe_time_cursor(query.get("maxTimestamp"))
            or (("prevId" in query) != ("maxTimestamp" in query))
        ):
            raise SourceUnavailable("Spond-adapteren avviste ukjente API-parametere")
        return
    if parsed.path == "/core/v1/sponds":
        required = {
            "includeComments",
            "includeHidden",
            "addProfileInfo",
            "scheduled",
            "order",
            "max",
        }
        direction = query.get("order")
        boundary_name = "minEndTimestamp" if direction == "asc" else "maxEndTimestamp"
        cursor_name = "minStartTimestamp" if direction == "asc" else "maxStartTimestamp"
        if (
            not required.issubset(query)
            or direction not in {"asc", "desc"}
            or not set(query).issubset(required | {boundary_name, "prevId", cursor_name})
            or query["includeComments"] != "true"
            or query["includeHidden"] != "false"
            or query["addProfileInfo"] != "true"
            or query["scheduled"] != "true"
            or query["max"] != str(PAGE_SIZE)
            or boundary_name not in query
            or not _safe_time_cursor(query.get(boundary_name))
            or not _safe_id_cursor(query.get("prevId"))
            or not _safe_time_cursor(query.get(cursor_name))
            or (("prevId" in query) != (cursor_name in query))
        ):
            raise SourceUnavailable("Spond-adapteren avviste ukjente API-parametere")
        return
    if parsed.path == "/core/v1/activities/summary":
        locale = query.get("lang")
        if set(query) not in (set(), {"lang"}) or (
            locale is not None
            and (
                not locale
                or len(locale) > 35
                or re.fullmatch(r"[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*", locale)
                is None
            )
        ):
            raise SourceUnavailable("Spond-adapteren avviste ukjente API-parametere")
        return
    if _CORE_ID_ROUTE.fullmatch(parsed.path):
        is_post = parsed.path.startswith("/core/v1/posts/")
        expected = (
            {
                "includeComments": "true",
                "includeReadStatus": "false",
                "includeSeenCount": "false",
                "addGroupInfo": "true",
            }
            if is_post
            else {"includeComments": "true", "addProfileInfo": "true"}
        )
        if query != expected:
            raise SourceUnavailable("Spond-adapteren avviste ukjente API-parametere")
        return
    if _GROUP_TOKEN_ROUTE.fullmatch(parsed.path) and not query:
        return
    if (
        _GROUP_STORAGE_ROUTE.fullmatch(parsed.path)
        and set(query) == {"auth"}
        and 0 < len(query["auth"]) <= 64 * 1024
        and all(ord(character) >= 33 and ord(character) != 127 for character in query["auth"])
    ):
        return
    raise SourceUnavailable("Spond-adapteren avviste en ikke-lesende API-rute")


UNSUPPORTED_ATTACHMENT_ACTION = (
    'Behandle det som er kjent fra kilden som ufullstendig; '
    'ikke gjenta samme vedleggsoppslag uten nytt grunnlag.')


def _unsupported_attachment(message, reason='unsupported_attachment_route'):
    diagnostic = {'reason': reason, 'next_action': UNSUPPORTED_ATTACHMENT_ACTION}
    error = SourceUnavailable(message + '\n' + json.dumps(diagnostic, ensure_ascii=False))
    error.diagnostic = diagnostic
    return error


def _guard_external_attachment(url: str, allowed_hosts: frozenset[str]) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = _normalized_host(url)
    except ValueError:
        raise SourceUnavailable("Spond-vedlegget har ugyldig URL") from None
    decoded = urllib.parse.unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or host not in allowed_hosts
        or not parsed.path.startswith("/")
        or any(segment in {".", ".."} for segment in decoded.split("/"))
    ):
        raise _unsupported_attachment(
            "Spond-vedlegget peker utenfor den smale vedleggsvertslisten"
        )
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise SourceUnavailable("Spond-vedlegget peker til en privat adresse")


class AuthorizedClient:
    def __init__(
        self,
        root: Path,
        *,
        transport: CollectorTransport | None,
        auth_transport: AuthTransport | None,
        budget: RequestBudget,
        wall_time: Callable[[], float],
        pacing_clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self.root = root
        self.auth_transport = auth_transport
        self.budget = budget
        self.wall_time = wall_time
        self.pacing_clock = pacing_clock
        self.sleep = sleep
        self.token, self._refreshed = access_token_for_use(
            root,
            now=self.wall_time(),
            transport=auth_transport,
            before_request=self._before_request,
        )
        session = local_session_for_use(root, at=self.wall_time())
        config = load_client_config(root)
        if (
            session.get("api_root") != config.api_root
            or session.get("app_version") != config.app_version
            or session.get("attachment_hosts") != list(config.attachment_hosts)
            or len(config.attachment_hosts) > MAX_ATTACHMENT_HOSTS
        ):
            raise SourceUnavailable("Spond-providerprofilens runtimebinding er ugyldig")
        self.api_root = config.api_root
        self.app_version = config.app_version
        self.attachment_hosts = frozenset(config.attachment_hosts)
        self.transport = transport or _default_transport(config)
        self._group_file_tokens: dict[str, str] = {}

    def _before_request(self) -> None:
        assert_network_allowed(self.root, at=self.wall_time())
        self.budget.consume()
        pace_network_request(
            self.root,
            clock=self.pacing_clock,
            sleep=self.sleep,
            minimum_interval=MIN_REQUEST_INTERVAL_SECONDS,
        )
        from .request_budget import reserve
        reserve(self.root, 'spond', at=self.wall_time())

    def _record_provider_stop(self, exc: ApiHTTPError) -> None:
        if exc.status_code == 429:
            record_cooldown(
                self.root,
                429,
                retry_after_seconds=exc.retry_after_seconds,
                at=self.wall_time(),
            )
            raise SpondCooldownActive(
                "Spond avviste videre trafikk midlertidig"
            ) from None
        if 500 <= exc.status_code <= 599:
            record_cooldown(self.root, exc.status_code, at=self.wall_time())
            raise SpondCooldownActive("Spond er midlertidig utilgjengelig") from None

    def _get_once(self, url: str) -> Any:
        _guard_api_get(url)
        self._before_request()
        return self.transport.get_json(url, bearer_token=self.token)

    def get_json(self, url: str) -> Any:
        _guard_api_get(url)
        if urllib.parse.urlsplit(url).path.endswith('/filesToken'):
            return self._get_json(url)
        from .sync_snapshot import memo_read

        return memo_read(self.root, SOURCE, ('GET', url), lambda: self._get_json(url))

    def _get_json(self, url: str) -> Any:
        try:
            return self._get_once(url)
        except ApiHTTPError as exc:
            self._record_provider_stop(exc)
            if exc.status_code not in {401, 403}:
                raise SourceUnavailable("Spond avviste en lesende forespørsel") from None
            if self._refreshed:
                if exc.status_code == 403:
                    raise SourceUnavailable("Spond avviste tilgang til denne ressursen") from None
                raise SourceLoginRequired("Spond krever ny innlogging") from None
        except (ApiTransportError, OSError, TimeoutError):
            record_cooldown(self.root, 503, at=self.wall_time())
            raise SpondCooldownActive("Spond-nettverket er midlertidig utilgjengelig") from None
        refresh_session(
            self.root,
            transport=self.auth_transport,
            at=self.wall_time(),
            before_request=self._before_request,
        )
        self.token = str(
            local_session_for_use(self.root, at=self.wall_time())["access_token"]
        )
        self._refreshed = True
        try:
            return self._get_once(url)
        except ApiHTTPError as exc:
            self._record_provider_stop(exc)
            if exc.status_code == 401:
                raise SourceLoginRequired("Spond krever ny innlogging") from None
            if exc.status_code == 403:
                raise SourceUnavailable("Spond avviste tilgang til denne ressursen") from None
            raise SourceUnavailable("Spond avviste en lesende forespørsel") from None
        except (ApiTransportError, OSError, TimeoutError):
            record_cooldown(self.root, 503, at=self.wall_time())
            raise SpondCooldownActive("Spond-nettverket er midlertidig utilgjengelig") from None

    def _group_file_url(self, group_id: str, resource_id: str) -> str:
        group = urllib.parse.quote(group_id, safe="")
        resource = urllib.parse.quote(resource_id, safe="")
        if group_id != group or resource_id != resource:
            raise SourceUnavailable("Spond-vedlegget har ugyldig gruppe- eller fil-ID")
        token = self._group_file_tokens.get(group_id)
        if token is None:
            response = self.get_json(f"{API_ROOT}/group/{group}/filesToken")
            if not isinstance(response, Mapping):
                raise SourceUnavailable("Spond returnerte ugyldig filtoken")
            value = response.get("value")
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 64 * 1024
                or any(ord(character) < 33 or ord(character) == 127 for character in value)
            ):
                raise SourceUnavailable("Spond returnerte ugyldig filtoken")
            token = value
            self._group_file_tokens[group_id] = token
        resolver = (
            f"{BASE_API_URL}/storage/upload/group/{resource}?"
            + urllib.parse.urlencode({"auth": token})
        )
        response = self.get_json(resolver)
        if not isinstance(response, Mapping) or not isinstance(response.get("url"), str):
            raise SourceUnavailable("Spond returnerte ugyldig vedleggslenke")
        return str(response["url"])

    def download(
        self,
        url: str,
        target: Path,
        *,
        group_id: str | None,
        max_bytes: int,
        owner_id: str | None = None,
    ) -> DownloadResult | Any:
        if not isinstance(url, str) or not url:
            raise SourceUnavailable("Spond-vedlegget har ugyldig URL")
        try:
            parsed = urllib.parse.urlsplit(url)
        except (TypeError, ValueError):
            raise SourceUnavailable("Spond-vedlegget har ugyldig URL") from None
        decoded_path = urllib.parse.unquote(parsed.path)
        allowed_hosts = self.attachment_hosts
        content_types = None
        if "/storage/upload/group/" in decoded_path and "postcode" not in decoded_path:
            _guard_external_attachment(url, frozenset({"api.spond.com", "spond.com"}))
            if not _GROUP_STORAGE_ROUTE.fullmatch(parsed.path) or parsed.query:
                raise SourceUnavailable("Spond-gruppevedlegget har ugyldig filrute")
            resource_id = parsed.path.rsplit("/", 1)[-1]
            if not isinstance(group_id, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,512}", group_id) or group_id in {".", ".."}:
                raise SourceUnavailable(
                    "Spond-gruppevedlegget mangler en sikker gruppekobling"
                )
            url = self._group_file_url(group_id, resource_id)
            allowed_hosts = self.attachment_hosts | {GROUP_FILE_STORAGE_HOST}
        elif _normalized_host(url) == IMAGE_STORAGE_HOST and decoded_path.startswith("/images/"):
            _guard_external_attachment(url, frozenset({IMAGE_STORAGE_HOST}))
            match = _IMAGE_STORAGE_ROUTE.fullmatch(parsed.path)
            if not match or match[1] != match[2][:3] or parsed.query:
                raise _unsupported_attachment("Spond-bildevedlegget har en filrute som ikke støttes")
            if not isinstance(group_id, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,512}", group_id) or group_id in {".", ".."}:
                raise SourceUnavailable("Spond-bildevedlegget mangler en sikker gruppekobling")
            allowed_hosts = frozenset({IMAGE_STORAGE_HOST})
            # Spond serves some .png image URLs as JPEG (verified 2026-10-15).
            content_types = {"image/jpeg"} if match[3] == 'jpeg' else {"image/png", "image/jpeg"}
        elif _normalized_host(url) == IMAGE_STORAGE_HOST and decoded_path.startswith('/storage/upload/'):
            _guard_external_attachment(url, frozenset({IMAGE_STORAGE_HOST}))
            match = _POST_UPLOAD_ROUTE.fullmatch(parsed.path)
            if not match or parsed.query:
                raise _unsupported_attachment('Spond-opplastingen har en filrute som ikke støttes')
            if match[1] != owner_id:
                raise _unsupported_attachment('Spond-opplastingen mangler bekreftet eierkobling')
            if not isinstance(group_id, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{1,512}', group_id) or group_id in {'.', '..'}:
                raise SourceUnavailable('Spond-opplastingen mangler en sikker gruppekobling')
            allowed_hosts = frozenset({IMAGE_STORAGE_HOST})
            content_types = {'image/png'}
        _guard_external_attachment(url, allowed_hosts)
        self._before_request()
        try:
            # External signed URLs are deliberately token-free.  The Spond
            # bearer is only ever sent by get_json to exact api.spond.com paths.
            return self.transport.download(
                url,
                target,
                bearer_token=None,
                headers=None,
                max_bytes=max_bytes,
                **({"allowed_content_types": content_types} if content_types else {}),
            )
        except ApiHTTPError as exc:
            self._record_provider_stop(exc)
            raise SourceUnavailable("Spond-vedlegget kunne ikke lastes ned") from None
        except ApiContentRejected:
            raise _unsupported_attachment('Spond-vedlegget har en ikke-støttet MIME-type',
                reason='unsupported_attachment_content') from None
        except (ApiTransportError, OSError, TimeoutError):
            record_cooldown(self.root, 503, at=self.wall_time())
            raise SpondCooldownActive("Spond-nettverket er midlertidig utilgjengelig") from None




def pick(mapping: Mapping[str, Any] | None, *fields: str) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    for field_name in fields:
        value = mapping.get(field_name)
        if value not in (None, ""):
            return value
    return None


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        if numeric > 100_000_000_000:
            numeric /= 1000
        try:
            return datetime.fromtimestamp(numeric, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _iso(value: Any) -> str | None:
    parsed = parse_time(value)
    return parsed.isoformat() if parsed is not None else None


def _comment_values(entity: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for field_name in ("comments", "commentModels"):
        if field_name not in entity:
            continue
        raw = entity[field_name]
        if not isinstance(raw, list) or any(
            not isinstance(item, Mapping) for item in raw
        ):
            raise SourceUnavailable("Spond returnerte ugyldig kommentarstruktur")
        result.extend(raw)
    return result


def _comment_children(comment: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for field_name in ("children", "replies"):
        if field_name not in comment:
            continue
        raw = comment[field_name]
        if not isinstance(raw, list) or any(
            not isinstance(item, Mapping) for item in raw
        ):
            raise SourceUnavailable("Spond returnerte ugyldig svarstruktur")
        result.extend(raw)
    return result


def _all_comments(entity: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    stack: list[tuple[Mapping[str, Any], int]] = [
        (comment, 0) for comment in reversed(_comment_values(entity))
    ]
    while stack:
        comment, depth = stack.pop()
        if depth > MAX_COMMENT_DEPTH:
            raise SourceUnavailable("Spond-kommentartråden er for dypt nestet")
        result.append(comment)
        if len(result) > MAX_COMMENTS_PER_ENTITY:
            raise SourceUnavailable("Spond returnerte for mange kommentarer i én tråd")
        stack.extend(
            (child, depth + 1) for child in reversed(_comment_children(comment))
        )
    return result


def _known_time_values(
    value: Mapping[str, Any],
    fields: Iterable[str],
    *,
    label: str,
    require_one: bool = False,
) -> list[datetime]:
    result: list[datetime] = []
    for field_name in fields:
        if field_name not in value or value[field_name] in (None, ""):
            continue
        parsed = parse_time(value[field_name])
        if parsed is None:
            raise SourceUnavailable(f"Spond-{label} har ugyldig tidsstempel")
        result.append(parsed)
    if require_one and not result:
        raise SourceUnavailable(f"Spond-{label} mangler tidsstempel")
    return result


def timestamp_values(entity: Mapping[str, Any]) -> list[datetime]:
    fields = (
        "timestamp",
        "createdTime",
        "createdAt",
        "publishedAt",
        "updatedAt",
        "modifiedAt",
        "lastModified",
        "changedAt",
        "inviteTime",
        # Created-time pagination alone is insufficient: an old event can be
        # upcoming, and an old thread can contain a new comment.
        "startTimestamp",
        "startTime",
        "endTimestamp",
        "endTime",
        "meetupTimestamp",
        "rsvpDate",
        "responseDeadline",
        "dueBy",
    )
    result = _known_time_values(entity, fields, label="saken")
    for nested_name in ("poll", "payment", "clubPayment"):
        nested = _mapping(entity.get(nested_name))
        if nested is None:
            continue
        result.extend(
            _known_time_values(
                nested,
                ("dueBy", "dueDate", "deadline"),
                label="fristen",
            )
        )
    for comment in _all_comments(entity):
        result.extend(
            _known_time_values(
                comment,
                fields,
                label="kommentaren",
                require_one=True,
            )
        )
    return result


def _published_at(entity: Mapping[str, Any]) -> str | None:
    for field_name in ("timestamp", "createdTime", "createdAt", "publishedAt"):
        value = _iso(entity.get(field_name))
        if value is not None:
            return value
    return None


def _changed_at(
    entity: Mapping[str, Any], notification_time: datetime | None = None
) -> str | None:
    values: list[datetime] = []
    for field_name in ("updatedAt", "modifiedAt", "lastModified", "changedAt"):
        parsed = parse_time(entity.get(field_name))
        if parsed is not None:
            values.append(parsed)
    for comment in _all_comments(entity):
        for field_name in ("timestamp", "createdTime", "createdAt", "updatedAt"):
            parsed = parse_time(comment.get(field_name))
            if parsed is not None:
                values.append(parsed)
    if notification_time is not None:
        values.append(notification_time)
    return max(values).isoformat() if values else None


def identifier(entity: Mapping[str, Any], label: str) -> str:
    value = pick(entity, "id", "postId", "spondId")
    if value in (None, ""):
        raise SourceUnavailable(f"Spond-{label} mangler stabil ID")
    result = str(value)
    if len(result) > 512 or any(ord(character) < 33 for character in result):
        raise SourceUnavailable(f"Spond-{label} har ugyldig ID")
    return result


def validated_post_payload(
    post: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any] | None]:
    raw_type = post.get("type")
    if not isinstance(raw_type, str):
        raise SourceUnavailable("Spond-innlegget mangler gyldig type")
    post_type = raw_type.upper()
    expected_field = {
        "PLAIN": None,
        "POLL": "poll",
        "PAYMENT": "payment",
        "CLUB_PAYMENT": "clubPayment",
    }.get(post_type)
    if post_type not in {"PLAIN", "POLL", "PAYMENT", "CLUB_PAYMENT"}:
        raise SourceUnavailable("Spond-innlegget har ukjent type")
    if expected_field is None:
        return post_type, None
    nested = post.get(expected_field)
    if not isinstance(nested, Mapping):
        raise SourceUnavailable(
            "Spond-innleggets avstemnings- eller betalingsdata er ugyldige"
        )
    return post_type, nested


def _response_list(value: Any, label: str) -> list[Mapping[str, Any]]:
    candidate = value
    if isinstance(value, Mapping):
        for field_name in ("items", "data", "results", "sponds", "posts"):
            if isinstance(value.get(field_name), list):
                candidate = value[field_name]
                break
    if not isinstance(candidate, list) or any(
        not isinstance(item, Mapping) for item in candidate
    ):
        raise SourceUnavailable(f"Spond returnerte ugyldig {label}")
    return list(candidate)


def url_with_query(endpoint: str, query: Mapping[str, Any] | None = None) -> str:
    if not query:
        return endpoint
    fields = [(str(key), str(value).lower() if isinstance(value, bool) else str(value)) for key, value in query.items()]
    return f"{endpoint}?{urllib.parse.urlencode(fields)}"


def posts_page(client, post_type, cursor=None):
    """One verified post page; its marker is safe for durable continuation."""
    query = {"type": post_type, "includeComments": True,
             "includeReadStatus": False, "includeSeenCount": False, "max": PAGE_SIZE}
    if cursor is not None:
        query.update(prevId=cursor[0], maxTimestamp=cursor[1])
    page = _response_list(client.get_json(url_with_query(POSTS_ENDPOINT, query)), "innleggsliste")
    if len(page) > PAGE_SIZE:
        raise SourceUnavailable("Spond returnerte en for stor innleggsside")
    following = None
    if len(page) == PAGE_SIZE:
        ident = identifier(page[-1], "innleggssiden")
        stamp = pick(page[-1], "timestamp", "createdTime", "createdAt")
        if (not isinstance(stamp, (str, int, float)) or not _safe_id_cursor(ident)
                or not _safe_time_cursor(str(stamp))):
            raise SourceUnavailable("Spond-innleggssiden mangler sikker markør")
        following = (ident, str(stamp))
    return page, following


def events_page(client, direction, boundary, cursor=None):
    """One verified event page, using a fixed day boundary across resumptions."""
    boundary_name = "minEndTimestamp" if direction == "asc" else "maxEndTimestamp"
    cursor_name = "minStartTimestamp" if direction == "asc" else "maxStartTimestamp"
    query = {"includeComments": True, "includeHidden": False, "addProfileInfo": True,
             "scheduled": True, "order": direction, "max": PAGE_SIZE,
             boundary_name: boundary}
    if cursor is not None:
        query.update(prevId=cursor[0])
        query[cursor_name] = cursor[1]
    page = _response_list(client.get_json(url_with_query(EVENTS_BY_CREATED_ENDPOINT, query)), "arrangementsliste")
    if len(page) > PAGE_SIZE:
        raise SourceUnavailable("Spond returnerte en for stor arrangementsside")
    following = None
    if len(page) == PAGE_SIZE:
        ident = identifier(page[-1], "arrangementssiden")
        stamp = pick(page[-1], "meetupTimestamp", "startTimestamp")
        if (not isinstance(stamp, str) or not _safe_id_cursor(ident)
                or not _safe_time_cursor(stamp)):
            raise SourceUnavailable("Spond-arrangementssiden mangler sikker markør")
        following = (ident, stamp)
    return page, following


def fetch_posts(
    client: AuthorizedClient, cutoff: datetime
) -> tuple[dict[str, Mapping[str, Any]], int]:
    selected: dict[str, Mapping[str, Any]] = {}
    failures = 0
    for post_type in POST_TYPES:
        cursor = None
        seen_cursors: set[tuple[str | None, str | None]] = set()
        for _page_number in range(MAX_PAGES_PER_STREAM):
            page, following = posts_page(client, post_type, cursor)
            for post in page:
                try:
                    post_id = identifier(post, "innlegget")
                    validated_post_payload(post)
                    timestamps = timestamp_values(post)
                    if not timestamps:
                        failures += 1
                    elif max(timestamps) >= cutoff:
                        selected[post_id] = post
                except (FamiliekilderError, ValueError):
                    failures += 1
            # The endpoint is ordered by creation time, not change/comment
            # time.  A later page can therefore still contain an in-window
            # edit or comment; only the provider's short page is a safe end.
            if following is None:
                break
            if following in seen_cursors:
                raise SourceUnavailable("Spond gjentok samme innleggsside")
            seen_cursors.add(following)
            cursor = following
        else:
            raise SourceUnavailable("Spond returnerte for mange innleggssider")
    return selected, failures


def fetch_events(
    client: AuthorizedClient, cutoff: datetime
) -> tuple[dict[str, Mapping[str, Any]], int]:
    selected: dict[str, Mapping[str, Any]] = {}
    failures = 0
    boundary = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    streams = (
        ("asc", (boundary + timedelta(milliseconds=1)).isoformat()),
        ("desc", boundary.isoformat()),
    )
    for direction, boundary_value in streams:
        cursor = None
        seen_cursors: set[tuple[str, str]] = set()
        for _page_number in range(MAX_PAGES_PER_STREAM):
            page, following = events_page(client, direction, boundary_value, cursor)
            for event in page:
                try:
                    event_id = identifier(event, "arrangementet")
                    timestamps = timestamp_values(event)
                    if not timestamps:
                        failures += 1
                    elif max(timestamps) >= cutoff:
                        selected[event_id] = event
                except (FamiliekilderError, ValueError):
                    failures += 1
            # Start-time order says nothing about later edits/comments, so the
            # collector continues to the documented end or its hard cap.
            if following is None:
                break
            if following in seen_cursors:
                raise SourceUnavailable("Spond gjentok samme arrangementsside")
            seen_cursors.add(following)
            cursor = following
        else:
            raise SourceUnavailable("Spond returnerte for mange arrangementssider")
    return selected, failures


def _is_chat_notification(value: Mapping[str, Any]) -> bool:
    if pick(value, "chatId", "threadId") not in (None, ""):
        raw_type = " ".join(
            str(pick(value, field_name) or "")
            for field_name in ("key", "type", "activityType")
        ).lower()
        if "chat" in raw_type or value.get("chatId") not in (None, ""):
            return True
    return any(
        "chat" in str(pick(value, field_name) or "").lower()
        for field_name in ("key", "type", "activityType")
    )


def fetch_notifications(
    client: AuthorizedClient, locale: str | None, cutoff: datetime
) -> tuple[list[Mapping[str, Any]], int]:
    # The first-party client performs this GET without a provider-side read
    # mutation; its activity-panel last-read timestamp is only localStorage.
    query = {"lang": locale} if locale else None
    values = _response_list(
        client.get_json(url_with_query(ACTIVITIES_ENDPOINT, query)), "aktivitetsvarsler"
    )
    if len(values) > MAX_NOTIFICATIONS:
        raise SourceUnavailable("Spond returnerte for mange aktivitetsvarsler")
    result: list[Mapping[str, Any]] = []
    failures = 0
    for value in values:
        if _is_chat_notification(value):
            continue
        timestamp = parse_time(pick(value, "timestamp", "createdAt", "updatedAt"))
        if timestamp is None:
            failures += 1
        elif timestamp >= cutoff:
            result.append(value)
    return result, failures


def fetch_notification_details(
    client: AuthorizedClient,
    notifications: list[Mapping[str, Any]],
    posts: dict[str, Mapping[str, Any]],
    events: dict[str, Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], datetime], int]:
    touched: dict[tuple[str, str], datetime] = {}
    failures = 0
    lookups = 0
    for notification in notifications:
        timestamp = parse_time(
            pick(notification, "timestamp", "createdAt", "updatedAt")
        )
        if timestamp is None:
            failures += 1
            continue
        for kind, field_name, current in (
            ("post", "postId", posts),
            ("event", "spondId", events),
        ):
            raw_id = notification.get(field_name)
            if raw_id in (None, ""):
                continue
            entity_id = str(raw_id)
            key = (kind, entity_id)
            previous = touched.get(key)
            touched[key] = max(previous, timestamp) if previous is not None else timestamp
            if entity_id in current:
                continue
            lookups += 1
            if lookups > MAX_NOTIFICATION_DETAILS:
                raise SourceUnavailable(
                    "Spond overskred grensen for varseldrevne detaljoppslag"
                )
            quoted = urllib.parse.quote(entity_id, safe="")
            if quoted != entity_id:
                failures += 1
                continue
            if kind == "post":
                url = url_with_query(
                    f"{POSTS_ENDPOINT}/{quoted}",
                    {
                        "includeComments": True,
                        "includeReadStatus": False,
                        "includeSeenCount": False,
                        "addGroupInfo": True,
                    },
                )
            else:
                url = url_with_query(
                    f"{API_ROOT}/sponds/{quoted}",
                    {"includeComments": True, "addProfileInfo": True},
                )
            try:
                detail = client.get_json(url)
                if not isinstance(detail, Mapping):
                    raise SourceUnavailable("Spond returnerte ugyldige detaljdata")
                returned_id = identifier(
                    detail,
                    "detaljinnlegget" if kind == "post" else "detaljarrangementet",
                )
                if returned_id != entity_id:
                    raise SourceUnavailable(
                        "Spond-detaljsvaret samsvarer ikke med varslet sak"
                    )
                if kind == "post":
                    validated_post_payload(detail)
                if not timestamp_values(detail):
                    raise SourceUnavailable("Spond-detaljsvaret mangler tidsstempel")
                current[entity_id] = detail
            except (SpondCooldownActive, SourceLoginRequired):
                raise
            except (ApiTransportError, FamiliekilderError, OSError, ValueError):
                failures += 1
    return touched, failures


def person_name(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    direct = pick(value, "displayName", "name", "fullName")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts = [pick(value, "firstName", "givenName"), pick(value, "lastName", "familyName")]
    result = " ".join(
        str(part).strip() for part in parts if isinstance(part, str) and part.strip()
    )
    return result or None


def group_list(value: Any) -> list[Mapping[str, Any]]:
    return _response_list(value, "gruppeliste")


def profile_names(groups: list[Mapping[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for group in groups:
        for member in _as_list(group.get("members")):
            if not isinstance(member, Mapping):
                continue
            for person in [member, _mapping(member.get("profile"))]:
                if not isinstance(person, Mapping):
                    continue
                person_id = pick(person, "id", "profileId")
                name = person_name(person)
                if person_id not in (None, "") and name:
                    result[str(person_id)] = name
            for guardian in _as_list(member.get("guardians")):
                if not isinstance(guardian, Mapping):
                    continue
                candidates = [guardian, _mapping(guardian.get("profile"))]
                for person in candidates:
                    if not isinstance(person, Mapping):
                        continue
                    person_id = pick(person, "id", "profileId")
                    name = person_name(person)
                    if person_id not in (None, "") and name:
                        result[str(person_id)] = name
    return result


def guardian_matches(guardian: Mapping[str, Any], profile_id: str) -> bool:
    candidates = [
        pick(guardian, "profileId", "id"),
        pick(_mapping(guardian.get("profile")), "id", "profileId"),
    ]
    return any(str(candidate) == profile_id for candidate in candidates if candidate not in (None, ""))


def group_children(
    groups: list[Mapping[str, Any]], profile_id: str
) -> tuple[dict[str, str], dict[str, str]]:
    children: dict[str, str] = {}
    group_names: dict[str, str] = {}
    for group in groups:
        group_id = pick(group, "id", "groupId")
        if group_id in (None, ""):
            continue
        group_id = str(group_id)
        name = person_name(group) or html_to_text(pick(group, "name", "title"))
        if name:
            group_names[group_id] = name
        names: list[str] = []
        for member in _as_list(group.get("members")):
            if not isinstance(member, Mapping):
                continue
            guardians = [
                guardian
                for guardian in _as_list(member.get("guardians"))
                if isinstance(guardian, Mapping)
            ]
            if any(guardian_matches(guardian, profile_id) for guardian in guardians):
                child_name = person_name(member)
                if child_name and child_name not in names:
                    names.append(child_name)
        if names:
            children[group_id] = ", ".join(names)
    return children, group_names


def entity_group_id(entity: Mapping[str, Any]) -> str | None:
    direct = pick(entity, "groupId")
    if direct not in (None, ""):
        return str(direct)
    group = _mapping(entity.get("group"))
    recipients = _mapping(entity.get("recipients"))
    recipient_group = _mapping(recipients.get("group")) if recipients else None
    value = pick(group, "id", "groupId") or pick(recipient_group, "id", "groupId")
    return str(value) if value not in (None, "") else None


def _sender(entity: Mapping[str, Any], profiles: Mapping[str, str]) -> str | None:
    for field_name in ("creator", "owner", "sender", "fromProfile"):
        name = person_name(entity.get(field_name))
        if name:
            return name
    direct = pick(entity, "creatorName", "ownerName", "senderName", "fromName")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for field_name in ("creatorId", "ownerId", "fromProfileId", "profileId"):
        value = entity.get(field_name)
        if value not in (None, "") and str(value) in profiles:
            return profiles[str(value)]
    owners = _as_list(entity.get("owners"))
    if owners:
        first = owners[0]
        if isinstance(first, Mapping):
            return person_name(first) or profiles.get(str(pick(first, "id") or ""))
        return profiles.get(str(first))
    return None


def _comment_body(entity: Mapping[str, Any], profiles: Mapping[str, str]) -> str:
    lines: list[str] = []
    for comment in _all_comments(entity):
        text = html_to_text(pick(comment, "text", "body", "message", "description"))
        if not text:
            continue
        author = _sender(comment, profiles) or "Ukjent avsender"
        timestamp = _iso(pick(comment, "timestamp", "createdTime", "createdAt", "updatedAt"))
        label = f"Kommentar – {author}"
        if timestamp:
            label += f" ({timestamp})"
        lines.append(f"{label}:\n{text}")
    return "\n\n".join(lines)


def _location(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, Mapping):
        return None
    values: list[str] = []
    for field_name in ("name", "address", "description"):
        part = value.get(field_name)
        if isinstance(part, str) and part.strip() and part.strip() not in values:
            values.append(part.strip())
    return ", ".join(values) or None


def _requires_response(entity: Mapping[str, Any]) -> bool:
    for field_name in ("unanswered", "responseRequired", "actionRequired"):
        if entity.get(field_name) is True:
            return True
    response = pick(entity, "response", "responseStatus", "status")
    return isinstance(response, str) and response.upper() in {
        "UNANSWERED",
        "PENDING",
        "RESPONSE_REQUIRED",
        "UNPAID",
    }


def _post_text(post: Mapping[str, Any], profiles: Mapping[str, str]) -> str:
    post_type, nested = validated_post_payload(post)
    parts: list[str] = []
    if post_type == "POLL":
        poll = nested or {}
        description = html_to_text(pick(poll, "description", "body"))
        if description:
            parts.append(description)
        options: list[str] = []
        for option in _as_list(poll.get("options")):
            if isinstance(option, Mapping):
                text = html_to_text(pick(option, "text", "name")) or _iso(option.get("date"))
                if text:
                    options.append(text)
        if options:
            parts.append("Alternativer:\n" + "\n".join(f"- {item}" for item in options))
    elif post_type in {"PAYMENT", "CLUB_PAYMENT"}:
        payment = nested or {}
        description = html_to_text(pick(payment, "description", "body"))
        if description:
            parts.append(description)
    else:
        body = html_to_text(pick(post, "body", "description", "text"))
        if body:
            parts.append(body)
    comments = _comment_body(post, profiles)
    if comments:
        parts.append(comments)
    return "\n\n".join(parts) or "Spond-innlegg uten tekstinnhold"


def _post_title(post: Mapping[str, Any]) -> str:
    post_type, nested = validated_post_payload(post)
    if post_type == "POLL":
        value = pick(nested, "question", "title")
    elif post_type in {"PAYMENT", "CLUB_PAYMENT"}:
        value = pick(nested, "title", "name")
    else:
        value = pick(post, "title", "heading", "subject")
    return html_to_text(value) or "Spond-innlegg"


def _post_deadline(post: Mapping[str, Any]) -> str | None:
    post_type, nested = validated_post_payload(post)
    if post_type == "POLL":
        return _iso(pick(nested, "dueBy", "deadline"))
    return _iso(pick(nested, "dueBy", "dueDate", "deadline"))


def normalize_post(
    post: Mapping[str, Any],
    *,
    profiles: Mapping[str, str],
    children: Mapping[str, str],
    group_names: Mapping[str, str],
    attachments: list[dict[str, Any]],
    notification_time: datetime | None,
) -> dict[str, Any]:
    post_id = identifier(post, "innlegget")
    post_type, _nested = validated_post_payload(post)
    group_id = entity_group_id(post)
    item_type = "payment" if post_type in {"PAYMENT", "CLUB_PAYMENT"} else "message" if post_type == "POLL" else "announcement"
    action: str | None = None
    if _requires_response(post):
        action = (
            "Følg opp betalingsforespørselen i Spond"
            if item_type == "payment"
            else "Svar på avstemningen i Spond"
            if post_type == "POLL"
            else "Svar i Spond"
        )
    return {
        "source": SOURCE,
        "source_id": f"post:{post_id}",
        "thread_id": f"post:{post_id}",
        "child": children.get(group_id or ""),
        "sender": _sender(post, profiles),
        "published_at": _published_at(post),
        "changed_at": _changed_at(post, notification_time),
        "title": _post_title(post),
        "body": _post_text(post, profiles),
        "type": item_type,
        "deadline": _post_deadline(post),
        "event_start": None,
        "event_end": None,
        "location": None,
        "action_required": action,
        "source_url": f"https://spond.com/client/posts?postId={urllib.parse.quote(post_id, safe='')}",
        "topic": group_names.get(group_id or "") or "Spond",
        "date_note": None,
        "attachments": attachments,
    }


def _event_text(event: Mapping[str, Any], profiles: Mapping[str, str]) -> str:
    parts: list[str] = []
    description = html_to_text(pick(event, "description", "body", "text"))
    if description:
        parts.append(description)
    meetup = _iso(event.get("meetupTimestamp"))
    if meetup:
        parts.append(f"Oppmøte: {meetup}")
    tasks: list[str] = []
    for task in _as_list(event.get("tasks")):
        if isinstance(task, Mapping):
            text = html_to_text(pick(task, "name", "title", "description"))
            if text:
                tasks.append(text)
    if tasks:
        parts.append("Oppgaver:\n" + "\n".join(f"- {item}" for item in tasks))
    comments = _comment_body(event, profiles)
    if comments:
        parts.append(comments)
    return "\n\n".join(parts) or "Spond-arrangement uten beskrivelse"


def normalize_event(
    event: Mapping[str, Any],
    *,
    profiles: Mapping[str, str],
    children: Mapping[str, str],
    group_names: Mapping[str, str],
    attachments: list[dict[str, Any]],
    notification_time: datetime | None,
) -> dict[str, Any]:
    event_id = identifier(event, "arrangementet")
    group_id = entity_group_id(event)
    return {
        "source": SOURCE,
        "source_id": f"event:{event_id}",
        "thread_id": f"event:{event_id}",
        "child": children.get(group_id or ""),
        "sender": _sender(event, profiles),
        "published_at": _published_at(event),
        "changed_at": _changed_at(event, notification_time),
        "title": html_to_text(pick(event, "heading", "title", "name")) or "Spond-arrangement",
        "body": _event_text(event, profiles),
        "type": "event",
        "cancelled": bool(event.get("cancelled") or event.get("cancelledAt")),
        "cancelled_at": _iso(event.get("cancelledAt")),
        "deadline": _iso(pick(event, "rsvpDate", "responseDeadline", "dueBy")),
        "event_start": _iso(pick(event, "startTimestamp", "startTime")),
        "event_end": _iso(pick(event, "endTimestamp", "endTime")),
        "location": _location(event.get("location")),
        "action_required": "Svar på arrangementet i Spond" if _requires_response(event) else None,
        "source_url": f"https://spond.com/client/sponds/{urllib.parse.quote(event_id, safe='')}",
        "topic": group_names.get(group_id or "") or "Spond",
        "date_note": None,
        "attachments": attachments,
    }


def notification_identifier(value: Mapping[str, Any]) -> str:
    direct = pick(value, "id", "activityId", "notificationId")
    if direct not in (None, ""):
        return str(direct)
    canonical = "\0".join(
        str(pick(value, field_name) or "")
        for field_name in (
            "timestamp",
            "type",
            "key",
            "postId",
            "spondId",
            "text",
            "message",
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_notification(
    value: Mapping[str, Any], profiles: Mapping[str, str]
) -> dict[str, Any]:
    notification_id = notification_identifier(value)
    post_id = value.get("postId")
    event_id = value.get("spondId")
    if post_id not in (None, ""):
        thread_id = f"post:{post_id}"
        source_url = f"https://spond.com/client/posts?postId={urllib.parse.quote(str(post_id), safe='')}"
    elif event_id not in (None, ""):
        thread_id = f"event:{event_id}"
        source_url = f"https://spond.com/client/sponds/{urllib.parse.quote(str(event_id), safe='')}"
    else:
        thread_id = f"notification:{notification_id}"
        source_url = "https://spond.com/client"
    raw_type = html_to_text(pick(value, "type", "key", "activityType"))
    title = html_to_text(pick(value, "title", "heading", "subject")) or raw_type or "Spond-varsel"
    body = html_to_text(pick(value, "text", "message", "description", "body")) or title
    published = _iso(pick(value, "timestamp", "createdAt", "updatedAt"))
    return {
        "source": SOURCE,
        "source_id": f"notification:{notification_id}",
        "thread_id": thread_id,
        "child": None,
        "sender": _sender(value, profiles),
        "published_at": published,
        "changed_at": published,
        "title": title,
        "body": body,
        "type": "announcement",
        "deadline": None,
        "event_start": None,
        "event_end": None,
        "location": None,
        "action_required": None,
        "source_url": source_url,
        "topic": "Spond-varsel",
        "date_note": None,
        "attachments": [],
    }


def attachment_candidates(entity: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    nested = [
        value
        for field_name in ("poll", "payment", "clubPayment")
        if (value := _mapping(entity.get(field_name))) is not None
    ]
    containers: list[Mapping[str, Any]] = [
        entity,
        *nested,
        *_all_comments(entity),
    ]
    for container in containers:
        for field_name in ("attachments", "media", "images"):
            if field_name not in container:
                continue
            values = container[field_name]
            if not isinstance(values, list):
                raise SourceUnavailable("Spond returnerte ugyldig vedleggsstruktur")
            for raw in values:
                if isinstance(raw, str):
                    item: Mapping[str, Any] = {"url": raw}
                elif isinstance(raw, Mapping):
                    item = raw
                else:
                    raise SourceUnavailable("Spond returnerte ugyldig vedleggsstruktur")
                url = pick(item, "media", "url", "downloadUrl", "fileUrl", "imageUrl")
                if not isinstance(url, str) or not url:
                    raise SourceUnavailable("Spond-vedlegget mangler gyldig URL")
                identifier = str(pick(item, "id", "fileId", "uploadId") or "")
                key = (identifier, url)
                if key in seen:
                    continue
                seen.add(key)
                result.append(item)
                if len(result) > MAX_ATTACHMENTS_PER_ENTITY:
                    raise SourceUnavailable("Spond returnerte for mange vedlegg i én sak")
    return result
