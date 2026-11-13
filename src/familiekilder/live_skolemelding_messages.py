"""New teacher messages using the parent app contract and exact sent readback."""

import html
from urllib.parse import urlencode

from .core import FamiliekilderError, html_to_text
from .live_create import perform
from .live_readers import provider
from .live_skolemelding import _context, _key, _rows
from .live_skolemelding_threads import _id, _validate_detail
from .action_ledger import fingerprint


def _text(value, maximum):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(c) < 32 and c not in "\n\t" for c in value)
    ):
        raise FamiliekilderError(f"Meldingstekst må ha 1–{maximum} tegn")
    return value


def _recipient(module, client, child_id, school_id, recipient_id):
    children, _ = _context(module, client)
    child = next((v for v in children if v["id"] == child_id), None)
    if not child or school_id not in {s["id"] for s in child["schools"]}:
        raise FamiliekilderError("Velg eget barn og skole fra fersk kontekst")
    value = client.get_json(
        module.CHILD_TEACHERS_ENDPOINT
        + "?"
        + urlencode({"schoolId": school_id, "childId": child_id})
    )
    if not isinstance(value, dict):
        raise FamiliekilderError("Mottakerlisten mangler")
    matches = [
        row
        for row in _rows(value.get("Users"), "lærerliste")
        if row.get("Username") == recipient_id
    ]
    names = {row.get("FullName") for row in matches}
    if len(names) != 1 or not isinstance(next(iter(names), None), str):
        raise FamiliekilderError("Velg én entydig lærer-ID fra mottakeroppslaget")
    name = next(iter(names))
    if not name.strip():
        raise FamiliekilderError("Mottakeren mangler navn")
    # TeacherSelector's mapper, including its static object-buffer fields.
    # IsMine describes association; it is not the parent's send permission.
    return {
        "Id": recipient_id,
        "Name": name,
        "UserRecipientsToInclude": "None",
        "TeacherCount": 0,
        "StudentCount": 0,
        "ParentCount": 0,
        "ParentIds": [],
    }


def _matches(
    module, client, child_id, school_id, recipient_id, title, body, *, is_absence=False
):
    """Complete bounded sent search; return only exact own-message matches."""
    seen, matches = set(), []
    expected_total = None
    for page in range(1, 201):
        payload = module.search_payload(page, child_id)
        payload["SearchInbox"] = False
        response = client.post_json(module.MESSAGE_SEARCH_ENDPOINT, payload)
        if not isinstance(response, dict) or not isinstance(
            response.get("Result"), list
        ):
            raise FamiliekilderError("Sendtlisten mangler Result")
        rows, total = module.search_result(response)
        if total is None or (expected_total is not None and total != expected_total):
            raise FamiliekilderError("Sendtlisten mangler stabilt totalantall")
        expected_total = total
        if len(rows) > module.PAGE_SIZE or total > 200 * module.PAGE_SIZE:
            raise FamiliekilderError("Sendtlisten overskrider kontrollgrensen")
        for row in rows:
            ident = _id(row.get("Id"))
            if ident in seen or row.get("OnBehalfOf") != child_id:
                raise FamiliekilderError(
                    "Duplikat melding eller feil barn i sendtlisten"
                )
            seen.add(ident)
            if (
                row.get("Header") != title
                or html_to_text(row.get("Body")) != body
            ):
                continue
            summary = {"Id": ident, "OnBehalfOf": child_id, "ThreadId": ""}
            detail = _validate_detail(
                module, client.get_json(module.detail_url(summary)), summary
            )
            receiver = detail.get("ReceiverId")
            if type(detail.get("IsSender")) is not bool:
                raise FamiliekilderError("Meldingen mangler verifisert avsenderstatus")
            if not isinstance(receiver, str) or not receiver:
                raise FamiliekilderError("Meldingen mangler mottakeridentitet")
            if (
                detail["IsSender"]
                and detail.get("SchoolId") == school_id
                and detail.get("OnBehalfOf") == child_id
                and detail.get("Header") == title
                and html_to_text(detail.get("Body")) == body
                and (is_absence or receiver == recipient_id)
                and detail.get("IsAbsenceReport") is is_absence
                and not module.attachment_specs(detail)
                and detail.get("Images") == []
            ):
                matches.append(ident)
        if len(seen) == total:
            return matches
        if len(seen) > total or len(rows) < module.PAGE_SIZE:
            raise FamiliekilderError(
                "Sendtlistens sider samsvarer ikke med totalantallet"
            )
    raise FamiliekilderError("Sendtlisten kunne ikke kontrolleres fullstendig")


