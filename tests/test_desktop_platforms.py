"""Browser choice, browser checks, password storage and machine binding per platform.

Every test runs on every platform with the platform passed in. Two parts pin
the behaviour from before the browser catalog existed, with frozen copies of
the old code: macOS with Brave, Linux with Brave, the password store's names
and the macOS machine binding. Stored data (the Keychain service, the account
names and the owner files) must come out exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from familiekilder import auth_lifecycle as life
from familiekilder import browser_bootstrap
from familiekilder import browsers
from familiekilder import desktop_platform
from familiekilder import login_passwords
from familiekilder.browsers import BrowserBootstrapError, select_browser
from familiekilder.core import FamiliekilderError, project_paths

HOME = "/hjem/oppdiktet"
MAC_BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
MAC_HOME_BRAVE = f"{HOME}/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MAC_EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
PROGRAM_FILES = r"C:\Program Files"
PROGRAM_FILES_X86 = r"C:\Program Files (x86)"
WIN_CHROME = PROGRAM_FILES + r"\Google\Chrome\Application\chrome.exe"
WIN_EDGE = PROGRAM_FILES_X86 + r"\Microsoft\Edge\Application\msedge.exe"
WIN_BRAVE = PROGRAM_FILES + r"\BraveSoftware\Brave-Browser\Application\brave.exe"
# What `codesign -dv` prints for each app bundle, in short.
SIGNATURES = {
    "Brave Browser.app": "Identifier=com.brave.Browser\nTeamIdentifier=KL8N8XSYF4\n",
    "Google Chrome.app": "Identifier=com.google.Chrome\nTeamIdentifier=EQHXZ8M8AV\n",
    "Microsoft Edge.app": "Identifier=com.microsoft.edgemac\nTeamIdentifier=UBF8T346G9\n",
}


def _stat(mode: int, uid: int = 501, attributes: int = 0) -> os.stat_result:
    result = os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))
    if attributes:
        # st_file_attributes exists only on Windows; a namespace stands in.
        return types.SimpleNamespace(  # type: ignore[return-value]
            st_mode=mode, st_uid=uid, st_file_attributes=attributes
        )
    return result


FILE = stat.S_IFREG | 0o755
DIRECTORY = stat.S_IFDIR | 0o755
LINK = stat.S_IFLNK | 0o777


class FakeSystem(browsers.System):
    """Files, signatures and program folders made up for one test."""

    def __init__(self, platform: str, files: dict[str, os.stat_result] | None = None):
        self.platform = platform
        self.files = dict(files or {})
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []
        self.signatures = dict(SIGNATURES)
        self.spctl = 0
        self.windows_report: dict | None = None
        self.env: dict[str, str] = {}
        self.links: dict[str, str] = {}

    def lstat(self, path: str) -> os.stat_result:
        if path in self.files:
            return self.files[path]
        if self.platform != browsers.WINDOWS:
            # A folder on the way to a known file exists, owned by root.
            if any(known.startswith(path.rstrip("/") + "/") for known in self.files):
                return _stat(DIRECTORY, uid=0)
        else:
            lowered = path.lower().rstrip("\\") + "\\"
            if any(known.lower().startswith(lowered) for known in self.files):
                return _stat(DIRECTORY)
        raise FileNotFoundError(path)

    def executable(self, path: str) -> bool:
        return path in self.files and bool(self.files[path].st_mode & 0o111)

    def listdir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted({known[len(prefix):].split("/")[0] for known in self.files if known.startswith(prefix)})

    def readlink(self, path: str) -> str:
        return self.links[path]

    def run(self, argv, *, env=None, timeout=15):
        argv = list(argv)
        self.calls.append((argv, None if env is None else dict(env)))
        if argv[0] == "/usr/sbin/spctl":
            return subprocess.CompletedProcess(argv, self.spctl, "", "")
        if argv[0] == "/usr/bin/codesign":
            bundle = PurePosixPath(argv[-1]).parents[2].name
            return subprocess.CompletedProcess(argv, 0, self.signatures.get(bundle, ""), "")
        if argv[0].endswith("powershell.exe"):
            report = self.windows_report
            return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")
        raise AssertionError(argv)

    def home(self) -> str:
        return HOME

    def program_folders(self) -> tuple[str, ...]:
        return (PROGRAM_FILES, PROGRAM_FILES_X86)

    def system_folder(self) -> str:
        return r"C:\Windows\System32"

    def environ(self):
        return self.env


def _old_macos_select(system: FakeSystem, explicit=None, *, allow_chrome=False) -> str:
    """Frozen copy of the macOS part of the old `_official_brave_path`.

    Path() is replaced by strings and the system calls go through the fake;
    the order of checks, the paths, the commands and the identities are as
    they were.
    """

    brave_paths = (MAC_BRAVE, MAC_HOME_BRAVE)
    chrome_paths = (
        MAC_CHROME,
        f"{HOME}/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    candidates = brave_paths + chrome_paths if allow_chrome else brave_paths
    if explicit is not None:
        requested = (
            str(explicit).replace("~", HOME, 1) if str(explicit).startswith("~") else str(explicit)
        )
        if requested not in candidates:
            raise BrowserBootstrapError("Brave-stien er ikke en godkjent appsti")
        candidates = (requested,)
    selected = None
    for candidate in candidates:
        try:
            metadata = system.lstat(candidate)
        except OSError:
            continue
        if (
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and system.executable(candidate)
        ):
            selected = candidate
            break
    if selected is None:
        raise BrowserBootstrapError("Fant ikke den offisielle Brave-installasjonen")
    assessment = system.run(
        [
            "/usr/sbin/spctl",
            "--assess",
            "--type",
            "execute",
            str(PurePosixPath(selected).parents[2]),
        ]
    )
    details = system.run(["/usr/bin/codesign", "-dv", "--verbose=4", selected])
    expected = (
        ("com.google.Chrome", "EQHXZ8M8AV")
        if selected in chrome_paths
        else ("com.brave.Browser", "KL8N8XSYF4")
    )
    if (
        assessment.returncode != 0
        or details.returncode != 0
        or browsers._codesign_value(details.stdout, "Identifier") != expected[0]
        or browsers._codesign_value(details.stdout, "TeamIdentifier") != expected[1]
    ):
        raise BrowserBootstrapError("Brave-installasjonen har ukjent signatur")
    return selected


def _old_linux_select(system: FakeSystem, explicit=None) -> str:
    """Frozen copy of the Linux part of the old `_official_brave_path`."""

    candidate = PurePosixPath("/opt/brave.com/brave/brave")
    if explicit is not None and PurePosixPath(explicit) != candidate:
        raise BrowserBootstrapError("Brave-stien er ikke en godkjent appsti")
    try:
        for entry in (candidate, *candidate.parents[:-1]):
            info = system.lstat(str(entry))
            if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
                raise OSError
        if not stat.S_ISREG(system.lstat(str(candidate)).st_mode) or not system.executable(
            str(candidate)
        ):
            raise OSError
    except OSError:
        raise BrowserBootstrapError("Fant ikke en administrert Brave-installasjon") from None
    return str(candidate)


def _outcome(function, *args, **kwargs):
    try:
        return ("ok", function(*args, **kwargs))
    except BrowserBootstrapError:
        return ("error", None)


class MacosBraveUnchangedTests(unittest.TestCase):
    """macOS with Brave installed behaves as before the catalog."""

    def scenarios(self):
        yield "brave", {MAC_BRAVE: _stat(FILE)}, {}
        yield "brave in home", {MAC_HOME_BRAVE: _stat(FILE)}, {}
        yield "brave and chrome", {MAC_BRAVE: _stat(FILE), MAC_CHROME: _stat(FILE)}, {}
        yield (
            "all three",
            {MAC_BRAVE: _stat(FILE), MAC_CHROME: _stat(FILE), MAC_EDGE: _stat(FILE)},
            {},
        )
        yield "unsigned", {MAC_BRAVE: _stat(FILE)}, {"spctl": 3}
        yield (
            "other team",
            {MAC_BRAVE: _stat(FILE)},
            {"brave": "Identifier=com.brave.Browser\nTeamIdentifier=XXXXXXXXXX\n"},
        )
        yield (
            "other identifier",
            {MAC_BRAVE: _stat(FILE)},
            {"brave": "Identifier=com.example.Fake\nTeamIdentifier=KL8N8XSYF4\n"},
        )
        yield (
            "not executable",
            {MAC_BRAVE: _stat(stat.S_IFREG | 0o644), MAC_HOME_BRAVE: _stat(FILE)},
            {},
        )
        yield "link then real", {MAC_BRAVE: _stat(LINK), MAC_HOME_BRAVE: _stat(FILE)}, {}

    def make(self, files, options):
        system = FakeSystem(browsers.MACOS, files)
        system.spctl = options.get("spctl", 0)
        if "brave" in options:
            system.signatures["Brave Browser.app"] = options["brave"]
        return system

    def test_same_choice_commands_and_result_as_the_old_code(self):
        explicit_values = (
            None,
            MAC_BRAVE,
            MAC_HOME_BRAVE,
            "~/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        )
        for name, files, options in self.scenarios():
            for explicit in explicit_values:
                with self.subTest(name, explicit=explicit):
                    old_system = self.make(files, options)
                    new_system = self.make(files, options)
                    old = _outcome(_old_macos_select, old_system, explicit)
                    new = _outcome(select_browser, None, explicit, system=new_system)
                    if new[0] == "ok":
                        new = ("ok", new[1].executable)
                    self.assertEqual(new, old)
                    self.assertEqual(
                        [argv for argv, _ in new_system.calls],
                        [argv for argv, _ in old_system.calls],
                    )

    def test_exact_signature_commands(self):
        system = FakeSystem(browsers.MACOS, {MAC_BRAVE: _stat(FILE)})
        chosen = select_browser(system=system)
        self.assertEqual(chosen.app.id, "brave")
        self.assertEqual(chosen.executable, MAC_BRAVE)
        self.assertEqual(
            [argv for argv, _ in system.calls],
            [
                [
                    "/usr/sbin/spctl",
                    "--assess",
                    "--type",
                    "execute",
                    "/Applications/Brave Browser.app",
                ],
                ["/usr/bin/codesign", "-dv", "--verbose=4", MAC_BRAVE],
            ],
        )

    def test_signature_commands_may_take_a_minute(self):
        # spctl on a large app such as Chrome took longer than the old 15 seconds.
        timeouts = []

        class Timed(FakeSystem):
            def run(self, argv, *, env=None, timeout=15):
                timeouts.append(timeout)
                return super().run(argv, env=env, timeout=timeout)

        select_browser(system=Timed(browsers.MACOS, {MAC_BRAVE: _stat(FILE)}))
        self.assertEqual(timeouts, [60, 60])

    def test_old_constants_keep_their_values(self):
        home = Path.home()
        self.assertEqual(
            browser_bootstrap._BRAVE_PATHS,
            (
                Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
                home / "Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            ),
        )
        self.assertEqual(
            browser_bootstrap._CHROME_PATHS,
            (
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                home / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ),
        )

    def test_launch_arguments_are_unchanged(self):
        profile = Path("/tmp/oppdiktet/.private/browser-bootstrap/run-x")
        arguments = browser_bootstrap._brave_arguments(
            Path(MAC_BRAVE), profile, "https://spond.com/client"
        )
        self.assertEqual(
            arguments,
            [
                str(Path(MAC_BRAVE)),
                f"--user-data-dir={profile}",
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
                "--disable-features=BraveSync,PasswordImport,PasswordManagerEnabled,"
                "PasswordManagerOnboarding",
                "--new-window",
                "https://spond.com/client",
            ],
        )


class MacosLoginServerUnchangedTests(unittest.TestCase):
    """A login server on a Mac (FAMILIEKILDER_HOSTED=1) starts what it did before.

    Before the catalog, the OAuth callback allowed Brave, then Chrome
    (allow_chrome=True), and the session bootstrap allowed Brave only.
    """

    def scenarios(self):
        yield "brave", {MAC_BRAVE: _stat(FILE)}
        yield "chrome", {MAC_CHROME: _stat(FILE)}
        yield "edge", {MAC_EDGE: _stat(FILE)}
        yield "brave and chrome", {MAC_BRAVE: _stat(FILE), MAC_CHROME: _stat(FILE)}
        yield "chrome and edge", {MAC_CHROME: _stat(FILE), MAC_EDGE: _stat(FILE)}
        yield "brave link, chrome", {MAC_BRAVE: _stat(LINK), MAC_CHROME: _stat(FILE)}

    def test_same_choice_and_commands_as_the_old_code(self):
        explicit_values = (None, MAC_BRAVE, MAC_CHROME, MAC_EDGE)
        for name, files in self.scenarios():
            for oauth_callback in (False, True):
                for explicit in explicit_values:
                    with self.subTest(name, oauth_callback=oauth_callback, explicit=explicit):
                        old_system = FakeSystem(browsers.MACOS, files)
                        new_system = FakeSystem(browsers.MACOS, files)
                        new_system.env = {"FAMILIEKILDER_HOSTED": "1"}
                        old = _outcome(
                            _old_macos_select, old_system, explicit, allow_chrome=oauth_callback
                        )
                        new = _outcome(
                            select_browser,
                            None,
                            explicit,
                            system=new_system,
                            oauth_callback=oauth_callback,
                        )
                        if new[0] == "ok":
                            new = ("ok", new[1].executable)
                        self.assertEqual(new, old)
                        self.assertEqual(
                            [argv for argv, _ in new_system.calls],
                            [argv for argv, _ in old_system.calls],
                        )

    def test_the_callback_asks_for_the_old_rule_and_the_saved_choice_is_ignored(self):
        seen = []

        def launch(profile, url, path, *, browser=None, oauth_callback=False):
            seen.append((browser, oauth_callback))
            raise BrowserBootstrapError("stopp")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browsers.save_browser(root, "edge")
            with (
                patch.dict(os.environ, {"FAMILIEKILDER_HOSTED": "1"}),
                patch.object(browser_bootstrap, "_launch_brave", launch),
                patch("familiekilder.spond_auth.load_client_config"),
            ):
                with self.assertRaises(BrowserBootstrapError):
                    browser_bootstrap.capture_oauth_callback(
                        root,
                        authorization_url="https://login.example.invalid/authorize",
                        expected_redirect_uri="app.example.invalid://callback",
                    )
                with self.assertRaises(BrowserBootstrapError):
                    browser_bootstrap.bootstrap_spond(root, wait_for_user=lambda prompt: None)
        self.assertEqual(seen, [(None, True), (None, False)])


class MacosCatalogTests(unittest.TestCase):
    def test_chrome_then_edge_when_brave_is_missing(self):
        system = FakeSystem(browsers.MACOS, {MAC_CHROME: _stat(FILE), MAC_EDGE: _stat(FILE)})
        self.assertEqual(select_browser(system=system).app.id, "chrome")
        system = FakeSystem(browsers.MACOS, {MAC_EDGE: _stat(FILE)})
        chosen = select_browser(system=system)
        self.assertEqual(chosen.app.id, "edge")
        self.assertEqual(
            system.calls[0][0],
            [
                "/usr/sbin/spctl",
                "--assess",
                "--type",
                "execute",
                "/Applications/Microsoft Edge.app",
            ],
        )

    def test_the_users_choice_wins_over_the_order(self):
        files = {MAC_BRAVE: _stat(FILE), MAC_CHROME: _stat(FILE), MAC_EDGE: _stat(FILE)}
        for browser in ("chrome", "edge", "brave"):
            with self.subTest(browser):
                chosen = select_browser(browser, system=FakeSystem(browsers.MACOS, files))
                self.assertEqual(chosen.app.id, browser)

    def test_a_missing_choice_names_the_browser_and_never_falls_back(self):
        system = FakeSystem(browsers.MACOS, {MAC_BRAVE: _stat(FILE)})
        with self.assertRaisesRegex(BrowserBootstrapError, "Fant ikke Microsoft Edge"):
            select_browser("edge", system=system)
        self.assertEqual(system.calls, [])

    def test_unknown_choice_is_refused(self):
        with self.assertRaisesRegex(BrowserBootstrapError, "Ukjent nettleser"):
            select_browser("firefox", system=FakeSystem(browsers.MACOS, {MAC_BRAVE: _stat(FILE)}))

    def test_a_signature_from_another_vendor_is_refused(self):
        system = FakeSystem(browsers.MACOS, {MAC_CHROME: _stat(FILE)})
        system.signatures["Google Chrome.app"] = SIGNATURES["Brave Browser.app"]
        with self.assertRaisesRegex(BrowserBootstrapError, "ukjent signatur"):
            select_browser(system=system)

    def test_unsigned_browser_does_not_fall_back_to_the_next(self):
        system = FakeSystem(browsers.MACOS, {MAC_BRAVE: _stat(FILE), MAC_CHROME: _stat(FILE)})
        system.spctl = 3
        with self.assertRaisesRegex(BrowserBootstrapError, "Brave har ukjent signatur"):
            select_browser(system=system)

    def test_a_named_path_outside_the_catalog_is_refused(self):
        system = FakeSystem(
            browsers.MACOS, {MAC_BRAVE: _stat(FILE), "/tmp/Brave Browser": _stat(FILE)}
        )
        for path in ("/tmp/Brave Browser", "/Applications/Brave Browser.app/../x", "brave"):
            with (
                self.subTest(path),
                self.assertRaisesRegex(BrowserBootstrapError, "godkjent appsti"),
            ):
                select_browser(explicit=path, system=system)

    def test_a_link_at_a_catalog_path_is_refused(self):
        system = FakeSystem(browsers.MACOS, {MAC_BRAVE: _stat(LINK)})
        with self.assertRaisesRegex(BrowserBootstrapError, "Fant ingen godkjent nettleser"):
            select_browser(system=system)
        with self.assertRaisesRegex(BrowserBootstrapError, "Fant ingen godkjent nettleser"):
            select_browser(explicit=MAC_BRAVE, system=system)

    def test_a_login_server_only_starts_brave(self):
        system = FakeSystem(browsers.MACOS, {MAC_CHROME: _stat(FILE)})
        system.env = {"FAMILIEKILDER_HOSTED": "1"}
        with self.assertRaises(BrowserBootstrapError):
            select_browser(system=system)
        with self.assertRaises(BrowserBootstrapError):
            select_browser("chrome", system=system)

    def test_unsupported_platform(self):
        with self.assertRaisesRegex(BrowserBootstrapError, "macOS, Windows og Linux"):
            select_browser(system=FakeSystem("freebsd14"))


LINUX_BRAVE = "/opt/brave.com/brave/brave"
LINUX_CHROME = "/opt/google/chrome/chrome"
LINUX_EDGE = "/opt/microsoft/msedge/msedge"


class LinuxTests(unittest.TestCase):
    def system(self, files):
        return FakeSystem(browsers.LINUX, files)

    def test_brave_on_linux_is_unchanged(self):
        root_file = _stat(FILE, uid=0)
        scenarios = {
            "root owned": {LINUX_BRAVE: root_file},
            "user owned": {LINUX_BRAVE: _stat(FILE, uid=1000)},
            "group writable": {LINUX_BRAVE: _stat(stat.S_IFREG | 0o775, uid=0)},
            "writable folder": {
                LINUX_BRAVE: root_file,
                "/opt/brave.com": _stat(DIRECTORY | 0o777, uid=0),
            },
            "user folder": {
                LINUX_BRAVE: root_file,
                "/opt/brave.com/brave": _stat(DIRECTORY, uid=1000),
            },
            "linked folder": {LINUX_BRAVE: root_file, "/opt/brave.com": _stat(LINK, uid=0)},
        }
        for name, files in scenarios.items():
            for explicit in (None, LINUX_BRAVE):
                with self.subTest(name, explicit=explicit):
                    old = _outcome(_old_linux_select, self.system(files), explicit)
                    new = _outcome(select_browser, None, explicit, system=self.system(files))
                    if new[0] == "ok":
                        new = ("ok", new[1].executable)
                    self.assertEqual(new, old)

    def test_chrome_and_edge_from_the_vendor_packages(self):
        root_file = _stat(FILE, uid=0)
        chosen = select_browser(
            system=self.system({LINUX_CHROME: root_file, LINUX_EDGE: root_file})
        )
        self.assertEqual((chosen.app.id, chosen.executable), ("chrome", LINUX_CHROME))
        chosen = select_browser(
            "edge", system=self.system({LINUX_CHROME: root_file, LINUX_EDGE: root_file})
        )
        self.assertEqual((chosen.app.id, chosen.executable), ("edge", LINUX_EDGE))

    def test_a_user_writable_or_linked_package_is_refused(self):
        cases = {
            "user owned": {LINUX_CHROME: _stat(FILE, uid=1000)},
            "others may write": {LINUX_CHROME: _stat(stat.S_IFREG | 0o757, uid=0)},
            "linked": {LINUX_CHROME: _stat(FILE, uid=0), "/opt/google": _stat(LINK, uid=0)},
        }
        for name, files in cases.items():
            with (
                self.subTest(name),
                self.assertRaisesRegex(BrowserBootstrapError, "administrert pakke"),
            ):
                select_browser(system=self.system(files))

    def test_wrappers_and_other_paths_are_refused(self):
        system = self.system(
            {LINUX_CHROME: _stat(FILE, uid=0), "/usr/bin/google-chrome": _stat(FILE, uid=0)}
        )
        for path in ("/usr/bin/google-chrome", "/home/oppdiktet/chrome", "chrome"):
            with (
                self.subTest(path),
                self.assertRaisesRegex(BrowserBootstrapError, "godkjent appsti"),
            ):
                select_browser(explicit=path, system=system)

    def package(self, extra=None):
        root_file = _stat(stat.S_IFREG | 0o644, uid=0)
        files = {
            LINUX_CHROME: _stat(FILE, uid=0),
            "/opt/google/chrome/libEGL.so": root_file,
            "/opt/google/chrome/locales/nb.pak": root_file,
            "/opt/google/chrome/chrome-sandbox": _stat(stat.S_IFREG | 0o4755, uid=0),
        }
        files.update(extra or {})
        return self.system(files)

    def test_the_whole_package_folder_is_checked_on_a_desktop(self):
        self.assertEqual(select_browser(system=self.package()).executable, LINUX_CHROME)
        cases = {
            "library others may write": {"/opt/google/chrome/libEGL.so": _stat(stat.S_IFREG | 0o666, uid=0)},
            "library group may write": {"/opt/google/chrome/libEGL.so": _stat(stat.S_IFREG | 0o664, uid=0)},
            "data file owned by a user": {"/opt/google/chrome/locales/nb.pak": _stat(stat.S_IFREG | 0o644, uid=1000)},
            "subfolder others may write": {"/opt/google/chrome/locales": _stat(DIRECTORY | 0o777, uid=0)},
        }
        for name, extra in cases.items():
            with self.subTest(name), self.assertRaisesRegex(BrowserBootstrapError, "kan endres av andre"):
                select_browser(system=self.package(extra))

    def test_links_must_stay_inside_the_package_folder(self):
        link = _stat(LINK, uid=0)
        inside = self.package({"/opt/google/chrome/libvk.so.1": link})
        inside.links["/opt/google/chrome/libvk.so.1"] = "libEGL.so"
        self.assertEqual(select_browser(system=inside).executable, LINUX_CHROME)
        for target in ("/home/oppdiktet/libEGL.so", "../../../tmp/libEGL.so", "/opt/google/other.so"):
            with self.subTest(target):
                outside = self.package({"/opt/google/chrome/libvk.so.1": link})
                outside.links["/opt/google/chrome/libvk.so.1"] = target
                with self.assertRaisesRegex(BrowserBootstrapError, "lenker ut av mappen"):
                    select_browser(system=outside)

    def test_a_login_server_keeps_the_old_brave_check(self):
        # The old check looked at the executable and the folders above it only.
        writable_library = {"/opt/brave.com/brave/libEGL.so": _stat(stat.S_IFREG | 0o666, uid=0)}
        files = {LINUX_BRAVE: _stat(FILE, uid=0), **writable_library}
        server = self.system(files)
        server.env = {"FAMILIEKILDER_HOSTED": "1"}
        self.assertEqual(
            _outcome(select_browser, system=server)[0], _outcome(_old_linux_select, self.system(files))[0]
        )
        self.assertEqual(select_browser(system=server).executable, LINUX_BRAVE)
        with self.assertRaisesRegex(BrowserBootstrapError, "kan endres av andre"):
            select_browser(system=self.system(files))
        server.files["/opt/google/chrome/chrome"] = _stat(FILE, uid=0)
        del server.files[LINUX_BRAVE]
        with self.assertRaises(BrowserBootstrapError):
            select_browser(system=server, oauth_callback=True)

    def test_no_screen_gives_a_clear_message(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(BrowserBootstrapError, "Fant ingen skjerm"),
        ):
            browser_bootstrap._require_display()
        for environment in (
            {"DISPLAY": ":0"},
            {"WAYLAND_DISPLAY": "wayland-0"},
            {"FAMILIEKILDER_HOSTED": "1"},
        ):
            with (
                patch.object(sys, "platform", "linux"),
                patch.dict(os.environ, environment, clear=True),
            ):
                browser_bootstrap._require_display()


class WindowsTests(unittest.TestCase):
    def system(self, files=None, report=None):
        system = FakeSystem(browsers.WINDOWS, files or {WIN_CHROME: _stat(FILE)})
        system.windows_report = (
            report
            if report is not None
            else {
                "status": "Valid",
                "subject": "Google LLC",
                "writers": ["S-1-5-18", "S-1-5-32-544"],
            }
        )
        system.env = {
            "SystemRoot": r"C:\Windows",
            "PATH": r"C:\Users\oppdiktet\bin",
            "TEMP": r"C:\Temp",
        }
        return system

    def test_signed_chrome_in_program_files(self):
        system = self.system()
        chosen = select_browser(system=system)
        self.assertEqual((chosen.app.id, chosen.executable), ("chrome", WIN_CHROME))
        argv, env = system.calls[0]
        self.assertEqual(argv[0], r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        self.assertIn("Get-AuthenticodeSignature", argv[-1])
        # The path travels in the environment, never in the script, and PATH stays out.
        self.assertNotIn(WIN_CHROME, argv[-1])
        self.assertEqual(env["FAMILIEKILDER_BROWSER_CHECK"], WIN_CHROME)
        self.assertNotIn("PATH", env)

    def test_catalog_is_built_from_the_program_folders(self):
        paths = {app.id: app.executables for app in browsers.catalog(self.system())}
        self.assertEqual(
            paths["edge"],
            (
                PROGRAM_FILES + r"\Microsoft\Edge\Application\msedge.exe",
                WIN_EDGE,
            ),
        )
        self.assertIn(WIN_BRAVE, paths["brave"])
        self.assertTrue(
            all("AppData" not in path for app_paths in paths.values() for path in app_paths)
        )

    def test_order_brave_chrome_edge(self):
        files = {WIN_BRAVE: _stat(FILE), WIN_CHROME: _stat(FILE), WIN_EDGE: _stat(FILE)}
        system = self.system(
            files, {"status": "Valid", "subject": "Brave Software, Inc.", "writers": []}
        )
        self.assertEqual(select_browser(system=system).app.id, "brave")
        system = self.system(
            files, {"status": "Valid", "subject": "Microsoft Corporation", "writers": "S-1-5-18"}
        )
        self.assertEqual(select_browser("edge", system=system).executable, WIN_EDGE)

    def test_unsigned_or_wrong_publisher_is_refused(self):
        for report in (
            {"status": "NotSigned", "subject": None, "writers": []},
            {"status": "HashMismatch", "subject": "Google LLC", "writers": []},
            {"status": "Valid", "subject": "Oppdiktet Programvare AS", "writers": []},
            {"status": "Valid", "subject": "Microsoft Corporation", "writers": []},
        ):
            with (
                self.subTest(report),
                self.assertRaisesRegex(BrowserBootstrapError, "ukjent signatur"),
            ):
                select_browser(system=self.system(report=report))

    def test_a_folder_users_can_change_is_refused(self):
        for writer in ("S-1-5-32-545", "S-1-5-11", "S-1-1-0", "S-1-5-21-1-2-3-1001"):
            report = {"status": "Valid", "subject": "Google LLC", "writers": ["S-1-5-18", writer]}
            with (
                self.subTest(writer),
                self.assertRaisesRegex(BrowserBootstrapError, "administratorer"),
            ):
                select_browser(system=self.system(report=report))

    def test_the_script_uses_the_write_mask(self):
        self.assertIn(f"0x{browsers._WINDOWS_WRITE_MASK:08X}", browsers._WINDOWS_ACCESS_SCRIPT)

    def test_a_check_that_cannot_run_is_refused(self):
        for report in ({}, [], "Valid"):
            with (
                self.subTest(report),
                self.assertRaisesRegex(BrowserBootstrapError, "ukjent signatur|kontrolleres"),
            ):
                select_browser(system=self.system(report=report))
        system = self.system()
        system.run = lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
        with self.assertRaisesRegex(BrowserBootstrapError, "kunne ikke kontrolleres"):
            select_browser(system=system)

    def test_a_junction_on_the_way_is_refused(self):
        files = {
            WIN_CHROME: _stat(FILE),
            PROGRAM_FILES + r"\Google": _stat(DIRECTORY, attributes=0x400),
        }
        with self.assertRaisesRegex(BrowserBootstrapError, "lenke"):
            select_browser(system=self.system(files))
        files = {WIN_CHROME: _stat(FILE, attributes=0x400)}
        with self.assertRaisesRegex(BrowserBootstrapError, "Fant ingen godkjent nettleser"):
            select_browser(system=self.system(files))

    def test_per_user_and_named_paths_are_refused(self):
        user_chrome = r"C:\Users\oppdiktet\AppData\Local\Google\Chrome\Application\chrome.exe"
        system = self.system({user_chrome: _stat(FILE)})
        with self.assertRaisesRegex(BrowserBootstrapError, "Fant ingen godkjent nettleser"):
            select_browser(system=system)
        for path in (user_chrome, r"D:\chrome.exe", "chrome.exe"):
            with (
                self.subTest(path),
                self.assertRaisesRegex(BrowserBootstrapError, "godkjent appsti"),
            ):
                select_browser(explicit=path, system=system)
        # The same catalog path in another case or with forward slashes is the same file.
        chosen = select_browser(
            explicit=WIN_CHROME.upper().replace("\\", "/"), system=self.system()
        )
        self.assertEqual(chosen.executable, WIN_CHROME)

    def test_launch_passes_two_inherited_pipe_handles(self):
        handles = iter((701, 702))
        started = {}

        class FakeStartupInfo:
            lpAttributeList: dict = {}

        class FakePopen:
            def __init__(self, command, **options):
                started["command"] = command
                started["options"] = options

        fake_msvcrt = types.SimpleNamespace(get_osfhandle=lambda descriptor: next(handles))
        inheritable = []
        with (
            patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
            patch.object(
                os,
                "set_handle_inheritable",
                lambda handle, value: inheritable.append((handle, value)),
                create=True,
            ),
            patch.object(subprocess, "STARTUPINFO", FakeStartupInfo, create=True),
            patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True),
            patch.object(subprocess, "Popen", FakePopen),
            patch.object(
                browser_bootstrap, "_PipeCdpClient", lambda write, read: ("client", write, read)
            ),
        ):
            handle = browser_bootstrap._launch_windows(
                ["chrome.exe", "--user-data-dir=x", "https://example.invalid/"]
            )
        self.assertEqual(
            started["command"],
            [
                "chrome.exe",
                "--user-data-dir=x",
                "https://example.invalid/",
                "--remote-debugging-io-pipes=701,702",
            ],
        )
        self.assertEqual(
            started["options"]["startupinfo"].lpAttributeList, {"handle_list": [701, 702]}
        )
        self.assertTrue(started["options"]["close_fds"])
        self.assertEqual(inheritable, [(701, True), (702, True)])
        _, write, read = handle.cdp
        os.close(write)
        os.close(read)

    def test_pipe_reader_thread_used_on_windows(self):
        request_read, request_write = os.pipe()
        reply_read, reply_write = os.pipe()
        with patch.object(os, "name", "nt"):
            client = browser_bootstrap._PipeCdpClient(request_write, reply_read)
        self.assertIsNotNone(client._reader)

        def browser():
            data = b""
            while not data.endswith(b"\0"):
                data += os.read(request_read, 4096)
            request = json.loads(data[:-1])
            os.write(reply_write, b'{"method":"Page.event"}\0')
            os.write(
                reply_write,
                json.dumps({"id": request["id"], "result": {"ok": True}}).encode() + b"\0",
            )

        thread = threading.Thread(target=browser)
        thread.start()
        self.assertEqual(client.call("Browser.getVersion", timeout=5), {"ok": True})
        thread.join(5)
        with self.assertRaisesRegex(BrowserBootstrapError, "tidsgrensen"):
            client.call("Browser.getVersion", timeout=0.2)
        os.close(reply_write)
        client.close()
        os.close(request_read)


class InstalledTests(unittest.TestCase):
    def test_installed_lists_present_browsers_without_checking_them(self):
        system = FakeSystem(browsers.MACOS, {MAC_CHROME: _stat(FILE), MAC_EDGE: _stat(LINK)})
        self.assertEqual([app.id for app in browsers.installed(system)], ["chrome"])
        self.assertEqual(system.calls, [])


class SavedBrowserTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "data"

    def test_round_trip_and_reset(self):
        self.assertIsNone(browsers.saved_browser(self.root))
        browsers.save_browser(self.root, "edge")
        self.assertEqual(browsers.saved_browser(self.root), "edge")
        browsers.save_browser(self.root, None)
        self.assertIsNone(browsers.saved_browser(self.root))
        with self.assertRaises(BrowserBootstrapError):
            browsers.save_browser(self.root, "/usr/bin/evil")

    def test_bootstrap_starts_the_saved_browser(self):
        browsers.save_browser(self.root, "chrome")
        seen = {}

        def launch(profile, url, path, *, browser=None):
            seen["browser"] = browser
            raise BrowserBootstrapError("stopp")

        with (
            patch.object(browser_bootstrap, "_launch_brave", launch),
            patch("familiekilder.spond_auth.load_client_config"),
            self.assertRaisesRegex(BrowserBootstrapError, "stopp"),
        ):
            browser_bootstrap.bootstrap_spond(self.root, wait_for_user=lambda prompt: None)
        self.assertEqual(seen["browser"], "chrome")
        with (
            patch.object(browser_bootstrap, "_launch_brave", launch),
            patch("familiekilder.spond_auth.load_client_config"),
            self.assertRaisesRegex(BrowserBootstrapError, "stopp"),
        ):
            browser_bootstrap.bootstrap_spond(
                self.root, wait_for_user=lambda prompt: None, login_browser="edge"
            )
        self.assertEqual(seen["browser"], "edge")


class PasswordStorageTests(unittest.TestCase):
    def test_service_and_account_names_are_unchanged(self):
        root = Path("/hjem/oppdiktet/Library/Application Support/familiekilder-mcp")
        self.assertEqual(login_passwords.SERVICE, "no.familiekilder.login-password.v1")
        digest = hashlib.sha256(str(project_paths(root).root).encode()).hexdigest()
        self.assertEqual(login_passwords.account(root, "bankid"), f"{digest}:bankid")

    def fake_backends(self, available=True):
        class Backend:
            priority = 5

        class Unavailable:
            # keyring's `priority` is a class property that raises without a store.
            def __get__(self, instance, owner):
                raise RuntimeError("no store")

        class Missing(Backend):
            priority = Unavailable()

        chosen = Backend if available else Missing
        return {
            "keyring.backends.macOS": types.SimpleNamespace(Keyring=Backend),
            "keyring.backends.Windows": types.SimpleNamespace(WinVaultKeyring=chosen),
            "keyring.backends.SecretService": types.SimpleNamespace(Keyring=chosen),
        }

    def test_each_platform_uses_its_own_store(self):
        modules = self.fake_backends()
        with (
            patch.dict(sys.modules, modules),
            patch.dict(
                os.environ, {"PYTHON_KEYRING_BACKEND": "keyrings.alt.file.PlaintextKeyring"}
            ),
        ):
            self.assertIs(
                type(desktop_platform.password_backend("darwin")),
                modules["keyring.backends.macOS"].Keyring,
            )
            self.assertIs(
                type(desktop_platform.password_backend("win32")),
                modules["keyring.backends.Windows"].WinVaultKeyring,
            )
            self.assertIs(
                type(desktop_platform.password_backend("linux")),
                modules["keyring.backends.SecretService"].Keyring,
            )
        self.assertEqual(desktop_platform.password_storage("darwin"), "macos-keychain")
        self.assertEqual(desktop_platform.password_storage("win32"), "windows-credential-manager")
        self.assertEqual(desktop_platform.password_storage("linux"), "secret-service")

    def test_a_missing_store_is_a_clear_error(self):
        with patch.dict(sys.modules, self.fake_backends(available=False)):
            with self.assertRaisesRegex(FamiliekilderError, "Fant ingen nøkkelring"):
                desktop_platform.password_backend("linux")
            with self.assertRaisesRegex(FamiliekilderError, "Legitimasjonsbehandling"):
                desktop_platform.password_backend("win32")
        with self.assertRaisesRegex(FamiliekilderError, "macOS, Windows og Linux"):
            desktop_platform.password_backend("freebsd14")

    def test_saving_without_a_store_says_what_is_missing(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(sys.modules, self.fake_backends(available=False)),
            patch.object(sys, "platform", "linux"),
        ):
            with self.assertRaisesRegex(FamiliekilderError, "Fant ingen nøkkelring"):
                login_passwords.set_password(Path(directory), "bankid", "oppdiktet")
            self.assertFalse(login_passwords.configured(Path(directory), "bankid"))

    def test_login_without_a_stored_password_never_touches_the_store(self):
        from familiekilder.password_autofill import PasswordAutofill

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                desktop_platform, "password_backend", side_effect=AssertionError("store read")
            ),
        ):
            autofill = PasswordAutofill(Path(directory), "bankid")
            self.assertFalse(autofill.enabled)
            self.assertFalse(autofill.step(object()))

    def test_status_names_the_store(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, "platform", "darwin"):
            status = login_passwords.run_password_command(Path(directory), "bankid", "status")
        self.assertEqual(
            status, {"kind": "bankid", "configured": False, "storage": "macos-keychain"}
        )


def _old_machine_identity(query):
    """Frozen copy of the old macOS `_machine_identity`."""

    found = re.search(
        r'"IOPlatformUUID"\s*=\s*"([^"]+)"',
        query('/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'),
    )
    return found.group(1) if found else None


class MachineBindingTests(unittest.TestCase):
    IOREG = '  "IOPlatformUUID" = "00000000-1111-2222-3333-444444444444"\n'

    def test_macos_hardware_id_is_unchanged(self):
        for output in (self.IOREG, "", '"IOPlatformUUID" = ""', "noise"):
            calls_old, calls_new = [], []

            def old_query(*command):
                calls_old.append(command)
                return output

            def new_query(*command):
                calls_new.append(command)
                return output

            with self.subTest(output):
                self.assertEqual(
                    desktop_platform.machine_identity("darwin", query=new_query),
                    _old_machine_identity(old_query),
                )
                self.assertEqual(calls_new, calls_old)

    def test_macos_owner_file_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(sys, "platform", "darwin"),
                patch.object(life, "_mac_query", return_value=self.IOREG),
                patch.object(life.socket, "gethostname", return_value="oppdiktet.lan"),
            ):
                owner = life._expected_owner(root)
        self.assertEqual(
            owner,
            {
                "root": str(root.resolve()),
                "host": hashlib.sha256(b"oppdiktet.lan").hexdigest(),
                "machine": hashlib.sha256(b"00000000-1111-2222-3333-444444444444").hexdigest(),
            },
        )

    def test_windows_machine_guid(self):
        guid = "0A1B2C3D-0000-1111-2222-333344445555"
        self.assertEqual(
            desktop_platform.machine_identity(
                "win32", windows_guid=lambda: guid, installation=lambda: None
            ),
            guid.lower(),
        )
        self.assertIsNone(
            desktop_platform.machine_identity(
                "win32", windows_guid=lambda: "x", installation=lambda: None
            )
        )

    def test_linux_keeps_the_hostname_binding(self):
        self.assertIsNone(desktop_platform.machine_identity("linux", installation=lambda: None))

    def test_a_server_keeps_binding_to_its_installation_id(self):
        self.assertIsNone(
            desktop_platform.machine_identity(
                "win32",
                windows_guid=lambda: "0A1B2C3D-0000-1111-2222-333344445555",
                installation=lambda: "installasjon",
            )
        )


USER_SID = "S-1-5-21-1111-2222-3333-1001"


class WindowsPrivateFolderTests(unittest.TestCase):
    OWNED = f"O:{USER_SID}G:{USER_SID}"

    def test_the_access_list_written_is_private(self):
        from familiekilder.windows_acl import private_sddl, sddl_is_private

        self.assertEqual(private_sddl(USER_SID), f"D:P(A;OICI;FA;;;{USER_SID})(A;OICI;FA;;;SY)")
        self.assertTrue(sddl_is_private(self.OWNED + private_sddl(USER_SID), USER_SID))

    def test_inherited_or_shared_access_lists_are_refused(self):
        from familiekilder.windows_acl import sddl_is_private

        cases = {
            "inherited from the drive": f"D:AI(A;OICIID;FA;;;{USER_SID})(A;OICIID;0x1301bf;;;AU)",
            "protected but users may read": f"D:P(A;OICI;FA;;;{USER_SID})(A;OICI;0x1200a9;;;BU)",
            "another user": "D:P(A;OICI;FA;;;S-1-5-21-1111-2222-3333-1002)",
            "everyone": f"D:P(A;OICI;FA;;;{USER_SID})(A;;FR;;;WD)",
            "empty": "D:P",
        }
        for name, dacl in cases.items():
            with self.subTest(name):
                self.assertFalse(sddl_is_private(self.OWNED + dacl, USER_SID))
        self.assertFalse(sddl_is_private("O:BA", USER_SID))
        self.assertTrue(
            sddl_is_private(
                f"O:BAG:SYD:PAI(A;OICI;FA;;;{USER_SID})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", USER_SID
            )
        )
        # Windows writes the built-in Administrator account as the alias LA.
        aliases = {"LA": USER_SID}
        self.assertTrue(
            sddl_is_private(
                "O:LAG:SYD:P(A;OICI;FA;;;LA)(A;OICI;FA;;;SY)",
                USER_SID,
                lambda sid: aliases.get(sid, sid),
            )
        )
        self.assertFalse(
            sddl_is_private(
                "O:LAG:SYD:P(A;OICI;FA;;;LA)(A;OICI;FA;;;SY)",
                "S-1-5-21-9-9-9-1001",
                lambda sid: aliases.get(sid, sid),
            )
        )

    def test_a_folder_owned_by_another_user_is_refused(self):
        from familiekilder.windows_acl import sddl_is_private, sddl_owner_ok

        dacl = f"D:P(A;OICI;FA;;;{USER_SID})(A;OICI;FA;;;SY)"
        other = "S-1-5-21-1111-2222-3333-1002"
        for owner in (other, "BU", "WD", "AU"):
            with self.subTest(owner):
                sddl = f"O:{owner}G:SY{dacl}"
                self.assertFalse(sddl_owner_ok(sddl, USER_SID))
                self.assertFalse(sddl_is_private(sddl, USER_SID))
        for owner in (USER_SID, "SY", "BA", "S-1-5-32-544"):
            with self.subTest(owner):
                self.assertTrue(sddl_is_private(f"O:{owner}G:SY{dacl}", USER_SID))
        self.assertFalse(sddl_owner_ok(dacl, USER_SID))

    def test_a_private_file_may_inherit_but_not_be_shared(self):
        from familiekilder.windows_acl import sddl_is_private

        inherited = f"O:{USER_SID}G:SYD:AI(A;ID;FA;;;{USER_SID})(A;ID;FA;;;SY)"
        self.assertTrue(sddl_is_private(inherited, USER_SID, protected=False))
        self.assertFalse(sddl_is_private(inherited, USER_SID))
        for extra in ("(A;;FR;;;BU)", "(A;;0x1200a9;;;AU)", "(A;ID;FR;;;WD)"):
            with self.subTest(extra):
                self.assertFalse(sddl_is_private(inherited + extra, USER_SID, protected=False))
        # Python 3.12.4+ makes folders with mode 0o700 like this on Windows;
        # OWNER RIGHTS means the owner, who must be allowed anyway.
        python_folder = "O:LAG:SYD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)"
        self.assertTrue(sddl_is_private(python_folder, USER_SID, lambda sid: {"LA": USER_SID}.get(sid, sid)))
        self.assertFalse(sddl_is_private(python_folder.replace("O:LA", "O:BU"), USER_SID))
        # A deny entry only takes access away.
        self.assertTrue(sddl_is_private(inherited + "(D;;FA;;;BU)", USER_SID, protected=False))

    @unittest.skipUnless(os.name == "nt", "Windows access lists exist only on Windows")
    def test_private_folder_and_files_on_windows(self):
        from familiekilder_testing.private import readable_by_others

        from familiekilder.private_state import (
            PrivateStateError,
            atomic_write_private_json,
            read_private_json,
        )
        from familiekilder.windows_acl import is_private, owner_allowed, read_sddl

        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / ".private"
            target = private / "credentials" / "x.json"
            atomic_write_private_json(target, {"a": 1})
            self.assertTrue(is_private(private), read_sddl(private))
            self.assertTrue(owner_allowed(private), read_sddl(private))
            self.assertEqual(read_private_json(target), {"a": 1})
            readable_by_others(target)
            with self.assertRaisesRegex(PrivateStateError, "brede"):
                read_private_json(target)


class DataRootTests(unittest.TestCase):
    def test_default_data_root_per_platform(self):
        home = Path("/home/oppdiktet")
        self.assertEqual(
            desktop_platform.default_data_root("darwin", {}, home),
            home / "Library/Application Support/familiekilder-mcp",
        )
        self.assertEqual(
            desktop_platform.default_data_root("linux", {}, home),
            home / ".local/share/familiekilder-mcp",
        )
        self.assertEqual(
            desktop_platform.default_data_root("linux", {"XDG_DATA_HOME": "/data"}, home),
            Path("/data/familiekilder-mcp"),
        )
        self.assertEqual(
            desktop_platform.default_data_root("win32", {"LOCALAPPDATA": "/appdata"}, home),
            Path("/appdata/familiekilder-mcp"),
        )


if __name__ == "__main__":
    unittest.main()
