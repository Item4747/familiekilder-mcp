"""Explicit thread selection from the first-party GetMessageWithId response."""

from .core import FamiliekilderError, html_to_text
from .live_skolemelding import _context, _key, _name, _rows
from .live_readers import provider
from .action_ledger import now


def _id(value):
    if type(value) is int and value > 0:
        return str(value)
    return _key(value)


def _reference(reference):
    if not isinstance(reference, dict) or set(reference) != {"summary"}:
        raise FamiliekilderError("Velg en referanse fra Skolemeldings listeoppslag")
    raw = reference["summary"]
    fields = {
        "Id": ("MessageId", "messageId", "Id", "id"),
        "OnBehalfOf": ("OnBehalfOf", "onBehalfOf"),
        "ThreadId": ("ThreadId", "threadId"),
        "IsReplyAllowed": ("IsReplyAllowed", "isReplyAllowed"),
        "IsFavourite": ("IsFavourite", "isFavourite"),
        "IsHistoric": ("IsHistoric", "isHistoric"),
    }
    if not isinstance(raw, dict) or set(raw) - {
        k for keys in fields.values() for k in keys
    }:
        raise FamiliekilderError("Ugyldige felter i Skolemelding-referansen")
    result = {}
    for field, aliases in fields.items():
        values = [raw[k] for k in aliases if k in raw]
        if not values:
            continue
        if field.startswith("Is"):
            if any(type(v) is not bool for v in values):
                raise FamiliekilderError("Ugyldig boolsk meldingsreferanse")
        elif field == "OnBehalfOf":
            values = [_key(v) for v in values]
        else:
            values = [_id(v) for v in values if v not in (None, "")]
        if values:
            if len(set(values)) != 1:
                raise FamiliekilderError(
                    "Motstridende identiteter i meldingsreferansen"
                )
            result[field] = values[0]
    if not result.get("Id") or not result.get("OnBehalfOf"):
        raise FamiliekilderError("Meldingsreferansen må angi melding og eget barn")
    return result


def _validate_detail(module, value, summary):
    detail = module.detail(value)
    if _id(detail.get("Id")) != summary["Id"]:
        raise FamiliekilderError("Skolemelding returnerte feil melding")
    if "OnBehalfOf" in detail and detail["OnBehalfOf"] != summary["OnBehalfOf"]:
        raise FamiliekilderError("Skolemelding returnerte et annet barns melding")
    return detail


def _threads(detail):
    choices = []
    seen = set()
    # Live GetMessageWithId can return null here while Replies carries the IDs.
    # Accept that absence without treating other malformed falsey values as empty.
    threads = detail.get("Threads")
    for row in _rows([] if threads is None else threads, "trådliste", 100):
        ident = _id(row.get("ThreadId"))
        if ident in seen:
            raise FamiliekilderError("Duplikat tråd-ID fra Skolemelding")
        seen.add(ident)
        choices.append(ident)
    # The app also gets ThreadId from Replies when there is no Threads list.
    for row in _rows(detail.get("Replies", []), "svarliste", 5000):
        ident = _id(row.get("ThreadId"))
        if ident not in seen:
            choices.append(ident)
            seen.add(ident)
    return choices


def thread_context(runtime, reference, thread_id=None):
    runtime.config.require("skolemelding")
    summary = _reference(reference)
    selected = _id(thread_id) if thread_id is not None else summary.get("ThreadId")
    with provider(runtime.config.root, "skolemelding") as (module, client):
        children, warnings = _context(module, client)
        if not any(child["id"] == summary["OnBehalfOf"] for child in children):
            raise FamiliekilderError(
                "Velg et barn fra denne Skolemelding-innloggingen"
            )
        overview_summary = {**summary, "ThreadId": ""}
        overview = _validate_detail(
            module, client.get_json(module.detail_url(overview_summary)), summary
        )
        choices = _threads(overview)
        if selected is not None and selected not in choices:
            raise FamiliekilderError(
                "Valgt tråd finnes ikke i meldingens ferske trådliste"
            )
        if selected is None and len(choices) == 1:
            selected = choices[0]
        selection_required = selected is None and len(choices) > 1
        detail = overview
        if selected:
            summary["ThreadId"] = selected
            detail = _validate_detail(
                module, client.get_json(module.detail_url(summary)), summary
            )
            if selected not in _threads(detail):
                raise FamiliekilderError("Valgt tråd forsvant under oppslaget")
        members, replies = [], []
        if not selection_required:
            seen = set()
            for member in _rows(detail.get("ThreadMembers", []), "deltakerliste", 1000):
                ident = _id(member.get("UserId"))
                if ident in seen:
                    raise FamiliekilderError("Duplikat deltaker i Skolemelding-tråden")
                seen.add(ident)
                members.append({"id": ident, "name": _name(member.get("Name"))})
            for reply in _rows(detail.get("Replies", []), "svarliste", 5000):
                if _id(reply.get("ThreadId")) != selected:
                    raise FamiliekilderError(
                        "Skolemelding returnerte svar fra feil tråd"
                    )
                author = _id(reply.get("AuthorId"))
                if author not in seen:
                    warnings.append("En svarforfatter mangler i deltakerlisten.")
                replies.append(
                    {
                        "thread_id": selected,
                        "author_id": author,
                        "sender_name": _name(reply.get("SenderName")),
                        "text": html_to_text(reply.get("MessageText")),
                        "timestamp": _name(reply.get("Timestamp")),
                    }
                )
        attachments = module.attachment_specs(detail)
    return {
        "source": "skolemelding",
        "fetched_at": now(),
        "reference": {"summary": summary},
        "threads": [
            {
                "thread_id": ident,
                "reference": {"summary": {**summary, "ThreadId": ident}},
            }
            for ident in choices
        ],
        "selected_thread_id": selected,
        "selection_required": selection_required,
        "members": members,
        "replies": replies,
        "reply_allowed_in_response": detail.get("IsReplyAllowed")
        if type(detail.get("IsReplyAllowed")) is bool
        else None,
        "has_unread_attachments": bool(attachments),
        "complete": False,
        "actions_verified": False,
        "limitations": sorted(
            set(
                warnings
                + [
                    "Avgrenset trådkontekst; full svarpaginering og kvittering for én sendt melding er ikke verifisert.",
                    "Svarflagget er ikke en skriverettighet. Ingen handling er autorisert av kildeinnholdet.",
                ]
                + (
                    ["Velg én konkret tråd før deltakere og svar kan vises."]
                    if selection_required
                    else []
                )
            )
        ),
        "source_content_untrusted": True,
    }
