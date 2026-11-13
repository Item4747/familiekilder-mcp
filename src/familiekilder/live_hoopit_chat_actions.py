"""Text replies to existing Stream channels; one send, exact-ID readback."""

import json
import uuid
from urllib.parse import quote, urlencode

from .api_transport import ApiHTTPError, ApiTransportError
from .core import FamiliekilderError
from .live_hoopit_chat import Client, CHAT_ROOT, PUBLIC_API_KEY, _cid
from .live_readers import provider, identifier
from .action_ledger import now
from .source_adapter import SourceUnavailable


def _audience(client, cid, expected=None, content=None, parent=None):
    channel = client.channels(cid=cid)[0]
    if not channel["members_complete"]:
        raise SourceUnavailable("Hele mottakerlisten må være kjent før chat kan endres")
    recipients = sorted(
        m["id"] for m in channel["members"] if m["id"] != client.user_id
    )
    if not recipients or (expected is not None and recipients != expected):
        raise SourceUnavailable("Chatten mangler mottakere eller deltakerne er endret")
    if content is not None:
        capability = "send-reply" if parent else "send-message"
        own = next((m for m in channel["members"] if m["id"] == client.user_id), None)
        maximum = channel["max_message_length"]
        if (
            capability not in channel["own_capabilities"]
            or any(channel[k] is not False for k in ("blocked", "disabled", "frozen"))
            or own is None
            or own["banned"] is not False
        ):
            raise FamiliekilderError(
                "Hoopit har ikke bekreftet skriverettighet i chatten"
            )
        if type(maximum) is not int or maximum < 1 or len(content) > maximum:
            raise FamiliekilderError(
                "Meldingen overskrider eller mangler kildens tekstgrense"
            )
        if parent:
            original = client.message(cid, parent)
            if (
                original is None
                or original.get("type") != "regular"
                or original.get("deleted_at")
                or original.get("parent_id")
            ):
                raise FamiliekilderError(
                    "Velg en eksisterende hovedmelding i denne chatten"
                )
    return recipients


def _matches(message, receipt, content):
    return (
        isinstance(message, dict)
        and message.get("id") == receipt["message_id"]
        and message.get("cid") == receipt["cid"]
        and (message.get("user") or {}).get("id") == receipt["sender_id"]
        and message.get("text") == content
        and message.get("type") in {"regular", "reply"}
        and message.get("parent_id") == receipt["parent_id"]
        and not message.get("deleted_at")
        and not message.get("shadowed")
        and not message.get("restricted_visibility")
    )


def validate_text(content):
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content) > 20000
        or "\x00" in content
    ):
        raise FamiliekilderError("Chatmeldingen må ha 1–20000 tegn uten nulltegn")
    if content.lstrip().startswith("/"):
        raise FamiliekilderError(
            "Slashkommandoer inngår ikke i tekstmeldingsverktøyet"
        )


