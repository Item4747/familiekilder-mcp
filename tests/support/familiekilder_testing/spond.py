"""Fake Spond posts, events, clock and transport."""

from __future__ import annotations
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from familiekilder.api_transport import (
    DownloadResult,
)


NOW = 2_000_000_000.0
CUTOFF = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
OLD = "2026-01-01T08:00:00Z"
RECENT = "2026-08-31T08:00:00Z"
FUTURE = "2026-09-02T16:00:00Z"
TEN_DAYS = 10 * 24 * 60 * 60


class FakeClock:
    def __init__(self, value: float = NOW) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class FakeCollectorTransport:
    def __init__(
        self,
        handler: Callable[[str], Any] | None = None,
        *,
        download_failure: BaseException | None = None,
    ) -> None:
        self.handler = handler or (lambda url: [])
        self.download_failure = download_failure
        self.gets: list[tuple[str, str | None, dict[str, str]]] = []
        self.downloads: list[
            tuple[str, Path, str | None, Mapping[str, str] | None, int | None]
        ] = []

    def get_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.gets.append((url, bearer_token, dict(headers or {})))
        return self.handler(url)

    def download(
        self,
        url: str,
        target: Path | str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        allowed_content_types: set[str] | frozenset[str] | None = None,
    ) -> DownloadResult:
        path = Path(target)
        self.downloads.append((url, path, bearer_token, headers, max_bytes))
        if self.download_failure is not None:
            raise self.download_failure
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        payload = b"fixture"
        path.write_bytes(payload)
        path.chmod(0o600)
        return DownloadResult(len(payload), "0" * 64, "application/octet-stream")


def _query(url: str) -> dict[str, str]:
    return {
        key: values[0]
        for key, values in urllib.parse.parse_qs(
            urllib.parse.urlsplit(url).query, keep_blank_values=True
        ).items()
    }


def _path(url: str) -> str:
    return urllib.parse.urlsplit(url).path


def _post(
    identifier: str,
    *,
    timestamp: str = RECENT,
    post_type: str = "PLAIN",
    comments: list[Mapping[str, Any]] | None = None,
    attachments: list[Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": identifier,
        "type": post_type,
        "timestamp": timestamp,
        "title": f"Post {identifier}",
        "body": f"Body {identifier}",
        "groupId": "group-1",
        "creatorId": "coach-1",
        "comments": list(comments or []),
        "attachments": list(attachments or []),
    }
    if post_type == "POLL":
        value["poll"] = {
            "question": f"Poll {identifier}",
            "description": "Velg et alternativ",
            "options": [{"text": "Ja"}, {"text": "Nei"}],
        }
    if post_type == "CLUB_PAYMENT":
        value["clubPayment"] = {
            "title": f"Payment {identifier}",
            "description": "Betal treningsavgift",
            "dueBy": "2026-09-05T20:00:00Z",
        }
        value["responseStatus"] = "UNPAID"
    return value


def _event(
    identifier: str,
    *,
    created: str = OLD,
    start: str | None = None,
    comments: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": identifier,
        "createdTime": created,
        "heading": f"Event {identifier}",
        "description": f"Description {identifier}",
        "groupId": "group-1",
        "comments": list(comments or []),
        "attachments": [],
    }
    if start is not None:
        value["startTimestamp"] = start
        value["endTimestamp"] = "2026-09-02T17:00:00Z"
    return value
