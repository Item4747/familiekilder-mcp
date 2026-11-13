from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .core import FamiliekilderError, html_to_text_unescaped, parse_iso
from .live_traffic import paced_request
from .network_usage import http_attempt
from .private_state import (
    ensure_private_directory,
)


ENDPOINT = "https://miniforum-api.home.ist.com/v2/school?env=prod-home&version=2.14.0&locale=nb"
PAGE_SIZE = 50
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_RUN_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_GRAPHQL_BYTES = 25 * 1024 * 1024
MAX_POSTS = 5_000
# One collection found more posts than MAX_POSTS allows.
TOO_MANY_POSTS = "IST Home returnerte flere Innlegg enn innhentingsgrensen"
MAX_PAGES = 200

CONTEXT_QUERY = """
query PostContext {
  getContext {
    serverTime
    children {
      id
      firstName
      lastName
      placements { id }
    }
  }
}
"""

POSTS_QUERY = """
query GetBlogPosts($input: GetBlogPostsInput!) {
  getBlogPosts(input: $input) {
    page { startCursor endCursor hasNextPage }
    minRetentionTime
    nodes {
      id
      publishedAt
      editedAt
      title
      content
      read
      pinned
      categoryId
      scope
      placements { id }
      publishedBy { id firstName lastName }
      files { id name url mimeType }
      images
    }
  }
}
"""


class IstHomeAuthError(FamiliekilderError):
    """The local IST session is absent, invalid, expired, or rejected."""


