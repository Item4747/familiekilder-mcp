"""Shared pacing and bounded recovery for read-only Gmail requests.

Only timing and fixed reason codes are persisted, never URLs or Google bodies.
The caller holds the Gmail source lock through reservation and response handling.
"""
import math
import random
import time
from datetime import UTC, datetime

from .api_transport import _retry_after_seconds
from .private_state import credential_path, read_private_json, atomic_write_private_json
from .source_adapter import SourceUnavailable

MIN_INTERVAL = .5  # Local safety margin, not a claimed Google quota.
MAX_INTERVAL = 4.
MAX_WAIT = 10.
READ_LOCK_TIMEOUT = 30.  # Bound contention separately from provider backoff.
MAX_RETRIES = 2
REASONS = {'rate_limit', 'daily_quota', 'backend_error'}


class GmailDeferred(SourceUnavailable):
    def __init__(self, reason, retry_at):
        self.reason, self.retry_at = reason, retry_at
        prefix = {'daily_quota': 'Gmail-dagskvote', 'backend_error': 'Gmail-kildefeil og trafikkpause',
                  'rate_limit': 'Gmail-hastighetsgrense eller trafikkpause'}[reason]
        super().__init__(f'{prefix}; nytt forsøk tidligst {retry_at}')


class Traffic:
    def __init__(self, root, *, clock=None, sleep=None, jitter=None):
        self.path = credential_path(root, 'gmail-traffic.json')
        self.clock, self.sleep = clock or time.time, sleep or time.sleep
        self.jitter = jitter or random.random

    def now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise SourceUnavailable('Gmail-trafikk har ugyldig klokke')
        return value

    def state(self):
        if not self.path.exists() and not self.path.is_symlink():
            return {'version': 1, 'last_request_at': None, 'interval': MIN_INTERVAL,
                    'blocked_until': None, 'reason': None, 'failures': 0, 'successes': 0}
        value = read_private_json(self.path, max_bytes=4096)
        if (set(value) != {'version', 'last_request_at', 'interval', 'blocked_until', 'reason', 'failures', 'successes'}
                or value['version'] != 1 or value['reason'] not in REASONS | {None}):
            raise SourceUnavailable('Gmail-trafikk har ugyldig lokal tilstand')
        for key in ('last_request_at', 'interval', 'blocked_until'):
            n = value[key]
            if n is None and key != 'interval':
                continue
            if type(n) not in (int, float) or not math.isfinite(n) or n < 0:
                raise SourceUnavailable('Gmail-trafikk har ugyldig tidspunkt')
        if not MIN_INTERVAL <= value['interval'] <= MAX_INTERVAL or any(
                type(value[k]) is not int or not 0 <= value[k] <= 1000 for k in ('failures', 'successes')):
            raise SourceUnavailable('Gmail-trafikk har ugyldig begrensning')
        return value

    @staticmethod
    def deferred(state):
        stamp = datetime.fromtimestamp(state['blocked_until'], UTC).isoformat(timespec='seconds')
        return GmailDeferred(state['reason'] or 'rate_limit', stamp)

    def before(self, deadline):
        state, now = self.state(), self.now()
        target = max(state['blocked_until'] or 0,
                     state['last_request_at'] + state['interval'] if state['last_request_at'] is not None else 0)
        if target > now:
            if target > deadline:
                if state['blocked_until'] and state['blocked_until'] > now:
                    raise self.deferred(state)
                raise SourceUnavailable('Gmail-trafikk må vente; ingen forespørsel sendt')
            self.sleep(target-now)
            now = self.now()
            if now + .001 < target:
                raise SourceUnavailable('Gmail-klokken gikk ikke fremover; ingen forespørsel sendt')
        # Persist before sending so a crash/new MCP process cannot cause a burst.
        state['last_request_at'] = now
        atomic_write_private_json(self.path, state)

    def succeeded(self):
        state = self.state()
        state.update(blocked_until=None, reason=None, failures=0)
        state['successes'] += 1
        if state['successes'] >= 20:
            state.update(interval=max(MIN_INTERVAL, state['interval']/2), successes=0)
        atomic_write_private_json(self.path, state)

    def rejected(self, response, reason, attempt, *, exhausted=False):
        now, state = self.now(), self.state()
        retry_after = _retry_after_seconds(response.headers.get('Retry-After'), now=datetime.fromtimestamp(now, UTC))
        delay = max(2**attempt + self.jitter(), retry_after or 0)
        state['failures'] = min(10, state['failures']+1)
        state['successes'] = 0
        if reason in {'rate_limit', 'daily_quota'}:
            state['interval'] = min(MAX_INTERVAL, state['interval']*2)
        if reason == 'daily_quota':
            delay = max(delay, 3600)
        elif exhausted:
            delay = max(delay, min(900, 60 * 2**min(state['failures']-1, 4)))
        state.update(blocked_until=max(state['blocked_until'] or 0, now+delay), reason=reason)
        atomic_write_private_json(self.path, state)
        return self.deferred(state)


def retry_reason(response):
    if response.status_code == 429:
        return 'rate_limit'
    if response.status_code in (500, 502, 503, 504):
        return 'backend_error'
    if response.status_code == 403:
        try:
            reasons = {e.get('reason') for e in response.json().get('error', {}).get('errors', [])}
        except (ValueError, TypeError, AttributeError):
            reasons = set()
        if 'dailyLimitExceeded' in reasons:
            return 'daily_quota'
        if reasons & {'rateLimitExceeded', 'userRateLimitExceeded'}:
            return 'rate_limit'
    return None
