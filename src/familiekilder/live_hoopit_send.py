"""New Hoopit text conversations, with separate durable open/send stages."""

import json
import uuid
from collections import defaultdict
from urllib.parse import quote, urlencode

from .api_transport import ApiHTTPError, ApiTransportError
from .core import FamiliekilderError
from .live_hoopit import _id
from .live_hoopit_chat import Client, CHAT_ROOT, PUBLIC_API_KEY, _cid
from .live_hoopit_chat_actions import _audience, reply, validate_text
from .live_hoopit_recipients import (
    ROLES,
    MAX_PAGES_PER_FEED,
    contact_page,
    check_chat_users,
)
from .live_readers import provider, identifier
from .action_ledger import now
from .source_adapter import SourceUnavailable


def selections(references):
    if not isinstance(references, list) or not 1 <= len(references) <= 99:
        raise FamiliekilderError(
            "Velg 1–99 mottakerreferanser fra hoopit_chat_recipients"
        )
    normalized = []
    for ref in references:
        if (
            not isinstance(ref, dict)
            or set(ref) != {"kind", "group_id", "role", "id", "uuid"}
            or ref["kind"] != "chat_recipient"
            or ref["role"] not in ROLES
        ):
            raise FamiliekilderError("Bruk en fullstendig chat_recipient-referanse")
        normalized.append(
            {
                "kind": "chat_recipient",
                "group_id": _id(ref["group_id"]),
                "role": ref["role"],
                "id": _id(ref["id"]),
                "uuid": identifier(ref["uuid"]),
            }
        )
    for field in ("id", "uuid"):
        if len({r[field] for r in normalized}) != len(normalized):
            raise FamiliekilderError(
                "Mottakerne må være unike, også på tvers av grupper og roller"
            )
    return sorted(normalized, key=lambda r: r["uuid"])


def validate_recipients(client, selected):
    grouped = defaultdict(list)
    for ref in selected:
        if ref["uuid"] == client.user_id:
            raise FamiliekilderError("Egen person kan ikke velges som mottaker")
        grouped[(ref["group_id"], ref["role"])].append(ref)
    matched = []
    for (group_id, role), refs in grouped.items():
        remaining = {r["id"]: r for r in refs}
        seen = set()
        for page in range(1, MAX_PAGES_PER_FEED + 1):
            result = contact_page(client.core, group_id, role, page)
            for row in result["recipients"]:
                ref = row["reference"]
                if ref["id"] in seen:
                    raise SourceUnavailable("Hoopit gjentok mottakere mellom sider")
                seen.add(ref["id"])
                if ref["id"] in remaining:
                    if ref != remaining[ref["id"]]:
                        raise SourceUnavailable(
                            "Den valgte mottakerens identitet er endret"
                        )
                    matched.append(row)
                    del remaining[ref["id"]]
            if not remaining or result["end_of_list"]:
                break
        if remaining:
            raise FamiliekilderError(
                "Alle valgte mottakere kunne ikke bekreftes i fersk kontaktliste"
            )
    check_chat_users(client, matched)
    if any(not r["chat_available"] for r in matched):
        raise FamiliekilderError(
            "En valgt mottaker kan ikke motta meldinger; ingen blir utelatt"
        )
    # The sender can also have blocked one of the selected recipients.
    own = client.users([client.user_id]).get(client.user_id)
    if (
        not own
        or own.get("banned") is not False
        or own.get("shadow_banned") is not False
        or own.get("deleted_at")
        or own.get("deactivated_at")
        or not isinstance(own.get("blocked_user_ids"), list)
        or set(own["blocked_user_ids"]) & {r["uuid"] for r in selected}
    ):
        raise FamiliekilderError(
            "Egen chatkonto eller blokkering tillater ikke sending"
        )