class IstHomeFatalConfigurationError(IstHomeAuthError):
    """The provisioned IST app registration was rejected; stop using it."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class DownloadBudget:
    def __init__(self, limit: int = MAX_RUN_DOWNLOAD_BYTES) -> None:
        self.limit = limit
        self.used = 0

    def consume(self, amount: int) -> None:
        if amount < 0 or self.used + amount > self.limit:
            raise FamiliekilderError("IST-vedlegg overskred samlet nedlastingsgrense")
        self.used += amount




def _credentials(root: Path) -> dict[str, Any]:
    from .ist_oauth import (
        OAuthError,
        OAuthFatalConfigurationError,
        OAuthReauthenticationRequired,
        OAuthRefreshUncertain,
        REGISTRATION_REJECTED,
        local_session_for_use,
    )

    value: dict[str, Any] | None = None
    fatal = False
    invalid = False
    try:
        value = local_session_for_use(root)
    except OAuthFatalConfigurationError:
        fatal = True
    except OAuthRefreshUncertain:
        raise
    except (OAuthReauthenticationRequired, OAuthError):
        invalid = True
    if fatal:
        raise IstHomeFatalConfigurationError(REGISTRATION_REJECTED)
    if invalid or value is None:
        raise IstHomeAuthError(
            "IST Home-økten mangler eller er ugyldig; logg inn eller importer en ny appøkt"
        )
    return value


def import_app_session(
    root: Path,
    *,
    replace_existing: bool = False,
    transport: Any = None,
    access_token_validator: Any = None,
    at: float | None = None,
) -> dict[str, Any]:
    """Read the official Mac app session and route it through live validation."""

    async_storage = (
        Path.home()
        / "Library/Containers/com.ist.home.miniforum/Data/Library/Application Support"
        / "com.ist.home.miniforum/RCTAsyncLocalStorage_V1"
    )
    credentials: dict[str, Any] | None = None
    failed = False
    try:
        manifest = json.loads((async_storage / "manifest.json").read_text(encoding="utf-8"))
        inline = manifest["_skolid_sdk_sessions"]
        if isinstance(inline, str):
            sessions = json.loads(inline)
        elif inline is None:
            filename = hashlib.md5(b"_skolid_sdk_sessions").hexdigest()
            sessions = json.loads((async_storage / filename).read_text(encoding="utf-8"))
        else:
            raise ValueError("unknown AsyncStorage representation")
        candidates = [session for session in sessions.values() if session.get("state", {}).get("access_token")]
        selected = max(candidates, key=lambda session: float(session["state"]["issued_at"]))
        state = selected["state"]
        config = selected["config"]
        credentials = {
            "access_token": state["access_token"],
            "refresh_token": state["refresh_token"],
            "client_secret": config["clientSecret"],
            "client_id": config["clientId"],
            "scope": config["scope"],
            "authority": config["authority"],
            "redirect_uri": config["redirectUri"],
            "issued_at": state["issued_at"],
            "expires_in": state["expires_in"],
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        failed = True
    if failed or credentials is None:
        raise FamiliekilderError("Fant ingen brukbar IST Home-appøkt")
    from .ist_oauth import import_session

    return import_session(
        root,
        credentials,
        transport=transport,
        access_token_validator=access_token_validator,
        at=at,
        replace_existing=replace_existing,
    )


def _jwt_exp(token: str) -> float:
    expiry: float | None = None
    failed = False
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        expiry = float(payload["exp"])
    except (IndexError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        failed = True
    if failed or expiry is None:
        raise IstHomeAuthError("IST Home access-tokenet har ukjent format; logg inn på nytt")
    return expiry


def refresh_token(root: Path, *, at: float | None = None) -> str:
    from .ist_oauth import (
        OAuthError,
        OAuthFatalConfigurationError,
        OAuthReauthenticationRequired,
        OAuthRefreshUncertain,
        REGISTRATION_REJECTED,
        refresh_session,
    )

    fatal = False
    reauthentication_required = False
    invalid = False
    try:
        refresh_session(root, at=at)
    except OAuthFatalConfigurationError:
        fatal = True
    except OAuthRefreshUncertain:
        raise
    except OAuthReauthenticationRequired:
        reauthentication_required = True
    except OAuthError:
        invalid = True
    if fatal:
        raise IstHomeFatalConfigurationError(REGISTRATION_REJECTED)
    if reauthentication_required:
        raise IstHomeAuthError(
            "IST Home-refresh er avvist eller har ukjent resultat; logg inn på nytt"
        )
    if invalid:
        raise IstHomeAuthError(
            "IST Home-økten kan ikke fornyes; logg inn eller importer økten på nytt"
        )
    refreshed = _credentials(root).get("access_token")
    if not isinstance(refreshed, str) or not refreshed:
        raise IstHomeAuthError("IST Home-økten er ufullstendig; logg inn på nytt")
    return refreshed


def access_token(root: Path, *, now: float | None = None) -> str:
    token = str(_credentials(root).get("access_token") or "")
    timestamp = datetime.now(UTC).timestamp() if now is None else now
    if _jwt_exp(token) <= timestamp + 60:
        return refresh_token(root, at=now)
    return token


def graphql(token: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    # GraphQL reads use POST too; mutations never enter the sync snapshot.
    if re.match(r'^\s*query\b', query) is None:
        return _graphql(token, query, variables)
    from .sync_snapshot import memo_read

    return memo_read(None, 'ist-home', ('query', ENDPOINT, query, variables or {}),
                     lambda: _graphql(token, query, variables))


@paced_request
def _graphql(token: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = json.dumps(
        {"query": query, "variables": variables or {}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    request = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept-Language": "nb-NO",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    http_status: int | None = None
    retry_after = None
    network_failed = False
    try:
        http_attempt()
        with opener.open(request, timeout=45) as response:
            raw = response.read(MAX_GRAPHQL_BYTES + 1)
    except urllib.error.HTTPError as exc:
        from .api_transport import _retry_after_seconds

        http_status = int(exc.code)
        retry_after = _retry_after_seconds(exc.headers.get("Retry-After"))
        exc.close()
    except (urllib.error.URLError, TimeoutError):
        network_failed = True
    if http_status in {401, 403}:
        raise IstHomeAuthError("IST Home krever ny innlogging")
    if http_status is not None:
        from .api_transport import ApiHTTPError

        raise ApiHTTPError(http_status, retry_after_seconds=retry_after)
    if network_failed:
        raise FamiliekilderError("IST Home svarte ikke med gyldige data")
    if len(raw) > MAX_GRAPHQL_BYTES:
        raise FamiliekilderError("IST Home returnerte for mye data")
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        result = None
    if result is None:
        raise FamiliekilderError("IST Home svarte ikke med gyldige data")
    if not isinstance(result, dict):
        raise FamiliekilderError("IST Home returnerte ukjent responsformat")
    errors = result.get("errors") or []
    if errors:
        auth_codes = {"UNAUTHENTICATED", "UNAUTHORIZED", "FORBIDDEN"}
        codes = {
            str((error.get("extensions") or {}).get("code", "")).upper()
            for error in errors
            if isinstance(error, dict) and isinstance(error.get("extensions"), dict)
        }
        if codes & auth_codes:
            raise IstHomeAuthError("IST Home krever ny innlogging")
        raise FamiliekilderError("IST Home returnerte en API-feil")
    from .api_transport import api_call_completed
    api_call_completed()
    return result.get("data") or {}


def fetch_snapshot(token: str) -> dict[str, Any]:
    context = graphql(token, CONTEXT_QUERY)["getContext"]
    children = context.get("children") or []
    child_ids = [child["id"] for child in children]
    posts: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    min_retention_time: float | None = None

    pages = 0
    while True:
        pages += 1
        if pages > MAX_PAGES:
            raise FamiliekilderError("IST Home returnerte for mange sider med Innlegg")
        page: dict[str, Any] = {"next": PAGE_SIZE}
        if cursor:
            page["cursor"] = cursor
        data = graphql(
            token,
            POSTS_QUERY,
            {
                "input": {
                    "childIds": child_ids,
                    "page": page,
                    "options": {"filterIds": []},
                }
            },
        )["getBlogPosts"]
        nodes = data.get("nodes") or []
        if not isinstance(nodes, list):
            raise FamiliekilderError("IST Home returnerte ugyldige Innlegg")
        posts.extend(nodes)
        if len(posts) > MAX_POSTS:
            raise FamiliekilderError(TOO_MANY_POSTS)
        min_retention_time = data.get("minRetentionTime", min_retention_time)
        page_info = data.get("page") or {}
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor in seen_cursors:
            raise FamiliekilderError("IST Home returnerte ugyldig sideinndeling for Innlegg")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return {
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "server_time": context.get("serverTime"),
        "children": children,
        "posts": posts,
        "min_retention_time": min_retention_time,
    }



def _safe_filename(value: str, fallback: str) -> str:
    name = Path(value).name.strip() or fallback
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name[:180] or fallback


def _validated_public_https_target(
    url: str,
) -> tuple[urllib.parse.SplitResult, str, int, list[tuple[int, int, int, tuple[Any, ...]]]]:
    if (
        not isinstance(url, str)
        or not url
        or len(url) > 8192
        or any(ord(character) < 33 or ord(character) == 127 for character in url)
    ):
        raise FamiliekilderError("IST-vedlegget har en ugyldig URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FamiliekilderError("IST-vedlegget har en ugyldig URL") from None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise FamiliekilderError("IST-vedlegget bruker ikke en trygg HTTPS-URL")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii")
        addresses = socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise FamiliekilderError("IST-vedleggets vertsnavn kunne ikke bekreftes") from None
    validated_addresses: list[tuple[int, int, int, tuple[Any, ...]]] = []
    for family, kind, proto, _canonical, sockaddr in addresses:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            raise FamiliekilderError("IST-vedleggets vertsadresse er ugyldig") from None
        if not address.is_global:
            raise FamiliekilderError("IST-vedlegget peker ikke til en offentlig vertsadresse")
        validated_addresses.append((family, kind, proto, sockaddr))
    if not validated_addresses:
        raise FamiliekilderError("IST-vedleggets vertsadresse mangler")
    return parsed, hostname, port or 443, validated_addresses


def _open_pinned_https(
    url: str,
) -> tuple[http.client.HTTPSConnection, http.client.HTTPResponse]:
    parsed, hostname, port, addresses = _validated_public_https_target(url)
    tls_context = ssl.create_default_context()
    connection: http.client.HTTPSConnection | None = None
    for family, kind, proto, sockaddr in addresses:
        raw_socket: socket.socket | None = None
        try:
            raw_socket = socket.socket(family, kind, proto)
            raw_socket.settimeout(60)
            raw_socket.connect(sockaddr)
            tls_socket = tls_context.wrap_socket(raw_socket, server_hostname=hostname)
            raw_socket = None
            connection = http.client.HTTPSConnection(
                hostname, port=port, timeout=60, context=tls_context
            )
            connection.sock = tls_socket
            peer = ipaddress.ip_address(tls_socket.getpeername()[0])
            if not peer.is_global:
                connection.close()
                connection = None
                continue
            request_target = urllib.parse.urlunsplit(
                ("", "", parsed.path or "/", parsed.query, "")
            )
            http_attempt()
            connection.request(
                "GET", request_target, headers={"User-Agent": "familiekilder/0.1"}
            )
            response = connection.getresponse()
            return connection, response
        except (OSError, ssl.SSLError, http.client.HTTPException):
            if connection is not None:
                connection.close()
                connection = None
            if raw_socket is not None:
                raw_socket.close()
    raise FamiliekilderError("IST-vedleggets HTTPS-vert kunne ikke bekreftes")


@paced_request
def download(url: str, target: Path, budget: DownloadBudget | None = None) -> None:
    from .api_transport import ApiHTTPError, _retry_after_seconds, save_download_response

    selected_budget = budget or DownloadBudget()
    ensure_private_directory(target.parent)
    connection, response = _open_pinned_https(url)
    try:
        if response.status != 200:
            raise ApiHTTPError(
                response.status,
                retry_after_seconds=_retry_after_seconds(response.headers.get("Retry-After")),
            )
        save_download_response(response, target, max_bytes=MAX_ATTACHMENT_BYTES,
                               consume=selected_budget.consume)
    finally:
        connection.close()


def _post_children(post: dict[str, Any], children: list[dict[str, Any]]) -> str | None:
    placement_ids = {placement["id"] for placement in post.get("placements") or []}
    names = []
    for child in children:
        child_placements = {placement["id"] for placement in child.get("placements") or []}
        if placement_ids & child_placements:
            names.append(" ".join(part for part in (child.get("firstName"), child.get("lastName")) if part))
    return ", ".join(name for name in names if name) or None


def normalize_post(post, children, incoming=None, cache=None):
    from .live_readers import attachment_metadata
    if incoming is not None or cache is not None:
        raise FamiliekilderError("Persistente vedleggscacher er avviklet")
    specs = list(post.get("files") or []) + [
        {"url": u, "name": f"bilde-{i}.jpg"}
        for i, u in enumerate(post.get("images") or [], 1)
    ]
    return post_metadata(post, children, [attachment_metadata(v) for v in specs])


def post_metadata(post, children, attachments):
    post_id = str(post["id"])
    publisher = post.get("publishedBy") or {}
    sender = " ".join(part for part in (publisher.get("firstName"), publisher.get("lastName")) if part) or None
    return {
        "source": "ist-home",
        "source_id": post_id,
        "thread_id": post_id,
        "child": _post_children(post, children),
        "sender": sender,
        "published_at": post.get("publishedAt"),
        "changed_at": post.get("editedAt"),
        "title": post.get("title") or "IST-innlegg",
        "body": html_to_text_unescaped(post.get("content")) or "(Innlegget har ingen tekst)",
        "type": "announcement",
        "deadline": None,
        "event_start": None,
        "event_end": None,
        "location": None,
        "action_required": None,
        "source_url": None,
        "topic": "IST Innlegg",
        "date_note": None,
        "attachments": attachments,
        "missing_attachments": [],
    }


def posts_in_window(posts: list[dict[str, Any]], cutoff_at: str) -> list[dict[str, Any]]:
    cutoff = datetime.fromisoformat(cutoff_at)
    selected: list[dict[str, Any]] = []
    for post in posts:
        timestamps = [
            datetime.fromisoformat(value)
            for field in ("publishedAt", "editedAt")
            if (value := parse_iso(post.get(field), field)) is not None
        ]
        # Unknown age must never turn a bounded onboarding into arbitrary history.
        if timestamps and max(timestamps) >= cutoff:
            selected.append(post)
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Importer en verifisert IST Home-økt")
    parser.add_argument("--root")
    parser.add_argument("--import-session", action="store_true")
    # How the consent text names the program that uses the session
    # (source_cli.Program.name for the command line).
    parser.add_argument("--program-name", default="familiekilder-mcp", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        root = Path(args.root).resolve()
        if args.import_session:
            from .ist_oauth import consent_status, record_consent, session_path

            replace_existing = False
            if session_path(root).exists():
                print(
                    "En lokal IST Home-økt finnes allerede og beholdes ved valideringsfeil."
                )
                if input("Skriv ERSTATT for å importere en nyere Mac-økt: ") != "ERSTATT":
                    raise FamiliekilderError(
                        "Importen ble avbrutt; eksisterende økt er beholdt"
                    )
                replace_existing = True
            if not consent_status(root)["current"]:
                print(
                    "IST OAuth-piloten er uoffisiell. Tokenet kan ha bredere IST-rettigheter "
                    f"enn {args.program_name} bruker."
                )
                if input("Skriv JEG FORSTÅR for å fortsette: ") != "JEG FORSTÅR":
                    raise FamiliekilderError("Importen ble avbrutt uten samtykke")
                record_consent(root)
            print(
                json.dumps(
                    import_app_session(root, replace_existing=replace_existing),
                    ensure_ascii=False,
                )
            )
            return 0
        parser.error("--import-session er påkrevd")
    except (FamiliekilderError, OSError) as exc:
        print(f"import-ist-session: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
