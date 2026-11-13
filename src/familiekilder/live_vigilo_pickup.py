"""Read-only pickup notes, including recurring agreements and daily overrides.

Contracts: Vigilo Android 3751 RxRestAPI, CheckoutNotesRepositoryImpl,
CheckoutNoteMapper and ConvertCheckoutNotesGridDataUseCase. The server supplies
dateOccurrences; never expand recurrence rules locally or infer actual checkout.
"""

import re
from datetime import date, datetime, time, timedelta, UTC
from zoneinfo import ZoneInfo

from .core import FamiliekilderError
from .live_readers import identifier, provider
from .action_ledger import now
from .live_vigilo import ActionClient, _rows
from .source_adapter import SourceUnavailable

ZONE = ZoneInfo("Europe/Oslo")
MAX_NOTES = 5000
MAX_OCCURRENCES = 100_000
COVERAGE = (
    "Hentemeldinger returnert nå for valgt barns måned. Kildens dateOccurrences "
    "avgjør datoene; ingen historikk- eller endringslogg garanteres. "
    "Hentetid er en avtale, ikke bekreftet faktisk utsjekk eller plassomfang."
)


def _requested_date(value):
    if value is None:
        return datetime.now(ZONE).date()
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        parsed = date.fromisoformat(value)
        if parsed.year == 9999 and parsed.month == 12:
            raise ValueError()
        return parsed
    except ValueError:
        raise FamiliekilderError("on_date må være en gyldig YYYY-MM-DD-dato") from None


def _instant(value):
    try:
        if not isinstance(value, str) or "T" not in value:
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # parseUTCTime in the official client assigns UTC then converts to local.
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        raise SourceUnavailable("Vigilo returnerte ugyldig hentemeldingsdato") from None


def _local_date(value):
    return _instant(value).astimezone(ZONE).date()


def _validate(row, child_id, units):
    identifier(row.get("id"))
    if row.get("childPersonId") != child_id or row.get("organizationalUnitId") not in units:
        raise SourceUnavailable("Vigilo returnerte hentemelding uten valgt barn/enhet")
    identifier(row.get("category"))
    for key in ("isRecurring", "isEnabled"):
        if type(row.get(key)) is not bool:
            raise SourceUnavailable("Vigilo mangler hentemeldingens status")
    kind = row.get("type")
    if not isinstance(kind, dict) or not isinstance(kind.get("name"), str):
        raise SourceUnavailable("Vigilo mangler hentemeldingstype")
    identifier(kind.get("id"))
    if not isinstance(row.get("noteText"), str):
        raise SourceUnavailable("Vigilo mangler hentemeldingstekst")
    start, end = _local_date(row.get("fromDate")), _local_date(row.get("toDate"))
    if end <= start:
        raise SourceUnavailable("Vigilo returnerte ugyldig henteavtaleperiode")
    try:
        clock = row.get("timeOfDay")
        if not isinstance(clock, str) or not re.fullmatch(r"\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?", clock):
            raise ValueError()
        time.fromisoformat(clock)
    except ValueError:
        raise SourceUnavailable("Vigilo returnerte ugyldig hentetid") from None
    occurrences = row.get("dateOccurrences")
    if not isinstance(occurrences, list) or len(occurrences) > MAX_OCCURRENCES:
        raise SourceUnavailable("Vigilo mangler eller overskrider hentedatoene")
    days = [_local_date(v) for v in occurrences]
    if len(set(days)) != len(days) or any(not start <= d < end for d in days):
        raise SourceUnavailable("Vigilo returnerte dupliserte hentedatoer eller dato utenfor avtalen")
    _instant(row.get("registeredTime"))
    if row.get("lastUpdatedTime") not in (None, ""):
        _instant(row["lastUpdatedTime"])
    pattern = row.get("recurrencePattern")
    if not isinstance(pattern, dict) or not isinstance(pattern.get("recurrenceType"), str):
        raise SourceUnavailable("Vigilo mangler repeteringsmønster")
    if type(pattern.get("interval")) is not int:
        raise SourceUnavailable("Vigilo returnerte ugyldig repeteringsintervall")
    weekdays = pattern.get("daysOfWeek")
    if weekdays is not None and (not isinstance(weekdays, list) or any(not isinstance(v, str) for v in weekdays)):
        raise SourceUnavailable("Vigilo returnerte ugyldige repeteringsdager")


class Client(ActionClient):
    def __init__(self, root, module, connection):
        super().__init__(module, connection)
        self.root = root
        self.person_id = self.identity()

    def identity(self):
        return identifier(self.module.local_session_for_use(self.root).get("person_id"))

    def notes(self, child_id, day, organization_id=None):
        child, _ = self.select(child_id, organization_id)
        units = {identifier(u.get("id")) for u in _rows(child.get("organizationalUnits"), "enheter")}
        start = day.replace(day=1)
        # Match the app's monthly request, with an exclusive next-month boundary.
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        data = self.get("api/check-in-notes", {
            "childPersonId": child_id, "fromDate": start.isoformat(), "toDate": end.isoformat(),
        })
        if set(data) != {"items"}:
            raise SourceUnavailable("Uventet henteoversikt; sidedekningen kan ikke bekreftes")
        rows = _rows(data.get("items"), "hentemeldinger")
        if len(rows) > MAX_NOTES:
            raise SourceUnavailable("For mange hentemeldinger til fullstendig kontroll")
        seen, total = set(), 0
        for row in rows:
            _validate(row, child_id, units)
            if row["id"] in seen:
                raise SourceUnavailable("Vigilo returnerte duplisert hentemelding")
            seen.add(row["id"])
            total += len(row["dateOccurrences"])
            if total > MAX_OCCURRENCES:
                raise SourceUnavailable("Hentedatoene overskred samlet lesegrense")
        if self.identity() != self.person_id:
            raise SourceUnavailable("Vigilo-kontoen endret seg under oppslaget")
        return [r for r in rows if organization_id is None or r["organizationalUnitId"] == organization_id], start, end


