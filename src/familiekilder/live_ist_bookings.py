"""IST parent conversation bookings, distinct from general event attendance."""

import json
import math
import uuid
from datetime import timedelta

from .core import FamiliekilderError
from .live_ist import Client, identifier, _rows, _period, _wire, _source_instant
from .live_readers import provider
from .action_ledger import now
from .source_adapter import SourceUnavailable

SLOT = "id startsAt endsAt cancelled booking { id } placement { id }"
FIELDS = (
    "id combinedBookingAndInvitationId createdAt createdBy { id firstName lastName } "
    "startsAt endsAt title message description location status placement { id } "
    "bookedSlot { " + SLOT + " } availableSlots { " + SLOT + " }"
)
FRAGMENT = "__typename ... on TimeBooking { " + FIELDS + " }"
LIST = (
    "query FamilyBookings($startsAt:LocalDateTime!,$endsAt:LocalDateTime!) {"
    "getCalendarItems(startsAt:$startsAt,endsAt:$endsAt,types:[TimeBooking]) { "
    + FRAGMENT
    + " } }"
)
DETAIL = (
    "query FamilyBooking($id:ID!) { getTimeBooking(input:$id) { " + FRAGMENT + " } }"
)
CHANGES = (
    "__typename ... on ItemsUpdate { items { " + FRAGMENT + " } } "
    "... on ItemsUpserted { items { " + FRAGMENT + " } } "
    "... on ItemsDeleted { items { " + FRAGMENT + " } }"
)
ALLOCATE = (
    "mutation FamilyBookSlot($inputs:AllocateTimeSlotInput!) {"
    "allocateTimeSlot(inputs:$inputs) { " + CHANGES + " } }"
)
CANCEL = (
    "mutation FamilyCancelSlot($inputs:[ID!]!) {"
    "cancelTimeSlot(inputs:$inputs) { " + CHANGES + " } }"
)
STATUSES = {
    "BOOKED",
    "CANCELLED",
    "DONE",
    "SENT",
    "TEACHER_TIMESLOT_CANCELLED",
    "TIMESLOT_CANCELLED",
    "TIMESLOT_DELETED",
}


def _booking(value, placement_id, booking_id=None):
    if (
        not isinstance(value, dict)
        or value.get("__typename") != "TimeBooking"
        or (value.get("placement") or {}).get("id") != placement_id
        or (booking_id is not None and value.get("id") != booking_id)
        or value.get("status") not in STATUSES
    ):
        raise SourceUnavailable("IST returnerte feil eller ufullstendig samtalebooking")
    identifier(value.get("id"))
    identifier(value.get("combinedBookingAndInvitationId"))
    slots = _rows(value.get("availableSlots"))
    if len(slots) > 5000 or len({s.get("id") for s in slots}) != len(slots):
        raise SourceUnavailable(
            "IST returnerte for mange eller dupliserte samtaletider"
        )
    booked = value.get("bookedSlot")
    if booked is not None:
        if not isinstance(booked, dict):
            raise SourceUnavailable("Ugyldig booket samtaletid")
        slots = [*slots, booked]
    for slot in slots:
        identifier(slot.get("id"))
        if (
            type(slot.get("cancelled")) is not bool
            or (slot.get("booking") or {}).get("id") != value["id"]
            or (slot.get("placement") or {}).get("id") != placement_id
        ):
            raise SourceUnavailable(
                "Samtaletiden tilhører ikke valgt booking/plassering"
            )
    return value


def detail(client, placement_id, booking_id):
    identifier(booking_id)
    return _booking(
        client.read(DETAIL, {"id": booking_id}).get("getTimeBooking"),
        placement_id,
        booking_id,
    )


def _decorate(value, child_id, placement):
    return {
        **value,
        "reference": {
            "kind": "time_booking",
            "id": value["id"],
            "child_id": child_id,
            "placement_id": placement["id"],
        },
        "timezone": placement.get("tz"),
    }


