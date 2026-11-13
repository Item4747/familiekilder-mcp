"""Text replies in existing Hoopit post/event threads, never private chat."""

from .core import FamiliekilderError
from .live_create import perform
from .live_hoopit import _id, _rows
from .live_readers import provider
from .action_ledger import now
from .source_adapter import SourceUnavailable


def _reference(value):
    if not isinstance(value, dict):
        raise FamiliekilderError("Velg en Hoopit-trådreferanse")
    kind = value.get("kind")
    if kind not in {"post", "event"}:
        raise FamiliekilderError("Svar støttes for innlegg og aktiviteter")
    allowed = {"kind", "id"} | ({"occurrence_id"} if kind == "event" else set())
    if set(value) != allowed:
        raise FamiliekilderError("Ugyldige felter i trådreferansen")
    result = {"kind": kind, "id": _id(value.get("id"))}
    if kind == "event":
        result["occurrence_id"] = _id(value.get("occurrence_id"))
    return result


def _thread(module, client, ref):
    if ref["kind"] == "post":
        path = f"/app/posts/{ref['id']}/"
        detail = client.get(path)
        if not isinstance(detail, dict) or _id(detail.get("id")) != ref["id"]:
            raise SourceUnavailable("Hoopit returnerte feil innlegg")
        comments = module.post_comments(client, detail)
        permitted = detail.get("are_comments_disabled") is False
        audience = {
            "group_id": detail.get("group_id"),
            "group_name": detail.get("group_name"),
            "visible_for_parents": detail.get("visible_for_parents"),
            "visible_for_members": detail.get("visible_for_members"),
        }
        version = 2
    else:
        path = f"/app/events/{ref['id']}/occurrences/{ref['occurrence_id']}/"
        detail = client.get(path)
        if (
            not isinstance(detail, dict)
            or _id(detail.get("event")) != ref["id"]
            or _id(detail.get("id")) != ref["occurrence_id"]
        ):
            raise SourceUnavailable("Hoopit returnerte feil aktivitet")
        comments = module.event_comments(client, ref["id"], ref["occurrence_id"])
        permitted = detail.get("can_create_comment") is True
        audience = {"group": detail.get("group")}
        version = 1
    rows = []
    seen = set()
    for root in _rows(comments):
        if root.get("parent_id") is not None:
            raise SourceUnavailable("Hoopit returnerte feil hovedkommentar")
        root_id = _id(root.get("id"))
        for row in [root, *_rows(root.get("_replies", []))]:
            ident = _id(row.get("id"))
            if ident in seen:
                raise SourceUnavailable("Hoopit returnerte dupliserte kommentarer")
            seen.add(ident)
            if row is not root and _id(row.get("parent_id")) != root_id:
                raise SourceUnavailable("Hoopit returnerte svar i feil kommentartråd")
            if ref["kind"] == "post" and row.get("post") is not None:
                if (
                    not isinstance(row["post"], dict)
                    or _id(row["post"].get("id")) != ref["id"]
                ):
                    raise SourceUnavailable("Hoopit-kommentaren tilhører feil innlegg")
            rows.append(row)
    return detail, rows, permitted, audience, path + "comments/", version


def thread_context(runtime, reference):
    runtime.config.require("hoopit")
    ref = _reference(reference)
    with provider(runtime.config.root, "hoopit") as (module, client):
        detail, rows, permitted, audience, _, _ = _thread(module, client, ref)
        return {
            "source": "hoopit",
            "reference": ref,
            "fetched_at": now(),
            "complete": True,
            "title": detail.get("title"),
            "can_reply": permitted,
            "audience": audience,
            "visibility": "Kommentarer er synlige for deltakerne i den eksisterende tråden",
            "comments": [
                {
                    "id": _id(r["id"]),
                    "parent_id": _id(r["parent_id"])
                    if r.get("parent_id") is not None
                    else None,
                    "author_id": _id((r.get("poster") or {}).get("id")),
                    "author_name": " ".join(
                        (r.get("poster") or {}).get(k) or ""
                        for k in ("first_name", "last_name")
                    ),
                    "text": r.get("comment"),
                    "timestamp": r.get("timestamp"),
                }
                for r in rows
            ],
            "source_content_is_untrusted": True,
        }


