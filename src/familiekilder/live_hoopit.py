"""Hoopit comment threads and the family's answers to occurrences.

Reads use the member endpoints in ``hoopit.py``. Answering uses the one
documented write, ``PATCH events/{event_id}/occurrences/{occurrence_id}/
responses/{user_id}/`` with the token scope ``events:respond``. It is
registered only when the service has the ``participation`` write permission.
Hoopit checks the answer as the app does (deadline, full occurrence, waiting
list); the adapter checks the occurrence and the person first, sends once and
reads the answer back.
"""

from datetime import UTC, datetime

from .action_ledger import now
from .core import FamiliekilderError
from .live_readers import provider
from .source_adapter import SourceUnavailable


def _id(value):
    if type(value) is int and value > 0:
        return str(value)
    if (
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and 0 < len(value) <= 20
        and not value.startswith("0")
    ):
        return value
    raise FamiliekilderError("Velg en gyldig, stabil Hoopit-ID fra ferskt oppslag")


def _day(value):
    if value is None:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise FamiliekilderError("day må være en dato på formen ÅÅÅÅ-MM-DD") from None
    return value


def thread_context(runtime, post_id):
    """A post's full comment thread with IDs; Hoopit has no comments on occurrences."""

    runtime.config.require("hoopit")
    post_id = _id(post_id)
    with provider(runtime.config.root, "hoopit") as (module, client):
        roots = module.post_comments(client, post_id)
        comments = []
        for root in roots:
            for row in [root, *root.get("_replies", [])]:
                author = row.get("author") if isinstance(row.get("author"), dict) else {}
                comments.append(
                    {
                        "id": _id(row.get("id")),
                        "parent_id": _id(row["parent_id"])
                        if row.get("parent_id") is not None
                        else None,
                        "author_id": _id(author.get("id"))
                        if author.get("id") is not None
                        else None,
                        "author_name": author.get("full_name"),
                        "text": row.get("text"),
                        "created_at": row.get("created_at"),
                    }
                )
        return {
            "source": "hoopit",
            "reference": {"kind": "post", "id": post_id},
            "fetched_at": now(),
            "complete": True,
            "comments": comments,
            "source_content_is_untrusted": True,
        }


def _context(module, client, event_id, occurrence_id, day):
    occurrence = module.find_occurrence(client, event_id, occurrence_id, day)
    return occurrence, module.response_rows(occurrence)


def participation_context(runtime, event_id, occurrence_id, day=None):
    runtime.config.require("hoopit")
    event_id, occurrence_id, day = _id(event_id), _id(occurrence_id), _day(day)
    with provider(runtime.config.root, "hoopit") as (module, client):
        occurrence, rows = _context(module, client, event_id, occurrence_id, day)
        return {
            "source": "hoopit",
            "fetched_at": now(),
            "complete": True,
            "reference": {
                "kind": "event",
                "id": event_id,
                "occurrence_id": occurrence_id,
                **(
                    {"day": module.occurrence_day(occurrence)}
                    if module.occurrence_day(occurrence)
                    else {}
                ),
            },
            "title": occurrence.get("title"),
            "start": occurrence.get("start"),
            "end": occurrence.get("end"),
            "meetup_time": occurrence.get("meetup_time"),
            "cancelled": occurrence.get("cancelled"),
            "participants": [
                {
                    "person_id": row["person_id"],
                    "name": row["name"],
                    "status": module.RESPONSE_STATES.get(row["response"] or ""),
                }
                for row in rows
            ],
            "decline_reasons": list(module.DECLINE_REASONS),
            "source_content_is_untrusted": True,
        }


def _select(rows, person_id):
    selected = [row for row in rows if row["person_id"] == person_id]
    if len(selected) != 1:
        raise FamiliekilderError(
            "Personen er ikke invitert til denne aktiviteten i familiens svar"
        )
    return selected[0]


def _validate(occurrence):
    if occurrence.get("cancelled") is not False:
        raise FamiliekilderError("Aktiviteten er avlyst")
    try:
        end = datetime.fromisoformat(str(occurrence.get("end")).replace("Z", "+00:00"))
        if end.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        raise SourceUnavailable("Hoopit mangler pålitelig sluttid") from None
    if end <= datetime.now(UTC):
        raise FamiliekilderError("Aktiviteten er passert")


