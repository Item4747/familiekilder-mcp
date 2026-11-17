"""Built-in client setup for sources whose app identity ships with the package.

Internal API. The values in ``data/client-defaults.json`` describe the official
app (client ID, endpoints, app version). They are the same for every user of
that app and give no access to anyone's account; a user still logs in.

When the setup wizard stores a built-in setup, it writes a mark next to it:
``.private/credentials/<source>-default-setup.json``. The mark holds two
SHA-256 fingerprints and no values: one of the stored setup file exactly as the
source wrote it, and one of the built-in setup it came from. The mark counts
only while the stored file is byte for byte what the wizard wrote, so a setup
the user imports with ``auth … configure`` or edits by hand is never renewed.
``refresh_stored_defaults`` replaces a marked setup when the package ships a
newer built-in setup; the wizard and ``mcp serve`` call it when they start.
"""

from __future__ import annotations

import copy
from functools import cache
import hashlib
from importlib import resources
import json
import logging
from pathlib import Path
import re
import secrets
from typing import Any

from .auth_common import canonical_sha256
from .private_lock import PrivateLockBusy, private_source_lock
from .private_state import (
    PrivateStateError,
    atomic_write_private_bytes,
    atomic_write_private_json,
    credential_path,
    ensure_private_directory,
    read_private_bytes,
    read_private_json,
)

DATA_FILE = "client-defaults.json"
MARK_SCHEMA = "familiekilder.default-setup/v1"
MARK_SUFFIX = "-default-setup.json"
# A pending renewal waits this long for a source busy elsewhere, then is
# left for the next start. A source with nothing pending is never locked.
LOCK_TIMEOUT_SECONDS = 2.0
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_log = logging.getLogger(__name__)


class RenewalRefused(Exception):
    """The source refused a newer built-in setup while it has a session."""


@cache
def _defaults() -> dict[str, dict[str, Any]]:
    text = resources.files(__package__).joinpath("data", DATA_FILE).read_text(encoding="utf-8")
    value = json.loads(text)
    if not isinstance(value, dict) or not all(
        isinstance(source, str) and isinstance(config, dict) for source, config in value.items()
    ):
        raise ValueError(f"{DATA_FILE} must map each source to an object")
    return value


def default_client_config(source: str) -> dict[str, Any] | None:
    """Return the built-in client setup for a source, or None."""

    if not isinstance(source, str):
        return None
    config = _defaults().get(source)
    return None if config is None else copy.deepcopy(config)


def default_fingerprint(config: dict[str, Any]) -> str:
    """SHA-256 of a built-in setup as compact, key-sorted JSON."""

    return canonical_sha256(config, ensure_ascii=False)


def mark_path(root: Path | str | None, source: str) -> Path:
    """Where the mark for a wizard-stored built-in setup lives."""

    return credential_path(root, f"{source}{MARK_SUFFIX}")


def import_client_text(root: Path, module: Any, content: str) -> None:
    """Import a client setup through the source's own ``configure_client``.

    The auth modules import from a private file, so the text is written to a
    short-lived file under ``.private/setup-imports`` first.
    """

    if not isinstance(json.loads(content), dict):
        raise ValueError
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ensure_private_directory(root / ".private")
    directory = root / ".private/setup-imports"
    ensure_private_directory(directory)
    path = directory / f"client-{secrets.token_hex(12)}.json"
    try:
        atomic_write_private_bytes(path, content.encode())
        module.configure_client(root, path)
    finally:
        path.unlink(missing_ok=True)


def _write_default(root: Path, source: str, config: dict[str, Any], module: Any) -> None:
    """Write a built-in setup and its mark; the caller holds the source lock.

    A crash between the two writes leaves the setup unmarked, which only
    means it is treated like the user's own and never renewed.
    """

    import_client_text(root, module, json.dumps(config))
    stored = read_private_bytes(module.client_config_path(root))
    atomic_write_private_json(
        mark_path(root, source),
        {
            "schema": MARK_SCHEMA,
            "setup_sha256": hashlib.sha256(stored).hexdigest(),
            "default_sha256": default_fingerprint(config),
        },
    )


def store_default(root: Path, source: str, config: dict[str, Any], *, module: Any = None) -> bool:
    """Store a built-in setup where none is stored, and mark it as the wizard's.

    Returns False and writes nothing when a setup appeared in the meantime,
    for example one the user imported from another process. The source lock
    covers the check and both writes.
    """

    module = _module(source, module)
    Path(root).mkdir(parents=True, exist_ok=True)
    with private_source_lock(root, source, wait=True, timeout=LOCK_TIMEOUT_SECONDS):
        if _present(module.client_config_path(root)):
            return False
        _write_default(Path(root), source, config, module)
    return True


