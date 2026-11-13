"""Direct sent-folder lookup needed before message outcome verification."""

from .core import FamiliekilderError
from .live_readers import provider
from .live_skolemelding_reader import message_reference
from .live_skolemelding import _context, _key
from .live_skolemelding_threads import _id
from .action_ledger import now


def list_sent(runtime, child_id, page=1):
    runtime.config.require("skolemelding")
    _key(child_id)
    if type(page) is not int or not 1 <= page <= 200:
        raise FamiliekilderError("Velg meldingsside mellom 1 og 200")
    with provider(runtime.config.root, "skolemelding") as (module, client):
        children, warnings = _context(module, client)
        child = next((c for c in children if c["id"] == child_id), None)
        if child is None:
            raise FamiliekilderError("Velg eget barn fra fersk Skolemelding-kontekst")
        payload = module.search_payload(page, child_id)
        payload["SearchInbox"] = False
        response = client.post_json(module.MESSAGE_SEARCH_ENDPOINT, payload)
        # The live search envelope is shared with the received-folder reader.
        # Missing Result must not masquerade as an empty sent folder.
        if not isinstance(response, dict) or not isinstance(
            response.get("Result"), list
        ):
            raise FamiliekilderError(
                "Skolemelding mangler Result for sendte meldinger"
            )
        rows, total = module.search_result(response)
        if len(rows) > module.PAGE_SIZE:
            raise FamiliekilderError("Skolemelding returnerte for stor meldingsside")
        seen = set()
        items = []
        for row in rows:
            ident = _id(row.get("Id"))
            if ident in seen or row.get("OnBehalfOf", child_id) != child_id:
                raise FamiliekilderError(
                    "Duplikat melding eller feil barn i sendtlisten"
                )
            seen.add(ident)
            summary = dict(row)
            summary.setdefault("OnBehalfOf", child_id)
            item = module.normalize_message(
                summary, {}, child_names={child_id: child["name"]}, attachments=[]
            )
            item.update(
                reference=message_reference(summary),
                content_complete=False,
                has_unread_attachments=bool(module.attachment_specs(summary)),
            )
            items.append(item)
    fetched_at = now()
    consumed = (page - 1) * module.PAGE_SIZE + len(rows)
    has_more = bool(rows) and (
        consumed < total if total is not None else len(rows) == module.PAGE_SIZE
    )
    if total is not None and consumed < total and len(rows) < module.PAGE_SIZE:
        warnings.append(
            "Kort side samsvarer ikke med oppgitt total; dekningen er usikker."
        )
    if has_more and page == 200:
        warnings.append("Sidegrensen er nådd; flere meldinger kan finnes.")
    return {
        "source": "skolemelding",
        "fetched_at": fetched_at,
        "folder": "sent",
        "child_id": child_id,
        "page": page,
        "page_size": module.PAGE_SIZE,
        "total_matching": total,
        "has_more": has_more,
        "next_page": page + 1 if has_more and page < 200 else None,
        "complete": False,
        "items": [
            runtime.expose("skolemelding", item, False, fetched_at) for item in items
        ],
        "limitations": warnings
        + [
            "Sidevis søk i sendte meldinger; hent detaljer, tråder og vedlegg separat.",
            "Sendtlisten alene bekrefter ikke utfallet av en bestemt skrivehandling.",
        ],
        "source_content_untrusted": True,
        "actions_verified": False,
    }
