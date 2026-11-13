from __future__ import annotations

import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest import mock

from contextlib import nullcontext
from familiekilder import live_readers
from familiekilder import vigilo
from familiekilder.api_transport import ApiHTTPError
from familiekilder_testing.vigilo import (
    FakeVigiloApi,
)


class PagingApi:
    def __init__(self) -> None:
        self.pages: list[str | None] = []

    def get_json(self, url: str, **_kwargs: Any) -> Any:
        parsed = urllib.parse.urlsplit(url)
        page = urllib.parse.parse_qs(parsed.query).get("pageId", [None])[0]
        self.pages.append(page)
        if page is None:
            return {
                "pageId": 0,
                "messageThreads": [
                    {
                        "threadUid": "one",
                        "lastMessage": {
                            "timeCreated": "2026-08-30T10:00:00+02:00"
                        },
                    },
                    {
                        "threadUid": "two",
                        "lastMessage": {
                            "timeCreated": "2026-08-30T09:00:00+02:00"
                        },
                    },
                ],
            }
        return {"pageId": 1, "messageThreads": []}


class _Conn:
    def close(self) -> None:
        pass


class VigiloCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp_path = Path(self.temporary.name)

    def test_snapshot_is_bounded_minimal_deduplicated_and_read_only(self) -> None:
        client = FakeVigiloApi()
        snapshot = vigilo.fetch_snapshot(
            "access-token",
            "2026-08-20T00:00:00+02:00",
            transport=client,
            app_version="Android 9.9.9-test",
        )
        self.assertEqual(
            snapshot["children"],
            [
                {"childId": "child-1", "firstName": "Ada", "lastName": "Nord"},
                {"childId": "child-2", "firstName": "Bo", "lastName": "Sør"},
            ],
        )
        self.assertEqual(len(snapshot["message_threads"]), 1)
        self.assertEqual(
            snapshot["message_threads"][0]["child_ids"], ["child-1", "child-2"]
        )
        self.assertEqual(client.thread_detail_calls, 1)
        self.assertEqual(
            [entry["summary"]["id"] for entry in snapshot["news"]], ["post-1"]
        )
        self.assertEqual(snapshot["news"][0]["child_ids"], ["child-1", "child-2"])
        self.assertEqual(client.post_detail_calls, 1)
        self.assertNotIn("must-not-be-snapshotted", json.dumps(snapshot))

        with (
            mock.patch.object(live_readers, "provider", return_value=nullcontext((
                vigilo, (client, "access-token", "Android 9.9.9-test")
            ))),
            mock.patch.object(vigilo, "fetch_snapshot", return_value=snapshot),
        ):
            items, complete = live_readers.list_live(self.tmp_path, "vigilo", "2026-08-20T00:00:00+02:00")
        self.assertTrue(complete)
        self.assertEqual(client.downloads, [])
        with (
            mock.patch.object(live_readers, "provider", return_value=nullcontext((
                vigilo, (client, "access-token", "Android 9.9.9-test")
            ))),
            mock.patch.object(live_readers, "get_with_client", return_value=items[0]),
        ):
            live_readers.download_live(
                self.tmp_path, "vigilo", items[0]["reference"],
                items[0]["attachments"][0]["attachment_id"],
                self.tmp_path / ".private" / "incoming",
            )
        self.assertEqual(
            [item["source_id"] for item in items],
            ["message-thread:thread-1", "news:post-1"],
        )
        self.assertEqual(items[0]["child"], "Ada Nord, Bo Sør")
        self.assertIn("Vi går på tur.", items[0]["body"])
        self.assertIn("Ta med sekk", items[0]["body"])
        self.assertEqual(items[0]["changed_at"], "2026-08-31T08:00:00+02:00")
        self.assertEqual(len(items[0]["attachments"]), 1)
        self.assertEqual(items[1]["body"], "Full tekst om møtet.")
        self.assertEqual(items[1]["event_start"], "2026-09-03T18:00:00+02:00")
        self.assertEqual(len(client.downloads), 1)
        self.assertIsNone(client.downloads[0][1])

        requested_paths = [
            urllib.parse.urlsplit(url).path for url, _token, _headers in client.gets
        ]
        self.assertTrue(all("read-status" not in path for path in requested_paths))
        self.assertTrue(all("viewing" not in path for path in requested_paths))
        self.assertTrue(all("reply" not in path for path in requested_paths))
        self.assertTrue(
            all(
                headers == {"appVersion": "Android 9.9.9-test"}
                for _url, _token, headers in client.gets
            )
        )

    def test_unknown_timestamp_is_not_in_window(self) -> None:
        cutoff = vigilo._parse_time("2026-08-20T00:00:00+02:00")
        self.assertIsNotNone(cutoff)
        assert cutoff is not None
        self.assertFalse(vigilo._in_window({"title": "undated"}, cutoff))

    def test_news_detail_rejects_missing_feed_summary(self):
        from familiekilder.source_adapter import SourceUnavailable

        client = FakeVigiloApi()
        with mock.patch.object(vigilo, "news_pages", return_value=iter([])):
            with self.assertRaisesRegex(SourceUnavailable, "sammendrag"):
                live_readers.get_with_client(
                    self.tmp_path, "vigilo", vigilo, (client, "token", "test-version"),
                    {"kind": "news", "id": "post-1", "child_ids": ["child-1"]},
                )

    def test_news_detail_finds_summary_on_later_feed_page(self):
        client = FakeVigiloApi()
        original_get = client.get_json
        cursors = []

        def get_json(url, **kwargs):
            parsed = urllib.parse.urlsplit(url)
            if parsed.path != "/api/news-feed":
                return original_get(url, **kwargs)
            cursors.append(urllib.parse.parse_qs(parsed.query)["lastDate"][0])
            if len(cursors) == 1:
                return [
                    {"id": f"newer-{i}", "type": "post", "postedTime": f"2026-09-0{i}T12:00:00Z"}
                    for i in (2, 1)
                ]
            return original_get(url, **kwargs)[:1]

        client.get_json = get_json
        with mock.patch.object(vigilo, "PAGE_SIZE", 2):
            item = live_readers.get_with_client(
                self.tmp_path, "vigilo", vigilo, (client, "token", "test-version"),
                {"kind": "news", "id": "post-1", "child_ids": ["child-1"]},
            )
        self.assertEqual(len(cursors), 2)
        self.assertEqual(cursors[1], "2026-09-01")
        self.assertEqual(item["event_start"], "2026-09-03T18:00:00+02:00")

    def test_older_item_edited_inside_window_is_included(self) -> None:
        cutoff = vigilo._parse_time("2026-08-20T00:00:00+02:00")
        self.assertIsNotNone(cutoff)
        assert cutoff is not None
        self.assertTrue(
            vigilo._thread_in_window(
                {
                    "lastMessage": {
                        "timeCreated": "2026-07-01T10:00:00+02:00"
                    },
                    "updatedAt": "2026-08-30T10:00:00+02:00",
                },
                cutoff,
            )
        )

    def test_attachment_url_cannot_be_an_authenticated_api_route(self) -> None:
        budget = vigilo.DownloadBudget()
        result = vigilo.download_attachments(
            iter(
                [
                    {
                        "id": "unsafe",
                        "name": "unsafe.txt",
                        "url": f"{vigilo.API_ROOT}api/messages/threads/delete",
                    }
                ]
            ),
            client=FakeVigiloApi(),
            token="must-not-be-sent",
            app_version="Android fixture",
            incoming=self.tmp_path / ".private" / "incoming",
            stable_id="unsafe",
            budget=budget,
        )
        self.assertEqual(result, [])
        self.assertEqual(budget.failed_items, {"unsafe"})

    def test_external_attachment_401_is_not_an_oauth_refresh_signal(self) -> None:
        client = FakeVigiloApi()
        client.download = mock.Mock(side_effect=ApiHTTPError(401))
        budget = vigilo.DownloadBudget()
        result = vigilo.download_attachments(
            iter(
                [
                    {
                        "id": "expired-link",
                        "name": "file.pdf",
                        "url": "https://files.example.test/expired.pdf",
                    }
                ]
            ),
            client=client,
            token="healthy-oauth-token",
            app_version="Android fixture",
            incoming=self.tmp_path / ".private" / "incoming",
            stable_id="expired-link",
            budget=budget,
        )
        self.assertEqual(result, [])
        self.assertEqual(budget.failed_items, {"expired-link"})
        self.assertIsNone(client.download.call_args.kwargs["bearer_token"])


    def test_message_pagination_stops(self) -> None:
        client = PagingApi()
        cutoff = vigilo._parse_time("2026-08-20T00:00:00+02:00")
        self.assertIsNotNone(cutoff)
        assert cutoff is not None
        with mock.patch.object(vigilo, "PAGE_SIZE", 2):
            values = list(
                vigilo.thread_pages(
                    client, "token", "Android test", "child", cutoff
                )
            )
        self.assertEqual([value["threadUid"] for value in values], ["one", "two"])
        self.assertEqual(client.pages, [None, "1"])


if __name__ == "__main__":
    unittest.main()
