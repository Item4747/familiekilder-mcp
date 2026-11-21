"""Fake Hoopit external API: responses, transport, limiter and token.

Everything here is made up: families, clubs, IDs and tokens.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from typing import Any, Mapping

from familiekilder.hoopit_auth import HoopitAccess

CUTOFF = "2026-08-17T12:00:00+00:00"
# Fictional tokens in the documented format; neither has ever existed.
TOKEN = "hoopit_pat_" + "Fiktiv0Token" * 3 + "Abcd"
OTHER_TOKEN = "hoopit_pat_" + "Annet1Token0" * 3 + "Wxyz"
POSTS = "/external/me/posts/"
OCCURRENCES = "/external/me/occurrences/"


def comments_path(post_id: int | str) -> str:
    return f"/external/posts/{post_id}/comments/"


def response_path(event_id, occurrence_id, user_id) -> str:
    return f"/external/events/{event_id}/occurrences/{occurrence_id}/responses/{user_id}/"


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


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def page(results: list[Mapping[str, Any]], has_next: bool = False) -> dict[str, Any]:
    return {
        "count": len(results),
        "next": None,
        "previous": None,
        "filters": [],
        "has_next": has_next,
        "results": [dict(value) for value in results],
    }


def person(ident: int, name: str) -> dict[str, Any]:
    return {"id": ident, "full_name": name}


def post(ident: int, created_at: str, **fields: Any) -> dict[str, Any]:
    value = {
        "id": ident,
        "title": "Treningsinfo",
        "text": "Ta med drikkeflaske.",
        "author": person(501, "Lagleder Lund"),
        "group": {"id": 77, "name": "J2014"},
        "created_at": created_at,
    }
    value.update(fields)
    return value


def comment(ident: int, created_at: str, text: str, *, parent: int | None = None, author=None):
    return {
        "id": ident,
        "parent_id": parent,
        "author": author or person(601, "Ada Nord"),
        "text": text,
        "created_at": created_at,
    }


def occurrence(ident: int, event: int, start: str, end: str, **fields: Any) -> dict[str, Any]:
    value = {
        "id": ident,
        "event_id": event,
        "title": "Trening",
        "event_type": "training",
        "group": {"id": 77, "name": "J2014"},
        "start": start,
        "end": end,
        "meetup_time": start,
        "location": "Hallen",
        "cancelled": False,
        "responses": [
            {"user": person(901, "Oliver Fjell"), "response": "not_answered"},
        ],
    }
    value.update(fields)
    return value


def responses() -> dict[str, Any]:
    """One post with a comment and a reply, an older post, and one occurrence."""

    return {
        POSTS: page(
            [
                post(1001, "2026-08-30T10:00:00Z"),
                post(1000, "2026-07-01T10:00:00Z", title=None, group=None, text="Gammelt."),
            ]
        ),
        comments_path(1001): page(
            [
                comment(2001, "2026-08-31T08:10:00Z", "Oppmøte 14:45."),
                comment(
                    2002,
                    "2026-08-31T08:20:00Z",
                    "Takk!",
                    parent=2001,
                    author=person(602, "Per Sør"),
                ),
            ]
        ),
        OCCURRENCES: page(
            [
                occurrence(
                    3001,
                    4001,
                    "2026-09-01T15:00:00Z",
                    "2026-09-01T16:30:00Z",
                    meetup_time="2026-09-01T14:45:00Z",
                )
            ]
        ),
    }


class FakeTransport:
    """Answers GET and PATCH by path; a value may be a callable taking the URL."""

    def __init__(
        self,
        values: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.values = dict(values if values is not None else responses())
        self.headers = dict(headers or {})
        self.gets: list[tuple[str, str | None, dict[str, str]]] = []
        self.patches: list[tuple[str, dict[str, Any], str | None, dict[str, str]]] = []
        self.patch_result: Any = None
        self.on_patch: Callable[[str, dict[str, Any]], None] | None = None

    @staticmethod
    def query(url: str) -> dict[str, str]:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))

    def _answer(self, url: str) -> Any:
        path = urllib.parse.urlsplit(url).path
        value = self.values.get(path)
        if callable(value):
            value = value(url)
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise AssertionError(f"unexpected request {path}")
        return value

    def get_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> Any:
        self.gets.append((url, bearer_token, dict(headers or {})))
        value = self._answer(url)
        if response_headers is not None:
            response_headers.update(self.headers)
        return value

    def patch_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> Any:
        self.patches.append((url, dict(payload), bearer_token, dict(headers or {})))
        if isinstance(self.patch_result, BaseException):
            raise self.patch_result
        if self.on_patch is not None:
            self.on_patch(url, dict(payload))
        return self.patch_result


def access(token: str = TOKEN) -> HoopitAccess:
    return HoopitAccess(token=token, account_scope="a" * 64)