def _selection(rows, day):
    states = {r["id"]: "not_for_date" for r in rows}
    groups = {}
    for row in rows:
        if day in {_local_date(v) for v in row["dateOccurrences"]}:
            groups.setdefault((row["organizationalUnitId"], row["category"]), []).append(row)
    decisions = []
    for (unit, category), candidates in sorted(groups.items()):
        # The official grid chooses latest update, falling back to registration.
        stamps = {r["id"]: _instant(r.get("lastUpdatedTime") or r["registeredTime"]) for r in candidates}
        latest = max(stamps.values())
        winners = [r for r in candidates if stamps[r["id"]] == latest]
        status = "ambiguous" if len(winners) != 1 else "current" if winners[0]["isEnabled"] else "disabled"
        for row in candidates:
            states[row["id"]] = status if row in winners else "superseded"
        decisions.append({
            "organization_id": unit, "category": category, "status": status,
            "effective_note_id": winners[0]["id"] if status == "current" else None,
            "candidate_ids": [r["id"] for r in candidates],
        })
    return states, decisions


def item(row, child_id, day, state):
    return {
        "source_id": "pickup-note:" + row["id"] + ":" + child_id,
        "reference": {"kind": "pickup_note", "id": row["id"], "child_id": child_id,
                      "organization_id": row["organizationalUnitId"], "on_date": day.isoformat()},
        "child_id": child_id, "organization_id": row["organizationalUnitId"],
        "title": row["type"]["name"], "body": row["noteText"],
        "category": row["category"], "pickup_type": row["type"],
        "pickup_time": row["timeOfDay"], "timezone": str(ZONE),
        "from_date": _local_date(row["fromDate"]).isoformat(),
        "through_date": (_local_date(row["toDate"]) - timedelta(days=1)).isoformat(),
        "is_recurring": row["isRecurring"], "is_enabled": row["isEnabled"],
        "recurrence_pattern": row["recurrencePattern"],
        "date_occurrences": [_local_date(v).isoformat() for v in row["dateOccurrences"]],
        "registered_at": row["registeredTime"], "updated_at": row.get("lastUpdatedTime"),
        "registered_by": row.get("registeredBy"), "updated_by": row.get("lastUpdatedBy"),
        "on_date": day.isoformat(), "applies_on_date": state != "not_for_date",
        "selection_status": state, "attachments": [], "content_complete": True,
        "coverage": COVERAGE,
    }


def get_item(root, module, connection, reference):
    child_id, ident = identifier(reference.get("child_id")), identifier(reference.get("id"))
    unit = identifier(reference.get("organization_id"))
    if reference.get("on_date") is None:
        raise FamiliekilderError("Hentemeldingsreferansen må angi on_date")
    day = _requested_date(reference["on_date"])
    rows, _, _ = Client(root, module, connection).notes(child_id, day, unit)
    matches = [r for r in rows if r["id"] == ident]
    if len(matches) != 1:
        raise SourceUnavailable("Hentemeldingen finnes ikke i ferskt oppslag; dette beviser ikke at avtalen er avlyst")
    states, _ = _selection(rows, day)
    return item(matches[0], child_id, day, states[ident])


def list_pickup_notes(runtime, child_id, on_date=None, organization_id=None):
    runtime.config.require("vigilo")
    identifier(child_id)
    if organization_id is not None:
        identifier(organization_id)
    day = _requested_date(on_date)
    runtime.cleanup()
    with provider(runtime.config.root, "vigilo") as (module, connection):
        rows, start, end = Client(runtime.config.root, module, connection).notes(child_id, day, organization_id)
        states, decisions = _selection(rows, day)
        return {
            "source": "vigilo", "fetched_at": now(), "child_id": child_id,
            "on_date": day.isoformat(), "timezone": str(ZONE),
            "window": {"from_date": start.isoformat(), "to_date_exclusive": end.isoformat()},
            "items": [runtime.expose("vigilo", item(r, child_id, day, states[r["id"]])) for r in rows],
            "selections": decisions, "complete": True, "next_page": None,
            "coverage": COVERAGE, "source_content_is_untrusted": True,
        }


def register(server, runtime):
    from .source_server import READ, safe_tool

    def vigilo_list_pickup_notes(child_id: str, on_date: str | None = None,
                               organization_id: str | None = None) -> dict:
        """Les hentemeldinger og repeterende hente-/hjemsendingsavtaler. on_date er YYYY-MM-DD, standard i dag i Europe/Oslo. Henter hele månedens svar og velger gjeldende melding per enhet/kategori fra kildens datoer og siste endring. Les selections og selection_status; ambiguous/disabled gir ingen bekreftet avtale. Manglende treff eller kildefeil betyr ikke manglende avtale. pickup_time er planlagt hentetid, ikke faktisk utsjekk eller plassomfang. Referanser kan leses ferskt med vigilo_get_item. Ingen skriving eller lest-markering."""
        return list_pickup_notes(runtime, child_id, on_date, organization_id)

    server.add_tool(safe_tool(vigilo_list_pickup_notes), annotations=READ)
