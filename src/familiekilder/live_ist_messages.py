"""IST recipient search and resumable two-stage message creation.

Contracts: first-party build 422 loadTeachers/createFetch and live GraphQL schema.
The local ledger stores only fingerprints, stage IDs and source references.
"""

import json
import uuid
from datetime import datetime, timedelta, UTC
from urllib.parse import urlencode, urlsplit

from .api_transport import ApiHTTPError, ApiTransport
from .core import FamiliekilderError
from .live_ist import Client, CONVERSATIONS, identifier, _rows, _wire, reply_message
from .live_readers import provider
from .action_ledger import now
from .source_adapter import SourceUnavailable

TEACHER_URL = "query FamilyTeacherURL($id: ID!) { getTeacherSearchURL(unitId: $id) { url headers { key value } } }"
OPEN = """mutation FamilyOpenConversation($inputs: [ConversationInputEx!]!) {
  openConversationEx(inputs: $inputs, dryRun: false) { id }
}"""


def _message_text(value, label, limit):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or "\x00" in value
    ):
        raise FamiliekilderError(label + " mangler eller er for lang")
    return value


def _settings(placement):
    settings = placement["effective_settings"]
    if (
        settings.get("enableMessaging") is not True
        or settings.get("messagingEnableOpenChat") is not True
        or settings.get("readonly") is not False
        or placement.get("readonly") is True
    ):
        raise FamiliekilderError("Plasseringen tillater ikke nye samtaler")
    return settings


def teacher_page(client, child_id, placement, search="", cursor=None):
    if (
        not isinstance(search, str)
        or len(search) > 200
        or any(ord(c) < 32 for c in search)
    ):
        raise FamiliekilderError("Ugyldig mottakersøk")
    if cursor is not None and (
        not isinstance(cursor, str)
        or not cursor
        or len(cursor) > 4096
        or any(ord(c) < 32 for c in cursor)
    ):
        raise FamiliekilderError("Ugyldig mottakermarkør")
    unit_id = identifier((placement.get("unit") or {}).get("id"))
    signed = client.read(TEACHER_URL, {"id": unit_id}).get("getTeacherSearchURL")
    if not isinstance(signed, dict):
        raise SourceUnavailable("IST mangler mottakersøk")
    url = signed.get("url")
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        valid = (
            parts
            and parts.scheme == "https"
            and parts.hostname == "api.ist.com"
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
            and not parts.fragment
            and not parts.query
        )
    except ValueError:
        valid = False
    if not valid:
        raise SourceUnavailable(
            "IST-mottakersøket har en uverifisert vert eller adresse"
        )
    headers = {}
    allowed = {"x-url-signature", "x-url-expires", "x-url-target"}
    for row in _rows(signed.get("headers")):
        key = row.get("key")
        if (
            not isinstance(key, str)
            or key.lower() not in allowed
            or key.lower() in headers
        ):
            raise SourceUnavailable(
                "IST-mottakersøket har uventede autentiseringsfelter"
            )
        value = row.get("value")
        if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
            raise SourceUnavailable("IST-mottakersøket har ugyldige signaturfelter")
        headers[key.lower()] = value
    if set(headers) != allowed:
        raise SourceUnavailable("IST-mottakersøket mangler signaturfelter")
    query = {
        "childId": child_id,
        "placementId": placement["id"],
        "search": search,
        "language": "nb",
        "fetchNextQuery": "false",
    }
    if cursor is not None:
        query["cursor"] = cursor
    try:
        result = ApiTransport(
            allowed_hosts={"api.ist.com"}, max_json_bytes=2 * 1024 * 1024
        ).get_json(
            url + "?" + urlencode(query), bearer_token=client.token, headers=headers
        )
    except ApiHTTPError as exc:
        if exc.status_code != 401 or client.refreshed:
            raise
        client.refreshed = True
        client.token = client.module.refresh_token(client.root)
        # Reacquire the signed URL as well; retry only this read, at most once.
        return teacher_page(client, child_id, placement, search, cursor)
    if not isinstance(result, dict):
        raise SourceUnavailable("IST returnerte ugyldige mottakere")
    rows = _rows(result.get("teachers"))
    page = result.get("page")
    if len(rows) > 5000:
        raise SourceUnavailable("IST returnerte for mange mottakere")
    normalized = []
    for row in rows:
        normalized.append(
            {
                "id": identifier(row.get("id")),
                "firstName": row.get("firstName"),
                "lastName": row.get("lastName"),
                "isTutor": row.get("isTutor"),
            }
        )
    if page is None:
        return normalized, None
    if not isinstance(page, dict) or type(page.get("isEnd")) is not bool:
        raise SourceUnavailable("IST returnerte ugyldig mottakerpaginering")
    if page["isEnd"]:
        return normalized, None
    following = page.get("lastCursor")
    if (
        not isinstance(following, str)
        or not following
        or following == cursor
        or not rows
    ):
        raise SourceUnavailable("IST returnerte en ugyldig neste mottakerside")
    return normalized, following


