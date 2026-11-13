from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from familiekilder.api_transport import ApiHTTPError, ApiTransportError, DownloadResult
from familiekilder.source_adapter import SourceUnavailable
import familiekilder.skolemelding as skolemelding
from familiekilder_testing.skolemelding import (
    RECENT_1,
    OLD,
)


class SkolemeldingCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()


    def test_unknown_timestamp_is_only_allowed_for_detail_reconnaissance(self) -> None:
        cutoff = "2026-01-01T00:00:00+00:00"
        self.assertTrue(skolemelding.message_in_window({"Id": "m"}, cutoff))
        self.assertFalse(
            skolemelding.message_in_window({"Id": "m"}, cutoff, {"Id": "m"})
        )
        self.assertTrue(
            skolemelding.message_in_window(
                {"Id": "m"}, cutoff, {"Id": "m", "Timestamp": RECENT_1}
            )
        )

    def test_older_message_edited_inside_window_is_included(self) -> None:
        cutoff = "2026-01-01T00:00:00+00:00"
        self.assertTrue(
            skolemelding.message_in_window(
                {
                    "Id": "edited-old-message",
                    "Timestamp": OLD,
                    "UpdatedAt": RECENT_1,
                },
                cutoff,
            )
        )

    def test_same_provider_message_id_remains_distinct_per_child(self) -> None:
        first = skolemelding._stable_message_id(
            {"Id": "shared-message", "OnBehalfOf": "child-a"}
        )
        second = skolemelding._stable_message_id(
            {"Id": "shared-message", "OnBehalfOf": "child-b"}
        )
        self.assertEqual(first, "shared-message@child-a")
        self.assertEqual(second, "shared-message@child-b")
        self.assertNotEqual(first, second)

    def test_child_count_is_bounded_before_message_search(self) -> None:
        values = [
            {"Id": f"child-{index}", "Name": f"Child {index}"}
            for index in range(skolemelding.MAX_CHILDREN + 1)
        ]
        with self.assertRaises(SourceUnavailable):
            skolemelding.children({"Children": values}, {})

    def test_api_route_guard_rejects_unlock_and_read_mutations(self) -> None:
        attachment_client = object.__new__(skolemelding.AuthorizedClient)
        attachment_client.attachment_hosts = frozenset(
            {"portal.skoleplattform.no"}
        )
        attachment_client.attachment_path_prefixes = ("/api/files/",)
        urls = (
            "https://portal.skoleplattform.no/api/MyProfileV1/VerifyPinCode",
            "https://portal.skoleplattform.no/api/MyProfileV1/VerifyBiometric",
            "https://portal.skoleplattform.no/api/message/MarkMessageRead?messageId=1",
            "https://portal.skoleplattform.no/api/MyProfileV1/Logout",
            "https://portal.skoleplattform.no/api/message/Delete?messageId=1",
            "https://portal.skoleplattform.no/api/files/%2e%2e/message/Delete",
        )
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(SourceUnavailable):
                    skolemelding.AuthorizedClient._guard_api_url(url, method="GET")
                with self.assertRaises(SourceUnavailable):
                    attachment_client._guard_attachment_url(url)

    def test_attachment_guard_uses_only_current_signed_profile_routes(self) -> None:
        client = object.__new__(skolemelding.AuthorizedClient)
        client.attachment_hosts = frozenset({"files.skoleplattform.no"})
        client.attachment_path_prefixes = ("/verified/",)

        self.assertIs(
            client._guard_attachment_url(
                "https://files.skoleplattform.no/verified/document.pdf"
            ),
            skolemelding._AttachmentAuthMode.TOKEN_FREE,
        )
        for rejected in (
            "https://portal.skoleplattform.no/verified/document.pdf",
            "https://files.skoleplattform.no/api/files/document.pdf",
            "https://files.skoleplattform.no/verified/%2e%2e/private.pdf",
        ):
            with self.subTest(rejected=rejected):
                with self.assertRaises(SourceUnavailable):
                    client._guard_attachment_url(rejected)

        client.attachment_hosts = frozenset()
        client.attachment_path_prefixes = ()
        with self.assertRaises(SourceUnavailable):
            client._guard_attachment_url(
                "https://portal.skoleplattform.no/api/files/document.pdf"
            )

    def test_external_attachment_failures_are_item_partial_without_auth(self) -> None:
        class ExternalRejects:
            def __init__(self, failure: ApiTransportError) -> None:
                self.calls: list[dict[str, object]] = []
                self.failure = failure

            def download(
                self, url: str, target: Path, **kwargs: object
            ) -> DownloadResult:
                self.calls.append(dict(kwargs))
                raise self.failure

        failures = (
            ("unauthorized", ApiHTTPError(401)),
            ("forbidden", ApiHTTPError(403)),
            ("network", ApiTransportError("network unavailable")),
        )
        incoming = self.root / ".private" / "incoming" / "run" / "skolemelding"
        for label, failure in failures:
            with self.subTest(failure=label):
                transport = ExternalRejects(failure)
                client = object.__new__(skolemelding.AuthorizedClient)
                client.root = self.root
                client.transport = transport
                client.auth_transport = None
                client.attachment_hosts = frozenset({"files.skoleplattform.no"})
                client.attachment_path_prefixes = ("/verified/",)
                client.session = {"access_token": "must-not-leave-the-portal"}
                client._refreshed = False
                source_id = f"external-{label}@child"
                budget = skolemelding.DownloadBudget()

                with patch.object(skolemelding, "refresh_session") as refresh:
                    attachments = skolemelding.download_attachments(
                        client,
                        incoming,
                        source_id,
                        {
                            "Attachments": [
                                "https://files.skoleplattform.no/verified/document.pdf"
                            ]
                        },
                        budget,
                    )

                self.assertEqual(attachments, [])
                self.assertEqual(budget.failed_items, {source_id})
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn("bearer_token", transport.calls[0])
                refresh.assert_not_called()

    def test_portal_attachment_keeps_bearer_refresh_behavior(self) -> None:
        class PortalRefreshes:
            def __init__(self) -> None:
                self.tokens: list[object] = []

            def download(
                self, url: str, target: Path, **kwargs: object
            ) -> DownloadResult:
                self.tokens.append(kwargs.get("bearer_token"))
                if len(self.tokens) == 1:
                    raise ApiHTTPError(401)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"ok")
                return DownloadResult(2, "0" * 64, "application/pdf")

        transport = PortalRefreshes()
        client = object.__new__(skolemelding.AuthorizedClient)
        client.root = self.root
        client.transport = transport
        client.auth_transport = None
        client.attachment_hosts = frozenset({"portal.skoleplattform.no"})
        client.attachment_path_prefixes = ("/api/files/",)
        client.session = {"access_token": "old-portal-token"}
        client._refreshed = False
        target = self.root / ".private" / "portal.pdf"

        with (
            patch.object(
                skolemelding,
                "local_session_for_use",
                return_value={"access_token": "new-portal-token"},
            ),
            patch.object(skolemelding, "refresh_session") as refresh,
        ):
            result = client.download(
                "https://portal.skoleplattform.no/api/files/document.pdf",
                target,
                max_bytes=1024,
            )

        self.assertEqual(result.byte_size, 2)
        self.assertEqual(transport.tokens, ["old-portal-token", "new-portal-token"])
        refresh.assert_called_once_with(self.root, transport=None)

    def test_attachment_download_is_capped_by_remaining_run_budget(self) -> None:
        incoming = self.root / ".private" / "incoming" / "run" / "skolemelding"
        observed_limits: list[int] = []

        class BudgetClient:
            def download(self, url: str, target: Path, *, max_bytes: int) -> DownloadResult:
                observed_limits.append(max_bytes)
                payload = b"abc"
                self.assert_limit(payload, max_bytes)
                target.write_bytes(payload)
                target.chmod(0o600)
                return DownloadResult(len(payload), "0" * 64, "text/plain")

            @staticmethod
            def assert_limit(payload: bytes, max_bytes: int) -> None:
                if len(payload) > max_bytes:
                    raise AssertionError("fixture exceeded collector-provided limit")

        budget = skolemelding.DownloadBudget(limit=10, used=7)
        attachments = skolemelding.download_attachments(
            BudgetClient(),
            incoming,
            "message@child",
            {"Attachments": ["/api/files/remaining.txt"]},
            budget,
        )

        self.assertEqual(observed_limits, [3])
        self.assertEqual(budget.used, 10)
        self.assertEqual(len(attachments), 1)

    def test_invalid_attachment_url_does_not_hide_valid_siblings(self) -> None:
        incoming = self.root / ".private" / "incoming" / "run" / "skolemelding"
        downloaded: list[str] = []
        route_guard = object.__new__(skolemelding.AuthorizedClient)
        route_guard.attachment_hosts = frozenset(
            {"portal.skoleplattform.no"}
        )
        route_guard.attachment_path_prefixes = ("/api/files/",)

        class MixedClient:
            def download(
                self, url: str, target: Path, *, max_bytes: int
            ) -> DownloadResult:
                route_guard._guard_attachment_url(url)
                downloaded.append(url)
                payload = b"ok"
                self.assert_limit(payload, max_bytes)
                target.write_bytes(payload)
                target.chmod(0o600)
                return DownloadResult(len(payload), "0" * 64, "text/plain")

            @staticmethod
            def assert_limit(payload: bytes, max_bytes: int) -> None:
                if len(payload) > max_bytes:
                    raise AssertionError("fixture exceeded collector-provided limit")

        budget = skolemelding.DownloadBudget()
        attachments = skolemelding.download_attachments(
            MixedClient(),
            incoming,
            "mixed-message@child",
            {
                "Attachments": [
                    "/api/files/first.txt",
                    "/api/not-files/rejected.txt",
                    "/api/files/last.txt",
                ]
            },
            budget,
        )

        self.assertEqual(
            downloaded,
            [
                "https://portal.skoleplattform.no/api/files/first.txt",
                "https://portal.skoleplattform.no/api/files/last.txt",
            ],
        )
        self.assertEqual(len(attachments), 2)
        self.assertEqual(budget.failed_items, {"mixed-message@child"})


if __name__ == "__main__":
    unittest.main()
