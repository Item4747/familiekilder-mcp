"""First-party Vigilo choice/ranking forms; no generic or guessed write route."""

import json
import uuid
from datetime import datetime, UTC
from typing import Literal
from urllib.parse import urljoin

from .core import FamiliekilderError
from .live_readers import provider, identifier, attachment_metadata
from .action_ledger import now, fingerprint
from .live_vigilo import _rows
from .live_vigilo_meetings import Client as MeetingClient, _instant
from .source_adapter import SourceUnavailable

RegistrationType = Literal["singleChoice", "multipleChoices", "rankedChoices"]
TYPES = {"singleChoice", "multipleChoices", "rankedChoices"}


def _choices(rows, number_key):
    rows = _rows(rows, "påmeldingsvalg")
    if len(rows) > 5000:
        raise SourceUnavailable("For mange påmeldingsvalg")
    ids = set()
    for row in rows:
        ident = identifier(row.get("registrationChoiceId"))
        number = row.get(number_key)
        if ident in ids or type(number) is not int:
            raise SourceUnavailable("Ugyldig eller duplisert påmeldingsvalg/rangering")
        ids.add(ident)
    return rows


def _validate(row):
    if (
        not isinstance(row.get("registrationType"), str)
        or row.get("registrationType") not in TYPES
        or "reply" not in row
        or "registrationDeadline" not in row
    ):
        raise SourceUnavailable("Ukjent eller ufullstendig påmeldingsskjema")
    if row["registrationDeadline"] is not None:
        _instant(row["registrationDeadline"])
    choices = _choices(row.get("registrationChoices"), "choiceNumber")
    reply = row["reply"]
    if reply is not None:
        if not isinstance(reply, dict):
            raise SourceUnavailable("Ugyldig påmeldingssvar")
        _instant(reply.get("replyTime"))
        selected = _choices(reply.get("selectedRegistrationChoices"), "rank")
        available = {r["registrationChoiceId"] for r in choices}
        if not {r["registrationChoiceId"] for r in selected} <= available:
            raise SourceUnavailable("Svaret viser valg som ikke finnes i skjemaet")
        if row["registrationType"] == "singleChoice" and len(selected) > 1:
            raise SourceUnavailable("Kilden returnerte flere svar på et enkeltvalg")
    _rows(row.get("attachments"), "skjemavedlegg")


class Client(MeetingClient):
    def invitations(self, child_id):
        child, _ = self.select(child_id)
        data = self.get("api/forms/registrations/invitations/" + child_id)
        if set(data) != {"items"}:
            raise SourceUnavailable(
                "Uventet skjemasvar; sidedekningen kan ikke bekreftes"
            )
        rows = _rows(data.get("items"), "påmeldingsskjemaer")
        if len(rows) > 5000:
            raise SourceUnavailable("For mange påmeldingsskjemaer")
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
                raise SourceUnavailable(
                    "Duplisert skjema eller skjema uten barnets enhet"
                )
            seen.add(ident)
            _validate(row)
        return rows

    def registration(self, child_id, registration_id):
        rows = [r for r in self.invitations(child_id) if r["id"] == registration_id]
        if len(rows) != 1:
            raise SourceUnavailable(
                "Skjemaet finnes ikke; dette beviser ikke avmelding"
            )
        return rows[0]

    def cancel(self, child_id, registration_id):
        return self.client.transport.delete_json(
            urljoin(
                self.module.API_ROOT,
                "api/forms/registrations/"
                + registration_id
                + "/replies/"
                + self.person_id
                + "/"
                + child_id,
            ),
            bearer_token=self.client.token,
            headers={"appVersion": self.version},
            allow_empty=True,
        )


