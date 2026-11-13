"""A fake Skolemelding API with one child, two recent messages and old ones."""

from __future__ import annotations
import urllib.parse
from pathlib import Path

from familiekilder.api_transport import DownloadResult
import familiekilder.skolemelding as skolemelding


RECENT_1 = "2099-01-03T10:00:00+00:00"
RECENT_2 = "2099-01-02T09:00:00+00:00"
OLD = "2000-01-01T12:00:00+00:00"


class FakeCollectorTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def get_json(self, url: str, **kwargs: object) -> object:
        self.calls.append(("GET", url, dict(kwargs)))
        parsed = urllib.parse.urlsplit(url)
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if base == skolemelding.PROFILE_ENDPOINT:
            return {
                "DependantUsers": [
                    {
                        "Name": "Ada Barn",
                        "DomainUser": {"UserName": "child-1"},
                    }
                ],
                "SensitiveInformationHtml": "profile-only-value",
            }
        if base == skolemelding.CHILDREN_ENDPOINT:
            return {
                "Items": [
                    {
                        "Name": "Ada Barn",
                        "DomainUser": {"UserName": "child-1"},
                    }
                ]
            }
        if base != skolemelding.MESSAGE_DETAIL_ENDPOINT:
            raise AssertionError("unexpected GET endpoint")
        message_id = urllib.parse.parse_qs(parsed.query)["messageId"][0]
        if message_id == "m1":
            return {
                "Id": "m1",
                "ThreadId": "thread-1",
                "Header": "Turdag",
                "Body": "<p>Ta med matpakke.</p>",
                "From": "Kontaktlærer",
                "Timestamp": RECENT_1,
                "Attachments": ["/api/files/info.txt"],
                "Images": [{"ImageUrl": "/api/files/map.jpg"}],
                "Replies": [
                    {
                        "ThreadId": "thread-1",
                        "Timestamp": RECENT_2,
                        "SenderName": "Foresatt",
                        "MessageText": "Takk, det gjør vi.",
                    }
                ],
            }
        if message_id == "m2":
            return {
                "message": {
                    "Id": "m2",
                    "ThreadId": "thread-2",
                    "Header": "Svar ønskes",
                    "Body": "Kan dere bekrefte?",
                    "From": "Skolen",
                    "Timestamp": RECENT_2,
                    "IsFeedbackRequired": True,
                    "Attachments": [],
                    "Replies": [],
                }
            }
        if message_id == "unknown-date":
            return {
                "Id": "unknown-date",
                "Header": "Ukjent dato",
                "Body": "Skal ikke lagres uten dokumentert tidsstempel.",
                "Attachments": ["/api/files/should-not-download.txt"],
            }
        raise AssertionError(f"old message unexpectedly opened: {message_id}")

    def post_json(self, url: str, payload: dict[str, object], **kwargs: object) -> object:
        self.calls.append(("POST_JSON", url, dict(payload)))
        if url != skolemelding.MESSAGE_SEARCH_ENDPOINT:
            raise AssertionError("unexpected POST endpoint")
        if payload["SearchInbox"] is not True or payload["OnBehalfOf"] != "child-1":
            raise AssertionError("unexpected read filter")
        if payload["Page"] == 1:
            return {
                "Result": [
                    {
                        "Id": "m1",
                        "ThreadId": "thread-1",
                        "Header": "Turdag",
                        "Timestamp": RECENT_1,
                        "IsReplyAllowed": True,
                    },
                    {
                        "Id": "m2",
                        "ThreadId": "thread-2",
                        "Header": "Svar ønskes",
                        "Timestamp": RECENT_2,
                    },
                ],
                "TotalMatching": 4,
            }
        if payload["Page"] != 2:
            raise AssertionError("unexpected page")
        return {
            "Result": [
                {
                    "Id": "old-message",
                    "ThreadId": "old-thread",
                    "Header": "Gammel",
                    "Timestamp": OLD,
                },
                {
                    "Id": "unknown-date",
                    "ThreadId": "unknown-thread",
                    "Header": "Ukjent dato",
                },
            ],
            "TotalMatching": 4,
        }

    def download(self, url: str, target: Path, **kwargs: object) -> DownloadResult:
        self.calls.append(("DOWNLOAD", url, str(target)))
        target.write_bytes(f"downloaded:{url}".encode())
        content_type = "text/plain" if url.endswith(".txt") else "image/jpeg"
        return DownloadResult(
            byte_size=target.stat().st_size,
            sha256="0" * 64,
            content_type=content_type,
        )
