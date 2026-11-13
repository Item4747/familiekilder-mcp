"""Fresh identity and absence lookups used to prepare Vigilo actions.

Contracts come from the first-party parent's RxRestAPI and model mappers.
No read in this module marks a message/absence seen or submits an action.
"""

from datetime import date, datetime, timedelta, UTC
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

from .core import FamiliekilderError
from .live_readers import identifier, provider
from .action_ledger import now
from .source_adapter import SourceUnavailable


def _rows(value, label):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise SourceUnavailable("Vigilo returnerte ugyldig " + label)
    return value


def _name(value):
    return " ".join(
        str(value.get(key) or "") for key in ("firstName", "lastName")
    ).strip()


class ActionClient:
    def __init__(self, module, connection):
        self.module = module
        self.client, self.token, self.version = connection

    def get(self, path, query=None):
        url = urljoin(self.module.API_ROOT, path)
        if query:
            url += "?" + urlencode(query, doseq=True)
        result = self.client.get_json(
            url, bearer_token=self.token, headers={"appVersion": self.version}
        )
        if not isinstance(result, dict):
            raise SourceUnavailable("Vigilo returnerte et ugyldig objekt")
        return result

    def children(self):
        return _rows(self.get("api/children").get("items"), "barneliste")

    def post(self, path, payload):
        # POST is deliberately outside the GET-only refresh/retry wrapper.
        return self.client.transport.post_json(
            urljoin(self.module.API_ROOT, path),
            payload,
            bearer_token=self.client.token,
            headers={"appVersion": self.version},
            allow_empty=True,
        )

    def thread(self, child_id, thread_id):
        result = self.module.full_thread(
            self.client, self.token, self.version, thread_id, child_id
        )
        if result.get("threadUid") != thread_id:
            raise SourceUnavailable("Vigilo returnerte feil meldingstråd")
        if (result.get("child") or {}).get("childId") != child_id:
            raise SourceUnavailable("Meldingstråden tilhører ikke det valgte barnet")
        return result

    def participants(self, child_id, thread_id):
        result = self.get(
            "api/message-threads/" + thread_id + "/participants",
            {"childPersonId": child_id},
        )
        recipients = _rows(result.get("recipients"), "trådmottakere")
        starter = result.get("messageThreadStarter")
        if not isinstance(starter, dict):
            raise SourceUnavailable("Vigilo mangler trådstarter")
        return recipients, starter

    def select(self, child_id, organization_id=None, *, feature=None):
        identifier(child_id)
        children = [row for row in self.children() if row.get("childId") == child_id]
        if len(children) != 1:
            raise FamiliekilderError("Velg en entydig barn-ID fra denne innloggingen")
        child = children[0]
        if organization_id is None:
            return child, None
        identifier(organization_id)
        units = [
            row
            for row in _rows(child.get("organizationalUnits"), "enhetsliste")
            if row.get("id") == organization_id
        ]
        if len(units) != 1:
            raise FamiliekilderError("Enheten tilhører ikke det valgte barnet")
        unit = units[0]
        if feature and (unit.get("features") or {}).get(feature) is not True:
            raise FamiliekilderError(
                "Vigilo har ikke aktivert denne handlingen for enheten"
            )
        return child, unit

    def contacts(self, child_id, organization_id):
        data = self.get(
            "api/messages/contact-list",
            {"childId": child_id, "organizationalUnitId": organization_id},
        )
        people = {}

        def add(rows, recipient_type):
            for row in _rows(rows or [], "mottakerliste"):
                ident = identifier(row.get("id"))
                key = (recipient_type, ident)
                people[key] = {
                    "id": ident,
                    "type": recipient_type,
                    "name": _name(row),
                    "role": row.get("jobTitle") or row.get("role"),
                }

        # The official mapper uses the person id, not employeeId, and expands
        # communication groups before converting them to wire recipients.
        add(data.get("relatedEmployees"), "employee")
        add(data.get("otherEmployees"), "employee")
        add(data.get("legalGuardians"), "legalGuardian")
        groups = []
        for group in _rows(data.get("communicationGroups") or [], "kontaktgrupper"):
            members = _rows(group.get("employees") or [], "gruppemedlemmer")
            add(members, "employee")
            groups.append(
                {
                    "id": group.get("groupId"),
                    "name": group.get("name"),
                    "members": [
                        {"id": row["id"], "type": "employee"} for row in members
                    ],
                }
            )
        return list(people.values()), groups

    def absence_codes(self, organization_id):
        data = self.get("api/absencecodes", {"organizationalUnitIds": organization_id})
        result = []
        for group in _rows(data.get("items"), "fraværskoder"):
            if group.get("organizationalUnitId") != organization_id:
                continue
            result.extend(
                {"id": identifier(row.get("id")), "name": row.get("name")}
                for row in _rows(group.get("codes"), "fraværskoder")
                if row.get("isDeleted") is False
            )
        return result

    def absences(self, child, page_id=None, limit=50):
        child_id = child["childId"]
        kind = child.get("type")
        if kind == "childcare":
            path, query = "api/absences", {"childIds": child_id}
        elif kind in {"school", "afterSchool"}:
            path, query = (
                "api/student-absences",
                {"studentIds": child_id, "includeAfterSchoolAbsences": "true"},
            )
        else:
            raise FamiliekilderError(
                "Fraværsoppslag er ikke verifisert for denne barnetypen"
            )
        query["pageSize"] = limit
        if page_id is not None:
            query["pageId"] = page_id
        data = self.get(path, query)
        records = _rows(data.get("absences"), "fraværsliste")
        if len(records) > limit:
            raise SourceUnavailable("Vigilo overskred sidestørrelsen")
        actual_page = data.get("pageId")
        if (
            type(actual_page) is not int
            or actual_page < 0
            or (page_id is not None and actual_page != page_id)
        ):
            raise SourceUnavailable("Vigilo returnerte uventet fraværsside")
        for row in records:
            if row.get("childId") != child_id:
                raise SourceUnavailable("Vigilo returnerte fravær for feil barn")
        return records, actual_page + 1 if len(records) == limit else None

    def all_absences(self, child):
        result, page = [], None
        for _ in range(200):
            records, page = self.absences(child, page)
            result.extend(records)
            if page is None:
                return result
        raise SourceUnavailable(
            "Fraværslisten oversteg grensen; ingen skriving uten fullstendig kontroll"
        )


