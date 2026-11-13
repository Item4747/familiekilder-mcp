"""Direct Spond chat reads required for recipient and delivery verification.

Chat authentication stays in memory. Only the first-party session endpoint and
explicit read routes are callable; no message, read-state or membership writes.
"""

from datetime import datetime
from urllib.parse import urlencode

from .api_transport import ApiHTTPError, ApiTransportError
from .core import FamiliekilderError
from .live_readers import identifier, provider, attachment_metadata
from .live_spond_reader import spond_context, spond_read_value
from .action_ledger import now
from .source_adapter import SourceUnavailable

CHAT_ROOT = "https://api.spond.com/chat/v1"
API_LEVEL = "2.7.14"
PAGE_SIZE = 20


def _rows(value, maximum=PAGE_SIZE):
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(not isinstance(r, dict) for r in value)
    ):
        raise SourceUnavailable("Spond returnerte ugyldig eller for stor chatliste")
    return value


def _ids(value):
    if not isinstance(value, list) or len(value) > 10000:
        raise SourceUnavailable("Spond returnerte ugyldige chatidentiteter")
    ids = [identifier(v) for v in value]
    if len(ids) != len(set(ids)):
        raise SourceUnavailable("Spond returnerte dupliserte chatidentiteter")
    return ids


def _number(value):
    if type(value) is not int or value < 0 or value > 2**53 - 1:
        raise FamiliekilderError("Ugyldig Spond-meldingsnummer")
    return value


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
    except (AttributeError, TypeError, ValueError):
        raise FamiliekilderError(
            "Chatmarkøren må være et ISO-tidspunkt med tidssone"
        ) from None
    return parsed


