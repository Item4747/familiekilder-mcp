"""Which browsers a login may start, and how each one is checked first.

A login handles BankID passwords and sessions, so it only starts a browser
installed by its vendor in a place the user cannot write to, and checks the
vendor's signature before every start:

- macOS: the app's code signature, Gatekeeper's assessment, and the vendor's
  bundle identifier and team ID.
- Windows: the Authenticode signature and the vendor's certificate subject,
  under the system's program folders, without links, and with no write access
  for ordinary users.
- Linux: the vendor package's fixed path, owned by root, without links and
  without write access for others.

A path the user names is accepted only when it is one of these paths. There is
no fallback to a TCP debugging port or to a browser outside the catalog.

Brave is first, as before this catalog existed, then Chrome, then Edge.

A login server (``FAMILIEKILDER_HOSTED=1``) keeps exactly what it did before
the catalog: Brave only, and on macOS also Chrome for an OAuth callback. On
Linux it also keeps the old package check (the executable and the folders
above it); a Linux desktop checks the whole package folder.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .core import FamiliekilderError


BRAVE = "brave"
CHROME = "chrome"
EDGE = "edge"
BROWSER_IDS = (BRAVE, CHROME, EDGE)

MACOS = "darwin"
WINDOWS = "win32"
LINUX = "linux"
PLATFORMS = (MACOS, WINDOWS, LINUX)

_NAMES = {BRAVE: "Brave", CHROME: "Google Chrome", EDGE: "Microsoft Edge"}

# macOS: (app bundle, executable, bundle identifier, team ID). The identifiers
# and team IDs are the ones `codesign -dv` prints for the vendors' own apps.
_MACOS_APPS = {
    BRAVE: ("Brave Browser.app", "Brave Browser", "com.brave.Browser", "KL8N8XSYF4"),
    CHROME: ("Google Chrome.app", "Google Chrome", "com.google.Chrome", "EQHXZ8M8AV"),
    EDGE: ("Microsoft Edge.app", "Microsoft Edge", "com.microsoft.edgemac", "UBF8T346G9"),
}

# Windows: (folder below a program folder, executable, certificate subject).
_WINDOWS_APPS = {
    BRAVE: (
        r"BraveSoftware\Brave-Browser\Application",
        "brave.exe",
        "Brave Software, Inc.",
    ),
    CHROME: (r"Google\Chrome\Application", "chrome.exe", "Google LLC"),
    EDGE: (r"Microsoft\Edge\Application", "msedge.exe", "Microsoft Corporation"),
}

# Linux: the executables the vendors' .deb and .rpm packages install.
_LINUX_PATHS = {
    BRAVE: "/opt/brave.com/brave/brave",
    CHROME: "/opt/google/chrome/chrome",
    EDGE: "/opt/microsoft/msedge/msedge",
}

# Accounts that may write below a Windows program folder: SYSTEM,
# Administrators and TrustedInstaller.
_WINDOWS_TRUSTED_WRITERS = frozenset(
    {
        "S-1-5-18",
        "S-1-5-32-544",
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    }
)

# Reads the signature and the access list of one file. The path comes from
# the environment, never from the script text.
_WINDOWS_CHECK_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$path = $env:FAMILIEKILDER_BROWSER_CHECK
$signature = Get-AuthenticodeSignature -LiteralPath $path
$subject = $null
if ($signature.SignerCertificate) {
    $subject = $signature.SignerCertificate.GetNameInfo(
        [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
}
$writers = @()
$item = Get-Item -LiteralPath $path
while ($item) {
    $acl = Get-Acl -LiteralPath $item.FullName
    foreach ($rule in $acl.GetAccessRules($true, $true,
            [System.Security.Principal.SecurityIdentifier])) {
        $rights = [int]$rule.FileSystemRights
        $inheritOnly = ($rule.PropagationFlags -band
            [System.Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0
        if ($rule.AccessControlType -eq 'Allow' -and -not $inheritOnly -and
                ($rights -band 0x500D0156) -ne 0) {
            $writers += $rule.IdentityReference.Value
        }
    }
    $writers += $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    if ($item -is [System.IO.FileInfo]) { $item = $item.Directory } else { $item = $item.Parent }
    if ($item -and $item.FullName -eq $env:FAMILIEKILDER_BROWSER_STOP) { $item = $null }
}
[pscustomobject]@{
    status = [string]$signature.Status
    subject = $subject
    writers = @($writers | Sort-Object -Unique)
} | ConvertTo-Json -Compress
"""