def recipients(runtime, child_id, placement_id, search="", cursor=None):
    runtime.config.require("ist-home")
    with provider(runtime.config.root, "ist-home") as (module, token):
        client = Client(runtime.config.root, module, token)
        ctx = client.context()
        placement = client.select(ctx, child_id, placement_id)
        rows, following = teacher_page(client, child_id, placement, search, cursor)
        return {
            "source": "ist-home",
            "reference": {"child_id": child_id, "placement_id": placement_id},
            "fetched_at": now(),
            "items": rows,
            "next_cursor": following,
            "complete": following is None,
            "search_scope": "Tjenestens mottakersøk for valgt plassering",
            "source_content_is_untrusted": True,
        }


def _validate_recipients(client, child_id, placement, selected):
    remaining = set(selected)
    seen = set()
    cursor = None
    for _ in range(100):
        rows, following = teacher_page(client, child_id, placement, cursor=cursor)
        remaining -= {r["id"] for r in rows}
        if not remaining:
            return
        if following is None:
            break
        if following in seen:
            raise SourceUnavailable("IST gjentok en mottakermarkør")
        seen.add(following)
        cursor = following
    if remaining:
        raise FamiliekilderError(
            "Valgte mottakere kunne ikke bekreftes i plasseringens ferske mottakerliste"
        )


def _matches(thread, child_id, placement_id, sender_id, payload):
    # Only the sender may be added implicitly to the explicitly selected people.
    return (
        thread.get("title") == payload["title"]
        and (thread.get("publishedBy") or {}).get("id") == sender_id
        and {r.get("id") for r in _rows(thread.get("children"))} == {child_id}
        and {r.get("id") for r in _rows(thread.get("placements"))} == {placement_id}
        and {r.get("id") for r in _rows(thread.get("recipients"))} - {sender_id}
        == set(payload["recipientIds"]) - {sender_id}
    )


def _open(client, ledger, operation_id, child_id, placement_id, sender_id, payload):
    prior = ledger.reserve_operation(
        operation_id, "ist-home", "open_conversation", {"child_id": child_id, **payload}
    )
    if prior and prior["state"] == "verified":
        return ledger.operation_result(operation_id, "verified")

    def observe():
        end = datetime.now(UTC)
        start = end - timedelta(days=14)
        rows = _rows(
            client.read(
                CONVERSATIONS, {"startsAt": _wire(start), "endsAt": _wire(end)}
            ).get("getConversations")
        )
        if len(rows) > 5000:
            raise SourceUnavailable("For mange samtaler til entydig kontroll")
        return {
            identifier(r.get("id"))
            for r in rows
            if _matches(r, child_id, placement_id, sender_id, payload)
        }

    if prior and prior["state"] in {"sending", "unknown"}:
        receipt = json.loads(prior["receipt_json"] or "null")
        if not isinstance(receipt, dict) or not isinstance(
            receipt.get("baseline_ids"), list
        ):
            return ledger.operation_result(operation_id, "unknown")
    else:
        baseline = sorted(observe())
        if len(baseline) > 50:
            raise FamiliekilderError("For mange like samtaler til entydig kontroll")
        receipt = {
            "source": "ist-home",
            "action": "open_conversation",
            "child_id": child_id,
            "placement_id": placement_id,
            "started_at": now(),
            "baseline_ids": baseline,
            "readback_window_days": 14,
        }
        ledger.operation_result(operation_id, "sending", receipt)
        try:
            result = client.write(OPEN, {"inputs": [payload]})
            rows = _rows(result.get("openConversationEx"))
            if len(rows) != 1:
                raise SourceUnavailable(
                    "IST returnerte et tvetydig opprettelsesresultat"
                )
            receipt["candidate_id"] = identifier(rows[0].get("id"))
            # Preserve the service receipt before further requests. A restart
            # can verify it even when empty conversations are absent from lists.
            ledger.operation_result(operation_id, "sending", receipt)
        except ApiHTTPError as exc:
            if exc.status_code in {401, 403, 429}:
                return ledger.operation_result(operation_id, "unknown", receipt)
        except Exception:
            pass
    try:
        candidate = receipt.get("candidate_id")
        if candidate is None:
            created = observe() - set(receipt["baseline_ids"])
            if len(created) != 1:
                return ledger.operation_result(operation_id, "unknown", receipt)
            candidate = next(iter(created))
        thread = client.conversation(candidate)
        if not _matches(thread, child_id, placement_id, sender_id, payload):
            return ledger.operation_result(operation_id, "unknown", receipt)
    except Exception:
        return ledger.operation_result(operation_id, "unknown", receipt)
    receipt.pop("baseline_ids")
    receipt.pop("candidate_id", None)
    receipt.update(reference=candidate, verified_at=now())
    return ledger.operation_result(operation_id, "verified", receipt)


