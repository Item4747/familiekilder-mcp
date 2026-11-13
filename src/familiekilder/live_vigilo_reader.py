"""Vigilo live reads for the shared reader contract in `sources.py`."""

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .core import FamiliekilderError
from .live_readers import (
    identifier, checked_detail, default_reference_values, fetch_attachment, ref_item,
)
from .source_adapter import SourceUnavailable


class RefreshingClient:
    """Preserve the collector's single-refresh-on-401 behavior for live GETs."""

    def __init__(self, root, module, transport, token, already_refreshed):
        self.root = root
        self.module = module
        self.transport = transport
        self.token = token
        self.refreshed = already_refreshed

    def _call(self, method, *args, **kwargs):
        from .api_transport import ApiHTTPError

        authenticated = kwargs.get("bearer_token") is not None
        if authenticated:
            kwargs["bearer_token"] = self.token
        try:
            return getattr(self.transport, method)(*args, **kwargs)
        except ApiHTTPError as exc:
            if not authenticated or exc.status_code != 401 or self.refreshed:
                raise
        self.refreshed = True
        self.module.refresh_session(self.root)
        self.token = str(self.module.local_session_for_use(self.root)["access_token"])
        kwargs["bearer_token"] = self.token
        return getattr(self.transport, method)(*args, **kwargs)

    def get_json(self, *args, **kwargs):
        from .sync_snapshot import memo_read
        return memo_read(self.root, 'vigilo', ('GET', args, {k: v for k, v in kwargs.items()
                         if k != 'bearer_token'}), lambda: self._call("get_json", *args, **kwargs))

    def download(self, *args, **kwargs):
        return self._call("download", *args, **kwargs)


@contextmanager
def connect(root):
    from . import vigilo as m

    token, refreshed = m.access_token_for_use(root)
    session = m.local_session_for_use(root)
    yield m, (
        RefreshingClient(root, m, m.api_transport(session), token, refreshed),
        token,
        str(session["app_version"]),
    )


def reference_values(reference):
    extra = ()
    if reference.get("kind") == "pickup_note":
        from .live_vigilo_pickup import _requested_date
        extra = ("organization_id", "on_date")
        identifier(reference.get("organization_id"))
        if not isinstance(reference.get("on_date"), str):
            raise FamiliekilderError("Hentemeldingsreferansen må angi on_date")
        _requested_date(reference["on_date"])
    return default_reference_values(reference, extra)


def _item(m, c, kind, entry, children):
    client, token, version = c
    names = {str(x["childId"]): m.child_name(x) for x in children}
    fn = m.normalize_thread if kind == "thread" else m.normalize_news
    item = fn(
        entry,
        names,
        client=client,
        token=token,
        app_version=version,
        incoming=None,
        budget=m.DownloadBudget(),
    )
    return ref_item(
        item,
        {
            "kind": kind,
            "id": item["source_id"].split(":", 1)[1],
            "child_ids": entry.get("child_ids", []),
        },
    )


def list_items(root, m, c, since, *, include_detail_hash=False):
    client, token, version = c
    data = m.fetch_snapshot(token, since, transport=client, app_version=version)
    result = []
    for kind, rows in (("thread", data["message_threads"]), ("news", data["news"])):
        for row in rows:
            item = _item(m, c, kind, row, data["children"])
            canonical = item
            if include_detail_hash and kind == "thread":
                canonical = _item(m, c, kind,
                    {**row, "summary": {"threadUid": item["reference"]["id"]}}, data["children"])
            result.append(checked_detail(item, canonical, include_detail_hash))
    return result, True


def get_item(root, m, c, ref):
    if ref.get("kind") == "pickup_note":
        from .live_vigilo_pickup import get_item

        return get_item(root, m, c, ref)
    if ref.get("kind") == "registration":
        from .live_vigilo_registrations import get_item

        return get_item(root, m, c, ref)
    if ref.get("kind") == "meeting":
        from .live_vigilo_meetings import get_item

        return get_item(root, m, c, ref)
    client, token, version = c
    ident = identifier(ref.get("id"))
    kind = ref.get("kind")
    children = m.fetch_children(client, token, version)
    child_ids = ref.get("child_ids", [])
    if (
        not isinstance(child_ids, list)
        or not child_ids
        or not set(child_ids) <= {str(v["childId"]) for v in children}
    ):
        raise FamiliekilderError(
            "Referansen må angi et barn fra denne innloggingen"
        )
    if kind == "thread":
        detail = m.full_thread(client, token, version, ident, child_ids[0])
        summary = {"threadUid": ident}
    elif kind == "news":
        # Scheduling fields live only in the feed summary. Resolve it anew;
        # caller-supplied/cached fields would defeat the freshness check.
        summary = next(
            (
                row
                for row in m.news_pages(
                    client, token, version, child_ids[0],
                    datetime.min.replace(tzinfo=UTC),
                )
                if str(row.get("id")) == ident
            ),
            None,
        )
        if summary is None:
            raise SourceUnavailable("Vigilo-nyhetens ferske sammendrag finnes ikke")
        detail = m.get(client, token, version, "api/posts/" + ident)
    else:
        raise FamiliekilderError("Ukjent Vigilo-element")
    return _item(
        m,
        c,
        kind,
        {"summary": summary, "detail": detail, "child_ids": child_ids},
        children,
    )


def download(root, m, c, item, attachment_id, reference, directory):
    def fetch(spec, target):
        client, token, version = c
        files = m.download_attachments(
            iter([spec]),
            client=client,
            token=token,
            app_version=version,
            incoming=directory,
            stable_id=item["source_id"],
            budget=m.DownloadBudget(),
        )
        if not files:
            raise SourceUnavailable("Vedlegget kunne ikke lastes ned")
        return Path(files[0]["path"]), files[0].get("mime_type")

    return fetch_attachment(item, attachment_id, directory, fetch)
