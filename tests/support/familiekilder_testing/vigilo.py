"""A fake Vigilo API with messages, news, registrations and attachments."""

from __future__ import annotations
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from familiekilder.api_transport import DownloadResult


class FakeVigiloApi:
    def __init__(self) -> None:
        self.gets: list[tuple[str, str | None, dict[str, str]]] = []
        self.downloads: list[tuple[str, str | None]] = []
        self.thread_detail_calls = 0
        self.post_detail_calls = 0

    def get_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.gets.append((url, bearer_token, dict(headers or {})))
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/children":
            return {
                "items": [
                    {
                        "childId": "child-1",
                        "firstName": "Ada",
                        "lastName": "Nord",
                        "nationalIdentityNumber": "must-not-be-snapshotted",
                        "organizationalUnits": [{"id": "extra"}],
                    },
                    {
                        "childId": "child-2",
                        "firstName": "Bo",
                        "lastName": "Sør",
                        "profilePicture": {"url": "extra"},
                    },
                ]
            }
        if parsed.path == "/api/message-threads":
            assert query["fromDate"] == ["2026-08-20"]
            return {
                "pageId": 0,
                "unreadMessageThreadsCount": 1,
                "messageThreads": [
                    {
                        "threadUid": "thread-1",
                        "title": "Turdag",
                        "updatedAt": "2026-08-31T08:00:00+02:00",
                        "sender": {"firstName": "Lærer", "lastName": "En"},
                        "lastMessage": {
                            "id": "message-2",
                            "body": "Ta med sekk",
                            "timeCreated": "2026-08-30T10:00:00+02:00",
                        },
                        "isSeen": False,
                    }
                ],
            }
        if parsed.path == "/api/messages/threads/thread-1":
            self.thread_detail_calls += 1
            return {
                "pageId": 0,
                "threadUid": "thread-1",
                "title": "Turdag",
                "createdAt": "2026-08-29T09:00:00+02:00",
                "sender": {"firstName": "Lærer", "lastName": "En"},
                "messages": [
                    {
                        "id": "message-1",
                        "body": "<p>Vi går på tur.</p>",
                        "timeCreated": "2026-08-29T09:00:00+02:00",
                        "sender": {"firstName": "Lærer", "lastName": "En"},
                        "isSeen": False,
                        "attachments": [
                            {
                                "id": "attachment-1",
                                "name": "plan.pdf",
                                "mimeType": "application/pdf",
                                "url": "https://files.example.test/plan.pdf?sig=private",
                            }
                        ],
                    },
                    {
                        "id": "message-2",
                        "body": "Ta med sekk",
                        "timeCreated": "2026-08-30T10:00:00+02:00",
                        "sender": {"firstName": "Lærer", "lastName": "En"},
                        "isSeen": False,
                        "attachments": [
                            {
                                "id": "attachment-1",
                                "name": "plan.pdf",
                                "mimeType": "application/pdf",
                                "url": "https://files.example.test/plan.pdf?sig=private",
                            }
                        ],
                    },
                ],
            }
        if parsed.path == "/api/news-feed":
            assert query["lastDate"]
            return [
                {
                    "id": "post-1",
                    "title": "Foreldremøte",
                    "type": "post",
                    "postedTime": "2026-08-30T12:00:00+02:00",
                    "postedBy": {"firstName": "Rektor", "lastName": "To"},
                    "text": "Kort tekst",
                    "isSeen": False,
                    "startDate": "2026-09-03",
                    "startTime": "18:00:00+02:00",
                    "endDate": "2026-09-03",
                    "endTime": "19:00:00+02:00",
                },
                {
                    "id": "unknown-age",
                    "title": "Mangler dato",
                    "type": "post",
                    "text": "Skal ikke inkluderes",
                },
                {
                    "id": "not-a-news-post",
                    "type": "routine",
                    "postedTime": "2026-08-30T12:00:00+02:00",
                },
            ]
        if parsed.path == "/api/posts/post-1":
            self.post_detail_calls += 1
            return {
                "id": "post-1",
                "title": "Foreldremøte",
                "text": "<p>Full tekst om møtet.</p>",
                "posted": "2026-08-30T12:00:00+02:00",
                "author": {"firstName": "Rektor", "lastName": "To"},
                "childIds": ["child-1", "child-2"],
                "attachments": [],
                "isSeen": False,
            }
        raise AssertionError(url)

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
        self.downloads.append((url, bearer_token))
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.chmod(0o700)
        payload = b"fixture pdf"
        assert max_bytes is not None and max_bytes >= len(payload)
        destination.write_bytes(payload)
        destination.chmod(0o600)
        return DownloadResult(len(payload), "unused", "application/pdf")