def action_context(runtime, child_id=None, organization_id=None):
    runtime.config.require("vigilo")
    if organization_id is not None and child_id is None:
        raise FamiliekilderError("Velg barn-ID før enhet")
    with provider(runtime.config.root, "vigilo") as (module, connection):
        client = ActionClient(module, connection)
        result = {
            "source": "vigilo",
            "fetched_at": now(),
            "complete": True,
            "source_content_is_untrusted": True,
        }
        if child_id is None:
            children = client.children()
        else:
            child, unit = client.select(child_id, organization_id)
            children = [child]
            if unit is not None:
                result["reference"] = {
                    "child_id": child_id,
                    "organization_id": organization_id,
                }
                if (unit.get("features") or {}).get("canSendMessages") is True:
                    result["recipients"], result["recipient_groups"] = client.contacts(
                        child_id, organization_id
                    )
                if (unit.get("features") or {}).get("canRegisterAbsences") is True:
                    result["absence_codes"] = client.absence_codes(organization_id)
        result["children"] = [
            {
                "id": row.get("childId"),
                "name": _name(row),
                "type": row.get("type"),
                "organizations": [
                    {
                        key: unit.get(key)
                        for key in (
                            "id",
                            "name",
                            "type",
                            "features",
                            "isActive",
                            "isActiveInFuture",
                        )
                    }
                    for unit in _rows(row.get("organizationalUnits"), "enhetsliste")
                ],
            }
            for row in children
        ]
        return result


