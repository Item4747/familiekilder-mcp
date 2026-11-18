"""Move logins from the user's Mac to a host, so that only one place renews them.

Source tokens rotate, and only one installation may renew a token chain (see
``docs/auth-lifecycle.md``). A login is therefore moved, never copied:

1. The Mac asks the host for a fresh one-time X25519 key (owner code required).
2. Under each source's lock, the Mac reads the source's private files and
   encrypts them to that key (X25519 + HKDF-SHA256 + AES-256-GCM). The header
   (format, version, key ID and host fingerprint) is authenticated data.
3. The Mac writes a "moved" marker for each source. From then on it refuses to
   renew them, even if the rest fails.
4. The Mac sends the package (owner code required). The host deletes the key
   at first use, checks the package, writes the files, binds ownership to its
   own installation and turns the sources on, read-only.
5. The Mac deletes its copies of the moved files. Only a clear refusal from
   the host, sent before anything was written, removes the markers again.

The worst outcome of an interruption is that the user logs in again on the
Mac. The read cache is never moved. A new login on the Mac clears the marker.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .core import FamiliekilderError, project_paths
from .hosting import HostSettings, normalize_public_url
from .private_lock import private_named_lock, private_source_lock, register_lock_names
from .private_state import (
    _SAFE_FILENAME,
    atomic_write_private_bytes,
    atomic_write_private_json,
    credential_path,
    read_private_bytes,
    read_private_json,
    remove_private_file,
)
from .sources import REGISTRY

FORMAT = "familiekilder-move"
PAYLOAD_FORMAT = "familiekilder-move-payload"
KEY_FORMAT = "familiekilder-move-key"
VERSION = 1
KEY_PATH = "/flytt/nokkel"
IMPORT_PATH = "/flytt/importer"
OWNER_CODE_HEADER = "X-Familiekilder-Eierkode"
KEY_SECONDS = 15 * 60
MAX_KEYS = 3
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
_HKDF_INFO = b"familiekilder-move v1"

register_lock_names("remote-move")


class MoveError(FamiliekilderError):
    """The move did not happen; local logins are unchanged."""


class MoveUncertain(FamiliekilderError):
    """The package was sent, but the host's answer was lost or unclear."""


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: Any) -> bytes:
    if not isinstance(value, str) or len(value) > 2 * MAX_PACKAGE_BYTES:
        raise ValueError("not base64")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


# Files


def session_filename(source: str) -> str:
    return f"{source}.json" if REGISTRY[source].login == "google" else f"{source}-session.json"


def marker_path(root: Path, source: str) -> Path:
    return credential_path(root, f"{source}-moved.json")


def _belongs_to(source: str, filename: str) -> bool:
    if not _SAFE_FILENAME.fullmatch(filename) or not filename.endswith(".json"):
        return False
    if filename != f"{source}.json" and not filename.startswith(f"{source}-"):
        return False
    # A source named like "x-y" owns its files, not a source named "x".
    for other in REGISTRY:
        if other.startswith(f"{source}-") and filename.startswith(f"{other}-"):
            return False
    if filename in (f"{source}-auth-owner.json", f"{source}-moved.json"):
        return False
    return "-read-cache-" not in filename


def source_files(root: Path, source: str) -> list[str]:
    directory = project_paths(root).private / "credentials"
    if not directory.is_dir():
        return []
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.is_file() and not entry.is_symlink() and _belongs_to(source, entry.name)
    )


def moved_sources(root: Path) -> dict[str, dict[str, Any]]:
    """Sources moved away from here and not logged in again since."""

    result = {}
    for source in REGISTRY:
        path = marker_path(root, source)
        if not path.exists():
            continue
        try:
            marker = read_private_json(path)
        except FamiliekilderError:
            marker = {}
        result[source] = {
            "moved_to": marker.get("moved_to") if isinstance(marker.get("moved_to"), str) else None,
            "moved_at": marker.get("moved_at") if isinstance(marker.get("moved_at"), (int, float)) else None,
            "confirmed": marker.get("confirmed") is True,
        }
    return result


# Crypto


def host_fingerprint(settings: HostSettings) -> str:
    """Names the host without revealing its installation ID."""

    identity = settings.installation_id or settings.public_url
    return hashlib.sha256(b"familiekilder-move-host\0" + identity.encode()).hexdigest()[:32]


def _derive(shared: bytes, ephemeral: bytes, recipient: bytes, key_id: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=ephemeral + recipient,
        info=_HKDF_INFO + b"\0" + key_id.encode(),
    ).derive(shared)


def _raw(public: X25519PublicKey) -> bytes:
    return public.public_bytes(Encoding.Raw, PublicFormat.Raw)


