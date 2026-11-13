"""School-managed absence reports; never claim attendance registration."""

import re
from datetime import date, datetime, time, UTC

from .core import FamiliekilderError, OSLO, html_to_text
from .live_create import perform
from .live_readers import provider
from .live_skolemelding import _context, _key
from .live_skolemelding_messages import _matches
from .action_ledger import fingerprint

TITLE = "Melding om fravær"


def _period(starts_on, ends_on, starts_at=None, ends_at=None):
    try:
        if any(
            not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)
            for v in (starts_on, ends_on)
        ):
            raise ValueError
        start, end = date.fromisoformat(starts_on), date.fromisoformat(ends_on)
        if not 0 <= (end - start).days < 366:
            raise ValueError
        if starts_at is None and ends_at is None:
            return start, end, None, None
        if any(
            not isinstance(v, str) or not re.fullmatch(r"\d{2}:\d{2}", v)
            for v in (starts_at, ends_at)
        ):
            raise ValueError
        a, b = time.fromisoformat(starts_at), time.fromisoformat(ends_at)
        first, last = datetime.combine(start, a), datetime.combine(end, b)
        if last <= first:
            raise ValueError
        for value in (first, last):
            aware = value.replace(tzinfo=OSLO)
            if (
                aware.astimezone(UTC).astimezone(OSLO).replace(tzinfo=None) != value
                or aware.utcoffset() != value.replace(tzinfo=OSLO, fold=1).utcoffset()
            ):
                raise ValueError
        return start, end, a, b
    except (ValueError, TypeError):
        raise FamiliekilderError(
            "Velg inklusive ISO-datoer over høyst 366 dager og eventuelt begge klokkeslett HH:MM i Europe/Oslo; perioden må være entydig og positiv"
        ) from None


def _body(name, period):
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 256
        or any(ord(c) < 32 or c in "<>&" for c in name)
    ):
        raise FamiliekilderError(
            "Barnets navn kan ikke brukes entydig i fraværsskjemaet"
        )
    start, end, first, last = period
    left, right = start.strftime("%d.%m.%Y"), end.strftime("%d.%m.%Y")
    if first is None:
        when = f"{left} til {right}"
    elif start == end:
        when = f"{left} kl. {first:%H.%M} til {last:%H.%M}"
    else:
        when = f"{left} kl. {first:%H.%M} til {right} kl. {last:%H.%M}"
    return f"Elev: {name}\nTidspunkt for fravær: {when}"


def _child(module, client, child_id, school_id):
    children, _ = _context(module, client)
    child = next((v for v in children if v["id"] == child_id), None)
    school = (
        next((s for s in child["schools"] if s["id"] == school_id), None)
        if child
        else None
    )
    if not school or school["has_teacher_connected"] is not True:
        raise FamiliekilderError(
            "Velg eget barn/skole med bekreftet lærertilknytning for fravær"
        )
    return child


def report_absence(
    runtime,
    operation_id,
    child_id,
    school_id,
    starts_on,
    ends_on,
    starts_at=None,
    ends_at=None,
):
    runtime.config.require("skolemelding", "absence")
    _key(child_id)
    _key(school_id)
    period = _period(starts_on, ends_on, starts_at, ends_at)
    with (
        provider(runtime.config.root, "skolemelding") as (module, client),
        runtime.ledger() as ledger,
    ):
        profile, _ = module.fetch_context(client, client.app_version)
        domain = profile.get("DomainUser") if isinstance(profile, dict) else None
        actor = _key(domain.get("UserName") if isinstance(domain, dict) else None)
        if actor != client.session.get("userName"):
            raise FamiliekilderError("Profilen samsvarer ikke med innlogget konto")
        child = _child(module, client, child_id, school_id)
        body = _body(child["name"], period)
        target = {
            "child_id": child_id,
            "school_id": school_id,
            "starts_on": starts_on,
            "ends_on": ends_on,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "timezone": "Europe/Oslo",
            "recipient_selection": "service_managed_contact_teacher_and_absence_group",
            "attendance_registration_verified": False,
        }
        # A source-side name change must not evade the unresolved-operation
        # barrier for the same child and period. Names are presentation only.
        request = {**target, "account_fingerprint": fingerprint(actor)}

        def observe():
            current = _child(module, client, child_id, school_id)
            if _body(current["name"], period) != body:
                raise FamiliekilderError("Barnets kildegrunnlag er endret")
            return _matches(
                module,
                client,
                child_id,
                school_id,
                None,
                TITLE,
                html_to_text(body),
                is_absence=True,
            )

        def send():
            payload = {
                "Header": TITLE,
                "body": body,
                "OnBehalfOf": child_id,
                "SchoolId": school_id,
                "GroupRecipients": [],
                "UserRecipients": [],
                "Attachments": [],
                "ExistingFileIds": [],
                "MessageImages": [],
                "IsReplyAllowed": True,
                "IsOtherUserAdded": False,
                "FeedbackRequired": False,
                "FeedbackQuestionRequired": False,
                "IsSmsMessage": False,
                "SmsNotification": False,
                "IsAbsenceReport": True,
            }
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
            "report_absence",
            request,
            target,
            observe,
            send,
        )


def register(server, runtime):
    from .source_server import WRITE, safe_tool

    def skolemelding_report_absence(
        operation_id: str,
        child_id: str,
        school_id: str,
        starts_on: str,
        ends_on: str,
        starts_at: str | None = None,
        ends_at: str | None = None,
    ) -> dict:
        """Meld fravær for eget barn etter konkret brukerbeskjed, med separat absence-rettighet. Datoer YYYY-MM-DD er inklusive; eventuelle begge klokkeslett HH:MM gjelder Europe/Oslo. Tjenesten ruter til kontaktlærer/fraværsgruppe. Ingen fritekst, årsak eller kopi til annen foresatt. Kvittering bekrefter meldingen, IKKE føring i skolens fraværsregister. Ukjent utfall kontrolleres med samme UUID uten ny sending."""
        return report_absence(
            runtime,
            operation_id,
            child_id,
            school_id,
            starts_on,
            ends_on,
            starts_at,
            ends_at,
        )

    if "absence" in runtime.config.services["skolemelding"]:
        server.add_tool(safe_tool(skolemelding_report_absence), annotations=WRITE)
