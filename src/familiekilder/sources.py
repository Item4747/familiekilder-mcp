"""One entry per family service. Plain data: no provider imports on load.

To add a service, write its modules and add one `Source` below:

- `auth`: module with `auth_status`, `refresh_session`, `logout` and the
  login functions its `auth_commands` need (see `auth_cli.py`).
- `reader`: module with
  `connect(root)`: context manager yielding `(module, client)`,
  `list_items(root, m, c, since, *, include_detail_hash)` -> `(items, complete)`,
  `get_item(root, m, c, reference)` -> item,
  `download(root, m, c, item, attachment_id, reference, directory)` -> `(path, mime)`.
  Optional: `list_notifications(m, c, cutoff)` when `notifications` is set,
  `discover_chats(runtime, since, until, *, observe=None)` when `streams`
  has "chat", `discover_feed(runtime, since, until, *, observe=None)`
  (`observe(conn, source, stream, item, stamp)` records a page's items in
  its checkpoint transaction; see `chat_discovery.py`), `read_scope(root)` and
  `reference_values(reference)` (see `live_readers.stored_reference`).
- `actions`: modules with `register(server, runtime)`, registered in order.

`live_readers.py` has the shared helpers a reader uses (`ref_item`,
`attachment_metadata`, `checked_detail`, `fetch_attachment`).

`web_script()` renders `label`, `letter`, `description` and `kind` as
`/services.js` for a web front end.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass

COMMON_ACTIONS = ("send_message", "reply_message", "register_absence", "set_participation")


@dataclass(frozen=True)
class Source:
    name: str
    label: str
    auth: str
    reader: str
    # Shown in the web pages: the mark on the service's badge and a short
    # Norwegian description in the service lists.
    letter: str = ""
    description: str = ""
    # Groups the service lists: "school", "sport", "mail" or "other".
    kind: str = "other"
    actions: tuple[str, ...] = ()
    # Action categories with verified wire contracts, and implemented actions.
    verified: frozenset[str] = frozenset()
    implemented: frozenset[str] = frozenset()
    # Actions offered besides COMMON_ACTIONS; False means read-only (Gmail).
    extra_actions: tuple[str, ...] = ()
    writes: bool = True
    auth_commands: frozenset[str] = frozenset()
    logout_revokes: bool = False
    login: str = "browser"
    streams: tuple[str, ...] = ("feed",)
    # The feed holds notifications that are read from the list only.
    notifications: bool = False
    # Local request pacing and cooldowns in `live_traffic`.
    paced: bool = False
    # "login_30_days", "refresh_deadline", "long_lived" or "google".
    session_estimate: str = "login_30_days"
    # Part of signed native provider bundles (`provider_bundle.py`).
    provider_bundle: bool = True
    password_origins: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


_OAUTH_COMMANDS = frozenset(
    {"browser-login", "configure", "login", "refresh", "status", "logout"}
)
_BOOTSTRAP_COMMANDS = frozenset(
    {"browser-bootstrap", "configure", "import-session", "refresh", "status", "logout"}
)

REGISTRY = {
    source.name: source
    for source in (
        Source(
            "vigilo", "Vigilo", "vigilo_auth", "live_vigilo_reader",
            letter="V", description="Beskjeder fra barnehage og skole",
            kind="school",
            actions=("live_vigilo", "live_vigilo_meetings",
                     "live_vigilo_registrations", "live_vigilo_pickup"),
            verified=frozenset({"messages", "absence", "participation"}),
            implemented=frozenset({"send_message", "reply_message", "register_absence",
                                   "set_participation", "set_registration"}),
            extra_actions=("set_registration",),
            auth_commands=_OAUTH_COMMANDS,
            logout_revokes=True,
            paced=True,
        ),
        Source(
            "skolemelding", "Skolemelding", "skolemelding_auth", "live_skolemelding_reader",
            letter="S", description="Meldinger fra Osloskolen",
            kind="school",
            actions=("live_skolemelding", "live_skolemelding_messages",
                     "live_skolemelding_absence"),
            verified=frozenset({"messages", "absence"}),
            implemented=frozenset({"send_message", "report_absence"}),
            extra_actions=("report_absence",),
            auth_commands=_OAUTH_COMMANDS,
            paced=True,
        ),
        Source(
            "spond", "Spond", "spond_auth", "live_spond_reader",
            letter="s", description="Aktiviteter, grupper og beskjeder",
            kind="sport",
            actions=("live_spond_messages", "live_spond_chat",
                     "live_spond_chat_actions", "live_spond_participation"),
            verified=frozenset({"participation", "messages"}),
            implemented=frozenset({"set_participation", "reply_message",
                                   "send_message", "reply_chat"}),
            extra_actions=("reply_chat",),
            auth_commands=_BOOTSTRAP_COMMANDS,
            streams=("feed", "chat"),
            notifications=True,
            session_estimate="refresh_deadline",
            password_origins=("https://spond.com",),
        ),
        Source(
            "hoopit", "Hoopit", "hoopit_auth", "live_hoopit_reader",
            letter="h", description="Idrett, arrangementer og chat",
            kind="sport",
            actions=("live_hoopit", "live_hoopit_chat", "live_hoopit_chat_actions"),
            verified=frozenset({"participation", "messages"}),
            implemented=frozenset({"set_participation", "reply_message",
                                   "reply_chat", "send_message"}),
            extra_actions=("reply_chat",),
            auth_commands=_BOOTSTRAP_COMMANDS | {"verify-direct"},
            streams=("feed", "chat"),
            notifications=True,
            session_estimate="long_lived",
            password_origins=("https://app.hoopit.io",),
        ),
        Source(
            "ist-home", "IST Home", "ist_oauth", "live_ist_reader",
            letter="ist", description="Innlegg og samtaler fra barnehagen",
            kind="school",
            actions=("live_ist", "live_ist_bookings"),
            verified=frozenset({"messages", "absence", "participation"}),
            implemented=frozenset({"send_message", "reply_message",
                                   "register_absence", "set_participation"}),
            auth_commands=frozenset(
                {"browser-login", "configure", "login", "refresh", "status", "logout"}),
            logout_revokes=True,
            streams=("feed", "chat"),
            paced=True,
            aliases=("ist_home", "ist home", "isthome"),
        ),
        # Email is searched for specific questions, never listed wholesale.
        Source(
            "gmail", "Gmail", "gmail_auth", "live_gmail_reader",
            letter="@", description="Supplerende informasjon på e-post",
            kind="mail",
            writes=False,
            login="google",
            streams=(),
            session_estimate="google",
            provider_bundle=False,
        ),
    )
}


def web_script() -> str:
    """`/services.js`: labels, letters, descriptions and kinds for the web pages."""
    services = REGISTRY.values()
    catalog = {
        "labels": {s.name: s.label for s in services},
        "letters": {s.name: s.letter for s in services},
        "descriptions": {s.name: s.description for s in services},
        "kinds": {s.name: s.kind for s in services},
        "readOnly": [s.name for s in services if not s.writes],
    }
    return ("'use strict';\n// Generated from REGISTRY in familiekilder/sources.py.\n"
            f"globalThis.Services = {json.dumps(catalog, ensure_ascii=False, indent=2)};\n")


def module(name: str):
    """Import a service module named in the registry, on first use only."""
    return importlib.import_module(f"{__package__}.{name}")


def reader(source: str):
    return module(REGISTRY[source].reader)
