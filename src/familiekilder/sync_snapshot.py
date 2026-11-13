"""Bounded, account-bound source reads shared under one snapshot key.

A `SyncSnapshot` memoizes source reads for as long as its key is current
(`Runtime.snapshot_key()`), so repeated reads in one sync see the same source
state. Only explicit read scopes install this memoizer. Source writes and auth
recovery suspend it, as can a caller that needs a fresh read. Original fetch
times are retained.
"""
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
from datetime import UTC, datetime

from .private_lock import private_source_lock
from .read_cache import MAX_BYTES, MAX_ENTRIES, MAX_ENTRY_BYTES, account_scope, digest
from .source_adapter import SourceUnavailable

_ACTIVE = ContextVar('familiekilder_sync_snapshot', default=None)
_SUSPENDED = ContextVar('familiekilder_sync_snapshot_suspended', default=False)


def current_key(source):
    """Key of the snapshot that served `source` in the active read scope, if any."""
    active = _ACTIVE.get()
    if active is not None and not _SUSPENDED.get() and source in active['sources']:
        return active['snapshot'].key
    return None


def fetched_at(source):
    active = _ACTIVE.get()
    stamp = active['fetched'].get(source) if active is not None and not _SUSPENDED.get() else None
    return datetime.fromtimestamp(stamp, UTC).isoformat() if stamp is not None else None


def suspended():
    return _SUSPENDED.get()


@contextmanager
def suspend_snapshot():
    token = _SUSPENDED.set(True)
    try:
        yield
    finally:
        _SUSPENDED.reset(token)


@contextmanager
def snapshot_scope(snapshot, root, source=None):
    token = _ACTIVE.set({'snapshot': snapshot, 'root': Path(root).resolve(), 'sources': set(), 'fetched': {}}
                        if snapshot is not None else None)
    try:
        yield
    except BaseException:
        if snapshot is not None:
            snapshot.clear(source)
        raise
    finally:
        _ACTIVE.reset(token)


class SyncSnapshot:
    def __init__(self, key):
        self.key = key
        self._entries = OrderedDict()
        self._bindings = {}
        self._bytes = 0
        self._lock = threading.RLock()
        self._stats = {}

    def status(self):
        with self._lock:
            return {'entries': len(self._entries), 'bytes': self._bytes,
                    'sources': deepcopy(self._stats)}

    def _mark(self, source, stamp):
        active = _ACTIVE.get()
        if active is not None and active['snapshot'] is self:
            active['sources'].add(source)
            active['fetched'][source] = max(stamp, active['fetched'].get(source, stamp))

    def clear(self, source=None):
        with self._lock:
            for key in list(self._entries):
                if source is None or key[0] == source:
                    self._bytes -= self._entries.pop(key)['bytes']
            if source is None:
                self._bindings.clear()
            else:
                self._bindings.pop(source, None)

    @staticmethod
    def cacheable(value):
        if isinstance(value, dict):
            if value.get('isError') or value.get('errors') or value.get('cacheable') is False:
                return False
            data = value.get('data')
            if isinstance(data, dict) and data.get('cacheable') is False:
                return False
        return True

    def read(self, root, source, key, loader, *, record=False, source_lock=True, refresh_before=None):
        root = Path(root).resolve()
        entry_key = (source, digest([str(root), key]))
        # Acquire the source lock before the short-lived memory lock. Adapter
        # hooks can re-enter here while a higher-level record is being loaded.
        with (private_source_lock(root, source, wait=True, timeout=30) if source_lock else nullcontext()):
            binding = account_scope(root, source)
            with self._lock:
                if self._bindings.get(source) != (str(root), binding) or binding is None:
                    self.clear(source)
                    self._bindings[source] = (str(root), binding)
                cached = self._entries.get(entry_key) if binding is not None else None
                if cached is not None and refresh_before is not None:
                    original = cached['value'].get('data', {}).get('fetched_at')
                    if not isinstance(original, str) or original < refresh_before:
                        self._bytes -= self._entries.pop(entry_key)['bytes']
                        cached = None
                stats = self._stats.setdefault(source, {'hits': 0, 'loads': 0})
                if cached is not None:
                    stats['hits'] += 1
                    self._mark(source, cached['fetched'])
                    value = deepcopy(cached['value'])
                    if record:
                        value.update(cache_hit=True,
                                     age_seconds=max(0, time.time() - cached['fetched']))
                    return value
                stats['loads'] += 1
            try:
                value = loader()
            except BaseException:
                self.clear(source)
                raise
            if account_scope(root, source) != binding:
                self.clear(source)
                raise SourceUnavailable('Kontoen er endret under lesing; hent kildegrunnlaget på nytt')
            if binding is None or not self.cacheable(value):
                return value
            stamp = value['stored_at'] if record else time.time()
            if record:
                original = value.get('data', {}).get('fetched_at')
                if original:
                    try:
                        stamp = min(stamp, datetime.fromisoformat(original).timestamp())
                    except (TypeError, ValueError, OverflowError):
                        pass
            self._mark(source, stamp)
            if record:
                value = dict(value, snapshot_job=self.key, snapshot_scope=binding,
                             age_seconds=max(0, time.time() - stamp))
            try:
                size = len(json.dumps(value, ensure_ascii=False).encode())
            except (TypeError, ValueError):
                return value
            if size > MAX_ENTRY_BYTES:
                return value
            with self._lock:
                self._entries[entry_key] = {'value': deepcopy(value), 'bytes': size, 'fetched': stamp}
                self._bytes += size
                while self._bytes > MAX_BYTES or len(self._entries) > MAX_ENTRIES:
                    _, oldest = self._entries.popitem(last=False)
                    self._bytes -= oldest['bytes']
            return value


def memo_read(root, source, key, loader):
    active = _ACTIVE.get()
    if active is None or _SUSPENDED.get():
        return loader()
    snapshot, selected_root = active['snapshot'], active['root']
    root = selected_root if root is None else Path(root).resolve()
    if root != selected_root:
        return loader()
    value = snapshot.read(root, source, ('request', key), loader)
    return value
