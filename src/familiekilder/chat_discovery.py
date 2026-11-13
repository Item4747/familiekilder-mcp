"""Resumable chat discovery with atomic identifier-only page checkpoints.

`observe(conn, source, stream, item, stamp)` is the caller's hook for recording
each page's items in the same transaction as its checkpoint, for example in a
change tracker that shares the ledger; without it, pages only return their
items.
"""
import copy
import json
from datetime import datetime

from .action_ledger import fingerprint
from .core import FamiliekilderError
from .discovery_checkpoints import ensure_table
from .private_lock import private_named_lock, private_source_lock
from .read_cache import account_scope
from .source_adapter import SourceUnavailable
from .source_diagnostics import failure

VERSION = 1
MAX_PAGES = 100
LIMITATIONS = {
    'spond': ['Sponds tidsmarkør kan overse chatter med likt sidetidspunkt.'],
    'hoopit': ['Hoopit-søk dekker indekserte vanlige meldinger og svar; ikke system-/slettede meldinger.',
               'Offsetpaginering og samtidige kildeendringer kan begrense dekningen.'],
}


def _instant(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise SourceUnavailable('Chatmeldingen mangler tidssone')
    return stamp


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise SourceUnavailable('Chatten mangler stabil referanse')
    return value


def _ignore(conn, source, stream, item, stamp):
    return None


def discover(runtime, source, since, until, *, observe=None):
    from .live_readers import read_scope
    with read_scope(runtime.config.root, [source]), \
            private_named_lock(runtime.config.root, 'changes'):
        return _discover(runtime, source, since, until, observe or _ignore)


def _discover(runtime, source, since, until, observe):
    from .live_spond_chat import list_chats, get_chat
    from .live_hoopit_chat import read as channels, SearchCursorRejected
    from .live_hoopit_history import discovery_page

    scope = account_scope(runtime.config.root, source)
    with runtime.ledger() as ledger:
        ensure_table(ledger.conn)
        row = ledger.conn.execute('SELECT checkpoint_json FROM change_discovery WHERE source=? AND stream=\'chat\'',
                                  (source,)).fetchone()
    old = json.loads(row[0]) if row else None
    reusable = (scope is not None and isinstance(old, dict) and old.get('version') == VERSION
                and old.get('account_scope') == scope and not old.get('traversed'))
    state = old if reusable else {
        'version': VERSION, 'account_scope': scope, 'since': since, 'until': until,
        'list_cursor': None if source == 'spond' else 0, 'list_done': False,
        'pending_chats': [], 'seen_chats': [], 'seen_lists': [],
        'message_cursor': None, 'seen_messages': [], 'seen_message_pages': [], 'traversed': False,
    }
    lower, upper = _instant(state['since']), _instant(state['until'])
    items, diagnostic = [], None

    def commit(updated, page_items=()):
        if account_scope(runtime.config.root, source) != scope:
            raise SourceUnavailable('Chatkontoen er endret; ingen side er lagret')
        # An interrupted DB write cannot move the continuation beyond evidence.
        with runtime.ledger() as ledger, ledger.conn:
            for item in page_items:
                observe(ledger.conn, source, 'chat', item, state['until'])
            ledger.conn.execute('INSERT OR REPLACE INTO change_discovery VALUES (?,?,?,?)',
                (source, 'chat', json.dumps(updated), until))
        state.clear()
        state.update(updated)
        items.extend(page_items)

    for _ in range(MAX_PAGES):
        updated = copy.deepcopy(state)
        try:
            with private_source_lock(runtime.config.root, source, wait=True, timeout=30):
                if account_scope(runtime.config.root, source) != scope:
                    raise SourceUnavailable('Chatkontoen er endret; start neste kontroll på nytt')
                if not state['pending_chats']:
                    if state['list_done']:
                        updated['traversed'] = True
                        commit(updated)
                        break
                    page = (list_chats(runtime, state['list_cursor']) if source == 'spond'
                            else channels(runtime, state['list_cursor']))
                    chats = []
                    for chat in page['chats']:
                        ident = _identifier(chat['reference']['id' if source == 'spond' else 'cid'])
                        if ident in updated['seen_chats']:
                            raise SourceUnavailable('Chatlisten gjentok en referanse')
                        updated['seen_chats'].append(ident)
                        if source != 'spond' or _instant(chat['newest_at']) >= lower:
                            chats.append(ident)
                    done = page['complete' if source == 'spond' else 'chat_list_complete']
                    following = page['next_before' if source == 'spond' else 'next_offset']
                    if not done:
                        marker = fingerprint(following)
                        if following is None or following == state['list_cursor'] or marker in state['seen_lists']:
                            raise SourceUnavailable('Chatlisten mangler fremdrift')
                        updated['seen_lists'].append(marker)
                    updated.update(pending_chats=chats, list_done=done, list_cursor=following)
                    updated['traversed'] = done and not chats
                    commit(updated)
                else:
                    ident = state['pending_chats'][0]
                    page = (get_chat(runtime, ident, state['message_cursor']) if source == 'spond'
                            else discovery_page(runtime, ident, state['since'], state['until'], state['message_cursor']))
                    page_items, stamps = [], []
                    for message in page['messages']:
                        if source == 'spond':
                            key = message['source_id']
                            stamp = _instant(message['timestamp'])
                            stamps.append(stamp)
                            eligible = lower <= stamp < upper
                            item = message
                        else:
                            key = 'chat:' + ident + ':message:' + _identifier(message['reference']['id'])
                            item = dict(message, source_id=key)
                            eligible = True  # Existing search verifies the fixed interval.
                        marker = fingerprint(key)
                        if marker in updated['seen_messages']:
                            raise SourceUnavailable('Chatmeldingssiden gjentok en referanse')
                        updated['seen_messages'].append(marker)
                        if eligible:
                            page_items.append(item)
                    done = (page['complete'] or bool(stamps and stamps == sorted(stamps, reverse=True) and stamps[-1] < lower)
                            if source == 'spond' else page['end_of_results'])
                    following = page['next_before_msg_num' if source == 'spond' else 'next_cursor']
                    if not done:
                        marker = fingerprint(following)
                        if following is None or following == state['message_cursor'] or marker in state['seen_message_pages']:
                            raise SourceUnavailable('Chatmeldingssiden mangler fremdrift')
                        updated['seen_message_pages'].append(marker)
                    if done:
                        updated['pending_chats'].pop(0)
                        updated.update(message_cursor=None, seen_messages=[], seen_message_pages=[])
                    else:
                        updated['message_cursor'] = following
                    updated['traversed'] = updated['list_done'] and not updated['pending_chats']
                    commit(updated, page_items)
        except SearchCursorRejected as exc:
            # Provider cursors may expire. Re-read only this chat on the next
            # run; already committed observations deduplicate in the ledger.
            updated = copy.deepcopy(state)
            updated.update(message_cursor=None, seen_messages=[], seen_message_pages=[])
            with private_source_lock(runtime.config.root, source, wait=True, timeout=30):
                commit(updated)
            diagnostic = failure(exc, at=_instant(until))
            break
        except (FamiliekilderError, ValueError, TypeError, KeyError, OSError) as exc:
            diagnostic = failure(exc, at=_instant(until))
            break
        if state['traversed']:
            break
    complete = state['traversed']
    state.update(diagnostic if diagnostic else
                 {'incomplete_reason': None if complete else 'pagination', 'retry_at': None})
    return items, complete, LIMITATIONS[source], state
