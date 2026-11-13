"""Small cross-platform, process-reentrant lock for private source state."""

from __future__ import annotations

import os
import math
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .core import SOURCES, FamiliekilderError, ensure_private_layout, normalize_source, project_paths


_LOCAL = threading.local()

# Shared locks besides the one per source. A lock lives in `<name>.lock`, so
# a name must never change once it has been used.
_NAMES = {'changes', 'read-cache', 'android-devices'}


def register_lock_names(*names: str) -> None:
    """Allow more `private_named_lock` names, for a program built on this package."""
    _NAMES.update(names)


class PrivateLockBusy(FamiliekilderError):
    retryable = True


def _acquire(handle, lock_path, source, *, wait, timeout):
    """Keep OS-backed exclusion; optional waiting has a monotonic deadline."""
    deadline = time.monotonic() + timeout if timeout is not None else None
    if os.name == "nt":
        import msvcrt

        if lock_path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)

        def attempt():
            mode = msvcrt.LK_LOCK if wait and deadline is None else msvcrt.LK_NBLCK
            msvcrt.locking(handle.fileno(), mode, 1)

        busy_errors = (OSError,)
    else:
        import fcntl

        def attempt():
            flags = fcntl.LOCK_EX
            if not wait or deadline is not None:
                flags |= fcntl.LOCK_NB
            fcntl.flock(handle, flags)

        busy_errors = (BlockingIOError,)
    while True:
        try:
            attempt()
            return
        except busy_errors:
            if deadline is None:
                raise PrivateLockBusy(
                    f"En annen {source}-operasjon kjører allerede"
                ) from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PrivateLockBusy(
                    f"Tidsavbrudd etter å ha ventet på en annen {source}-operasjon; "
                    "ingen forespørsel sendt. Prøv oppslaget igjen senere."
                ) from None
            time.sleep(min(.05, remaining))


def _depths() -> dict[str, int]:
    value = getattr(_LOCAL, "depths", None)
    if value is None:
        value = {}
        _LOCAL.depths = value
    return value


@contextmanager
def private_source_lock(
    root: Path | str | None,
    source: str,
    *,
    wait: bool = False,
    timeout: float | None = None,
) -> Iterator[None]:
    """Hold a reentrant per-source lock; timeout bounds explicit waiting."""

    with private_named_lock(root, normalize_source(source), wait=wait, timeout=timeout):
        yield


@contextmanager
def private_named_lock(root, name, *, wait=False, timeout=None):
    """Internal lock namespace, shared by source and whole-cycle guards."""
    if timeout is not None and (
        not wait or type(timeout) not in (int, float)
        or not math.isfinite(timeout) or timeout < 0
    ):
        raise FamiliekilderError("Ugyldig ventetid for privat lås")
    if name not in _NAMES and name not in SOURCES:
        raise FamiliekilderError('Ukjent privat lås')
    source = name
    paths = project_paths(root)
    ensure_private_layout(paths)
    normalized_source = name
    lock_dir = paths.private / "locks"
    try:
        lock_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise FamiliekilderError("Den private låsekatalogen kunne ikke opprettes") from None
    try:
        directory_metadata = lock_dir.lstat()
        if stat.S_ISLNK(directory_metadata.st_mode) or not stat.S_ISDIR(
            directory_metadata.st_mode
        ):
            raise FamiliekilderError("Den private låsekatalogen har utrygg type")
        if hasattr(os, "geteuid") and directory_metadata.st_uid != os.geteuid():
            raise FamiliekilderError("Den private låsekatalogen har uventet eier")
        lock_dir.chmod(0o700)
    except FamiliekilderError:
        raise
    except OSError:
        raise FamiliekilderError("Den private låsekatalogen kunne ikke sikres") from None
    lock_path = lock_dir / f"{normalized_source}.lock"
    key = str(lock_path.absolute())
    depths = _depths()
    if depths.get(key, 0):
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        try:
            before = lock_path.lstat()
        except FileNotFoundError:
            before = None
        if before is not None and (
            stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
        ):
            raise FamiliekilderError("Den private låsefilen har utrygg type")
        descriptor = os.open(lock_path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FamiliekilderError("Den private låsefilen har utrygg type")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise FamiliekilderError("Den private låsefilen har uventet eier")
        if before is not None and (before.st_dev, before.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise FamiliekilderError("Den private låsefilen ble endret under åpning")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            lock_path.chmod(0o600)
        handle = os.fdopen(descriptor, "r+b", closefd=True)
        descriptor = None
    except FamiliekilderError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise FamiliekilderError("Den private låsefilen kunne ikke åpnes sikkert") from None
    locked = False
    try:
        _acquire(handle, lock_path, source, wait=wait, timeout=timeout)
        locked = True
        depths[key] = 1
        try:
            yield
        finally:
            depths.pop(key, None)
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