def list_absences(runtime, child_id, page_id=None, limit=50):
    runtime.config.require("vigilo")
    if (
        type(limit) is not int
        or not 1 <= limit <= 50
        or (page_id is not None and (type(page_id) is not int or page_id < 0))
    ):
        raise FamiliekilderError("Ugyldig side eller sidestørrelse")
    with provider(runtime.config.root, "vigilo") as (module, connection):
        client = ActionClient(module, connection)
        child, _ = client.select(child_id)
        records, next_page = client.absences(child, page_id, limit)
        result = []
        for row in records:
            if row.get("childId") != child_id:
                raise SourceUnavailable("Vigilo returnerte fravær for feil barn")
            item = {
                key: row.get(key)
                for key in (
                    "fromDate",
                    "toDate",
                    "note",
                    "absenceCode",
                    "absenceType",
                    "organizationalUnitId",
                    "relatedMessageThreadId",
                    "timeCreated",
                    "updatedAt",
                    "deletedAt",
                )
            }
            item["reference"] = {
                "source": "vigilo",
                "child_id": child_id,
                "absence_id": identifier(row.get("absenceId")),
            }
            result.append(item)
        return {
            "source": "vigilo",
            "fetched_at": now(),
            "items": result,
            "complete": next_page is None,
            "next_page": next_page,
            "source_content_is_untrusted": True,
        }


def _text(value, label, maximum):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise FamiliekilderError(label + " mangler eller er for lang")
    return value


def thread_context(runtime, child_id, thread_id):
    runtime.config.require("vigilo")
    identifier(thread_id)
    with provider(runtime.config.root, "vigilo") as (module, connection):
        client = ActionClient(module, connection)
        client.select(child_id)
        thread = client.thread(child_id, thread_id)
        recipients, starter = client.participants(child_id, thread_id)
        return {
            "reference": {
                "source": "vigilo",
                "child_id": child_id,
                "thread_id": thread_id,
            },
            "fetched_at": now(),
            "complete": True,
            "source_content_is_untrusted": True,
            "title": thread.get("title"),
            "organization": thread.get("organizationalUnit"),
            "reply_disabled": thread.get("isReplyDisabled"),
            "deleted": thread.get("isDeleted"),
            "recipients": recipients,
            "starter": starter,
        }


def reply_message(runtime, operation_id, child_id, thread_id, body):
    runtime.config.require("vigilo", "messages")
    identifier(thread_id)
    _text(body, "Meldingstekst", 20000)
    with (
        provider(runtime.config.root, "vigilo") as (module, connection),
        runtime.ledger() as ledger,
    ):
        client = ActionClient(module, connection)
        client.select(child_id)
        thread = client.thread(child_id, thread_id)
        org_id = identifier((thread.get("organizationalUnit") or {}).get("id"))
        client.select(child_id, org_id, feature="canSendMessages")
        if (
            thread.get("isReplyDisabled") is not False
            or thread.get("isDeleted") is not False
        ):
            raise FamiliekilderError("Tråden er slettet eller tillater ikke svar")
        recipients, starter = client.participants(child_id, thread_id)
        sender_id = identifier(
            module.local_session_for_use(runtime.config.root).get("person_id")
        )
        if sender_id not in {row.get("id") for row in [*recipients, starter]}:
            raise FamiliekilderError("Innlogget person er ikke deltaker i tråden")

        def observe():
            current = client.thread(child_id, thread_id)
            return [
                identifier(row.get("id"))
                for row in _rows(current.get("messages"), "meldinger")
                if row.get("body") == body
                and (row.get("sender") or {}).get("id") == sender_id
                and row.get("isDeleted") is not True
                and not row.get("attachments")
            ]

        from .live_create import perform

        return perform(
            ledger,
            operation_id,
            "vigilo",
            "reply_message",
            {"child_id": child_id, "thread_id": thread_id, "body": body},
            {"child_id": child_id, "thread_id": thread_id, "organization_id": org_id},
            observe,
            lambda: client.post(
                "api/messages/" + thread_id + "/reply",
                {"body": body, "attachments": []},
            ),
        )