class Client:
    def __init__(self, module, core):
        self.module = module
        self.core = core
        self.auth = None
        self._renewed = False

    def _failure(self, exc):
        if isinstance(exc, ApiHTTPError):
            self.core._record_provider_stop(exc)
        elif isinstance(exc, (ApiTransportError, OSError, TimeoutError)):
            self.module.record_cooldown(self.core.root, 503, at=self.core.wall_time())

    def _bootstrap(self):
        self.core._before_request()
        try:
            result = self.core.transport.post_json(
                self.module.API_ROOT + "/chat",
                {},
                bearer_token=self.core.token,
                headers={"Api-Level": API_LEVEL},
            )
        except (ApiHTTPError, ApiTransportError, OSError, TimeoutError) as exc:
            self._failure(exc)
            raise SourceUnavailable("Spond-chatøkten kunne ikke opprettes") from None
        if not isinstance(result, dict) or result.get("url") != CHAT_ROOT:
            raise SourceUnavailable("Spond returnerte en ukjent chatvert eller API-sti")
        auth = result.get("auth")
        if (
            not isinstance(auth, str)
            or not auth
            or len(auth) > 65536
            or any(ord(c) < 33 or ord(c) > 126 for c in auth)
        ):
            raise SourceUnavailable("Spond returnerte en ugyldig chatnøkkel")
        self.auth = auth

    def _get(self, path, query=None):
        from .sync_snapshot import memo_read

        return memo_read(getattr(self.core, 'root', None), 'spond', ('chat-GET', path, query),
                         lambda: self._get_live(path, query))

    def _get_live(self, path, query=None):
        if self.auth is None:
            self._bootstrap()
        url = CHAT_ROOT + path + ("?" + urlencode(query) if query else "")
        for attempt in range(2):
            self.core._before_request()
            try:
                return self.core.transport.get_json(
                    url,
                    bearer_token=self.core.token,
                    headers={"Auth": self.auth, "Api-Level": API_LEVEL},
                )
            except ApiHTTPError as exc:
                self._failure(exc)
                if exc.status_code == 401 and not self._renewed and attempt == 0:
                    self._renewed = True
                    # Core client's own bounded refresh and uncertainty barrier apply.
                    from .sync_snapshot import suspend_snapshot

                    with suspend_snapshot():
                        self.core.get_json(self.module.PROFILE_ENDPOINT)
                    self._bootstrap()
                    continue
                raise SourceUnavailable("Spond avviste chatoppslaget") from None
            except (ApiTransportError, OSError, TimeoutError) as exc:
                self._failure(exc)
                raise SourceUnavailable(
                    "Spond-chat er midlertidig utilgjengelig"
                ) from None
        raise SourceUnavailable("Spond-chatoppslaget kunne ikke fullføres")

    def recipients(self, group_id):
        value = self._get("/chats/groupRecipients/" + identifier(group_id))
        if not isinstance(value, dict) or "standard" not in value:
            raise SourceUnavailable("Spond mangler standardlisten for chatmottakere")
        return _ids(value["standard"])

    def find(self, group_id, recipient_ids):
        """First-party recipient lookup. This POST does not create a chat."""
        group_id = identifier(group_id)
        ids = _ids(recipient_ids)
        if not ids or len(ids) > 99:
            raise FamiliekilderError("Velg 1–99 chatmottakere")
        from .sync_snapshot import memo_read

        return memo_read(getattr(self.core, 'root', None), 'spond', ('chat-find', group_id, ids),
                         lambda: self._find_live(group_id, ids))

    def _find_live(self, group_id, ids):
        if self.auth is None:
            self._bootstrap()
        self.core._before_request()
        try:
            value = self.core.transport.post_json_array(
                CHAT_ROOT + "/chats/find/" + group_id,
                ids,
                bearer_token=self.core.token,
                headers={"Auth": self.auth, "Api-Level": API_LEVEL},
            )
        except (ApiHTTPError, ApiTransportError, OSError, TimeoutError) as exc:
            self._failure(exc)
            raise SourceUnavailable("Spond-chatten kunne ikke slås opp") from None
        if not isinstance(value, dict):
            raise SourceUnavailable("Spond returnerte ugyldig chatoppslag")
        if value:
            identifier(value.get("id"))
        return value

    def chats(self, before=None):
        query = {"max": PAGE_SIZE}
        if before is not None:
            _timestamp(before)
            query["maxTimestamp"] = before
        rows = _rows(self._get("/chats", query))
        ids = set()
        timestamps = []
        for row in rows:
            ident = identifier(row.get("id"))
            if ident in ids:
                raise SourceUnavailable("Duplikat chat-ID i Spond-listen")
            ids.add(ident)
            stamp = _timestamp(row.get("newestTimestamp"))
            if before is not None and stamp >= _timestamp(before):
                raise SourceUnavailable("Spond-chatmarkøren flyttet seg ikke bakover")
            timestamps.append(stamp)
        if timestamps != sorted(timestamps, reverse=True):
            raise SourceUnavailable("Spond returnerte usortert chathistorikk")
        return rows

    def thread(self, chat_id):
        chat_id = identifier(chat_id)
        value = self._get("/chats/" + chat_id)
        if not isinstance(value, dict) or value.get("id") != chat_id:
            raise SourceUnavailable("Spond returnerte feil chat")
        return value

    def messages(self, chat_id, before=None):
        chat_id = identifier(chat_id)
        query = {"count": PAGE_SIZE}
        if before is not None:
            if _number(before) == 0:
                raise FamiliekilderError(
                    "Ingen tidligere meldingsside finnes før nummer 0"
                )
            query["maxNum"] = before
        rows = _rows(self._get("/chats/" + chat_id + "/messages", query))
        seen = set()
        for row in rows:
            num = _number(row.get("msgNum"))
            if (
                row.get("chatId") != chat_id
                or num in seen
                or (before is not None and num > before)
            ):
                raise SourceUnavailable(
                    "Spond returnerte feil eller overlappende meldingsside"
                )
            seen.add(num)
        if [r["msgNum"] for r in rows] != sorted(seen, reverse=True):
            raise SourceUnavailable("Spond returnerte usorterte meldingsnumre")
        # maxNum includes the boundary row. Drop that one overlap instead of
        # subtracting one: maxNum=0 means "latest", and message zero is real.
        return [r for r in rows if before is None or r["msgNum"] < before]

    def message(self, chat_id, msg_num):
        msg_num = _number(msg_num)
        # maxNum is inclusive and zero means latest. A positive bound one
        # above the target reuses the checked pagination path, including zero.
        before = msg_num + 1 if msg_num < 2**53 - 1 else None
        rows = self.messages(chat_id, before)
        matches = [r for r in rows if r["msgNum"] == msg_num]
        if len(matches) != 1:
            raise SourceUnavailable("Chatmeldingen finnes ikke lenger i kildeoppslaget")
        return matches[0]


