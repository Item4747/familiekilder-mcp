"""Hoopit live reads for the shared reader contract in `sources.py`."""

from contextlib import contextmanager
from pathlib import Path

from .core import FamiliekilderError
from .live_readers import identifier, attachment_metadata, checked_detail, fetch_attachment, ref_item
from .source_adapter import SourceUnavailable


@contextmanager
def connect(root):
    from . import hoopit as m

    m.ensure_direct_access(root)
    limiter = m.BudgetedRateLimiter(
        m.PersistentRateLimiter(root), m.RequestBudget()
    )
    access = m.session_for_use(root, rate_limiter=limiter)
    client = m.AuthorizedClient(access, m.default_transport(access), limiter)
    client.root = Path(root)
    yield m, client


def _item(m, kind, record):
    if kind == "notification":
        item = m.notification_item(record)
        ref = {"kind": kind, "id": str(record["id"])}
    elif kind == "post":
        item = m.post_item(
            record,
            attachments=[
                attachment_metadata(value)
                for value in m.attachment_rows(m.require_mapping(record.get("post"), "post").get("attachments"))
            ],
        )
        ref = {"kind": kind, "id": str(record["post"]["id"])}
    else:
        item = m.event_item(
            record,
            attachments=[
                attachment_metadata(value)
                for value in m.attachment_rows(m.require_mapping(record.get("detail"), "aktivitetsdetalj").get("attachments"))
            ],
        )
        _, event, occurrence = item["source_id"].split(":")
        ref = {"kind": "event", "id": event, "occurrence_id": occurrence}
    return ref_item(item, ref)


def list_notifications(m, c, cutoff):
    notices = []
    for notice in m.iter_feed(c, '/app/users/current/notifications/',
            label='varsler', maximum_pages=m.MAX_PAGES_PER_FEED):
        stamp = m.latest_time(notice, ('timestamp', 'created_at'))
        if stamp is None:
            raise SourceUnavailable('Hoopit-varslet mangler en pålitelig tidsangivelse')
        if stamp >= cutoff:
            if len(notices) >= m.MAX_NOTIFICATIONS:
                raise m.RequestBudgetExceeded('Hoopit returnerte for mange varsler')
            notices.append(_item(m, 'notification', notice))
    return notices, True


def list_items(root, m, c, since, *, include_detail_hash=False):
    data = m.fetch_snapshot(c, since)
    result = []
    for key, kind in (
        ("posts", "post"),
        ("events", "event"),
        ("notifications", "notification"),
    ):
        for row in data[key]:
            item = _item(m, kind, row)
            if kind == "notification":
                result.append(item)
                continue
            canonical = (_item(m, kind, {**row, "summary": {}})
                         if include_detail_hash and kind == "event" else item)
            result.append(checked_detail(item, canonical, include_detail_hash))
    return result, True


def get_item(root, m, c, ref):
    if ref.get("kind") == "chat_message":
        from .live_hoopit_chat_details import get_message_item

        return get_message_item(m, c, ref)
    ident = identifier(ref.get("id"))
    kind = ref.get("kind")
    if kind == "post":
        post = c.get(f"/app/posts/{ident}/")
        record = {"post": post, "comments": m.post_comments(c, post)}
    elif kind == "event":
        occurrence = identifier(ref.get("occurrence_id"))
        detail = c.get(f"/app/events/{ident}/occurrences/{occurrence}/")
        record = {
            "detail": detail,
            "summary": {},
            "comments": m.event_comments(c, ident, occurrence),
        }
    else:
        raise FamiliekilderError("Varsler må hentes gjennom listeverktøyet")
    return _item(m, kind, record)


def download(root, m, c, item, attachment_id, reference, directory):
    def fetch(spec, target):
        if reference.get("kind") == "chat_message":
            from .live_hoopit_chat_details import download

            result = download(c, spec.get("url"), target, m.MAX_ATTACHMENT_BYTES)
        else:
            result = c.download(spec.get("url"), target, max_bytes=m.MAX_ATTACHMENT_BYTES)
        return target, getattr(result, "content_type", None)

    return fetch_attachment(item, attachment_id, directory, fetch)


def discover_chats(runtime, since, until, *, observe=None):
    from .chat_discovery import discover

    return discover(runtime, "hoopit", since, until, observe=observe)
