"""Request pacing for direct providers that have no existing durable limiter.

The scope is execution-local, but timestamps are shared by all MCP processes for
one installation/source. No URLs, request bodies, tokens or responses are stored.
Spond and Hoopit keep their existing provider-specific traffic controls.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
import math
import time

from .core import project_paths
from .private_lock import private_source_lock
from .private_state import atomic_write_private_json, read_private_json
from .sources import REGISTRY

_ACTIVE = ContextVar("familiekilder_live_traffic", default=None)
SOURCES = frozenset(name for name, source in REGISTRY.items() if source.paced)
MIN_INTERVAL = 0.25  # Local pacing policy, not a claimed provider quota.
MAX_REQUESTS = 1000


def _not_sent(message):
    from .api_transport import ApiRequestNotSent

    return ApiRequestNotSent(message)


class Traffic:
    def __init__(self, root, source, *, clock=time.time, sleep=time.sleep):
        if source not in SOURCES:
            raise ValueError("Provider already has its own traffic controls")
        self.root = project_paths(root).root
        self.source, self.clock, self.sleep = source, clock, sleep
        self.used = 0
        self.path = (
            project_paths(root).private / "credentials" / f"{source}-live-traffic.json"
        )

    def _time(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise _not_sent("Trafikkbegrensningen har ugyldig klokke")
        return value

    def _state(self):
        if not self.path.exists() and not self.path.is_symlink():
            return {"version": 1, "last_request_at": None, "cooldown_until": None}
        state = read_private_json(self.path, max_bytes=4096)
        if (
            not isinstance(state, dict)
            or set(state) != {"version", "last_request_at", "cooldown_until"}
            or state["version"] != 1
        ):
            raise _not_sent("Trafikkbegrensningen har ugyldig lokal tilstand")
        for key in ("last_request_at", "cooldown_until"):
            value = state[key]
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
            ):
                raise _not_sent("Trafikkbegrensningen har ugyldig tidspunkt")
        return state

    def before(self):
        state = self._state()
        instant = self._time()
        cooldown = state["cooldown_until"]
        if cooldown is not None and cooldown > instant:
            until = datetime.fromtimestamp(cooldown, UTC).isoformat(timespec="seconds")
            raise _not_sent(
                f"{self.source} har trafikkpause til {until}; ingen forespørsel sendt"
            )
        if self.used >= MAX_REQUESTS:
            raise _not_sent("Oppslaget har nådd forespørselsgrensen; avgrens oppslaget")
        last = state["last_request_at"]
        if last is not None:
            delay = last + MIN_INTERVAL - instant
            if delay > 2:
                raise _not_sent(
                    "Klokken har gått bakover; trafikk må vente til lagret tidspunkt"
                )
            if delay > 0:
                self.sleep(delay)
                instant = self._time()
                if instant + 0.001 < last + MIN_INTERVAL:
                    raise _not_sent(
                        "Klokken gikk ikke fremover under trafikkbegrensningen"
                    )
        state.update(last_request_at=instant, cooldown_until=None)
        from .request_budget import reserve
        reserve(self.root, self.source, at=instant)
        atomic_write_private_json(self.path, state)
        self.used += 1

    def rejected(self, status, retry_after):
        if status != 429 and not (status == 503 and retry_after is not None):
            return
        delay = retry_after if type(retry_after) is int and retry_after >= 0 else 900
        # The shared HTTP parser bounds Retry-After to 366 days.
        delay = max(60, min(delay, 366 * 24 * 60 * 60))
        state = self._state()
        state["cooldown_until"] = max(
            state["cooldown_until"] or 0, self._time() + delay
        )
        atomic_write_private_json(self.path, state)


@contextmanager
def traffic_scope(root, source):
    active = _ACTIVE.get()
    if source in SOURCES:
        selected = (
            active
            if active
            and (active.root, active.source) == (project_paths(root).root, source)
            else Traffic(root, source)
        )
    else:
        selected = None
    token = _ACTIVE.set(selected)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def paced_request(function):
    """Wrap exactly one network request; never retries reads or mutations."""

    @wraps(function)
    def call(*args, **kwargs):
        traffic = _ACTIVE.get()
        if traffic is None:
            return function(*args, **kwargs)
        with private_source_lock(traffic.root, traffic.source):
            # All failures here prove the request has not been sent. In particular,
            # auth recovery must not persist an ambiguous-refresh barrier for one.
            try:
                traffic.before()
            except Exception as exc:
                if getattr(exc, "request_sent", None) is False:
                    raise
                raise _not_sent(
                    "Trafikkbegrensningen kunne ikke reservere forespørselen"
                ) from None
            try:
                return function(*args, **kwargs)
            except Exception as exc:
                status = getattr(exc, "status_code", getattr(exc, "http_status", None))
                if type(status) is int:
                    traffic.rejected(status, getattr(exc, "retry_after_seconds", None))
                raise

    return call
