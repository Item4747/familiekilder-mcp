"""Hoopit participant responses, verified in the official Flutter web client.

PATCH response is distinct from organizer actions or registration of attendance.
The existing authorized reader retains its JWT headers, pacing and cooldowns.
"""

from datetime import datetime, UTC

from .core import FamiliekilderError
from .live_readers import provider
from .action_ledger import now
from .source_adapter import SourceUnavailable

# EventResponseStatus.c, not its Dart enum index (.a).
STATES = {0: "unanswered", 1: "declined", 2: "accepted", 4: "waitlisted", 5: "maybe"}


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


def _rows(value):
    if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
        raise SourceUnavailable("Hoopit returnerte en ugyldig person-/svarliste")
    return value


def _context(client, event_id, occurrence_id):
    profile = client.get("/app/users/current/")
    children = _rows(client.get("/app/users/current/children/"))
    if not isinstance(profile, dict):
        raise SourceUnavailable("Hoopit mangler egen profil")
    people = {_id(p.get("id")): p for p in [profile, *children]}
    path = f"/app/events/{event_id}/occurrences/{occurrence_id}/"
    event = client.get(path)
    if (
        not isinstance(event, dict)
        or _id(event.get("id")) != occurrence_id
        or _id(event.get("event")) != event_id
    ):
        raise SourceUnavailable("Hoopit returnerte feil arrangement/forekomst")
    data = client.get(path + "my/responses/")
    if not isinstance(data, dict):
        raise SourceUnavailable("Hoopit mangler egne svar")
    participants = []
    seen = set()
    for row in _rows(data.get("participants")):
        ident = _id(row.get("id"))
        person = _id((row.get("user") or {}).get("id"))
        if (
            ident in seen
            or _id(row.get("event_id")) != event_id
            or _id(row.get("occurrence_id")) != occurrence_id
        ):
            raise SourceUnavailable(
                "Hoopit returnerte tvetydig eller feil svarreferanse"
            )
        if row.get("user_id") is not None and _id(row["user_id"]) != person:
            raise SourceUnavailable("Hoopit returnerte motstridende person-ID-er")
        seen.add(ident)
        if person in people:
            participants.append(row)
    return event, participants


def _status(row):
    value = row.get("response")
    return STATES.get(value) if type(value) is int else None


def participation_context(runtime, event_id, occurrence_id):
    runtime.config.require("hoopit")
    event_id, occurrence_id = _id(event_id), _id(occurrence_id)
    with provider(runtime.config.root, "hoopit") as (_, client):
        event, rows = _context(client, event_id, occurrence_id)
        return {
            "source": "hoopit",
            "fetched_at": now(),
            "complete": True,
            "reference": {
                "kind": "event",
                "id": event_id,
                "occurrence_id": occurrence_id,
            },
            "title": event.get("title"),
            "start": event.get("start"),
            "end": event.get("end"),
            "cancelled": event.get("cancelled"),
            "locked": event.get("is_locked"),
            "response_deadline": event.get("response_deadline"),
            "payment_required": bool(event.get("payment")),
            "participants": [
                {
                    "response_id": _id(r["id"]),
                    "person_id": _id(r["user"]["id"]),
                    "name": " ".join(
                        r["user"].get(k) or "" for k in ("first_name", "last_name")
                    ),
                    "status": _status(r),
                    "can_edit_response": r.get("can_edit_response") is True,
                    "payment_linked": r.get("user_payment") is not None,
                    "waiting_list_position": r.get("waiting_list_position"),
                }
                for r in rows
            ],
            "source_content_is_untrusted": True,
        }


def _select(rows, response_id, person_id):
    selected = [
        r
        for r in rows
        if _id(r["id"]) == response_id and _id(r["user"]["id"]) == person_id
    ]
    if len(selected) != 1:
        raise FamiliekilderError(
            "Svaret tilhører ikke en entydig egen person eller eget barn"
        )
    return selected[0]


def _validate(event, row):
    if row.get("can_edit_response") is not True:
        raise FamiliekilderError("Hoopit tillater ikke endring av dette svaret")
    if any(
        event.get(k) is not False
        for k in ("cancelled", "is_locked", "is_attendance_managed")
    ):
        raise FamiliekilderError(
            "Arrangementet er avlyst, låst eller oppmøte er registrert"
        )
    if event.get("payment") or row.get("user_payment") is not None:
        raise FamiliekilderError(
            "Deltakelse knyttet til betaling må håndteres i Hoopit"
        )
    for key in ("end", "response_deadline"):
        value = event.get(key)
        if value is None and key == "response_deadline":
            continue
        try:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            raise SourceUnavailable(
                "Hoopit mangler pålitelig sluttid eller svarfrist"
            ) from None
        if stamp <= datetime.now(UTC):
            raise FamiliekilderError("Arrangementet eller svarfristen er passert")