def read(
    runtime, child_id, placement_id, *, booking_id=None, starts_at=None, ends_at=None
):
    runtime.config.require("ist-home")
    identifier(child_id)
    identifier(placement_id)
    if booking_id is not None:
        identifier(booking_id)
    else:
        start, end = _period(starts_at, ends_at)
    with provider(runtime.config.root, "ist-home") as (module, token):
        client = Client(runtime.config.root, module, token)
        ctx = client.context()
        placement = client.select(ctx, child_id, placement_id)
        if booking_id is not None:
            rows = [detail(client, placement_id, booking_id)]
        else:
            all_rows = _rows(
                client.read(LIST, {"startsAt": _wire(start), "endsAt": _wire(end)}).get(
                    "getCalendarItems"
                )
            )
            if len(all_rows) > 5000:
                raise SourceUnavailable("For mange samtalebookinger; avgrens perioden")
            if any(r.get("__typename") != "TimeBooking" for r in all_rows):
                raise SourceUnavailable("IST returnerte feil kalendertype")
            rows = [
                _booking(r, placement_id)
                for r in all_rows
                if (r.get("placement") or {}).get("id") == placement_id
            ]
            if len({r["id"] for r in rows}) != len(rows):
                raise SourceUnavailable("Dupliserte samtalebookinger")
        settings = placement["effective_settings"]
        return {
            "source": "ist-home",
            "fetched_at": now(),
            "source_content_is_untrusted": True,
            "items": [_decorate(r, child_id, placement) for r in rows],
            "complete": not ctx["failedCalendarFetches"],
            "failed_calendar_fetches": ctx["failedCalendarFetches"],
            "settings": {
                k: settings.get(k)
                for k in ("enableTimeBookings", "ICBCancelLimit", "readonly")
            },
            "coverage": "Valgt plassering og booking eller tidsrom; API-et har ingen sidemarkør. Kun samtaletider, ikke generell arrangementsdeltakelse.",
        }


def _slot(value, slot_id, accepted):
    if accepted:
        candidates = value["availableSlots"]
        if value.get("bookedSlot"):
            candidates = [*candidates, value["bookedSlot"]]
    else:
        candidates = [value["bookedSlot"]] if value.get("bookedSlot") else []
    matches = [s for s in candidates if s["id"] == slot_id]
    if not matches:
        raise FamiliekilderError(
            "Velg en tilgjengelig eller booket samtaletid fra ferskt oppslag"
        )
    if any(s != matches[0] for s in matches):
        raise SourceUnavailable("Samtaletiden har motstridende opplysninger")
    return matches[0]


def _clock(ctx, placement):
    return _source_instant(ctx.get("serverTime"), placement.get("tz"))


def validate(ctx, placement, booking, slot, accepted):
    settings = placement["effective_settings"]
    if (
        ctx["failedCalendarFetches"]
        or settings.get("enableTimeBookings") is not True
        or settings.get("readonly") is not False
        or placement.get("readonly") is True
    ):
        raise FamiliekilderError(
            "IST har ikke bekreftet ferske data og skriverettighet for samtalebooking"
        )
    zone = placement.get("tz")
    first = _source_instant(slot.get("startsAt"), zone)
    last = _source_instant(slot.get("endsAt"), zone) if slot.get("endsAt") else None
    current = _clock(ctx, placement)
    if slot["cancelled"] or first <= current or (last is not None and last <= first):
        raise FamiliekilderError("Samtaletiden er avlyst, utløpt eller ugyldig")
    begins = _source_instant(placement.get("startsAt"), zone)
    ends = (
        _source_instant(placement["endsAt"], zone) if placement.get("endsAt") else None
    )
    if first < begins or (ends and (last or first) > ends):
        raise FamiliekilderError("Samtaletiden er utenfor barnets plassering")
    if accepted:
        if booking["status"] != "SENT" or booking.get("bookedSlot") is not None:
            raise FamiliekilderError(
                "Velg en åpen invitasjon; verktøyet flytter ikke en eksisterende bestilling"
            )
        if not any(s["id"] == slot["id"] for s in booking["availableSlots"]):
            raise FamiliekilderError("Samtaletiden er ikke lenger tilgjengelig")
    else:
        hours = settings.get("ICBCancelLimit")
        if (
            type(hours) not in (int, float)
            or not math.isfinite(hours)
            or not 0 <= hours <= 87600
        ):
            raise FamiliekilderError("IST mangler en gyldig avbestillingsfrist")
        if (
            booking["status"] != "BOOKED"
            or (booking.get("bookedSlot") or {}).get("id") != slot["id"]
        ):
            raise FamiliekilderError(
                "Bare den konkret valgte bestillingen kan avbestilles"
            )
        if current + timedelta(hours=hours) >= first:
            raise FamiliekilderError("ISTs avbestillingsfrist er passert")