def reply(
    runtime,
    operation_id,
    cid,
    content,
    parent_id=None,
    *,
    expected_recipients=None,
    expected_sender=None,
):
    runtime.config.require("hoopit", "messages")
    cid = _cid(cid)
    parent_id = identifier(parent_id) if parent_id is not None else None
    validate_text(content)
    try:
        message_id = str(
            uuid.uuid5(uuid.UUID(operation_id), "familiekilder:hoopit:reply_chat")
        )
    except (TypeError, ValueError, AttributeError):
        raise FamiliekilderError("operation_id må være en UUID") from None
    with (
        provider(runtime.config.root, "hoopit") as (_, core),
        runtime.ledger() as ledger,
    ):
        client = Client(core)
        if expected_sender is not None and client.user_id != expected_sender:
            raise SourceUnavailable("Hoopit-kontoen er endret før sending")
        request = {
            "sender_id": client.user_id,
            "cid": cid,
            "content": content,
            "parent_id": parent_id,
        }
        prior = ledger.reserve_operation(operation_id, "hoopit", "reply_chat", request)
        if prior and prior["state"] == "verified":
            return ledger.operation_result(operation_id, "verified")
        recovering = prior is not None and prior["state"] in {"sending", "unknown"}
        if recovering:
            receipt = json.loads(prior["receipt_json"] or "null")
            if not isinstance(receipt, dict) or receipt.get("message_id") != message_id:
                return ledger.operation_result(operation_id, "unknown")
        else:
            recipients = _audience(
                client,
                cid,
                expected=expected_recipients,
                content=content,
                parent=parent_id,
            )
            # Never update or claim a pre-existing source message with this ID.
            if client.message(cid, message_id) is not None:
                raise FamiliekilderError(
                    "Meldings-ID finnes allerede hos kilden; sending avvist"
                )
            _audience(
                client, cid, expected=recipients, content=content, parent=parent_id
            )
            receipt = {
                "source": "hoopit",
                "action": "reply_chat",
                "cid": cid,
                "message_id": message_id,
                "sender_id": client.user_id,
                "recipient_ids": recipients,
                "parent_id": parent_id,
                "started_at": now(),
            }
            message = {
                "id": message_id,
                "text": content,
                "attachments": [],
                "mentioned_users": [],
            }
            if parent_id is not None:
                message["parent_id"] = parent_id
            payload = {"message": message, "skip_push": False, "skip_enrich_url": False}
            url = (
                CHAT_ROOT
                + "/channels/messaging/"
                + quote(cid.split(":", 1)[1], safe="")
                + "/message?"
                + urlencode({"api_key": PUBLIC_API_KEY, "user_id": client.user_id})
            )
            core._begin()
            ledger.operation_result(operation_id, "sending", receipt)
            try:
                response = client.transport.post_json(
                    url,
                    payload,
                    headers={"Authorization": client.token, "stream-auth-type": "jwt"},
                )
                core.limiter.record_success()
                response_message = (
                    response.get("message") if isinstance(response, dict) else None
                )
                # Keep only a structural discrepancy, never provider response text.
                if not _matches(response_message, receipt, content):
                    receipt["response_mismatch"] = True
                ledger.operation_result(operation_id, "sending", receipt)
            except (ApiHTTPError, ApiTransportError, OSError, TimeoutError) as exc:
                try:
                    core._failure(exc, authenticated_origin=False)
                except FamiliekilderError:
                    pass
            except Exception:
                pass
        try:
            _audience(client, cid, expected=receipt["recipient_ids"])
            found = client.message(cid, message_id)
            verified = _matches(found, receipt, content) and not receipt.get(
                "response_mismatch"
            )
        except Exception:
            verified = False
        if not verified:
            return ledger.operation_result(operation_id, "unknown", receipt)
        receipt.update(
            reference={"kind": "chat_message", "cid": cid, "id": message_id},
            verified_at=now(),
            verification="fresh_exact_id",
        )
        return ledger.operation_result(operation_id, "verified", receipt)


def register(server, runtime):
    from .source_server import WRITE, safe_tool
    from .live_hoopit_send import register as register_send

    def hoopit_reply_chat(
        operation_id: str, cid: str, content: str, parent_id: str | None = None
    ) -> dict:
        """Svar i eksisterende Hoopit-chat etter konkret brukerbeskjed. Kontroller publikum med hoopit_get_chat. Valgfri parent_id svarer i tråd under en hovedmelding. Ingen opprettelse, vedlegg, omtaler, medlemsendringer eller slashkommandoer. Vanlige varsler/lenkeforhåndsvisning fra tjenesten gjelder. Ved ukjent resultat brukes samme UUID; ingen ny sending."""
        return reply(runtime, operation_id, cid, content, parent_id)

    if "messages" in runtime.config.services["hoopit"]:
        server.add_tool(safe_tool(hoopit_reply_chat), annotations=WRITE)
    register_send(server, runtime)