def set_participation(
    runtime,
    operation_id,
    event_id,
    occurrence_id,
    response_id,
    person_id,
    accepted,
    reason=None,
):
    runtime.config.require("hoopit", "participation")
    event_id, occurrence_id, response_id, person_id = map(
        _id, (event_id, occurrence_id, response_id, person_id)
    )
    if type(accepted) is not bool:
        raise FamiliekilderError("accepted må være true eller false")
    if reason is not None and (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > 2000
        or "\x00" in reason
    ):
        raise FamiliekilderError("Begrunnelse må være kort, ikke tom tekst")
    if accepted and reason is not None:
        raise FamiliekilderError("Begrunnelse brukes bare ved avmelding")
    request = dict(
        event_id=event_id,
        occurrence_id=occurrence_id,
        response_id=response_id,
        person_id=person_id,
        accepted=accepted,
        reason=reason,
    )
    payload = {"response": 2 if accepted else 1}
    if reason is not None:
        payload["response_reason"] = reason
    with (
        provider(runtime.config.root, "hoopit") as (module, client),
        runtime.ledger() as ledger,
    ):
        prior = ledger.reserve_operation(
            operation_id, "hoopit", "set_participation", request
        )
        if prior and prior["state"] == "verified":
            return ledger.operation_result(operation_id, "verified")
        event, rows = _context(client, event_id, occurrence_id)
        row = _select(rows, response_id, person_id)
        receipt = {
            "source": "hoopit",
            "action": "set_participation",
            "event_id": event_id,
            "occurrence_id": occurrence_id,
            "person_id": person_id,
            "reference": response_id,
            "requested_participation": "accepted" if accepted else "declined",
        }

        def reconcile(row):
            status = _status(row)
            receipt.update(
                participation=status,
                checked_at=now(),
                waiting_list_position=row.get("waiting_list_position"),
            )
            matched = status in (
                {"accepted", "waitlisted"} if accepted else {"declined"}
            )
            if reason is not None:
                matched = matched and row.get("response_reason") == reason
            if matched:
                receipt["verified_at"] = now()
            return matched

        if reconcile(row):
            return ledger.operation_result(operation_id, "verified", receipt)
        if prior and prior["state"] in {"sending", "unknown"}:
            return ledger.operation_result(operation_id, "unknown", receipt)
        if _status(row) is None:
            raise FamiliekilderError("Ukjent svarstatus; ingen handling utført")
        _validate(event, row)
        client._begin()  # Existing request budget and persistent per-source pacing.
        receipt["started_at"] = now()
        ledger.operation_result(operation_id, "sending", receipt)
        try:
            client.transport.patch_json(
                module.API_ROOT
                + f"/app/events/{event_id}/occurrences/{occurrence_id}/responses/{response_id}/",
                payload,
                headers=client._headers(4),
            )
        except Exception as exc:
            # Persist the provider's stop condition, never retry the PATCH.
            try:
                client._failure(exc, authenticated_origin=True)
            except FamiliekilderError:
                pass
            return ledger.operation_result(operation_id, "unknown", receipt)
        client.limiter.record_success()
        try:
            _, rows = _context(client, event_id, occurrence_id)
            verified = reconcile(_select(rows, response_id, person_id))
        except Exception:
            verified = False
        return ledger.operation_result(
            operation_id, "verified" if verified else "unknown", receipt
        )


def register(server, runtime):
    from .source_server import READ, WRITE, safe_tool

    def hoopit_participation_context(event_id: str, occurrence_id: str) -> dict:
        """Les arrangement og svar for deg/egne barn. Velg stabile response_id og person_id. Organisatorsvar og faktisk oppmøte inngår ikke."""
        return participation_context(runtime, event_id, occurrence_id)

    def hoopit_set_participation(
        operation_id: str,
        event_id: str,
        occurrence_id: str,
        response_id: str,
        person_id: str,
        accepted: bool,
        reason: str | None = None,
    ) -> dict:
        """Meld på/av etter konkret brukerbeskjed; valgfri begrunnelse ved avmelding. UUID per handling. Ukjent resultat kontrolleres med samme UUID uten ny sending. Betaling/organisatoroppmøte inngår ikke; venteliste rapporteres separat."""
        return set_participation(
            runtime,
            operation_id,
            event_id,
            occurrence_id,
            response_id,
            person_id,
            accepted,
            reason,
        )

    server.add_tool(safe_tool(hoopit_participation_context), annotations=READ)
    if "participation" in runtime.config.services["hoopit"]:
        server.add_tool(safe_tool(hoopit_set_participation), annotations=WRITE)
    from .live_hoopit_messages import register as register_messages

    register_messages(server, runtime)
