"""Spond live reads for the shared reader contract in `sources.py`."""

import time
import urllib.parse
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from .core import FamiliekilderError
from .live_readers import identifier, attachment_metadata, fetch_attachment, ref_item
from .source_adapter import SourceUnavailable

_SPOND_READ_SCOPE = ContextVar('familiekilder_spond_read_scope', default=None)


@contextmanager
def spond_read_scope(root):
    """Reuse account metadata only within this synchronous source scan."""
    root = Path(root).resolve()
    active = _SPOND_READ_SCOPE.get()
    if active is not None and active['root'] == root:
        yield
        return
    token = _SPOND_READ_SCOPE.set({'root': root, 'account': None, 'values': {}})
    try:
        yield
    finally:
        _SPOND_READ_SCOPE.reset(token)


read_scope = spond_read_scope


def spond_read_value(client, key, loader):
    from .read_cache import account_scope

    active = _SPOND_READ_SCOPE.get()
    root = getattr(client, 'root', None)
    if active is None or root is None or Path(root).resolve() != active['root']:
        return loader()
    scope = account_scope(active['root'], 'spond')
    if scope is None or scope != active['account']:
        active['values'].clear()
        active['account'] = scope
    if scope is None:
        return loader()
    if key not in active['values']:
        value = loader()
        if account_scope(active['root'], 'spond') != scope:
            active['values'].clear()
            raise SourceUnavailable('Spond-kontoen er endret; start oppslaget på nytt')
        active['values'][key] = value
    return deepcopy(active['values'][key])


def spond_context(m, c):
    def load():
        p = c.get_json(m.PROFILE_ENDPOINT)
        groups = m.group_list(c.get_json(m.GROUPS_ENDPOINT))
        names = m.profile_names(groups)
        pid = str(m.pick(p, "id", "profileId"))
        names[pid] = m.person_name(p) or pid
        children, group_names = m.group_children(groups, pid)
        return p, groups, dict(profiles=names, children=children, group_names=group_names)
    return spond_read_value(c, 'context', load)


def spond_item(m, kind, raw, context):
    spec = [attachment_metadata(value) for value in m.attachment_candidates(raw)]
    for a in spec:
        a["_spec"]["_group_id"] = m.entity_group_id(raw)
    fn = m.normalize_event if kind == "event" else m.normalize_post
    return ref_item(
        fn(raw, **context, attachments=spec, notification_time=None),
        {"kind": kind, "id": str(raw["id"])},
    )


@contextmanager
def connect(root):
    from . import spond as m

    yield m, m.AuthorizedClient(
        root,
        transport=None,
        auth_transport=None,
        budget=m.RequestBudget(),
        wall_time=time.time,
        pacing_clock=time.time,
        sleep=time.sleep,
    )


def _notification(m, notice, context):
    return ref_item(m.normalize_notification(notice, profiles=context['profiles']),
                    {'kind': 'notification', 'id': m.notification_identifier(notice)})


def list_notifications(m, c, cutoff):
    profile, _, context = spond_context(m, c)
    notices, failures = m.fetch_notifications(c, profile.get('locale'), cutoff)
    return [_notification(m, notice, context) for notice in notices], not failures


def list_items(root, m, c, since, *, include_detail_hash=False):
    profile, groups, context = spond_context(m, c)
    cutoff = datetime.fromisoformat(since)
    posts, pfail = m.fetch_posts(c, cutoff)
    events, efail = m.fetch_events(c, cutoff)
    notices, nfail = m.fetch_notifications(c, profile.get("locale"), cutoff)
    _, dfail = m.fetch_notification_details(c, notices, posts, events)
    result = [spond_item(m, "post", r, context) for r in posts.values()]
    result += [spond_item(m, "event", r, context) for r in events.values()]
    result += [_notification(m, n, context) for n in notices]
    return result, not any((pfail, efail, nfail, dfail))


def get_item(root, m, c, ref):
    if ref.get("kind") == "chat_message":
        from .live_spond_chat import get_message_item

        return get_message_item(m, c, ref)
    _, _, context = spond_context(m, c)
    kind = ref.get("kind")
    ident = identifier(ref.get("id"))
    if kind not in {"post", "event"}:
        raise FamiliekilderError("Varsler må hentes gjennom listeverktøyet")
    endpoint = (
        (m.POSTS_ENDPOINT if kind == "post" else m.API_ROOT + "/sponds")
        + "/"
        + ident
    )
    query = (
        {
            "includeComments": True,
            "includeReadStatus": False,
            "includeSeenCount": False,
            "addGroupInfo": True,
        }
        if kind == "post"
        else {"includeComments": True, "addProfileInfo": True}
    )
    raw = c.get_json(m.url_with_query(endpoint, query))
    return spond_item(m, kind, raw, context)


def download(root, m, c, item, attachment_id, reference, directory):
    def fetch(spec, target):
        url = next(
            (
                spec[k]
                for k in ("media", "url", "downloadUrl", "fileUrl", "imageUrl")
                if isinstance(spec.get(k), str)
            ),
            None,
        )
        if url and url.startswith("/"):
            url = urllib.parse.urljoin(m.BASE_API_URL + "/", url)
        result = c.download(
            url,
            target,
            group_id=spec.get("_group_id"),
            max_bytes=m.MAX_ATTACHMENT_BYTES,
            **({'owner_id': spec['ownerId']} if spec.get('ownerId') else {}),
        )
        return target, getattr(result, "content_type", None)

    return fetch_attachment(item, attachment_id, directory, fetch)


def discover_feed(runtime, since, until, *, observe=None):
    from .spond_discovery import discover

    return discover(runtime, since, until, observe=observe)


def discover_chats(runtime, since, until, *, observe=None):
    from .chat_discovery import discover

    return discover(runtime, "spond", since, until, observe=observe)