def reply_message(runtime, operation_id, reference, content, parent_comment_id=None):
    runtime.config.require("hoopit", "messages")
    ref = _reference(reference)
    parent = _id(parent_comment_id) if parent_comment_id is not None else None
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content) > 20000
        or "\x00" in content
    ):
        raise FamiliekilderError(
            "Kommentar må være ikke-tom tekst på høyst 20000 tegn"
        )
    with (
        provider(runtime.config.root, "hoopit") as (module, client),
        runtime.ledger() as ledger,
    ):
        profile = client.get("/app/users/current/")
        if not isinstance(profile, dict):
            raise SourceUnavailable("Hoopit mangler egen avsender")
        sender = _id(profile.get("id"))
        request = {
            "reference": ref,
            "content": content,
            "parent_comment_id": parent,
            "sender_id": sender,
        }
        payload = {"comment": content}
        if parent is not None:
            payload["parent"] = int(parent)
        target = {
            "thread_reference": ref,
            "parent_comment_id": parent,
            "sender_id": sender,
        }
        # observe refreshes both permissions and the complete comment tree before
        # sending, then again for readback. An unknown write only needs readback.
        prior = ledger.reserve_operation(
            operation_id, "hoopit", "reply_message", request
        )
        if prior and prior["state"] == "verified":
            return ledger.operation_result(operation_id, "verified")
        recovering = prior is not None and prior["state"] in {"sending", "unknown"}
        prepared = {}

        def observe():
            _, rows, permitted, _, path, version = _thread(module, client, ref)
            if not recovering and not prepared.get("sent"):
                if not permitted:
                    raise FamiliekilderError(
                        "Hoopit har stengt kommentarer i denne tråden"
                    )
                if parent is not None and not any(
                    _id(r["id"]) == parent and r.get("parent_id") is None for r in rows
                ):
                    raise FamiliekilderError("Velg en hovedkommentar fra denne tråden")
            prepared.update(path=path, version=version)
            return [
                _id(row["id"])
                for row in rows
                if row.get("comment") == content
                and _id((row.get("poster") or {}).get("id")) == sender
                and (
                    _id(row["parent_id"]) if row.get("parent_id") is not None else None
                )
                == parent
            ]

        def send():
            client._begin()
            prepared["sent"] = True
            try:
                result = client.transport.post_json(
                    module.API_ROOT + prepared["path"],
                    payload,
                    headers=client._headers(prepared["version"]),
                    allow_empty=True,
                )
            except Exception as exc:
                client._failure(exc, authenticated_origin=True)
                raise
            client.limiter.record_success()
            return result

        return perform(
            ledger,
            operation_id,
            "hoopit",
            "reply_message",
            request,
            target,
            observe,
            send,
        )


def register(server, runtime):
    from .source_server import READ, WRITE, safe_tool

    def hoopit_thread_context(reference: dict) -> dict:
        """Hent fersk kommentartråd, kommentar-ID-er, publikum og svarrettighet for Hoopit-innlegg/aktivitet. Ingen lest-markering."""
        return thread_context(runtime, reference)

    def hoopit_reply_message(
        operation_id: str,
        reference: dict,
        content: str,
        parent_comment_id: str | None = None,
    ) -> dict:
        """Svar som kommentar etter konkret brukerbeskjed, synlig for trådens deltakere. Valgfri hovedkommentar-ID gir et svar under den kommentaren. Samme UUID ved ukjent resultat; ingen automatisk ny sending. Privat chat og varsle-alle inngår ikke."""
        return reply_message(
            runtime, operation_id, reference, content, parent_comment_id
        )

    server.add_tool(safe_tool(hoopit_thread_context), annotations=READ)
    if "messages" in runtime.config.services["hoopit"]:
        server.add_tool(safe_tool(hoopit_reply_message), annotations=WRITE)
