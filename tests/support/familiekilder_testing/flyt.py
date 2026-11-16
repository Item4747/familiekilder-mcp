"""A fake transport that serves an invented Flyt municipality list."""

from __future__ import annotations

import threading
from typing import Any, Callable

from familiekilder.flyt_auth import MUNICIPALITIES_ENDPOINT

# Invented municipalities; the last entry is invalid and must be skipped.
MUNICIPALITIES = [
    {
        "customerName": "fjordvik",
        "displayName": "Fjordvik kommune",
        "organizationNumber": "912345678",
        "vismaConnectClientContext": "Vismaconnect998877665",
    },
    {"customerName": "bergli", "displayName": "Bergli kommune", "organizationNumber": "923456789"},
    {
        "customerName": "aasby",
        "displayName": "Åsby kommune",
        "organizationNumber": "934567890",
    },
    {"displayName": "Mangler alt"},
]


class FakeMunicipalityTransport:
    """Answers only the public municipality list and records each call.

    ``on_call`` runs inside ``get_json``, so a test can see which thread made
    the request and what it held at the time.
    """

    def __init__(self, *, on_call: Callable[[], None] | None = None) -> None:
        self.calls: list[str] = []
        self.threads: list[threading.Thread] = []
        self.failures: list[Exception] = []
        self.on_call = on_call

    def get_json(self, url: str, **_kwargs: Any) -> Any:
        self.calls.append(url)
        self.threads.append(threading.current_thread())
        if url != MUNICIPALITIES_ENDPOINT:
            raise AssertionError("unexpected GET")
        if self.on_call is not None:
            self.on_call()
        if self.failures:
            raise self.failures.pop(0)
        return [dict(entry) for entry in MUNICIPALITIES]

    def post_json(self, url: str, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("unexpected POST")

    def post_form(self, url: str, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("unexpected POST")
