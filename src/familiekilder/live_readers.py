"""Live provider access. No ingestion, source windows or archive database.

Raw responses exist only while servicing a request/pagination. References contain
provider identifiers, never credentials or caller-supplied endpoint URLs.
"""

from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
import json
import mimetypes
import urllib.parse
from .core import FamiliekilderError
from .private_lock import private_source_lock
from .source_adapter import SourceUnavailable
from .action_ledger import fingerprint
from .sources import REGISTRY, reader as _source_reader


def normalized_item(item):
    """Public source content; expiring download specifications are not evidence."""
    return {**item, "attachments": [
        {key: value for key, value in attachment.items() if not key.startswith("_")}
        for attachment in item.get("attachments", [])]}


def observation_content_hash(item):
    """Use the same source version for observations and attachment resolution."""
    clean = normalized_item(item)
    clean["source_content_is_untrusted"] = True
    return fingerprint({key: value for key, value in clean.items()
                        if key not in {"fetched_at", "email_triage"}})


def content_hash(item):
    """Ignore transport/observation fields; retain semantic content and file IDs.

    Stored as `change_items.scan_hash` and `detail_hash`: the output must stay
    byte-identical (see tests/test_change_identity_parity.py).
    """

    def clean(value):
        if isinstance(value, dict):
            return {
                k: clean(v)
                for k, v in value.items()
                if not k.startswith("_")
                and k
                not in {
                    "fetched_at",
                    "observation_token",
                    "source_content_is_untrusted",
                    "read_at",
                    "seen_at",
                    "is_read",
                    "is_seen",
                    "email_triage",
                }
            }
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return fingerprint(clean(item))


def default_reference_values(reference, extra_keys=()):
    """ID values of an ordinary kind/id reference; a reader may allow extra keys."""
    allowed = {
        "kind",
        "id",
        "child_id",
        "child_ids",
        "cid",
        "chat_id",
        "msg_num",
        "occurrence_id",
        *extra_keys,
    }
    if reference.keys() - allowed:
        raise FamiliekilderError("Ugyldige felt i kildereferansen")
    values = [v for k, v in reference.items() if k != "child_ids"]
    children = reference.get("child_ids", [])
    if not isinstance(children, list):
        raise FamiliekilderError("Ugyldig barnereferanse")
    return values + children


def stored_reference(source, reference):
    """Persist only identifiers accepted by existing getters, never arbitrary data.

    Returns the JSON stored as `change_items.reference_json`; a reader may
    validate its own references with `reference_values(reference)`.
    """
    if not isinstance(reference, dict):
        raise FamiliekilderError("Kilden mangler en stabil referanse")
    custom = getattr(_source_reader(source), "reference_values", None) if source in REGISTRY else None
    values = custom(reference) if custom else default_reference_values(reference)
    if any(not isinstance(v, (str, int, bool, type(None))) for v in values):
        raise FamiliekilderError("Kildereferansen skal bare inneholde ID-er")
    encoded = json.dumps(reference, sort_keys=True)
    if len(encoded) > 4096:
        raise FamiliekilderError("Kildereferansen er for stor")
    return encoded


def attachment_metadata(spec):
    name = str(
        spec.get("name")
        or spec.get("filename")
        or spec.get("fileName")
        or spec.get("title")
        or "vedlegg"
    )
    url = next(
        (
            spec[k]
            for k in ("url", "media", "downloadUrl", "fileUrl", "imageUrl")
            if isinstance(spec.get(k), str)
        ),
        "",
    )
    if name == "vedlegg":
        name = (
            Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
            or "vedlegg"
        )
    identity = str(
        spec.get("id") or spec.get("uid") or urllib.parse.urlsplit(url).path or name
    )
    return {
        "attachment_id": fingerprint([identity, name])[:24],
        "filename": (
            Path(name).name if Path(name).name not in {"", ".", ".."} else "vedlegg"
        ),
        "mime_type": spec.get("mimeType")
        or mimetypes.guess_type(name)[0]
        or "application/octet-stream",
        "_spec": dict(spec),
    }


def ref_item(item, ref):
    item["reference"] = ref
    return item


@contextmanager
def provider(root, source):
    """Cross-process serialization includes token rotation and provider pacing."""
    from .live_traffic import traffic_scope
    from .gmail_traffic import READ_LOCK_TIMEOUT

    # Parallel Gmail reads queue briefly instead of failing on normal contention.
    with private_source_lock(
        root, source, wait=source == "gmail",
        timeout=READ_LOCK_TIMEOUT if source == "gmail" else None,
    ), traffic_scope(root, source):
        with _reader(source).connect(root) as (m, client):
            yield m, client


