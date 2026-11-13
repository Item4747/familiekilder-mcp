"""Direct parent meeting invitations and single-send attendance responses.

Wire contracts: Vigilo Android 3751 RxRestAPI, MeetingsRemoteDataSourceImpl,
MeetingInvitationApiModel and AuthInterceptor. No archive or seen markers.
"""

import json
from datetime import datetime, UTC
from urllib.parse import urljoin

from .core import FamiliekilderError
from .live_readers import provider, identifier, attachment_metadata
from .action_ledger import now
from .live_vigilo import ActionClient, _rows
from .source_adapter import SourceUnavailable

TYPES = {"singleMeetingTime", "multipleMeetingTimes"}


def _instant(value):
    try:
        if not isinstance(value, str) or "T" not in value:
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # The first-party parseUTCTime explicitly interprets source values as UTC.
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        raise SourceUnavailable("Vigilo returnerte ugyldig møtetid") from None


class Client(ActionClient):
    def __init__(self, root, module, connection):
        super().__init__(module, connection)
        self.root = root
        self.person_id = self.identity()

    def identity(self):
        return identifier(
            self.module.local_session_for_use(self.root).get("person_id")
        )

    def invitations(self, child_id):
        child, _ = self.select(child_id)
        # userId in Retrofit is consumed and removed by AuthInterceptor; it is
        # not a wire query. The selected bearer belongs to this person/tenant.
        data = self.get("api/forms/meetings/invitations/" + child_id)
        if set(data) != {"items"}:
            raise SourceUnavailable(
                "Uventet møteoversikt; sidedekningen kan ikke bekreftes"
            )
        rows = _rows(data.get("items"), "møteinvitasjoner")
        if len(rows) > 5000:
            raise SourceUnavailable(
                "For mange møteinvitasjoner til fullstendig kontroll"
            )
        if self.identity() != self.person_id:
            raise SourceUnavailable("Vigilo-kontoen endret seg under oppslaget")
        units = {
            identifier(u.get("id"))
            for u in _rows(child.get("organizationalUnits"), "enheter")
        }
        seen = set()
        for row in rows:
            ident = identifier(row.get("id"))
            if ident in seen or row.get("organizationalUnitId") not in units:
                raise SourceUnavailable("Duplisert møte eller møte uten barnets enhet")
            seen.add(ident)
            _validate(row)
        return rows

    def meeting(self, child_id, meeting_id):
        rows = [r for r in self.invitations(child_id) if r["id"] == meeting_id]
        if len(rows) != 1:
            raise SourceUnavailable(
                "Møteinvitasjonen finnes ikke; dette beviser ikke avmelding"
            )
        return rows[0]

    def cancel(self, child_id, meeting_id):
        return self.client.transport.delete_json(
            urljoin(
                self.module.API_ROOT,
                "api/forms/meetings/"
                + meeting_id
                + "/replies/"
                + self.person_id
                + "/"
                + child_id,
            ),
            bearer_token=self.client.token,
            headers={"appVersion": self.version},
            allow_empty=True,
        )


def _validate(row):
    if (
        row.get("meetingType") not in TYPES
        or "reply" not in row
        or "registrationDeadline" not in row
    ):
        raise SourceUnavailable("Ukjent eller ufullstendig møteinvitasjon")
    if row["registrationDeadline"] is not None:
        _instant(row["registrationDeadline"])
    slots = _rows(row.get("meetingOccurrences"), "møtetider")
    ids = [identifier(s.get("meetingOccurrenceId")) for s in slots]
    if len(slots) > 5000 or len(set(ids)) != len(ids):
        raise SourceUnavailable("Dupliserte eller for mange møtetider")
    for slot in slots:
        if type(slot.get("isAvailable")) is not bool or _instant(
            slot.get("endTime")
        ) <= _instant(slot.get("startTime")):
            raise SourceUnavailable("Ugyldig møtetid eller tilgjengelighet")
    reply = row["reply"]
    if reply is not None:
        if not isinstance(reply, dict) or reply.get("meetingOccurrenceId") not in ids:
            raise SourceUnavailable("Ugyldig møtesvar eller ukjent valgt møtetid")
        _instant(reply.get("replyTime"))
    _rows(row.get("attachments"), "møtevedlegg")


