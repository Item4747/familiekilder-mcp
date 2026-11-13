"""Fresh Stream messages/threads and lazy, unauthenticated attachment reads."""

from pathlib import Path
import mimetypes
from urllib.parse import urlsplit, unquote

from .api_transport import ApiHTTPError, ApiTransport, ApiTransportError, DownloadResult
from .core import FamiliekilderError
from .live_hoopit_chat import Client, _cid, _rows, message_summary
from .live_hoopit_history import _scope, _time
from .live_readers import identifier, attachment_metadata
from .source_adapter import SourceUnavailable

PAGE_SIZE = 100
MAX_PAGES = 10
MAX_QUOTES = 10


def attachment_url(core, url):
    try:
        host = (urlsplit(url).hostname or "").lower()
    except (TypeError, ValueError, AttributeError):
        raise SourceUnavailable("Hoopit-chat har ugyldig vedleggsadresse") from None
    # Stream documents region-specific, changing subdomains. Pin the exact
    # observed host for transport only after checking the owned DNS suffix.
    hosts = core.access.attachment_hosts
    if host.endswith(".stream-io-cdn.com"):
        hosts = (*hosts, host)
    return core._attachment_url(url, hosts)


def download(core, url, target, max_bytes):
    url = attachment_url(core, url)
    host = urlsplit(url).hostname.lower()
    if not host.endswith(".stream-io-cdn.com"):
        return core.download(url, target, max_bytes=max_bytes)
    transport = ApiTransport(
        allowed_hosts={host}, timeout=45, max_download_bytes=max_bytes
    )
    core._begin()
    try:
        result = transport.download(
            url, target, bearer_token=None, headers=None, max_bytes=max_bytes
        )
    except (ApiHTTPError, ApiTransportError, OSError, TimeoutError) as exc:
        core._failure(exc, authenticated_origin=False)
        raise SourceUnavailable("Hoopit-chatvedlegget kunne ikke lastes ned") from None
    if not isinstance(result, DownloadResult):
        raise SourceUnavailable("Hoopit-chatvedlegget returnerte ugyldig metadata")
    core.limiter.record_success()
    return result


def _normalize(row, cid, core):
    summary = message_summary(row, cid)
    _time(row.get("created_at"))
    summary["updated_at"] = row.get("updated_at")
    summary["quoted_reference"] = None
    attachments, limitations, links = [], [], []
    if summary["deleted"]:
        limitations.append("En melding er slettet; innholdet kan ikke bekreftes.")
    else:
        if row.get("type") not in {"regular", "reply", "system"}:
            limitations.append("Ukjent meldingstype er ikke fullt tolket.")
        quote_id = row.get("quoted_message_id")
        if quote_id:
            summary["quoted_reference"] = {
                "kind": "chat_message",
                "cid": cid,
                "id": identifier(quote_id),
            }
        for field in ("poll_id", "poll", "shared_location", "mentioned_channel"):
            if row.get(field):
                limitations.append("Meldingen har innhold som ikke er tolket: " + field)
        for index, attachment in enumerate(_rows(row.get("attachments"), 100)):
            if attachment.get("og_scrape_url"):
                links.append(
                    {
                        "url": attachment["og_scrape_url"],
                        "title": attachment.get("title"),
                        "text": attachment.get("text"),
                    }
                )
                limitations.append(
                    "Lenkeforhåndsvisning er vist, men lenkens innhold er ikke lest."
                )
                continue
            kind = attachment.get("type")
            url = attachment.get("asset_url") or attachment.get("image_url")
            if kind not in {
                "image",
                "file",
                "video",
                "audio",
                "voiceRecording",
            } or not isinstance(url, str):
                limitations.append(
                    "Ukjent eller ufullstendig chatvedlegg er ikke lest."
                )
                continue
            try:
                parsed = urlsplit(url)
            except ValueError:
                limitations.append("Et chatvedlegg har ugyldig URL og er ikke lest.")
                continue
            try:
                attachment_url(core, url)
                supported = True
            except FamiliekilderError:
                supported = False
                limitations.append(
                    "Et vedlegg peker til en ukjent eller ugyldig vedleggsvert."
                )
            name = attachment.get("title") or attachment.get("name")
            if not isinstance(name, str) or not name:
                name = Path(unquote(parsed.path)).name or "vedlegg"
            # Signing parameters rotate. Identity uses the source path, owning
            # message and ordinal, so refreshed signatures do not change IDs.
            spec = {
                "id": row["id"]
                + ":"
                + str(index)
                + ":"
                + str(attachment.get("id") or "")
                + ":"
                + parsed.netloc
                + parsed.path,
                "name": name,
                "url": url,
                "mimeType": attachment.get("mime_type")
                or mimetypes.guess_type(parsed.path)[0],
            }
            metadata = attachment_metadata(spec)
            metadata.update(
                message_reference=summary["reference"],
                attachment_type=kind,
                download_supported=supported,
            )
            attachments.append(metadata)
            if kind in {"video", "audio", "voiceRecording"}:
                limitations.append(
                    "Lyd/video er ikke tolket av tekst- og bildebehandlingen."
                )
    summary.update(
        attachment_ids=[a["attachment_id"] for a in attachments], linked_previews=links
    )
    return summary, attachments, limitations