# Rights that let an account change a file or a folder's contents:
# WriteData/CreateFiles, AppendData/CreateDirectories, WriteExtendedAttributes,
# DeleteSubdirectoriesAndFiles, WriteAttributes, Delete, ChangePermissions and
# TakeOwnership, plus GENERIC_WRITE and GENERIC_ALL. Read, execute and
# synchronize are not in the mask. The check script uses the same value.
_WINDOWS_WRITE_MASK = 0x500D0156


class BrowserBootstrapError(FamiliekilderError):
    """A sanitized isolated-browser bootstrap failure."""


@dataclass(frozen=True)
class BrowserApp:
    """One browser in the catalog for one platform."""

    id: str
    name: str
    executables: tuple[str, ...]
    signing_identifier: str | None = None
    team_id: str | None = None
    publisher: str | None = None


@dataclass(frozen=True)
class Selection:
    app: BrowserApp
    executable: str


class System:
    """The operating system as the catalog sees it. Tests pass a fake one."""

    def __init__(self, platform: str | None = None) -> None:
        self.platform = platform_name(platform)

    def lstat(self, path: str) -> os.stat_result:
        return os.lstat(path)

    def executable(self, path: str) -> bool:
        return os.access(path, os.X_OK)

    def listdir(self, path: str) -> list[str]:
        return os.listdir(path)

    def readlink(self, path: str) -> str:
        return os.readlink(path)

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = 15,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=None if env is None else dict(env),
            timeout=timeout,
            check=False,
            text=True,
        )

    def home(self) -> str:
        return str(Path.home())

    def program_folders(self) -> tuple[str, ...]:
        return _windows_program_folders()

    def system_folder(self) -> str:
        return _windows_system_folder()

    def environ(self) -> Mapping[str, str]:
        return os.environ


def platform_name(platform: str | None = None) -> str:
    value = sys.platform if platform is None else platform
    return LINUX if value.startswith("linux") else value


def catalog(system: System | None = None) -> tuple[BrowserApp, ...]:
    """The browsers a login may start on this platform, in order of preference."""

    system = system or System()
    apps: list[BrowserApp] = []
    for browser in BROWSER_IDS:
        if system.platform == MACOS:
            bundle, binary, identifier, team = _MACOS_APPS[browser]
            relative = f"{bundle}/Contents/MacOS/{binary}"
            executables = (
                f"/Applications/{relative}",
                str(PurePosixPath(system.home()) / "Applications" / relative),
            )
            apps.append(BrowserApp(browser, _NAMES[browser], executables, identifier, team))
        elif system.platform == WINDOWS:
            folder, binary, publisher = _WINDOWS_APPS[browser]
            executables = tuple(
                str(PureWindowsPath(base) / folder / binary) for base in system.program_folders()
            )
            apps.append(BrowserApp(browser, _NAMES[browser], executables, publisher=publisher))
        elif system.platform == LINUX:
            apps.append(BrowserApp(browser, _NAMES[browser], (_LINUX_PATHS[browser],)))
    return tuple(apps)


def browser_name(browser: str) -> str:
    """The name users see, such as «Google Chrome»."""

    if browser not in _NAMES:
        raise BrowserBootstrapError("Ukjent nettleser. Velg brave, chrome eller edge.")
    return _NAMES[browser]


def _present(system: System, path: str) -> bool:
    """A regular, executable file that is not a link. Says nothing about trust."""

    try:
        metadata = system.lstat(path)
    except (OSError, ValueError):
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and not _reparse_point(metadata)
        and system.executable(path)
    )


def _reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0) or 0
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def installed(system: System | None = None) -> tuple[BrowserApp, ...]:
    """The catalog browsers found on disk, without checking signatures.

    For the setup page only; a login always runs `select_browser`.
    """

    system = system or System()
    return tuple(
        app for app in catalog(system) if any(_present(system, path) for path in app.executables)
    )


def select_browser(
    choice: str | None = None,
    explicit: Path | str | None = None,
    *,
    system: System | None = None,
    oauth_callback: bool = False,
) -> Selection:
    """Pick and check the browser a login starts.

    `choice` is a browser ID; without it the first installed browser in the
    catalog is used. `explicit` must be one of the catalog's own paths. The
    picked browser must pass the platform's check; a failed check never falls
    back to the next browser. `oauth_callback` matters only on a login server.
    """

    system = system or System()
    if system.platform not in PLATFORMS:
        raise BrowserBootstrapError("Innlogging i nettleser støttes på macOS, Windows og Linux")
    apps = catalog(system)
    hosted = system.environ().get("FAMILIEKILDER_HOSTED") == "1"
    if hosted:
        # A login server starts only what it did before the catalog: Brave,
        # whose URL policy a Linux server manages, and on macOS also Chrome
        # for an OAuth callback.
        allowed = {BRAVE, CHROME} if system.platform == MACOS and oauth_callback else {BRAVE}
        apps = tuple(app for app in apps if app.id in allowed)
    if choice is not None:
        browser_name(choice)
        apps = tuple(app for app in apps if app.id == choice)
    candidates = [(app, path) for app in apps for path in app.executables]
    if explicit is not None:
        requested = _explicit_path(system, explicit)
        candidates = [
            (app, path) for app, path in candidates if _same_path(system, path, requested)
        ]
        if not candidates:
            raise BrowserBootstrapError("Nettleserstien er ikke en godkjent appsti")
    for app, path in candidates:
        if _present(system, path):
            _verify(system, app, path, whole_package=not hosted)
            return Selection(app, path)
    if choice is not None:
        raise BrowserBootstrapError(
            f"Fant ikke {browser_name(choice)}. Installer den fra leverandøren, "
            "eller velg en annen nettleser."
        )
    raise BrowserBootstrapError(
        "Fant ingen godkjent nettleser. Installer Brave, Google Chrome eller "
        "Microsoft Edge fra leverandøren."
    )


