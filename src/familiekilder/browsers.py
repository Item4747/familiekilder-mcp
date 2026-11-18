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

Brave is first, as before this catalog existed, then Chrome, then Edge. When
the user has not chosen a browser, a browser that is refused (it fails its
check, or it is only installed in a way the catalog does not accept) is
skipped and the next one is tried; every browser that starts has passed the
same check. The refusals travel with the result, each with a reason and what
the user can do, and `survey` lists every browser with its status for
``familiekilder-mcp doctor`` and the setup page. A browser the user chose is
the only one tried; when it cannot be used, the error says precisely why.

Some installations are recognized only to explain why they are refused, and
are never started: on Windows a browser installed for one user under
``%LOCALAPPDATA%``; on Linux Snap, Flatpak, a browser outside the vendor
package's path, and the distribution's Chromium.

A login server (``FAMILIEKILDER_HOSTED=1``) keeps exactly what it did before
the catalog: Brave only, and on macOS also Chrome for an OAuth callback. On
Linux it also keeps the old package check (the executable and the folders
above it); a Linux desktop checks the whole package folder. It never moves on
to another browser after a refusal.
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
# The vendors' package names, for advice only.
_LINUX_PACKAGES = {
    BRAVE: "brave-browser",
    CHROME: "google-chrome-stable",
    EDGE: "microsoft-edge-stable",
}

# Installations that are recognized only to explain why they are refused.
# None of these paths is ever started or checked as a browser.
CHROMIUM = "chromium"
_PER_USER = "per-user"
_SNAP = "snap"
_FLATPAK = "flatpak"
_ELSEWHERE = "elsewhere"
_DISTRIBUTION = "distribution"
_FLATPAK_SYSTEM = "/var/lib/flatpak/exports/bin/"
_FLATPAK_USER = "~/.local/share/flatpak/exports/bin/"
_LINUX_OTHER_INSTALLS = {
    BRAVE: (
        (_SNAP, "/snap/bin/brave"),
        (_FLATPAK, _FLATPAK_SYSTEM + "com.brave.Browser"),
        (_FLATPAK, _FLATPAK_USER + "com.brave.Browser"),
        (_ELSEWHERE, "/usr/bin/brave-browser"),
        (_ELSEWHERE, "/usr/bin/brave"),
        (_ELSEWHERE, "/usr/local/bin/brave-browser"),
        (_ELSEWHERE, "/usr/local/bin/brave"),
    ),
    CHROME: (
        (_FLATPAK, _FLATPAK_SYSTEM + "com.google.Chrome"),
        (_FLATPAK, _FLATPAK_USER + "com.google.Chrome"),
        (_ELSEWHERE, "/usr/bin/google-chrome-stable"),
        (_ELSEWHERE, "/usr/bin/google-chrome"),
        (_ELSEWHERE, "/usr/local/bin/google-chrome"),
    ),
    EDGE: (
        (_FLATPAK, _FLATPAK_SYSTEM + "com.microsoft.Edge"),
        (_FLATPAK, _FLATPAK_USER + "com.microsoft.Edge"),
        (_ELSEWHERE, "/usr/bin/microsoft-edge-stable"),
        (_ELSEWHERE, "/usr/bin/microsoft-edge"),
    ),
    CHROMIUM: (
        (_SNAP, "/snap/bin/chromium"),
        (_FLATPAK, _FLATPAK_SYSTEM + "org.chromium.Chromium"),
        (_FLATPAK, _FLATPAK_USER + "org.chromium.Chromium"),
        (_DISTRIBUTION, "/usr/bin/chromium"),
        (_DISTRIBUTION, "/usr/bin/chromium-browser"),
    ),
}

# How the user picks another browser; shown in advice.
_CHOOSE_COMMAND = "familiekilder-mcp browser"

# Statuses in `survey`.
SELECTED = "selected"
APPROVED = "approved"
CANDIDATE = "candidate"
PENDING = "pending"
REJECTED = "rejected"
MISSING = "missing"

# Accounts that may write below a Windows program folder: SYSTEM,
# Administrators and TrustedInstaller.
_WINDOWS_TRUSTED_WRITERS = frozenset(
    {
        "S-1-5-18",
        "S-1-5-32-544",
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    }
)