def item(row, child_id, person_id):
    return {
        "source_id": "meeting:" + row["id"] + ":" + child_id + ":" + person_id,
        "reference": {"kind": "meeting", "id": row["id"], "child_id": child_id},
        "title": row.get("title"),
        "body": row.get("description"),
        "location": row.get("location"),
        "organization_id": row["organizationalUnitId"],
        "invitee_person_id": person_id,
        "meeting_type": row["meetingType"],
        "registration_deadline": row["registrationDeadline"],
        "occurrences": row["meetingOccurrences"],
        "reply": row["reply"],
        "attachments": [attachment_metadata(a) for a in row["attachments"]],
        "content_complete": True,
        "coverage": "Invitasjoner returnert nå for valgt barn/foresatt; ingen historikkgaranti eller endringslogg.",
    }


def get_item(root, module, connection, reference):
    child_id = identifier(reference.get("child_id"))
    meeting_id = identifier(reference.get("id"))
    client = Client(root, module, connection)
    return item(client.meeting(child_id, meeting_id), child_id, client.person_id)


def list_meetings(runtime, child_id):
    runtime.config.require("vigilo")
    runtime.cleanup()
    identifier(child_id)
    with provider(runtime.config.root, "vigilo") as (module, connection):
        client = Client(runtime.config.root, module, connection)
        rows = client.invitations(child_id)
        return {
            "source": "vigilo",
            "fetched_at": now(),
            "items": [
                runtime.expose("vigilo", item(r, child_id, client.person_id))
                for r in rows
            ],
            "complete": True,
            "next_page": None,
            "coverage": "Hele invitasjonssvaret fra kilden; API-et har ingen sidemarkør eller historikkgaranti.",
            "source_content_is_untrusted": True,
        }


def _selected(row):
    return row["reply"]["meetingOccurrenceId"] if row["reply"] is not None else None


def _slot(row, occurrence_id):
    matches = [
        s
        for s in row["meetingOccurrences"]
        if s["meetingOccurrenceId"] == occurrence_id
    ]
    if len(matches) != 1:
        raise FamiliekilderError("Velg en møtetid fra den ferske invitasjonen")
    return matches[0]


def _validate_action(row, occurrence_id, accepted):
    if (
        row["meetingType"] == "singleMeetingTime"
        and len(row["meetingOccurrences"]) != 1
    ):
        raise FamiliekilderError(
            "Enkelttidsmøtet har uventet antall tider; velg i Vigilo"
        )
    current = _selected(row)
    if current is not None and current != occurrence_id:
        raise FamiliekilderError(
            "En annen møtetid er valgt; avklar den eksisterende påmeldingen først"
        )
    slot = _slot(row, occurrence_id)
    instant = datetime.now(UTC)
    deadline = row["registrationDeadline"]
    if deadline is not None and _instant(deadline) <= instant:
        raise FamiliekilderError("Påmeldingsfristen er utløpt")
    if _instant(slot["startTime"]) <= instant:
        raise FamiliekilderError("Møtet har allerede startet")
    if accepted and slot["isAvailable"] is not True:
        raise FamiliekilderError("Møtetiden er ikke tilgjengelig")