def _explicit_path(system: System, explicit: Path | str) -> str:
    text = os.fspath(explicit)
    if system.platform == WINDOWS:
        return str(PureWindowsPath(text))
    if text == "~" or text.startswith("~/"):
        text = system.home() + text[1:]
    return str(PurePosixPath(text))


def _same_path(system: System, left: str, right: str) -> bool:
    if system.platform == WINDOWS:
        return PureWindowsPath(left) == PureWindowsPath(right)
    return left == right


def _verify(system: System, app: BrowserApp, path: str, *, whole_package: bool = True) -> None:
    if system.platform == MACOS:
        _verify_macos(system, app, path)
    elif system.platform == WINDOWS:
        _verify_windows(system, app, path)
    else:
        _verify_linux(system, app, path, whole_package=whole_package)


def _codesign_value(output: str, label: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(label)}=([^\r\n]+)$", output)
    return match.group(1).strip() if match else None


def _verify_macos(system: System, app: BrowserApp, path: str) -> None:
    bundle = str(PurePosixPath(path).parents[2])
    try:
        assessment = system.run(["/usr/sbin/spctl", "--assess", "--type", "execute", bundle])
        details = system.run(["/usr/bin/codesign", "-dv", "--verbose=4", path])
    except (OSError, subprocess.SubprocessError):
        raise BrowserBootstrapError(f"Signaturen til {app.name} kunne ikke kontrolleres") from None
    if (
        assessment.returncode != 0
        or details.returncode != 0
        or _codesign_value(details.stdout, "Identifier") != app.signing_identifier
        or _codesign_value(details.stdout, "TeamIdentifier") != app.team_id
    ):
        raise BrowserBootstrapError(f"{app.name} har ukjent signatur")


def _verify_linux(
    system: System, app: BrowserApp, path: str, *, whole_package: bool = True
) -> None:
    # The package manager checks the repository signature at installation.
    # Accept only the package path, owned by root and not writable by others,
    # with no link anywhere on the way.
    candidate = PurePosixPath(path)
    try:
        for entry in (candidate, *candidate.parents[:-1]):
            info = system.lstat(str(entry))
            if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
                raise OSError
        info = system.lstat(path)
        if not stat.S_ISREG(info.st_mode) or not system.executable(path):
            raise OSError
    except OSError:
        raise BrowserBootstrapError(
            f"{app.name} er ikke installert som en administrert pakke"
        ) from None
    if whole_package:
        _verify_linux_package(system, app, str(candidate.parent))


# A package folder holds a few hundred files; a much larger tree is not one.
_MAX_PACKAGE_ENTRIES = 20000


def _verify_linux_package(system: System, app: BrowserApp, folder: str) -> None:
    """Everything in the package folder belongs to root and only root can change it.

    The browser loads libraries and data files from its own folder, so one
    file another user can replace is as bad as a replaceable executable. A
    link may point only to something inside the folder, which is checked too.
    """

    root = PurePosixPath(folder)
    pending = [root]
    seen = 0
    try:
        while pending:
            current = pending.pop()
            for name in sorted(system.listdir(str(current))):
                seen += 1
                if seen > _MAX_PACKAGE_ENTRIES:
                    raise OSError
                entry = current / name
                info = system.lstat(str(entry))
                if info.st_uid != 0:
                    raise OSError
                if stat.S_ISLNK(info.st_mode):
                    target = PurePosixPath(
                        posixpath.normpath(posixpath.join(str(current), system.readlink(str(entry))))
                    )
                    if target != root and root not in target.parents:
                        raise OSError
                    continue
                if info.st_mode & 0o022:
                    raise OSError
                if stat.S_ISDIR(info.st_mode):
                    pending.append(entry)
    except OSError:
        raise BrowserBootstrapError(
            f"Filene til {app.name} i {folder} kan endres av andre enn administrator, "
            "eller de lenker ut av mappen. Installer pakken på nytt fra leverandøren."
        ) from None


