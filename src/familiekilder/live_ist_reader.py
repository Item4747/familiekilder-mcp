"""IST Home live reads for the shared reader contract in `sources.py`."""

from contextlib import contextmanager

from .core import FamiliekilderError
from .live_readers import checked_detail, fetch_attachment, ref_item
from .source_adapter import SourceUnavailable


@contextmanager
def connect(root):
    from . import ist_home as m

    yield m, m.access_token(root)


def _snapshot(root, module, token):
    try:
        return module.fetch_snapshot(token)
    except module.IstHomeAuthError:
        # One read-only retry with a refreshed token; never repeat writes.
        return module.fetch_snapshot(module.refresh_token(root))


def list_items(root, m, c, since, *, include_detail_hash=False):
    data = _snapshot(root, m, c)
    result = []
    for post in m.posts_in_window(data["posts"], since):
        item = ref_item(m.normalize_post(post, data["children"], None, None), {"id": str(post["id"])})
        result.append(checked_detail(item, item, include_detail_hash))
    return result, True


def get_item(root, m, c, ref):
    from .live_ist import Client, POST_DETAIL, identifier as ist_identifier, conversation_item

    if ref.get("kind") == "conversation":
        return conversation_item(root, m, c, ref)
    if ref.get("kind") not in {None, "post"}:
        raise FamiliekilderError("Ukjent IST-referansetype")
    ident = ist_identifier(ref.get("id"))
    client = Client(root, m, c)
    post = client.read(POST_DETAIL, {"id": ident}).get("getBlogPost")
    if not isinstance(post, dict) or post.get("id") != ident:
        raise SourceUnavailable("IST-innlegget finnes ikke eller kan ikke identifiseres")
    context = client.read(m.CONTEXT_QUERY).get("getContext") or {}
    return ref_item(m.normalize_post(post, context.get("children") or [], None, None), ref)


def download(root, m, c, item, attachment_id, reference, directory):
    def fetch(spec, target):
        m.download(spec["url"], target, m.DownloadBudget())
        return target, None

    return fetch_attachment(item, attachment_id, directory, fetch)


def discover_chats(runtime, since, until, *, observe=None):
    """One read without checkpoints: the caller records the returned items."""
    from .live_ist import list_conversations

    result = list_conversations(runtime, since, until)
    # Conversation summaries have no last-message marker. Read the thread
    # to detect replies even when its title/editedAt did not change.
    refs = {}
    for row in sorted(result["items"], key=lambda r: r["reference"]["child_id"]):
        refs.setdefault(row["reference"]["id"], row["reference"])
    items = [runtime.getter(runtime.config.root, "ist-home", ref) for ref in refs.values()]
    return items, result["complete"], [result["coverage"]]
