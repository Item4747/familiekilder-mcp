"""Local connection status of a source and the origin of a failed login.

Shared by the CLI and any login front end built on the package. Nothing here
reads a source remotely or returns secrets, URLs or exception text.
"""

from __future__ import annotations

import importlib
import traceback
from pathlib import Path
from typing import Any

from .core import FamiliekilderError
from .source_adapter import AUTH_MODULES

# Helpers that replace any failure with their own fixed error.
_WRAPPERS = frozenset({"safe_call", "_safe_call"})


def auth_module(source: str) -> Any:
    try:
        module_name = AUTH_MODULES[source]
    except KeyError:
        raise FamiliekilderError(f"{source!r} har ingen lokal API-autentisering") from None
    return importlib.import_module(module_name)


def has_local_session(auth_module: Any, root: Path | None) -> bool:
    path_factory = getattr(auth_module, "session_path", None)
    if callable(path_factory):
        try:
            return bool(path_factory(root).exists())
        except OSError:
            return False
    metadata = auth_module.auth_status(root)
    return bool(metadata.get("authenticated") or metadata.get("session_present"))


def connection_state(status):
    """Access-token expiry is normal; the source renews it during MCP reads.

    Only missing configuration/authentication or a recorded block needs setup
    attention. This is local readiness, not a successful remote read.
    """
    if status.get("status_unavailable"):
        return "unavailable"
    if not status.get("configured"):
        return "setup_required"
    if status.get("refresh_blocked"):
        return "attention_required"
    if status.get("authenticated"):
        return "connected"
    return "login_required"


def failure_origin(exc):
    """Class and code location of the first error behind a failed login.

    Never its text: auth exceptions can contain URLs or responses.
    """
    seen = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        earlier = exc.__cause__ or exc.__context__
        if earlier is None:
            break
        exc = earlier
    frames = traceback.extract_tb(exc.__traceback__)
    # safe_call raises its fixed error itself; the caller's line names the step.
    frames = [frame for frame in frames if frame.name not in _WRAPPERS] or frames
    place = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "ukjent"
    return type(exc).__name__, place
