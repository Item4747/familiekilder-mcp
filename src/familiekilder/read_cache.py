"""Bounded private read-through cache; cached evidence keeps its original age."""

import hashlib
import json
import math
import os
import time
from datetime import UTC, datetime

from .private_lock import private_named_lock, private_source_lock
from .private_state import ensure_private_directory, read_private_json, atomic_write_private_json, PrivateStateError
from .live_config import SOURCES

RETENTION = 7 * 86400
DETAIL_FRESHNESS = 300
LIST_FRESHNESS = 60
MAX_ENTRY_BYTES = 64 * 1024 * 1024
MAX_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 2000


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def account_scope(root, source):
    """Hash local account/client identity; never persist tokens in the cache.

    Missing credentials disable reuse. Stable subjects survive token rotation;
    Gmail, which does not store a subject, binds to the refresh grant instead.
    """
    private = root / '.private' / 'credentials'
    path = private / ('gmail.json' if source == 'gmail' else source + '-session.json')
    if not path.exists() and not path.is_symlink():
        return None
    try:
        session = read_private_json(path)
    except PrivateStateError:
        # Broken login disables reuse without stopping other sources/diagnostics.
        return None
    identity = {key: session[key] for key in (
        'subject', 'person_id', 'tenant_id', 'profile_id', 'userName',
        'device_unique_id', 'session_binding', 'client_id', 'client_fingerprint',
    ) if key in session}
    if not any(key in identity for key in ('subject', 'profile_id', 'userName')):
        identity['grant'] = digest(session.get('refresh_token', session))
    client_paths = [private / (source + suffix) for suffix in ('-client.json', '-provider.json')]
    try:
        client = [read_private_json(path) if path.exists() or path.is_symlink() else None
                  for path in client_paths]
    except PrivateStateError:
        return None
    return digest([identity, client])


