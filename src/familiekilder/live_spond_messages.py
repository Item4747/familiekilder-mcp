"""Replies to Spond posts/events using first-party comment contracts."""

from .api_transport import ApiHTTPError, ApiTransportError
from .core import FamiliekilderError
from .live_create import perform
from .live_readers import identifier, provider
from .action_ledger import now
from .source_adapter import SourceUnavailable


def _reference(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "id"}
        or value.get("kind") not in {"post", "event"}
    ):
        raise FamiliekilderError("Velg en Spond-referanse for innlegg eller aktivitet")
    return {"kind": value["kind"], "id": identifier(value["id"])}


def _rows(value):
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        raise SourceUnavailable("Spond returnerte ufullstendig kommentarstruktur")
    return value


def _thread(module, client, ref):
    path = ("/posts/" if ref["kind"] == "post" else "/sponds/") + ref["id"]
    query = (
        {
            "includeComments": True,
            "includeReadStatus": False,
            "includeSeenCount": False,
            "addGroupInfo": True,
        }
        if ref["kind"] == "post"
        else {"includeComments": True, "addProfileInfo": True}
    )
    detail = client.get_json(module.url_with_query(module.API_ROOT + path, query))
    if not isinstance(detail, dict) or detail.get("id") != ref["id"]:
        raise SourceUnavailable("Spond returnerte feil innlegg eller aktivitet")
    # Dedicated endpoint returns the entire tree; UI's load-more only expands
    # the already fetched array. Do not use potentially clipped list summaries.
    tree = client.get_json(module.API_ROOT + path + "/comments")
    rows, seen = [], set()
    for root in _rows(tree):
        root_id = identifier(root.get("id"))
        for row, parent in [
            (root, None),
            *((r, root_id) for r in _rows(root.get("children", []))),
        ]:
            ident = identifier(row.get("id"))
            if ident in seen or (parent is not None and row.get("children")):
                raise SourceUnavailable(
                    "Spond returnerte duplikater eller ukjent svarnivå"
                )
            seen.add(ident)
            if len(seen) > module.MAX_COMMENTS_PER_ENTITY:
                raise SourceUnavailable("Spond-tråden overskrider kommentargrensen")
            author = identifier(row.get("fromProfileId"))
            rows.append(
                {
                    "id": ident,
                    "parent_id": parent,
                    "author_id": author,
                    "text": row.get("text"),
                    "timestamp": row.get("timestamp"),
                    "has_image": row.get("image") is not None,
                }
            )
    group = detail.get("group") or (detail.get("recipients") or {}).get("group")
    if group is not None and not isinstance(group, dict):
        raise SourceUnavailable("Spond returnerte ugyldig gruppetilknytning")
    audience = {
        "group_id": module.entity_group_id(detail),
        "group_name": (group or {}).get("name"),
        "visibility": detail.get("visibility"),
        "participants_hidden": detail.get("participantsHidden"),
    }
    return detail, rows, audience, path + "/comments"


def thread_context(runtime, reference):
    runtime.config.require("spond")
    ref = _reference(reference)
    with provider(runtime.config.root, "spond") as (module, client):
        detail, rows, audience, _ = _thread(module, client, ref)
        return {
            "source": "spond",
            "reference": ref,
            "fetched_at": now(),
            "title": detail.get("title")
            if ref["kind"] == "post"
            else detail.get("heading"),
            "comments": rows,
            "complete": True,
            "can_reply": detail.get("commentsDisabled") is False,
            "audience": audience,
            "visibility": "Svaret er synlig for deltakerne i den eksisterende tråden; dette er ikke privat chat.",
            "source_content_is_untrusted": True,
        }


def reply_message(runtime, operation_id, reference, content, parent_comment_id=None):
    runtime.config.require("spond", "messages")
    ref = _reference(reference)
    parent = identifier(parent_comment_id) if parent_comment_id is not None else None
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content) > 4096
        or "\x00" in content
    ):
        raise FamiliekilderError(
            "Spond-svar må være ikke-tom tekst på høyst 4096 tegn"
        )
    with (
        provider(runtime.config.root, "spond") as (module, client),
        runtime.ledger() as ledger,
    ):
        profile = client.get_json(module.PROFILE_ENDPOINT)
        if not isinstance(profile, dict):
            raise SourceUnavailable("Spond returnerte ugyldig avsenderprofil")
        sender = identifier(profile.get("id"))
        notifications = {"mentions": None}
        if ref["kind"] == "event":
            notifications["hostsOnly"] = True
        payload = {"text": content, "notifications": notifications}
        request = {
            "reference": ref,
            "parent_comment_id": parent,
            "sender_id": sender,
            **payload,
        }
        target = {
            "thread_reference": ref,
            "parent_comment_id": parent,
            "sender_id": sender,
        }
        prior = ledger.reserve_operation(
            operation_id, "spond", "reply_message", request
        )
        if prior and prior["state"] == "verified":
            return ledger.operation_result(operation_id, "verified")
        recovering = prior is not None and prior["state"] in {"sending", "unknown"}
        prepared = {}

        def observe():
            detail, rows, _, path = _thread(module, client, ref)
            if not recovering and not prepared.get("sent"):
                if detail.get("commentsDisabled") is not False:
                    raise FamiliekilderError(
                        "Spond tillater ikke kommentarer eller svarrettigheten er ukjent"
                    )
                if parent is not None and not any(
                    r["id"] == parent and r["parent_id"] is None for r in rows
                ):
                    raise FamiliekilderError(
                        "Velg en hovedkommentar fra denne Spond-tråden"
                    )
            prepared["path"] = path + ("/" + parent if parent else "")
            return [
                r["id"]
                for r in rows
                if r["text"] == content
                and r["author_id"] == sender
                and r["parent_id"] == parent
                and not r["has_image"]
            ]

        def send():
            client._before_request()
            prepared["sent"] = True
            try:
                return client.transport.post_json(
                    module.API_ROOT + prepared["path"],
                    payload,
                    bearer_token=client.token,
                    allow_empty=True,
                )
            except ApiHTTPError as exc:
                # Preserve provider-wide cooldown even if it interrupts readback.
                client._record_provider_stop(exc)
                raise
            except (ApiTransportError, OSError, TimeoutError):
                module.record_cooldown(client.root, 503, at=client.wall_time())
                raise

        return perform(
            ledger,
            operation_id,
            "spond",
            "reply_message",
            request,
            target,
            observe,
            send,
        )


def register(server, runtime):
    from .source_server import READ, WRITE, safe_tool

    def spond_thread_context(reference: dict) -> dict:
        """Les Spond-innlegg/aktivitet med alle kommentarer, publikum og svarrettighet. Ingen lest-markering. Bruk referanse fra list_items/get_item."""
        return thread_context(runtime, reference)

    def spond_reply_message(
        operation_id: str,
        reference: dict,
        content: str,
        parent_comment_id: str | None = None,
    ) -> dict:
        """Svar i eksisterende Spond-tråd etter konkret brukerbeskjed. Synlig for trådens deltakere; privat chat inngår ikke. Valgfri hovedkommentar-ID. Aktivitetskommentarer varsler bare verter; ingen omtaler. Maks 4096 tegn. Ved ukjent resultat brukes samme UUID uten ny POST."""
        return reply_message(
            runtime, operation_id, reference, content, parent_comment_id
        )

    server.add_tool(safe_tool(spond_thread_context), annotations=READ)
    if "messages" in runtime.config.services["spond"]:
        server.add_tool(safe_tool(spond_reply_message), annotations=WRITE)
