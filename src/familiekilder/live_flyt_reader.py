"""Flyt Foresatt live reads for the shared reader contract in `sources.py`."""

from contextlib import contextmanager
from datetime import datetime

from .core import FamiliekilderError
from .live_readers import checked_detail, fetch_attachment, identifier, ref_item

_KINDS = frozenset({"announcement", "diary", "message", "event", "absence", "attendance"})


def reference(item):
    """Only IDs: kind, the provider ID, the placement and an event's occurrence."""
    kind, placement_id, raw_id = item["source_id"].split(":", 2)
    ref = {"kind": kind, "id": raw_id, "child_id": placement_id}
    if kind == "event":
        ref["id"], ref["occurrence_id"] = raw_id.rsplit(":", 1)
    return ref


def _checked(ref):
    if not isinstance(ref, dict) or ref.get("kind") not in _KINDS:
        raise FamiliekilderError("Ugyldig Flyt-referanse")
    expected = {"kind", "id", "child_id"} | ({"occurrence_id"} if ref["kind"] == "event" else set())
    if set(ref) != expected:
        raise FamiliekilderError("Ugyldig Flyt-referanse")
    for key in expected - {"kind"}:
        identifier(ref[key])
    return ref


@contextmanager
def connect(root):
    from . import flyt as m

    yield m, m.Client(root)


def list_items(root, m, c, since, *, include_detail_hash=False):
    items, complete = m.collect(c, datetime.fromisoformat(since))
    result = []
    for item in m.unique(items):
        item = ref_item(item, reference(item))
        # The detail getter normalizes the same row the same way.
        result.append(checked_detail(item, item, include_detail_hash))
    return result, complete


def get_item(root, m, c, ref):
    ref = _checked(ref)
    placement = m.placement_for(c, ref["child_id"])
    item = m.find(c, placement, ref["kind"], ref["id"], ref.get("occurrence_id"))
    return ref_item(item, reference(item))


def download(root, m, c, item, attachment_id, reference, directory):
    def fetch(spec, target):
        result = c.download(spec["url"], target, m.DownloadBudget())
        return target, getattr(result, "content_type", None)

    return fetch_attachment(item, attachment_id, directory, fetch)
