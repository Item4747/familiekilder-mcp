"""Single-attempt Spond text messages with durable, paginated outcome checks."""

import json
from datetime import date

from .api_transport import ApiHTTPError, ApiTransportError
from .core import FamiliekilderError
from .live_readers import provider, identifier
from .live_spond_reader import spond_context
from .live_spond_chat import Client, CHAT_ROOT, API_LEVEL, _ids, _number, _thread
from .action_ledger import now
from .source_adapter import SourceUnavailable

MAX_CHECK_PAGES = 250


def _content(value):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 4096
        or "\x00" in value
    ):
        raise FamiliekilderError("Chatmeldingen må ha 1–4096 tegn uten nulltegn")
    return value


def _can_start(group, sender):
    """Ordinary first-party age gate. Never use the age-bypass flag."""
    members = group.get("members")
    if not isinstance(members, list):
        raise SourceUnavailable("Spond mangler gruppemedlemmer")
    own = None
    for member in members:
        if (member.get("profile") or {}).get("id") == sender:
            own = member
        for guardian in member.get("guardians", []):
            if (guardian.get("profile") or {}).get("id") == sender:
                return
    if own is None:
        raise FamiliekilderError("Avsender er ikke medlem eller foresatt i gruppen")
    limit = group.get("chatAgeLimit")
    if group.get("type") == 1 or limit is None or limit == 0:
        return
    if type(limit) is not int or not 1 <= limit <= 120:
        raise SourceUnavailable("Ukjent aldersgrense for Spond-chat")
    try:
        born = date.fromisoformat(
            own.get("dateOfBirth") or (own.get("profile") or {}).get("dateOfBirth")
        )
        today = date.today()
        # Strictly older than the limit, like the first-party isBefore gate.
        old_enough = (born.year + limit, born.month, born.day) < (
            today.year,
            today.month,
            today.day,
        )
    except (ValueError, TypeError):
        old_enough = False
    if not old_enough:
        raise FamiliekilderError(
            "Avsenders vanlige aldersrettighet er ikke bekreftet; ingen omgåelse"
        )


def _bound_thread(client, chat_id, sender, group_id, recipients, *, sending=False):
    raw = client.thread(chat_id)
    normalized = _thread(raw, sender, {})
    if raw.get("groupId") != group_id or set(_ids(raw.get("participants"))) != {
        sender,
        *recipients,
    }:
        raise SourceUnavailable("Chatten har endret gruppe eller mottakere")
    if sending and normalized["source_indicates_removed"]:
        raise FamiliekilderError("Chatten krever reaktivering eller nytt medlemskap")
    return raw


def _check(client, receipt, sender, content):
    """Read all messages newer than the durable pre-send high-water mark."""
    cid = receipt.get("chat_id")
    if cid is None:
        found = client.find(receipt["group_id"], receipt["recipient_ids"])
        if not found:
            return None
        cid = identifier(found.get("id"))
    _bound_thread(client, cid, sender, receipt["group_id"], receipt["recipient_ids"])
    baseline = receipt["baseline_msg_num"]
    matches = []
    before = None
    for _ in range(MAX_CHECK_PAGES):
        rows = client.messages(cid, before)
        for row in rows:
            if row["msgNum"] <= baseline:
                continue
            if (
                row.get("user") == sender
                and row.get("text") == content
                and row.get("type") == "TEXT"
                and all(
                    row.get(k) is None
                    for k in ("image", "images", "file", "post", "spond", "campaign")
                )
            ):
                matches.append(row["msgNum"])
        if not rows or rows[-1]["msgNum"] <= max(baseline, 0):
            # Recheck membership after paging; a moving audience is not a receipt.
            _bound_thread(
                client, cid, sender, receipt["group_id"], receipt["recipient_ids"]
            )
            if len(matches) == 1:
                hint = receipt.get("response_reference")
                if hint is not None and hint != {"chat_id": cid, "msg_num": matches[0]}:
                    return None
                return {"kind": "chat_message", "chat_id": cid, "msg_num": matches[0]}
            return None
        before = rows[-1]["msgNum"]
    raise SourceUnavailable(
        "Resultatkontrollen overskrider 5000 meldinger; utfallet er ukjent"
    )