def send_message(
    runtime, operation_id, child_id, placement_id, recipient_ids, title, content
):
    runtime.config.require("ist-home", "messages")
    identifier(child_id)
    identifier(placement_id)
    _message_text(title, "Tittel", 500)
    _message_text(content, "Meldingstekst", 20000)
    if (
        not isinstance(recipient_ids, list)
        or not recipient_ids
        or len(recipient_ids) > 100
    ):
        raise FamiliekilderError("Velg konkrete mottaker-ID-er fra mottakersøket")
    selected = sorted({identifier(i) for i in recipient_ids})
    request = {
        "child_id": child_id,
        "placement_id": placement_id,
        "recipient_ids": selected,
        "title": title,
        "content": content,
    }
    with (
        provider(runtime.config.root, "ist-home") as (module, token),
        runtime.ledger() as ledger,
    ):
        prior = ledger.reserve_operation(
            operation_id, "ist-home", "send_message", request
        )
        if prior and prior["state"] == "verified":
            return ledger.operation_result(operation_id, "verified")
        client = Client(runtime.config.root, module, token)
        ctx = client.context()
        placement = client.select(ctx, child_id, placement_id)
        settings = _settings(placement)
        limit = settings.get("conversationTeacherLimit")
        if type(limit) is not int or limit < 1 or len(selected) > limit:
            raise FamiliekilderError(
                "Mottakerantallet overstiger plasseringens samtalegrense"
            )
        _validate_recipients(client, child_id, placement, selected)
        previous_receipt = (
            json.loads(prior["receipt_json"] or "null") if prior else None
        )
        open_id = str(uuid.uuid5(uuid.UUID(operation_id), "conversation"))
        message_id = str(uuid.uuid5(uuid.UUID(operation_id), "message"))
        receipt = {
            "source": "ist-home",
            "action": "send_message",
            "child_id": child_id,
            "placement_id": placement_id,
            "recipient_ids": selected,
            "open_operation_id": open_id,
            "message_operation_id": message_id,
            "phase": "conversation",
            "started_at": (previous_receipt or {}).get("started_at") or now(),
        }
        ledger.operation_result(operation_id, "sending", receipt)
        opened = _open(
            client,
            ledger,
            open_id,
            child_id,
            placement_id,
            ctx["user"]["id"],
            {"title": title, "placementId": placement_id, "recipientIds": selected},
        )
        if opened["status"] != "verified":
            return ledger.operation_result(operation_id, "unknown", receipt)
        thread_id = opened["receipt"]["reference"]
        receipt.update(conversation_id=thread_id, phase="message")
        ledger.operation_result(operation_id, "sending", receipt)

        def validate_thread(thread):
            if not _matches(
                thread,
                child_id,
                placement_id,
                ctx["user"]["id"],
                {"title": title, "recipientIds": selected},
            ):
                raise FamiliekilderError(
                    "Samtalens mottakere eller tilknytning er endret; meldingen er ikke sendt"
                )

        # A previously sent message is reconciled by its deterministic operation
        # ID; only an as-yet-unsent second stage may be started after recovery.
        result = reply_message(
            runtime,
            message_id,
            child_id,
            thread_id,
            content,
            validate_thread=validate_thread,
        )
        if result["status"] != "verified":
            return ledger.operation_result(operation_id, "unknown", receipt)
        receipt.update(
            reference=result["receipt"]["reference"],
            verified_at=now(),
            phase="complete",
        )
        return ledger.operation_result(operation_id, "verified", receipt)


def register(server, runtime):
    from .source_server import READ, WRITE, safe_tool

    def ist_home_recipients(
        child_id: str, placement_id: str, search: str = "", cursor: str | None = None
    ) -> dict:
        """Søk i ferske IST-mottakere for barn/plassering. Følg next_cursor med samme søk; ID-er brukes ved ny melding."""
        return recipients(runtime, child_id, placement_id, search, cursor)

    def ist_home_send_message(
        operation_id: str,
        child_id: str,
        placement_id: str,
        recipient_ids: list[str],
        title: str,
        content: str,
    ) -> dict:
        """Send ny tekstmelding etter konkret brukerbeskjed. To trinn: åpne samtale og sende. Samme operation_id gjenopptar bare usendte trinn og kontrollerer uavklarte trinn uten gjentakelse."""
        return send_message(
            runtime, operation_id, child_id, placement_id, recipient_ids, title, content
        )

    server.add_tool(safe_tool(ist_home_recipients), annotations=READ)
    if "messages" in runtime.config.services["ist-home"]:
        server.add_tool(safe_tool(ist_home_send_message), annotations=WRITE)
