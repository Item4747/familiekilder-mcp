"""Skolemelding live reads for the shared reader contract in `sources.py`."""

from contextlib import contextmanager
from pathlib import Path

from .core import FamiliekilderError
from .live_readers import attachment_metadata, checked_detail, fetch_attachment, ref_item
from .source_adapter import SourceUnavailable

# Summary fields a message reference may carry; nothing else is persisted.
REFERENCE_KEYS = (
    "MessageId",
    "messageId",
    "Id",
    "id",
    "ThreadId",
    "threadId",
    "IsReplyAllowed",
    "isReplyAllowed",
    "OnBehalfOf",
    "onBehalfOf",
    "IsFavourite",
    "isFavourite",
    "IsHistoric",
    "isHistoric",
)


def message_reference(summary):
    return {"summary": {k: summary[k] for k in REFERENCE_KEYS if k in summary}}


def reference_values(reference):
    summary = reference.get("summary")
    if (set(reference) != {"summary"} or not isinstance(summary, dict)
            or summary.keys() - set(REFERENCE_KEYS)):
        raise FamiliekilderError("Ugyldig meldingsreferanse")
    return list(summary.values())


@contextmanager
def connect(root):
    from . import skolemelding as m

    cfg = m.load_client_config(root)
    yield m, m.AuthorizedClient(root, m.default_transport(cfg), None, cfg)


def list_items(root, m, c, since, *, include_detail_hash=False):
    profile, info = m.fetch_context(c, m.load_client_config(root).app_version)
    children = m.children(profile, info)
    names = {v.search_id: v.name for v in children}
    rows = m.fetch_summaries(c, children, since)
    result = []
    complete = True
    for summary in rows:
        detail = m.detail(c.get_json(m.detail_url(summary)))
        if not m.timestamp_values(summary) and not m.timestamp_values(detail):
            complete = False
        if m.message_in_window(summary, since, detail):
            specs = [
                attachment_metadata({"url": u, "name": n})
                for u, n in m.attachment_specs(detail)
            ]
            item = ref_item(m.normalize_message(
                summary, detail, child_names=names, attachments=specs), message_reference(summary))
            canonical = item
            if include_detail_hash:
                canonical = ref_item(m.normalize_message(item["reference"]["summary"], detail,
                    child_names=names, attachments=specs), item["reference"])
            result.append(checked_detail(item, canonical, include_detail_hash))
    return result, complete


def get_item(root, m, c, ref):
    summary = ref.get("summary")
    if not isinstance(summary, dict):
        raise FamiliekilderError("Ugyldig meldingsreferanse")
    detail = m.detail(c.get_json(m.detail_url(summary)))
    profile, info = m.fetch_context(c, m.load_client_config(root).app_version)
    names = {v.search_id: v.name for v in m.children(profile, info)}
    specs = [
        attachment_metadata({"url": u, "name": n})
        for u, n in m.attachment_specs(detail)
    ]
    return ref_item(
        m.normalize_message(summary, detail, child_names=names, attachments=specs),
        message_reference(summary),
    )


def download(root, m, c, item, attachment_id, reference, directory):
    def fetch(spec, target):
        # Use the existing exact-host/auth-mode download implementation.
        detail = {"Attachments": [spec]}
        files = m.download_attachments(
            c, directory, item["source_id"], detail, m.DownloadBudget()
        )
        if not files:
            raise SourceUnavailable("Vedlegget kunne ikke lastes ned")
        return Path(files[0]["path"]), files[0].get("mime_type")

    return fetch_attachment(item, attachment_id, directory, fetch)
