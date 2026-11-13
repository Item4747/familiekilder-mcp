"""Fakes for Spond's login: a clock and an auth transport."""

from __future__ import annotations
import tempfile
from pathlib import Path
from typing import Any, Mapping

from familiekilder.private_state import atomic_write_private_json
from familiekilder.spond_auth import API_ROOT, client_config_path, session_path


NOW = 2_000_000_000.0
TEN_DAYS = 10 * 24 * 60 * 60


class FakeClock:
    def __init__(self, value: float = NOW) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class FakeAuthTransport:
    def __init__(self, *, profile: Any = None, refresh: Any = None) -> None:
        self.profile = {"id": "profile-1"} if profile is None else profile
        self.refresh = (
            {
                "accessToken": {
                    "token": "rotated-access-secret",
                    "expiration": NOW + 3_600,
                },
                "refreshToken": {
                    "token": "rotated-refresh-secret",
                    "expiration": NOW + 86_400,
                },
            }
            if refresh is None
            else refresh
        )
        self.gets: list[tuple[str, str | None]] = []
        self.posts: list[tuple[str, dict[str, Any], str | None]] = []

    def get_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.gets.append((url, bearer_token))
        if isinstance(self.profile, BaseException):
            raise self.profile
        return self.profile

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.posts.append((url, dict(payload), bearer_token))
        if isinstance(self.refresh, BaseException):
            raise self.refresh
        return self.refresh


def _canonical_session(
    *,
    access_token: str = "old-access-secret",
    refresh_token: str = "old-refresh-secret",
    access_expires_at: float = NOW + 3_600,
    refresh_expires_at: float = NOW + 86_400,
    stored: bool = False,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "access_token": access_token,
        "access_expires_at": access_expires_at,
        "refresh_token": refresh_token,
        "refresh_expires_at": refresh_expires_at,
    }
    if stored:
        value.update(
            {
                "profile_id": "profile-1",
                "verified_at": NOW - 10,
                "updated_at": NOW - 10,
            }
        )
    return value


class SpondAuthFixture:
    """A temporary folder with helpers to store a Spond provider profile and session."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _store_provider(
        self,
        *,
        app_version: str = "4.62.0",
        attachment_hosts: list[str] | None = None,
    ) -> None:
        atomic_write_private_json(
            client_config_path(self.root),
            {
                "api_root": API_ROOT,
                "app_version": app_version,
                "attachment_hosts": list(attachment_hosts or []),
            },
        )

    def _store_session(self, value: Mapping[str, Any] | None = None) -> None:
        self._store_provider(attachment_hosts=["files.example.test"])
        atomic_write_private_json(
            session_path(self.root), dict(value or _canonical_session(stored=True))
        )