def _open(client, ledger, operation_id, selected):
    recipient_ids = sorted(r["uuid"] for r in selected)
    channel_id = (
        str(uuid.uuid5(uuid.UUID(operation_id), "channel"))
        if len(selected) > 1
        else None
    )
    request = {
        "sender_id": client.user_id,
        "recipient_ids": recipient_ids,
        "channel_id": channel_id,
    }
    prior = ledger.reserve_operation(operation_id, "hoopit", "open_chat", request)
    if prior and prior["state"] == "verified":
        return ledger.operation_result(operation_id, "verified")
    recovering = prior and prior["state"] in {"sending", "unknown"}
    if recovering:
        receipt = json.loads(prior["receipt_json"] or "null")
        if not isinstance(receipt, dict):
            return ledger.operation_result(operation_id, "unknown")
    else:
        validate_recipients(client, selected)
        if channel_id is None:
            existing = client.direct_channel(recipient_ids[0])
            if existing:
                cid = existing["reference"]["cid"]
                _audience(client, cid, expected=recipient_ids)
                return ledger.operation_result(
                    operation_id,
                    "verified",
                    {
                        "source": "hoopit",
                        "action": "open_chat",
                        **request,
                        "cid": cid,
                        "reused": True,
                        "verified_at": now(),
                    },
                )
        else:
            if client.channels(cid="messaging:" + channel_id, allow_missing=True):
                raise FamiliekilderError(
                    "Den nye kanal-ID-en finnes allerede; opprettelse avvist"
                )
        receipt = {
            "source": "hoopit",
            "action": "open_chat",
            **request,
            "started_at": now(),
        }
        payload = {
            "data": {"members": sorted([client.user_id, *recipient_ids])},
            "state": True,
            "watch": False,
            "presence": False,
        }
        route = (
            "/channels/messaging"
            + ("/" + quote(channel_id, safe="") if channel_id else "")
            + "/query"
        )
        url = (
            CHAT_ROOT
            + route
            + "?"
            + urlencode({"api_key": PUBLIC_API_KEY, "user_id": client.user_id})
        )
        client.core._begin()
        ledger.operation_result(operation_id, "sending", receipt)
        try:
            result = client.transport.post_json(
                url,
                payload,
                headers={
                    "Authorization": client.token,
                    "stream-auth-type": "jwt",
                },
            )
            client.core.limiter.record_success()
            try:
                channel = client._channel(result)
                cid = channel["reference"]["cid"]
                if (
                    not channel["members_complete"]
                    or sorted(m["id"] for m in channel["members"])
                    != payload["data"]["members"]
                    or (channel_id and cid != "messaging:" + channel_id)
                ):
                    raise SourceUnavailable(
                        "Uventet kanal eller publikum i kvitteringen"
                    )
                receipt["cid"] = cid
            except Exception:
                receipt["response_mismatch"] = True
            ledger.operation_result(operation_id, "sending", receipt)
        except (ApiHTTPError, ApiTransportError, OSError, TimeoutError) as exc:
            try:
                client.core._failure(exc, authenticated_origin=False)
            except FamiliekilderError:
                pass
        except Exception:
            pass
    try:
        if receipt.get("response_mismatch"):
            raise SourceUnavailable("Opprettelseskvitteringen stemmer ikke")
        cid = receipt.get("cid")
        if not cid:
            if channel_id:
                cid = "messaging:" + channel_id
            else:
                found = client.direct_channel(recipient_ids[0])
                if found is None:
                    raise SourceUnavailable("Opprettelsen kan ikke bekreftes")
                cid = found["reference"]["cid"]
        _cid(cid)
        _audience(client, cid, expected=recipient_ids)
    except Exception:
        return ledger.operation_result(operation_id, "unknown", receipt)
    receipt.update(cid=cid, verified_at=now(), verification="fresh_exact_audience")
    return ledger.operation_result(operation_id, "verified", receipt)


def send(runtime, operation_id, recipient_references, content):
    runtime.config.require("hoopit", "messages")
    selected = selections(recipient_references)
    validate_text(content)
    try:
        op = uuid.UUID(operation_id)
    except (TypeError, ValueError, AttributeError):
        raise FamiliekilderError("operation_id må være en UUID") from None
    open_id = str(uuid.uuid5(op, "familiekilder:hoopit:open"))
    message_id = str(uuid.uuid5(op, "familiekilder:hoopit:message"))
    with (
        provider(runtime.config.root, "hoopit") as (_, core),
        runtime.ledger() as ledger,
    ):
        client = Client(core)
        recipient_ids = [r["uuid"] for r in selected]
        # Roles/groups are lookup context, not a way to bypass an unresolved send.
        request = {
            "sender_id": client.user_id,
            "recipient_ids": recipient_ids,
            "content": content,
        }
        prior = ledger.reserve_operation(
            operation_id, "hoopit", "send_message", request
        )
        if prior and prior["state"] == "verified":
            return ledger.operation_result(operation_id, "verified")
        receipt = json.loads(prior["receipt_json"] or "null") if prior else None
        if not isinstance(receipt, dict):
            receipt = {
                "source": "hoopit",
                "action": "send_message",
                "sender_id": client.user_id,
                "recipient_ids": recipient_ids,
                "open_operation_id": open_id,
                "message_operation_id": message_id,
                "phase": "conversation",
                "started_at": now(),
            }
        ledger.operation_result(operation_id, "sending", receipt)
        try:
            opened = _open(client, ledger, open_id, selected)
            if opened["status"] != "verified":
                return ledger.operation_result(operation_id, "unknown", receipt)
            cid = opened["receipt"]["cid"]
            receipt.update(cid=cid, phase="message")
            ledger.operation_result(operation_id, "sending", receipt)
            child = ledger.conn.execute(
                "SELECT state FROM operations WHERE operation_id=?", (message_id,)
            ).fetchone()
            if child is None or child["state"] == "prepared":
                # Recheck rights before the as-yet-unsent message stage. An unknown
                # message is only reconciled, even if contacts have since changed.
                validate_recipients(client, selected)
            result = reply(
                runtime,
                message_id,
                cid,
                content,
                expected_recipients=recipient_ids,
                expected_sender=client.user_id,
            )
            if result["status"] != "verified":
                return ledger.operation_result(operation_id, "unknown", receipt)
        except FamiliekilderError:
            ledger.operation_result(operation_id, "unknown", receipt)
            raise
        receipt.update(
            reference=result["receipt"]["reference"],
            phase="complete",
            verified_at=now(),
        )
        return ledger.operation_result(operation_id, "verified", receipt)


def register(server, runtime):
    from .source_server import WRITE, safe_tool

    def hoopit_send_message(
        operation_id: str, recipient_references: list[dict], content: str
    ) -> dict:
        """Send tekst til 1–99 konkrete Hoopit-mottakere etter brukerbeskjed. Bruk referanser fra hoopit_chat_recipients. Alle valideres ferskt; ingen utelates. 1:1 bruker en entydig eksisterende samtale eller åpner en medlemsbasert samtale; flere mottakere oppretter ny gruppechat. Opprettelse og sending registreres separat. Ved ukjent utfall bruk samme UUID; sendte trinn gjentas aldri. Ingen vedlegg, omtaler eller slashkommandoer."""
        return send(runtime, operation_id, recipient_references, content)

    if "messages" in runtime.config.services["hoopit"]:
        server.add_tool(safe_tool(hoopit_send_message), annotations=WRITE)
