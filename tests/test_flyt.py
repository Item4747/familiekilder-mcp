"""Flyt reads against an invented barnehage family; no network."""

from __future__ import annotations

import tempfile
import unittest
import urllib.parse
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from familiekilder import flyt, flyt_auth, live_flyt_reader, live_readers
from familiekilder.api_transport import ApiHTTPError, DownloadResult
from familiekilder.flyt_auth import API_BASE_URL, TOKEN_ENDPOINT
from familiekilder.private_state import atomic_write_private_json
from familiekilder.source_adapter import SourceLoginRequired, SourceUnavailable
from familiekilder.sync_snapshot import SyncSnapshot, snapshot_scope

PLACEMENT = "plass-1"
OLD_PLACEMENT = "plass-0"
MEDIA = f"https://{flyt.MEDIA_HOST}"
SINCE = datetime(2026, 11, 1, tzinfo=UTC)
TODAY = date(2026, 11, 13)


def photo(name: str) -> dict:
    """A diary photo as Flyt lists it: signed CloudFront links, no file name."""

    signed = "?d=150x150&Policy=oppdiktet&Signature=oppdiktet&Key-Pair-Id=oppdiktet"
    return {
        "id": f"bilde-{name}",
        "diaryPostId": "dagbok-1",
        "attachmentFileId": f"fil-{name}",
        "createdDate": "2026-11-12T13:00:00.123Z",
        "thumbnailUrl": f"{MEDIA}/barnehage-1/{name}{signed}",
        "previewUrl": f"{MEDIA}/barnehage-1/{name}{signed.replace('150x150', '1920x1920')}",
    }


def counts(payload: dict) -> dict:
    """Like Flyt: one key per day from ``from`` up to, not including, ``to``."""

    first = date.fromisoformat(payload["dateRange"]["from"][:10])
    end = date.fromisoformat(payload["dateRange"]["to"][:10])
    days = {}
    while first < end:
        days[first.isoformat()] = {"events": 1 if first in EVENT_DAYS else 0}
        first += timedelta(days=1)
    return {"counts": days}


EVENTS = {
    date(2026, 11, 20): {
        "id": "hendelse-1",
        "title": "Lysfest",
        "isAllDay": False,
        "attachmentCount": 1,
        "startDate": "2026-11-20T15:00:00Z",
        "endDate": "2026-11-20T16:30:00Z",
        "children": [{"id": "barn-1", "firstName": "Ola", "colorIndex": 3}],
    },
    # The last day of the first month-sized counts range (1 Nov - 1 Dec).
    date(2026, 12, 1): {
        "id": "hendelse-2",
        "title": "Juleverksted",
        "isAllDay": True,
        "attachmentCount": 0,
        "startDate": "2026-11-30T23:00:00Z",
        "endDate": "2026-12-01T23:00:00Z",
    },
}
EVENT_DAYS = set(EVENTS)


def events(payload: dict) -> list:
    day = date.fromisoformat(payload["statusDate"][:10])
    return [EVENTS[day], {"title": "Uten id"}] if day in EVENTS else []


def event_detail(payload: dict) -> dict:
    return {
        **EVENTS[date(2026, 11, 20)],
        "description": "Ta med lykt.",
        "attachments": [
            {
                "id": "fil-2",
                "fileName": "program.pdf",
                "url": f"{API_BASE_URL}/vfk/v1/files/fil-2",
            }
        ],
    }


def announcement(number: int, **extra) -> dict:
    return {
        "id": f"beskjed-{number}",
        "title": f"Beskjed {number}",
        "content": "<p>Husk <b>regntøy</b></p>",
        "startDate": "2026-11-10T08:00:00Z",
        "lastEditedDate": None,
        "stopDate": "2026-11-20T08:00:00Z",
        "hasAttachments": False,
        "attachments": [],
        **extra,
    }


