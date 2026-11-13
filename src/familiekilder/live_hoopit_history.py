"""Pure GET search and thread pagination, without channel query/create/watch."""

from datetime import datetime
import time
import uuid

from .core import FamiliekilderError
from .live_hoopit_chat import Client, _cid, _rows, message_summary
from .live_readers import provider, identifier
from .action_ledger import now
from .source_adapter import SourceUnavailable

PAGE_SIZE = 20


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise FamiliekilderError("Tidspunkt må være ISO-format med tidssone") from None


def _cursor(value):
    if value is not None and (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise FamiliekilderError("Ugyldig sidemarkør for chatsøk")
    return value


def _scope(client, cid):
    channel = client.channels(cid=cid)[0]
    # Successful authenticated GET plus verified own membership establishes
    # this read scope. own_capabilities is not a list of REST read permissions;
    # e.g. read-events controls receipts, not access to message contents.
    return {
        k: channel[k]
        for k in (
            "reference",
            "members",
            "member_count",
            "members_complete",
            "own_capabilities",
            "guardian_monitoring_checked",
        )
    }


def search(runtime, cid, query=None, since=None, until=None, cursor=None,
           *, _provider_cursor=None, _durable=False):
    runtime.config.require("hoopit")
    _cid(cid)
    _cursor(cursor)
    _cursor(_provider_cursor)
    if query is not None and (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > 1000
        or "\x00" in query
    ):
        raise FamiliekilderError("Søketekst må ha 1–1000 tegn")
    filters = {"type": {"$in": ["regular", "reply"]}}
    if query is not None:
        filters["text"] = {"$q": query}
    times = {}
    if since is not None:
        times["$gte"] = _time(since).isoformat()
    if until is not None:
        times["$lt"] = _time(until).isoformat()
    if since is not None and until is not None and _time(since) >= _time(until):
        raise FamiliekilderError("Fra-tidspunkt må være før til-tidspunkt")
    if len(times) > 1:
        # Stream permits only one operator per field expression. Combine the
        # inclusive lower and exclusive upper bounds as separate AND clauses.
        filters["$and"] = [{"created_at": {op: value}} for op, value in times.items()]
    elif times:
        filters["created_at"] = times
    runtime.cleanup()
    with provider(runtime.config.root, "hoopit") as (_, core):
        client = Client(core)
        audience = _scope(client, cid)
        signature = ("hoopit_chat_search", cid, query, since, until, client.user_id)
        provider_cursor = _provider_cursor
        if cursor is not None:
            with runtime.lock:
                entry = runtime.pages.get(cursor)
            if not entry or entry["signature"] != signature:
                raise FamiliekilderError(
                    "Utløpt sidemarkør eller endret chat/konto/søk; start oppslaget på nytt"
                )
            provider_cursor = entry["provider_cursor"]
        payload = {
            "filter_conditions": {"cid": cid, "members": {"$in": [client.user_id]}},
            "message_filter_conditions": filters,
            "limit": PAGE_SIZE,
        }
        if provider_cursor is None:
            payload["sort"] = [{"created_at": -1}, {"id": 1}]
        else:
            payload["next"] = provider_cursor
        value = client._get("/search", payload)
        if not isinstance(value, dict):
            raise SourceUnavailable("Hoopit returnerte ugyldig søkeresultat")
        results = _rows(value.get("results"), PAGE_SIZE)
        messages = []
        for result in results:
            row = result.get("message")
            if not isinstance(row, dict) or row.get("type") not in {"regular", "reply"}:
                raise SourceUnavailable("Hoopit-søket returnerte uventet meldingstype")
            stamp = _time(row.get("created_at"))
            if (since is not None and stamp < _time(since)) or (
                until is not None and stamp >= _time(until)
            ):
                raise SourceUnavailable(
                    "Hoopit returnerte meldinger utenfor søkeperioden"
                )
            messages.append(message_summary(row, cid))
        if len({m["reference"]["id"] for m in messages}) != len(messages):
            raise SourceUnavailable("Hoopit returnerte dupliserte søketreff")
        next_provider_cursor = _cursor(value.get("next"))
        if next_provider_cursor is not None and (
            next_provider_cursor == provider_cursor or not messages
        ):
            raise SourceUnavailable("Hoopit returnerte en sidemarkør uten fremdrift")
        next_cursor = None
        if next_provider_cursor is not None and not _durable:
            next_cursor = str(uuid.uuid4())
            with runtime.lock:
                runtime.pages[next_cursor] = {
                    "signature": signature,
                    "provider_cursor": next_provider_cursor,
                    "at": time.monotonic(),
                }
        return {
            "source": "hoopit",
            "fetched_at": now(),
            "source_content_is_untrusted": True,
            "audience": audience,
            "messages": messages,
            "next_cursor": next_provider_cursor if _durable else next_cursor,
            "end_of_results": next_provider_cursor is None,
            "history_complete": False,
            "limitations": [
                "Søk dekker bare kildens indekserte vanlige meldinger og svar. Systemmeldinger, slettede meldinger og meldinger uten søkeindeks er ikke med.",
                "Bruk samme chat og filtre med next_cursor. Vedlegg er ikke lest. Kildeendringer under paginering kan endre treffene.",
                "Foreldreinnsyn kan gjelde etter Hoopits regler.",
            ],
        }


def discovery_page(runtime, cid, since, until, provider_cursor=None):
    """Internal durable traversal; preserve membership checks and raw cursor.

    Public MCP searches still expose only short-lived runtime UUIDs. A durable
    checkpoint stores the provider's cursor bound to its account and interval.
    """
    return search(runtime, cid, since=since, until=until,
                  _provider_cursor=provider_cursor, _durable=True)


def replies(runtime, cid, parent_id, before_id=None):
    runtime.config.require("hoopit")
    _cid(cid)
    identifier(parent_id)
    if before_id is not None:
        identifier(before_id)
    with provider(runtime.config.root, "hoopit") as (_, core):
        client = Client(core)
        audience = _scope(client, cid)
        parent = client.message(cid, parent_id)
        if (
            parent is None
            or parent.get("parent_id")
            or parent.get("deleted_at")
            or parent.get("type") != "regular"
        ):
            raise FamiliekilderError("Velg en tilgjengelig hovedmelding i chatten")
        count = parent.get("reply_count")
        if type(count) is not int or count < 0:
            raise SourceUnavailable("Hoopit mangler pålitelig svarantall")
        parameters = {"limit": PAGE_SIZE}
        boundary = None
        if before_id is not None:
            boundary = client.message(cid, before_id)
            if boundary is None or boundary.get("parent_id") != parent_id:
                raise FamiliekilderError("Sidemarkøren tilhører ikke denne svartråden")
            parameters["id_lt"] = before_id
        value = client._get(
            "/messages/" + parent_id + "/replies", parameters=parameters
        )
        rows = _rows(
            value.get("messages") if isinstance(value, dict) else None, PAGE_SIZE
        )
        stamps = []
        messages = []
        for row in rows:
            if row.get("parent_id") != parent_id or row.get("id") == before_id:
                raise SourceUnavailable(
                    "Hoopit returnerte feil svartråd eller sidemarkør"
                )
            stamp = _time(row.get("created_at"))
            if boundary and stamp > _time(boundary.get("created_at")):
                raise SourceUnavailable(
                    "Hoopit returnerte svar på feil side av markøren"
                )
            stamps.append(stamp)
            messages.append(message_summary(row, cid))
        if stamps != sorted(stamps) or len(
            {r["reference"]["id"] for r in messages}
        ) != len(messages):
            raise SourceUnavailable("Uventet rekkefølge eller dupliserte trådsvar")
        more = len(rows) == PAGE_SIZE
        return {
            "source": "hoopit",
            "fetched_at": now(),
            "source_content_is_untrusted": True,
            "audience": audience,
            "parent": message_summary(parent, cid),
            "messages": messages,
            "reply_count": count,
            "next_before_id": rows[0]["id"] if more else None,
            "end_of_thread": not more,
            "thread_complete": before_id is None and not more and len(rows) == count,
            "limitations": [
                "Bare én svarside. Følg next_before_id til end_of_thread. Antall svar kan endres under lesing. Vedlegg er ikke lest."
            ],
        }


def register(server, runtime):
    from .source_server import READ, safe_tool

    def hoopit_search_chat(
        cid: str,
        query: str | None = None,
        since: str | None = None,
        until: str | None = None,
        cursor: str | None = None,
    ) -> dict:
        """Søk eller hent eldre indekserte meldinger i egen chat, 20 per side. Følg next_cursor med samme filtre. since er inklusiv, until eksklusiv. Systemmeldinger og slettede meldinger er ikke med; ingen komplett historikkgaranti. Ingen lest-markering eller opprettelse."""
        return search(runtime, cid, query, since, until, cursor)

    def hoopit_chat_replies(
        cid: str, parent_id: str, before_id: str | None = None
    ) -> dict:
        """Les én side trådsvar i egen Hoopit-chat. Velg hovedmeldings-ID fra ferskt oppslag og følg next_before_id til end_of_thread. Vedlegg er ikke lest. Ingen sending eller lest-markering."""
        return replies(runtime, cid, parent_id, before_id)

    for tool in (hoopit_search_chat, hoopit_chat_replies):
        server.add_tool(safe_tool(tool), annotations=READ)
