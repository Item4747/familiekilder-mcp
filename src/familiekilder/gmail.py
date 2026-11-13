"""Official Gmail read-only API. Full MIME mail exists only during reads."""
import base64
import binascii
import re
import time
from datetime import UTC, datetime
from email.header import decode_header, make_header
from pathlib import Path
from urllib.parse import quote

import httpx

from .network_usage import request as measured_request

from .core import FamiliekilderError, _TextExtractor
from .private_state import credential_path, read_private_json, atomic_write_private_json
from .source_adapter import SourceLoginRequired, SourceUnavailable
from .private_lock import private_source_lock
from .gmail_traffic import Traffic, MAX_WAIT, MAX_RETRIES, READ_LOCK_TIMEOUT, retry_reason

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
MAX_TEXT = 64000
MAX_ATTACHMENT = 25 * 1024 * 1024


class GmailNotFound(SourceUnavailable):
    """An individual Gmail resource no longer exists (HTTP 404)."""


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", value):
        raise FamiliekilderError("Ugyldig Gmail-ID")
    return quote(value, safe="")


def search_query(value):
    if not isinstance(value, str) or not value.strip():
        raise FamiliekilderError("query må være en ikke-tom Gmail-søkestreng")
    return value


def decode(value):
    try:
        if not isinstance(value, str):
            raise ValueError()
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise SourceUnavailable("Ugyldig Gmail-MIME-data") from None


def normalize(raw):
    ident = identifier(raw.get("id"))
    payload = raw.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    parts, attachments, complete = [], [], True

    def walk(part, depth=0, path="root"):
        nonlocal complete
        if depth > 30:
            complete = False
            return
        mime, body = part.get("mimeType", ""), part.get("body", {})
        filename = part.get("filename")
        if filename or body.get("attachmentId"):
            # Google's attachmentId is a rotating transport token. The MIME part
            # identifies the file across fresh message reads and content hashes.
            aid = "part:" + str(part.get("partId") or path)
            attachments.append({"attachment_id": aid, "filename": Path(filename or "vedlegg").name,
                                "mime_type": mime or "application/octet-stream",
                                "_spec": {"gmail_attachment_id": body.get("attachmentId"), "data": body.get("data")}})
            return
        if mime in {"text/plain", "text/html"}:
            if not body.get("data") and body.get("size", 0):
                complete = False
            if body.get("data"):
                data = decode(body["data"])
                content_type = next((h["value"] for h in part.get("headers", []) if h["name"].lower() == "content-type"), "")
                charset = re.search(r'charset=["\']?([A-Za-z0-9_-]+)', content_type, re.I)
                try:
                    text = data.decode(charset[1] if charset else "utf-8")
                except UnicodeDecodeError:
                    # Windows-1252 has five undefined byte values. Preserve
                    # those bytes visibly instead of discarding otherwise
                    # complete mail or replacing source characters silently.
                    import codecs
                    if charset and codecs.lookup(charset[1]).name == 'cp1252':
                        text = ('[Udefinerte bytes i kildens Windows-1252-tekst er vist som \\xNN.]\n'
                                + data.decode('cp1252', errors='backslashreplace'))
                    else:
                        text = data.decode('utf-8', errors='replace')
                        complete = False
                except (LookupError, UnicodeError):
                    text = data.decode("utf-8", errors="replace")
                    complete = False
                if mime == "text/html":
                    extractor = _TextExtractor()
                    extractor.feed(text)
                    text = extractor.text()
                parts.append((mime, text))
        # Prefer plain text in multipart/alternative, retain all mixed body parts.
        children = list(enumerate(part.get("parts", [])))
        if mime == "multipart/alternative":
            plain = [(n, p) for n, p in children if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data")]
            children = plain or children
        for n, child in children:
            walk(child, depth + 1, path + "." + str(n))

    walk(payload)
    body = "\n\n".join(text for _, text in parts).strip()
    if not body and not attachments:
        body = raw.get("snippet", "")
        complete = False
    if len(body) > MAX_TEXT:
        body, complete = body[:MAX_TEXT], False
    try:
        stamp = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, UTC).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise SourceUnavailable("Gmail-meldingen mangler mottakstid") from None
    return {"source": "gmail", "source_id": "message:" + ident, "type": "message",
            "title": str(make_header(decode_header(headers.get("subject", "")))), "body": body,
            "sender": headers.get("from", ""), "published_at": stamp,
            "reference": {"kind": "message", "id": ident},
            "thread_id": raw.get("threadId"), "attachments": attachments,
            "content_complete": complete, "url": "https://mail.google.com/mail/u/0/#all/" + ident}


