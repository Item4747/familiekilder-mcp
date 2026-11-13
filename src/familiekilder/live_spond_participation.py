"""Spond participation: fresh member context and verified on-/off-registration."""

import json
from .core import FamiliekilderError
from .api_transport import ApiHTTPError, ApiTransportError
from .live_readers import provider, identifier
from .live_spond_reader import spond_context
from .action_ledger import now


def participation_status(event, member_id):
    responses = event.get("responses")
    if not isinstance(responses, dict):
        return None
    for key, value in (
        ("acceptedIds", "accepted"),
        ("declinedIds", "declined"),
        ("waitinglistIds", "waitlisted"),
        ("unansweredIds", "unanswered"),
    ):
        if member_id in responses.get(key, []):
            return value
    return None


def allowed_members(profile, groups):
    from .spond import guardian_matches

    pid = str(profile.get("id") or profile.get("profileId"))
    result = {pid}
    for group in groups:
        for member in group.get("members", []):
            if not isinstance(member, dict):
                continue
            member_profile = member.get("profile") or {}
            guardians = member.get("guardians") or []
            own = member_profile.get("id") == pid or any(
                isinstance(g, dict) and guardian_matches(g, pid) for g in guardians
            )
            if own and member.get("id"):
                result.add(str(member["id"]))
    return result


def set_participation(runtime, operation_id, event_id, member_id, accepted):
    runtime.config.require("spond", "participation")
    identifier(event_id)
    identifier(member_id)
    if type(accepted) is not bool:
        raise FamiliekilderError("accepted må være true eller false")
    payload = {"event_id": event_id, "member_id": member_id, "accepted": accepted}
    expected = "accepted" if accepted else "declined"
    with provider(runtime.config.root, "spond") as (m, c), runtime.ledger() as ledger:
        prior = ledger.reserve_operation(
            operation_id, "spond", "participation", payload
        )
        if prior and prior["state"] == "verified":
            return {
                "operation_id": operation_id,
                "status": "verified",
                "receipt": json.loads(prior["receipt_json"]),
            }
        profile, groups, _ = spond_context(m, c)
        if member_id not in allowed_members(profile, groups):
            raise FamiliekilderError(
                "Personen er ikke deg eller et barn du representerer"
            )
        url = m.url_with_query(
            m.API_ROOT + "/sponds/" + event_id,
            {"includeComments": True, "addProfileInfo": True},
        )
        event = c.get_json(url)
        current = participation_status(event, member_id)
        receipt = {
            "source": "spond",
            "event_id": event_id,
            "member_id": member_id,
            "participation": current,
            "requested_participation": expected,
            "action": "set_participation",
            "verified_at": now(),
        }
        if current == expected:
            return ledger.operation_result(operation_id, "verified", receipt)
        if prior and prior["state"] in {"sending", "unknown"}:
            return ledger.operation_result(operation_id, "unknown", receipt)
        if event.get("cancelled") or event.get("cancelledAt"):
            raise FamiliekilderError("Arrangementet er avlyst")
        if accepted and any(
            event.get(k)
            for k in ("payment", "paymentRequest", "paymentInfo", "price", "fee")
        ):
            raise FamiliekilderError("Arrangement med betaling må håndteres i Spond")
        # A known participant state is required; never turn an arbitrary ID into an invitation.
        if current is None:
            raise FamiliekilderError(
                "Deltakeren finnes ikke i arrangementets svarliste"
            )
        response_url = m.API_ROOT + "/sponds/" + event_id + "/responses/" + member_id
        c._before_request()
        ledger.operation_result(operation_id, "sending", receipt)
        try:
            # Exact PUT + accepted boolean observed in the first-party web client.
            c.transport.put_json(
                response_url, {"accepted": accepted}, bearer_token=c.token
            )
        except ApiHTTPError as exc:
            result = ledger.operation_result(operation_id, "unknown")
            try:
                c._record_provider_stop(exc)
            except FamiliekilderError:
                pass
            return result
        except (ApiTransportError, OSError, TimeoutError):
            return ledger.operation_result(operation_id, "unknown")
        try:
            after = c.get_json(url)
        except (ApiHTTPError, ApiTransportError, FamiliekilderError, OSError):
            return ledger.operation_result(operation_id, "unknown")
        actual = participation_status(after, member_id)
        receipt.update(participation=actual, verified_at=now())
        # A waitlist is a distinct, truthful result, not a confirmed place.
        state = (
            "verified"
            if actual == expected or (accepted and actual == "waitlisted")
            else "unknown"
        )
        return ledger.operation_result(operation_id, state, receipt)


def register(server, runtime):
    from .source_server import WRITE, READ, safe_tool

    def spond_participation_context(event_id: str) -> dict:
        """Hent personer og svarstatus før på-/avmelding til et Spond-arrangement.

        Bruk event_id fra ferskt kildeoppslag. Returnerte members gir stabile
        person-ID-er for spond_set_participation; velg personen brukeren mener.
        Oppslaget endrer ingen deltakelse. Venteliste er ikke bekreftet plass.
        """
        runtime.config.require("spond")
        with provider(runtime.config.root, "spond") as (m, c):
            p, groups, _ = spond_context(m, c)
            event = c.get_json(
                m.url_with_query(
                    m.API_ROOT + "/sponds/" + identifier(event_id),
                    {"includeComments": True, "addProfileInfo": True},
                )
            )
            allowed = allowed_members(p, groups)
            names = {str(p.get("id") or p.get("profileId")): m.person_name(p)}
            for group in groups:
                for member in group.get("members", []):
                    if isinstance(member, dict):
                        names[str(member.get("id"))] = m.person_name(member)
            return {
                "event_id": event_id,
                "heading": event.get("heading"),
                "fetched_at": now(),
                "members": [
                    {
                        "id": member,
                        "name": names.get(member),
                        "status": participation_status(event, member),
                    }
                    for member in sorted(allowed)
                ],
                "source_content_is_untrusted": True,
            }

    def spond_set_participation(
        operation_id: str, event_id: str, member_id: str, accepted: bool
    ) -> dict:
        """Meld på/av etter konkret brukerbeskjed. Gjenbruk operation_id ved usikkert utfall. Betaling støttes ikke."""
        return set_participation(
            runtime, operation_id, event_id, member_id, accepted
        )

    server.add_tool(safe_tool(spond_participation_context), annotations=READ)
    if "participation" in runtime.config.services["spond"]:
        server.add_tool(safe_tool(spond_set_participation), annotations=WRITE)