def seal(payload: dict[str, Any], *, key_id: str, public_key: bytes, host: str) -> bytes:
    """Encrypt ``payload`` to the host's one-time key; returns the package bytes."""

    recipient = X25519PublicKey.from_public_bytes(public_key)
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = _raw(ephemeral.public_key())
    key = _derive(ephemeral.exchange(recipient), ephemeral_public, public_key, key_id)
    header = {
        "format": FORMAT,
        "version": VERSION,
        "key_id": key_id,
        "host": host,
        "ephemeral_public": _b64(ephemeral_public),
    }
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, _canonical(payload), _canonical(header))
    return _canonical({**header, "nonce": _b64(nonce), "ciphertext": _b64(ciphertext)})


def open_package(package: bytes, private_key: X25519PrivateKey, *, key_id: str, host: str) -> dict[str, Any]:
    """Decrypt and check a package; raises MoveError for anything unexpected."""

    try:
        envelope = json.loads(package)
        if not isinstance(envelope, dict) or set(envelope) != {
            "format", "version", "key_id", "host", "ephemeral_public", "nonce", "ciphertext",
        }:
            raise ValueError
        header = {key: envelope[key] for key in ("format", "version", "key_id", "host", "ephemeral_public")}
        if header["format"] != FORMAT or header["version"] != VERSION:
            raise MoveError("Ukjent pakkeformat")
        if header["key_id"] != key_id or header["host"] != host:
            raise MoveError("Pakken er laget for en annen vert eller nøkkel")
        ephemeral_public = _unb64(header["ephemeral_public"])
        nonce = _unb64(envelope["nonce"])
        ciphertext = _unb64(envelope["ciphertext"])
        recipient = _raw(private_key.public_key())
        shared = private_key.exchange(X25519PublicKey.from_public_bytes(ephemeral_public))
        key = _derive(shared, ephemeral_public, recipient, key_id)
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _canonical(header))
        payload = json.loads(plaintext)
    except MoveError:
        raise
    except InvalidTag:
        raise MoveError("Pakken er endret eller ikke laget for denne verten") from None
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        raise MoveError("Pakken har ugyldig format") from None
    return validate_payload(payload, host=host)


