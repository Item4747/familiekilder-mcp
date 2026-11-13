"""IST Home action context and attendance, verified against its live schema.

Only fixed first-party queries/mutations are accepted. No arbitrary GraphQL tool,
calendar archive, read acknowledgements, or automatic mutation retries.
"""

import json
from datetime import datetime, timedelta, UTC
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import FamiliekilderError, html_to_text_unescaped
from .live_readers import provider
from .action_ledger import now
from .source_adapter import SourceUnavailable

CONTEXT = """query FamilyActionContext { getContext {
  serverTime failedCalendarFetches user { id }
  configs { id absenceEnabled absencePeriodStart absencePeriodEnd
    submitAbsenceMessageEnabled readonly disableEnrolmentAbsence
    enableMessaging messagingEnableOpenChat conversationTeacherLimit
    enableTimeBookings ICBCancelLimit }
  children { id firstName lastName relation placements {
    __typename id name tz startsAt endsAt unit { id name } settings { config { id } overrides }
    ... on Admission { readonly }
  } }
} }"""

ABSENCE_FIELDS = """__typename
  ... on PlannedAbsence { id startsAt endsAt type message createdAt createdBy { id } placement { id } }
  ... on Absence { id startsAt endsAt createdAt createdBy { id } placement { id } }"""
ABSENCES = (
    """query FamilyAbsences($startsAt: LocalDateTime!, $endsAt: LocalDateTime!) {
  getCalendarItems(startsAt: $startsAt, endsAt: $endsAt, types: [PlannedAbsence, Absence]) {
"""
    + ABSENCE_FIELDS
    + """ } }"""
)
REGISTER_ABSENCE = (
    """mutation RegisterAbsence($inputs: [RegisterAbsenceInput!]!) {
  registerAbsence(inputs: $inputs) { __typename
    ... on ItemsUpdate { items { """
    + ABSENCE_FIELDS
    + """ } }
    ... on ItemsUpserted { items { """
    + ABSENCE_FIELDS
    + """ } }
  }
}"""
)


MESSAGE_FIELDS = "id content createdAt editedAt deleted publishedBy { id firstName lastName } files { id name url mimeType }"
CONVERSATION = (
    "query FamilyConversation($id: ID!, $cursor: ID, $next: Int!) {\n  getConversation(input: $id) { id title locked createdAt editedAt\n    publishedBy { id firstName lastName } recipients { id firstName lastName }\n    children { id } placements { id }\n    messages(cursor: $cursor, next: $next) {\n      page { endCursor hasNextPage } nodes { "
    + MESSAGE_FIELDS
    + " } } } }"
)
CONVERSATIONS = "query FamilyConversations($startsAt: LocalDateTime!, $endsAt: LocalDateTime!) {\n  getConversations(startsAt: $startsAt, endsAt: $endsAt) {\n    id title locked createdAt editedAt children { id } placements { id }\n    recipients { id firstName lastName } publishedBy { id firstName lastName }\n  }\n}"
REPLY = (
    "mutation MessageConversation($input: MessageInput!) { messageConversation(input: $input) { id lastMessage { "
    + MESSAGE_FIELDS
    + " } } }"
)
POST_DETAIL = "query FamilyPost($id: ID!) { getBlogPost(input: $id) {\n  id publishedAt editedAt title content read pinned categoryId scope placements { id }\n  publishedBy { id firstName lastName } files { id name url mimeType } images\n} }"