def _thread(value, sender, names):
    if value.get("type") not in {"DM", "GROUP"}:
        raise FamiliekilderError(
            "Bare vanlige private-/gruppechatter inngår i dette oppslaget"
        )
    participants = _ids(value.get("participants"))
    former = _ids(value.get("formerParticipants", []))
    if sender not in participants and sender not in former:
        raise FamiliekilderError("Innlogget person er ikke deltaker i chatten")
    return {
        "reference": {"kind": "chat", "id": identifier(value.get("id"))},
        "group_id": value.get("groupId"),
        "type": value["type"],
        "name": value.get("name"),
        "newest_at": value.get("newestTimestamp"),
        "participants": [{"id": p, "name": names.get(p)} for p in participants],
        "former_participant_ids": former,
        "source_indicates_removed": sender in former
        or (value["type"] == "DM" and bool(former)),
        "guardian_access_possible": True,
    }


def _base():
    return {
        "source": "spond",
        "fetched_at": now(),
        "source_content_is_untrusted": True,
        "chat_sending_verified": True,
        "privacy_note": "Foresatte kan ha innsyn i barnas samtaler etter Sponds regler.",
    }


def message_item(row, thread, names):
    """A stable item usable by the existing attachment and follow-up tools."""
    chat_id = thread["reference"]["id"]
    num = _number(row.get("msgNum"))
    if row.get("chatId") != chat_id:
        raise SourceUnavailable("Meldingen tilhører en annen Spond-chat")
    user = row.get("user")
    if user is not None:
        identifier(user)
    specs = []
    limitations = []
    for key in ("file", "image", "images"):
        value = row.get(key)
        if value is None:
            continue
        if key == "images" and not isinstance(value, list):
            limitations.append("Ukjent bildeliste; innholdet er ikke lest.")
            continue
        values = value if key == "images" and isinstance(value, list) else [value]
        for spec in values:
            if not isinstance(spec, dict) or not any(
                isinstance(spec.get(k), str) and spec[k] for k in ("media", "url")
            ):
                limitations.append("Ukjent vedleggsstruktur; innholdet er ikke lest.")
                continue
            specs.append({**spec, "_group_id": thread["group_id"]})
    if len(specs) > 100:
        raise SourceUnavailable("Chatmeldingen har for mange vedlegg")
    if row.get("type") in {"IMAGE", "FILE"} and not specs:
        limitations.append("Kilden oppgir en mediemelding uten lesbart vedlegg.")
    attachments = {}
    for spec in specs:
        metadata = attachment_metadata(spec)
        attachments.setdefault(metadata["attachment_id"], metadata)
    forwarded = []
    for key, kind in (("post", "post"), ("spond", "event")):
        value = row.get(key)
        if value is not None:
            try:
                forwarded.append({"kind": kind, "id": identifier(value.get("id"))})
            except (AttributeError, FamiliekilderError):
                pass
            limitations.append(
                "Videresendt innlegg/aktivitet må hentes separat; grunnlaget er ufullstendig."
            )
    if any(row.get(k) is not None for k in ("campaign", "promo", "clubMessage")):
        limitations.append("Kampanje-/organisasjonsinnhold er ikke lest.")
    if row.get("type") == "DELETED":
        attachments = {}
        limitations.append(
            "Meldingen er slettet hos kilden; innholdet kan ikke bekreftes."
        )
    audience = {k: v for k, v in thread.items() if k != "newest_at"}
    return {
        "source": "spond",
        "source_id": f"chat:{chat_id}:message:{num}",
        "reference": {"kind": "chat_message", "chat_id": chat_id, "msg_num": num},
        "title": "Chatmelding",
        "body": row.get("text"),
        "text": row.get("text"),
        "author_id": user,
        "author_name": names.get(user),
        "type": row.get("type"),
        "timestamp": row.get("timestamp"),
        "audience": audience,
        "attachments": list(attachments.values()),
        "has_unread_attachment": bool(attachments) or bool(limitations),
        "content_complete": not limitations,
        "limitations": limitations,
        "forwarded_references": forwarded,
    }


def get_message_item(module, core, reference):
    if (
        not isinstance(reference, dict)
        or set(reference) != {"kind", "chat_id", "msg_num"}
        or reference.get("kind") != "chat_message"
    ):
        raise FamiliekilderError("Velg en chatmeldingsreferanse fra spond_get_chat")
    cid = identifier(reference["chat_id"])
    num = _number(reference["msg_num"])
    profile, _, context = spond_context(module, core)
    client = Client(module, core)
    thread = spond_read_value(core, ('thread', cid), lambda: _thread(
        client.thread(cid), identifier(profile.get("id")), context["profiles"]))
    return message_item(client.message(cid, num), thread, context["profiles"])