def send_message(
    runtime, operation_id, child_id, school_id, recipient_id, title, content
):
    runtime.config.require("skolemelding", "messages")
    for value in (child_id, school_id, recipient_id):
        _key(value)
    if "," in recipient_id or ";" in recipient_id:
        raise FamiliekilderError("Velg én lærer-ID, ikke en sammensatt mottakerliste")
    _text(title, 200)
    _text(content, 10000)
    with (
        provider(runtime.config.root, "skolemelding") as (module, client),
        runtime.ledger() as ledger,
    ):
        profile, _ = module.fetch_context(client, client.app_version)
        domain = profile.get("DomainUser") if isinstance(profile, dict) else None
        actor = _key(domain.get("UserName") if isinstance(domain, dict) else None)
        if actor != client.session.get("userName"):
            raise FamiliekilderError("Profilen samsvarer ikke med innlogget konto")
        body_html = "<p>" + html.escape(content).replace("\n", "<br>") + "</p>"
        body = html_to_text(body_html)
        request = {
            "account_fingerprint": fingerprint(actor),
            "child_id": child_id,
            "school_id": school_id,
            "recipient_id": recipient_id,
            "title": title,
            "content": content,
        }
        target = {k: request[k] for k in ("child_id", "school_id", "recipient_id")}
        prepared = {}

        def observe():
            # Revalidate child/school even during recovery; no stale account data.
            prepared["recipient"] = _recipient(
                module, client, child_id, school_id, recipient_id
            )
            return _matches(
                module, client, child_id, school_id, recipient_id, title, body
            )

        def send():
            payload = {
                "Header": title,
                "body": body_html,
                "OnBehalfOf": child_id,
                "SchoolId": school_id,
                "UserRecipients": [prepared["recipient"]],
                "GroupRecipients": [],
                "Attachments": [],
                "ExistingFileIds": [],
                "MessageImages": [],
                "IsReplyAllowed": True,
                "IsOtherUserAdded": False,
                "FeedbackRequired": False,
                "FeedbackQuestionRequired": False,
                "IsSmsMessage": False,
                "SmsNotification": False,
                "IsAbsenceReport": False,
                "HideReceivers": False,
                "tryAttachment": True,
            }
            # Deliberately bypass the read-only adapter method, not its transport
            # host validation/pacing. Never refresh/retry a mutating request on 401.
            return client.transport.post_json(
                module.PORTAL_BASE_URL + "/api/message",
                payload,
                bearer_token=client.token,
                allow_empty=True,
            )

        return perform(
            ledger,
            operation_id,
            "skolemelding",
            "send_message",
            request,
            target,
            observe,
            send,
        )


def register(server, runtime):
    from .source_server import WRITE, safe_tool

    def skolemelding_send_message(
        operation_id: str,
        child_id: str,
        school_id: str,
        recipient_id: str,
        title: str,
        content: str,
    ) -> dict:
        """Send ny ren tekstmelding til én valgt lærer etter konkret brukerbeskjed. Bruk barn/skole og lærer-ID fra ferske oppslag. Ingen kopi til annen foresatt, SMS eller vedlegg. Maks 200 tegn i tittel og 10000 i tekst. Ukjent utfall kontrolleres med samme UUID, aldri ny sending."""
        return send_message(
            runtime, operation_id, child_id, school_id, recipient_id, title, content
        )

    if "messages" in runtime.config.services["skolemelding"]:
        server.add_tool(safe_tool(skolemelding_send_message), annotations=WRITE)
