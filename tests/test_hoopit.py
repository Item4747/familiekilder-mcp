"""Hoopit external API: reads, items, pacing, errors, version notices and answers."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from familiekilder import hoopit, hoopit_auth, live_hoopit, live_hoopit_reader
from familiekilder.action_ledger import ActionLedger
from familiekilder.api_transport import ApiHTTPError, ApiTransportError
from familiekilder.hoopit import (
    HoopitCooldownActive,
    PersistentRateLimiter,
    comment_tree,
    event_item,
    fetch_snapshot,
    post_item,
)
from familiekilder.hoopit_auth import HoopitScopeMissing, HoopitTokenRejected, rate_state_path
from familiekilder.live_config import Config
from familiekilder.live_readers import content_hash
from familiekilder.private_state import read_private_json
from familiekilder.source_adapter import SourceUnavailable
from familiekilder_testing.hoopit import (
    CUTOFF,
    OCCURRENCES,
    POSTS,
    TOKEN,
    FakeClock,
    FakeTransport,
    NoopLimiter,
    access,
    comment,
    comments_path,
    occurrence,
    page,
    person,
    post,
    response_path,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
OPERATION = "8c3c9a52-4a5e-4c43-9d1a-2a7f0d6c1b11"


class HoopitTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.transport = FakeTransport()
        self.limiter = NoopLimiter()
        self.client = hoopit.Client(access(), self.transport, self.limiter, root=self.root)


class ReadTests(HoopitTestCase):
    def test_snapshot_reads_recent_posts_with_comments_and_the_occurrence_window(self):
        data = fetch_snapshot(self.client, CUTOFF, now=NOW)
        self.assertEqual([record["post"]["id"] for record in data["posts"]], [1001])
        paths = [
            url.split("?")[0].removeprefix("https://api.hoopit.io")
            for url, _, _ in self.transport.gets
        ]
        # The old post is outside the comment lookback, so its comments are not read.
        self.assertEqual(paths, [POSTS, comments_path(1001), OCCURRENCES])
        query = FakeTransport.query(self.transport.gets[-1][0])
        self.assertEqual(query["from_date"], "2026-08-17T12:00:00Z")
        self.assertEqual(query["to_date"], "2026-12-29T12:00:00Z")
        for _, bearer, headers in self.transport.gets:
            self.assertEqual(bearer, TOKEN)
            self.assertEqual(headers["Version"], "1")
            self.assertNotIn("Authorization", headers)
        self.assertEqual(self.limiter.before, 3)

    def test_a_long_occurrence_window_is_read_in_parts_of_at_most_366_days(self):
        # Hoopit answers 400 when from_date and to_date are more than 366 days apart.
        self.transport.values[comments_path(1000)] = page([])
        data = fetch_snapshot(self.client, "2025-03-01T00:00:00+00:00", now=NOW)
        windows = [
            FakeTransport.query(url)
            for url, _, _ in self.transport.gets
            if url.split("?")[0].endswith(OCCURRENCES)
        ]
        self.assertGreater(len(windows), 1)
        self.assertEqual(windows[0]["from_date"], "2025-03-01T00:00:00Z")
        self.assertEqual(windows[-1]["to_date"], "2026-12-29T12:00:00Z")
        for window, following in zip(windows, windows[1:]):
            self.assertEqual(window["to_date"], following["from_date"])
        for window in windows:
            start, end = (
                datetime.fromisoformat(window[key].replace("Z", "+00:00"))
                for key in ("from_date", "to_date")
            )
            self.assertLessEqual((end - start).days, 366)
        # The same occurrence in two parts is kept once.
        keys = [(row["event_id"], row["id"]) for row in data["events"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_a_post_item_has_the_text_comments_and_a_day_in_its_reference(self):
        record = {
            "post": post(1001, "2026-08-30T10:00:00Z"),
            "comments": comment_tree(
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
        }
        item = live_hoopit_reader._post(hoopit, record)
        self.assertEqual(item["source_id"], "post:1001")
        self.assertEqual(item["reference"], {"kind": "post", "id": "1001", "day": "2026-08-30"})
        self.assertEqual(item["title"], "Treningsinfo")
        self.assertEqual(item["sender"], "Lagleder Lund")
        self.assertEqual(item["topic"], "J2014")
        self.assertEqual(item["changed_at"], "2026-08-31T08:20:00+00:00")
        self.assertEqual(item["source_url"], "https://app.hoopit.io/app/posts/1001")
        self.assertEqual(
            item["body"],
            "Ta med drikkeflaske.\n\nKommentarer:\n"
            "- Ada Nord (2026-08-31T08:10:00+00:00): Oppmøte 14:45.\n"
            "  ↳ Per Sør (2026-08-31T08:20:00+00:00): Takk!",
        )
        self.assertNotIn("content_complete", item)

    def test_an_older_post_with_a_new_comment_is_included(self):
        values = {
            POSTS: page([post(1001, "2026-08-10T10:00:00Z")]),
            comments_path(1001): page([comment(2001, "2026-08-20T10:00:00Z", "Ny kommentar")]),
            OCCURRENCES: page([]),
        }
        client = hoopit.Client(access(), FakeTransport(values), NoopLimiter(), root=self.root)
        data = fetch_snapshot(client, CUTOFF, now=NOW)
        self.assertEqual(len(data["posts"]), 1)
        self.assertEqual(post_item(data["posts"][0])["changed_at"], "2026-08-20T10:00:00+00:00")

    def test_paging_stops_after_a_page_older_than_the_lookback(self):
        def posts(url):
            number = int(FakeTransport.query(url)["page"])
            stamp = "2026-08-30T10:00:00Z" if number == 1 else "2026-06-01T10:00:00Z"
            return page([post(1000 + number, stamp)], has_next=True)

        values = {
            POSTS: posts,
            comments_path(1001): page([]),
            OCCURRENCES: page([]),
        }
        transport = FakeTransport(values)
        client = hoopit.Client(access(), transport, NoopLimiter(), root=self.root)
        fetch_snapshot(client, CUTOFF, now=NOW)
        pages = [
            FakeTransport.query(url).get("page") for url, _, _ in transport.gets if POSTS in url
        ]
        self.assertEqual(pages, ["1", "2"])

    def test_a_page_cap_gives_a_partial_read_and_repeated_pages_fail(self):
        values = {
            POSTS: lambda url: page(
                [post(int(FakeTransport.query(url)["page"]), "2026-08-30T10:00:00Z")], has_next=True
            ),
            OCCURRENCES: self.transport.values[OCCURRENCES],
        }
        values.update({comments_path(n): page([]) for n in range(1, 30)})
        client = hoopit.Client(access(), FakeTransport(values), NoopLimiter(), root=self.root)
        with self.assertLogs("familiekilder.hoopit", "WARNING"):
            data = fetch_snapshot(client, CUTOFF, now=NOW)
        self.assertEqual(len(data["posts"]), hoopit.MAX_PAGES_PER_FEED)
        self.assertEqual(len(data["events"]), 1)
        self.assertIn("for mange sider", data["limits"][0])
        with patch.object(hoopit, "fetch_snapshot", return_value=data):
            items, complete = live_hoopit_reader.list_items(self.root, hoopit, client, CUTOFF)
        self.assertFalse(complete)
        self.assertTrue(items)
        repeated = {
            POSTS: page([post(1, "2026-08-30T10:00:00Z")], has_next=True),
            comments_path(1): page([]),
        }
        client = hoopit.Client(access(), FakeTransport(repeated), NoopLimiter(), root=self.root)
        with self.assertRaises(SourceUnavailable):
            fetch_snapshot(client, CUTOFF, now=NOW)

    def test_comment_tree_keeps_orphans_and_nests_deep_replies_under_their_root(self):
        tree = comment_tree(
            [
                comment(1, "2026-08-31T08:00:00Z", "Rot"),
                comment(2, "2026-08-31T08:01:00Z", "Svar", parent=1),
                comment(3, "2026-08-31T08:02:00Z", "Svar på svar", parent=2),
                comment(4, "2026-08-31T08:03:00Z", "Foreldreløs", parent=99),
            ]
        )
        self.assertEqual([row["id"] for row in tree], [1, 4])
        self.assertEqual([row["id"] for row in tree[0]["_replies"]], [2, 3])

    def test_event_items_read_answers_and_keep_their_change_time_until_they_change(self):
        data = fetch_snapshot(self.client, CUTOFF, now=NOW)
        first = live_hoopit_reader._events(self.root, hoopit, data["events"])[0]
        self.assertEqual(first["source_id"], "occurrence:4001:3001")
        self.assertEqual(
            first["reference"],
            {"kind": "event", "id": "4001", "occurrence_id": "3001", "day": "2026-09-01"},
        )
        self.assertEqual(first["action_required"], "Svar på aktivitetsinvitasjonen")
        self.assertIn("Oppmøte: 16:45", first["body"])
        self.assertIn("- Oliver Fjell: har ikke svart", first["body"])
        self.assertEqual(first["attachments"], [])

        def observe(value, at):
            items = [event_item(value)]
            hoopit.observed_changed_at(self.root, items, now=at)
            return items[0]

        seen = observe(data["events"][0], NOW)
        again = observe(data["events"][0], datetime(2026, 9, 1, 9, tzinfo=UTC))
        self.assertEqual(again["changed_at"], seen["changed_at"])
        self.assertEqual(content_hash(again), content_hash(seen))
        answered = dict(
            data["events"][0],
            responses=[{"user": person(901, "Oliver Fjell"), "response": "accepted"}],
        )
        changed = observe(answered, datetime(2026, 9, 1, 10, tzinfo=UTC))
        self.assertEqual(changed["changed_at"], "2026-09-01T10:00:00+00:00")
        self.assertIsNone(changed["action_required"])

    def test_a_refused_comment_list_keeps_the_post_and_the_token(self):
        values = dict(self.transport.values)
        values[comments_path(1001)] = ApiHTTPError(403)
        client = hoopit.Client(access(), FakeTransport(values), NoopLimiter(), root=self.root)
        with self.assertLogs("familiekilder.hoopit", "WARNING"):
            data = fetch_snapshot(client, CUTOFF, now=NOW)
        item = post_item(data["posts"][0])
        self.assertIs(item["content_complete"], False)
        self.assertTrue(data["limits"])
        self.assertIsNone(hoopit_auth.auth_status(self.root)["missing_scope"])
        self.assertFalse(hoopit_auth.token_state_path(self.root).exists())

    def test_get_item_stops_paging_at_the_post_day_and_reports_not_found(self):
        def posts(url):
            number = int(FakeTransport.query(url)["page"])
            return page(
                [post(2000 - number, f"2026-08-{30 - number:02d}T10:00:00Z")], has_next=True
            )

        transport = FakeTransport({POSTS: posts})
        client = hoopit.Client(access(), transport, NoopLimiter(), root=self.root)
        with self.assertRaisesRegex(SourceUnavailable, "Fant ikke innlegget"):
            live_hoopit_reader.get_item(
                self.root, hoopit, client, {"kind": "post", "id": "1001", "day": "2026-08-26"}
            )
        # Page 6 (24 August) is the first one older than a day before the post day.
        self.assertEqual(len(transport.gets), 6)
        transport.gets.clear()
        with self.assertRaisesRegex(SourceUnavailable, "Fant ikke innlegget"):
            live_hoopit_reader.get_item(self.root, hoopit, client, {"kind": "post", "id": "1001"})
        self.assertEqual(len(transport.gets), hoopit.MAX_PAGES_PER_FEED)

    def test_get_item_finds_a_post_and_an_occurrence(self):
        item = live_hoopit_reader.get_item(
            self.root, hoopit, self.client, {"kind": "post", "id": "1001"}
        )
        self.assertEqual(item["source_id"], "post:1001")
        self.transport.gets.clear()
        reference = {"kind": "event", "id": "4001", "occurrence_id": "3001", "day": "2026-09-01"}
        item = live_hoopit_reader.get_item(self.root, hoopit, self.client, reference)
        self.assertEqual(item["source_id"], "occurrence:4001:3001")
        query = FakeTransport.query(self.transport.gets[0][0])
        self.assertEqual(
            (query["from_date"], query["to_date"]), ("2026-08-30T22:00:00Z", "2026-09-02T22:00:00Z")
        )
        with self.assertRaises(SourceUnavailable):
            live_hoopit_reader.get_item(
                self.root, hoopit, self.client, {**reference, "occurrence_id": "9"}
            )

    def test_attachments_and_unknown_kinds_are_refused(self):
        with self.assertRaises(Exception) as raised:
            live_hoopit_reader.download(self.root, hoopit, self.client, {}, "a", {}, self.root)
        self.assertIn("vedlegg", str(raised.exception))
        self.assertEqual(
            live_hoopit_reader.reference_values(
                {"kind": "event", "id": "1", "occurrence_id": "2", "day": "2026-09-01"}
            ),
            ["event", "1", "2", "2026-09-01"],
        )


class ErrorTests(HoopitTestCase):
    def connect(self):
        hoopit_auth.connect_token(
            self.root,
            TOKEN,
            transport=FakeTransport({POSTS: page([]), OCCURRENCES: page([])}),
            rate_limiter=NoopLimiter(),
        )

    def test_401_marks_the_token_rejected_and_403_names_the_scope(self):
        self.connect()
        client = hoopit.Client(
            access(), FakeTransport({POSTS: ApiHTTPError(401)}), NoopLimiter(), root=self.root
        )
        with self.assertRaises(HoopitTokenRejected):
            client.get("posts", {"page": 1})
        self.assertEqual(
            hoopit_auth.auth_status(self.root)["refresh_block_reason"], "token-rejected"
        )
        hoopit_auth.connect_token(
            self.root,
            TOKEN,
            replace_existing=True,
            transport=FakeTransport({POSTS: page([]), OCCURRENCES: page([])}),
            rate_limiter=NoopLimiter(),
        )
        client = hoopit.Client(
            access(), FakeTransport({OCCURRENCES: ApiHTTPError(403)}), NoopLimiter(), root=self.root
        )
        with self.assertRaises(HoopitScopeMissing):
            client.get("occurrences", {"page": 1, "from_date": "x", "to_date": "y"})
        self.assertEqual(hoopit_auth.auth_status(self.root)["missing_scope"], "events:read")

    def test_429_5xx_and_network_failures_store_a_pause_and_are_never_retried(self):
        for error in (
            ApiHTTPError(429, retry_after_seconds=120),
            ApiHTTPError(503),
            ApiTransportError("nede"),
        ):
            with self.subTest(error=type(error).__name__):
                root = self.root / str(id(error))
                root.mkdir(mode=0o700)
                clock = FakeClock()
                limiter = PersistentRateLimiter(
                    root, clock=clock, sleeper=clock.sleep, jitter=lambda _a, _b: 0
                )
                transport = FakeTransport({POSTS: error})
                client = hoopit.Client(access(), transport, limiter, root=root)
                with self.assertRaises(SourceUnavailable):
                    client.get("posts", {"page": 1})
                with self.assertRaises(HoopitCooldownActive):
                    client.get("posts", {"page": 2})
                self.assertEqual(len(transport.gets), 1)

    def test_pacing_is_one_second_and_honors_retry_after(self):
        clock = FakeClock()
        limiter = PersistentRateLimiter(
            self.root, clock=clock, sleeper=clock.sleep, jitter=lambda _a, _b: 0
        )
        limiter.before_request()
        clock.value += 0.25
        limiter.before_request()
        self.assertEqual(clock.sleeps, [0.75])
        limiter.record_http_failure(429, 3600)
        state = read_private_json(rate_state_path(self.root))
        self.assertEqual(state["cooldown_until"], clock.value + 3600)
        with self.assertRaises(SourceUnavailable):
            PersistentRateLimiter(self.root, minimum_interval=0.5)

    def test_version_notices_are_stored_and_shown_until_hoopit_stops_sending_them(self):
        self.connect()
        transport = FakeTransport(headers={"Deprecated-Version": "1"})
        client = hoopit.Client(access(), transport, NoopLimiter(), root=self.root)
        with self.assertLogs("familiekilder.hoopit", "WARNING"):
            client.get("posts", {"page": 1})
        warning = hoopit_auth.auth_status(self.root)["api_version_warning"]
        self.assertIn("fases ut", warning)
        self.assertIn("innlegg", warning)
        transport.headers = {}
        client.get("posts", {"page": 2})
        self.assertIsNone(hoopit_auth.auth_status(self.root)["api_version_warning"])
        rejected = FakeTransport({POSTS: ApiHTTPError(400, headers={"invalid-version": "1"})})
        client = hoopit.Client(access(), rejected, NoopLimiter(), root=self.root)
        with (
            self.assertRaises(SourceUnavailable),
            self.assertLogs("familiekilder.hoopit", "WARNING"),
        ):
            client.get("posts", {"page": 3})
        self.assertIn(
            "kjenner ikke versjon 1", hoopit_auth.auth_status(self.root)["api_version_warning"]
        )


class ParticipationTests(HoopitTestCase):
    def runtime(self, writes=frozenset({"participation"})):
        config = Config(self.root, {"hoopit": writes})
        ledgers = []

        def ledger():
            from contextlib import contextmanager

            @contextmanager
            def scope():
                value = ActionLedger(self.root)
                ledgers.append(value)
                try:
                    yield value
                finally:
                    value.close()

            return scope()

        return SimpleNamespace(config=config, ledger=ledger)

    def provider(self):
        from contextlib import contextmanager

        @contextmanager
        def scope(_root, _source):
            yield hoopit, self.client

        return patch.object(live_hoopit, "provider", scope)

    def future(self, **fields):
        return occurrence(3001, 4001, "2099-09-01T15:00:00Z", "2099-09-01T16:30:00Z", **fields)

    def test_context_lists_the_familys_answers(self):
        self.transport.values[OCCURRENCES] = page([self.future()])
        with self.provider():
            context = live_hoopit.participation_context(
                self.runtime(), "4001", "3001", "2099-09-01"
            )
        self.assertEqual(
            context["participants"],
            [{"person_id": "901", "name": "Oliver Fjell", "status": "unanswered"}],
        )
        self.assertEqual(
            context["decline_reasons"], ["illness", "injury", "other_training", "other_reason"]
        )

    def test_an_answer_is_sent_once_and_read_back(self):
        state = {"response": "not_answered"}

        def occurrences(_url):
            return page(
                [
                    self.future(
                        responses=[
                            {"user": person(901, "Oliver Fjell"), "response": state["response"]}
                        ]
                    )
                ]
            )

        self.transport.values[OCCURRENCES] = occurrences
        self.transport.on_patch = lambda url, payload: state.update(response=payload["response"])
        with self.provider():
            result = live_hoopit.set_participation(
                self.runtime(), OPERATION, "4001", "3001", "901", False, "illness", "2099-09-01"
            )
            again = live_hoopit.set_participation(
                self.runtime(), OPERATION, "4001", "3001", "901", False, "illness", "2099-09-01"
            )
        self.assertEqual(result["status"], "verified")
        self.assertEqual(again["status"], "verified")
        self.assertEqual(len(self.transport.patches), 1)
        url, payload, bearer, headers = self.transport.patches[0]
        self.assertEqual(url, "https://api.hoopit.io" + response_path(4001, 3001, 901))
        self.assertEqual(payload, {"response": "declined", "response_reason": "illness"})
        self.assertEqual((bearer, headers["Version"]), (TOKEN, "1"))

    def test_refusals_happen_before_any_write(self):
        self.transport.values[OCCURRENCES] = page([self.future(cancelled=True)])
        with self.provider():
            for arguments, text in (
                (("901", True, "illness"), "bare ved avmelding"),
                (("901", False, "sick"), "illness"),
                (("902", True, None), "ikke invitert"),
                (("901", True, None), "avlyst"),
            ):
                with self.subTest(text=text), self.assertRaises(Exception) as raised:
                    live_hoopit.set_participation(
                        self.runtime(), str(uuid.uuid4()), "4001", "3001", *arguments
                    )
                self.assertIn(text, str(raised.exception))
            with self.assertRaises(Exception):
                live_hoopit.set_participation(
                    self.runtime(frozenset()), str(uuid.uuid4()), "4001", "3001", "901", True
                )
        self.assertEqual(self.transport.patches, [])

    def test_a_missing_respond_scope_is_reported_and_not_retried(self):
        self.transport.values[OCCURRENCES] = page([self.future()])
        self.transport.patch_result = ApiHTTPError(403)
        with self.provider():
            result = live_hoopit.set_participation(
                self.runtime(), str(uuid.uuid4()), "4001", "3001", "901", True
            )
        self.assertEqual(result["status"], "unknown")
        self.assertIn("«Svare på invitasjoner for deg selv og barna dine»", result["receipt"]["detail"])
        self.assertEqual(len(self.transport.patches), 1)
        # A missing answer scope does not block reading.
        self.assertIsNone(
            json.loads(json.dumps(hoopit_auth.auth_status(self.root)))["missing_scope"]
        )


if __name__ == "__main__":
    unittest.main()