def _recipients(client, child_id, organization_id, recipients):
    rows = _rows(recipients, "valgte mottakere")
    if not rows or len(rows) > 100:
        raise FamiliekilderError("Velg 1–100 konkrete mottakere fra action_context")
    contacts, _ = client.contacts(child_id, organization_id)
    available = {(row["type"], row["id"]) for row in contacts}
    selected = set()
    for row in rows:
        if set(row) != {"id", "type"}:
            raise FamiliekilderError("Mottakere krever bare id og type")
        ident = identifier(row["id"])
        if (
            row["type"] not in {"employee", "legalGuardian"}
            or (row["type"], ident) not in available
        ):
            raise FamiliekilderError(
                "Mottakeren finnes ikke i barnets ferske kontaktliste"
            )
        selected.add((row["type"], ident))
    if len({ident for _, ident in selected}) != len(selected):
        raise FamiliekilderError("Samme person er valgt med flere mottakertyper")
    return [{"type": kind, "externalId": ident} for kind, ident in sorted(selected)]


def send_message(
    runtime, operation_id, child_id, organization_id, recipients, title, body
):
    runtime.config.require("vigilo", "messages")
    _text(title, "Tittel", 500)
    _text(body, "Meldingstekst", 20000)
    with (
        provider(runtime.config.root, "vigilo") as (module, connection),
        runtime.ledger() as ledger,
    ):
        client = ActionClient(module, connection)
        _, unit = client.select(child_id, organization_id, feature="canSendMessages")
        selected = _recipients(client, child_id, organization_id, recipients)
        if unit.get("type") == "school" and not any(
            r["type"] == "employee" for r in selected
        ):
            raise FamiliekilderError(
                "Skolemelding i Vigilo krever minst én ansatt som mottaker"
            )
        sender_id = identifier(
            module.local_session_for_use(runtime.config.root).get("person_id")
        )
        # CreateNewMessageThreadUseCase prepends the child as a client recipient.
        payload = {
            "title": title,
            "body": body,
            "attachments": [],
            "organizationalUnitId": organization_id,
            "recipients": [{"type": "client", "externalId": child_id}, *selected],
        }
        wanted = {r["externalId"] for r in selected}

        def observe():
            matches = []
            # Explicit bounded readback. Older unresolved actions stay unknown;
            # no archive is consulted and no second POST is attempted.
            cutoff = datetime.now(UTC) - timedelta(days=14)
            for item in module.thread_pages(
                client.client, client.token, client.version, child_id, cutoff
            ):
                if item.get("title") != title:
                    continue
                thread_id = identifier(item.get("threadUid"))
                thread = client.thread(child_id, thread_id)
                if (
                    thread.get("title") != title
                    or (thread.get("organizationalUnit") or {}).get("id")
                    != organization_id
                    or thread.get("isDeleted") is True
                ):
                    continue
                messages = _rows(thread.get("messages"), "meldinger")
                if (
                    not messages
                    or messages[0].get("body") != body
                    or (messages[0].get("sender") or {}).get("id") != sender_id
                    or messages[0].get("attachments")
                ):
                    continue
                actual, starter = client.participants(child_id, thread_id)
                ids = {row.get("id") for row in actual}
                # Read models call guardian recipients family. Compare stable
                # person IDs, allowing only the sender and selected child extra.
                if (
                    ids - {sender_id, child_id} != wanted - {sender_id, child_id}
                    or starter.get("id") != sender_id
                ):
                    continue
                matches.append(thread_id)
            return matches

        from .live_create import perform

        return perform(
            ledger,
            operation_id,
            "vigilo",
            "send_message",
            payload,
            {
                "child_id": child_id,
                "organization_id": organization_id,
                "recipient_ids": sorted(wanted),
                "readback_window_days": 14,
            },
            observe,
            lambda: client.post("api/messages", payload),
        )


