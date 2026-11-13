"""Resumable Spond feed traversal; checkpoints contain only scope and cursors.

A caller that records the items commits them and this checkpoint together:
`observe(conn, source, stream, item, stamp)` runs in the page transaction.
Without it, pages only return their items.
Partial pages are never a successful watermark or evidence of cancellation.
"""
import json
from datetime import datetime, timedelta

from .action_ledger import fingerprint
from .core import FamiliekilderError
from .discovery_checkpoints import ensure_table
from .live_readers import provider, ref_item
from .live_spond_reader import spond_context, spond_item
from .read_cache import account_scope
from .source_diagnostics import failure
from .source_adapter import SourceUnavailable

VERSION = 1
LIMITATIONS = ['Spond gjennomgås fra lagrede sidemarkører. Gamle innlegg kan ha nye endringer; ingen komplett historikkgaranti.']


def _ignore(conn, source, stream, item, stamp):
    return None


def discover(runtime, since, until, *, observe=None):
    observe = observe or _ignore
    root = runtime.config.root
    with provider(root, 'spond') as (m, client):
        # Token rotation inside provider happens before the checkpoint identity
        # is compared. Stable account subjects survive ordinary refreshes.
        scope = account_scope(root, 'spond')
        with runtime.ledger() as ledger:
            ensure_table(ledger.conn)
            row = ledger.conn.execute("SELECT checkpoint_json FROM change_discovery WHERE source='spond' AND stream='feed'").fetchone()
        old = json.loads(row[0]) if row else None
        parts = [*m.POST_TYPES, 'events_asc', 'events_desc']
        reusable = (scope is not None and isinstance(old, dict)
                    and old.get('version') == VERSION and old.get('account_scope') == scope
                    and not old.get('traversed') and set(old.get('parts', {})) == set(parts))
        state = old if reusable else {
            'version': VERSION, 'account_scope': scope, 'since': since, 'until': until,
            'parts': {key: {'cursor': None, 'seen': [], 'done': False} for key in parts},
            'notifications_done': False, 'notification_ids': [], 'record_errors': False,
        }
        cutoff = datetime.fromisoformat(state['since'])
        boundary = datetime.fromisoformat(state['until']).replace(hour=0, minute=0, second=0, microsecond=0)
        profile, _, context = spond_context(m, client)
        items, problems = {}, []

        def commit(page_items):
            # A source-check subprocess may be stopped before the whole feed
            # returns. Commit each validated page with its continuation, never
            # the marker alone, so restarting cannot skip discovered updates.
            with runtime.ledger() as ledger, ledger.conn:
                for item in page_items:
                    observe(ledger.conn, 'spond', 'feed', item, until)
                ledger.conn.execute('INSERT OR REPLACE INTO change_discovery VALUES (?,?,?,?)',
                    ('spond', 'feed', json.dumps(state), until))

        def add(kind, records, *, from_notification=False):
            added = []
            for record in records:
                try:
                    m.identifier(record, 'elementet')
                    if kind == 'post':
                        m.validated_post_payload(record)
                    stamps = m.timestamp_values(record)
                    if not stamps:
                        raise SourceUnavailable('Spond-elementet mangler tidsstempel')
                    if from_notification or max(stamps) >= cutoff:
                        item = spond_item(m, kind, record, context)
                        items[item['source_id']] = item
                        added.append(item)
                except (FamiliekilderError, ValueError, TypeError, KeyError):
                    state['record_errors'] = True
            return added

        def stopped(exc):
            problems.append(failure(exc, at=datetime.fromisoformat(until)))

        # Each subtype gets its existing bounded allowance. A full post stream
        # cannot prevent the other types/events from being visited this round.
        for key in parts:
            part = state['parts'][key]
            if part['done']:
                continue
            for _ in range(m.MAX_PAGES_PER_STREAM):
                budget = getattr(client, 'budget', None)
                if budget is not None and budget.used >= budget.limit:
                    break
                try:
                    if key in m.POST_TYPES:
                        page, following = m.posts_page(client, key, part['cursor'])
                        kind = 'post'
                    else:
                        direction = key.removeprefix('events_')
                        bound = boundary + timedelta(milliseconds=1) if direction == 'asc' else boundary
                        page, following = m.events_page(client, direction, bound.isoformat(), part['cursor'])
                        kind = 'event'
                    marker = fingerprint(following) if following is not None else None
                    if marker in part['seen']:
                        raise SourceUnavailable('Spond gjentok samme side')
                    added = add(kind, page)
                except Exception as exc:
                    stopped(exc)
                    break
                part['cursor'] = following
                part['done'] = following is None
                if following is not None:
                    part['seen'].append(marker)
                # A failed DB write aborts discovery: it must never allow a
                # subsequent page to save an uncommitted earlier continuation.
                commit(added)
                if part['done']:
                    break

        if not state['notifications_done']:
            added = []
            try:
                notices, count = m.fetch_notifications(client, profile.get('locale'), cutoff)
                # At most two entity references per notice. Keep each chunk
                # inside the existing 100-detail cap and checkpoint IDs only.
                seen = set(state.get('notification_ids', []))
                notices = [notice for notice in notices
                           if m.notification_identifier(notice) not in seen
                           and m.parse_time(m.pick(notice, 'timestamp', 'createdAt', 'updatedAt'))
                               <= datetime.fromisoformat(state['until'])]
                remaining = len(notices)
                notices = notices[:m.MAX_NOTIFICATION_DETAILS // 2]
                posts, events = {}, {}
                try:
                    _, detail_errors = m.fetch_notification_details(client, notices, posts, events)
                    state['notification_ids'] = list(dict.fromkeys([
                        *state.get('notification_ids', []), *[m.notification_identifier(notice) for notice in notices]]))
                    state['notifications_done'] = len(notices) == remaining
                    state['record_errors'] |= bool(count or detail_errors)
                finally:
                    added = (add('post', posts.values(), from_notification=True)
                             + add('event', events.values(), from_notification=True))
                    for notice in notices:
                        item = ref_item(m.normalize_notification(notice, profiles=context['profiles']),
                                        {'kind': 'notification', 'id': m.notification_identifier(notice)})
                        items[item['source_id']] = item
                        added.append(item)
            except Exception as exc:
                stopped(exc)
            commit(added)
        state['traversed'] = all(part['done'] for part in state['parts'].values()) and state['notifications_done']
        complete = state['traversed'] and not state['record_errors'] and not problems
        diagnostic = problems[0] if problems else {
            'incomplete_reason': 'incomplete_read' if state['record_errors'] else 'pagination', 'retry_at': None}
        state.update(diagnostic if not complete else {'incomplete_reason': None, 'retry_at': None})
        return list(items.values()), complete, LIMITATIONS, state
