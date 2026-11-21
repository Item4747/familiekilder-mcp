"""Hoopit live reads for the shared reader contract in `sources.py`.

References: ``{"kind": "post", "id": "<post id>", "day": "YYYY-MM-DD"}`` and
``{"kind": "event", "id": "<event id>", "occurrence_id": "<id>", "day": "YYYY-MM-DD"}``.
``day`` is the post's creation day or the occurrence's day (Oslo time). It
lets ``get_item`` stop paging the post list once posts are older, and look an
occurrence up in a few days' window. Without it the search is wider.
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .core import FamiliekilderError
from .live_readers import checked_detail, default_reference_values, identifier, ref_item


@contextmanager
def connect(root):
    from . import hoopit as m

    limiter = m.BudgetedRateLimiter(m.PersistentRateLimiter(root), m.RequestBudget())
    access = m.token_for_use(root)
    client = m.Client(access, m.default_transport(), limiter, root=Path(root))
    yield m, client


def reference_values(reference):
    values = default_reference_values(reference, extra_keys=("day",))
    day = reference.get("day")
    if day is not None:
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except (TypeError, ValueError):
            raise FamiliekilderError("Ugyldig dag i Hoopit-referansen") from None
    return values


def _post(m, record):
    item = m.post_item(record)
    post_id = m.identifier(record["post"].get("id"), "innleggs-ID")
    reference = {"kind": "post", "id": post_id}
    day = m.post_day(record["post"])
    if day:
        reference["day"] = day
    return ref_item(item, reference)


def _events(root, m, occurrences):
    items = []
    for occurrence in occurrences:
        item = m.event_item(occurrence)
        _, event, number = item["source_id"].split(":")
        reference = {"kind": "event", "id": event, "occurrence_id": number}
        day = m.occurrence_day(occurrence)
        if day:
            reference["day"] = day
        items.append(ref_item(item, reference))
    m.observed_changed_at(root, items)
    return items


def list_items(root, m, c, since, *, include_detail_hash=False):
    data = m.fetch_snapshot(c, since)
    result = [_post(m, row) for row in data["posts"]]
    result.extend(_events(root, m, data["events"]))
    # The detail getter builds the same item, so the item is its own canonical form.
    items = [checked_detail(item, dict(item), include_detail_hash) for item in result]
    # A limit gives a partial list (complete=false), never a failed read.
    return items, not data["limits"]


def get_item(root, m, c, ref):
    kind = ref.get("kind")
    if kind == "post":
        post_id = m.identifier(identifier(ref.get("id")), "innleggs-ID")
        floor = None
        if ref.get("day") is not None:
            try:
                day = datetime.strptime(ref["day"], "%Y-%m-%d").replace(tzinfo=UTC)
            except (TypeError, ValueError):
                raise FamiliekilderError("Ugyldig dag i Hoopit-referansen") from None
            # A day before the creation day covers the Oslo/UTC boundary.
            floor = day - timedelta(days=1)
        missing = m.HoopitItemUnavailable(
            "Fant ikke innlegget i Hoopit. Det kan være slettet eller eldre enn listen viser."
        )
        # No single-post endpoint for members: find the post in the newest-first list.
        try:
            for page in m.iter_pages(
                c, "posts", label="innlegg", maximum_pages=m.MAX_PAGES_PER_FEED
            ):
                for post in page:
                    if m.identifier(post.get("id"), "innleggs-ID") == post_id:
                        record = {"post": post}
                        try:
                            record["comments"] = m.post_comments(c, post_id)
                        except m.HoopitItemUnavailable:
                            record.update(comments=[], comments_unavailable=True)
                        return _post(m, record)
                created = [m._parse_time(post.get("created_at")) for post in page]
                if floor is not None and page and all(t is not None and t < floor for t in created):
                    raise missing
        except m.RequestBudgetExceeded:
            raise missing from None
        raise missing
    if kind == "event":
        occurrence = m.find_occurrence(
            c,
            identifier(ref.get("id")),
            identifier(ref.get("occurrence_id")),
            ref.get("day"),
        )
        return _events(root, m, [occurrence])[0]
    raise FamiliekilderError("Hoopit-referansen må gjelde et innlegg eller en aktivitet")


def download(root, m, c, item, attachment_id, reference, directory):
    raise FamiliekilderError("Hoopits API gir ikke vedlegg; åpne innlegget i Hoopit-appen")