class ReadCache:
    def __init__(self, root, *, clock=None, scope_resolver=None):
        self.root = root
        self.path = root / '.private' / 'read-cache'
        ensure_private_directory(self.path)
        self.clock = clock or time.time
        self.scope_resolver = scope_resolver

    def _account_scope(self, source):
        return (self.scope_resolver or account_scope)(self.root, source)

    def _lock(self):
        return private_named_lock(self.root, 'read-cache', wait=True, timeout=30)

    def _stamp(self):
        stamp = self.clock()
        if type(stamp) not in (float, int) or not math.isfinite(stamp) or stamp < 0:
            raise ValueError('Invalid cache clock')
        return stamp

    def _record(self, path):
        if path.is_symlink():
            # Do not follow or remove an unexpected link at the private boundary.
            return None
        try:
            value = read_private_json(path, max_bytes=MAX_ENTRY_BYTES)
            stamp = value['stored_at']
            if (value.get('version') != 1 or type(stamp) not in (float, int)
                    or not math.isfinite(stamp) or stamp < 0 or 'data' not in value):
                return None
            return value
        except (ValueError, KeyError, TypeError):
            return None

    def _cleanup(self, stamp, enabled=None):
        entries = []
        for path in self.path.iterdir():
            if path.is_symlink():
                continue
            if (path.name.startswith('.') and path.name.endswith('.tmp')
                    and path.name[1:].split('--', 1)[0] in SOURCES):
                # Atomic writers hold this same lock: leftover payload files
                # therefore belong to interrupted writes, never an active one.
                path.unlink(missing_ok=True)
                continue
            if path.suffix != '.json':
                continue
            source = path.name.split('--', 1)[0]
            # mtime is immutable on reads; expiration never slides on a hit.
            metadata = path.stat()
            if (stamp - metadata.st_mtime >= RETENTION or metadata.st_mtime > stamp
                    or enabled is not None and source not in enabled):
                path.unlink(missing_ok=True)
            else:
                entries.append((metadata.st_mtime, path, metadata.st_size))
        total = sum(entry[2] for entry in entries)
        count = len(entries)
        for _, path, size in sorted(entries):
            if total <= MAX_BYTES and count <= MAX_ENTRIES:
                break
            path.unlink(missing_ok=True)
            total -= size
            count -= 1

    def cleanup(self, enabled=None):
        with self._lock():
            if enabled is not None:
                for source in SOURCES:
                    self._scope(source, self._account_scope(source) if source in enabled else None)
            self._cleanup(self._stamp(), enabled)

    def _scope(self, source, scope):
        path = self.root / '.private' / 'credentials' / (source + '-read-cache-scope.json')
        previous = read_private_json(path).get('scope') if path.exists() else None
        if scope is None or previous != scope:
            for entry in self.path.glob(source + '--*.json'):
                if not entry.is_symlink():
                    entry.unlink(missing_ok=True)
            if previous != scope:
                atomic_write_private_json(path, {'scope': scope})

    def _stats(self, source, hit):
        path = self.root / '.private' / 'credentials' / (source + '-read-cache-stats.json')
        value = read_private_json(path) if path.exists() else {'hits': 0, 'loads': 0}
        key = 'hits' if hit else 'loads'
        value[key] = value.get(key, 0) + 1
        atomic_write_private_json(path, value)

    def fetch(self, source, kind, key, loader, *, max_age, fresh=False):
        if source not in SOURCES:
            raise ValueError('Unknown cache source')
        started = self._stamp()
        filename = self.path / (source + '--' + digest([kind, key]) + '.json')
        # This is also the provider/auth lock. Other processes wait, then reuse
        # the completed load rather than doing a duplicate request/download.
        with private_source_lock(self.root, source, wait=True, timeout=30):
            scope = self._account_scope(source)
            with self._lock():
                stamp = self._stamp()
                self._scope(source, scope)
                self._cleanup(stamp)
                record = self._record(filename) if filename.exists() else None
                age = stamp - record['stored_at'] if record else RETENTION
                usable = (scope is not None and record and record.get('scope') == scope
                          and 0 <= age < min(max_age, RETENTION)
                          and (not fresh or record['stored_at'] > started))
                if usable:
                    self._stats(source, True)
                    return dict(record, cache_hit=True, age_seconds=age)
                # Never retain a prior account's response under a reused key.
                if record and record.get('scope') != scope:
                    filename.unlink(missing_ok=True)
            data = loader()
            stamp = self._stamp()
            record = {'version': 1, 'scope': account_scope(self.root, source),
                      'stored_at': stamp, 'data': data}
            if kind == 'list':
                record['request'] = {'kind': kind, 'key': key}
            with self._lock():
                self._scope(source, record['scope'])
                self._stats(source, False)
                # Partial reads are returned honestly, but never cached as success.
                if (record['scope'] is not None and data.get('cacheable', True)
                        and len(json.dumps(record, ensure_ascii=False).encode()) <= MAX_ENTRY_BYTES):
                    atomic_write_private_json(filename, record)
                    # A test clock and wall time must use the same expiry origin.
                    os.utime(filename, (stamp, stamp), follow_symlinks=False)
                    self._cleanup(stamp)
                else:
                    filename.unlink(missing_ok=True)
            return dict(record, cache_hit=False, age_seconds=0)

    def peek(self, source, kind, key, *, max_age=RETENTION):
        """Return stored data for a derived result, or None; never loads a source.

        For small results computed outside a source read, such as page check
        verdicts. The same account binding, retention and limits apply as for
        fetch. Neither lookup nor storage counts as a source cache hit or load.
        """
        if source not in SOURCES:
            raise ValueError('Unknown cache source')
        filename = self.path / (source + '--' + digest([kind, key]) + '.json')
        scope = self._account_scope(source)
        with self._lock():
            stamp = self._stamp()
            self._scope(source, scope)
            record = self._record(filename) if filename.exists() else None
            if scope is None or not record or record.get('scope') != scope:
                return None
            age = stamp - record['stored_at']
            return record['data'] if 0 <= age < min(max_age, RETENTION) else None

    def store(self, source, kind, key, data):
        """Store a derived result for peek; returns False when reuse is disabled."""
        if source not in SOURCES:
            raise ValueError('Unknown cache source')
        filename = self.path / (source + '--' + digest([kind, key]) + '.json')
        scope = self._account_scope(source)
        if scope is None:
            return False
        with self._lock():
            stamp = self._stamp()
            self._scope(source, scope)
            record = {'version': 1, 'scope': scope, 'stored_at': stamp, 'data': data}
            if len(json.dumps(record, ensure_ascii=False).encode()) > MAX_ENTRY_BYTES:
                return False
            atomic_write_private_json(filename, record)
            os.utime(filename, (stamp, stamp), follow_symlinks=False)
            self._cleanup(stamp)
            return True

    def status(self):
        """Content-free counters; status alone never fetches or cleans anything."""
        result = {}
        for source in SOURCES:
            path = self.root / '.private/credentials' / (source + '-read-cache-stats.json')
            if path.exists():
                result[source] = dict(read_private_json(path))
            budget = self.root / '.private/credentials' / (source + '-request-budget.json')
            if budget.exists():
                result.setdefault(source, {})['requests'] = dict(read_private_json(budget))
        return {'retention_seconds': RETENTION, 'max_bytes': MAX_BYTES, 'sources': result}

    @staticmethod
    def metadata(record):
        stamp = record['stored_at']
        return {'hit': record['cache_hit'], 'age_seconds': round(record['age_seconds'], 3),
                'fetched_at': record['data'].get('fetched_at') or datetime.fromtimestamp(stamp, UTC).isoformat(),
                'expires_at': datetime.fromtimestamp(stamp + RETENTION, UTC).isoformat(timespec='seconds')}