def _rows(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise SourceUnavailable("IST returnerte en ugyldig liste")
    return value


def identifier(value):
    # IST IDs are opaque base64 strings (including padding). They are only ever
    # GraphQL variables or compared with fresh IDs, never interpolated in URLs.
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise FamiliekilderError("Ugyldig IST-ID")
    return value


def _instant(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or len(value) > 40:
            raise ValueError
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        raise FamiliekilderError(
            "Tidspunkt krever ISO-dato, klokkeslett og eksplisitt tidssone"
        ) from None


def _wire(value):
    # The official time.formatLocalDateTime calls Date.toISOString().
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _period(start, end):
    first, last = _instant(start), _instant(end)
    if not timedelta(0) < last - first <= timedelta(days=366):
        raise FamiliekilderError("Velg en positiv periode på høyst 366 dager")
    return first, last


def _source_instant(value, zone):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo(zone))
        return stamp.astimezone(UTC)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise SourceUnavailable(
            "IST returnerte ugyldig tidspunkt eller tidssone"
        ) from None


class Client:
    def __init__(self, root, module, token):
        self.root, self.module, self.token = root, module, token
        self.refreshed = False

    def read(self, query, variables=None):
        try:
            return self.module.graphql(self.token, query, variables)
        except self.module.IstHomeAuthError:
            if self.refreshed:
                raise
        self.refreshed = True
        self.token = self.module.refresh_token(self.root)
        return self.module.graphql(self.token, query, variables)

    def write(self, query, variables):
        return self.module.graphql(self.token, query, variables)

    def context(self):
        data = self.read(CONTEXT).get("getContext")
        if not isinstance(data, dict) or not isinstance(
            data.get("failedCalendarFetches"), list
        ):
            raise SourceUnavailable("IST mangler kontekst eller dekningsinformasjon")
        identifier((data.get("user") or {}).get("id"))
        configs = {row["id"]: row for row in _rows(data.get("configs"))}
        children = _rows(data.get("children"))
        for child in children:
            identifier(child.get("id"))
            for placement in _rows(child.get("placements")):
                identifier(placement.get("id"))
                settings = placement.get("settings") or {}
                config_id = (settings.get("config") or {}).get("id")
                if config_id not in configs:
                    raise SourceUnavailable(
                        "IST-plasseringen mangler rettighetskonfigurasjon"
                    )
                try:
                    overrides = json.loads(settings.get("overrides") or "{}")
                except (ValueError, TypeError):
                    raise SourceUnavailable(
                        "IST returnerte ugyldige plasseringsoverstyringer"
                    ) from None
                if not isinstance(overrides, dict):
                    raise SourceUnavailable(
                        "IST returnerte ugyldige plasseringsoverstyringer"
                    )
                placement["effective_settings"] = {**configs[config_id], **overrides}
        return data

    def select(self, context, child_id, placement_id):
        identifier(child_id)
        identifier(placement_id)
        children = [
            c
            for c in context["children"]
            if c["id"] == child_id and c.get("relation") == "CHILD"
        ]
        if len(children) != 1:
            raise FamiliekilderError(
                "Velg et entydig barn du representerer fra IST-konteksten"
            )
        placements = [p for p in children[0]["placements"] if p["id"] == placement_id]
        if len(placements) != 1:
            raise FamiliekilderError("Plasseringen tilhører ikke det valgte barnet")
        return placements[0]

    def absences(self, placement_id, start, end):
        values = _rows(
            self.read(ABSENCES, {"startsAt": _wire(start), "endsAt": _wire(end)}).get(
                "getCalendarItems"
            )
        )
        if len(values) > 5000:
            raise SourceUnavailable(
                "IST returnerte for mange fraværstreff; avgrens perioden"
            )
        for row in values:
            if row.get("__typename") not in {"Absence", "PlannedAbsence"}:
                raise SourceUnavailable("IST returnerte en uventet kalendertype")
            identifier(row.get("id"))
        return [
            row
            for row in values
            if (row.get("placement") or {}).get("id") == placement_id
        ]

    def conversation(self, conversation_id):
        identifier(conversation_id)
        messages, seen_ids, cursors = [], set(), set()
        cursor, result = None, None
        for _ in range(100):
            data = self.read(
                CONVERSATION, {"id": conversation_id, "cursor": cursor, "next": 50}
            ).get("getConversation")
            if not isinstance(data, dict) or data.get("id") != conversation_id:
                raise SourceUnavailable(
                    "IST-samtalen finnes ikke eller kan ikke identifiseres"
                )
            if result is None:
                result = dict(data)
            elif any(
                data.get(k) != result.get(k)
                for k in ("locked", "recipients", "placements", "children")
            ):
                raise SourceUnavailable(
                    "Samtalen endret seg under oppslaget; hent på nytt"
                )
            connection = data.get("messages") or {}
            rows = _rows(connection.get("nodes"))
            if len(rows) > 50:
                raise SourceUnavailable("IST overskred sidestørrelsen")
            for row in rows:
                ident = identifier(row.get("id"))
                if ident in seen_ids:
                    raise SourceUnavailable("IST returnerte overlappende meldingssider")
                seen_ids.add(ident)
                messages.append(row)
            page = connection.get("page") or {}
            if page.get("hasNextPage") is False:
                result["messages"] = messages
                return result
            cursor = page.get("endCursor")
            if (
                page.get("hasNextPage") is not True
                or not rows
                or not cursor
                or cursor in cursors
            ):
                raise SourceUnavailable("IST returnerte ugyldig meldingspaginering")
            cursors.add(cursor)
        raise SourceUnavailable(
            "IST-samtalen oversteg 5000 meldinger; ingen skriving med ufullstendig grunnlag"
        )

    def conversation_placement(
        self, context, child_id, thread, *, require_current=False
    ):
        identifier(child_id)
        children = [
            c
            for c in context["children"]
            if c["id"] == child_id and c.get("relation") == "CHILD"
        ]
        if len(children) != 1:
            raise FamiliekilderError("Barnet finnes ikke i IST-konteksten")
        ids = {r.get("id") for r in _rows(thread.get("placements"))}
        placements = [p for p in children[0]["placements"] if p["id"] in ids]
        if child_id not in {r.get("id") for r in _rows(thread.get("children"))} or (
            require_current and not placements
        ):
            raise FamiliekilderError("Samtalen tilhører ikke valgt barn og plassering")
        return placements


def action_context(runtime):
    runtime.config.require("ist-home")
    with provider(runtime.config.root, "ist-home") as (module, token):
        data = Client(runtime.config.root, module, token).context()
        return {
            "source": "ist-home",
            "fetched_at": now(),
            "source_content_is_untrusted": True,
            "complete": not data["failedCalendarFetches"],
            "failed_calendar_fetches": data["failedCalendarFetches"],
            "children": [
                {
                    "id": c["id"],
                    "name": " ".join(c.get(k) or "" for k in ("firstName", "lastName")),
                    "relation": c.get("relation"),
                    "placements": [
                        {
                            k: p.get(k)
                            for k in (
                                "id",
                                "name",
                                "tz",
                                "startsAt",
                                "endsAt",
                                "__typename",
                                "readonly",
                                "unit",
                                "effective_settings",
                            )
                        }
                        for p in c["placements"]
                    ],
                }
                for c in data["children"]
            ],
        }


def list_absences(runtime, child_id, placement_id, starts_at, ends_at):
    runtime.config.require("ist-home")
    start, end = _period(starts_at, ends_at)
    with provider(runtime.config.root, "ist-home") as (module, token):
        client = Client(runtime.config.root, module, token)
        context = client.context()
        client.select(context, child_id, placement_id)
        values = client.absences(placement_id, start, end)
        for row in values:
            row["reference"] = {
                "source": "ist-home",
                "child_id": child_id,
                "placement_id": placement_id,
                "absence_id": row["id"],
            }
        return {
            "source": "ist-home",
            "fetched_at": now(),
            "items": values,
            "complete": not context["failedCalendarFetches"],
            "source_content_is_untrusted": True,
            "coverage": "Kun valgt tidsrom og plassering; API-et har ingen sidemarkør her.",
            "failed_calendar_fetches": context["failedCalendarFetches"],
        }


def register_absence(
    runtime, operation_id, child_id, placement_id, starts_at, ends_at, message=None
):
    runtime.config.require("ist-home", "absence")
    start, end = _period(starts_at, ends_at)
    if message is not None and (
        not isinstance(message, str)
        or not message.strip()
        or len(message) > 20000
        or "\x00" in message
    ):
        raise FamiliekilderError("Ugyldig fraværsmerknad")
    with (
        provider(runtime.config.root, "ist-home") as (module, token),
        runtime.ledger() as ledger,
    ):
        client = Client(runtime.config.root, module, token)
        context = client.context()
        placement = client.select(context, child_id, placement_id)
        settings = placement["effective_settings"]
        zone = placement.get("tz")
        sender_id = context["user"]["id"]
        payload = {
            "placementId": placement_id,
            "startsAt": _wire(start),
            "endsAt": _wire(end),
            "type": "Absence",
        }
        if message is not None:
            payload["message"] = message

        def validate():
            if context["failedCalendarFetches"]:
                raise FamiliekilderError(
                    "IST melder ufullstendig kalendergrunnlag; fravær kan ikke sendes"
                )
            if (
                settings.get("absenceEnabled") is not True
                or settings.get("readonly") is not False
                or placement.get("readonly") is True
            ):
                raise FamiliekilderError(
                    "Fraværsregistrering er ikke aktivert for plasseringen"
                )
            if (
                placement["__typename"] == "Enrolment"
                and settings.get("disableEnrolmentAbsence") is not False
            ):
                raise FamiliekilderError(
                    "Skoleplasseringen tillater ikke fraværsregistrering"
                )
            if (
                message is not None
                and settings.get("submitAbsenceMessageEnabled") is not True
            ):
                raise FamiliekilderError(
                    "Plasseringen tillater ikke fritekst ved fravær"
                )
            if _source_instant(placement["startsAt"], zone) > start or (
                placement.get("endsAt")
                and _source_instant(placement["endsAt"], zone) < end
            ):
                raise FamiliekilderError("Perioden ligger utenfor barnets plassering")
            for row in client.absences(placement_id, start, end):
                if not row.get("endsAt") or (
                    _source_instant(row["startsAt"], zone) < end
                    and _source_instant(row["endsAt"], zone) > start
                ):
                    raise FamiliekilderError(
                        "Perioden overlapper eksisterende fravær; avklar registreringen først"
                    )

        def observe():
            # Recheck source coverage, including after sending or restart.
            if client.context()["failedCalendarFetches"]:
                raise SourceUnavailable(
                    "IST kan ikke bekrefte fullstendig kalendergrunnlag"
                )
            return [
                row["id"]
                for row in client.absences(placement_id, start, end)
                if row.get("__typename") == "PlannedAbsence"
                and row.get("type") == "Absence"
                and (row.get("createdBy") or {}).get("id") == sender_id
                and _source_instant(row.get("startsAt"), zone) == start
                and _source_instant(row.get("endsAt"), zone) == end
                and (row.get("message") or None) == message
            ]

        from .live_create import perform

        return perform(
            ledger,
            operation_id,
            "ist-home",
            "register_absence",
            payload,
            {
                "child_id": child_id,
                "placement_id": placement_id,
                "starts_at": _wire(start),
                "ends_at": _wire(end),
            },
            observe,
            lambda: client.write(REGISTER_ABSENCE, {"inputs": [payload]}),
            validate=validate,
        )


def list_conversations(runtime, starts_at, ends_at):
    runtime.config.require("ist-home")
    start, end = _period(starts_at, ends_at)
    with provider(runtime.config.root, "ist-home") as (module, token):
        client = Client(runtime.config.root, module, token)
        context = client.context()
        rows = _rows(
            client.read(
                CONVERSATIONS, {"startsAt": _wire(start), "endsAt": _wire(end)}
            ).get("getConversations")
        )
        if len(rows) > 5000:
            raise SourceUnavailable(
                "IST returnerte for mange samtaler; avgrens perioden"
            )
        result = []
        own_children = {
            c["id"] for c in context["children"] if c.get("relation") == "CHILD"
        }
        for row in rows:
            ident = identifier(row.get("id"))
            for child in _rows(row.get("children")):
                if child.get("id") in own_children:
                    client.conversation_placement(context, child["id"], row)
                    result.append(
                        {
                            **row,
                            "reference": {
                                "kind": "conversation",
                                "id": ident,
                                "child_id": child["id"],
                            },
                        }
                    )
        return {
            "source": "ist-home",
            "fetched_at": now(),
            "items": result,
            "complete": True,
            "source_content_is_untrusted": True,
            "coverage": "Samtaler i valgt tidsrom; API-et har ingen sidemarkør her.",
        }


def conversation_item(root, module, token, reference):
    from .live_readers import attachment_metadata, ref_item

    client = Client(root, module, token)
    context = client.context()
    thread = client.conversation(reference.get("id"))
    client.conversation_placement(context, reference.get("child_id"), thread)
    messages = [m for m in thread["messages"] if m.get("deleted") is not True]
    files = {}
    for msg in messages:
        for file in _rows(msg.get("files") or []):
            files[identifier(file.get("id"))] = file
    item = module.post_metadata(
        {**thread, "publishedAt": thread.get("createdAt")},
        context["children"],
        [attachment_metadata(f) for f in files.values()],
    )
    item.update(
        source_id="conversation:" + thread["id"],
        thread_id=thread["id"],
        type="message",
        topic="IST samtale",
        body="\n\n".join(
            " ".join(
                (m.get("publishedBy") or {}).get(k) or ""
                for k in ("firstName", "lastName")
            )
            + "\n"
            + str(m.get("createdAt") or "")
            + "\n"
            + (html_to_text_unescaped(m.get("content")) or "")
            for m in messages
        ),
        recipients=thread.get("recipients"),
        locked=thread.get("locked"),
    )
    return ref_item(item, reference)


def reply_message(
    runtime, operation_id, child_id, conversation_id, content, *, validate_thread=None
):
    runtime.config.require("ist-home", "messages")
    identifier(conversation_id)
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content) > 20000
        or "\x00" in content
    ):
        raise FamiliekilderError("Meldingstekst mangler eller er for lang")
    with (
        provider(runtime.config.root, "ist-home") as (module, token),
        runtime.ledger() as ledger,
    ):
        client = Client(runtime.config.root, module, token)
        context = client.context()
        thread = client.conversation(conversation_id)
        placements = client.conversation_placement(
            context, child_id, thread, require_current=True
        )
        sender_id = context["user"]["id"]
        payload = {
            "conversationId": conversation_id,
            "content": content,
            "attachmentIds": [],
        }

        def validate():
            if validate_thread is not None:
                validate_thread(thread)
            if thread.get("locked") is not False:
                raise FamiliekilderError("IST-samtalen er låst")
            if sender_id not in {
                r.get("id")
                for r in [
                    *_rows(thread.get("recipients")),
                    thread.get("publishedBy") or {},
                ]
            }:
                raise FamiliekilderError(
                    "Innlogget person er ikke deltaker i samtalen"
                )
            if any(
                p["effective_settings"].get("enableMessaging") is not True
                or p["effective_settings"].get("readonly") is not False
                or p.get("readonly") is True
                for p in placements
            ):
                raise FamiliekilderError("Plasseringen tillater ikke meldinger")

        def observe():
            return [
                m["id"]
                for m in client.conversation(conversation_id)["messages"]
                if m.get("content") == content
                and (m.get("publishedBy") or {}).get("id") == sender_id
                and m.get("deleted") is not True
                and not m.get("files")
            ]

        from .live_create import perform

        return perform(
            ledger,
            operation_id,
            "ist-home",
            "reply_message",
            {"child_id": child_id, **payload},
            {"child_id": child_id, "conversation_id": conversation_id},
            observe,
            lambda: client.write(REPLY, {"input": payload}),
            validate=validate,
        )