def set_participation(
    runtime,
    operation_id,
    event_id,
    occurrence_id,
    person_id,
    accepted,
    reason=None,
    day=None,
):
    runtime.config.require("hoopit", "participation")
    event_id, occurrence_id, person_id = map(_id, (event_id, occurrence_id, person_id))
    day = _day(day)
    if type(accepted) is not bool:
        raise FamiliekilderError("accepted må være true eller false")
    if reason is not None and accepted:
        raise FamiliekilderError("Begrunnelse brukes bare ved avmelding")
    if reason is not None and reason not in ("illness", "injury", "other_training", "other_reason"):
        raise FamiliekilderError(
            "Begrunnelse må være illness, injury, other_training eller other_reason"
        )
    request = dict(
        event_id=event_id,
        occurrence_id=occurrence_id,
        person_id=person_id,
        accepted=accepted,
        reason=reason,
    )
    payload = {"response": "accepted" if accepted else "declined"}
    if reason is not None:
        payload["response_reason"] = reason
    with (
        provider(runtime.config.root, "hoopit") as (module, client),
        runtime.ledger() as ledger,
    ):
        prior = ledger.reserve_operation(operation_id, "hoopit", "set_participation", request)
        if prior and prior["state"] == "verified":
            return ledger.operation_result(operation_id, "verified")
        occurrence, rows = _context(module, client, event_id, occurrence_id, day)
        row = _select(rows, person_id)
        receipt = {
            "source": "hoopit",
            "action": "set_participation",
            "event_id": event_id,
            "occurrence_id": occurrence_id,
            "person_id": person_id,
            "requested_participation": "accepted" if accepted else "declined",
        }

        def reconcile(row):
            status = module.RESPONSE_STATES.get(row["response"] or "")
            receipt.update(participation=status, checked_at=now())
            matched = status in ({"accepted", "waitlisted"} if accepted else {"declined"})
            if matched:
                receipt["verified_at"] = now()
            return matched

        if reconcile(row):
            return ledger.operation_result(operation_id, "verified", receipt)
        if prior and prior["state"] in {"sending", "unknown"}:
            return ledger.operation_result(operation_id, "unknown", receipt)
        if row["response"] is None:
            raise FamiliekilderError("Ukjent svarstatus; ingen handling utført")
        _validate(occurrence)
        receipt["started_at"] = now()
        ledger.operation_result(operation_id, "sending", receipt)
        try:
            client.patch(
                "response",
                payload,
                event_id=event_id,
                occurrence_id=occurrence_id,
                user_id=person_id,
            )
        except module.HoopitRequestRefused as exc:
            # A clear refusal (deadline, full, not invited, or no events:respond
            # on a 403) is not retried. The text says which.
            receipt["refused_status"] = exc.status_code
            receipt["detail"] = str(exc)
            return ledger.operation_result(operation_id, "unknown", receipt)
        except Exception:
            # The provider's stop condition is stored by the client; never retry.
            return ledger.operation_result(operation_id, "unknown", receipt)
        try:
            _, rows = _context(module, client, event_id, occurrence_id, day)
            verified = reconcile(_select(rows, person_id))
        except Exception:
            verified = False
        return ledger.operation_result(operation_id, "verified" if verified else "unknown", receipt)


def register(server, runtime):
    from .source_server import READ, WRITE, safe_tool

    def hoopit_thread_context(post_id: str) -> dict:
        """Hent fersk kommentartråd med kommentar-ID-er for ett Hoopit-innlegg. Hoopits API har ikke kommentarer på aktiviteter og ingen lest-markering."""
        return thread_context(runtime, post_id)

    def hoopit_participation_context(
        event_id: str, occurrence_id: str, day: str | None = None
    ) -> dict:
        """Les aktiviteten og familiens svar (deg og egne barn). day (ÅÅÅÅ-MM-DD) fra referansen gjør oppslaget raskere. Svarfrist, betaling og oppmøte gir ikke Hoopits API."""
        return participation_context(runtime, event_id, occurrence_id, day)

    def hoopit_set_participation(
        operation_id: str,
        event_id: str,
        occurrence_id: str,
        person_id: str,
        accepted: bool,
        reason: str | None = None,
        day: str | None = None,
    ) -> dict:
        """Meld på/av etter konkret brukerbeskjed. reason bare ved avmelding: illness, injury, other_training eller other_reason. UUID per handling. Ukjent resultat kontrolleres med samme UUID uten ny sending. Hoopit avgjør frist og venteliste; venteliste rapporteres som waitlisted."""
        return set_participation(
            runtime, operation_id, event_id, occurrence_id, person_id, accepted, reason, day
        )

    server.add_tool(safe_tool(hoopit_thread_context), annotations=READ)
    server.add_tool(safe_tool(hoopit_participation_context), annotations=READ)
    if "participation" in runtime.config.services["hoopit"]:
        server.add_tool(safe_tool(hoopit_set_participation), annotations=WRITE)