@contextmanager
def read_scope(root, sources):
    """One synchronous scan: readers with `read_scope` may reuse account data."""
    with ExitStack() as stack:
        for source in sources:
            scope = getattr(_reader(source), "read_scope", None) if source in REGISTRY else None
            if scope is not None:
                stack.enter_context(scope(root))
        yield


def search_gmail_live(root, query, limit, page_token=None, include_spam_trash=False, *, budget=None):
    """`budget` is the caller's optional read allowance; see `gmail.Client.search`."""
    with provider(root, "gmail") as (_, client):
        return client.search(query, limit, page_token, include_spam_trash, budget=budget)


def _reader(source):
    if source not in REGISTRY:
        raise FamiliekilderError("Ukjent tjeneste")
    return _source_reader(source)


def _notifications(source):
    return source in REGISTRY and REGISTRY[source].notifications


def list_notifications_live(root, source, since):
    """Read the verified notification feeds without related entity scans."""
    cutoff = datetime.fromisoformat(since)
    if cutoff.tzinfo is None or not _notifications(source):
        raise FamiliekilderError('Ugyldig varseloppslag')
    with provider(root, source) as (m, c):
        return _reader(source).list_notifications(m, c, cutoff)


def checked_detail(item, canonical, enabled):
    """Transient discovery evidence, normalized exactly as the detail getter."""
    if (enabled and canonical.get("source_id") == item.get("source_id")
            and canonical.get("content_complete", True) is True):
        item["_fresh_detail_hash"] = content_hash(canonical)
    return item


def list_live(root, source, since, *, include_detail_hash=False):
    """Bounded provider traversal, preserving existing coverage and hard caps."""
    if source == "gmail":
        raise FamiliekilderError("Gmail støtter målrettet gmail_search, ikke brede listeoppslag")
    with provider(root, source) as (m, c):
        return _reader(source).list_items(root, m, c, since, include_detail_hash=include_detail_hash)


def _require_detail_reference(source, reference):
    if _notifications(source) and reference.get('kind') == 'notification':
        from .read_evidence import ReadEvidenceError
        raise ReadEvidenceError('Varselet leses fra listen, ikke gjennom get_item. Bruk innholdet fra listeoppslaget.',
            reason='notification_list_only',
            next_calls=[{'tool': source + '_list_items', 'arguments': {}}],
            next_action='Bruk varselet som allerede er levert i listen. Hent listen på nytt bare når grunnlaget er utløpt.')


def get_live(root, source, reference):
    if not isinstance(reference, dict) or len(str(reference)) > 8192:
        raise FamiliekilderError("Ugyldig kildereferanse")
    _require_detail_reference(source, reference)
    with provider(root, source) as (m, c):
        return get_with_client(root, source, m, c, reference)


def get_with_client(root, source, m, c, ref):
    _require_detail_reference(source, ref)
    return _reader(source).get_item(root, m, c, ref)


def identifier(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or urllib.parse.quote(value, safe="") != value
        or value in {".", ".."}
    ):
        raise FamiliekilderError("Ugyldig kilde-ID")
    return value


def download_live(root, source, reference, attachment_id, directory, *,
                  expected_content_hash=None):
    """Resolve the attachment again at the provider; never trust supplied URLs."""
    with provider(root, source) as (m, c):
        # Expiring download authorization must be resolved live, even when the
        # message belongs to this sync's snapshot. The hash check below still
        # rejects changed source content instead of silently adopting it.
        from .sync_snapshot import suspend_snapshot
        with suspend_snapshot():
            item = get_with_client(root, source, m, c, reference)
        if (expected_content_hash is not None
                and observation_content_hash(item) != expected_content_hash):
            raise FamiliekilderError("Kilden er endret; hent og vurder innholdet på nytt")
        return _reader(source).download(root, m, c, item, attachment_id, reference, directory)


def fetch_attachment(item, attachment_id, directory, fetch):
    """Download one listed attachment with `fetch(spec, target) -> (path, mime)`."""
    matches = [
        v
        for v in item.get("attachments", [])
        if v["attachment_id"] == attachment_id
    ]
    if len(matches) != 1:
        raise SourceUnavailable("Vedlegget er endret eller finnes ikke lenger")
    entry = matches[0]
    target, detected_mime = fetch(entry["_spec"], directory / Path(entry["filename"]).name)
    mime = entry["mime_type"]
    if mime == "application/octet-stream" and isinstance(detected_mime, str):
        mime = detected_mime.split(";", 1)[0].strip().lower() or mime
    return target, mime
