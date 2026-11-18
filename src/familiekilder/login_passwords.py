"""Profile-bound password storage. Secrets only enter the system's own store.

That is the macOS Keychain, Windows Credential Manager or the Secret Service on
Linux; `desktop_platform.password_backend` picks it. The service name and the
account format are stored data and must not change.
"""

import getpass
import hashlib
import sys

from . import desktop_platform
from .core import FamiliekilderError, project_paths
from .private_state import atomic_write_private_json, read_private_json
from .sources import REGISTRY

KINDS = ("bankid", *(name for name, s in REGISTRY.items() if s.password_origins))
SERVICE = "no.familiekilder.login-password.v1"


def account(root, kind):
    if kind not in KINDS:
        raise FamiliekilderError("Ukjent passordtype")
    digest = hashlib.sha256(str(project_paths(root).root).encode()).hexdigest()
    return f"{digest}:{kind}"


def _backend():
    return desktop_platform.password_backend()


def _store(failure):
    """The store, or its own fixed text when it is missing; any other error is hidden."""

    try:
        return _backend()
    except FamiliekilderError:
        raise
    except Exception:  # noqa: BLE001 - never expose secret-bearing backend errors
        raise FamiliekilderError(failure) from None


def _path(root):
    return project_paths(root).private / "login-passwords.json"


def _settings(root):
    path = _path(root)
    if not path.exists():
        return {}
    value = read_private_json(path)
    if set(value) - set(KINDS) or any(
        not isinstance(v, str) or v != account(root, k) for k, v in value.items()
    ):
        # A copied configuration must never silently select the old owner's key.
        raise FamiliekilderError(
            "Passordoppsettet tilhører en annen datarot eller er ugyldig"
        )
    return dict(value)


def configured(root, kind):
    account(root, kind)
    return kind in _settings(root)


def set_password(root, kind, password):
    identity = account(root, kind)
    if (
        not isinstance(password, str)
        or not password
        or len(password) > 1024
        or any(ord(c) < 32 for c in password)
    ):
        raise FamiliekilderError("Passordet har ugyldig format")
    settings = _settings(root)
    # A missing store raises its own fixed text, which says what to do.
    backend = _store("Passordet kunne ikke lagres i nøkkelringen")
    try:
        backend.set_password(SERVICE, identity, password)
    except Exception:  # noqa: BLE001 - never expose secret-bearing backend errors
        raise FamiliekilderError(
            "Passordet kunne ikke lagres i nøkkelringen"
        ) from None
    settings[kind] = identity
    project_paths(root).root.mkdir(parents=True, exist_ok=True)
    atomic_write_private_json(_path(root), settings)


def get_password(root, kind):
    if not configured(root, kind):
        return None
    backend = _store("Passordet kunne ikke hentes fra nøkkelringen")
    try:
        value = backend.get_password(SERVICE, account(root, kind))
    except Exception:  # noqa: BLE001 - never expose secret-bearing backend errors
        raise FamiliekilderError(
            "Passordet kunne ikke hentes fra nøkkelringen"
        ) from None
    if not value:
        raise FamiliekilderError(
            "Passordet mangler i nøkkelringen; lagre det på nytt for denne profilen"
        )
    return value


def delete_password(root, kind):
    settings = _settings(root)
    if kind not in settings:
        return
    backend = _store("Passordet kunne ikke slettes fra nøkkelringen")
    try:
        if backend.get_password(SERVICE, account(root, kind)) is not None:
            backend.delete_password(SERVICE, account(root, kind))
    except Exception:  # noqa: BLE001 - never expose secret-bearing backend errors
        raise FamiliekilderError(
            "Passordet kunne ikke slettes fra nøkkelringen"
        ) from None
    del settings[kind]
    atomic_write_private_json(_path(root), settings)


def run_password_command(root, kind, command):
    if command == "set":
        if not sys.stdin.isatty():
            raise FamiliekilderError(
                "Passord må oppgis skjult i en interaktiv terminal"
            )
        first = getpass.getpass("Passord: ")
        second = getpass.getpass("Gjenta passord: ")
        if first != second:
            raise FamiliekilderError("Passordene er ikke like")
        set_password(root, kind, first)
    elif command == "delete":
        delete_password(root, kind)
    return {
        "kind": kind,
        "configured": configured(root, kind),
        "storage": desktop_platform.password_storage(),
    }
