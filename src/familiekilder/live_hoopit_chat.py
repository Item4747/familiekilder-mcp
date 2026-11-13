"""Read-only Stream chat access using Hoopit's fresh, user-bound session.

Never call channel.query/watch: those routes can create channels. Tokens remain
in memory and only reach the fixed Stream API host, without the Hoopit JWT.
"""

import base64
import json
import re
from urllib.parse import urlencode

from .api_transport import ApiHTTPError, ApiTransport, ApiTransportError
from .core import FamiliekilderError
from .live_readers import provider, identifier
from .action_ledger import now
from .source_adapter import SourceUnavailable


class SearchCursorRejected(SourceUnavailable):
    """A continuation was rejected; a new bounded search can rediscover safely."""
    reason = 'pagination'

CHAT_ROOT = "https://chat.stream-io-api.com"
# Public application key from Hoopit's production web configuration (bC5.w).
PUBLIC_API_KEY = "y8afyfq3u8a5"
PAGE_SIZE = 20


def _cid(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"messaging:[A-Za-z0-9!_-]{1,128}", value
    ):
        raise FamiliekilderError("Velg en messaging-cid fra Hoopits chatliste")
    return value


def _rows(value, limit):
    if (
        not isinstance(value, list)
        or len(value) > limit
        or any(not isinstance(r, dict) for r in value)
    ):
        raise SourceUnavailable("Hoopit-chat returnerte ufullstendig datastruktur")
    return value


