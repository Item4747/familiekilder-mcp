"""Installation/source traffic ceilings shared by jobs and processes.

These are local ceilings, not provider-approved quotas. Existing pacing and
Retry-After barriers remain authoritative. No request content is retained.
"""

import math
import time
from datetime import UTC, datetime

from .private_lock import private_source_lock
from .private_state import credential_path, read_private_json, atomic_write_private_json
from .live_config import SOURCES
from .source_adapter import SourceBudgetExceeded

HOURLY_LIMIT = 1000
DAILY_LIMIT = 3000


class SourceTrafficBudgetExceeded(SourceBudgetExceeded):
    request_sent = False
    reason = 'traffic_budget'

    def __init__(self, retry_at):
        self.retry_at = datetime.fromtimestamp(retry_at, UTC).isoformat(timespec='seconds')
        super().__init__(f'Tjenestens lokale trafikkbudsjett er brukt; ingen forespørsel sendt. '
                         f'Nytt forsøk tidligst {self.retry_at}')


def reserve(root, source, *, at=None):
    if source not in SOURCES:
        raise ValueError('Unknown request-budget source')
    stamp = time.time() if at is None else at
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
        raise SourceBudgetExceeded('Trafikkbudsjettet har ugyldig klokke')
    with private_source_lock(root, source, wait=True, timeout=30):
        path = credential_path(root, source + '-request-budget.json')
        state = read_private_json(path) if path.exists() or path.is_symlink() else {'version': 1}
        if state.get('version') != 1:
            raise SourceBudgetExceeded('Trafikkbudsjettet har ugyldig tilstand')
        retry = []
        for key, duration, maximum in (('hour', 3600, HOURLY_LIMIT), ('day', 86400, DAILY_LIMIT)):
            window = state.get(key, {'started_at': stamp, 'used': 0})
            start, used = window.get('started_at'), window.get('used')
            if (type(start) not in (int, float) or not math.isfinite(start) or start < 0
                    or type(used) is not int or used < 0):
                raise SourceBudgetExceeded('Trafikkbudsjettet har ugyldig tilstand')
            if stamp < start:
                retry.append(start + duration)
            elif stamp - start >= duration:
                window = {'started_at': stamp, 'used': 0}
            if window['used'] >= maximum:
                retry.append(window['started_at'] + duration)
            state[key] = window
        if retry:
            raise SourceTrafficBudgetExceeded(max(retry))
        for key in ('hour', 'day'):
            state[key]['used'] += 1
        atomic_write_private_json(path, state)