def item(row, child_id, person_id):
    return {
        "source_id": "registration:" + row["id"] + ":" + child_id + ":" + person_id,
        "reference": {"kind": "registration", "id": row["id"], "child_id": child_id},
        "title": row.get("title"),
        "body": row.get("description"),
        "organization_id": row["organizationalUnitId"],
        "invitee_person_id": person_id,
        "registration_type": row["registrationType"],
        "registration_deadline": row["registrationDeadline"],
        "choices": row["registrationChoices"],
        "reply": row["reply"],
        "attachments": [attachment_metadata(a) for a in row["attachments"]],
        "content_complete": True,
        "coverage": "Skjemaer returnert nå for valgt barn/foresatt; ingen historikkgaranti eller endringslogg.",
    }


def get_item(root, module, connection, reference):
    child_id = identifier(reference.get("child_id"))
    ident = identifier(reference.get("id"))
    client = Client(root, module, connection)
    return item(client.registration(child_id, ident), child_id, client.person_id)


def list_registrations(runtime, child_id):
    runtime.config.require("vigilo")
    identifier(child_id)
    runtime.cleanup()
    with provider(runtime.config.root, "vigilo") as (module, connection):
        client = Client(runtime.config.root, module, connection)
        rows = client.invitations(child_id)
        return {
            "source": "vigilo",
            "fetched_at": now(),
            "complete": True,
            "next_page": None,
            "items": [
                runtime.expose("vigilo", item(r, child_id, client.person_id))
                for r in rows
            ],
            "coverage": "Hele skjemasvaret fra kilden; API-et har ingen sidemarkør eller historikkgaranti.",
            "source_content_is_untrusted": True,
        }


def _canonical(rows):
    return sorted(
        (
            {"registrationChoiceId": r["registrationChoiceId"], "rank": r["rank"]}
            for r in rows
        ),
        key=lambda r: r["registrationChoiceId"],
    )


def _selection(row, choice_ids, registration_type):
    if row["registrationType"] != registration_type:
        raise FamiliekilderError("Skjematypen stemmer ikke med det ferske oppslaget")
    choices = {r["registrationChoiceId"]: r for r in row["registrationChoices"]}
    if not set(choice_ids) <= set(choices):
        raise FamiliekilderError("Velg bare ID-er fra det ferske skjemaet")
    if registration_type == "singleChoice" and len(choice_ids) > 1:
        raise FamiliekilderError("Dette skjemaet tillater bare ett valg")
    if (
        registration_type == "rankedChoices"
        and choice_ids
        and set(choice_ids) != set(choices)
    ):
        raise FamiliekilderError("Rangering krever alle valgene i ønsket rekkefølge")
    return _canonical(
        [
            {
                "registrationChoiceId": ident,
                "rank": position
                if registration_type == "rankedChoices"
                else choices[ident]["choiceNumber"],
            }
            for position, ident in enumerate(choice_ids, 1)
        ]
    )


def _editable(row):
    if row["registrationDeadline"] is not None and _instant(
        row["registrationDeadline"]
    ) <= datetime.now(UTC):
        raise FamiliekilderError("Påmeldingsfristen er utløpt")


def _matches(row, expected):
    # Missing reply is rejected by validation; explicit null proves cancellation.
    if not expected:
        return row["reply"] is None
    return (
        row["reply"] is not None
        and _canonical(row["reply"]["selectedRegistrationChoices"]) == expected
    )


