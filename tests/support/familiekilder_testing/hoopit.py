"""Fake Hoopit API responses, transport, limiter and access token."""

from __future__ import annotations
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from familiekilder.api_transport import DownloadResult
from familiekilder.hoopit_auth import HoopitAccess


CUTOFF = "2026-08-17T12:00:00+00:00"


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class NoopLimiter:
    def __init__(self) -> None:
        self.before = 0
        self.success = 0
        self.http: list[tuple[int, int | None]] = []
        self.network = 0

    def before_request(self) -> None:
        self.before += 1

    def record_success(self) -> None:
        self.success += 1

    def record_http_failure(self, status_code: int, retry_after: int | None = None) -> None:
        self.http.append((status_code, retry_after))

    def record_network_failure(self) -> None:
        self.network += 1


def _page(results: list[Mapping[str, Any]], has_next: bool = False) -> dict[str, Any]:
    return {"results": [dict(value) for value in results], "has_next": has_next}


def _responses() -> dict[str, Any]:
    return {
        "/app/users/current/notifications/": _page(
            [
                {
                    "id": "n1",
                    "timestamp": "2026-08-31T08:00:00Z",
                    "is_accessed": False,
                    "is_seen": False,
                    "data": {
                        "title": "Ny beskjed",
                        "body": "Trening flyttet",
                        "EXTRA_GROUP_NAME": "J2014",
                        "EXTRA_POST_ID": "p1",
                    },
                }
            ]
        ),
        "/app/users/current/posts/": _page(
            [
                {
                    "id": "p1",
                    "title": "Treningsinfo",
                    "text": "Ta med drikkeflaske.",
                    "poster_name": "Lagleder",
                    "created_at": "2026-08-30T10:00:00Z",
                    "updated_at": "2026-08-31T07:30:00Z",
                    "group_name": "J2014",
                    "comment_count": 2,
                    "attachments": [
                        {
                            "id": "a1",
                            "name": "plan.pdf",
                            "url": "https://files.example.test/objects/a1",
                        }
                    ],
                }
            ]
        ),
        "/app/users/current/events/": _page(
            [
                {
                    "id": "o1",
                    "event_id": "e1",
                    "title": "Trening",
                    "datetime": "2026-09-01T15:00:00Z",
                    "end": "2026-09-01T16:30:00Z",
                    "multi_answer_status": "NOT_RESPONDED",
                    "group": {"name": "J2014"},
                }
            ]
        ),
        "/app/posts/p1/comments/": _page(
            [
                {
                    "id": "c1",
                    "timestamp": "2026-08-31T08:10:00Z",
                    "comment": "Oppmøte 14:45.",
                    "poster": {"first_name": "Ada", "last_name": "Nord"},
                    "reply_count": 1,
                }
            ]
        ),
        "/app/posts/p1/comments/c1/replies/": _page(
            [
                {
                    "id": "r1",
                    "timestamp": "2026-08-31T08:20:00Z",
                    "comment": "Takk!",
                    "poster": {"first_name": "Per", "last_name": "Sør"},
                    "reply_count": 0,
                }
            ]
        ),
        "/app/events/e1/occurrences/o1/": {
            "id": "o1",
            "event": "e1",
            "title": "Trening",
            "description": "Vanlig trening.",
            "start": "2026-09-01T15:00:00Z",
            "end": "2026-09-01T16:30:00Z",
            "updated_at": "2026-08-31T09:00:00Z",
            "response_deadline": "2026-08-31T20:00:00Z",
            "location": {"name": "Hallen"},
            "group": {"name": "J2014"},
            "attachments": [
                {
                    "id": "a2",
                    "name": "kart.png",
                    "url": "https://files.example.test/objects/a2",
                }
            ],
        },
        "/app/events/e1/occurrences/o1/comments/": _page(
            [
                {
                    "id": "ec1",
                    "timestamp": "2026-08-31T09:10:00Z",
                    "comment": "Vi bruker inngang B.",
                    "poster_name": "Trener",
                    "reply_count": 0,
                }
            ]
        ),
        "/app/events/e1/occurrences/o1/posts/": _page(
            [
                {
                    "id": "p2",
                    "title": "Aktivitetsbeskjed",
                    "quill_delta": {"ops": [{"insert": "Ny inngang.\n"}]},
                    "created_at": "2026-08-31T09:05:00Z",
                    "updated_at": "2026-08-31T09:05:00Z",
                    "event_name": "Trening",
                    "group_name": "J2014",
                    "comment_count": 0,
                    "attachments": [],
                }
            ]
        ),
    }


class FakeTransport:
    def __init__(self, responses: Mapping[str, Any] | None = None) -> None:
        self.responses = dict(responses or {})
        self.gets: list[tuple[str, str | None, dict[str, str]]] = []
        self.downloads: list[tuple[str, Path, str | None, dict[str, str]]] = []
        self.download_error: BaseException | None = None

    def get_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.gets.append((url, bearer_token, dict(headers or {})))
        path = urllib.parse.urlsplit(url).path
        value = self.responses.get(path)
        if callable(value):
            value = value(url)
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise AssertionError(f"unexpected GET {path}")
        return value

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
        self.downloads.append((url, path, bearer_token, dict(headers or {})))
        if self.download_error is not None:
            raise self.download_error
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        payload = b"fixture"
        path.write_bytes(payload)
        path.chmod(0o600)
        content_type = "application/pdf" if url.endswith("a1") else "image/png"
        return DownloadResult(len(payload), "0" * 64, content_type)


def _access() -> HoopitAccess:
    return HoopitAccess(
        id_token="firebase-secret",
        app_version="Web-test",
        expires_at=2_000_000_000,
        attachment_hosts=("files.example.test",),
    )