class Client:
    def __init__(self, core):
        self.core = core
        self.transport = ApiTransport(
            allowed_hosts={"chat.stream-io-api.com"},
            timeout=45,
            max_json_bytes=8 * 1024 * 1024,
        )
        self.token = None
        self.user_id = None
        self.renewed = False
        self._session()

    def _session(self):
        value = self.core.get("/app/users/current/tokens/")
        try:
            user = identifier(value["uuid"])
            token = value["stream_chat_token"]
            if (
                not isinstance(token, str)
                or len(token) > 65536
                or any(ord(c) < 33 or ord(c) > 126 for c in token)
            ):
                raise ValueError()
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError()
            claims = json.loads(
                base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
            )
            if claims.get("user_id") != user or (
                self.user_id is not None and self.user_id != user
            ):
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceUnavailable(
                "Hoopit mangler en entydig brukerbundet chatøkt"
            ) from None
        # This checks identity binding, not the JWT signature. Stream verifies it.
        self.user_id, self.token = user, token

    def _get(self, path, payload=None, *, allow_missing=False, parameters=None):
        from .sync_snapshot import memo_read

        return memo_read(
            getattr(self.core, 'root', None), 'hoopit',
            ('chat-GET', path, payload, allow_missing, parameters),
            lambda: self._get_live(path, payload, allow_missing=allow_missing, parameters=parameters),
        )

    def _get_live(self, path, payload=None, *, allow_missing=False, parameters=None):
        for attempt in range(2):
            query = {"api_key": PUBLIC_API_KEY, "user_id": self.user_id}
            if parameters:
                if set(parameters) - {"limit", "id_lt"}:
                    raise FamiliekilderError("Ukjente chatparametre")
                query.update(parameters)
            if payload is not None:
                query["payload"] = json.dumps(payload, separators=(",", ":"))
            self.core._begin()
            try:
                result = self.transport.get_json(
                    CHAT_ROOT + path + "?" + urlencode(query),
                    headers={"Authorization": self.token, "stream-auth-type": "jwt"},
                )
                self.core.limiter.record_success()
                return result
            except ApiHTTPError as exc:
                if exc.status_code == 404 and allow_missing:
                    return None
                if exc.status_code == 401 and not self.renewed and attempt == 0:
                    self.renewed = True
                    self._session()
                    continue
                if exc.status_code == 400 and path == '/search' and payload and payload.get('next'):
                    raise SearchCursorRejected('Hoopit avviste sidemarkøren; søket må starte på nytt') from None
                self.core._failure(exc, authenticated_origin=False)
                raise SourceUnavailable("Hoopit-chat avviste leseoppslaget") from None
            except (ApiTransportError, OSError, TimeoutError) as exc:
                self.core._failure(exc, authenticated_origin=False)
                raise SourceUnavailable("Hoopit-chat kunne ikke nås") from None
        raise SourceUnavailable("Hoopit-chat kunne ikke nås")

    def message(self, cid, message_id):
        _cid(cid)
        message_id = identifier(message_id)
        value = self._get("/messages/" + message_id, allow_missing=True)
        if value is None:
            return None
        message = value.get("message") if isinstance(value, dict) else None
        if (
            not isinstance(message, dict)
            or message.get("id") != message_id
            or message.get("cid") != cid
        ):
            raise SourceUnavailable("Hoopit returnerte feil chatmelding")
        return message

    def users(self, ids):
        """Resolve only already selected Hoopit UUIDs; never search a directory."""
        if not isinstance(ids, list) or len(ids) > 1000:
            raise FamiliekilderError("Maks 1000 chatidentiteter per oppslag")
        ids = [identifier(ident) for ident in ids]
        if len(ids) != len(set(ids)):
            raise FamiliekilderError("Chatidentitetene må være unike")
        result = {}
        for start in range(0, len(ids), 30):
            batch = ids[start : start + 30]
            value = self._get(
                "/users",
                {
                    "filter_conditions": {"id": {"$in": batch}},
                    "presence": False,
                    "limit": 30,
                    "offset": 0,
                },
            )
            rows = _rows(value.get("users") if isinstance(value, dict) else None, 30)
            for row in rows:
                ident = identifier(row.get("id"))
                if ident not in batch or ident in result:
                    raise SourceUnavailable(
                        "Hoopit-chat returnerte feil brukeridentitet"
                    )
                # Keep only fields used to validate identity and send eligibility.
                result[ident] = {
                    key: row.get(key)
                    for key in (
                        "id",
                        "hoopit_id",
                        "banned",
                        "shadow_banned",
                        "deleted_at",
                        "deactivated_at",
                        "blocked_user_ids",
                    )
                }
        return result

    def direct_channel(self, recipient):
        recipient = identifier(recipient)
        if recipient == self.user_id:
            raise FamiliekilderError("Velg en annen mottaker enn deg selv")
        value = self._get(
            "/channels",
            {
                "filter_conditions": {
                    "type": "messaging",
                    "member_count": 2,
                    "$and": [
                        {"members": {"$in": [self.user_id]}},
                        {"members": {"$in": [recipient]}},
                    ],
                },
                "sort": [{"last_message_at": -1}],
                "state": True,
                "watch": False,
                "presence": False,
                "message_limit": 0,
                "member_limit": 100,
                "limit": 2,
                "offset": 0,
            },
        )
        rows = _rows(value.get("channels") if isinstance(value, dict) else None, 2)
        if len(rows) > 1:
            raise SourceUnavailable(
                "Flere samtaler med samme mottaker; velg en CID og bruk chatsvar"
            )
        result = [self._channel(r) for r in rows]
        for channel in result:
            if not channel["members_complete"] or {
                r["id"] for r in channel["members"]
            } != {self.user_id, recipient}:
                raise SourceUnavailable("Hoopit returnerte feil direktemottakere")
        return result[0] if result else None

    def channels(self, offset=0, cid=None, *, allow_missing=False):
        if type(offset) is not int or not 0 <= offset <= 1000:
            raise FamiliekilderError("Chatforskyvning må være 0–1000")
        filters = {"type": "messaging", "members": {"$in": [self.user_id]}}
        if cid is not None:
            filters["cid"] = _cid(cid)
        response = self._get(
            "/channels",
            {
                "filter_conditions": filters,
                "sort": [{"last_message_at": -1}],
                "state": True,
                "watch": False,
                "presence": False,
                "message_limit": 300 if cid else 20,
                "member_limit": 100,
                "limit": 1 if cid else PAGE_SIZE,
                "offset": offset,
            },
        )
        if not isinstance(response, dict):
            raise SourceUnavailable("Hoopit-chat returnerte ugyldig kanalliste")
        rows = _rows(response.get("channels"), 1 if cid else PAGE_SIZE)
        result = [self._channel(r) for r in rows]
        if len({r["reference"]["cid"] for r in result}) != len(result):
            raise SourceUnavailable("Hoopit-chat returnerte dupliserte samtaler")
        if cid is not None and (
            (not result and not allow_missing)
            or (result and result[0]["reference"]["cid"] != cid)
        ):
            raise SourceUnavailable("Chatten finnes ikke i dine tilgjengelige samtaler")
        return result

    def _channel(self, state):
        channel = state.get("channel")
        if not isinstance(channel, dict):
            raise SourceUnavailable("Hoopit-chat mangler kanaldata")
        cid = _cid(channel.get("cid"))
        if channel.get("type") != "messaging" or cid != "messaging:" + str(
            channel.get("id")
        ):
            raise SourceUnavailable("Hoopit-chat returnerte feil kanalidentitet")
        members = []
        for member in _rows(state.get("members"), 100):
            user = member.get("user")
            if not isinstance(user, dict):
                raise SourceUnavailable("Hoopit-chat mangler medlemsidentitet")
            uid = identifier(user.get("id"))
            if member.get("user_id") is not None and member["user_id"] != uid:
                raise SourceUnavailable("Hoopit-chat har motstridende medlemsidentitet")
            members.append(
                {
                    "id": uid,
                    "name": user.get("name"),
                    "role": member.get("channel_role"),
                    "banned": member.get("banned"),
                }
            )
        ids = [m["id"] for m in members]
        membership = state.get("membership") or {}
        own = membership.get("user_id") or (membership.get("user") or {}).get("id")
        if len(ids) != len(set(ids)) or (
            self.user_id not in ids and own != self.user_id
        ):
            raise SourceUnavailable("Eget medlemskap i chatten er ikke bekreftet")
        count = channel.get("member_count")
        if type(count) is not int or count < len(members):
            raise SourceUnavailable("Hoopit-chat mangler pålitelig deltakerantall")
        capabilities = channel.get("own_capabilities")
        if not isinstance(capabilities, list) or any(
            not isinstance(c, str) for c in capabilities
        ):
            raise SourceUnavailable("Hoopit-chat mangler rettighetslisten")
        messages = []
        for message in _rows(state.get("messages"), 300):
            if message.get("cid") != cid:
                raise SourceUnavailable("Meldingen tilhører en annen Hoopit-chat")
            messages.append(message_summary(message, cid))
        if len({m["reference"]["id"] for m in messages}) != len(messages):
            raise SourceUnavailable("Hoopit-chat returnerte dupliserte meldinger")
        return {
            "reference": {"kind": "chat", "cid": cid},
            "name": channel.get("name"),
            "members": members,
            "member_count": count,
            "members_complete": len(members) == count,
            "own_capabilities": capabilities,
            "blocked": channel.get("blocked"),
            "disabled": channel.get("disabled"),
            "frozen": channel.get("frozen"),
            "max_message_length": (channel.get("config") or {}).get(
                "max_message_length"
            ),
            "messages": messages,
            "message_history_complete": False,
            "last_message_at": channel.get("last_message_at"),
            "guardian_monitoring_checked": channel.get("guardian_monitoring_checked"),
            "limitations": [
                "Siste 20/300 meldinger. Bruk hoopit_search_chat for eldre indekserte meldinger og hoopit_chat_replies for trådsvar. Vedlegg er ikke lest.",
                "Foreldreinnsyn kan gjelde etter Hoopits regler; deltakerlisten alene er ikke et løfte om privat synlighet.",
            ],
        }


