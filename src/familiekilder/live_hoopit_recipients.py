"""Fresh chat recipients from Hoopit's permitted contact groups, not Stream search."""

from .core import FamiliekilderError
from .hoopit import MAX_PAGES_PER_FEED
from .live_hoopit import _id
from .live_hoopit_chat import Client, _rows
from .live_readers import provider, identifier
from .action_ledger import now
from .source_adapter import SourceUnavailable

ROLES = ("players", "guardians", "admins")
BASE = "/app/messaging/contacts/groups/"


def groups(core):
    result = []
    for row in _rows(core.get(BASE), 5000):
        ident = _id(row.get("id"))
        result.append(
            {
                "reference": {"kind": "chat_group", "group_id": ident},
                "name": row.get("name"),
                "organization_name": row.get("organization_name"),
                "division_name": row.get("division_name"),
                "parent_group_name": row.get("parent_group_name"),
            }
        )
    if len({r["reference"]["group_id"] for r in result}) != len(result):
        raise SourceUnavailable("Hoopit returnerte dupliserte kontaktgrupper")
    return result


def _selection(group_id, role, page):
    group_id = _id(group_id)
    if role not in ROLES:
        raise FamiliekilderError("Velg players, guardians eller admins")
    if type(page) is not int or not 1 <= page <= MAX_PAGES_PER_FEED:
        raise FamiliekilderError(f"Kontaktside må være 1–{MAX_PAGES_PER_FEED}")
    return group_id


def contact_page(core, group_id, role, page=1):
    group_id = _selection(group_id, role, page)
    available = groups(core)
    group = next((g for g in available if g["reference"]["group_id"] == group_id), None)
    if group is None:
        raise FamiliekilderError("Velg en gruppe fra Hoopits ferske kontaktgrupper")
    value = core.get(f"{BASE}{group_id}/{role}/", page=page)
    if not isinstance(value, dict):
        raise SourceUnavailable("Hoopit mangler en kontaktside")
    rows = _rows(value.get("results"), 1000)
    count, more = value.get("count"), value.get("has_next")
    if type(count) is not int or count < len(rows) or type(more) is not bool:
        raise SourceUnavailable("Hoopit mangler pålitelig kontaktpaginering")
    if more and not rows:
        raise SourceUnavailable("Hoopit returnerte en tom mellomside")
    result = []
    seen_ids, seen_uuids = set(), set()
    for row in rows:
        ident, uid = _id(row.get("id")), identifier(row.get("uuid"))
        if (
            ident in seen_ids
            or uid in seen_uuids
            or type(row.get("can_message")) is not bool
        ):
            raise SourceUnavailable(
                "Hoopit mangler entydig kontakt eller meldingsrettighet"
            )
        seen_ids.add(ident)
        seen_uuids.add(uid)
        result.append(
            {
                "reference": {
                    "kind": "chat_recipient",
                    "group_id": group_id,
                    "role": role,
                    "id": ident,
                    "uuid": uid,
                },
                "name": row.get("full_name"),
                "can_message": row["can_message"],
                "supervised_by_parents": row.get("supervised_by_parents"),
            }
        )
    return {
        "group": group,
        "role": role,
        "recipients": result,
        "page": page,
        "total_count": count,
        "end_of_list": not more,
        "next_page": page + 1 if more and page < MAX_PAGES_PER_FEED else None,
        "page_limit_reached": more and page == MAX_PAGES_PER_FEED,
        "coverage_complete": page == 1 and not more and len(rows) == count,
    }


def check_chat_users(client, contacts):
    """Explicitly mark unavailable recipients; never silently shrink an audience."""
    eligible = [
        r["reference"]["uuid"]
        for r in contacts
        if r["can_message"] and r["reference"]["uuid"] != client.user_id
    ]
    users = client.users(eligible)
    for row in contacts:
        ref = row["reference"]
        user = users.get(ref["uuid"])
        reason = None
        if ref["uuid"] == client.user_id:
            reason = "self"
        elif not row["can_message"]:
            reason = "source_disallows_message"
        elif user is None:
            reason = "missing_chat_user"
        elif _id(user.get("hoopit_id")) != ref["id"]:
            raise SourceUnavailable("Hoopit- og chatidentiteten stemmer ikke overens")
        elif (
            user.get("banned") is not False
            or user.get("shadow_banned") is not False
            or user.get("deleted_at")
            or user.get("deactivated_at")
        ):
            reason = "chat_user_unavailable"
        elif (
            not isinstance(user.get("blocked_user_ids"), list)
            or client.user_id in user["blocked_user_ids"]
        ):
            reason = "blocked_or_unverified"
        row["chat_available"] = reason is None
        row["unavailable_reason"] = reason


def read(runtime, group_id=None, role="admins", page=1):
    runtime.config.require("hoopit")
    if group_id is not None:
        group_id = _selection(group_id, role, page)
    with provider(runtime.config.root, "hoopit") as (_, core):
        if group_id is None:
            data = {
                "groups": groups(core),
                "roles": list(ROLES),
                "coverage_complete": True,
            }
        else:
            data = contact_page(core, group_id, role, page)
            client = Client(core)
            check_chat_users(client, data["recipients"])
        return {
            "source": "hoopit",
            "fetched_at": now(),
            "source_content_is_untrusted": True,
            **data,
            "limitations": [
                "Bare Hoopits tilgjengelige kontaktgrupper og valgt rolle; ingen generell brukerkatalog.",
                "Sidene og rettighetene kan endres mellom oppslag. Kontroller valgte mottakere på nytt før sending.",
                "chat_available gjelder mottakerens tilgjengelighet, ikke godkjenning av en skrivehandling. Foreldreinnsyn kan gjelde.",
            ],
        }


def register(server, runtime):
    from .source_server import READ, safe_tool

    def hoopit_chat_groups() -> dict:
        """List ferske kontaktgrupper for nye Hoopit-samtaler. Oppslaget henter ingen meldinger og oppretter ingen samtale."""
        return read(runtime)

    def hoopit_chat_recipients(
        group_id: str, role: str = "admins", page: int = 1
    ) -> dict:
        """List mottakere fra en fersk kontaktgruppe, rolle admins/guardians/players. Følg next_page. Bruk stabile referanser; kontroller can_message og chat_available. Utilgjengelige mottakere beholdes med årsak. Ingen sending eller opprettelse."""
        return read(runtime, group_id, role, page)

    for tool in (hoopit_chat_groups, hoopit_chat_recipients):
        server.add_tool(safe_tool(tool), annotations=READ)