def validate_payload(payload: Any, *, host: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("format") != PAYLOAD_FORMAT:
        raise MoveError("Ukjent innhold i pakken")
    if payload.get("version") != VERSION or payload.get("host") != host:
        raise MoveError("Pakken er laget for en annen vert")
    sources = payload.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise MoveError("Pakken inneholder ingen kilder")
    checked: dict[str, dict[str, bytes]] = {}
    for source, entry in sources.items():
        if source not in REGISTRY or not isinstance(entry, dict) or set(entry) != {"files"}:
            raise MoveError("Pakken inneholder en ukjent kilde")
        files = entry["files"]
        if not isinstance(files, dict) or session_filename(source) not in files:
            raise MoveError("Pakken mangler en innlogging")
        checked[source] = {}
        for filename, content in files.items():
            if not isinstance(filename, str) or not _belongs_to(source, filename):
                raise MoveError("Pakken inneholder en fil som ikke hører til kilden")
            try:
                data = _unb64(content)
                if len(data) > MAX_FILE_BYTES or not isinstance(json.loads(data), dict):
                    raise ValueError
            except (ValueError, TypeError, UnicodeDecodeError):
                raise MoveError("Pakken inneholder en ugyldig fil") from None
            checked[source][filename] = data
    return {"package_id": str(payload.get("package_id", "")), "sources": checked}


# Host side


class MoveReceiver:
    """The host's two move endpoints. Both require the owner code."""

    def __init__(self, root: Path, settings: HostSettings, gate, *, clock: Callable[[], float] = time.time):
        self.root = root
        self.settings = settings
        self.gate = gate
        self.clock = clock
        self.host = host_fingerprint(settings)
        self._path = project_paths(root).private / "remote" / "move-keys.json"
        self._lock = threading.Lock()

    def routes(self):
        from mcp.server.transport_security import RequestBodyLimitMiddleware
        from starlette.routing import Route, request_response

        return [
            Route(
                KEY_PATH,
                endpoint=RequestBodyLimitMiddleware(request_response(self.new_key), 1024),
                methods=["POST"],
            ),
            Route(
                IMPORT_PATH,
                endpoint=RequestBodyLimitMiddleware(request_response(self.receive), MAX_PACKAGE_BYTES),
                methods=["POST"],
            ),
        ]

    def _keys(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        value = read_private_json(self._path)
        now = self.clock()
        return {
            key_id: entry
            for key_id, entry in value.get("keys", {}).items()
            if isinstance(entry, dict) and entry.get("created_at", 0) > now - KEY_SECONDS
        }

    def _save(self, keys: dict[str, Any]) -> None:
        atomic_write_private_json(self._path, {"version": 1, "keys": keys})

    def create_key(self) -> dict[str, Any]:
        private = X25519PrivateKey.generate()
        key_id = secrets.token_urlsafe(16)
        raw_private = private.private_bytes_raw()
        with self._lock, private_named_lock(self.root, "remote-move", wait=True, timeout=30):
            keys = self._keys()
            for old in sorted(keys, key=lambda item: keys[item]["created_at"])[: max(0, len(keys) - MAX_KEYS + 1)]:
                keys.pop(old)
            keys[key_id] = {"created_at": self.clock(), "private": _b64(raw_private)}
            self._save(keys)
        return {
            "format": KEY_FORMAT,
            "version": VERSION,
            "key_id": key_id,
            "public_key": _b64(_raw(private.public_key())),
            "host": self.host,
        }

    def take_key(self, key_id: str) -> X25519PrivateKey | None:
        """Remove and return a key; each key opens at most one package."""

        with self._lock, private_named_lock(self.root, "remote-move", wait=True, timeout=30):
            keys = self._keys()
            entry = keys.pop(key_id, None) if isinstance(key_id, str) else None
            self._save(keys)
        if entry is None:
            return None
        return X25519PrivateKey.from_private_bytes(_unb64(entry["private"]))

    async def _owner(self, request) -> Any:
        from starlette.responses import JSONResponse

        from .remote_auth import OwnerCodeLimited

        code = request.headers.get(OWNER_CODE_HEADER, "")
        try:
            correct = await self.gate.check_async(code)
        except OwnerCodeLimited as exc:
            return JSONResponse(
                {"error": "too_many_attempts", "imported": False},
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )
        if not correct:
            return JSONResponse({"error": "wrong_owner_code", "imported": False}, status_code=401)
        return None

    async def new_key(self, request):
        from starlette.responses import JSONResponse

        refused = await self._owner(request)
        if refused is not None:
            return refused
        return JSONResponse(self.create_key(), headers={"Cache-Control": "no-store"})

    async def receive(self, request):
        import anyio
        from starlette.responses import JSONResponse

        refused = await self._owner(request)
        if refused is not None:
            return refused
        body = await request.body()
        try:
            key_id = json.loads(body).get("key_id")
        except (ValueError, AttributeError):
            return JSONResponse({"error": "invalid_package", "imported": False}, status_code=400)
        private = self.take_key(key_id)
        if private is None:
            return JSONResponse({"error": "unknown_or_used_key", "imported": False}, status_code=410)
        try:
            package = open_package(body, private, key_id=key_id, host=self.host)
        except MoveError as exc:
            return JSONResponse(
                {"error": "invalid_package", "detail": str(exc), "imported": False}, status_code=400
            )
        # Writing has begun: from here on only success (200) or an unclear
        # failure (500) is reported, never a refusal.
        imported = await anyio.to_thread.run_sync(import_sources, self.root, package)
        return JSONResponse({"imported": imported}, headers={"Cache-Control": "no-store"})


def import_sources(root: Path, package: dict[str, Any]) -> list[str]:
    """Write moved sources, bind them to this installation and turn them on."""

    from . import auth_lifecycle
    from .live_config import Config

    imported = []
    for source, files in sorted(package["sources"].items()):
        with private_source_lock(root, source, wait=True, timeout=60):
            # Persist ownership before any credentials, and never unlink it.
            # Every crash snapshot therefore keeps the destination binding.
            auth_lifecycle.bind_owner(root, source)
            for filename in source_files(root, source) + [f"{source}-moved.json"]:
                remove_private_file(credential_path(root, filename))
            for filename, data in sorted(files.items()):
                atomic_write_private_bytes(credential_path(root, filename), data)
            if source in auth_lifecycle.SOURCES:
                auth_lifecycle.record(root, source, "import_succeeded")
        imported.append(source)
    config = Config.load(root)
    services = dict(config.services)
    for source in imported:
        # Read-only on the host, whatever the Mac allowed.
        services[source] = frozenset()
    Config(config.root, services).save()
    return imported


# Mac side


def _has_session(root: Path, source: str) -> bool:
    return credential_path(root, session_filename(source)).exists()


def movable_sources(root: Path, requested: Iterable[str] | None = None) -> list[str]:
    from .live_config import Config

    config = Config.load(root)
    if requested:
        chosen = []
        for source in requested:
            if source not in REGISTRY:
                raise MoveError(f"Ukjent kilde: {source}")
            if not _has_session(root, source):
                raise MoveError(f"{REGISTRY[source].label} har ingen innlogging her å flytte")
            chosen.append(source)
        return sorted(set(chosen))
    return sorted(source for source in config.services if _has_session(root, source))


def _post(client, url: str, owner_code: str, body: bytes) -> Any:
    return client.post(
        url,
        content=body,
        headers={OWNER_CODE_HEADER: owner_code, "Content-Type": "application/json"},
    )


def _refusal(response) -> str | None:
    """The host's reason for a clean refusal, or None if the answer is unclear."""

    if not 400 <= response.status_code < 500:
        return None
    try:
        value = response.json()
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("imported") is not False:
        return None
    return {
        "wrong_owner_code": "Feil eierkode",
        "too_many_attempts": "For mange feil forsøk på eierkoden; vent og prøv igjen",
        "unknown_or_used_key": "Nøkkelen fra verten er utløpt; prøv igjen",
    }.get(value.get("error"), "Verten avviste pakken")


def move_to(
    root: Path,
    url: str,
    owner_code: str,
    *,
    sources: Iterable[str] | None = None,
    client_factory: Callable[[], Any] | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Move the chosen logins (default: all turned-on sources) to the host at ``url``."""

    import httpx

    from . import auth_lifecycle

    base = normalize_public_url(url)
    chosen = movable_sources(root, sources)
    if not chosen:
        raise MoveError("Fant ingen påslåtte kilder med innlogging å flytte")
    factory = client_factory or (lambda: httpx.Client(timeout=60, follow_redirects=False))
    with factory() as client:
        try:
            response = _post(client, base + KEY_PATH, owner_code, b"{}")
        except httpx.HTTPError:
            raise MoveError("Fikk ikke kontakt med verten; ingenting er flyttet") from None
        if response.status_code != 200:
            raise MoveError((_refusal(response) or "Verten svarte ikke som ventet") + "; ingenting er flyttet")
        try:
            key = response.json()
            if key.get("format") != KEY_FORMAT or key.get("version") != VERSION:
                raise ValueError
            key_id, host, public_key = key["key_id"], key["host"], _unb64(key["public_key"])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise MoveError("Verten sendte en ukjent nøkkel; ingenting er flyttet") from None

        with contextlib.ExitStack() as locks:
            for source in chosen:
                locks.enter_context(private_source_lock(root, source, wait=True, timeout=60))
                auth_lifecycle.require_owner(root, source)
                auth_lifecycle.require_not_moved(root, source)
            files = {
                source: {
                    name: _b64(read_private_bytes(credential_path(root, name), max_bytes=MAX_FILE_BYTES))
                    for name in source_files(root, source)
                }
                for source in chosen
            }
            package_id = str(uuid.uuid4())
            payload = {
                "format": PAYLOAD_FORMAT,
                "version": VERSION,
                "package_id": package_id,
                "created_at": clock(),
                "host": host,
                "sources": {source: {"files": names} for source, names in files.items()},
            }
            package = seal(payload, key_id=key_id, public_key=public_key, host=host)
            if len(package) > MAX_PACKAGE_BYTES:
                raise MoveError("Innloggingene er for store til å flyttes samlet; flytt én kilde om gangen")

            # From here the Mac never renews these sources again.
            marker = {"version": 1, "moved_to": base, "moved_at": clock(), "package_id": package_id}
            for source in chosen:
                atomic_write_private_json(marker_path(root, source), {**marker, "confirmed": False})

            try:
                response = _post(client, base + IMPORT_PATH, owner_code, package)
            except httpx.HTTPError:
                response = None
            refusal = _refusal(response) if response is not None else None
            if refusal is not None:
                for source in chosen:
                    remove_private_file(marker_path(root, source))
                raise MoveError(refusal + "; ingenting er flyttet")
            for source in chosen:
                for name in source_files(root, source):
                    remove_private_file(credential_path(root, name))
                remove_private_file(credential_path(root, f"{source}-auth-owner.json"))
            confirmed = response is not None and response.status_code == 200
            if confirmed:
                for source in chosen:
                    atomic_write_private_json(marker_path(root, source), {**marker, "confirmed": True})
                return {"moved": chosen, "host": base}
    raise MoveUncertain(
        "Pakken ble sendt, men svaret fra verten kom ikke fram. Innloggingene er fjernet "
        "her. Spør assistenten om family_connections på verten; mangler noe, logg inn på "
        "nytt på datamaskinen og flytt igjen."
    )
