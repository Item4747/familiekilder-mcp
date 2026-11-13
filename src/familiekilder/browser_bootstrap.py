"""One-shot, isolated Brave bootstrap for Spond and Hoopit API sessions.

The user signs in in a visible, disposable Brave profile, optionally assisted
by the profile-bound Keychain password helper. Phone approval stays manual. This
module then reads only the provider's exact allowlisted authentication fields
over a private DevTools pipe.  It never reads cookies, passwords, arbitrary
storage keys, or the user's normal Brave profile.

Secret-bearing values live only in local variables and one short-lived 0600
import file.  Public return values are restricted to validated, non-secret
metadata, and all failures have fixed messages which cannot include provider
data.
"""

from __future__ import annotations

import base64
import fcntl
import json
import logging
import math
import os
import re
import select
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .core import FamiliekilderError, project_paths
from .private_state import (
    PrivateStateError,
    atomic_write_private_json,
    ensure_private_directory,
    validate_private_parent_tree,
)


SPOND_ORIGIN = "https://spond.com"
SPOND_LOGIN_URL = f"{SPOND_ORIGIN}/client"
HOOPIT_ORIGIN = "https://app.hoopit.io"
HOOPIT_LOGIN_URL = f"{HOOPIT_ORIGIN}/login"

_BOOTSTRAP_DIRECTORY = "browser-bootstrap"
_RUN_DIRECTORY = re.compile(r"run-[a-f0-9]{32}\Z")
_OWNER_MARKER = ".familiekilder-browser-bootstrap"
_OWNER_MARKER_CONTENT = b"family.browser-bootstrap/v1\n"
_IMPORT_FILE = "session-import.json"
_MAX_CDP_FRAME_BYTES = 4 * 1024 * 1024
_MAX_SECRET_BYTES = 128 * 1024
_CDP_TIMEOUT_SECONDS = 20.0

_SPOND_STORAGE_KEYS = (
    "token",
    "tokenExpiration",
    "refreshToken",
    "refreshTokenExpiration",
)

_BRAVE_PATHS = (
    Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
    Path.home()
    / "Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
_BRAVE_IDENTIFIER = "com.brave.Browser"
_BRAVE_TEAM_ID = "KL8N8XSYF4"
_CHROME_PATHS = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
_CHROME_IDENTIFIER = "com.google.Chrome"
_CHROME_TEAM_ID = "EQHXZ8M8AV"
_PIPE_EXEC_WRAPPER = """\
import os
import sys

read_descriptor = int(sys.argv[1])
write_descriptor = int(sys.argv[2])
executable = sys.argv[3]
arguments = sys.argv[3:]
os.dup2(read_descriptor, 3, inheritable=True)
os.dup2(write_descriptor, 4, inheritable=True)
if read_descriptor not in {3, 4}:
    os.close(read_descriptor)
if write_descriptor not in {3, 4}:
    os.close(write_descriptor)
os.execv(executable, arguments)
"""


class BrowserBootstrapError(FamiliekilderError):
    """A sanitized isolated-browser bootstrap failure."""


class CdpClient(Protocol):
    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = _CDP_TIMEOUT_SECONDS,
    ) -> Mapping[str, Any]: ...

    def wait_for_event(
        self, methods: frozenset[str], *, timeout: float
    ) -> Mapping[str, Any] | None: ...


class BrowserHandle(Protocol):
    @property
    def cdp(self) -> CdpClient: ...

    def close(self) -> None: ...


BrowserFactory = Callable[[Path, str, Path | str | None], BrowserHandle]
WaitForUser = Callable[[str], None]
Importer = Callable[..., Mapping[str, Any]]


def _owned(metadata: os.stat_result) -> bool:
    return not hasattr(os, "geteuid") or metadata.st_uid == os.geteuid()


def _private_directory_ok(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and _owned(metadata)
        and (os.name == "nt" or metadata.st_mode & 0o077 == 0)
    )


def _private_file_ok(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and _owned(metadata)
        and (os.name == "nt" or metadata.st_mode & 0o077 == 0)
    )


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        if not _private_directory_ok(os.fstat(descriptor)):
            raise BrowserBootstrapError(
                "Den isolerte nettleserkatalogen har utrygg type, eier eller modus"
            )
        return descriptor
    except BrowserBootstrapError:
        try:
            os.close(descriptor)
        except (NameError, OSError):
            pass
        raise
    except OSError:
        raise BrowserBootstrapError(
            "Den isolerte nettleserkatalogen kunne ikke åpnes sikkert"
        ) from None


def _create_run_directory(root: Path | str | None) -> Path:
    """Create and mark one helper-owned directory below literal ``.private``."""

    base = project_paths(root).private / _BOOTSTRAP_DIRECTORY
    try:
        ensure_private_directory(base)
        if not validate_private_parent_tree(base / "probe"):
            raise BrowserBootstrapError(
                "Den private nettleserkatalogen mangler etter opprettelse"
            )
    except (PrivateStateError, OSError):
        raise BrowserBootstrapError(
            "Den private nettleserkatalogen kunne ikke sikres"
        ) from None
    base_descriptor = _open_directory(base)
    created_name: str | None = None

    def remove_incomplete_profile() -> None:
        if created_name is None:
            return
        descriptor: int | None = None
        try:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(created_name, flags, dir_fd=base_descriptor)
            if not _private_directory_ok(os.fstat(descriptor)):
                raise OSError
            _purge_directory(descriptor)
            os.close(descriptor)
            descriptor = None
            os.rmdir(created_name, dir_fd=base_descriptor)
            os.fsync(base_descriptor)
        except Exception:
            raise BrowserBootstrapError(
                "En ufullstendig nettleserprofil kunne ikke ryddes sikkert"
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    try:
        for _ in range(20):
            name = f"run-{secrets.token_hex(16)}"
            try:
                os.mkdir(name, mode=0o700, dir_fd=base_descriptor)
                created_name = name
                break
            except FileExistsError:
                continue
            except OSError:
                raise BrowserBootstrapError(
                    "En isolert nettleserprofil kunne ikke opprettes sikkert"
                ) from None
        else:
            raise BrowserBootstrapError(
                "En unik isolert nettleserprofil kunne ikke opprettes"
            )
        run_path = base / name
        run_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        run_descriptor = os.open(name, run_flags, dir_fd=base_descriptor)
        try:
            metadata = os.fstat(run_descriptor)
            if not _private_directory_ok(metadata):
                raise BrowserBootstrapError(
                    "Den isolerte nettleserprofilen har utrygge rettigheter"
                )
            marker_flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
            )
            marker = os.open(
                _OWNER_MARKER,
                marker_flags,
                0o600,
                dir_fd=run_descriptor,
            )
            try:
                os.write(marker, _OWNER_MARKER_CONTENT)
                os.fsync(marker)
            finally:
                os.close(marker)
            os.fsync(run_descriptor)
        finally:
            os.close(run_descriptor)
        os.fsync(base_descriptor)
        return run_path
    except BrowserBootstrapError:
        remove_incomplete_profile()
        raise
    except OSError:
        remove_incomplete_profile()
        raise BrowserBootstrapError(
            "Den isolerte nettleserprofilen kunne ikke markeres sikkert"
        ) from None
    finally:
        os.close(base_descriptor)