def _date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise FamiliekilderError("Fraværsdato må være YYYY-MM-DD") from None


def _source_date(value):
    # Vigilo's app parses server date-times as UTC then displays local dates.
    # These are Norwegian school/childcare dates, independent of the host zone.
    try:
        parsed = datetime.fromisoformat(value)
        return (
            parsed.replace(tzinfo=parsed.tzinfo or UTC)
            .astimezone(ZoneInfo("Europe/Oslo"))
            .date()
        )
    except (ValueError, TypeError):
        raise SourceUnavailable("Vigilo returnerte ugyldig fraværsdato") from None


def register_absence(
    runtime,
    operation_id,
    child_id,
    organization_id,
    from_date,
    to_date,
    note="",
    absence_code_id=None,
    recipients=None,
    title=None,
):
    runtime.config.require("vigilo", "absence")
    start, end = _date(from_date), _date(to_date)
    if end < start:
        raise FamiliekilderError("Sluttdato kan ikke være før startdato")
    if not isinstance(note, str) or len(note) > 20000 or "\x00" in note:
        raise FamiliekilderError("Ugyldig fraværsmerknad")
    with (
        provider(runtime.config.root, "vigilo") as (module, connection),
        runtime.ledger() as ledger,
    ):
        client = ActionClient(module, connection)
        child, unit = client.select(
            child_id, organization_id, feature="canRegisterAbsences"
        )
        kind = unit.get("type")
        paths = {
            "childcare": "api/absences",
            "school": "api/student-absences",
            "afterSchool": "api/after-school-absences",
        }
        if kind not in paths:
            raise FamiliekilderError("Fravær er ikke verifisert for denne enhetstypen")
        sender_id = identifier(
            module.local_session_for_use(runtime.config.root).get("person_id")
        )
        payload = {
            "childId": child_id,
            "organizationalUnitId": organization_id,
            "fromDate": start.strftime("%d.%m.%Y"),
            "toDate": end.strftime("%d.%m.%Y"),
            "note": note,
        }
        if kind == "childcare":
            code = identifier(absence_code_id)
            if code not in {r["id"] for r in client.absence_codes(organization_id)}:
                raise FamiliekilderError("Velg en aktiv fraværskode for enheten")
            payload["absenceCodeId"] = code
        elif absence_code_id is not None:
            raise FamiliekilderError(
                "Denne fraværstypen bruker ikke valgt fraværskode i foreldreappen"
            )
        if kind == "school":
            _text(note, "Fraværsmerknad", 20000)
            _text(title, "Fraværstittel", 500)
            selected = _recipients(client, child_id, organization_id, recipients)
            if not any(r["type"] == "employee" for r in selected):
                raise FamiliekilderError("Skolefravær krever en ansatt som mottaker")
            payload.update(title=title, recipients=selected, attachments=[])
        elif recipients or title is not None:
            raise FamiliekilderError("Mottakere og tittel gjelder bare skolefravær")

        def relevant():
            return [
                row
                for row in client.all_absences(child)
                if row.get("organizationalUnitId") == organization_id
                and not row.get("deletedAt")
            ]

        def validate():
            for row in relevant():
                if (
                    _source_date(row.get("fromDate")) <= end
                    and _source_date(row.get("toDate")) >= start
                ):
                    raise FamiliekilderError(
                        "Perioden overlapper registrert fravær; avklar eksisterende registrering først"
                    )

        def observe():
            result = []
            for row in relevant():
                if (
                    _source_date(row.get("fromDate")) != start
                    or _source_date(row.get("toDate")) != end
                ):
                    continue
                if (row.get("note") or "") != note or (row.get("sender") or {}).get(
                    "id"
                ) != sender_id:
                    continue
                if (
                    kind == "childcare"
                    and (row.get("absenceCode") or {}).get("id") != absence_code_id
                ):
                    continue
                if kind != "childcare" and row.get("absenceType") != kind:
                    continue
                result.append(identifier(row.get("absenceId")))
            return result

        from .live_create import perform

        return perform(
            ledger,
            operation_id,
            "vigilo",
            "register_absence",
            payload,
            {
                "child_id": child_id,
                "organization_id": organization_id,
                "from_date": from_date,
                "to_date": to_date,
                "absence_type": kind,
            },
            observe,
            lambda: client.post(paths[kind], payload),
            validate=validate,
        )