def _verify_windows(system: System, app: BrowserApp, path: str) -> None:
    target = PureWindowsPath(path)
    base = next(
        (
            PureWindowsPath(folder)
            for folder in system.program_folders()
            if PureWindowsPath(folder) in target.parents
        ),
        None,
    )
    if base is None:
        raise BrowserBootstrapError(f"{app.name} ligger ikke i en programmappe")
    try:
        for entry in (target, *target.parents):
            info = system.lstat(str(entry))
            if stat.S_ISLNK(info.st_mode) or _reparse_point(info):
                raise OSError
            if entry == base:
                break
    except OSError:
        raise BrowserBootstrapError(
            f"{app.name} ligger bak en lenke eller kunne ikke kontrolleres"
        ) from None
    powershell = str(
        PureWindowsPath(system.system_folder()) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    environment = {
        key: value
        for key, value in system.environ().items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP"}
    }
    environment["FAMILIEKILDER_BROWSER_CHECK"] = path
    environment["FAMILIEKILDER_BROWSER_STOP"] = str(base.parent)
    try:
        result = system.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _WINDOWS_CHECK_SCRIPT,
            ],
            env=environment,
            timeout=60,
        )
        report: Any = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        report = None
    if not isinstance(report, dict):
        raise BrowserBootstrapError(f"Signaturen til {app.name} kunne ikke kontrolleres")
    writers = report.get("writers")
    if isinstance(writers, str):
        writers = [writers]
    if (
        report.get("status") != "Valid"
        or report.get("subject") != app.publisher
        or not isinstance(writers, list)
    ):
        raise BrowserBootstrapError(f"{app.name} har ukjent signatur")
    if any(writer not in _WINDOWS_TRUSTED_WRITERS for writer in writers):
        raise BrowserBootstrapError(
            f"{app.name} ligger i en mappe som andre enn administratorer kan endre"
        )


_CHOICE_FILE = "login-browser.json"


def saved_browser(root: Path | str | None) -> str | None:
    """The browser the user chose for logins, or None for the first installed one."""

    from .core import project_paths
    from .private_state import read_private_json

    path = project_paths(root).private / _CHOICE_FILE
    if not path.exists() and not path.is_symlink():
        return None
    try:
        value = read_private_json(path).get("browser")
    except FamiliekilderError:
        return None
    return value if value in BROWSER_IDS else None


def save_browser(root: Path | str | None, browser: str | None) -> None:
    """Remember the browser for later logins; None goes back to the first installed."""

    from .core import project_paths
    from .private_state import atomic_write_private_json, remove_private_file

    path = project_paths(root).private / _CHOICE_FILE
    if browser is None:
        remove_private_file(path)
        return
    browser_name(browser)
    project_paths(root).root.mkdir(parents=True, exist_ok=True)
    atomic_write_private_json(path, {"browser": browser})


def _windows_program_folders() -> tuple[str, ...]:
    """Program Files and Program Files (x86) from Windows, not from variables."""

    if os.name != "nt":
        return ()
    import ctypes
    import uuid
    from ctypes import wintypes

    class _Guid(ctypes.Structure):
        _fields_ = [
            ("data1", wintypes.DWORD),
            ("data2", wintypes.WORD),
            ("data3", wintypes.WORD),
            ("data4", ctypes.c_ubyte * 8),
        ]

    def known_folder(identifier: str) -> str | None:
        raw = uuid.UUID(identifier).bytes_le
        guid = _Guid.from_buffer_copy(raw)
        pointer = ctypes.c_wchar_p()
        shell = ctypes.windll.shell32
        result = shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(pointer))
        try:
            return pointer.value if result == 0 else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(pointer)

    folders = []
    # FOLDERID_ProgramFiles and FOLDERID_ProgramFilesX86.
    for identifier in (
        "905e63b6-c1bf-494e-b29c-65b732d3d21a",
        "7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e",
    ):
        folder = known_folder(identifier)
        if folder and folder not in folders:
            folders.append(folder)
    return tuple(folders)


def _windows_system_folder() -> str:
    if os.name != "nt":
        return r"C:\Windows\System32"
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise BrowserBootstrapError("Windows-systemmappen kunne ikke finnes")
    return buffer.value


__all__ = [
    "BRAVE",
    "BROWSER_IDS",
    "BrowserApp",
    "BrowserBootstrapError",
    "CHROME",
    "EDGE",
    "Selection",
    "System",
    "browser_name",
    "catalog",
    "installed",
    "save_browser",
    "saved_browser",
    "select_browser",
]