def _matches(value, receipt):
    if (
        value["combinedBookingAndInvitationId"] != receipt["invitation_id"]
        or value["id"] != receipt["booking_id"]
    ):
        return False
    booked = value.get("bookedSlot")
    if receipt["accepted"]:
        return (
            value["status"] == "BOOKED"
            and booked is not None
            and booked["id"] == receipt["slot_id"]
            and booked["cancelled"] is False
            and booked["startsAt"] == receipt["starts_at"]
            and booked.get("endsAt") == receipt["ends_at"]
        )
    return value["status"] == "CANCELLED" and booked is None


def set_participation(
    runtime, operation_id, child_id, placement_id, booking_id, slot_id, accepted
):
    runtime.config.require("ist-home", "participation")
    try:
        uuid.UUID(operation_id)
    except (TypeError, ValueError, AttributeError):
        raise FamiliekilderError("operation_id må være en UUID") from None
    for ident in (child_id, placement_id, booking_id, slot_id):
        identifier(ident)
    if type(accepted) is not bool:
        raise FamiliekilderError("accepted må være boolsk")
    with (
        provider(runtime.config.root, "ist-home") as (module, token),
        runtime.ledger() as ledger,
    ):
        client = Client(runtime.config.root, module, token)
        ctx = client.context()
        placement = client.select(ctx, child_id, placement_id)
        request = {
            "sender_id": ctx["user"]["id"],
            "child_id": child_id,
            "placement_id": placement_id,
            "booking_id": booking_id,
            "slot_id": slot_id,
            "accepted": accepted,
        }
        prior = ledger.reserve_operation(
            operation_id, "ist-home", "set_participation", request
        )
        if prior and prior["state"] == "verified":
            return ledger.operation_result(operation_id, "verified")
        recovering = prior and prior["state"] in {"sending", "unknown"}
        if recovering:
            receipt = json.loads(prior["receipt_json"] or "null")
            if not isinstance(receipt, dict):
                return ledger.operation_result(operation_id, "unknown")
        else:
            before = detail(client, placement_id, booking_id)
            slot = _slot(before, slot_id, accepted)
            receipt = {
                "source": "ist-home",
                "action": "set_participation",
                **request,
                "invitation_id": before["combinedBookingAndInvitationId"],
                "starts_at": slot["startsAt"],
                "ends_at": slot.get("endsAt"),
                "started_at": now(),
            }
            if _matches(before, receipt):
                if ctx["failedCalendarFetches"]:
                    raise SourceUnavailable(
                        "IST kunne ikke bekrefte fersk bookingstatus"
                    )
                receipt.update(unchanged=True, verified_at=now())
                return ledger.operation_result(operation_id, "verified", receipt)
            validate(ctx, placement, before, slot, accepted)
            ctx2 = client.context()
            p2 = client.select(ctx2, child_id, placement_id)
            fresh = detail(client, placement_id, booking_id)
            chosen = _slot(fresh, slot_id, accepted)
            if (
                ctx2["user"]["id"] != ctx["user"]["id"]
                or fresh["combinedBookingAndInvitationId"] != receipt["invitation_id"]
                or chosen["startsAt"] != slot["startsAt"]
                or chosen.get("endsAt") != slot.get("endsAt")
            ):
                raise SourceUnavailable("Konto, invitasjon eller samtaletid er endret")
            validate(ctx2, p2, fresh, chosen, accepted)
            query = ALLOCATE if accepted else CANCEL
            field = "allocateTimeSlot" if accepted else "cancelTimeSlot"
            inputs = (
                {
                    "combinedBookingAndInvitationId": receipt["invitation_id"],
                    "timeSlotId": slot_id,
                }
                if accepted
                else [receipt["invitation_id"]]
            )
            ledger.operation_result(operation_id, "sending", receipt)
            try:
                result = client.write(query, {"inputs": inputs})
            except Exception:
                # Includes timeouts and auth errors: no mutation is repeated.
                pass
            else:
                try:
                    for change in _rows(result.get(field)):
                        if change.get("__typename") not in {
                            "ItemsUpdate",
                            "ItemsUpserted",
                            "ItemsDeleted",
                        }:
                            raise SourceUnavailable(
                                "Ukjent endringstype i kvitteringen"
                            )
                        for row in _rows(change.get("items")):
                            if row.get("id") == booking_id:
                                if change[
                                    "__typename"
                                ] == "ItemsDeleted" or not _matches(
                                    _booking(row, placement_id, booking_id), receipt
                                ):
                                    receipt["response_mismatch"] = True
                except Exception:
                    receipt["response_mismatch"] = True
                ledger.operation_result(operation_id, "sending", receipt)
        try:
            after = detail(client, placement_id, booking_id)
            latest = client.context()
            client.select(latest, child_id, placement_id)
            verified = (
                latest["user"]["id"] == receipt["sender_id"]
                and not latest["failedCalendarFetches"]
                and _matches(after, receipt)
                and not receipt.get("response_mismatch")
            )
        except Exception:
            verified = False
        if not verified:
            return ledger.operation_result(operation_id, "unknown", receipt)
        receipt.update(
            reference={
                "kind": "time_booking",
                "id": booking_id,
                "child_id": child_id,
                "placement_id": placement_id,
            },
            verified_at=now(),
        )
        return ledger.operation_result(operation_id, "verified", receipt)