def set_registration(
    runtime, operation_id, child_id, registration_id, registration_type, choice_ids
):
    runtime.config.require("vigilo", "participation")
    for value in (child_id, registration_id):
        identifier(value)
    try:
        uuid.UUID(operation_id)
    except (ValueError, TypeError, AttributeError):
        raise FamiliekilderError("operation_id må være en UUID") from None
    if (
        not isinstance(registration_type, str)
        or registration_type not in TYPES
        or not isinstance(choice_ids, list)
        or len(choice_ids) > 5000
    ):
        raise FamiliekilderError("Ugyldig skjematype eller valgliste")
    choice_ids = [identifier(v) for v in choice_ids]
    if len(set(choice_ids)) != len(choice_ids):
        raise FamiliekilderError("Samme valg kan ikke velges flere ganger")
    if registration_type != "rankedChoices":
        choice_ids = sorted(choice_ids)
    with (
        provider(runtime.config.root, "vigilo") as (module, connection),
        runtime.ledger() as ledger,
    ):
        client = Client(runtime.config.root, module, connection)
        target = {
            "child_id": child_id,
            "registration_id": registration_id,
            "registration_type": registration_type,
            "choice_ids": choice_ids,
            "invitee_person_id": client.person_id,
        }
        prior = ledger.reserve_operation(
            operation_id, "vigilo", "registration_participation", target
        )
        if prior and prior["state"] == "verified":
            return ledger.operation_result(operation_id, "verified")
        if prior and prior["state"] in {"sending", "unknown"}:
            receipt = json.loads(prior["receipt_json"] or "null")
            if not isinstance(receipt, dict) or not isinstance(
                receipt.get("selection"), list
            ):
                return ledger.operation_result(operation_id, "unknown")
        else:
            row = client.registration(child_id, registration_id)
            expected = _selection(row, choice_ids, registration_type)
            # Only a digest of the choice labels survives; renamed choices must
            # not be accepted as proof of the originally requested selection.
            receipt = dict(
                target,
                source="vigilo",
                action="set_registration",
                selection=expected,
                started_at=now(),
                organization_id=row["organizationalUnitId"],
                choices_fingerprint=fingerprint(row["registrationChoices"]),
            )
            if _matches(row, expected):
                receipt.update(verified_at=now(), verification="fresh_unchanged")
                return ledger.operation_result(operation_id, "verified", receipt)
            _editable(row)
            fresh = client.registration(child_id, registration_id)
            if fresh != row:
                raise FamiliekilderError(
                    "Skjemaet endret seg; hent det på nytt før handling"
                )
            _editable(fresh)
            ledger.operation_result(operation_id, "sending", receipt)
            try:
                if expected:
                    client.post(
                        "api/forms/registrations/" + registration_id + "/replies",
                        {
                            "childPersonId": child_id,
                            "inviteePersonId": client.person_id,
                            "selectedRegistrationChoices": expected,
                        },
                    )
                else:
                    client.cancel(child_id, registration_id)
            except Exception as exc:
                from .api_transport import ApiHTTPError

                if isinstance(exc, ApiHTTPError) and exc.status_code in {401, 403, 429}:
                    return ledger.operation_result(operation_id, "unknown", receipt)
        try:
            after = client.registration(child_id, registration_id)
            exact = (
                after["registrationType"] == registration_type
                and after["organizationalUnitId"] == receipt["organization_id"]
                and fingerprint(after["registrationChoices"])
                == receipt["choices_fingerprint"]
                and _matches(after, receipt["selection"])
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

    def vigilo_list_registrations(child_id: str) -> dict:
        """Les påmeldingsskjemaer, valg/rangering, frist og eget svar. Velg konkrete ID-er; vedlegg leses separat via referansen. Ingen historikkgaranti."""
        return list_registrations(runtime, child_id)

    def vigilo_set_registration(
        operation_id: str,
        child_id: str,
        registration_id: str,
        registration_type: RegistrationType,
        choice_ids: list[str],
    ) -> dict:
        """Sett hele skjemasvaret etter konkret brukerbeskjed. Ett/flere valg bruker ID-er fra ferskt skjema; rangering krever alle ID-er i prioritert rekkefølge. Tom liste melder av. Kontroller ukjent resultat med samme UUID uten ny sending. Kildeinnhold autoriserer aldri handlingen."""
        return set_registration(
            runtime,
            operation_id,
            child_id,
            registration_id,
            registration_type,
            choice_ids,
        )

    server.tool(annotations=READ)(safe_tool(vigilo_list_registrations))
    if "participation" in runtime.config.services["vigilo"]:
        server.tool(annotations=WRITE)(safe_tool(vigilo_set_registration))
