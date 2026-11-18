"""The parts of a login that depend on the operating system.

- Password storage: only the system's own store, with the backend chosen here
  and never by keyring settings or environment variables. That is the macOS
  Keychain, Windows Credential Manager or the Secret Service on Linux. When
  none is available, the password helper is off and the user types the
  password in the browser.
- Machine binding: an ID that stays the same when the network name changes.
  On macOS the hardware UUID and on Windows the installation's MachineGuid.
  Linux keeps the hostname binding it always had: servers rely on it, and a
  server binds to its installation ID instead.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .core import FamiliekilderError

MACOS = "darwin"
WINDOWS = "win32"
LINUX = "linux"

# Stable names in `password status`; macOS keeps the name it always had.
PASSWORD_STORAGE = {
    MACOS: "macos-keychain",
    WINDOWS: "windows-credential-manager",
    LINUX: "secret-service",
}
_STORAGE_LABEL = {
    MACOS: "macOS-nøkkelringen",
    WINDOWS: "Legitimasjonsbehandling i Windows",
    LINUX: "Nøkkelringen (Secret Service)",
}


def platform_name(platform: str | None = None) -> str:
    value = sys.platform if platform is None else platform
    return LINUX if value.startswith("linux") else value


def password_storage(platform: str | None = None) -> str | None:
    """A stable ID for this platform's password store, or None without one."""

    return PASSWORD_STORAGE.get(platform_name(platform))


def password_storage_label(platform: str | None = None) -> str:
    return _STORAGE_LABEL.get(platform_name(platform), "Passordlagring")


def password_backend(platform: str | None = None) -> Any:
    """The keyring backend for this platform's own store.

    Raises when the store is missing, so the caller can leave the password
    helper off. Never falls back to another backend or to a file.
    """

    platform = platform_name(platform)
    if platform not in PASSWORD_STORAGE:
        raise FamiliekilderError(
            "Lagrede passord støttes på macOS, Windows og Linux. Skriv passordet i nettleseren."
        )
    try:
        if platform == MACOS:
            # Explicit backend: never use an environment-selected plaintext fallback.
            from keyring.backends.macOS import Keyring

            return Keyring()
        if platform == WINDOWS:
            from keyring.backends.Windows import WinVaultKeyring as Keyring
        else:
            from keyring.backends.SecretService import Keyring
        # Raises when the store is missing, such as no Secret Service on D-Bus.
        Keyring.priority
        return Keyring()
    except Exception:  # noqa: BLE001 - never expose secret-bearing backend errors
        if platform == LINUX:
            raise FamiliekilderError(
                "Fant ingen nøkkelring (Secret Service). Lagrede passord er av; "
                "skriv passordet i nettleseren, eller start en nøkkelring som "
                "GNOME Keyring eller KWallet."
            ) from None
        raise FamiliekilderError(f"{password_storage_label(platform)} er utilgjengelig") from None


def _query(*command: str) -> str:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=15, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _windows_machine_guid() -> str | None:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as key:
            value, kind = winreg.QueryValueEx(key, "MachineGuid")
    except (ImportError, OSError):
        return None
    return value if kind == getattr(winreg, "REG_SZ", 1) and isinstance(value, str) else None


def machine_identity(
    platform: str | None = None,
    *,
    query: Callable[..., str] = _query,
    windows_guid: Callable[[], str | None] = _windows_machine_guid,
    installation: Callable[[], str | None] | None = None,
) -> str | None:
    """An ID for this computer that survives a new network name, or None."""

    platform = platform_name(platform)
    if platform == MACOS:
        found = re.search(
            r'"IOPlatformUUID"\s*=\s*"([^"]+)"',
            query('/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'),
        )
        return found.group(1) if found else None
    if platform != WINDOWS:
        return None
    if installation is None:
        from .hosting import installation_id as installation
    if installation():
        # A host binds to its installation ID.
        return None
    value = windows_guid()
    return value.lower() if value and re.fullmatch(r"[0-9A-Fa-f-]{36}", value) else None


def default_data_root(
    platform: str | None = None,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Where the data lives when neither ``--root`` nor ``FAMILIEKILDER_ROOT`` is set."""

    platform = platform_name(platform)
    environ = dict(os.environ) if environ is None else environ

    def home_folder() -> Path:
        # Only when needed: without HOME or USERPROFILE, Path.home() fails on
        # Windows even where XDG_DATA_HOME or LOCALAPPDATA gives the answer.
        return Path.home() if home is None else home

    if platform == MACOS:
        return home_folder() / "Library/Application Support/familiekilder-mcp"
    if platform == WINDOWS:
        base = environ.get("LOCALAPPDATA") or str(home_folder() / "AppData" / "Local")
        return Path(base) / "familiekilder-mcp"
    base = environ.get("XDG_DATA_HOME") or str(home_folder() / ".local/share")
    return Path(base) / "familiekilder-mcp"