def register(server, runtime):
    from .source_server import READ, WRITE, safe_tool

    def ist_home_action_context() -> dict:
        """Hent egne barn/plasseringer og effektive fraværs-/meldingsrettigheter fra IST. Plasseringens overstyringer gjelder."""
        return action_context(runtime)

    def ist_home_list_absences(
        child_id: str, placement_id: str, starts_at: str, ends_at: str
    ) -> dict:
        """Les fravær direkte i valgt periode (maks 366 dager). Tidspunkt må inkludere tidssone. Sjekk complete og failed_calendar_fetches."""
        return list_absences(runtime, child_id, placement_id, starts_at, ends_at)

    def ist_home_register_absence(
        operation_id: str,
        child_id: str,
        placement_id: str,
        starts_at: str,
        ends_at: str,
        message: str | None = None,
    ) -> dict:
        """Registrer fravær etter konkret brukerbeskjed. Eksakte tidspunkt med tidssone; angi nøyaktig sluttdato og klokkeslett. Merknad krever støtte i plasseringen. Ukjent resultat kontrolleres med samme operation_id uten ny sending."""
        return register_absence(
            runtime, operation_id, child_id, placement_id, starts_at, ends_at, message
        )

    def ist_home_list_conversations(starts_at: str, ends_at: str) -> dict:
        """Finn samtaler for egne barn i valgt tidsrom. Bruk reference med ist_home_get_item/read_attachment for full tråd og vedlegg."""
        return list_conversations(runtime, starts_at, ends_at)

    def ist_home_reply_message(
        operation_id: str, child_id: str, conversation_id: str, content: str
    ) -> dict:
        """Svar i valgt samtale etter konkret brukerbeskjed. Velg barn og mottakere fra fersk tråd. Ukjent resultat kontrolleres med samme operation_id uten ny sending."""
        return reply_message(runtime, operation_id, child_id, conversation_id, content)

    server.add_tool(safe_tool(ist_home_list_conversations), annotations=READ)
    if "messages" in runtime.config.services["ist-home"]:
        server.add_tool(safe_tool(ist_home_reply_message), annotations=WRITE)
    server.add_tool(safe_tool(ist_home_action_context), annotations=READ)
    server.add_tool(safe_tool(ist_home_list_absences), annotations=READ)
    if "absence" in runtime.config.services["ist-home"]:
        server.add_tool(safe_tool(ist_home_register_absence), annotations=WRITE)
    from .live_ist_messages import register as register_messages

    register_messages(server, runtime)