def _purge_directory(descriptor: int) -> None:
    """Delete directory contents relative to an already verified directory fd."""

    try:
        names = os.listdir(descriptor)
    except OSError:
        raise BrowserBootstrapError(
            "Den isolerte nettleserprofilen kunne ikke ryddes sikkert"
        ) from None
    for name in names:
        if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name:
            raise BrowserBootstrapError(
                "Den isolerte nettleserprofilen inneholder et ugyldig filnavn"
            )
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                child_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                child = os.open(name, child_flags, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    if (
                        not stat.S_ISDIR(opened.st_mode)
                        or opened.st_dev != metadata.st_dev
                        or opened.st_ino != metadata.st_ino
                        or not _owned(opened)
                    ):
                        raise BrowserBootstrapError(
                            "En nettleserprofilkatalog endret seg under opprydding"
                        )
                    _purge_directory(child)
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=descriptor)
            else:
                os.unlink(name, dir_fd=descriptor)
        except BrowserBootstrapError:
            raise
        except OSError:
            raise BrowserBootstrapError(
                "Den isolerte nettleserprofilen kunne ikke ryddes sikkert"
            ) from None


def _cleanup_run_directory(root: Path | str | None, run_path: Path) -> None:
    """Remove exactly one verified helper-owned profile without following links."""

    base = project_paths(root).private / _BOOTSTRAP_DIRECTORY
    if (
        not isinstance(run_path, Path)
        or run_path.parent != base
        or not _RUN_DIRECTORY.fullmatch(run_path.name)
    ):
        raise BrowserBootstrapError(
            "Avviste opprydding av en nettleserprofil utenfor privat område"
        )
    try:
        if not validate_private_parent_tree(run_path):
            raise BrowserBootstrapError(
                "Den isolerte nettleserprofilen mangler ved opprydding"
            )
    except PrivateStateError:
        raise BrowserBootstrapError(
            "Foreldretreet til nettleserprofilen er ikke sikkert"
        ) from None

    base_descriptor = _open_directory(base)
    run_descriptor: int | None = None
    marker_descriptor: int | None = None
    try:
        metadata = os.stat(run_path.name, dir_fd=base_descriptor, follow_symlinks=False)
        if not _private_directory_ok(metadata):
            raise BrowserBootstrapError(
                "Nettleserprofilen har utrygg type, eier eller modus"
            )
        run_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        run_descriptor = os.open(run_path.name, run_flags, dir_fd=base_descriptor)
        opened = os.fstat(run_descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or not _private_directory_ok(opened)
        ):
            raise BrowserBootstrapError(
                "Nettleserprofilen endret seg før opprydding"
            )
        marker_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        marker_descriptor = os.open(
            _OWNER_MARKER, marker_flags, dir_fd=run_descriptor
        )
        marker_metadata = os.fstat(marker_descriptor)
        if not _private_file_ok(marker_metadata):
            raise BrowserBootstrapError(
                "Nettleserprofilens eierskapsmarkør er ikke sikker"
            )
        marker_content = os.read(marker_descriptor, len(_OWNER_MARKER_CONTENT) + 1)
        if marker_content != _OWNER_MARKER_CONTENT:
            raise BrowserBootstrapError(
                "Nettleserprofilens eierskapsmarkør er ugyldig"
            )
        os.close(marker_descriptor)
        marker_descriptor = None
        _purge_directory(run_descriptor)
        os.fsync(run_descriptor)
        os.close(run_descriptor)
        run_descriptor = None
        final_metadata = os.stat(
            run_path.name, dir_fd=base_descriptor, follow_symlinks=False
        )
        if (
            final_metadata.st_dev != metadata.st_dev
            or final_metadata.st_ino != metadata.st_ino
            or not stat.S_ISDIR(final_metadata.st_mode)
        ):
            raise BrowserBootstrapError(
                "Nettleserprofilen endret seg under opprydding"
            )
        os.rmdir(run_path.name, dir_fd=base_descriptor)
        os.fsync(base_descriptor)
    except BrowserBootstrapError:
        raise
    except (FileNotFoundError, OSError):
        raise BrowserBootstrapError(
            "Den isolerte nettleserprofilen kunne ikke ryddes sikkert"
        ) from None
    finally:
        if marker_descriptor is not None:
            os.close(marker_descriptor)
        if run_descriptor is not None:
            os.close(run_descriptor)
        os.close(base_descriptor)


class _CdpFramer:
    """NUL-delimited DevTools pipe framing with a bounded secret-bearing buffer."""

    def __init__(self, *, maximum: int = _MAX_CDP_FRAME_BYTES) -> None:
        self._buffer = bytearray()
        self._maximum = maximum

    @staticmethod
    def encode(value: Mapping[str, Any]) -> bytes:
        try:
            return json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\0"
        except (TypeError, ValueError):
            raise BrowserBootstrapError(
                "DevTools-forespørselen kunne ikke serialiseres"
            ) from None

    def feed(self, chunk: bytes) -> list[Mapping[str, Any]]:
        if not isinstance(chunk, bytes) or not chunk:
            raise BrowserBootstrapError("DevTools-røret ble lukket uventet")
        self._buffer.extend(chunk)
        if len(self._buffer) > self._maximum and b"\0" not in self._buffer:
            self._buffer.clear()
            raise BrowserBootstrapError("DevTools-responsen var større enn tillatt")
        values: list[Mapping[str, Any]] = []
        while True:
            try:
                end = self._buffer.index(0)
            except ValueError:
                break
            raw = bytes(self._buffer[:end])
            del self._buffer[: end + 1]
            if not raw or len(raw) > self._maximum:
                raise BrowserBootstrapError("DevTools-responsen hadde ugyldig framing")
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise BrowserBootstrapError(
                    "DevTools-responsen hadde ugyldig JSON"
                ) from None
            if not isinstance(value, Mapping):
                raise BrowserBootstrapError(
                    "DevTools-responsen hadde ugyldig format"
                )
            values.append(value)
        if len(self._buffer) > self._maximum:
            self._buffer.clear()
            raise BrowserBootstrapError("DevTools-responsen var større enn tillatt")
        return values