def set_participation(
    runtime, operation_id, child_id, meeting_id, occurrence_id, accepted
):
    runtime.config.require("vigilo", "participation")
    for value in (child_id, meeting_id, occurrence_id):
        identifier(value)
    if type(accepted) is not bool:
        raise FamiliekilderError("accepted må være true eller false")
    with (
        provider(runtime.config.root, "vigilo") as (module, connection),
        runtime.ledger() as ledger,
    ):
        client = Client(runtime.config.root, module, connection)
        target = {
            "child_id": child_id,
            "meeting_id": meeting_id,
            "occurrence_id": occurrence_id,
            "invitee_person_id": client.person_id,
            "accepted": accepted,
        }
        prior = ledger.reserve_operation(
            operation_id, "vigilo", "meeting_participation", target
        )
        if prior and prior["state"] == "verified":
            return ledger.operation_result(operation_id, "verified")
        reconciling = prior and prior["state"] in {"sending", "unknown"}
        if reconciling:
            receipt = json.loads(prior["receipt_json"] or "null")
            if not isinstance(receipt, dict) or "start_time" not in receipt:
                return ledger.operation_result(operation_id, "unknown")
        else:
            row = client.meeting(child_id, meeting_id)
            slot = _slot(row, occurrence_id)
            receipt = dict(
                target,
                source="vigilo",
                action="set_participation",
                started_at=now(),
                organization_id=row["organizationalUnitId"],
                start_time=slot["startTime"],
                end_time=slot["endTime"],
            )
            expected = occurrence_id if accepted else None
            if _selected(row) == expected:
                receipt.update(verified_at=now(), verification="fresh_unchanged")
                return ledger.operation_result(operation_id, "verified", receipt)
            _validate_action(row, occurrence_id, accepted)
            # Recheck after all preparation; do not overwrite an intervening reply.
            fresh = client.meeting(child_id, meeting_id)
            if fresh != row:
                raise FamiliekilderError(
                    "Møteinvitasjonen endret seg; hent den på nytt før handling"
                )
            _validate_action(fresh, occurrence_id, accepted)
            ledger.operation_result(operation_id, "sending", receipt)
            try:
                if accepted:
                    client.post(
                        "api/forms/meetings/" + meeting_id + "/replies",
                        {
                            "childPersonId": child_id,
                            "inviteePersonId": client.person_id,
                            "meetingOccurrenceId": occurrence_id,
                        },
                    )
                else:
                    client.cancel(child_id, meeting_id)
            except Exception as exc:
                from .api_transport import ApiHTTPError

                if isinstance(exc, ApiHTTPError) and exc.status_code in {401, 403, 429}:
                    return ledger.operation_result(operation_id, "unknown", receipt)
        try:
            after = client.meeting(child_id, meeting_id)
            slot = _slot(after, occurrence_id)
            exact = (
                after["organizationalUnitId"] == receipt["organization_id"]
                and slot["startTime"] == receipt["start_time"]
                and slot["endTime"] == receipt["end_time"]
                and _selected(after) == (occurrence_id if accepted else None)
            )
        except Exception:
            return ledger.operation_result(operation_id, "unknown", receipt)
        if not exact:
            return ledger.operation_result(operation_id, "unknown", receipt)
        receipt.update(
            verified_at=now(),
            verification="fresh_exact_reply",
            reply_time=(after["reply"] or {}).get("replyTime"),
        )
        return ledger.operation_result(operation_id, "verified", receipt)


def register(server, runtime):
    from .source_server import READ, WRITE, safe_tool

    def vigilo_list_meetings(child_id: str) -> dict:
        """Les ferske møteinvitasjoner, møtetider og eget svar for valgt barn. Vedlegg leses separat med referansen. Ingen historikkgaranti."""
        return list_meetings(runtime, child_id)

    def vigilo_set_participation(
        operation_id: str,
        child_id: str,
        meeting_id: str,
        occurrence_id: str,
        accepted: bool,
    ) -> dict:
        """Meld innlogget foresatt på/av valgt møtetid for eget barn etter konkret brukerbeskjed. Ingen automatisk flytting. Ukjent resultat: kontroller med samme operation_id, uten ny sending. Gjelder møter, ikke generelle påmeldingsskjemaer."""
        return set_participation(
            runtime, operation_id, child_id, meeting_id, occurrence_id, accepted
        )

    server.tool(annotations=READ)(safe_tool(vigilo_list_meetings))
    if "participation" in runtime.config.services["vigilo"]:
        server.tool(annotations=WRITE)(safe_tool(vigilo_set_participation))
