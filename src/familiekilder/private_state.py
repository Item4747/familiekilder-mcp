"""Hardened filesystem boundary for private source credentials and state."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar, cast

from .core import FamiliekilderError, project_paths


MAX_PRIVATE_JSON_BYTES = 1024 * 1024
_SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_T = TypeVar("_T")


class PrivateStateError(FamiliekilderError):
    """A sanitized failure at the private file boundary."""


def credentials_directory(root: Path | str | None) -> Path:
    """Return and secure the canonical per-installation credential directory."""

    path = project_paths(root).private / "credentials"
    ensure_private_directory(path)
    return path


def credential_path(root: Path | str | None, filename: str) -> Path:
    """Resolve a safe credential filename below ``.private/credentials``."""

    if not isinstance(filename, str) or not _SAFE_FILENAME.fullmatch(filename):
        raise PrivateStateError("Credential-filnavnet har ugyldig format")
    # Path construction is intentionally pure. Offline status probes must not
    # create state; writers call ensure_private_directory themselves.
    return project_paths(root).private / "credentials" / filename


def _private_ancestor(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.name == ".private":
            return candidate
    raise PrivateStateError("Privat tilstand må ligge under .private")


def ensure_private_directory(path: Path | str) -> Path:
    """Create a private directory tree and enforce mode ``0700`` to ``.private``."""

    target = Path(path)
    private = _private_ancestor(target)
    try:
        # Walk outwards from ``.private`` instead of using parents=True.  The
        # latter can follow a symlinked intermediate directory before we have
        # had a chance to validate it, creating or chmod-ing files outside the
        # private boundary.
        relative = target.relative_to(private)
        current = private
        candidates = (private, *(private / Path(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1)))
        for current in candidates:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            metadata = current.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
            ):
                raise PrivateStateError("En privat katalog kan ikke være en symbolsk lenke")
            current.chmod(0o700)
    except PrivateStateError:
        raise
    except ValueError:
        raise PrivateStateError("Privat katalog må ligge under .private") from None
    except OSError:
        raise PrivateStateError("Den private katalogen kunne ikke sikres") from None
    return target


def fsync_directory(path: Path | str) -> None:
    """Durably commit a directory mutation where the platform supports it."""

    if os.name == "nt":
        return
    try:
        descriptor = os.open(
            Path(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise PrivateStateError("Den private katalogendringen kunne ikke bekreftes") from None


def _validate_open_file(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise PrivateStateError("En privat fil må være en vanlig fil")
    if os.name != "nt" and metadata.st_mode & 0o077:
        raise PrivateStateError("En privat fil har for brede rettigheter")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PrivateStateError("En privat fil har uventet eier")


def _validate_private_parent_tree(path: Path) -> None:
    """Validate every existing parent from .private without chmod or creation."""

    private = _private_ancestor(path)
    try:
        relative_parent = path.parent.relative_to(private)
    except ValueError:
        raise PrivateStateError("Privat fil må ligge under .private") from None
    candidates = (
        private,
        *(
            private / Path(*relative_parent.parts[:index])
            for index in range(1, len(relative_parent.parts) + 1)
        ),
    )
    for current in candidates:
        metadata = current.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
            or (os.name != "nt" and metadata.st_mode & 0o077)
        ):
            raise PrivateStateError(
                "En privat katalog har utrygg type, eier eller modus"
            )


def validate_private_parent_tree(path: Path | str) -> bool:
    """Validate every existing parent without following an intermediate symlink.

    ``False`` means that a parent is absent, and therefore the final path cannot
    currently exist.  Unsafe types, ownership or modes remain hard failures.
    This small public boundary is shared by deletion paths which must preserve a
    safe final symlink while refusing to traverse a symlinked parent directory.
    """

    try:
        _validate_private_parent_tree(Path(path))
    except FileNotFoundError:
        return False
    except PrivateStateError:
        raise
    except OSError:
        raise PrivateStateError(
            "Det private foreldretreet kunne ikke kontrolleres sikkert"
        ) from None
    return True


def read_private_bytes(
    path: Path | str,
    *,
    max_bytes: int = MAX_PRIVATE_JSON_BYTES,
    missing_message: str = "Den private filen mangler",
) -> bytes:
    """Read one owned, regular ``0600`` file without following a final symlink."""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise PrivateStateError("Grensen for privat filstørrelse er ugyldig")
    target = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        _validate_private_parent_tree(target)
        descriptor = os.open(target, flags)
        _validate_open_file(descriptor)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    except FileNotFoundError:
        raise PrivateStateError(missing_message) from None
    except PrivateStateError:
        raise
    except OSError:
        # ELOOP from O_NOFOLLOW and platforms without a stable lstat/open window
        # are intentionally indistinguishable at the public boundary.
        raise PrivateStateError("Den private filen kunne ikke leses sikkert") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) > max_bytes:
        raise PrivateStateError("Den private filen er større enn tillatt")
    return payload


def _mapping_validator(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PrivateStateError("Den private JSON-filen har ugyldig format")
    return value


def read_private_json(
    path: Path | str,
    *,
    validator: Callable[[Any], _T] | None = None,
    max_bytes: int = MAX_PRIVATE_JSON_BYTES,
    missing_message: str = "Den private JSON-filen mangler",
) -> _T | Mapping[str, Any]:
    """Read bounded JSON and pass it through a provider-supplied validator."""

    raw = read_private_bytes(
        path,
        max_bytes=max_bytes,
        missing_message=missing_message,
    )
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PrivateStateError("Den private JSON-filen har ugyldig format") from None
    operation: Callable[[Any], Any] = validator or _mapping_validator
    try:
        return cast(_T | Mapping[str, Any], operation(value))
    except PrivateStateError:
        raise
    except Exception:
        raise PrivateStateError("Den private JSON-filen besto ikke validering") from None


def _validate_existing_target(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise PrivateStateError("Målfilen kunne ikke kontrolleres sikkert") from None
    if stat.S_ISLNK(metadata.st_mode):
        raise PrivateStateError("En privat fil kan ikke være en symbolsk lenke")
    if not stat.S_ISREG(metadata.st_mode):
        raise PrivateStateError("En privat fil må være en vanlig fil")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PrivateStateError("En privat fil har uventet eier")


def atomic_write_private_bytes(
    path: Path | str,
    payload: bytes,
    *,
    overwrite: bool = True,
) -> None:
    """Atomically commit private bytes with mode ``0600`` and directory fsync."""

    if not isinstance(payload, bytes):
        raise PrivateStateError("Privat filinnhold må være bytes")
    target = Path(path)
    ensure_private_directory(target.parent)
    _atomic_write_bytes(target, payload, overwrite=overwrite)


def _atomic_write_bytes(target: Path, payload: bytes, *, overwrite: bool) -> None:
    """Commit to a prepared parent; callers choose the private/export boundary."""
    _validate_existing_target(target)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary_path.chmod(0o600)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        if overwrite:
            os.replace(temporary_path, target)
            temporary_path = None
        else:
            try:
                os.link(temporary_path, target)
            except FileExistsError:
                raise PrivateStateError(
                    "Målfilen finnes allerede; ingen fil ble overskrevet"
                ) from None
            temporary_path.unlink()
            temporary_path = None
        target.chmod(0o600)
        fsync_directory(target.parent)
    except PrivateStateError:
        raise
    except OSError:
        raise PrivateStateError("Den private filen kunne ikke lagres atomisk") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def atomic_write_private_json(
    path: Path | str,
    value: Mapping[str, Any],
    *,
    overwrite: bool = True,
) -> None:
    """Serialize and atomically commit one private JSON object."""

    if not isinstance(value, Mapping):
        raise PrivateStateError("Privat JSON-tilstand må være et objekt")
    try:
        payload = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise PrivateStateError("Privat JSON-tilstand kunne ikke serialiseres") from None
    atomic_write_private_bytes(path, payload, overwrite=overwrite)


def write_client_export_json(path: Path | str, value: Mapping[str, Any]) -> None:
    """Export client metadata to an explicit destination outside the private store.

    Unlike session writes, exports may have a caller-selected parent. Existing
    files are never overwritten and the new file still receives mode 0600.
    """
    target = Path(path)
    payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise PrivateStateError("Eksportkatalogen kunne ikke opprettes") from None
    _atomic_write_bytes(target, payload, overwrite=False)


def remove_private_file(path: Path | str, *, missing_ok: bool = True) -> bool:
    """Unlink exactly one regular file or symlink without following it."""

    target = Path(path)
    if not validate_private_parent_tree(target):
        if missing_ok:
            return False
        raise PrivateStateError("Den private filen mangler")
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        if missing_ok:
            return False
        raise PrivateStateError("Den private filen mangler") from None
    except OSError:
        raise PrivateStateError("Den private filen kunne ikke kontrolleres") from None
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
        raise PrivateStateError("Bare én privat fil kan fjernes")
    try:
        target.unlink()
        fsync_directory(target.parent)
    except PrivateStateError:
        raise
    except OSError:
        raise PrivateStateError("Den private filen kunne ikke fjernes") from None
    return True