class _PipeCdpClient:
    def __init__(self, write_descriptor: int, read_descriptor: int) -> None:
        self._write_descriptor = write_descriptor
        self._read_descriptor = read_descriptor
        self._framer = _CdpFramer()
        self._pending: list[Mapping[str, Any]] = []
        self._events: list[Mapping[str, Any]] = []
        self._next_id = 1
        self._closed = False

    def _write(self, payload: bytes) -> None:
        offset = 0
        try:
            while offset < len(payload):
                written = os.write(self._write_descriptor, payload[offset:])
                if written <= 0:
                    raise OSError
                offset += written
        except OSError:
            raise BrowserBootstrapError(
                "DevTools-røret kunne ikke skrive forespørselen"
            ) from None

    def _next_message(self, deadline: float) -> Mapping[str, Any]:
        while not self._pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserBootstrapError("Brave svarte ikke innen tidsgrensen")
            try:
                ready, _, _ = select.select([self._read_descriptor], [], [], remaining)
                if not ready:
                    raise BrowserBootstrapError("Brave svarte ikke innen tidsgrensen")
                chunk = os.read(self._read_descriptor, 64 * 1024)
            except OSError:
                raise BrowserBootstrapError(
                    "DevTools-røret kunne ikke lese responsen"
                ) from None
            self._pending.extend(self._framer.feed(chunk))
        return self._pending.pop(0)

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = _CDP_TIMEOUT_SECONDS,
    ) -> Mapping[str, Any]:
        if self._closed:
            raise BrowserBootstrapError("DevTools-røret er allerede lukket")
        if (
            not isinstance(method, str)
            or not method
            or len(method) > 128
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise BrowserBootstrapError("DevTools-forespørselen er ugyldig")
        request_id = self._next_id
        self._next_id += 1
        request: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = dict(params)
        if session_id is not None:
            request["sessionId"] = session_id
        self._write(_CdpFramer.encode(request))
        deadline = time.monotonic() + float(timeout)
        while True:
            response = self._next_message(deadline)
            if "id" not in response:
                self._events.append(response)
                if len(self._events) > 1024:
                    self._events.clear()
                    raise BrowserBootstrapError(
                        "Brave returnerte for mange DevTools-hendelser"
                    )
                continue
            if response.get("id") != request_id:
                raise BrowserBootstrapError(
                    "DevTools-røret returnerte en ukjent respons"
                )
            if "error" in response:
                raise BrowserBootstrapError(
                    f"Brave avviste DevTools-forespørselen {method}"
                )
            result = response.get("result")
            if not isinstance(result, Mapping):
                raise BrowserBootstrapError(
                    "DevTools-responsen mangler et gyldig resultat"
                )
            return result

    def wait_for_event(
        self, methods: frozenset[str], *, timeout: float
    ) -> Mapping[str, Any] | None:
        if (
            self._closed
            or not methods
            or not all(isinstance(method, str) and method for method in methods)
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise BrowserBootstrapError("DevTools-hendelsesventingen er ugyldig")
        deadline = time.monotonic() + float(timeout)
        while True:
            if self._events:
                event = self._events.pop(0)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    event = self._next_message(deadline)
                except BrowserBootstrapError as exc:
                    if str(exc) == "Brave svarte ikke innen tidsgrensen":
                        return None
                    raise
            if "id" in event:
                raise BrowserBootstrapError(
                    "DevTools-røret returnerte en uventet respons"
                )
            if event.get("method") in methods:
                return event

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in (self._write_descriptor, self._read_descriptor):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _codesign_value(output: str, label: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(label)}=([^\r\n]+)$", output)
    return match.group(1).strip() if match else None


def _official_brave_path(
    explicit: Path | str | None = None, *, allow_chrome: bool = False
) -> Path:
    if sys.platform.startswith("linux"):
        # APT verifies the repository signature at installation. Do not accept
        # a caller-selected executable, user-writable wrapper or symlink here.
        candidate = Path("/opt/brave.com/brave/brave")
        if explicit is not None and Path(explicit) != candidate:
            raise BrowserBootstrapError("Brave-stien er ikke en godkjent appsti")
        try:
            for entry in (candidate, *candidate.parents[:-1]):
                info = entry.lstat()
                if info.st_uid != 0 or info.st_mode & 0o022 or entry.is_symlink():
                    raise OSError
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise OSError
        except OSError:
            raise BrowserBootstrapError("Fant ikke en administrert Brave-installasjon") from None
        return candidate
    if sys.platform != "darwin":
        raise BrowserBootstrapError(
            "Isolert Brave-bootstrap støttes bare på en verifisert macOS-installasjon"
        )
    candidates = _BRAVE_PATHS + _CHROME_PATHS if allow_chrome else _BRAVE_PATHS
    if explicit is not None:
        requested = Path(explicit).expanduser()
        if requested not in candidates:
            raise BrowserBootstrapError("Brave-stien er ikke en godkjent appsti")
        candidates = (requested,)
    selected: Path | None = None
    for candidate in candidates:
        try:
            metadata = candidate.lstat()
        except OSError:
            continue
        if (
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and os.access(candidate, os.X_OK)
        ):
            selected = candidate
            break
    if selected is None:
        raise BrowserBootstrapError("Fant ikke den offisielle Brave-installasjonen")
    try:
        assessment = subprocess.run(
            [
                "/usr/sbin/spctl",
                "--assess",
                "--type",
                "execute",
                str(selected.parents[2]),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        details = subprocess.run(
            ["/usr/bin/codesign", "-dv", "--verbose=4", str(selected)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise BrowserBootstrapError(
            "Brave-signaturen kunne ikke verifiseres lokalt"
        ) from None
    expected_identity = (
        (_CHROME_IDENTIFIER, _CHROME_TEAM_ID)
        if selected in _CHROME_PATHS
        else (_BRAVE_IDENTIFIER, _BRAVE_TEAM_ID)
    )
    if (
        assessment.returncode != 0
        or details.returncode != 0
        or _codesign_value(details.stdout, "Identifier") != expected_identity[0]
        or _codesign_value(details.stdout, "TeamIdentifier") != expected_identity[1]
    ):
        raise BrowserBootstrapError("Brave-installasjonen har ukjent signatur")
    return selected


class _BraveHandle:
    def __init__(self, process: subprocess.Popen[bytes], cdp: _PipeCdpClient) -> None:
        self._process = process
        self._cdp = cdp
        self._closed = False

    @property
    def cdp(self) -> CdpClient:
        return self._cdp

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.poll() is None:
                try:
                    self._cdp.call("Browser.close", timeout=3.0)
                except Exception:
                    pass
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait(timeout=3)
        finally:
            self._cdp.close()


def _duplicate_child_descriptor(descriptor: int) -> int:
    try:
        return int(fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 10))
    except (OSError, ValueError):
        raise BrowserBootstrapError(
            "DevTools-røret kunne ikke opprettes sikkert"
        ) from None


def _brave_arguments(executable: Path, profile_directory: Path, start_url: str) -> list[str]:
    """Build the fixed visible-browser invocation without a TCP debug endpoint."""

    disabled_features = ",".join(
        (
            "BraveSync",
            "PasswordImport",
            "PasswordManagerEnabled",
            "PasswordManagerOnboarding",
        )
    )
    arguments = [
        str(executable),
        f"--user-data-dir={profile_directory}",
        "--profile-directory=Bootstrap",
        "--remote-debugging-pipe",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-save-password-bubble",
        "--disable-password-generation",
        "--disable-background-mode",
        "--disable-crash-reporter",
        "--disable-breakpad",
        f"--disable-features={disabled_features}",
        "--new-window",
        start_url,
    ]
    if os.environ.get('FAMILIEKILDER_HOSTED') == '1':
        # A login server shows this window remotely. Its managed URL policy
        # enforces navigation; app mode only simplifies the UI.
        window_size = os.environ.get('FAMILIEKILDER_HOSTED_BROWSER_SIZE', '1280,800')
        if window_size not in {'500,840', '1280,800'}:
            raise BrowserBootstrapError('Ugyldig størrelse på innloggingsvinduet')
        # A headless Xvfb display has no window manager to honor maximize hints.
        arguments[-2:] = [f'--app={start_url}', '--window-position=0,0',
                          f'--window-size={window_size}']
        if window_size == '500,840':
            # Window size is in CSS pixels, so this fills a 2x display.
            arguments.append('--force-device-scale-factor=2')
    return arguments


def _launch_brave(
    profile_directory: Path,
    start_url: str,
    brave_path: Path | str | None,
    *,
    allow_chrome: bool = False,
) -> BrowserHandle:
    executable = _official_brave_path(brave_path, allow_chrome=allow_chrome)
    try:
        profile_metadata = profile_directory.lstat()
    except OSError:
        raise BrowserBootstrapError(
            "Den isolerte nettleserprofilen kunne ikke kontrolleres"
        ) from None
    if not _private_directory_ok(profile_metadata):
        raise BrowserBootstrapError(
            "Den isolerte nettleserprofilen har utrygge rettigheter"
        )

    child_input_raw, parent_write = os.pipe()
    parent_read, child_output_raw = os.pipe()
    child_input: int | None = None
    child_output: int | None = None
    process: subprocess.Popen[bytes] | None = None
    try:
        child_input = _duplicate_child_descriptor(child_input_raw)
        child_output = _duplicate_child_descriptor(child_output_raw)
        os.close(child_input_raw)
        child_input_raw = -1
        os.close(child_output_raw)
        child_output_raw = -1

        arguments = _brave_arguments(executable, profile_directory, start_url)
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _PIPE_EXEC_WRAPPER,
                str(child_input),
                str(child_output),
                *arguments,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(child_input, child_output),
            start_new_session=True,
        )
        os.close(child_input)
        child_input = None
        os.close(child_output)
        child_output = None
        return _BraveHandle(process, _PipeCdpClient(parent_write, parent_read))
    except BrowserBootstrapError:
        raise
    except OSError:
        raise BrowserBootstrapError(
            "Den isolerte Brave-profilen kunne ikke startes"
        ) from None
    finally:
        for descriptor in (child_input_raw, child_output_raw, child_input, child_output):
            if descriptor is not None and descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if process is None:
            for descriptor in (parent_write, parent_read):
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _exact_origin(url: Any, expected: str) -> bool:
    if not isinstance(url, str) or len(url) > 4096:
        return False
    try:
        parsed = urlsplit(url)
        expected_parts = urlsplit(expected)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and expected_parts.scheme == "https"
        and parsed.hostname == expected_parts.hostname
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


def _page_session(cdp: CdpClient, origin: str) -> str:
    targets = cdp.call("Target.getTargets").get("targetInfos")
    if not isinstance(targets, list):
        raise BrowserBootstrapError("Brave returnerte en ukjent sidelayout")
    matching = [
        target
        for target in targets
        if isinstance(target, Mapping)
        and target.get("type") == "page"
        and _exact_origin(target.get("url"), origin)
    ]
    if len(matching) != 1:
        raise BrowserBootstrapError(
            "Fant ikke nøyaktig én innlogget side på forventet origin"
        )
    target_id = matching[0].get("targetId")
    if not isinstance(target_id, str) or not target_id or len(target_id) > 512:
        raise BrowserBootstrapError("Brave returnerte en ugyldig side-ID")
    attached = cdp.call(
        "Target.attachToTarget", {"targetId": target_id, "flatten": True}
    )
    session_id = attached.get("sessionId")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
        raise BrowserBootstrapError("Brave returnerte en ugyldig sesjons-ID")
    return session_id


def _single_https_page_session(
    cdp: CdpClient, *, timeout: float = _CDP_TIMEOUT_SECONDS
) -> str:
    """Attach to the one visible HTTPS page in a disposable profile."""

    deadline = time.monotonic() + float(timeout)
    pages: list[Mapping[str, Any]] = []
    while time.monotonic() < deadline:
        targets = cdp.call("Target.getTargets").get("targetInfos")
        if not isinstance(targets, list):
            raise BrowserBootstrapError("Brave returnerte en ukjent sidelayout")
        pages = []
        for target in targets:
            if not isinstance(target, Mapping) or target.get("type") != "page":
                continue
            url = target.get("url")
            if not isinstance(url, str) or len(url) > 4096:
                continue
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            if parsed.scheme == "https" and parsed.hostname:
                pages.append(target)
        if len(pages) == 1:
            break
        if len(pages) > 1:
            raise BrowserBootstrapError(
                "Fant mer enn én autorisasjonsside i isolert Brave"
            )
        time.sleep(0.1)
    else:
        raise BrowserBootstrapError(
            "Fant ikke autorisasjonssiden i isolert Brave"
        )
    target_id = pages[0].get("targetId")
    if not isinstance(target_id, str) or not target_id or len(target_id) > 512:
        raise BrowserBootstrapError("Brave returnerte en ugyldig side-ID")
    attached = cdp.call(
        "Target.attachToTarget", {"targetId": target_id, "flatten": True}
    )
    session_id = attached.get("sessionId")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
        raise BrowserBootstrapError("Brave returnerte en ugyldig sesjons-ID")
    return session_id


_CALLBACK_EVENTS = frozenset(
    {
        "Network.responseReceived",
        "Network.responseReceivedExtraInfo",
        "Page.frameRequestedNavigation",
        "Page.frameNavigated",
        "Page.navigatedWithinDocument",
        "Target.targetInfoChanged",
    }
)


def _report_unreachable(event: Mapping[str, Any], expected_redirect_uri: str) -> None:
    """Report only the origin of a failed browser navigation, never its URL."""
    try:
        if event.get('method') != 'Page.frameNavigated':
            return
        frame = (event.get('params') or {}).get('frame') or {}
        value = frame.get('unreachableUrl')
        if not isinstance(value, str):
            return
        parsed = urlsplit(value)
        expected = urlsplit(expected_redirect_uri)
        if (parsed.scheme, parsed.netloc, parsed.path) == (expected.scheme, expected.netloc, expected.path):
            return
        host = parsed.hostname
        if parsed.scheme not in {'http', 'https', 'bankid', 'intent', 'about'}:
            return
        if host and not re.fullmatch(r'[a-z0-9.-]{1,253}', host):
            return
        if parsed.scheme in {'http', 'https'} and not host:
            return
        service = {'com.cgi.oslomelding.foresatte': 'skolemelding',
                   'com.ist.home.miniforum-school': 'ist-home'}.get(expected.scheme, 'other')
        logging.getLogger(__name__).warning('auth_navigation_unreachable service=%s scheme=%s host=%s', service, parsed.scheme, host or 'none')
    except Exception:
        # Diagnostics must never alter the authentication flow.
        pass


def _header_location(headers: Any) -> str | None:
    if not isinstance(headers, Mapping):
        return None
    matches = [
        value
        for name, value in headers.items()
        if isinstance(name, str)
        and name.lower() == "location"
        and isinstance(value, str)
    ]
    return matches[0] if len(matches) == 1 else None


def _event_url(event: Mapping[str, Any]) -> str | None:
    method = event.get("method")
    params = event.get("params")
    if not isinstance(params, Mapping):
        return None
    if method in {"Page.frameRequestedNavigation", "Page.navigatedWithinDocument"}:
        value = params.get("url")
    elif method == "Page.frameNavigated":
        frame = params.get("frame")
        value = frame.get("url") if isinstance(frame, Mapping) else None
    elif method == "Target.targetInfoChanged":
        info = params.get("targetInfo")
        value = info.get("url") if isinstance(info, Mapping) else None
    elif method == "Network.responseReceived":
        response = params.get("response")
        value = (
            _header_location(response.get("headers"))
            if isinstance(response, Mapping)
            else None
        )
    elif method == "Network.responseReceivedExtraInfo":
        value = _header_location(params.get("headers"))
    else:
        return None
    return value if isinstance(value, str) else None


def _matches_callback(candidate: Any, expected_redirect_uri: str) -> bool:
    if (
        not isinstance(candidate, str)
        or not candidate
        or len(candidate) > 8192
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        return False
    try:
        actual = urlsplit(candidate)
        expected = urlsplit(expected_redirect_uri)
    except ValueError:
        return False
    return (
        actual.scheme == expected.scheme
        and actual.netloc == expected.netloc
        # The provider appends a root slash to a redirect registered without a path.
        and (actual.path == expected.path or (actual.path == "/" and not expected.path))
        and actual.username is None
        and actual.password is None
    )


def capture_oauth_callback(
    root: Path | str | None,
    *,
    authorization_url: str,
    expected_redirect_uri: str,
    timeout: float = 10 * 60,
    brave_path: Path | str | None = None,
    browser_factory: BrowserFactory | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """Capture one exact custom-scheme callback without displaying its value."""

    try:
        authorization = urlsplit(authorization_url)
        expected = urlsplit(expected_redirect_uri)
    except ValueError:
        raise BrowserBootstrapError("OAuth-nettleserflyten har ugyldig URL") from None
    if (
        authorization.scheme != "https"
        or not authorization.hostname
        or authorization.username is not None
        or authorization.password is not None
        or expected.scheme in {"", "http", "https"}
        or not expected.netloc
        or expected.query
        or expected.fragment
        or not math.isfinite(float(timeout))
        or timeout <= 0
        or timeout > 15 * 60
    ):
        raise BrowserBootstrapError("OAuth-nettleserflyten har ugyldig URL")

    from .password_autofill import PasswordAutofill

    autofill = PasswordAutofill(root, "bankid")
    run_path = _create_run_directory(root)
    browser: BrowserHandle | None = None
    primary_error: BaseException | None = None
    try:
        if browser_factory is not None:
            browser = browser_factory(run_path, authorization_url, brave_path)
        else:
            browser = _launch_brave(
                run_path,
                authorization_url,
                brave_path,
                allow_chrome=True,
            )
        session_id = _single_https_page_session(browser.cdp)
        browser.cdp.call("Page.enable", session_id=session_id)
        # Chromium can enforce the ID-porten page's form-action CSP across the
        # HTTPS response's redirect to a custom app scheme. Observe only the
        # response metadata so the already-issued callback can be captured
        # before Chromium blocks that final navigation.
        browser.cdp.call("Network.enable", session_id=session_id)
        browser.cdp.call("Target.setDiscoverTargets", {"discover": True})
        deadline = time.monotonic() + float(timeout)
        while True:
            if cancelled is not None and cancelled():
                raise BrowserBootstrapError("Innloggingen ble avbrutt")
            _password_step(autofill, browser.cdp)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserBootstrapError(
                    "OAuth-callbacken kom ikke innen tidsgrensen"
                )
            event = browser.cdp.wait_for_event(
                _CALLBACK_EVENTS, timeout=min(1.0, remaining)
            )
            if event is not None:
                _report_unreachable(event, expected_redirect_uri)
                candidate = _event_url(event)
                if _matches_callback(candidate, expected_redirect_uri):
                    return candidate
            targets = browser.cdp.call("Target.getTargets").get("targetInfos")
            if not isinstance(targets, list):
                raise BrowserBootstrapError("Brave returnerte en ukjent sidelayout")
            for target in targets:
                if not isinstance(target, Mapping):
                    continue
                candidate = target.get("url")
                if _matches_callback(candidate, expected_redirect_uri):
                    return candidate
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        close_error: BaseException | None = None
        if browser is not None:
            try:
                _close_browser(browser)
            except BaseException as exc:
                close_error = exc
        cleanup_error: BaseException | None = None
        try:
            _cleanup_run_directory(root, run_path)
        except BaseException as exc:
            cleanup_error = exc
        if close_error is not None:
            if primary_error is not None:
                raise BrowserBootstrapError(
                    "OAuth-flyten feilet og isolert Brave kunne ikke lukkes"
                ) from None
            raise close_error
        if cleanup_error is not None:
            if primary_error is not None:
                raise BrowserBootstrapError(
                    "OAuth-flyten feilet og nettleserprofilen kunne ikke ryddes"
                ) from None
            raise cleanup_error


def _runtime_value(response: Mapping[str, Any]) -> Any:
    if "exceptionDetails" in response:
        raise BrowserBootstrapError("Auth-feltene kunne ikke leses fra innlogget side")
    remote = response.get("result")
    if not isinstance(remote, Mapping) or "value" not in remote:
        raise BrowserBootstrapError("Innlogget side returnerte en ukjent auth-layout")
    return remote.get("value")


def _secret_text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8", "ignore")) > _MAX_SECRET_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise BrowserBootstrapError(f"Det isolerte auth-uttrekket har ugyldig {label}")
    return value


def _spond_payload(cdp: CdpClient) -> dict[str, Any]:
    session_id = _page_session(cdp, SPOND_ORIGIN)
    expression = """(() => {
  "use strict";
  const result = Object.create(null);
  const keys = ["token", "tokenExpiration", "refreshToken", "refreshTokenExpiration"];
  for (const key of keys) {
    const value = window.localStorage.getItem(key);
    if (value !== null) result[key] = value;
  }
  return result;
})()"""
    value = _runtime_value(
        cdp.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
                "userGesture": False,
                "silent": True,
            },
            session_id=session_id,
        )
    )
    return spond_fields(value)


def spond_fields(value: Any) -> dict[str, Any]:
    """Validate the four Spond localStorage fields, however they were read.

    The Brave bootstrap reads them over CDP; native iOS login reads them
    from its isolated WebView. Both must pass the same checks.
    """

    if not isinstance(value, Mapping) or set(value) != set(_SPOND_STORAGE_KEYS):
        raise BrowserBootstrapError(
            "Spond-siden har en ukjent eller ufullstendig auth-layout"
        )
    token = _secret_text(value.get("token"), "Spond access-token")
    refresh_token = _secret_text(
        value.get("refreshToken"), "Spond refresh-token"
    )
    for expiry_key in ("tokenExpiration", "refreshTokenExpiration"):
        expiry = value.get(expiry_key)
        if (
            isinstance(expiry, bool)
            or not isinstance(expiry, (str, int, float))
            or (isinstance(expiry, str) and (not expiry or len(expiry) > 128))
        ):
            raise BrowserBootstrapError("Spond-siden har ugyldig tokenutløp")
    return {
        "token": token,
        "tokenExpiration": value["tokenExpiration"],
        "refreshToken": refresh_token,
        "refreshTokenExpiration": value["refreshTokenExpiration"],
    }


def _remote_scalar(remote: Any) -> Any:
    if not isinstance(remote, Mapping) or "value" not in remote:
        raise BrowserBootstrapError("Hoopit returnerte en ukjent IndexedDB-nøkkel")
    return remote.get("value")


def _firebase_claim_binding(id_token: str, project_id: str) -> None:
    parts = id_token.split(".")
    if len(parts) != 3 or not all(parts):
        raise BrowserBootstrapError("Hoopit Firebase-tokenet har ugyldig format")
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        if len(raw) > 128 * 1024:
            raise ValueError
        claims = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise BrowserBootstrapError(
            "Hoopit Firebase-tokenet har ugyldig prosjektbinding"
        ) from None
    if (
        not isinstance(claims, Mapping)
        or claims.get("aud") != project_id
        or claims.get("iss") != f"https://securetoken.google.com/{project_id}"
    ):
        raise BrowserBootstrapError(
            "Hoopit Firebase-tokenet samsvarer ikke med providerprofilen"
        )


def _hoopit_payload(
    cdp: CdpClient, *, firebase_api_key: str, firebase_project_id: str
) -> dict[str, str]:
    session_id = _page_session(cdp, HOOPIT_ORIGIN)
    cdp.call("IndexedDB.enable", session_id=session_id)
    record_key = f"firebase:authUser:{firebase_api_key}:[DEFAULT]"
    response = cdp.call(
        "IndexedDB.requestData",
        {
            "securityOrigin": HOOPIT_ORIGIN,
            "databaseName": "firebaseLocalStorageDb",
            "objectStoreName": "firebaseLocalStorage",
            "skipCount": 0,
            "pageSize": 2,
            "keyRange": {
                "lower": {"type": "string", "string": record_key},
                "upper": {"type": "string", "string": record_key},
                "lowerOpen": False,
                "upperOpen": False,
            },
        },
        session_id=session_id,
    )
    entries = response.get("objectStoreDataEntries")
    if response.get("hasMore") is not False or not isinstance(entries, list) or len(entries) != 1:
        raise BrowserBootstrapError(
            "Hoopit har en ukjent, manglende eller duplisert Firebase-layout"
        )
    entry = entries[0]
    if not isinstance(entry, Mapping) or _remote_scalar(entry.get("key")) != record_key:
        raise BrowserBootstrapError("Hoopit Firebase-nøkkelen samsvarer ikke")
    if "primaryKey" in entry and _remote_scalar(entry.get("primaryKey")) != record_key:
        raise BrowserBootstrapError("Hoopit Firebase-primærnøkkelen samsvarer ikke")
    remote_record = entry.get("value")
    if not isinstance(remote_record, Mapping):
        raise BrowserBootstrapError("Hoopit Firebase-recorden har ukjent format")
    object_id = remote_record.get("objectId")
    if not isinstance(object_id, str) or not object_id or len(object_id) > 2048:
        raise BrowserBootstrapError("Hoopit Firebase-recorden kan ikke leses avgrenset")
    extraction = _runtime_value(
        cdp.call(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": """function () {
  "use strict";
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key)
    ? object[key] : null;
  if (this === null || typeof this !== "object") return null;
  const value = own(this, "value");
  if (value === null || typeof value !== "object") return null;
  const manager = own(value, "stsTokenManager");
  if (manager === null || typeof manager !== "object") return null;
  return {
    apiKey: own(value, "apiKey"),
    accessToken: own(manager, "accessToken"),
    refreshToken: own(manager, "refreshToken")
  };
}""",
                "returnByValue": True,
                "silent": True,
            },
            session_id=session_id,
        )
    )
    return hoopit_fields(
        extraction,
        firebase_api_key=firebase_api_key,
        firebase_project_id=firebase_project_id,
    )


def hoopit_fields(
    extraction: Any, *, firebase_api_key: str, firebase_project_id: str
) -> dict[str, str]:
    """Validate the three fields read from Hoopit's Firebase auth record.

    Shared by the CDP bootstrap and native iOS login.
    """

    if not isinstance(extraction, Mapping) or set(extraction) != {
        "apiKey",
        "accessToken",
        "refreshToken",
    }:
        raise BrowserBootstrapError("Hoopit Firebase-recorden har ukjent auth-layout")
    if extraction.get("apiKey") != firebase_api_key:
        raise BrowserBootstrapError(
            "Hoopit Firebase API-nøkkelen samsvarer ikke med providerprofilen"
        )
    id_token = _secret_text(
        extraction.get("accessToken"), "Hoopit Firebase ID-token"
    )
    refresh_token = _secret_text(
        extraction.get("refreshToken"), "Hoopit Firebase refresh-token"
    )
    _firebase_claim_binding(id_token, firebase_project_id)
    return {"id_token": id_token, "refresh_token": refresh_token}


def _default_wait(prompt: str) -> None:
    try:
        input(prompt)
    except (EOFError, OSError):
        raise BrowserBootstrapError(
            "Kunne ikke vente på manuell innlogging i Brave"
        ) from None


def _password_step(autofill, cdp):
    try:
        autofill.step(cdp)
    except FamiliekilderError:
        autofill.attempted = True
        print("Passordutfylling er utilgjengelig; fullfør passordsteget manuelt.", file=sys.stderr)


def _wait_with_password(prompt, autofill, cdp):
    if not autofill.enabled:
        return _default_wait(prompt)
    if not sys.stdin.isatty():
        raise BrowserBootstrapError("Innloggingen krever en interaktiv terminal")
    print(prompt, end="", flush=True)
    while True:
        _password_step(autofill, cdp)
        if select.select([sys.stdin], [], [], 1.0)[0]:
            if not sys.stdin.readline():
                raise BrowserBootstrapError("Innloggingen ble avbrutt")
            return


def _timestamp_metadata(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise BrowserBootstrapError("Tokenimporten returnerte ugyldig tidsmetadata")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise BrowserBootstrapError(
            "Tokenimporten returnerte ugyldig tidsmetadata"
        ) from None
    if parsed.tzinfo is None:
        raise BrowserBootstrapError("Tokenimportens tidsmetadata mangler tidssone")
    return value


def _safe_import_metadata(source: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BrowserBootstrapError("Tokenimporten returnerte ugyldig metadata")
    common = {"configured", "authenticated", "expires_at", "attachment_hosts"}
    expected = (
        common
        | {
            "remote_verified",
            "refresh_expires_at",
            "refresh_marker_cleanup_pending",
        }
        if source == "spond"
        else common | {"firebase_verified"}
    )
    if not expected.issubset(value):
        raise BrowserBootstrapError("Tokenimporten mangler forventet metadata")
    result: dict[str, Any] = {"source": source}
    for key in sorted(expected):
        item = value.get(key)
        if key.endswith("expires_at"):
            result[key] = _timestamp_metadata(item)
        elif key == "attachment_hosts":
            if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 100:
                raise BrowserBootstrapError(
                    "Tokenimporten returnerte ugyldig vertsmetadata"
                )
            result[key] = item
        else:
            if not isinstance(item, bool):
                raise BrowserBootstrapError(
                    "Tokenimporten returnerte ugyldig statusmetadata"
                )
            result[key] = item
    return result


def _close_browser(browser: BrowserHandle | None) -> None:
    if browser is None:
        return
    try:
        browser.close()
    except Exception:
        raise BrowserBootstrapError(
            "Den isolerte Brave-profilen kunne ikke lukkes sikkert"
        ) from None


def _write_import_file(run_path: Path, payload: dict[str, Any]) -> Path:
    """Write the validated fields to one short-lived 0600 file, then forget them."""

    import_file = run_path / _IMPORT_FILE
    try:
        atomic_write_private_json(import_file, payload, overwrite=False)
        metadata = import_file.lstat()
        if not _private_file_ok(metadata) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise BrowserBootstrapError(
                "Den kortlivede tokenimportfilen har utrygge rettigheter"
            )
    except PrivateStateError:
        raise BrowserBootstrapError(
            "Den kortlivede tokenimportfilen kunne ikke lagres sikkert"
        ) from None
    finally:
        payload.clear()
    return import_file


def _run_import(
    root: Path | str | None,
    source: str,
    import_file: Path,
    importer: Importer,
    replace_existing: bool,
) -> dict[str, Any]:
    try:
        imported = importer(
            root,
            import_file,
            replace_existing=replace_existing,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise BrowserBootstrapError(
            f"{source.capitalize()}-økten kunne ikke importeres sikkert"
        ) from None
    return _safe_import_metadata(source, imported)


def import_extracted_fields(
    root: Path | str | None,
    source: str,
    fields: Any,
    *,
    replace_existing: bool = False,
    importer: Importer | None = None,
) -> dict[str, Any]:
    """Validate and import auth fields that were read outside Brave.

    Used by native iOS login and the development test. The fields pass the same
    checks as the CDP extraction and the same private import file and importer
    as ``bootstrap_spond``/``bootstrap_hoopit``.
    """

    if source == "spond":
        from .spond_auth import import_session, load_client_config

        try:
            load_client_config(root)
        except Exception:
            raise BrowserBootstrapError(
                "Spond-providerprofilen mangler eller er ugyldig"
            ) from None
        payload = spond_fields(fields)
    elif source == "hoopit":
        from .hoopit_auth import import_session, load_client_config

        try:
            config = load_client_config(root)
        except Exception:
            raise BrowserBootstrapError(
                "Hoopit-providerprofilen mangler eller er ugyldig"
            ) from None
        payload = hoopit_fields(
            fields,
            firebase_api_key=config.firebase_api_key,
            firebase_project_id=config.firebase_project_id,
        )
    else:
        raise BrowserBootstrapError(
            "Feltimport støttes bare for spond og hoopit"
        )
    run_path = _create_run_directory(root)
    primary_error: BaseException | None = None
    try:
        import_file = _write_import_file(run_path, payload)
        return _run_import(
            root, source, import_file, importer or import_session, replace_existing
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            _cleanup_run_directory(root, run_path)
        except BaseException:
            if primary_error is not None:
                raise BrowserBootstrapError(
                    "Importen feilet og den private importmappen kunne ikke ryddes"
                ) from None
            raise


def _bootstrap(
    root: Path | str | None,
    *,
    source: str,
    start_url: str,
    prompt: str,
    extractor: Callable[[CdpClient], Mapping[str, Any]],
    importer: Importer,
    replace_existing: bool,
    brave_path: Path | str | None,
    wait_for_user: WaitForUser | None,
    browser_factory: BrowserFactory | None,
) -> dict[str, Any]:
    run_path = _create_run_directory(root)
    browser: BrowserHandle | None = None
    primary_error: BaseException | None = None
    try:
        try:
            browser = (browser_factory or _launch_brave)(
                run_path, start_url, brave_path
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except BrowserBootstrapError:
            raise
        except Exception:
            raise BrowserBootstrapError(
                "Den isolerte Brave-profilen kunne ikke startes"
            ) from None
        if wait_for_user is not None:
            wait_for_user(prompt)
        else:
            from .password_autofill import PasswordAutofill

            _wait_with_password(prompt, PasswordAutofill(root, source), browser.cdp)
        import_file = _write_import_file(run_path, dict(extractor(browser.cdp)))
        _close_browser(browser)
        browser = None
        return _run_import(root, source, import_file, importer, replace_existing)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        close_error: BaseException | None = None
        if browser is not None:
            try:
                _close_browser(browser)
            except BaseException as exc:
                close_error = exc
        cleanup_error: BaseException | None = None
        try:
            _cleanup_run_directory(root, run_path)
        except BaseException as exc:
            cleanup_error = exc
        if close_error is not None:
            if primary_error is not None:
                raise BrowserBootstrapError(
                    "Bootstrapen feilet og den isolerte Brave-profilen kunne ikke lukkes"
                ) from None
            raise close_error
        if cleanup_error is not None:
            if primary_error is not None:
                raise BrowserBootstrapError(
                    "Bootstrapen feilet og den private nettleserprofilen kunne ikke ryddes"
                ) from None
            raise cleanup_error


def bootstrap_spond(
    root: Path | str | None,
    *,
    replace_existing: bool = False,
    brave_path: Path | str | None = None,
    wait_for_user: WaitForUser | None = None,
    browser_factory: BrowserFactory | None = None,
    importer: Importer | None = None,
) -> dict[str, Any]:
    """Capture and import exactly four Spond localStorage auth fields."""

    from .spond_auth import import_session, load_client_config

    try:
        load_client_config(root)
    except Exception:
        raise BrowserBootstrapError(
            "Spond-providerprofilen mangler eller er ugyldig"
        ) from None
    if importer is None:
        importer = import_session
    return _bootstrap(
        root,
        source="spond",
        start_url=SPOND_LOGIN_URL,
        prompt=(
            "Logg inn i Spond i det isolerte Brave-vinduet. "
            "Når Spond er ferdig lastet, gå tilbake hit og trykk Enter: "
        ),
        extractor=_spond_payload,
        importer=importer,
        replace_existing=replace_existing,
        brave_path=brave_path,
        wait_for_user=wait_for_user,
        browser_factory=browser_factory,
    )


def bootstrap_hoopit(
    root: Path | str | None,
    *,
    replace_existing: bool = False,
    brave_path: Path | str | None = None,
    wait_for_user: WaitForUser | None = None,
    browser_factory: BrowserFactory | None = None,
    importer: Importer | None = None,
) -> dict[str, Any]:
    """Capture one exact, provider-bound Hoopit Firebase Auth record."""

    from .hoopit_auth import import_session, load_client_config

    try:
        config = load_client_config(root)
    except Exception:
        raise BrowserBootstrapError(
            "Hoopit-providerprofilen mangler eller er ugyldig"
        ) from None
    selected_importer = importer or import_session

    def extract(cdp: CdpClient) -> Mapping[str, Any]:
        return _hoopit_payload(
            cdp,
            firebase_api_key=config.firebase_api_key,
            firebase_project_id=config.firebase_project_id,
        )

    return _bootstrap(
        root,
        source="hoopit",
        start_url=HOOPIT_LOGIN_URL,
        prompt=(
            "Logg inn i Hoopit i det isolerte Brave-vinduet. "
            "Når Hoopit er ferdig lastet, gå tilbake hit og trykk Enter: "
        ),
        extractor=extract,
        importer=selected_importer,
        replace_existing=replace_existing,
        brave_path=brave_path,
        wait_for_user=wait_for_user,
        browser_factory=browser_factory,
    )


def bootstrap_source(
    root: Path | str | None,
    source: str,
    **options: Any,
) -> dict[str, Any]:
    """Dispatch a supported isolated-browser bootstrap without fallback."""

    if source == "spond":
        return bootstrap_spond(root, **options)
    if source == "hoopit":
        return bootstrap_hoopit(root, **options)
    raise BrowserBootstrapError(
        "Isolert nettleserbootstrap støttes bare for spond og hoopit"
    )


__all__ = [
    "BrowserBootstrapError",
    "HOOPIT_LOGIN_URL",
    "HOOPIT_ORIGIN",
    "SPOND_LOGIN_URL",
    "SPOND_ORIGIN",
    "bootstrap_hoopit",
    "bootstrap_source",
    "bootstrap_spond",
    "capture_oauth_callback",
    "hoopit_fields",
    "import_extracted_fields",
    "spond_fields",
]