def message(
    runtime, operation_id, content, *, chat_id=None, group_id=None, recipient_ids=None
):
    runtime.config.require("spond", "messages")
    content = _content(content)
    if chat_id is not None:
        chat_id = identifier(chat_id)
        if group_id is not None or recipient_ids is not None:
            raise FamiliekilderError("Velg enten chat-ID eller gruppe og mottakere")
    else:
        group_id = identifier(group_id)
        recipient_ids = sorted(_ids(recipient_ids))
        if not 1 <= len(recipient_ids) <= 99:
            raise FamiliekilderError("Velg 1–99 konkrete chatmottakere")
    with (
        provider(runtime.config.root, "spond") as (module, core),
        runtime.ledger() as ledger,
    ):
        profile, groups, _ = spond_context(module, core)
        sender = identifier(profile.get("id"))
        if recipient_ids is not None and sender in recipient_ids:
            raise FamiliekilderError("Avsender skal ikke legges til som mottaker")
        action = "reply_chat" if chat_id else "send_message"
        request = {
            "sender_id": sender,
            "chat_id": chat_id,
            "group_id": group_id,
            "recipient_ids": recipient_ids,
            "content": content,
        }
        prior = ledger.reserve_operation(operation_id, "spond", action, request)
        if prior and prior["state"] == "verified":
            return ledger.operation_result(operation_id, "verified")
        client = Client(module, core)
        recovering = prior is not None and prior["state"] in {"sending", "unknown"}
        if recovering:
            receipt = json.loads(prior["receipt_json"] or "null")
            if not isinstance(receipt, dict) or "baseline_msg_num" not in receipt:
                return ledger.operation_result(operation_id, "unknown")
        else:
            if chat_id:
                raw = client.thread(chat_id)
                _thread(raw, sender, {})
                group_id = identifier(raw.get("groupId"))
                recipient_ids = sorted(
                    p for p in _ids(raw.get("participants")) if p != sender
                )
            group = next((g for g in groups if g.get("id") == group_id), None)
            if group is None:
                raise FamiliekilderError(
                    "Chatgruppen finnes ikke blant dine ferske grupper"
                )
            if not recipient_ids or len(recipient_ids) > 99:
                raise FamiliekilderError("Chatten må ha 1–99 andre deltakere")
            if action == "send_message":
                _can_start(group, sender)
                valid = client.recipients(group_id)
                if not set(recipient_ids) <= set(valid):
                    raise FamiliekilderError(
                        "Velg standardmottakere fra gruppens ferske oppslag"
                    )
                found = client.find(group_id, recipient_ids)
                chat_id = identifier(found["id"]) if found else None
            baseline = -1
            if chat_id:
                _bound_thread(
                    client, chat_id, sender, group_id, recipient_ids, sending=True
                )
                rows = client.messages(chat_id)
                baseline = rows[0]["msgNum"] if rows else -1
                _bound_thread(
                    client, chat_id, sender, group_id, recipient_ids, sending=True
                )
            receipt = {
                "source": "spond",
                "action": action,
                "sender_id": sender,
                "group_id": group_id,
                "recipient_ids": recipient_ids,
                "chat_id": chat_id,
                "baseline_msg_num": baseline,
                "started_at": now(),
            }
            payload = {"text": content, "type": "TEXT", "mentions": []}
            if chat_id:
                payload["chatId"] = chat_id
            else:
                payload["groupId"] = group_id
                if len(recipient_ids) == 1:
                    payload["recipient"] = recipient_ids[0]
                else:
                    payload["recipients"] = recipient_ids
            # Establish auth and pacing before committing the sending marker.
            if client.auth is None:
                client._bootstrap()
            core._before_request()
            ledger.operation_result(operation_id, "sending", receipt)
            try:
                response = core.transport.post_json(
                    CHAT_ROOT + "/messages",
                    payload,
                    bearer_token=core.token,
                    headers={"Auth": client.auth, "Api-Level": API_LEVEL},
                )
                if isinstance(response, dict):
                    cid = identifier(response.get("chatId"))
                    num = _number(response.get("msgNum"))
                    # Save the provider reference before any follow-up request.
                    receipt["response_reference"] = {"chat_id": cid, "msg_num": num}
                    if chat_id is None:
                        receipt["chat_id"] = cid
                    ledger.operation_result(operation_id, "sending", receipt)
            except (ApiHTTPError, ApiTransportError, OSError, TimeoutError) as exc:
                try:
                    client._failure(exc)
                except FamiliekilderError:
                    pass
            except Exception:
                # Invalid/absent response cannot justify another send.
                pass
        try:
            reference = _check(client, receipt, sender, content)
        except Exception:
            reference = None
        if reference is None:
            return ledger.operation_result(operation_id, "unknown", receipt)
        receipt.pop("baseline_msg_num", None)
        receipt.pop("response_reference", None)
        receipt.update(
            chat_id=reference["chat_id"],
            reference=reference, verified_at=now(), verification="fresh_exact_match"
        )
        return ledger.operation_result(operation_id, "verified", receipt)


def register(server, runtime):
    from .source_server import WRITE, safe_tool

    def spond_send_message(
        operation_id: str, group_id: str, recipient_ids: list[str], content: str
    ) -> dict:
        """Send tekst i privat-/gruppechat etter konkret brukerbeskjed. Velg 1–99 profil-ID-er fra spond_chat_recipients. Eksisterende chat med samme mottakere gjenbrukes. Ingen aldersomgåelse, masseutsending eller vedlegg. Foresatte kan ha innsyn. Samme UUID kontrollerer ukjent utfall uten ny sending."""
        return message(
            runtime,
            operation_id,
            content,
            group_id=group_id,
            recipient_ids=recipient_ids,
        )

    def spond_reply_chat(operation_id: str, chat_id: str, content: str) -> dict:
        """Svar med tekst i chat fra spond_get_chat etter konkret brukerbeskjed. Svaret går til chattens deltakere; kontroller publikum. Ingen reaktivering. Bruk samme UUID ved ukjent resultat; ingen automatisk ny sending."""
        return message(runtime, operation_id, content, chat_id=chat_id)

    if "messages" in runtime.config.services["spond"]:
        for tool in (spond_send_message, spond_reply_chat):
            server.add_tool(safe_tool(tool), annotations=WRITE)