def _thread(client, cid, root):
    count = root.get("reply_count")
    if type(count) is not int or count < 0:
        raise SourceUnavailable("Hoopit mangler svarantall for oppfølgingsgrunnlaget")
    if count == 0:
        return [], True
    replies, seen, before = [], set(), None
    boundary_time = None
    for _ in range(MAX_PAGES):
        parameters = {"limit": PAGE_SIZE}
        if before is not None:
            parameters["id_lt"] = before
        value = client._get(
            "/messages/" + root["id"] + "/replies", parameters=parameters
        )
        page = _rows(
            value.get("messages") if isinstance(value, dict) else None, PAGE_SIZE
        )
        stamps = []
        for row in page:
            ident = identifier(row.get("id"))
            if (
                row.get("cid") != cid
                or row.get("parent_id") != root["id"]
                or ident in seen
                or ident == root["id"]
            ):
                raise SourceUnavailable(
                    "Uventet chat-/trådkobling eller dupliserte svar"
                )
            stamp = _time(row.get("created_at"))
            if boundary_time is not None and stamp > boundary_time:
                raise SourceUnavailable(
                    "Hoopit returnerte trådsvar utenfor sidegrensen"
                )
            stamps.append(stamp)
            seen.add(ident)
        if stamps != sorted(stamps):
            raise SourceUnavailable("Hoopit returnerte svar i uventet rekkefølge")
        replies = page + replies
        if len(page) < PAGE_SIZE:
            return replies, len(replies) == count
        before, boundary_time = page[0]["id"], stamps[0]
    return replies, False


def get_message_item(module, core, reference):
    if (
        not isinstance(reference, dict)
        or set(reference) != {"kind", "cid", "id"}
        or reference.get("kind") != "chat_message"
    ):
        raise FamiliekilderError("Velg en meldingsreferanse fra Hoopits chatverktøy")
    cid, ident = _cid(reference["cid"]), identifier(reference["id"])
    client = Client(core)
    audience = _scope(client, cid)
    selected = client.message(cid, ident)
    if selected is None:
        raise SourceUnavailable(
            "Chatmeldingen finnes ikke; dette beviser ikke avlysning"
        )
    root = selected
    if selected.get("parent_id"):
        root = client.message(cid, identifier(selected["parent_id"]))
        if root is None or root.get("parent_id"):
            raise SourceUnavailable("Chatmeldingen mangler entydig hovedmelding")
    raw_replies, thread_complete = _thread(client, cid, root)
    if selected.get("parent_id") and ident not in {r["id"] for r in raw_replies}:
        # Preserve the selected message even if the bounded thread read
        # does not include this older reply; never call this complete.
        raw_replies.append(selected)
        thread_complete = False
    raw = [root, *raw_replies]
    summaries, attachments, limitations = {}, {}, []
    for row in raw:
        summary, files, errors = _normalize(row, cid, core)
        summaries[row["id"]] = summary
        attachments.update((a["attachment_id"], a) for a in files)
        limitations.extend(errors)
    root_after = client.message(cid, root["id"])
    if root_after is None:
        raise SourceUnavailable("Hovedmeldingen forsvant under lesing; hent på nytt")
    # Compare normalized metadata, not short-lived CDN signing parameters.
    after_summary, after_files, _ = _normalize(root_after, cid, core)
    if after_summary != summaries[root["id"]]:
        raise SourceUnavailable("Svartråden endret seg under lesing; hent på nytt")
    attachments.update((a["attachment_id"], a) for a in after_files)
    quoted = []
    queue = [
        s["quoted_reference"]["id"] for s in summaries.values() if s["quoted_reference"]
    ]
    visited = set(summaries)
    while queue and len(quoted) < MAX_QUOTES:
        quote_id = queue.pop(0)
        if quote_id in visited:
            continue
        visited.add(quote_id)
        row = client.message(cid, quote_id)
        if row is None:
            limitations.append(
                "En sitert melding mangler; kildegrunnlaget er ufullstendig."
            )
            continue
        summary, files, errors = _normalize(row, cid, core)
        quoted.append(summary)
        if summary["reply_count"]:
            limitations.append("Svarene til en sitert melding er ikke hentet.")
        attachments.update((a["attachment_id"], a) for a in files)
        limitations.extend(errors)
        if summary["quoted_reference"]:
            queue.append(summary["quoted_reference"]["id"])
    if queue:
        limitations.append("Grensen for siterte meldinger er nådd.")
    if not thread_complete:
        limitations.append("Svartråden er begrenset eller svarantallet har endret seg.")
    if not audience["members_complete"]:
        limitations.append("Hele deltakerlisten er ikke tilgjengelig.")
    selected_summary = summaries[ident]
    return {
        "source": "hoopit",
        "source_id": "chat:" + cid + ":message:" + ident,
        "reference": reference,
        "title": "Chatmelding",
        "body": selected_summary["text"],
        "author_id": selected_summary["author_id"],
        "created_at": selected_summary["created_at"],
        "audience": audience,
        "message": selected_summary,
        "thread": {
            "root": summaries[root["id"]],
            "replies": [summaries[r["id"]] for r in raw_replies],
            "complete": thread_complete,
        },
        "quoted_messages": quoted,
        "attachments": list(attachments.values()),
        "content_complete": not limitations,
        "limitations": list(dict.fromkeys(limitations)),
        "has_unread_attachment": bool(attachments),
    }
