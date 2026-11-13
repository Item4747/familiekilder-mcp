from __future__ import annotations

import tempfile
import json
import io
from email.message import Message
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from contextlib import nullcontext
from familiekilder import live_readers, live_spond_reader
from familiekilder import spond, spond_auth
from familiekilder.api_transport import (
    ApiHTTPError,
    ApiTransport,
    ApiTransportError,
)
from familiekilder.private_state import atomic_write_private_json, read_private_json
from familiekilder.source_adapter import SourceLoginRequired, SourceUnavailable
from familiekilder.spond_auth import (
    API_ROOT,
    SpondCooldownActive,
    client_config_path,
    cooldown_path,
    load_client_config,
    pacing_path,
    session_path,
)
from familiekilder_testing.spond import (
    NOW,
    CUTOFF,
    OLD,
    RECENT,
    FUTURE,
    TEN_DAYS,
    FakeClock,
    FakeCollectorTransport,
    _query,
    _path,
    _post,
    _event,
)


class SpondAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _store_session(self, root: Path | None = None) -> None:
        target = root or self.root
        atomic_write_private_json(
            client_config_path(target),
            {
                "api_root": API_ROOT,
                "app_version": "4.62.0",
                "attachment_hosts": ["files.example.test"],
            },
        )
        atomic_write_private_json(
            session_path(target),
            {
                "access_token": "spond-access-secret",
                "access_expires_at": NOW + 3_600,
                "refresh_token": "spond-refresh-secret",
                "refresh_expires_at": NOW + 86_400,
                "profile_id": "profile-1",
                "verified_at": NOW - 10,
                "updated_at": NOW - 10,
            },
        )

    def _client(
        self,
        transport: FakeCollectorTransport,
        *,
        root: Path | None = None,
        clock: FakeClock | None = None,
    ) -> tuple[spond.AuthorizedClient, FakeClock]:
        target = root or self.root
        self._store_session(target)
        active_clock = clock or FakeClock()
        return (
            spond.AuthorizedClient(
                target,
                transport=transport,
                auth_transport=None,
                budget=spond.RequestBudget(),
                wall_time=lambda: NOW,
                pacing_clock=active_clock,
                sleep=active_clock.sleep,
            ),
            active_clock,
        )

    def test_created_time_pagination_keeps_new_comments_and_future_events(self) -> None:
        old_post_1 = _post("old-post-1", timestamp=OLD)
        old_post_2 = _post("old-post-2", timestamp=OLD)
        commented_post = _post(
            "commented-post",
            timestamp=OLD,
            comments=[{"id": "comment-1", "timestamp": RECENT, "text": "Ny kommentar"}],
        )
        club_payment = _post(
            "club-payment", timestamp=OLD, post_type="CLUB_PAYMENT"
        )
        future_event = _event("future-event", start=FUTURE)
        old_event = _event("old-event")
        old_event_2 = _event("old-event-2", start=OLD)
        old_event_2["endTimestamp"] = OLD
        commented_event = _event(
            "commented-event",
            comments=[{"id": "event-comment", "createdTime": RECENT, "text": "Ny beskjed"}],
        )

        def handler(url: str) -> Any:
            query = _query(url)
            if _path(url) == "/core/v1/posts":
                if query["type"] == "PLAIN":
                    return (
                        [old_post_1, old_post_2]
                        if "prevId" not in query
                        else [commented_post]
                    )
                if query["type"] == "PAYMENT":
                    return [club_payment]
                return []
            if _path(url) == "/core/v1/sponds":
                if query["order"] == "asc":
                    return [future_event]
                return [old_event, old_event_2] if "prevId" not in query else [commented_event]
            raise AssertionError(f"unexpected GET {url}")

        transport = FakeCollectorTransport(handler)
        client, _clock = self._client(transport)
        with patch.object(spond, "PAGE_SIZE", 2):
            posts, post_failures = spond.fetch_posts(client, CUTOFF)
            events, event_failures = spond.fetch_events(client, CUTOFF)

        self.assertEqual(post_failures, 0)
        self.assertEqual(event_failures, 0)
        self.assertEqual(set(posts), {"commented-post", "club-payment"})
        self.assertEqual(set(events), {"future-event", "commented-event"})
        self.assertEqual(
            spond.normalize_post(
                posts["club-payment"],
                profiles={},
                children={},
                group_names={},
                attachments=[],
                notification_time=None,
            )["type"],
            "payment",
        )
        plain_pages = [
            url
            for url, _token, _headers in transport.gets
            if _path(url) == "/core/v1/posts" and _query(url).get("type") == "PLAIN"
        ]
        event_pages = [
            url
            for url, _token, _headers in transport.gets
            if _path(url) == "/core/v1/sponds"
        ]
        self.assertEqual(len(plain_pages), 2)
        self.assertEqual(len(event_pages), 3)

    def test_missing_timestamps_are_counted_not_silently_dropped(self) -> None:
        def handler(url: str) -> Any:
            path = _path(url)
            if path == "/core/v1/posts":
                post_type = _query(url)["type"]
                return [
                    {
                        "id": f"missing-{post_type}",
                        "type": post_type,
                        "comments": [],
                        "attachments": [],
                    }
                ]
            if path == "/core/v1/sponds":
                return [{"id": "missing-event", "comments": [], "attachments": []}]
            if path == "/core/v1/activities/summary":
                return [{"id": "missing-notification", "type": "POST"}]
            raise AssertionError(f"unexpected GET {url}")

        client, _clock = self._client(FakeCollectorTransport(handler))
        posts, post_failures = spond.fetch_posts(client, CUTOFF)
        events, event_failures = spond.fetch_events(client, CUTOFF)
        notifications, notification_failures = spond.fetch_notifications(
            client, None, CUTOFF
        )

        self.assertEqual(posts, {})
        self.assertEqual(events, {})
        self.assertEqual(notifications, [])
        self.assertEqual(post_failures, len(spond.POST_TYPES))
        self.assertEqual(event_failures, 2)
        self.assertEqual(notification_failures, 1)

    def test_old_entity_with_undated_or_invalid_new_comment_fails_closed(self) -> None:
        undated = _post(
            "undated-comment",
            timestamp=OLD,
            comments=[{"id": "comment-1", "text": "Kan være ny"}],
        )
        invalid = _post(
            "invalid-comment-time",
            timestamp=OLD,
            comments=[
                {
                    "id": "comment-2",
                    "timestamp": "not-a-date",
                    "text": "Kan også være ny",
                }
            ],
        )

        def handler(url: str) -> Any:
            if _query(url)["type"] == "PLAIN":
                return [undated, invalid]
            return []

        client, _clock = self._client(FakeCollectorTransport(handler))
        posts, failures = spond.fetch_posts(client, CUTOFF)

        self.assertEqual(posts, {})
        self.assertEqual(failures, 2)

    def test_poll_and_payment_streams_fail_closed_on_missing_nested_payload(self) -> None:
        malformed = {
            "POLL": {
                "id": "bad-poll",
                "type": "POLL",
                "timestamp": RECENT,
                "poll": [],
                "comments": [],
                "attachments": [],
            },
            "PAYMENT": {
                "id": "bad-payment",
                "type": "PAYMENT",
                "timestamp": RECENT,
                "comments": [],
                "attachments": [],
            },
        }

        def handler(url: str) -> Any:
            post_type = _query(url)["type"]
            return [malformed[post_type]] if post_type in malformed else []

        client, _clock = self._client(FakeCollectorTransport(handler))
        posts, failures = spond.fetch_posts(client, CUTOFF)

        self.assertEqual(posts, {})
        self.assertEqual(failures, 2)
        with self.assertRaises(SourceUnavailable):
            spond.normalize_post(
                malformed["POLL"],
                profiles={},
                children={},
                group_names={},
                attachments=[],
                notification_time=None,
            )
        malformed_club = _post("bad-club", post_type="CLUB_PAYMENT")
        malformed_club["clubPayment"] = "wrong-wire-shape"
        with self.assertRaises(SourceUnavailable):
            spond.normalize_post(
                malformed_club,
                profiles={},
                children={},
                group_names={},
                attachments=[],
                notification_time=None,
            )

    def test_full_pages_fail_at_cap_instead_of_returning_a_partial_success(self) -> None:
        transport = FakeCollectorTransport(
            lambda url: [_event("always-full", start=FUTURE)]
        )
        client, _clock = self._client(transport)

        with patch.object(spond, "PAGE_SIZE", 1), patch.object(
            spond, "MAX_PAGES_PER_STREAM", 1
        ):
            with self.assertRaises(SourceUnavailable):
                spond.fetch_events(client, CUTOFF)

        self.assertEqual(len(transport.gets), 1)

    def test_query_guard_pins_read_only_keys_and_values(self) -> None:
        safe = spond.url_with_query(
            spond.POSTS_ENDPOINT,
            {
                "type": "PLAIN",
                "includeComments": True,
                "includeReadStatus": False,
                "includeSeenCount": False,
                "max": spond.PAGE_SIZE,
            },
        )
        spond._guard_api_get(safe)
        unsafe_urls = (
            safe.replace("includeReadStatus=false", "includeReadStatus=true"),
            safe.replace("includeComments=true", "includeComments=false"),
            safe.replace(f"max={spond.PAGE_SIZE}", "max=999"),
            safe + "&prevId=bad%2Fid&maxTimestamp=2026-08-31T08%3A00%3A00Z",
            "https://api.spond.com/core/v1/posts/markAsRead",
            "https://api.spond.com/core/v1/seen/post-1",
        )
        for url in unsafe_urls:
            with self.subTest(url=url), self.assertRaises(SourceUnavailable):
                spond._guard_api_get(url)

    def test_activity_summary_excludes_chat_and_never_uses_read_receipt_routes(self) -> None:
        def handler(url: str) -> Any:
            self.assertEqual(_path(url), "/core/v1/activities/summary")
            return [
                {
                    "id": "notification-1",
                    "type": "POST_COMMENT",
                    "timestamp": RECENT,
                    "text": "Ny kommentar",
                },
                {
                    "id": "private-chat",
                    "type": "CHAT_MESSAGE",
                    "chatId": "chat-1",
                    "timestamp": RECENT,
                },
            ]

        transport = FakeCollectorTransport(handler)
        client, _clock = self._client(transport)
        values, failures = spond.fetch_notifications(client, "nb-NO", CUTOFF)

        self.assertEqual([value["id"] for value in values], ["notification-1"])
        self.assertEqual(failures, 0)
        rendered = "\n".join(url.lower() for url, _token, _headers in transport.gets)
        self.assertNotIn("seen", rendered)
        self.assertNotIn("markasread", rendered)
        self.assertNotIn("chat", rendered)

    def test_forbidden_notification_detail_after_refresh_is_partial(self) -> None:
        def handler(_url):
            raise ApiHTTPError(403)
        transport = FakeCollectorTransport(handler)
        client, _ = self._client(transport)
        with patch.object(spond, "refresh_session") as refresh:
            touched, failures = spond.fetch_notification_details(
                client, [{"id": "notice", "timestamp": RECENT, "spondId": "event-1"}], {}, {}
            )
        self.assertEqual(failures, 1)
        self.assertIn(("event", "event-1"), touched)
        self.assertEqual(len(transport.gets), 2)
        refresh.assert_called_once()

    def test_forbidden_after_proactive_refresh_does_not_demand_login(self) -> None:
        def handler(_url):
            raise ApiHTTPError(403)
        client, _ = self._client(FakeCollectorTransport(handler))
        client._refreshed = True
        with patch.object(spond, "refresh_session") as refresh:
            with self.assertRaises(SourceUnavailable):
                client.get_json(spond_auth.PROFILE_ENDPOINT)
        refresh.assert_not_called()

    def test_unauthorized_after_refresh_still_requires_login(self) -> None:
        def handler(_url):
            raise ApiHTTPError(401)
        client, _ = self._client(FakeCollectorTransport(handler))
        with patch.object(spond, "refresh_session") as refresh:
            with self.assertRaises(SourceLoginRequired):
                client.get_json(spond_auth.PROFILE_ENDPOINT)
        refresh.assert_called_once()

    def test_notification_detail_id_mismatch_is_partial_and_not_ingested(self) -> None:
        transport = FakeCollectorTransport(
            lambda _url: _post("different-post-id", timestamp=RECENT)
        )
        client, _clock = self._client(transport)
        posts: dict[str, Mapping[str, Any]] = {}
        touched, failures = spond.fetch_notification_details(
            client,
            [
                {
                    "id": "notification-1",
                    "timestamp": RECENT,
                    "postId": "expected-post-id",
                }
            ],
            posts,
            {},
        )

        self.assertEqual(failures, 1)
        self.assertEqual(posts, {})
        self.assertIn(("post", "expected-post-id"), touched)

    def test_verified_storage_is_allowed_only_after_authenticated_resolver(self) -> None:
        storage = 'https://' + spond.GROUP_FILE_STORAGE_HOST + '/signed.pdf'
        def handler(url):
            if _path(url).endswith('/filesToken'):
                return {'value': 'file-token-secret'}
            return {'url': storage}
        transport = FakeCollectorTransport(handler)
        client, _clock = self._client(transport)
        target = self.root / '.private' / 'incoming' / 'file.pdf'
        with self.assertRaises(SourceUnavailable):
            client.download(storage, target, group_id='group-1', max_bytes=1024)
        client.download('https://spond.com/storage/upload/group/file-1', target,
                        group_id='group-1', max_bytes=1024)
        self.assertEqual(transport.downloads[0][0], storage)
        self.assertIsNone(transport.downloads[0][2])

    def test_verified_post_images_download_without_token_or_provider_profile_change(self) -> None:
        image_url = "https://spond.com/images/abc/" + "abc" + "1" * 29 + ".jpeg"
        raw = _post("image-post", attachments=[{"url": image_url, "type": "image", "width": 600, "height": 800}])
        item = live_spond_reader.spond_item(
            spond, "post", raw, {"profiles": {}, "children": {}, "group_names": {}},
        )
        transport = FakeCollectorTransport()
        client, _clock = self._client(transport)
        directory = self.root / ".private" / "incoming"
        with (
            patch.object(live_readers, "provider", return_value=nullcontext((spond, client))),
            patch.object(live_readers, "get_with_client", return_value=item),
            patch.object(transport, "download", wraps=transport.download) as download,
        ):
            path, _mime = live_readers.download_live(
                self.root, "spond", item["reference"], item["attachments"][0]["attachment_id"], directory,
            )
        self.assertTrue(path.exists())
        self.assertEqual(transport.gets, [])
        self.assertEqual(transport.downloads[0][0], image_url)
        self.assertIsNone(transport.downloads[0][2])
        self.assertIsNone(transport.downloads[0][3])
        self.assertEqual(download.call_args.kwargs["allowed_content_types"], {"image/jpeg"})
        self.assertEqual(download.call_args.kwargs["max_bytes"], spond.MAX_ATTACHMENT_BYTES)
        self.assertEqual(client.attachment_hosts, {"files.example.test"})
        self.assertIn("spond.com", spond._default_transport(load_client_config(self.root)).allowed_hosts)

    def test_verified_png_named_image_and_owner_bound_post_upload(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/spond-attachment-routes.json').read_text())
        for variant in fixture['variants']:
            with self.subTest(variant=variant):
                raw = _post('synthetic-post', attachments=[dict(variant)])
                item = live_spond_reader.spond_item(spond, 'post', raw,
                    {'profiles': {}, 'children': {}, 'group_names': {}})
                transport = FakeCollectorTransport()
                client, _ = self._client(transport)
                with patch.object(live_readers, 'provider', return_value=nullcontext((spond, client))), \
                     patch.object(live_readers, 'get_with_client', return_value=item), \
                     patch.object(transport, 'download', wraps=transport.download) as download:
                    live_readers.download_live(self.root, 'spond', item['reference'],
                        item['attachments'][0]['attachment_id'], self.root / '.private/incoming')
                self.assertEqual(download.call_args.kwargs['allowed_content_types'], set(variant['allowed_mime']))
                self.assertIsNone(download.call_args.kwargs['bearer_token'])
                self.assertIsNone(download.call_args.kwargs['headers'])
                self.assertEqual(client.attachment_hosts, {'files.example.test'})

    def test_upload_owner_route_and_mime_checks_cannot_be_bypassed(self):
        url = 'https://spond.com/storage/upload/' + 'A'*32 + '/1700000000_AB12CD34/file_' + 'a'*32 + '.png'
        transport = FakeCollectorTransport()
        client, _ = self._client(transport)
        target = self.root / '.private/incoming/image.png'
        for changed, owner, group in ((url, None, 'group-1'), (url, 'B'*32, 'group-1'),
                (url, 'A'*32, None), (url + '?auth=secret', 'A'*32, 'group-1'),
                (url.replace('/upload/', '/upload/../upload/'), 'A'*32, 'group-1'),
                (url.replace('spond.com/', 'spond.com.evil.test/'), 'A'*32, 'group-1'),
                (url.replace('.png', '.html'), 'A'*32, 'group-1')):
            with self.subTest(url=changed), self.assertRaises(SourceUnavailable):
                client.download(changed, target, group_id=group, owner_id=owner, max_bytes=1024)
        self.assertEqual(transport.downloads, [])
        native = ApiTransport(allowed_hosts={'spond.com'})
        client.transport = native
        response = io.BytesIO(b'<html>not an image</html>')
        response.headers = Message()
        response.headers['Content-Type'] = 'text/html'
        with patch.object(native, '_open', return_value=response):
            with self.assertRaisesRegex(SourceUnavailable, 'MIME'):
                client.download(url, target, group_id='group-1', owner_id='A'*32, max_bytes=1024)
        self.assertFalse(target.exists())
        self.assertFalse(cooldown_path(self.root).exists(), 'MIME rejection must not pause unrelated source reads')

    def test_first_party_image_route_rejects_unknown_urls_and_missing_group_before_network(self) -> None:
        filename = "abc" + "1" * 29 + ".jpeg"
        good = "https://spond.com/images/abc/" + filename
        urls = [
            good.replace("https:", "http:"),
            good.replace("spond.com/", "spond.com:444/"),
            good.replace("spond.com/", "spond.com.evil.test/"),
            good.replace("spond.com/", "user:secret@spond.com/"),
            good.replace("/images/", "/other/"),
            good.replace("/images/", "/images/../images/"),
            good.replace("/images/", "/%69mages/"),
            good.replace("/abc/", "/def/"),
            good.replace(".jpeg", ".gif"),
            good + "?auth=source-supplied",
            good + "#fragment",
        ]
        transport = FakeCollectorTransport()
        client, _clock = self._client(transport)
        target = self.root / ".private" / "incoming" / "image.jpeg"
        for url in urls:
            with self.subTest(url=url), self.assertRaises(SourceUnavailable):
                client.download(url, target, group_id="group-1", max_bytes=1024)
        for group in (None, "", ".", "..", "group/1", "group%2f1"):
            with self.subTest(group=group), self.assertRaises(SourceUnavailable):
                client.download(good, target, group_id=group, max_bytes=1024)
        self.assertEqual(transport.gets, [])
        self.assertEqual(transport.downloads, [])

    def test_group_file_resolver_requires_exact_trusted_source_route_before_token_fetch(self) -> None:
        transport = FakeCollectorTransport()
        client, _clock = self._client(transport)
        target = self.root / ".private" / "incoming" / "file.pdf"
        urls = [
            "https://evil.test/storage/upload/group/file-1",
            "https://spond.com/prefix/storage/upload/group/file-1",
            "https://spond.com/storage/upload/group/file-1/",
            "https://spond.com/storage/upload/group/%66ile-1",
            "https://spond.com/storage/upload/group/file-1?auth=source-supplied",
            "https://spond.com/storage/upload/group/file-1#fragment",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(SourceUnavailable):
                client.download(url, target, group_id="group-1", max_bytes=1024)
        self.assertEqual(transport.gets, [])
        self.assertEqual(transport.downloads, [])

    def test_external_attachments_are_allowlisted_and_always_token_free(self) -> None:
        def handler(url: str) -> Any:
            if _path(url) == "/core/v1/group/group-1/filesToken":
                return {"value": "file-token-secret"}
            if _path(url) == "/storage/upload/group/file-1":
                self.assertEqual(_query(url), {"auth": "file-token-secret"})
                return {"url": "https://files.example.test/signed/file-1"}
            raise AssertionError(f"unexpected GET {url}")

        transport = FakeCollectorTransport(handler)
        client, _clock = self._client(transport)
        target = self.root / ".private" / "incoming" / "file.bin"
        client.download(
            "https://api.spond.com/storage/upload/group/file-1",
            target,
            group_id="group-1",
            max_bytes=1_024,
        )

        self.assertEqual(len(transport.downloads), 1)
        download = transport.downloads[0]
        self.assertEqual(download[0], "https://files.example.test/signed/file-1")
        self.assertIsNone(download[2])
        self.assertIsNone(download[3])
        self.assertTrue(
            all(
                token == "spond-access-secret"
                for _url, token, _headers in transport.gets
            )
        )
        with self.assertRaises(SourceUnavailable):
            client.download(
                "https://evil.example.test/object",
                target,
                group_id=None,
                max_bytes=1_024,
            )
        self.assertEqual(len(transport.downloads), 1)

    def test_collector_uses_current_provider_hosts_and_app_version_not_session_copy(self) -> None:
        self._store_session()
        session = dict(read_private_json(session_path(self.root)))
        session["attachment_hosts"] = ["legacy-files.example.test"]
        atomic_write_private_json(session_path(self.root), session)
        atomic_write_private_json(
            client_config_path(self.root),
            {
                "api_root": API_ROOT,
                "app_version": "4.63.0",
                "attachment_hosts": ["current-files.example.test"],
            },
        )
        transport = FakeCollectorTransport()
        client = spond.AuthorizedClient(
            self.root,
            transport=transport,
            auth_transport=None,
            budget=spond.RequestBudget(),
            wall_time=lambda: NOW,
            pacing_clock=FakeClock(),
            sleep=lambda _seconds: None,
        )

        self.assertEqual(client.api_root, API_ROOT)
        self.assertEqual(client.app_version, "4.63.0")
        self.assertEqual(client.attachment_hosts, {"current-files.example.test"})
        config = load_client_config(self.root)
        self.assertEqual(
            spond._default_transport(config).user_agent,
            spond_auth._default_transport(config).user_agent,
        )
        self.assertIn("4.63.0", spond._default_transport(config).user_agent)

        target = self.root / ".private" / "incoming" / "provider-host.bin"
        client.download(
            "https://current-files.example.test/signed/object",
            target,
            group_id=None,
            max_bytes=1_024,
        )
        with self.assertRaises(SourceUnavailable):
            client.download(
                "https://legacy-files.example.test/signed/object",
                target,
                group_id=None,
                max_bytes=1_024,
            )
        self.assertEqual(len(transport.downloads), 1)

    def test_pacing_persists_across_clients(self) -> None:
        transport = FakeCollectorTransport(lambda _url: {"id": "profile-1"})
        first_clock = FakeClock(NOW)
        first, _clock = self._client(transport, clock=first_clock)
        first.get_json(spond_auth.PROFILE_ENDPOINT)

        second_clock = FakeClock(NOW + 1)
        second, _clock = self._client(transport, clock=second_clock)
        second.get_json(spond_auth.PROFILE_ENDPOINT)

        self.assertEqual(second_clock.sleeps, [1.0])
        self.assertEqual(read_private_json(pacing_path(self.root))["last_request_at"], NOW + 2)

    def test_request_budget_stops_before_an_extra_network_call(self) -> None:
        self._store_session()
        transport = FakeCollectorTransport(lambda _url: {"id": "profile-1"})
        clock = FakeClock()
        client = spond.AuthorizedClient(
            self.root,
            transport=transport,
            auth_transport=None,
            budget=spond.RequestBudget(limit=1),
            wall_time=lambda: NOW,
            pacing_clock=clock,
            sleep=clock.sleep,
        )
        client.get_json(spond_auth.PROFILE_ENDPOINT)
        with self.assertRaises(SourceUnavailable):
            client.get_json(spond_auth.PROFILE_ENDPOINT)

        self.assertEqual(len(transport.gets), 1)

    def test_429_and_get_network_errors_stop_globally_with_persistent_cooldown(self) -> None:
        for ordinal, failure in enumerate(
            (
                ApiHTTPError(429, retry_after_seconds=TEN_DAYS),
                ApiTransportError("secret-network-error"),
                OSError("secret-os-network-error"),
            )
        ):
            root = self.root / f"get-failure-{ordinal}"
            root.mkdir()

            def handler(_url: str, error: BaseException = failure) -> Any:
                raise error

            transport = FakeCollectorTransport(handler)
            client, _clock = self._client(transport, root=root)
            with self.assertRaises(SpondCooldownActive):
                client.get_json(spond_auth.PROFILE_ENDPOINT)
            with self.assertRaises(SpondCooldownActive):
                client.get_json(spond_auth.PROFILE_ENDPOINT)

            self.assertEqual(len(transport.gets), 1)
            expected = NOW + TEN_DAYS if ordinal == 0 else NOW + 900
            self.assertEqual(read_private_json(cooldown_path(root))["until"], expected)

    def test_attachment_5xx_and_network_failures_stop_before_next_file(self) -> None:
        for ordinal, failure in enumerate(
            (ApiHTTPError(503), ApiTransportError("network"), OSError("socket"))
        ):
            root = self.root / f"attachment-failure-{ordinal}"
            root.mkdir()
            transport = FakeCollectorTransport(download_failure=failure)
            client, _clock = self._client(transport, root=root)
            entity = _post(
                "with-files",
                attachments=[
                    {"id": "one", "url": "https://files.example.test/one"},
                    {"id": "two", "url": "https://files.example.test/two"},
                ],
            )
            item = {
                "attachments": [live_readers.attachment_metadata(spec) for spec in entity["attachments"]]
            }
            with (
                patch.object(live_readers, "provider", return_value=nullcontext((spond, client))),
                patch.object(live_readers, "get_with_client", return_value=item),
                self.assertRaises(SpondCooldownActive),
            ):
                live_readers.download_live(
                    root, "spond", {"kind": "post", "id": "with-files"},
                    item["attachments"][0]["attachment_id"], root / ".private" / "incoming",
                )

            self.assertEqual(len(transport.downloads), 1)
            self.assertIsNone(transport.downloads[0][2])
            self.assertEqual(read_private_json(cooldown_path(root))["until"], NOW + 900)

    def test_requested_attachment_404_is_reported_without_cooldown(self) -> None:
        transport = FakeCollectorTransport(download_failure=ApiHTTPError(404))
        client, _clock = self._client(transport)
        entity = _post(
            "with-missing-files",
            attachments=[
                {"id": "one", "url": "https://files.example.test/one"},
                {"id": "two", "url": "https://files.example.test/two"},
            ],
        )
        item = {
            "attachments": [live_readers.attachment_metadata(spec) for spec in entity["attachments"]]
        }
        with (
            patch.object(live_readers, "provider", return_value=nullcontext((spond, client))),
            patch.object(live_readers, "get_with_client", return_value=item),
            self.assertRaisesRegex(SourceUnavailable, "kunne ikke lastes ned"),
        ):
            live_readers.download_live(
                self.root, "spond", {"kind": "post", "id": "with-missing-files"},
                item["attachments"][0]["attachment_id"], self.root / ".private" / "incoming",
            )
        self.assertEqual(len(transport.downloads), 1)
        self.assertFalse(cooldown_path(self.root).exists())

    def test_comment_and_attachment_wire_shapes_fail_closed(self) -> None:
        malformed = (
            {"comments": {}},
            {"comments": [{"id": "c", "replies": {}}]},
        )
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(SourceUnavailable):
                spond._all_comments(value)
        for value in (
            {"attachments": {}},
            {"attachments": [42]},
            {"attachments": [{"id": "missing-url"}]},
        ):
            with self.subTest(value=value), self.assertRaises(SourceUnavailable):
                spond.attachment_candidates(value)


if __name__ == "__main__":
    unittest.main()