def register(server, runtime):
    from .source_server import READ, WRITE, safe_tool

    def ist_home_list_bookings(
        child_id: str, placement_id: str, starts_at: str, ends_at: str
    ) -> dict:
        """Les samtaleinvitasjoner og bestilte/ledige tider for eget barn/plassering i avgrenset periode. ISO-tidspunkt med tidssone. Ingen endring."""
        return read(
            runtime, child_id, placement_id, starts_at=starts_at, ends_at=ends_at
        )

    def ist_home_booking_context(
        child_id: str, placement_id: str, booking_id: str
    ) -> dict:
        """Les valgt IST-samtalebooking med stabile tid-ID-er, status og avbestillingsfrist. Kildesvikt eller manglende treff betyr ikke avbestilling."""
        return read(runtime, child_id, placement_id, booking_id=booking_id)

    def ist_home_set_participation(
        operation_id: str,
        child_id: str,
        placement_id: str,
        booking_id: str,
        slot_id: str,
        accepted: bool,
    ) -> dict:
        """Bestill (true) en tilgjengelig samtaletid fra en åpen invitasjon, eller avbestill (false) valgt booket samtaletid etter konkret brukerbeskjed. Gjelder samtalebooking, ikke generell arrangementsdeltakelse. Flytter ikke en eksisterende bestilling. Avbestillingsfrist håndheves. Bruk samme UUID ved ukjent resultat; ingen automatisk ny sending."""
        return set_participation(
            runtime, operation_id, child_id, placement_id, booking_id, slot_id, accepted
        )

    for tool in (ist_home_list_bookings, ist_home_booking_context):
        server.add_tool(safe_tool(tool), annotations=READ)
    if "participation" in runtime.config.services["ist-home"]:
        server.add_tool(safe_tool(ist_home_set_participation), annotations=WRITE)