# Reads the access list of one file. The path comes from
# the environment, never from the script text.
_WINDOWS_ACCESS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$path = $env:FAMILIEKILDER_BROWSER_CHECK
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
$report = @{ writers = @($writers | Sort-Object -Unique) }
"""

_WINDOWS_SIGNATURE_SCRIPT = r"""
$signature = Get-AuthenticodeSignature -LiteralPath $path
$report.status = [string]$signature.Status
if ($signature.SignerCertificate) {
    $report.subject = $signature.SignerCertificate.GetNameInfo(
        [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
}
"""

# Rights that let an account change a file or a folder's contents:
# WriteData/CreateFiles, AppendData/CreateDirectories, WriteExtendedAttributes,
# DeleteSubdirectoriesAndFiles, WriteAttributes, Delete, ChangePermissions and
# TakeOwnership, plus GENERIC_WRITE and GENERIC_ALL. Read, execute and
# synchronize are not in the mask. The check script uses the same value.
_WINDOWS_WRITE_MASK = 0x500D0156


class BrowserBootstrapError(FamiliekilderError):
    """A sanitized isolated-browser bootstrap failure."""


class _Refused(BrowserBootstrapError):
    """A failed browser check; ``fix`` says what makes the browser usable.

    The message is the same as before the advice existed, so a login server
    reports exactly what it did.
    """

    def __init__(self, message: str, fix: str) -> None:
        super().__init__(message)
        self.fix = fix


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
class Rejection:
    """A browser that was found but may not be started, and why.

    ``browser`` is a catalog ID, or ``chromium`` for the distribution's
    Chromium, which is never in the catalog. ``kind`` is ``check`` for a
    browser that failed its check, or the kind of installation the catalog
    does not accept (``per-user``, ``snap``, ``flatpak``, ``elsewhere``,
    ``distribution``). ``reason`` is a sentence; ``fix`` is a clause without
    the final full stop, so `advice` can add another browser to choose.
    """

    browser: str
    name: str
    path: str
    kind: str
    reason: str
    fix: str

    def advice(self, alternative: BrowserApp | None = None) -> str:
        if alternative is None or alternative.id == self.browser:
            return f"{self.fix}."
        return (
            f"{self.fix}, eller velg {alternative.name} med "
            f"«{_CHOOSE_COMMAND} {alternative.id}»."
        )

    def explain(self, alternative: BrowserApp | None = None) -> str:
        return f"{self.reason} {self.advice(alternative)}"


@dataclass(frozen=True)
class Selection:
    app: BrowserApp
    executable: str
    # Installations skipped before this one.
    rejected: tuple[Rejection, ...] = ()

    def notice(self) -> str | None:
        """One line for the terminal about skipped browsers, or None."""

        if not self.rejected:
            return None
        skipped = " ".join(
            f"Hopper over {'én installasjon av ' if item.browser == self.app.id else ''}"
            f"{item.name}: {item.reason}"
            for item in self.rejected
        )
        return f"{skipped} Innloggingen bruker {self.app.name}. Se «familiekilder-mcp doctor»."


@dataclass(frozen=True)
class BrowserStatus:
    """One browser in `survey`: its status, where it is, and why it is refused."""

    browser: str
    name: str
    status: str
    path: str | None = None
    reason: str | None = None
    advice: str | None = None


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

    def local_app_data(self) -> str | None:
        """Windows: the user's own program folder, used only to explain refusals."""

        return _windows_local_app_data()


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

    `choice` is a browser ID; without it the first browser in the catalog
    that passes the platform's check is used, and the browsers refused before
    it are in `Selection.rejected`. `explicit` must be one of the catalog's
    own paths. A chosen browser ID tries only that browser's catalog paths.
    A named path and a login server retain their first-candidate behavior.
    `oauth_callback` matters only on a login server.
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
    if not hosted and explicit is None:
        return _first_approved(system, apps, choice=choice)
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
            if hosted:
                _verify(system, app, path, whole_package=False)
            else:
                _verify_with_advice(system, app, path)
            return Selection(app, path)
    if choice is not None:
        refused = None if hosted else _unsupported(system, choice)
        if refused is not None:
            raise BrowserBootstrapError(refused.explain(_alternative(system, choice)))
        raise BrowserBootstrapError(
            f"Fant ikke {browser_name(choice)}. Installer den fra leverandøren, "
            "eller velg en annen nettleser."
        )
    raise BrowserBootstrapError(_NONE_FOUND)


_NONE_FOUND = (
    "Fant ingen godkjent nettleser. Installer Brave, Google Chrome eller "
    "Microsoft Edge fra leverandøren."
)


def _first_installation(
    system: System, app: BrowserApp, *, offline: bool = False
) -> tuple[str | None, list[Rejection]]:
    """Walk a browser's catalog paths in the same order in the report and at login."""

    rejected = []
    for path in app.executables:
        if not _present(system, path):
            continue
        try:
            _verify(system, app, path, offline=offline)
        except BrowserBootstrapError as error:
            rejected.append(_check_rejection(app, path, error))
            continue
        return path, rejected
    if not rejected and (refused := _unsupported(system, app.id)) is not None:
        rejected.append(refused)
    return None, rejected


def _first_approved(
    system: System, apps: Sequence[BrowserApp], *, choice: str | None
) -> Selection:
    """The first browser that passes its check; the refused ones come along."""

    rejected: list[Rejection] = []
    for app in apps:
        path, refusals = _first_installation(system, app)
        rejected.extend(refusals)
        if path is not None:
            return Selection(app, path, tuple(rejected))
    if choice is not None:
        if rejected:
            raise BrowserBootstrapError(rejected[0].explain(_alternative(system, choice)))
        raise BrowserBootstrapError(
            f"Fant ikke {browser_name(choice)}. Installer den fra leverandøren, "
            "eller velg en annen nettleser."
        )
    chromium = _unsupported(system, CHROMIUM)
    if chromium is not None:
        rejected.append(chromium)
    if not rejected:
        raise BrowserBootstrapError(_NONE_FOUND)
    lines = "\n".join(f"- {item.explain()}" for item in rejected)
    raise BrowserBootstrapError(f"Fant ingen godkjent nettleser.\n{lines}")


def _verify_with_advice(system: System, app: BrowserApp, path: str) -> None:
    """The desktop check for a browser the user chose; the error says what to do."""

    try:
        _verify(system, app, path)
    except BrowserBootstrapError as error:
        refused = _check_rejection(app, path, error)
        raise BrowserBootstrapError(refused.explain(_alternative(system, app.id))) from None


def survey(
    choice: str | None = None, *, system: System | None = None, offline: bool = False
) -> tuple[BrowserStatus, ...]:
    """Every browser in the catalog and every recognized refused installation.

    Uses the desktop login order for the user's `choice`, or for all
    browsers in automatic mode. Never starts a browser. In offline mode on Windows
    and Mac, the first locally suitable installation is a candidate awaiting
    the platform's trust assessment during login.
    """

    system = system or System()
    if choice is not None:
        browser_name(choice)
    if system.platform not in PLATFORMS:
        return ()
    found: list[tuple[BrowserApp | None, str | None, Rejection | None]] = []
    for app in catalog(system):
        path, refusals = _first_installation(system, app, offline=offline)
        found.append((app, path, refusals[0] if path is None and refusals else None))
    chromium = _unsupported(system, CHROMIUM)
    if chromium is not None:
        found.append((None, None, chromium))
    approved_apps = [app for app, path, _ in found if app is not None and path is not None]
    selected = next(
        (app.id for app in approved_apps if choice in (None, app.id)),
        None,
    )
    pending = offline and system.platform in (MACOS, WINDOWS)
    rows = []
    for app, path, refusal in found:
        if refusal is not None:
            alternative = None
            if choice is not None:
                alternative = next(
                    (other for other in approved_apps if other.id != refusal.browser), None
                )
            rows.append(
                BrowserStatus(
                    refusal.browser,
                    refusal.name,
                    REJECTED,
                    refusal.path,
                    refusal.reason,
                    refusal.advice(alternative),
                )
            )
        elif path is None:
            assert app is not None
            rows.append(BrowserStatus(app.id, app.name, MISSING))
        else:
            assert app is not None
            if pending:
                status = CANDIDATE if app.id == selected else PENDING
                reason = "Signaturen er uavklart til innloggingskontrollen."
            else:
                status = SELECTED if app.id == selected else APPROVED
                reason = None
            rows.append(BrowserStatus(app.id, app.name, status, path, reason))
    return tuple(rows)


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _check_rejection(app: BrowserApp, path: str, error: BrowserBootstrapError) -> Rejection:
    fix = getattr(error, "fix", None) or f"Installer {app.name} på nytt fra leverandøren"
    return Rejection(app.id, app.name, path, "check", _sentence(str(error)), fix)


def _alternative(system: System, browser: str) -> BrowserApp | None:
    """Another catalog browser to suggest: an installed one, else Edge on Windows.

    Only looks at what is on disk, without running any check.
    """

    apps = [app for app in catalog(system) if app.id != browser]
    for app in apps:
        if any(_present(system, path) for path in app.executables):
            return app
    if system.platform == WINDOWS:
        return next((app for app in apps if app.id == EDGE), None)
    return None


def _other_installs(system: System, browser: str) -> list[tuple[str, str]]:
    """Recognized installations outside the catalog, as (kind, path)."""

    if system.platform == WINDOWS:
        base = system.local_app_data()
        if not base or browser not in _WINDOWS_APPS:
            return []
        folder, binary, _publisher = _WINDOWS_APPS[browser]
        candidates = [(_PER_USER, str(PureWindowsPath(base) / folder / binary))]
    elif system.platform == LINUX:
        candidates = [
            (kind, system.home() + path[1:] if path.startswith("~/") else path)
            for kind, path in _LINUX_OTHER_INSTALLS.get(browser, ())
        ]
    else:
        return []
    found = []
    for kind, path in candidates:
        try:
            system.lstat(path)
        except (OSError, ValueError):
            continue
        found.append((kind, path))
    return found


def _unsupported(system: System, browser: str) -> Rejection | None:
    """The first recognized installation of `browser` the catalog refuses."""

    installs = _other_installs(system, browser)
    if not installs:
        return None
    kind, path = installs[0]
    name = "Chromium" if browser == CHROMIUM else browser_name(browser)
    package = (
        f"leverandørens .deb- eller .rpm-pakke ({_LINUX_PACKAGES[browser]}), "
        f"som legger den i {PurePosixPath(_LINUX_PATHS[browser]).parent}"
        if browser in _LINUX_PACKAGES
        else ""
    )
    if kind == _PER_USER:
        reason = f"{name} er bare installert for din bruker, under AppData."
        fix = "Installer den for alle brukere (kjør installasjonsprogrammet som administrator)"
    elif browser == CHROMIUM:
        reason = {
            _SNAP: "Chromium er installert som Snap, og Chromium støttes ikke.",
            _FLATPAK: "Chromium er installert som Flatpak, og Chromium støttes ikke.",
        }.get(kind, "Chromium fra distribusjonen støttes ikke.")
        fix = (
            "Installer Brave, Google Chrome eller Microsoft Edge fra leverandørens "
            ".deb- eller .rpm-pakke"
        )
    elif kind == _SNAP:
        reason = f"{name} er installert som Snap, som ikke kan kontrolleres på samme måte."
        fix = f"Installer {name} fra {package}"
    elif kind == _FLATPAK:
        reason = f"{name} er installert som Flatpak, som ikke kan kontrolleres på samme måte."
        fix = f"Installer {name} fra {package}"
    else:
        reason = f"{name} ligger i {path}, ikke i leverandørens pakkesti {_LINUX_PATHS[browser]}."
        fix = f"Installer {name} fra {package}"
    return Rejection(browser, name, path, kind, reason, fix)


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


def _verify(
    system: System,
    app: BrowserApp,
    path: str,
    *,
    whole_package: bool = True,
    offline: bool = False,
) -> None:
    if system.platform == MACOS:
        _verify_macos(system, app, path, offline=offline)
    elif system.platform == WINDOWS:
        _verify_windows(system, app, path, offline=offline)
    else:
        _verify_linux(system, app, path, whole_package=whole_package)


def _codesign_value(output: str, label: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(label)}=([^\r\n]+)$", output)
    return match.group(1).strip() if match else None


# Like the PowerShell check on Windows: a large app on a slow disk can need it.
_MACOS_CHECK_SECONDS = 60


def _verify_macos(system: System, app: BrowserApp, path: str, *, offline: bool) -> None:
    bundle = str(PurePosixPath(path).parents[2])
    try:
        # spctl reads the whole bundle; for Chrome that took up to 15 seconds.
        assessment = None
        if not offline:
            assessment = system.run(
                ["/usr/sbin/spctl", "--assess", "--type", "execute", bundle],
                timeout=_MACOS_CHECK_SECONDS,
            )
        # Display embedded signature metadata, without assessing its trust.
        details = system.run(
            ["/usr/bin/codesign", "-dv", "--verbose=4", path], timeout=_MACOS_CHECK_SECONDS
        )
    except (OSError, subprocess.SubprocessError):
        tools = "codesign" if offline else "spctl og codesign"
        raise _Refused(
            f"Signaturen til {app.name} kunne ikke kontrolleres",
            f"Prøv igjen, og kontroller at {tools} kan kjøres",
        ) from None
    if (
        (assessment is not None and assessment.returncode != 0)
        or details.returncode != 0
        or _codesign_value(details.stdout, "Identifier") != app.signing_identifier
        or _codesign_value(details.stdout, "TeamIdentifier") != app.team_id
    ):
        raise _Refused(
            f"{app.name} har ukjent signatur",
            f"Installer {app.name} på nytt fra leverandørens nettside, i Programmer-mappen",
        )


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
        raise _Refused(
            f"{app.name} er ikke installert som en administrert pakke",
            f"Installer {app.name} fra leverandørens .deb- eller .rpm-pakke "
            f"({_LINUX_PACKAGES.get(app.id, app.id)}), som legger den i {candidate.parent}",
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
        package = _LINUX_PACKAGES.get(app.id, app.id)
        raise _Refused(
            f"Filene til {app.name} i {folder} kan endres av andre enn administrator, "
            "eller de lenker ut av mappen. Installer pakken på nytt fra leverandøren.",
            f"Kjør for eksempel «sudo apt install --reinstall {package}» eller "
            f"«sudo dnf reinstall {package}»",
        ) from None


def _verify_windows(system: System, app: BrowserApp, path: str, *, offline: bool) -> None:
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
        raise _Refused(
            f"{app.name} ligger ikke i en programmappe",
            "Installer den for alle brukere (kjør installasjonsprogrammet som administrator)",
        )
    try:
        for entry in (target, *target.parents):
            info = system.lstat(str(entry))
            if stat.S_ISLNK(info.st_mode) or _reparse_point(info):
                raise OSError
            if entry == base:
                break
    except OSError:
        raise _Refused(
            f"{app.name} ligger bak en lenke eller kunne ikke kontrolleres",
            f"Installer {app.name} på nytt fra leverandøren, uten lenker eller "
            "koblingspunkter i programmappen",
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
    script = _WINDOWS_ACCESS_SCRIPT
    if not offline:
        script += _WINDOWS_SIGNATURE_SCRIPT
    script += "$report | ConvertTo-Json -Compress"
    try:
        result = system.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env=environment,
            timeout=60,
        )
        report: Any = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        report = None
    if not isinstance(report, dict):
        raise _Refused(
            f"Tilgangen til {app.name} kunne ikke kontrolleres" if offline
            else f"Signaturen til {app.name} kunne ikke kontrolleres",
            "Prøv igjen, og kontroller at Windows PowerShell kan kjøres på maskinen",
        )
    writers = report.get("writers")
    if isinstance(writers, str):
        writers = [writers]
    if not offline and (report.get("status") != "Valid" or report.get("subject") != app.publisher):
        raise _Refused(
            f"{app.name} har ukjent signatur",
            f"Installer {app.name} på nytt fra leverandørens nettside",
        )
    if not isinstance(writers, list):
        raise _Refused(
            f"Tilgangen til {app.name} kunne ikke kontrolleres",
            "Prøv igjen, og kontroller at Windows PowerShell kan kjøres på maskinen",
        )
    if any(writer not in _WINDOWS_TRUSTED_WRITERS for writer in writers):
        raise _Refused(
            f"{app.name} ligger i en mappe som andre enn administratorer kan endre",
            f"Installer {app.name} på nytt for alle brukere, eller be en administrator "
            "fjerne skrivetilgangen andre har til mappen",
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


def _windows_known_folder(identifier: str) -> str | None:
    """A Windows known folder by its FOLDERID, from Windows, not from variables."""

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

    raw = uuid.UUID(identifier).bytes_le
    guid = _Guid.from_buffer_copy(raw)
    pointer = ctypes.c_wchar_p()
    shell = ctypes.windll.shell32
    result = shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(pointer))
    try:
        return pointer.value if result == 0 else None
    finally:
        ctypes.windll.ole32.CoTaskMemFree(pointer)


def _windows_program_folders() -> tuple[str, ...]:
    """Program Files and Program Files (x86) from Windows, not from variables."""

    if os.name != "nt":
        return ()
    folders = []
    # FOLDERID_ProgramFiles and FOLDERID_ProgramFilesX86.
    for identifier in (
        "905e63b6-c1bf-494e-b29c-65b732d3d21a",
        "7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e",
    ):
        folder = _windows_known_folder(identifier)
        if folder and folder not in folders:
            folders.append(folder)
    return tuple(folders)


def _windows_local_app_data() -> str | None:
    """FOLDERID_LocalAppData, where a browser installed for one user lives."""

    if os.name != "nt":
        return None
    try:
        return _windows_known_folder("f1b32785-6fba-4fcf-9d55-7b8e7f157091")
    except OSError:
        return None


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
    "APPROVED",
    "BRAVE",
    "BROWSER_IDS",
    "BrowserApp",
    "BrowserBootstrapError",
    "BrowserStatus",
    "CHROME",
    "CHROMIUM",
    "EDGE",
    "MISSING",
    "REJECTED",
    "Rejection",
    "SELECTED",
    "Selection",
    "System",
    "browser_name",
    "catalog",
    "installed",
    "save_browser",
    "saved_browser",
    "select_browser",
    "survey",
]