def message_summary(message, cid):
    if not isinstance(message, dict) or message.get("cid") != cid:
        raise SourceUnavailable("Meldingen tilhører en annen Hoopit-chat")
    ident = identifier(message.get("id"))
    author = message.get("user") or {}
    deleted = bool(message.get("deleted_at")) or message.get("type") == "deleted"
    return {
        "reference": {"kind": "chat_message", "cid": cid, "id": ident},
        "author_id": identifier(author.get("id")),
        "author_name": author.get("name"),
        "text": None if deleted else message.get("text"),
        "type": message.get("type"),
        "created_at": message.get("created_at"),
        "parent_id": message.get("parent_id"),
        "reply_count": message.get("reply_count"),
        "has_unread_attachment": not deleted and bool(message.get("attachments")),
        "deleted": deleted,
    }


def read(runtime, offset=0, cid=None):
    runtime.config.require("hoopit")
    if type(offset) is not int or not 0 <= offset <= 1000:
        raise FamiliekilderError("Chatforskyvning må være 0–1000")
    if cid is not None:
        _cid(cid)
    with provider(runtime.config.root, "hoopit") as (_, core):
        client = Client(core)
        channels = client.channels(offset, cid)
        next_offset = offset + len(channels)
        more = cid is None and len(channels) == PAGE_SIZE
        return {
            "source": "hoopit",
            "fetched_at": now(),
            "source_content_is_untrusted": True,
            "chats": channels,
            "chat_list_complete": not more,
            "next_offset": next_offset if more and next_offset <= 1000 else None,
            "offset_limit_reached": more and next_offset > 1000,
            "chat_actions": {
                "reply_chat": "implemented",
                "send_message": "implemented",
            },
            "limitations": [
                "Offsetpaginering kan påvirkes av samtidige endringer i kilden."
            ],
        }


def register(server, runtime):
    from .source_server import READ, safe_tool
    from .live_hoopit_recipients import register as register_recipients
    from .live_hoopit_history import register as register_history

    def hoopit_list_chats(offset: int = 0) -> dict:
        """List egne Hoopit-chatter direkte, 20 per side. Følg next_offset og les dekningsgrensene. Bare siste meldinger; ingen opprettelse, lest-markering eller websocket."""
        return read(runtime, offset)

    def hoopit_get_chat(cid: str) -> dict:
        """Les eksisterende Hoopit-chat fra listeverktøyet, med deltakere og rettighetsflagg. Siste 300 meldinger, opptil 100 medlemmer. Sjekk members_complete og begrensningene. Ingen opprettelse eller lest-markering."""
        return read(runtime, cid=cid)

    for tool in (hoopit_list_chats, hoopit_get_chat):
        server.add_tool(safe_tool(tool), annotations=READ)
    register_recipients(server, runtime)
    register_history(server, runtime)