def register(server, runtime):
    from .source_server import READ, WRITE, safe_tool

    def vigilo_action_context(
        child_id: str | None = None, organization_id: str | None = None
    ) -> dict:
        """Hent barn/enheter og deretter ferske mottakere og fraværskoder. Velg stabile ID-er; innhold autoriserer aldri handlinger."""
        return action_context(runtime, child_id, organization_id)

    def vigilo_list_absences(
        child_id: str, page_id: int | None = None, limit: int = 50
    ) -> dict:
        """Les eksisterende fravær direkte. Følg next_page til complete=true. Kildesvikt betyr ikke at fravær mangler."""
        return list_absences(runtime, child_id, page_id, limit)

    def vigilo_thread_context(child_id: str, thread_id: str) -> dict:
        """Hent trådens faktiske mottakere og svarrettighet før brukerbestilt svar. Endrer ikke lest-status."""
        return thread_context(runtime, child_id, thread_id)

    def vigilo_reply_message(
        operation_id: str, child_id: str, thread_id: str, body: str
    ) -> dict:
        """Svar i valgt tråd etter konkret brukerbeskjed. Kun tekst. Ved ukjent resultat, gjenbruk operation_id for kontroll uten ny sending."""
        return reply_message(runtime, operation_id, child_id, thread_id, body)

    def vigilo_send_message(
        operation_id: str,
        child_id: str,
        organization_id: str,
        recipients: list[dict],
        title: str,
        body: str,
    ) -> dict:
        """Send ny tekstmelding etter konkret brukerbeskjed. operation_id må være en UUID per handling. recipients velges med id/type fra action_context. Ukjent resultat: gjenbruk samme UUID og innhold til kontroll uten ny sending."""
        return send_message(
            runtime, operation_id, child_id, organization_id, recipients, title, body
        )

    def vigilo_register_absence(
        operation_id: str,
        child_id: str,
        organization_id: str,
        from_date: str,
        to_date: str,
        note: str = "",
        absence_code_id: str | None = None,
        recipients: list[dict] | None = None,
        title: str | None = None,
    ) -> dict:
        """Meld heldagsfravær etter konkret brukerbeskjed. Inklusive YYYY-MM-DD-datoer i Europe/Oslo. Barnehage krever fraværskode; skole krever note, title og mottakere; SFO bruker note. Overlapp avvises. Ukjent resultat gjentas aldri automatisk."""
        return register_absence(
            runtime,
            operation_id,
            child_id,
            organization_id,
            from_date,
            to_date,
            note,
            absence_code_id,
            recipients,
            title,
        )

    server.add_tool(safe_tool(vigilo_action_context), annotations=READ)
    server.add_tool(safe_tool(vigilo_list_absences), annotations=READ)
    server.add_tool(safe_tool(vigilo_thread_context), annotations=READ)
    if "messages" in runtime.config.services["vigilo"]:
        server.add_tool(safe_tool(vigilo_reply_message), annotations=WRITE)
        server.add_tool(safe_tool(vigilo_send_message), annotations=WRITE)
    if "absence" in runtime.config.services["vigilo"]:
        server.add_tool(safe_tool(vigilo_register_absence), annotations=WRITE)
