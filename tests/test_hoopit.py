from __future__ import annotations

import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from contextlib import nullcontext
from familiekilder import live_readers
from familiekilder import hoopit
from familiekilder.api_transport import ApiHTTPError
from familiekilder.hoopit import (
    HoopitCooldownActive,
    HoopitRateLimited,
    PersistentRateLimiter,
    RequestBudget,
    RequestBudgetExceeded,
    fetch_snapshot,
)
from familiekilder.hoopit_auth import rate_state_path
from familiekilder.private_state import read_private_json
from familiekilder.source_adapter import SourceLoginRequired, SourceUnavailable
from familiekilder_testing.hoopit import (
    CUTOFF,
    FakeClock,
    NoopLimiter,
    _page,
    _responses,
    FakeTransport,
    _access,
)


class HoopitAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.transport = FakeTransport(_responses())
        self.limiter = NoopLimiter()
        self.client = hoopit.AuthorizedClient(
            _access(), self.transport, self.limiter
        )

    def live_items(self, snapshot, client):
        with (
            patch.object(live_readers, "provider", return_value=nullcontext((hoopit, client))),
            patch.object(hoopit, "fetch_snapshot", return_value=snapshot),
        ):
            items, complete = live_readers.list_live(self.root, "hoopit", CUTOFF)
        self.assertTrue(complete)
        return items

    def download_item(self, client, item, target):
        with (
            patch.object(live_readers, "provider", return_value=nullcontext((hoopit, client))),
            patch.object(live_readers, "get_with_client", return_value=item),
        ):
            return live_readers.download_live(
                self.root, "hoopit", item["reference"],
                item["attachments"][0]["attachment_id"], target,
            )

    def test_snapshot_and_normalization_cover_public_read_contract(self) -> None:
        snapshot = fetch_snapshot(self.client, CUTOFF)
        incoming = self.root / ".private" / "incoming" / "run" / "hoopit"
        (self.root / ".private").mkdir(mode=0o700)
        items = self.live_items(snapshot, self.client)
        self.assertEqual(self.transport.downloads, [])
        for item in items:
            if item["attachments"]:
                self.download_item(self.client, item, incoming)
        by_id = {item["source_id"]: item for item in items}
        self.assertEqual(
            set(by_id),
            {
                "notification:n1",
                "post:p1",
                "post:p2",
                "occurrence:e1:o1",
            },
        )
        self.assertIn("Oppmøte 14:45", by_id["post:p1"]["body"])
        self.assertIn("↳ Per Sør", by_id["post:p1"]["body"])
        self.assertIn("Vi bruker inngang B", by_id["occurrence:e1:o1"]["body"])
        self.assertEqual(by_id["occurrence:e1:o1"]["type"], "event")
        self.assertEqual(by_id["occurrence:e1:o1"]["location"], "Hallen")
        self.assertEqual(by_id["occurrence:e1:o1"]["deadline"], "2026-08-31")
        self.assertEqual(by_id["occurrence:e1:o1"]["action_required"], "Svar på aktivitetsinvitasjonen")
        self.assertEqual(len(by_id["post:p1"]["attachments"]), 1)
        self.assertEqual(len(by_id["occurrence:e1:o1"]["attachments"]), 1)

        self.assertTrue(self.transport.gets)
        for url, bearer, headers in self.transport.gets:
            self.assertTrue(url.startswith("https://api.hoopit.io/app/"))
            self.assertIsNone(bearer)
            self.assertEqual(headers["Authorization"], "JWT firebase-secret")
            self.assertNotIn("X-Firebase-AppCheck", headers)
            self.assertIn("version=", headers["Accept"])
            self.assertNotIn("bulk-update", url)
            self.assertNotIn("stream", url.lower())
        for _url, _path, bearer, headers in self.transport.downloads:
            self.assertIsNone(bearer)
            self.assertEqual(headers, {})

    def test_recent_notification_preserves_old_post_and_new_comment(self) -> None:
        responses = {
            "/app/users/current/notifications/": _page(
                [
                    {
                        "id": "n-old",
                        "timestamp": "2026-08-31T08:00:00Z",
                        "data": {"title": "Kommentar", "EXTRA_POST_ID": "old"},
                    }
                ]
            ),
            "/app/users/current/posts/": _page([]),
            "/app/users/current/events/": _page([]),
            "/app/posts/old/": {
                "id": "old",
                "title": "Gammel",
                "text": "Historikk",
                "created_at": "2026-01-01T08:00:00Z",
                "updated_at": "2026-01-01T08:00:00Z",
                "comment_count": 1,
                "attachments": [],
            },
            "/app/posts/old/comments/": _page(
                [
                    {
                        "id": "fresh-comment",
                        "timestamp": "2026-08-31T08:01:00Z",
                        "comment": "Ny kommentar i gammel tråd",
                        "poster_name": "Lagleder",
                        "reply_count": 0,
                    }
                ]
            ),
        }
        client = hoopit.AuthorizedClient(_access(), FakeTransport(responses), NoopLimiter())
        snapshot = fetch_snapshot(client, CUTOFF)
        self.assertEqual(len(snapshot["notifications"]), 1)
        self.assertEqual(len(snapshot["posts"]), 1)
        (self.root / ".private").mkdir(mode=0o700)
        items = self.live_items(snapshot, client)
        post = next(item for item in items if item["source_id"] == "post:old")
        self.assertIn("Ny kommentar i gammel tråd", post["body"])
        self.assertEqual(post["changed_at"], "2026-08-31T08:01:00+00:00")

    def test_recent_reaction_notification_does_not_create_false_post_change(self) -> None:
        responses = {
            "/app/users/current/notifications/": _page(
                [
                    {
                        "id": "n-reaction",
                        "timestamp": "2026-08-31T08:00:00Z",
                        "data": {"title": "Reaksjon", "EXTRA_POST_ID": "old"},
                    }
                ]
            ),
            "/app/users/current/posts/": _page([]),
            "/app/users/current/events/": _page([]),
            "/app/posts/old/": {
                "id": "old",
                "title": "Gammel",
                "text": "Uendret historikk",
                "created_at": "2026-01-01T08:00:00Z",
                "updated_at": "2026-01-01T08:00:00Z",
                "comment_count": 0,
                "attachments": [],
            },
        }
        client = hoopit.AuthorizedClient(_access(), FakeTransport(responses), NoopLimiter())
        snapshot = fetch_snapshot(client, CUTOFF)
        self.assertEqual(len(snapshot["notifications"]), 1)
        self.assertEqual(snapshot["posts"], [])

    def test_recent_event_notification_preserves_old_changed_activity(self) -> None:
        responses = {
            "/app/users/current/notifications/": _page(
                [
                    {
                        "id": "n-event",
                        "timestamp": "2026-08-31T08:00:00Z",
                        "data": {
                            "title": "Aktivitet avlyst",
                            "EXTRA_EVENT_ID": "e-old",
                            "EXTRA_OCCURRENCE_ID": "o-old",
                        },
                    }
                ]
            ),
            "/app/users/current/posts/": _page([]),
            "/app/users/current/events/": _page([]),
            "/app/events/e-old/occurrences/o-old/": {
                "id": "o-old",
                "event": "e-old",
                "title": "Tidligere kamp",
                "description": "Avlyst",
                "start": "2026-01-01T08:00:00Z",
                "end": "2026-01-01T09:00:00Z",
                "updated_at": "2026-08-31T08:00:00Z",
                "cancelled": True,
                "attachments": [],
            },
            "/app/events/e-old/occurrences/o-old/comments/": _page([]),
            "/app/events/e-old/occurrences/o-old/posts/": _page([]),
        }
        client = hoopit.AuthorizedClient(_access(), FakeTransport(responses), NoopLimiter())
        snapshot = fetch_snapshot(client, CUTOFF)
        items = self.live_items(snapshot, client)
        event = next(item for item in items if item["source_id"].startswith("occurrence:"))
        self.assertEqual(event["changed_at"], "2026-08-31T08:00:00+00:00")

    def test_missing_feed_timestamp_fails_instead_of_silent_drop(self) -> None:
        responses = {
            "/app/users/current/notifications/": _page([{"id": "n1", "data": {"title": "x"}}]),
            "/app/users/current/posts/": _page([]),
            "/app/users/current/events/": _page([]),
        }
        client = hoopit.AuthorizedClient(_access(), FakeTransport(responses), NoopLimiter())
        with self.assertRaises(SourceUnavailable):
            fetch_snapshot(client, CUTOFF)

    def test_declared_comment_counts_must_match_complete_thread(self) -> None:
        responses = {
            "/app/users/current/notifications/": _page([]),
            "/app/users/current/posts/": _page(
                [
                    {
                        "id": "p-count",
                        "text": "Melding",
                        "created_at": "2026-08-31T08:00:00Z",
                        "updated_at": "2026-08-31T08:00:00Z",
                        "comment_count": 2,
                        "attachments": [],
                    }
                ]
            ),
            "/app/users/current/events/": _page([]),
            "/app/posts/p-count/comments/": _page(
                [
                    {
                        "id": "only-one",
                        "timestamp": "2026-08-31T08:01:00Z",
                        "comment": "Bare en",
                        "reply_count": 0,
                    }
                ]
            ),
        }
        client = hoopit.AuthorizedClient(_access(), FakeTransport(responses), NoopLimiter())
        with self.assertRaisesRegex(SourceUnavailable, "alle oppgitte kommentarer"):
            fetch_snapshot(client, CUTOFF)

    def test_attachment_404_preserves_item_but_429_stops_immediately(self) -> None:
        snapshot = {
            "notifications": [],
            "events": [],
            "posts": [
                {
                    "post": {
                        "id": "p1",
                        "title": "Fil",
                        "text": "Melding",
                        "created_at": "2026-08-31T08:00:00Z",
                        "comment_count": 0,
                        "attachments": [
                            {
                                "id": "a1",
                                "name": "x.pdf",
                                "url": "https://files.example.test/x",
                            }
                        ],
                    },
                    "comments": [],
                }
            ],
        }
        (self.root / ".private").mkdir(mode=0o700)
        incoming = self.root / ".private" / "incoming"

        missing = FakeTransport()
        missing.download_error = ApiHTTPError(404)
        client = hoopit.AuthorizedClient(_access(), missing, NoopLimiter())
        items = self.live_items(snapshot, client)
        self.assertEqual(items[0]["body"], "Melding")
        self.assertEqual(len(items[0]["attachments"]), 1)
        self.assertEqual(missing.downloads, [])
        with self.assertRaises(hoopit.HoopitItemUnavailable):
            self.download_item(client, items[0], incoming)
        self.assertEqual(len(missing.downloads), 1)

        throttled = FakeTransport()
        throttled.download_error = ApiHTTPError(429, retry_after_seconds=3600)
        client = hoopit.AuthorizedClient(_access(), throttled, NoopLimiter())
        with self.assertRaises(HoopitRateLimited):
            self.download_item(client, items[0], incoming)
        self.assertEqual(len(throttled.downloads), 1)

    def test_persistent_limiter_paces_and_honors_full_retry_after(self) -> None:
        clock = FakeClock()
        limiter = PersistentRateLimiter(
            self.root, clock=clock, sleeper=clock.sleep, jitter=lambda _a, _b: 0
        )
        limiter.before_request()
        limiter.record_success()
        clock.value += 0.5
        limiter.before_request()
        self.assertEqual(clock.sleeps, [1.5])
        limiter.record_http_failure(429, 48 * 60 * 60)
        state = read_private_json(rate_state_path(self.root))
        self.assertEqual(state["retry_after_seconds"], 48 * 60 * 60)
        self.assertEqual(state["cooldown_until"], clock.value + 48 * 60 * 60)
        with self.assertRaises(HoopitCooldownActive):
            limiter.before_request()

    def test_429_and_5xx_are_never_retried_in_same_run(self) -> None:
        for error in (
            ApiHTTPError(429, retry_after_seconds=120),
            ApiHTTPError(503),
        ):
            with self.subTest(status=error.status_code):
                root = self.root / str(error.status_code)
                root.mkdir(mode=0o700)
                clock = FakeClock()
                limiter = PersistentRateLimiter(
                    root, clock=clock, sleeper=clock.sleep, jitter=lambda _a, _b: 0
                )
                transport = FakeTransport(
                    {"/app/users/current/posts/": error}
                )
                client = hoopit.AuthorizedClient(_access(), transport, limiter)
                with self.assertRaises(SourceUnavailable):
                    client.get("/app/users/current/posts/", page=1)
                self.assertEqual(len(transport.gets), 1)
                with self.assertRaises(HoopitCooldownActive):
                    client.get("/app/users/current/posts/", page=1)
                self.assertEqual(len(transport.gets), 1)

    def test_total_budget_wrapper_counts_before_network(self) -> None:
        delegate = NoopLimiter()
        budget = RequestBudget(maximum=1)
        wrapped = hoopit.BudgetedRateLimiter(delegate, budget)
        wrapped.before_request()
        with self.assertRaises(RequestBudgetExceeded):
            wrapped.before_request()
        self.assertEqual(delegate.before, 1)

    def test_page_cap_fails_closed(self) -> None:
        responses = {
            "/app/users/current/notifications/": lambda url: _page(
                [{"id": urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["page"][0],
                  "timestamp": CUTOFF}], has_next=True
            ),
        }
        transport = FakeTransport(responses)
        client = hoopit.AuthorizedClient(_access(), transport, NoopLimiter())
        with self.assertRaises(RequestBudgetExceeded):
            fetch_snapshot(client, CUTOFF)
        self.assertEqual(len(transport.gets), hoopit.MAX_PAGES_PER_FEED)

    def test_recent_edit_on_page_eleven_is_not_lost_after_old_pages(self) -> None:
        def posts(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertEqual(query["pagination_count"], ["false"])
            page = int(query["page"][0])
            return _page([{
                "id": f"p{page}", "created_at": "2025-01-01T00:00:00Z",
                "updated_at": CUTOFF if page == 11 else "2025-01-01T00:00:00Z",
                "comment_count": 0,
            }], has_next=page < 11)

        transport = FakeTransport({
            "/app/users/current/notifications/": _page([]),
            "/app/users/current/posts/": posts,
            "/app/users/current/events/": _page([]),
        })
        result = fetch_snapshot(hoopit.AuthorizedClient(_access(), transport, NoopLimiter()), CUTOFF)
        self.assertEqual([p["post"]["id"] for p in result["posts"]], ["p11"])
        self.assertEqual(len(transport.gets), 13)

    def test_repeated_or_empty_nonfinal_page_cannot_report_success(self) -> None:
        for rows, expected_calls in (([], 1), ([{"id": "n1", "timestamp": CUTOFF}], 2)):
            with self.subTest(rows=rows):
                transport = FakeTransport({
                    "/app/users/current/notifications/": _page(rows, has_next=True),
                })
                with self.assertRaises(SourceUnavailable):
                    fetch_snapshot(hoopit.AuthorizedClient(_access(), transport, NoopLimiter()), CUTOFF)
                self.assertEqual(len(transport.gets), expected_calls)


    def _simple_feeds(self):
        return {
            "/app/users/current/notifications/": _page([{
                "id": "n1", "timestamp": "2026-08-31T08:00:00Z", "data": {"title": "Beskjed"},
            }]),
            "/app/users/current/posts/": _page([{
                "id": "p1", "created_at": "2026-08-31T08:00:00Z",
                "comment_count": 0, "text": "Ta med vann.",
            }]),
            "/app/users/current/events/": _page([]),
        }


    def test_direct_gate_refreshes_once_and_reads_once(self) -> None:
        transport = FakeTransport(
            {"/app/users/current/notifications/": _page([])}
        )
        with (
            patch("familiekilder.hoopit._invalidate_direct_access_proof") as invalidate,
            patch("familiekilder.hoopit.refresh_session") as refresh,
            patch("familiekilder.hoopit.session_for_use", return_value=_access()) as session,
            patch("familiekilder.hoopit._record_direct_access_proof") as proof,
        ):
            result = hoopit.verify_direct_access(
                self.root,
                transport=transport,
                rate_limiter=self.limiter,
                at=1_000.0,
            )

        refresh.assert_called_once_with(
            self.root.resolve(),
            transport=None,
            verification_transport=None,
            rate_limiter=self.limiter,
            at=1_000.0,
        )
        invalidate.assert_called_once_with(self.root.resolve())
        session.assert_called_once_with(
            self.root.resolve(), rate_limiter=self.limiter, at=1_000.0
        )
        proof.assert_called_once_with(self.root.resolve(), _access(), at=1_000.0)
        self.assertEqual(len(transport.gets), 1)
        self.assertEqual(
            result,
            {
                "firebase_refresh_verified": True,
                "reading_api_verified": True,
                "app_check_required": False,
                "direct_access_proof_recorded": True,
            },
        )

    def test_direct_gate_fails_closed_on_fresh_token_rejection(self) -> None:
        transport = FakeTransport(
            {"/app/users/current/notifications/": ApiHTTPError(403)}
        )
        with (
            patch("familiekilder.hoopit._invalidate_direct_access_proof") as invalidate,
            patch("familiekilder.hoopit.refresh_session"),
            patch("familiekilder.hoopit.session_for_use", return_value=_access()),
            patch("familiekilder.hoopit._record_direct_access_proof") as proof,
            patch("familiekilder.hoopit._record_direct_access_rejection") as rejection,
            self.assertRaisesRegex(hoopit.HoopitReadAccessDenied, "mulig App Check"),
        ):
            hoopit.verify_direct_access(
                self.root,
                transport=transport,
                rate_limiter=self.limiter,
                at=1_000.0,
            )
        self.assertEqual(len(transport.gets), 1)
        invalidate.assert_called_once_with(self.root.resolve())
        proof.assert_not_called()
        rejection.assert_called_once_with(self.root.resolve(), _access(), at=1_000.0)

    def test_direct_gate_does_not_record_proof_for_invalid_read_payload(self) -> None:
        transport = FakeTransport(
            {"/app/users/current/notifications/": {"results": []}}
        )
        with (
            patch("familiekilder.hoopit._invalidate_direct_access_proof") as invalidate,
            patch("familiekilder.hoopit.refresh_session") as refresh,
            patch("familiekilder.hoopit.session_for_use", return_value=_access()),
            patch("familiekilder.hoopit._record_direct_access_proof") as proof,
            self.assertRaises(SourceUnavailable),
        ):
            hoopit.verify_direct_access(
                self.root,
                transport=transport,
                rate_limiter=self.limiter,
                at=1_000.0,
            )
        refresh.assert_called_once()
        invalidate.assert_called_once_with(self.root.resolve())
        self.assertEqual(len(transport.gets), 1)
        proof.assert_not_called()

    def test_direct_gate_preserves_transient_categories_without_denial_marker(self):
        from familiekilder.source_diagnostics import failure
        from datetime import UTC, datetime
        for exc, code in [(ApiHTTPError(503, retry_after_seconds=30), 'backend_error'),
                          (ApiHTTPError(429, retry_after_seconds=30), 'rate_limit'),
                          (OSError('private network detail'), 'transient')]:
            with self.subTest(code=code):
                transport = FakeTransport({'/app/users/current/notifications/': exc})
                with (patch('familiekilder.hoopit._invalidate_direct_access_proof'),
                      patch('familiekilder.hoopit.refresh_session'),
                      patch('familiekilder.hoopit.session_for_use', return_value=_access()),
                      patch('familiekilder.hoopit._record_direct_access_proof') as proof,
                      patch('familiekilder.hoopit._record_direct_access_rejection') as rejected,
                      self.assertRaises(SourceUnavailable) as caught):
                    hoopit.verify_direct_access(self.root, transport=transport, rate_limiter=NoopLimiter(), at=1_000.0)
                diagnostic = failure(caught.exception, at=datetime.now(UTC))
                self.assertEqual(diagnostic['incomplete_reason'], code)
                if isinstance(exc, ApiHTTPError):
                    self.assertTrue(diagnostic['retry_at'])
                self.assertNotIsInstance(caught.exception, SourceLoginRequired)
                self.assertNotIn('private network detail', str(caught.exception))
                proof.assert_not_called()
                rejected.assert_not_called()

    def test_direct_gate_true_refresh_rejection_remains_login_required(self):
        with (patch('familiekilder.hoopit._invalidate_direct_access_proof'),
              patch('familiekilder.hoopit.refresh_session', side_effect=SourceLoginRequired('invalid grant')),
              patch('familiekilder.hoopit.session_for_use') as session,
              patch('familiekilder.hoopit._record_direct_access_rejection') as rejected,
              self.assertRaises(SourceLoginRequired)):
            hoopit.verify_direct_access(self.root, rate_limiter=NoopLimiter(), at=1_000.0)
        session.assert_not_called()
        rejected.assert_not_called()


if __name__ == "__main__":
    unittest.main()
