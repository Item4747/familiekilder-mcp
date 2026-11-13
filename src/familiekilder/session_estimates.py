"""Read-only connection estimates, independent of short access-token expiry."""

import time
from datetime import UTC, datetime
from .auth_lifecycle import report
from .sources import REGISTRY


def estimate(root, source, *, at=None):
    kind = REGISTRY[source].session_estimate if source in REGISTRY else "unknown"
    if kind == "google":
        return {
            "kind": "long_lived",
            "basis": "google_refresh_grant",
            "label": "Innloggingen fornyes vanligvis automatisk",
            "progress": None,
        }
    now = time.time() if at is None else at
    health = report(root, source, at=now)
    if kind == "long_lived":
        return {
            "kind": "long_lived",
            "basis": "no_fixed_deadline",
            "label": "Forventet langvarig tilkobling",
            "progress": None,
        }
    deadline = health["refresh_expires_at"]
    start = health["last_login_observed_at"]
    if kind == "refresh_deadline" and deadline:
        return {
            "kind": "refresh",
            "basis": "provider_refresh_deadline",
            "deadline": datetime.fromtimestamp(deadline, UTC).isoformat(),
            "remaining_days": max(0, (deadline - now) / 86400),
            "progress": max(0, min(1, (deadline - now) / (90 * 86400))),
            "label": "Innloggingen fornyes automatisk. Nåværende tilgang varer i cirka %d dager"
            % max(0, (deadline - now) // 86400),
        }
    if kind == "login_30_days" and start:
        deadline = start + 30 * 86400
        remaining = max(0, (deadline - now) / 86400)
        return {
            "kind": "estimate",
            "basis": "initial_hypothesis_30_days",
            "deadline": datetime.fromtimestamp(deadline, UTC).isoformat(),
            "remaining_days": remaining,
            "progress": remaining / 30,
            "label": "Estimert ny innlogging om cirka %d dager" % remaining
            if remaining
            else "Tilkoblingen varer lenger enn første estimat",
        }
    return {"kind": "unknown", "label": "Vi vet ikke når du må logge inn igjen", "progress": None}