def recipients(runtime, group_id=None):
    runtime.config.require("spond")
    if group_id is not None:
        identifier(group_id)
    with provider(runtime.config.root, "spond") as (module, core):
        profile, groups, context = spond_context(module, core)
        sender = identifier(profile.get("id"))
        result = {
            **_base(),
            "sender_id": sender,
            "groups": [
                {"id": identifier(g.get("id")), "name": g.get("name")} for g in groups
            ],
        }
        if group_id is None:
            return result
        if not any(g.get("id") == group_id for g in groups):
            raise FamiliekilderError("Velg en gruppe fra ditt ferske Spond-oppslag")
        ids = Client(module, core).recipients(group_id)
        result.update(
            group_id=group_id,
            recipients=[
                {"id": i, "name": context["profiles"].get(i)}
                for i in ids
                if i != sender
            ],
            coverage="standard_recipients_only",
            limitations=[
                "Bare standardmottakere fra tjenesten; ingen omgåelse av alders- eller tilgangsgrenser."
            ],
        )
        return result


def list_chats(runtime, before=None):
    runtime.config.require("spond")
    if before is not None:
        _timestamp(before)
    with provider(runtime.config.root, "spond") as (module, core):
        profile, _, context = spond_context(module, core)
        sender = identifier(profile.get("id"))
        rows = Client(module, core).chats(before)
        chats = [
            _thread(row, sender, context["profiles"])
            for row in rows
            if row.get("type") in {"DM", "GROUP"}
        ]
        return {
            **_base(),
            "chats": chats,
            "excluded_non_chat_count": len(rows) - len(chats),
            "complete": not rows,
            "next_before": rows[-1]["newestTimestamp"] if rows else None,
            "coverage": "own_dm_and_group_chats",
            "limitations": [
                "Tidsmarkør som i appen; samtidige endringer eller like sidetidspunkt kan begrense historisk dekning."
            ],
        }


def get_chat(runtime, chat_id, before=None):
    runtime.config.require("spond")
    identifier(chat_id)
    if before is not None and _number(before) == 0:
        raise FamiliekilderError("Ingen tidligere meldingsside finnes før nummer 0")
    with provider(runtime.config.root, "spond") as (module, core):
        profile, _, context = spond_context(module, core)
        sender = identifier(profile.get("id"))
        client = Client(module, core)
        thread = spond_read_value(core, ('thread', chat_id),
            lambda: _thread(client.thread(chat_id), sender, context["profiles"]))
        rows = client.messages(chat_id, before)
        messages = [
            runtime.expose("spond", message_item(row, thread, context["profiles"]))
            for row in rows
        ]
        end = not rows or rows[-1]["msgNum"] == 0
        return {
            **_base(),
            "thread": thread,
            "messages": messages,
            "complete": end,
            "next_before_msg_num": None if end else rows[-1]["msgNum"],
            "attachments_complete": not any(
                m["has_unread_attachment"] for m in messages
            ),
        }


def register(server, runtime):
    from .source_server import READ, safe_tool

    def spond_chat_recipients(group_id: str | None = None) -> dict:
        """Hent egne Spond-grupper, eller standard chatmottakere for valgt gruppe. Stabile profil-ID-er. Ingen aldersomgåelse, meldingssending eller lest-markering."""
        return recipients(runtime, group_id)

    def spond_list_chats(before: str | None = None) -> dict:
        """List egne private-/gruppechatter sidevis, med kildens tidsmarkør next_before. Hver side hentes direkte. Les begrensningene; ingen lest-markering."""
        return list_chats(runtime, before)

    def spond_get_chat(chat_id: str, before_msg_num: int | None = None) -> dict:
        """Les chatdetaljer og én meldingsside (20). Følg next_before_msg_num til complete. Les vedlegg med spond_read_attachment og meldingens reference/attachment_id. Videresendte kildereferanser må leses separat. Sjekk attachments_complete/content_complete. Ingen lest-markering eller sending."""
        return get_chat(runtime, chat_id, before_msg_num)

    for tool in (spond_chat_recipients, spond_list_chats, spond_get_chat):
        server.add_tool(safe_tool(tool), annotations=READ)