class Client:
    def __init__(self, root, *, client=None, clock=None, sleep=None, jitter=None):
        self.root = Path(root)
        self.path = credential_path(root, "gmail.json")
        self.http = client or httpx.Client(timeout=20, follow_redirects=False)
        self.traffic = Traffic(root, clock=clock, sleep=sleep, jitter=jitter)

    def close(self):
        self.http.close()

    def grant(self, tokens):
        if SCOPE not in tokens.get("scope", "").split() or not tokens.get("refresh_token"):
            raise FamiliekilderError("Google ga ikke varig Gmail-lesetilgang")
        allowed = {SCOPE, "openid", "email", "profile", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"}
        if set(tokens["scope"].split()) - allowed:
            raise FamiliekilderError("Gmail-grantet har uventede rettigheter")
        value = {k: tokens[k] for k in ("refresh_token", "access_token", "scope")}
        value.update(client_id=tokens["_client_id"], client_secret=tokens["_client_secret"],
                     expires_at=time.time() + int(tokens.get("expires_in", 3600)))
        # Verify a real read before replacing an existing grant.
        response = measured_request(self.http, "get", BASE + "/profile", headers={"Authorization": "Bearer " + value["access_token"]})
        if response.status_code != 200 or not response.json().get("emailAddress"):
            raise SourceLoginRequired("Gmail-lesetilgangen kunne ikke verifiseres")
        atomic_write_private_json(self.path, value)
        return {"authenticated": True}

    def token(self, *, force=False):
        if not self.path.exists():
            raise SourceLoginRequired("Koble Gmail til med Google-innlogging")
        value = dict(read_private_json(self.path))
        if value.get("refresh_blocked"):
            raise SourceLoginRequired("Gmail-tilgangen må fornyes med Google-innlogging")
        if force or value["expires_at"] <= time.time() + 60:
            response = measured_request(self.http, "post", "https://oauth2.googleapis.com/token", data={k: value[k] for k in ("refresh_token", "client_id", "client_secret")} | {"grant_type": "refresh_token"})
            if response.status_code in (400, 401):
                atomic_write_private_json(self.path, value | {"refresh_blocked": True})
                raise SourceLoginRequired("Gmail-tilgangen må fornyes med Google-innlogging")
            if response.status_code != 200:
                raise SourceUnavailable("Google-token kunne ikke fornyes")
            tokens = response.json()
            if "scope" in tokens and SCOPE not in tokens["scope"].split():
                atomic_write_private_json(self.path, value | {"refresh_blocked": True})
                raise SourceLoginRequired("Gmail-leserettighet mangler")
            value.update(access_token=tokens["access_token"], expires_at=time.time() + int(tokens["expires_in"]))
            if tokens.get("refresh_token"):
                value["refresh_token"] = tokens["refresh_token"]
            atomic_write_private_json(self.path, value)
        return value["access_token"]

    def _request(self, path, *, force=False, params):
        """One actual Gmail attempt, also the scheduler's counting boundary."""
        from .request_budget import reserve
        reserve(self.root, 'gmail')
        return measured_request(self.http, "get", BASE + path, params=params, headers={"Authorization": "Bearer " + self.token(force=force)})

    def get(self, path, **params):
        from .sync_snapshot import memo_read

        return memo_read(self.root, 'gmail', ('GET', path, params),
                         lambda: self._get(path, **params))

    def _get(self, path, **params):
        with private_source_lock(self.root, 'gmail', wait=True, timeout=READ_LOCK_TIMEOUT):
            deadline = self.traffic.now() + MAX_WAIT
            retries, refreshed, force = 0, False, False
            while True:
                self.traffic.before(deadline)
                try:
                    response = self._request(path, force=force, params=params)
                except httpx.RequestError:
                    raise SourceUnavailable("Gmail-nettverksoppslaget feilet") from None
                force = False
                if response.status_code == 401 and not refreshed:
                    refreshed, force = True, True
                    continue
                reason = retry_reason(response)
                if reason:
                    exhausted = retries >= MAX_RETRIES or reason == 'daily_quota'
                    deferred = self.traffic.rejected(response, reason, retries, exhausted=exhausted)
                    if exhausted:
                        raise deferred
                    retries += 1
                    continue
                if response.status_code in (401, 403):
                    raise SourceLoginRequired("Gmail-lesetilgang mangler eller er avvist")
                if response.status_code == 404:
                    raise GmailNotFound("Gmail-elementet finnes ikke lenger")
                if response.status_code != 200:
                    raise SourceUnavailable("Gmail-oppslaget feilet")
                self.traffic.succeeded()
                try:
                    return response.json()
                except ValueError:
                    raise SourceUnavailable("Gmail returnerte ugyldige data") from None

    def item(self, reference):
        if set(reference) != {"kind", "id"} or reference.get("kind") != "message":
            raise FamiliekilderError("Ugyldig Gmail-referanse")
        raw = self.get("/messages/" + identifier(reference["id"]), format="full")
        if raw.get("id") != reference["id"]:
            raise SourceUnavailable("Gmail returnerte en annen melding")
        return normalize(raw)


    def search(self, query, limit=20, page_token=None, include_spam_trash=False, *, budget=None):
        """One native search page, without date defaults or relevance filtering.

        `budget` is an optional read allowance supplied by the caller (None
        means no allowance). It needs `reserve(message_id) -> bool`, called
        before each message body is fetched, and `limitation`, the text
        reported when `reserve` refuses a message.
        """
        search_query(query)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise FamiliekilderError("limit må være et heltall 1–50 per side")
        if not isinstance(include_spam_trash, bool):
            raise FamiliekilderError("include_spam_trash må være en boolsk verdi")
        page = self.get("/messages", q=query, maxResults=limit,
                        includeSpamTrash="true" if include_spam_trash else "false",
                        **({"pageToken": page_token} if page_token else {}))
        if not isinstance(page, dict) or not isinstance(page.get("messages", []), list):
            raise SourceUnavailable("Ugyldig Gmail-søkeside")
        rows = page.get("messages", [])
        following = page.get("nextPageToken")
        if (len(rows) > limit or any(not isinstance(row, dict) for row in rows)
                or (following is not None and (not isinstance(following, str) or not following))):
            raise SourceUnavailable("Ugyldig Gmail-søkeside")
        ids = [identifier(row.get("id")) for row in rows]
        items, seen, limitations = [], set(), []
        for ident in ids:
            if ident in seen:
                limitations.append("Gmail gjentok en meldings-ID; dekningen er ufullstendig.")
                continue
            seen.add(ident)
            if budget is not None and not budget.reserve(ident):
                # Never fetch bodies past the allowance, or skip unread IDs by
                # handing out the following page's token.
                return {'items': items, 'next_page_token': None, 'complete': False,
                        'restart_required': False, 'budget_exhausted': True,
                        'limitations': limitations + [budget.limitation]}
            try:
                items.append(self.item({"kind": "message", "id": ident}))
            except GmailNotFound:
                limitations.append("En Gmail-melding finnes ikke lenger; ingen avlysning utledes.")
            except SourceUnavailable:
                if not items:
                    raise
                # Do not advance past unread IDs. A fresh search can retry this page.
                limitations.append("Gmail-lesingen ble avbrutt; start søket på nytt for gjenstående treff.")
                return {"items": items, "next_page_token": None, "complete": False,
                        "restart_required": True, "limitations": limitations}
        if following and (following == page_token or not rows):
            limitations.append("Gmail gjentok en søkeside eller returnerte en tom fortsettelse; start søket på nytt.")
            return {"items": items, "next_page_token": None, "complete": False,
                    "restart_required": True, "limitations": limitations}
        return {"items": items, "next_page_token": following, "complete": not limitations,
                "restart_required": False, "limitations": list(dict.fromkeys(limitations))}

    def download(self, item, attachment_id, directory):
        matches = [a for a in item["attachments"] if a["attachment_id"] == attachment_id]
        if len(matches) != 1:
            raise SourceUnavailable("Gmail-vedlegget finnes ikke lenger")
        entry = matches[0]
        spec = entry["_spec"]
        value = self.get("/messages/" + identifier(item["reference"]["id"]) + "/attachments/" + identifier(spec["gmail_attachment_id"])) if spec.get("gmail_attachment_id") else spec
        data = decode(value.get("data"))
        if len(data) > MAX_ATTACHMENT:
            raise SourceUnavailable("Gmail-vedlegget er for stort")
        name = entry["filename"]
        if name in {"", ".", ".."}:
            name = "vedlegg"
        path = directory / name
        path.write_bytes(data)
        path.chmod(0o600)
        return path, entry["mime_type"]