def _present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        # Something is there, but it cannot be inspected; the reads below fail safely.
        return True
    return True


def _read_mark(root: Path | str | None, source: str) -> dict[str, str] | None:
    try:
        value = read_private_json(mark_path(root, source), max_bytes=4096)
    except PrivateStateError:
        return None
    if (
        value.get("schema") != MARK_SCHEMA
        or not isinstance(value.get("setup_sha256"), str)
        or not isinstance(value.get("default_sha256"), str)
        or not _SHA256.fullmatch(value["setup_sha256"])
        or not _SHA256.fullmatch(value["default_sha256"])
    ):
        return None
    return {"setup_sha256": value["setup_sha256"], "default_sha256": value["default_sha256"]}


def _provider_managed(root: Path | str | None) -> bool:
    """A signed provider bundle owns the setups in this data folder."""

    from .provider_bundle import provider_state_path

    return _present(provider_state_path(root))


def _newer_default(root: Path | str | None, source: str, module: Any) -> dict[str, Any] | None:
    """The newer built-in setup that may replace the stored one, or None.

    None when there is no valid mark, the package has no built-in setup, the
    mark already names this one, a provider bundle owns the folder, or the
    stored file is unreadable or no longer exactly what the wizard wrote.
    """

    if not _present(mark_path(root, source)):
        return None
    default = default_client_config(source)
    if default is None:
        return None
    mark = _read_mark(root, source)
    if mark is None or mark["default_sha256"] == default_fingerprint(default):
        return None
    if _provider_managed(root):
        return None
    try:
        stored = read_private_bytes(module.client_config_path(root))
    except PrivateStateError:
        return None
    if hashlib.sha256(stored).hexdigest() != mark["setup_sha256"]:
        return None
    return default


def _module(source: str, module: Any) -> Any:
    if module is not None:
        return module
    from .connection_status import auth_module

    return auth_module(source)


def renewal_pending(root: Path | str | None, source: str, *, module: Any = None) -> bool:
    """Whether a wizard-stored setup waits for a newer built-in setup; no lock, no writes."""

    if not _present(mark_path(root, source)):
        return False
    return _newer_default(root, source, _module(source, module)) is not None


def refresh_stored_default(root: Path | str | None, source: str, *, module: Any = None) -> bool:
    """Replace one wizard-stored setup with a newer built-in setup.

    Returns True when the setup was replaced. The source lock is taken only
    when a renewal is pending, and everything is checked again under it.
    Raises ``RenewalRefused`` when the source refuses the new setup while it
    has a session, typically because the app identity changes; nothing is
    changed then. Raises ``PrivateLockBusy`` when the source stays busy.
    Never deletes a session.
    """

    if not _present(mark_path(root, source)):
        return False
    module = _module(source, module)
    if _newer_default(root, source, module) is None:
        return False
    from .connection_status import has_local_session

    with private_source_lock(root, source, wait=True, timeout=LOCK_TIMEOUT_SECONDS):
        default = _newer_default(root, source, module)
        if default is None:
            return False
        try:
            _write_default(Path(root), source, default, module)
        except (PrivateStateError, OSError):
            raise
        except Exception:
            # Still pending means nothing was written.
            if has_local_session(module, root) and _newer_default(root, source, module) is not None:
                raise RenewalRefused from None
            raise
    return True


def refresh_stored_defaults(root: Path | str | None) -> list[str]:
    """Renew every wizard-stored setup that has a newer built-in setup.

    Returns the ids of the sources that changed. Never raises: a source that
    cannot be renewed now keeps its setup and is tried again at the next start.
    Only fixed messages are logged, never values.
    """

    from .sources import REGISTRY

    changed: list[str] = []
    for source in sorted(REGISTRY):
        try:
            if refresh_stored_default(root, source):
                changed.append(source)
        except PrivateLockBusy:
            _log.info(
                "%s var opptatt. Et nytt innebygd oppsett prøves igjen ved neste start.",
                source,
            )
        except RenewalRefused:
            _log.warning(
                "%s har en innlogging og godtar ikke det nye innebygde oppsettet før du "
                "logger inn på nytt i oppsettet eller logger ut. Innloggingen og det "
                "lagrede oppsettet er beholdt.",
                source,
            )
        except Exception:
            _log.warning(
                "Det nye innebygde oppsettet for %s kunne ikke lagres nå. Det lagrede "
                "oppsettet er beholdt og prøves igjen ved neste start.",
                source,
            )
    return changed