class FakeGuardian:
    """Answers per path; records every call with its headers."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.failures: dict[str, list[Exception]] = {}
        self.routes = {
            "v1/children": [
                {
                    "firstName": "Ola",
                    "lastName": "Testesen",
                    "birthDate": "2021-05-01T00:00:00Z",
                    "hasPhoto": False,
                    "kindergartenData": {
                        "placementId": PLACEMENT,
                        "kindergartenId": "barnehage-1",
                        "childId": "barn-1",
                        "kindergartenName": "Eventyrskogen barnehage",
                        "unitName": "Bjørn",
                        "formsAvailable": True,
                        "guardianHasParentalResponsibility": True,
                        "hasPhoto": False,
                        "placements": [
                            {"placementId": PLACEMENT, "kindergartenName": "Eventyrskogen"},
                            {"placementId": OLD_PLACEMENT, "kindergartenName": "Gamle"},
                        ],
                    },
                },
                {"firstName": "Kari", "kindergartenData": None, "schoolData": {"pupilId": 7}},
            ],
            f"vfk/v1/children/{PLACEMENT}/announcements/search": {
                "resultList": [
                    announcement(
                        1,
                        hasAttachments=True,
                        attachments=[
                            {
                                "id": "fil-1",
                                "fileName": "plan.pdf",
                                "url": f"{API_BASE_URL}/vfk/v1/files/fil-1",
                            }
                        ],
                    )
                ],
                "totalCount": 0,
            },
            f"vfk/v1/children/{PLACEMENT}/diary-posts/search": {
                "resultList": [
                    {
                        "id": "dagbok-1",
                        "title": "",
                        "content": "Vi var i skogen.",
                        "createdDate": "2026-11-12T13:00:00Z",
                        "createdBy": {"firstName": "Pia", "lastName": "Pedagog"},
                        # The search shows only the first photo, and only its thumbnail.
                        "attachments": [
                            {
                                key: value
                                for key, value in photo("a").items()
                                if key != "previewUrl"
                            }
                        ],
                    }
                ],
                "totalCount": 1,
            },
            f"vfk/v1/children/{PLACEMENT}/diary-posts/dagbok-1": {
                "id": "dagbok-1",
                "title": "",
                "content": "Vi var i skogen.",
                "createdDate": "2026-11-12T13:00:00Z",
                "createdBy": {"firstName": "Pia", "lastName": "Pedagog"},
                "attachments": [photo("a"), photo("b"), photo("c")],
            },
            f"vfk/v1/children/{PLACEMENT}/attendances/search": {
                "resultList": [
                    {"id": "oppmote-2", "checkInDateTime": "2026-11-13T07:52:00.5Z"},
                    {
                        "id": "oppmote-1",
                        "checkInDateTime": "2026-11-12T07:37:09Z",
                        "checkOutDateTime": "2026-11-12T14:46:48.2Z",
                    },
                ],
                "totalCount": 2,
            },
            f"vfk/v1/children/{PLACEMENT}/messages": [
                {
                    "id": "melding-2",
                    "message": "Hentes 15",
                    "createdDate": "2026-11-12T07:00:00Z",
                    "sender": {"firstName": "Forelder", "lastName": "Testesen"},
                },
                {
                    "id": "melding-1",
                    "message": "Skjult",
                    "createdDate": "2026-11-11T07:00:00Z",
                    "isHidden": True,
                },
            ],
            f"vfk/v1/children/{PLACEMENT}/absences/search": {
                "resultList": [
                    {
                        "id": "fravaer-1",
                        "startDate": "2026-11-05T00:00:00Z",
                        "stopDate": "2026-11-06T00:00:00Z",
                        "comment": "Forkjølet",
                        "createdDate": "2026-11-04T18:00:00Z",
                        "modifiedBy": {"firstName": "Forelder"},
                    }
                ],
                "totalCount": 1,
            },
            f"vfk/v1/children/{PLACEMENT}/calendar/events/counts": counts,
            f"vfk/v1/children/{PLACEMENT}/calendar/events/search": events,
            f"vfk/v2/children/{PLACEMENT}/calendar/events/hendelse-1": event_detail,
            f"vfk/v2/children/{PLACEMENT}/calendar/events/hendelse-2": lambda payload: {
                **EVENTS[date(2026, 12, 1)],
                "description": None,
                "attachments": [],
            },
        }
        # The previous placement has nothing left to show.
        for path, value in list(self.routes.items()):
            if PLACEMENT in path and "hendelse" not in path and "dagbok-1" not in path:
                old = path.replace(PLACEMENT, OLD_PLACEMENT)
                if path.endswith("/messages"):
                    self.routes[old] = []
                elif callable(value):
                    self.routes[old] = (
                        (lambda payload: {"counts": {}}) if path.endswith("counts") else value
                    )
                else:
                    self.routes[old] = {"resultList": [], "totalCount": 0}

    def _answer(self, method, url, payload, bearer_token, headers):
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path.lstrip("/")
        self.calls.append(
            {
                "method": method,
                "path": path,
                "query": parsed.query,
                "payload": payload,
                "token": bearer_token,
                "headers": dict(headers),
            }
        )
        failures = self.failures.get(path)
        if failures:
            raise failures.pop(0)
        if path not in self.routes:
            raise AssertionError(f"unexpected path {path}")
        value = self.routes[path]
        return value(payload) if callable(value) else value

    def get_json(self, url, *, bearer_token=None, headers=None):
        return self._answer("GET", url, None, bearer_token, headers)

    def post_json(self, url, payload, *, bearer_token=None, headers=None):
        return self._answer("POST", url, payload, bearer_token, headers)

    def download(self, url, target, *, bearer_token=None, headers=None, max_bytes=None):
        parsed = urllib.parse.urlsplit(url)
        self.calls.append(
            {
                "method": "DOWNLOAD",
                "host": parsed.hostname,
                "path": parsed.path,
                "query": parsed.query,
                "token": bearer_token,
                "headers": dict(headers or {}),
            }
        )
        failures = self.failures.get(parsed.hostname)
        if failures:
            raise failures.pop(0)
        if parsed.hostname == flyt.MEDIA_HOST:
            Path(target).write_bytes(b"\xff\xd8\xff oppdiktet")
            return DownloadResult(13, "0" * 64, "image/jpeg")
        Path(target).write_bytes(b"%PDF-1.4 oppdiktet")
        return DownloadResult(18, "0" * 64, "application/pdf")


class FakeAuth:
    def __init__(self) -> None:
        self.calls = 0

    def post_form(self, url, fields, **kwargs):
        assert url == TOKEN_ENDPOINT
        self.calls += 1
        return {
            "access_token": f"access-{self.calls + 1}",
            "refresh_token": f"refresh-{self.calls + 1}",
            "expires_in": 3600,
        }


class FlytReadTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        municipality = flyt_auth.parse_municipality(
            {
                "customerName": "fjordvik",
                "displayName": "Fjordvik kommune",
                "organizationNumber": "912345678",
            }
        )
        flyt_auth._store_client_config(self.root, flyt_auth.ClientConfig(municipality))
        flyt_auth.record_consent(self.root, at=1_800_000_000)
        atomic_write_private_json(flyt_auth.device_path(self.root), {"device_unique_id": "enhet-1"})
        atomic_write_private_json(
            flyt_auth.session_path(self.root),
            {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "subject": "a" * 64,
                "device_unique_id": "enhet-1",
                "customer_key": "fjordvik",
                "updated_at": 1_800_000_000,
                "access_expires_at": 4_000_000_000,
            },
        )
        self.guardian = FakeGuardian()
        self.auth = FakeAuth()

    def client(self) -> flyt.Client:
        return flyt.Client(self.root, self.guardian, self.auth)

    def collect(self):
        return flyt.collect(self.client(), SINCE, today=TODAY)

    def test_only_listed_reads_pass_the_route_guard(self) -> None:
        self.assertEqual(flyt.guard_route("GET", "v1/children"), "v1/children")
        refused = [
            ("PUT", f"vfk/v1/children/{PLACEMENT}/announcements/a/seen"),
            ("GET", f"vfk/v1/children/{PLACEMENT}/announcements/a/seen"),
            ("POST", f"vfk/v1/children/{PLACEMENT}/messages/seen"),
            ("POST", f"vfk/v1/children/{PLACEMENT}/messages"),
            ("POST", f"vfk/v1/children/{PLACEMENT}/absences"),
            ("PUT", f"vfk/v1/children/{PLACEMENT}/absences/a"),
            ("DELETE", f"vfk/v1/children/{PLACEMENT}/absences/a"),
            ("POST", f"vfk/v1/children/{PLACEMENT}/consents/samtykke-1"),
            ("POST", "vfs/v1/notifications/register-device"),
            ("POST", "v2/session"),
            ("DELETE", "v2/session"),
            ("GET", "v1/children/../v2/session"),
            ("POST", "v1/children"),
        ]
        for method, path in refused:
            with self.subTest(method=method, path=path), self.assertRaises(SourceUnavailable):
                flyt.guard_route(method, path)
        with self.assertRaises(SourceUnavailable):
            flyt.guard_route("GET", f"vfk/v1/children/{PLACEMENT}/messages", {"Seen": "true"})
        for url in (
            "https://andre.example/fil",
            f"{API_BASE_URL}/vfk/v1/a/seen",
            f"http://{flyt_auth.API_HOST}/fil",
            f"{API_BASE_URL}/fil#x",
        ):
            with self.subTest(url=url), self.assertRaises(SourceUnavailable):
                flyt.guard_attachment(url)

    def test_refused_route_never_reaches_the_transport(self) -> None:
        client = self.client()
        with self.assertRaises(SourceUnavailable):
            client.read("PUT", f"vfk/v1/children/{PLACEMENT}/announcements/beskjed-1/seen")
        self.assertEqual(self.guardian.calls, [])

    def test_collect_normalizes_every_content_type(self) -> None:
        items, complete = self.collect()
        self.assertTrue(complete)
        by_type = {item["source_id"]: item for item in items}
        self.assertEqual(
            sorted(by_type),
            [
                f"absence:{PLACEMENT}:fravaer-1",
                f"announcement:{PLACEMENT}:beskjed-1",
                f"attendance:{PLACEMENT}:oppmote-1",
                f"attendance:{PLACEMENT}:oppmote-2",
                f"diary:{PLACEMENT}:dagbok-1",
                f"event:{PLACEMENT}:hendelse-1:20261120T150000Z",
                f"event:{PLACEMENT}:hendelse-2:20261130T230000Z",
                f"message:{PLACEMENT}:melding-2",
            ],
        )
        beskjed = by_type[f"announcement:{PLACEMENT}:beskjed-1"]
        self.assertEqual(
            (beskjed["type"], beskjed["child"], beskjed["published_at"]),
            ("announcement", "Ola Testesen", "2026-11-10T08:00:00+00:00"),
        )
        self.assertEqual(beskjed["body"].split(), ["Husk", "regntøy"])
        self.assertEqual(beskjed["attachments"][0]["filename"], "plan.pdf")
        self.assertEqual(beskjed["date_note"], "Vises til 2026-11-20")
        dagbok = by_type[f"diary:{PLACEMENT}:dagbok-1"]
        self.assertEqual(
            (dagbok["title"], dagbok["sender"], dagbok["topic"]),
            ("Dagbok", "Pia Pedagog", "Dagbok"),
        )
        # Every photo comes from the detail read, as the full-size preview.
        self.assertEqual(
            [(a["filename"], a["mime_type"]) for a in dagbok["attachments"]],
            [("bilde-1.jpg", "image/jpeg"), ("bilde-2.jpg", "image/jpeg"), ("bilde-3.jpg", "image/jpeg")],
        )
        self.assertIn("1920x1920", dagbok["attachments"][0]["_spec"]["url"])
        self.assertNotIn("content_complete", dagbok)
        melding = by_type[f"message:{PLACEMENT}:melding-2"]
        self.assertEqual(
            (melding["type"], melding["thread_id"], melding["title"]),
            ("message", f"messages:{PLACEMENT}", "Melding fra Forelder Testesen"),
        )
        hendelse = by_type[f"event:{PLACEMENT}:hendelse-1:20261120T150000Z"]
        self.assertEqual(
            (hendelse["type"], hendelse["event_start"], hendelse["event_end"]),
            ("event", "2026-11-20T15:00:00+00:00", "2026-11-20T16:30:00+00:00"),
        )
        self.assertEqual(hendelse["body"], "Ta med lykt.")
        self.assertIsNone(hendelse["location"])
        heldag = by_type[f"event:{PLACEMENT}:hendelse-2:20261130T230000Z"]
        self.assertEqual(heldag["date_note"], "Hele dagen")
        oppmote = by_type[f"attendance:{PLACEMENT}:oppmote-1"]
        self.assertEqual(
            (oppmote["type"], oppmote["topic"], oppmote["title"], oppmote["body"]),
            ("attendance", "Inn-/utsjekk", "Inn-/utsjekk 12.11.2026", "Sjekket inn 08:37. Sjekket ut 15:46."),
        )
        self.assertEqual(
            (oppmote["event_start"], oppmote["event_end"]),
            ("2026-11-12T07:37:09+00:00", "2026-11-12T14:46:48+00:00"),
        )
        self.assertEqual(
            by_type[f"attendance:{PLACEMENT}:oppmote-2"]["body"], "Sjekket inn 08:52."
        )
        fravaer = by_type[f"absence:{PLACEMENT}:fravaer-1"]
        self.assertEqual(
            (fravaer["type"], fravaer["title"], fravaer["body"]),
            ("absence", "Fravær 2026-11-05 – 2026-11-06", "Forkjølet"),
        )
        # Every request carries the app's headers and no Session-Id.
        for call in self.guardian.calls:
            self.assertEqual(call["token"], "access-1")
            self.assertEqual(
                call["headers"], {"Customer-Key": "fjordvik", "Accept-Language": "nb-NO"}
            )
        search = next(c for c in self.guardian.calls if c["path"].endswith("announcements/search"))
        self.assertEqual(
            search["payload"],
            {"sort": [{"orderBy": 1, "orderType": 1}], "paging": {"skip": 0, "take": 25}},
        )
        counts = [c for c in self.guardian.calls if c["path"].endswith(f"{PLACEMENT}/calendar/events/counts")]
        self.assertEqual(counts[0]["payload"]["childIds"], ["barn-1"])
        # ``to`` is midnight after the last day; Flyt leaves that day out.
        self.assertEqual(
            [c["payload"]["dateRange"] for c in counts],
            [
                {"from": "2026-11-01T00:00:00Z", "to": "2026-12-02T00:00:00Z"},
                {"from": "2026-12-02T00:00:00Z", "to": "2027-01-02T00:00:00Z"},
                {"from": "2027-01-02T00:00:00Z", "to": "2027-01-13T00:00:00Z"},
            ],
        )
        attendance = next(c for c in self.guardian.calls if c["path"].endswith("attendances/search"))
        self.assertEqual(attendance["payload"], {"paging": {"skip": 0, "take": 50}})
        # The previous placement is read too.
        self.assertTrue(any(OLD_PLACEMENT in c["path"] for c in self.guardian.calls))
        detail = next(c for c in self.guardian.calls if "/v2/" in "/" + c["path"])
        self.assertEqual(
            detail["payload"],
            {
                "occurrenceStartDate": "2026-11-20T15:00:00Z",
                "occurrenceEndDate": "2026-11-20T16:30:00Z",
            },
        )

    def test_old_items_are_left_out(self) -> None:
        self.guardian.routes[f"vfk/v1/children/{PLACEMENT}/announcements/search"] = {
            "resultList": [announcement(1, startDate="2026-01-01T08:00:00Z", stopDate=None)],
            "totalCount": 1,
        }
        items, _complete = self.collect()
        self.assertEqual([i for i in items if i["source_id"].startswith("announcement:")], [])

    def test_refreshes_once_on_401_and_uses_the_rotated_token(self) -> None:
        self.guardian.failures["v1/children"] = [ApiHTTPError(401)]
        flyt.placements(self.client())
        self.assertEqual(self.auth.calls, 1)
        self.assertEqual([c["token"] for c in self.guardian.calls], ["access-1", "access-2"])
        self.assertEqual(flyt_auth.local_session_for_use(self.root)["refresh_token"], "refresh-2")

    def test_second_401_needs_a_new_login(self) -> None:
        self.guardian.failures["v1/children"] = [ApiHTTPError(401), ApiHTTPError(401)]
        with self.assertRaises(SourceLoginRequired):
            flyt.placements(self.client())
        self.assertEqual(self.auth.calls, 1)

    def test_page_limit_marks_the_list_incomplete(self) -> None:
        full = {"resultList": [announcement(n) for n in range(25)], "totalCount": 10_000}
        self.guardian.routes[f"vfk/v1/children/{PLACEMENT}/announcements/search"] = full
        with patch.object(flyt, "MAX_PAGES", 3):
            items, complete = self.collect()
        self.assertFalse(complete)
        searches = [c for c in self.guardian.calls if c["path"].endswith(f"{PLACEMENT}/announcements/search")]
        self.assertEqual([c["payload"]["paging"]["skip"] for c in searches], [0, 25, 50])

    def test_newest_first_page_older_than_window_stops_paging(self) -> None:
        old = [
            announcement(n, startDate=f"2026-0{9 - n // 10}-{28 - n:02d}T08:00:00Z")
            for n in range(25)
        ]
        self.guardian.routes[f"vfk/v1/children/{PLACEMENT}/announcements/search"] = {
            "resultList": old,
            "totalCount": 10_000,
        }
        _items, complete = self.collect()
        self.assertTrue(complete)
        searches = [c for c in self.guardian.calls if c["path"].endswith(f"{PLACEMENT}/announcements/search")]
        self.assertEqual(len(searches), 1)

    def test_one_refused_stream_keeps_the_rest_but_is_incomplete(self) -> None:
        self.guardian.failures[f"vfk/v1/children/{PLACEMENT}/diary-posts/search"] = [
            ApiHTTPError(403)
        ]
        items, complete = self.collect()
        self.assertFalse(complete)
        self.assertFalse(any(item["source_id"].startswith("diary:") for item in items))
        self.assertTrue(any(item["source_id"].startswith("announcement:") for item in items))

    def test_reader_reference_get_item_and_download(self) -> None:
        client = self.client()
        items, complete = live_flyt_reader.list_items(self.root, flyt, client, SINCE.isoformat())
        self.assertTrue(complete)
        event = next(item for item in items if item["type"] == "event")
        self.assertEqual(
            event["reference"],
            {
                "kind": "event",
                "id": "hendelse-1",
                "child_id": PLACEMENT,
                "occurrence_id": "20261120T150000Z",
            },
        )
        again = live_flyt_reader.get_item(self.root, flyt, client, event["reference"])
        self.assertEqual(again["source_id"], event["source_id"])
        beskjed = next(item for item in items if item["type"] == "announcement")
        fetched = live_flyt_reader.get_item(self.root, flyt, client, beskjed["reference"])
        self.assertEqual(fetched["body"], beskjed["body"])
        with self.assertRaises(Exception):
            live_flyt_reader.get_item(self.root, flyt, client, {**beskjed["reference"], "url": "x"})
        directory = self.root / ".private" / "nedlasting"
        directory.mkdir(parents=True, mode=0o700)
        path, mime = live_flyt_reader.download(
            self.root,
            flyt,
            client,
            fetched,
            fetched["attachments"][0]["attachment_id"],
            beskjed["reference"],
            directory,
        )
        self.assertEqual((path.name, mime), ("plan.pdf", "application/pdf"))
        download = self.guardian.calls[-1]
        self.assertEqual(
            download["headers"], {"Customer-Key": "fjordvik", "Accept-Language": "nb-NO"}
        )
        self.assertEqual(download["token"], "access-1")

    def test_zero_total_next_to_rows_does_not_stop_paging(self) -> None:
        # Flyt's announcement search answers totalCount 0 next to real rows.
        def page(payload):
            skip = payload["paging"]["skip"]
            rows = [announcement(n) for n in range(skip, min(skip + 25, 30))]
            return {"resultList": rows, "totalCount": 0}

        self.guardian.routes[f"vfk/v1/children/{PLACEMENT}/announcements/search"] = page
        rows, complete = flyt.announcements(
            self.client(), flyt.Placement(PLACEMENT, "barn-1", "Ola")
        )
        self.assertTrue(complete)
        self.assertEqual(len(rows), 30)

    def test_diary_photos_come_from_the_photo_host_without_token(self) -> None:
        client = self.client()
        items, _complete = live_flyt_reader.list_items(self.root, flyt, client, SINCE.isoformat())
        dagbok = next(item for item in items if item["source_id"].startswith("diary:"))
        directory = self.root / ".private" / "nedlasting"
        directory.mkdir(parents=True, mode=0o700)
        path, mime = live_flyt_reader.download(
            self.root,
            flyt,
            client,
            dagbok,
            dagbok["attachments"][1]["attachment_id"],
            dagbok["reference"],
            directory,
        )
        self.assertEqual((path.name, mime), ("bilde-2.jpg", "image/jpeg"))
        download = self.guardian.calls[-1]
        self.assertEqual(
            (download["host"], download["token"], download["headers"]),
            (flyt.MEDIA_HOST, None, {}),
        )
        self.assertIn("1920x1920", download["query"])
        # An expired link is a source failure, never a reason to refresh tokens.
        self.guardian.failures[flyt.MEDIA_HOST] = [ApiHTTPError(403)]
        with self.assertRaises(SourceUnavailable):
            live_flyt_reader.download(
                self.root,
                flyt,
                client,
                dagbok,
                dagbok["attachments"][0]["attachment_id"],
                dagbok["reference"],
                directory,
            )
        self.assertEqual(self.auth.calls, 0)
        self.assertEqual(
            flyt.guard_attachment(f"{MEDIA}/barnehage-1/a?Signature=x"),
            f"{MEDIA}/barnehage-1/a?Signature=x",
        )
        for url in (f"http://{flyt.MEDIA_HOST}/a", "https://cdn.example/a"):
            with self.subTest(url=url), self.assertRaises(SourceUnavailable):
                flyt.guard_attachment(url)

    def test_diary_get_item_is_one_detail_read(self) -> None:
        client = self.client()
        reference = {"kind": "diary", "id": "dagbok-1", "child_id": PLACEMENT}
        before = len(self.guardian.calls)
        item = live_flyt_reader.get_item(self.root, flyt, client, reference)
        paths = [c["path"] for c in self.guardian.calls[before:]]
        self.assertEqual(paths, ["v1/children", f"vfk/v1/children/{PLACEMENT}/diary-posts/dagbok-1"])
        self.assertEqual(len(item["attachments"]), 3)
        self.assertEqual(flyt.guard_route("GET", paths[-1]), paths[-1])

    def test_every_listed_item_equals_its_get_item(self) -> None:
        # The list's detail hash is the hash get_item gives later, so both
        # paths must build the same item (docs/adding-a-service.md, «Listen og
        # `get_item` gir samme element»).
        without_photos = {
            "id": "dagbok-2",
            "title": "Tur",
            "content": "<p>Vi gikk til <b>bekken</b>.</p>",
            "createdDate": "2026-11-11T12:00:00Z",
            "createdBy": {"firstName": "Pia", "lastName": "Pedagog"},
            "attachments": [],
        }
        search = self.guardian.routes[f"vfk/v1/children/{PLACEMENT}/diary-posts/search"]
        search["resultList"].append(dict(without_photos))
        search["totalCount"] = 2
        # Even a detail that lacks a field the search row has gives the same
        # item both ways: the list does not fill it in from the search row.
        del self.guardian.routes[f"vfk/v1/children/{PLACEMENT}/diary-posts/dagbok-1"]["createdBy"]
        self.guardian.routes[f"vfk/v1/children/{PLACEMENT}/diary-posts/dagbok-2"] = dict(
            without_photos
        )
        client = self.client()
        items, complete = live_flyt_reader.list_items(
            self.root, flyt, client, SINCE.isoformat(), include_detail_hash=True
        )
        self.assertTrue(complete)
        kinds = {item["reference"]["kind"] for item in items}
        self.assertEqual(
            kinds, {"announcement", "diary", "message", "absence", "attendance", "event"}
        )
        diaries = {item["source_id"] for item in items if item["reference"]["kind"] == "diary"}
        self.assertEqual(diaries, {f"diary:{PLACEMENT}:dagbok-1", f"diary:{PLACEMENT}:dagbok-2"})
        for listed in items:
            with self.subTest(listed["source_id"]):
                fetched = live_flyt_reader.get_item(self.root, flyt, client, listed["reference"])
                self.assertEqual(listed.pop("_fresh_detail_hash"), live_readers.content_hash(fetched))
                self.assertEqual(listed, fetched)

    def test_refused_diary_detail_keeps_the_post_but_is_incomplete(self) -> None:
        self.guardian.failures[f"vfk/v1/children/{PLACEMENT}/diary-posts/dagbok-1"] = [
            ApiHTTPError(403)
        ]
        items, complete = self.collect()
        self.assertFalse(complete)
        dagbok = next(item for item in items if item["source_id"].startswith("diary:"))
        self.assertIs(dagbok["content_complete"], False)
        # Only the search's first photo remains, as a thumbnail.
        self.assertEqual(len(dagbok["attachments"]), 1)
        self.assertIn("150x150", dagbok["attachments"][0]["_spec"]["url"])

    def test_attendance_reference_reads_again(self) -> None:
        client = self.client()
        reference = {"kind": "attendance", "id": "oppmote-1", "child_id": PLACEMENT}
        item = live_flyt_reader.get_item(self.root, flyt, client, reference)
        self.assertEqual(item["source_id"], f"attendance:{PLACEMENT}:oppmote-1")

    def test_snapshot_reuses_reads_with_the_same_payload(self) -> None:
        atomic_write_private_json(
            flyt_auth.session_path(self.root), {**flyt_auth._read_session(self.root)}
        )
        client = self.client()
        with snapshot_scope(SyncSnapshot("oppdiktet-jobb"), self.root):
            flyt.placements(client)
            flyt.placements(client)
            flyt.announcements(client, flyt.Placement(PLACEMENT, "barn-1", "Ola"))
            flyt.announcements(client, flyt.Placement(PLACEMENT, "barn-1", "Ola"))
        self.assertEqual(len(self.guardian.calls), 2)


if __name__ == "__main__":
    unittest.main()
