"""`familiekilder-mcp doctor`: a report that is safe to paste into a public issue.

Each platform is made up with a fake computer, a made-up home folder and a
made-up user name that must never show in the report. Nothing logs in, reads
a password or contacts a service.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from familiekilder import browsers
from familiekilder import doctor
from familiekilder import source_cli
from familiekilder.connection_status import auth_module
from familiekilder.core import FamiliekilderError
from familiekilder.live_config import Config
from familiekilder.private_state import atomic_write_private_json, ensure_private_directory
from familiekilder.setup_web import SetupServer
from familiekilder_testing.browsers import (
    FILE,
    PROGRAM_FILES,
    PROGRAM_FILES_X86,
    FakeBrowserSystem,
    stat_result,
)

# Distinctive, made-up values that must never appear in a report.
USER = "Qzorbatnik"
POSIX_HOME = f"/hjem/{USER}"
WINDOWS_HOME = rf"C:\Brukere\{USER}"
WINDOWS_LOCAL = WINDOWS_HOME + r"\AppData\Local"

MAC_BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
MAC_HOME_CHROME = f"{POSIX_HOME}/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WIN_EDGE = PROGRAM_FILES_X86 + r"\Microsoft\Edge\Application\msedge.exe"
WIN_CHROME = PROGRAM_FILES + r"\Google\Chrome\Application\chrome.exe"
USER_BRAVE = WINDOWS_LOCAL + r"\BraveSoftware\Brave-Browser\Application\brave.exe"
USER_CHROME = WINDOWS_LOCAL + r"\Google\Chrome\Application\chrome.exe"
LINUX_CHROME = "/opt/google/chrome/chrome"
LINUX_SNAP_BRAVE = "/snap/bin/brave"
LINUX_FLATPAK_EDGE = f"{POSIX_HOME}/.local/share/flatpak/exports/bin/com.microsoft.Edge"


def store_found(_platform):
    return object()


def store_missing(platform):
    raise FamiliekilderError(
        "Fant ingen nøkkelring (Secret Service). Lagrede passord er av; skriv passordet i "
        "nettleseren, eller start en nøkkelring som GNOME Keyring eller KWallet."
    )


def fake_computer(platform):
    """A made-up computer for each platform, with one refused and one usable browser."""

    if platform == browsers.WINDOWS:
        system = FakeBrowserSystem(
            platform,
            {
                USER_BRAVE: stat_result(FILE),
                USER_CHROME: stat_result(FILE),
                WIN_EDGE: stat_result(FILE),
            },
            home=WINDOWS_HOME,
            environ={"USERNAME": USER, "SystemRoot": r"C:\Windows"},
            local_app_data=WINDOWS_LOCAL,
        )
        return system, ("Windows", "11 (10.0.26100)", "AMD64")
    if platform == browsers.MACOS:
        system = FakeBrowserSystem(
            platform,
            {MAC_BRAVE: stat_result(FILE), MAC_HOME_CHROME: stat_result(FILE)},
            home=POSIX_HOME,
            environ={"USER": USER},
        )
        system.signatures["Brave Browser.app"] = "Identifier=x\nTeamIdentifier=y\n"
        return system, ("macOS", "15.1", "arm64")
    system = FakeBrowserSystem(
        platform,
        {
            LINUX_SNAP_BRAVE: stat_result(FILE, uid=0),
            LINUX_FLATPAK_EDGE: stat_result(FILE, uid=1000),
            LINUX_CHROME: stat_result(FILE, uid=0),
        },
        home=POSIX_HOME,
        environ={"USER": USER, "WAYLAND_DISPLAY": "wayland-0"},
    )
    return system, ("Ubuntu 24.04 LTS", "kjerne 6.8.0", "x86_64")


class DoctorCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "data"
        self.client_home = self.directory / "home"
        self.client_home.mkdir()

    def report(self, platform, *, root=None, store=store_found, **options):
        system, operating_system = fake_computer(platform)
        options.setdefault("system", system)
        options.setdefault("client_home", self.client_home)
        return doctor.run(
            root or self.root,
            operating_system=operating_system,
            python=("3.12.7", True),
            password_backend=store,
            **options,
        )


class PrivacyTests(DoctorCase):
    def test_home_folder_and_user_name_never_show_on_any_platform(self):
        for platform in browsers.PLATFORMS:
            home = WINDOWS_HOME if platform == browsers.WINDOWS else POSIX_HOME
            # A data root below the home folder and one elsewhere with the user name in it.
            for root in (
                Path(home) / ".local" / "share" / "familiekilder-mcp",
                Path(f"/data/{USER}/familiekilder-mcp"),
            ):
                with self.subTest(platform=platform, root=str(root)):
                    report = self.report(platform, root=root, client_home=Path(home))
                    text = doctor.render(report)
                    data = json.dumps(report, ensure_ascii=False)
                    for output in (text, data):
                        self.assertNotIn(USER.lower(), output.lower())
                        self.assertNotIn(home.lower(), output.lower())
                    self.assertIn("~", text)

    def test_paths_are_shown_from_the_home_folder(self):
        report = self.report(browsers.WINDOWS, client_home=Path(WINDOWS_HOME))
        paths = {item["browser"]: item["path"] for item in report["browsers"]["items"]}
        self.assertEqual(paths["chrome"], r"~\AppData\Local\Google\Chrome\Application\chrome.exe")
        self.assertEqual(
            paths["brave"], r"~\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"
        )
        report = self.report(browsers.MACOS, client_home=Path(POSIX_HOME))
        paths = {item["browser"]: item["path"] for item in report["browsers"]["items"]}
        self.assertEqual(
            paths["chrome"], "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )

    def test_the_redactor_keeps_other_users_and_words(self):
        redactor = doctor.Redactor(home="/home/ann", username="ann", windows=False)
        self.assertEqual(redactor.text("/home/ann/x"), "~/x")
        self.assertEqual(redactor.text("/home/anne/x"), "/home/anne/x")
        self.assertEqual(
            redactor.text("Plan for ann og /srv/ann/y"), "Plan for ann og /srv/<bruker>/y"
        )
        windows = doctor.Redactor(home=r"C:\Brukere\Ann", username="Ann", windows=True)
        self.assertEqual(windows.text(r"c:\brukere\ann\AppData"), r"~\AppData")
        self.assertEqual(windows.text("C:/Brukere/Ann/AppData"), "~/AppData")
        # A local app folder moved outside the home folder.
        moved = doctor.Redactor(
            home=r"C:\Brukere\Ann",
            username="Ann",
            windows=True,
            local_app_data=r"D:\Profiler\Ann\AppData\Local",
        )
        self.assertEqual(
            moved.text(r"D:\Profiler\Ann\AppData\Local\Google\chrome.exe"),
            r"%LOCALAPPDATA%\Google\chrome.exe",
        )
        self.assertEqual(moved.text(r"D:\Profiler\Ann\Annet"), r"D:\Profiler\<bruker>\Annet")


class ReportTests(DoctorCase):
    def test_each_platform_says_which_browser_is_used_and_why_others_are_refused(self):
        expected = {
            browsers.WINDOWS: (
                "edge",
                {"brave": "rejected", "chrome": "rejected", "edge": "candidate"},
            ),
            browsers.MACOS: (
                "chrome",
                {"brave": "rejected", "chrome": "candidate", "edge": "missing"},
            ),
            browsers.LINUX: (
                "chrome",
                {"brave": "rejected", "chrome": "selected", "edge": "rejected"},
            ),
        }
        for platform, (selected, statuses) in expected.items():
            with self.subTest(platform):
                system, _ = fake_computer(platform)
                report = self.report(platform, system=system)
                self.assertTrue(report["ok"])
                section = report["browsers"]
                if platform == browsers.LINUX:
                    self.assertEqual(section["selected"], selected)
                    self.assertIsNone(section["candidate"])
                    self.assertEqual(system.calls, [])
                else:
                    self.assertEqual(section["candidate"], selected)
                    self.assertIsNone(section["selected"])
                    self.assertEqual(section["status"], "pending")
                    self.assertTrue(system.calls)
                    if platform == browsers.MACOS:
                        self.assertTrue(all(call[:2] == ["/usr/bin/codesign", "-dv"]
                                            for call in system.calls))
                    else:
                        self.assertTrue(all("Get-AuthenticodeSignature" not in call[-1]
                                            for call in system.calls))
                items = report["browsers"]["items"]
                self.assertEqual({item["browser"]: item["status"] for item in items}, statuses)
                for item in items:
                    if item["status"] == "rejected":
                        self.assertTrue(item["reason"] and item["advice"], item)
                text = doctor.render(report)
                if platform == browsers.LINUX:
                    self.assertIn("Resultat: OK.", text)
                else:
                    self.assertIn("Signaturen sjekkes ved innlogging.", text)
                    self.assertNotIn("BRUKES", text)
                self.assertIn("AVVIST", text)
                self.assertIn("Råd: ", text)

    def test_the_text_format(self):
        report = self.report(browsers.WINDOWS)
        lines = doctor.render(report).splitlines()
        self.assertEqual(lines[0], "familiekilder-mcp doctor")
        self.assertEqual(
            lines[1], "Resultat: Ingen lokale feil funnet. Signaturen sjekkes ved innlogging."
        )
        start = lines.index("Nettleser for innlogging")
        self.assertEqual(
            lines[start : start + 10],
            [
                "Nettleser for innlogging",
                "  Valg: automatisk (den første godkjente av Brave, Google Chrome og Microsoft Edge)",
                "  AVVIST      Brave",
                r"              Sti: ~\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe",
                "              Grunn: Brave er bare installert for din bruker, under AppData.",
                "              Råd: Installer den for alle brukere (kjør installasjonsprogrammet "
                "som administrator).",
                "  AVVIST      Google Chrome",
                r"              Sti: ~\AppData\Local\Google\Chrome\Application\chrome.exe",
                "              Grunn: Google Chrome er bare installert for din bruker, under AppData.",
                "              Råd: Installer den for alle brukere (kjør installasjonsprogrammet "
                "som administrator).",
            ],
        )
        self.assertIn("  KANDIDAT    Microsoft Edge", lines)
        for title in (
            "Program",
            "Passordlagring",
            "Datamappe",
            "AI-klienter",
            "Tjenester som er slått på",
        ):
            self.assertIn(title, lines)
        self.assertNotIn("Skjerm", lines)
        self.assertIn("  System: Windows 11 (10.0.26100), AMD64", lines)
        self.assertIn("  Python: 3.12.7, i et virtuelt miljø (venv)", lines)

    def test_the_text_prints_in_a_windows_console(self):
        for platform in browsers.PLATFORMS:
            for store in (store_found, store_missing):
                with self.subTest(platform=platform, store=store.__name__):
                    text = doctor.render(self.report(platform, store=store))
                    for encoding in ("cp850", "cp1252"):
                        text.encode(encoding)

    def test_a_chosen_browser_that_cannot_be_used_fails_with_advice(self):
        browsers.save_browser(self.root, "chrome")
        report = self.report(browsers.WINDOWS)
        self.assertFalse(report["ok"])
        self.assertEqual(report["problems"], 1)
        self.assertEqual(report["browsers"]["choice"], "chrome")
        self.assertIsNone(report["browsers"]["selected"])
        chrome = next(item for item in report["browsers"]["items"] if item["browser"] == "chrome")
        self.assertEqual(
            chrome["advice"],
            "Installer den for alle brukere (kjør installasjonsprogrammet som administrator), "
            "eller velg Microsoft Edge med «familiekilder-mcp browser edge».",
        )
        text = doctor.render(report)
        self.assertIn("Resultat: 1 problem må rettes før innlogging.", text)
        self.assertIn("  FEIL        Ingen nettleser kan brukes til innlogging.", text)

    def test_no_browser_at_all_fails_with_advice(self):
        system = FakeBrowserSystem(browsers.LINUX, {}, home=POSIX_HOME, environ={"DISPLAY": ":0"})
        report = self.report(browsers.LINUX, system=system)
        self.assertFalse(report["ok"])
        self.assertIn(
            "Installer Brave, Google Chrome eller Microsoft Edge", report["browsers"]["advice"]
        )

    def test_missing_password_storage_is_a_note_with_advice_not_a_failure(self):
        report = self.report(browsers.LINUX, store=store_missing)
        self.assertTrue(report["ok"])
        self.assertEqual(report["passwords"]["status"], "note")
        self.assertIn("GNOME Keyring", report["passwords"]["advice"])
        self.assertIn("MERK", doctor.render(report))

    def test_the_password_store_is_only_found_never_used(self):
        class Store:
            def __getattr__(self, name):
                raise AssertionError(f"the store was used: {name}")

        report = self.report(browsers.MACOS, store=lambda _platform: Store())
        self.assertEqual(report["passwords"]["status"], "ok")

    def test_a_linux_desktop_without_a_screen_fails(self):
        system, _ = fake_computer(browsers.LINUX)
        system.env = {"USER": USER}
        report = self.report(browsers.LINUX, system=system)
        self.assertFalse(report["ok"])
        self.assertEqual(report["display"]["status"], "error")
        self.assertIn("ikke over SSH", report["display"]["advice"])
        self.assertIn("\nSkjerm\n  FEIL", doctor.render(report))

    def test_nothing_is_created(self):
        self.report(browsers.MACOS)
        self.assertFalse(self.root.exists())
        report = self.report(browsers.MACOS)
        self.assertEqual(report["data"]["status"], "absent")
        self.assertEqual(report["data"]["source"], "standard")

    @unittest.skipUnless(hasattr(os, "geteuid"), "file modes are checked on macOS and Linux")
    def test_a_private_folder_others_can_read_fails_with_advice(self):
        private = self.root / ".private"
        private.mkdir(parents=True, mode=0o700)
        self.assertEqual(self.report(browsers.LINUX)["data"]["status"], "ok")
        private.chmod(0o755)
        self.addCleanup(private.chmod, 0o700)
        report = self.report(browsers.LINUX)
        self.assertFalse(report["ok"])
        self.assertEqual(report["data"]["status"], "error")
        self.assertIn("chmod 700", report["data"]["advice"])

    def test_services_say_only_whether_a_login_is_stored(self):
        Config(self.root, {"spond": frozenset(), "vigilo": frozenset()}).save()
        session = auth_module("spond").session_path(self.root)
        ensure_private_directory(session.parent)
        atomic_write_private_json(session, {"refresh_token": "fiktivt-token-som-ikke-skal-vises"})
        report = self.report(browsers.LINUX)
        self.assertEqual(
            [(item["service"], item["stored_login"]) for item in report["services"]["items"]],
            [("vigilo", False), ("spond", True)],
        )
        text = doctor.render(report)
        self.assertIn("lagret innlogging: ja", text)
        self.assertNotIn("fiktivt-token", text + json.dumps(report))

    def test_clients_say_whether_they_start_this_installation(self):
        from familiekilder.setup_clients import install_client

        install_client("codex", self.root, self.client_home)
        report = self.report(browsers.LINUX)
        clients = {client["client"]: client for client in report["clients"]}
        self.assertEqual(clients["codex"]["status"], "ok")
        self.assertEqual(clients["claude"]["status"], "missing")
        # Registered for another data root: a note with advice.
        report = self.report(browsers.LINUX, root=self.directory / "other")
        codex = next(client for client in report["clients"] if client["client"] == "codex")
        self.assertEqual(codex["status"], "note")
        self.assertIn("Oppdater koblingen", codex["advice"])


class CommandTests(DoctorCase):
    def run_cli(self, platform, *arguments, files=None):
        system, _ = fake_computer(platform)
        if files is not None:
            system.files = files
        stdout = io.StringIO()
        with (
            patch.object(browsers, "System", return_value=system),
            patch("familiekilder.desktop_platform.password_backend", store_found),
            contextlib.redirect_stdout(stdout),
        ):
            code = source_cli.main(["--root", str(self.root), "doctor", *arguments])
        return code, stdout.getvalue()

    def test_exit_code_and_full_report(self):
        code, text = self.run_cli(browsers.LINUX)
        self.assertEqual(code, 0)
        self.assertIn("Datarot: valgt med --root", text)
        self.assertIn("Tjenester som er slått på", text)
        code, text = self.run_cli(browsers.LINUX, files={})
        self.assertEqual(code, 1)
        # The whole report is still written.
        self.assertIn("Tjenester som er slått på", text)
        self.assertFalse(self.root.exists())

    def test_json_has_the_same_privacy_and_is_ascii(self):
        code, output = self.run_cli(browsers.LINUX, "--json")
        self.assertEqual(code, 0)
        output.encode("ascii")
        report = json.loads(output)
        self.assertTrue(report["ok"])
        self.assertEqual(report["program"]["name"], "familiekilder-mcp")
        self.assertNotIn(USER.lower(), output.lower())

    def test_an_unreadable_configuration_still_gives_the_report(self):
        private = self.root / ".private"
        self.root.mkdir()
        ensure_private_directory(private)
        atomic_write_private_json(private / "mcp-config.json", {"version": 2})
        code, text = self.run_cli(browsers.LINUX)
        self.assertEqual(code, 1)
        self.assertIn("Oppsettet av tjenestene kan ikke leses", text)

    def test_cli_and_wizard_hide_named_and_custom_roots_even_in_errors(self):
        profile = "FamilienEksempel"
        folder = "Privat familiemappe"
        address = "foresatt@example.invalid"
        base = self.directory / folder / address
        system, _ = fake_computer(browsers.LINUX)
        system.home_folder = str(self.client_home)

        def read_report(server):
            with ThreadPoolExecutor(max_workers=1) as pool:
                server.timeout = 5
                handled = pool.submit(server.handle_request)
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.request(
                        "GET", "/api/doctor", headers={"X-Setup-Token": server.token}
                    )
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    return json.loads(response.read())
                finally:
                    connection.close()
                    handled.result(timeout=5)

        for options, source, root in (
            (["--profile", profile], "--profile", base / "profiles" / profile),
            (["--root", str(base)], "--root", base),
            ([], "FAMILIEKILDER_ROOT", base),
        ):
            with self.subTest(source=source):
                root.mkdir(parents=True, exist_ok=True)
                ensure_private_directory(root / ".private")
                failure = OSError(f"Kunne ikke lese {root} med kontoen {address}")
                wizard_reports = []

                def read_instead_of_serve(server, *, poll_interval):
                    wizard_reports.append(read_report(server))
                    with patch.object(Config, "load", side_effect=failure):
                        wizard_reports.append(read_report(server))

                with (
                    patch.dict(os.environ, {"FAMILIEKILDER_ROOT": str(base)}),
                    patch.object(Path, "home", return_value=self.client_home),
                    patch.object(browsers, "System", return_value=system),
                    patch("familiekilder.desktop_platform.password_backend", store_found),
                    patch.object(SetupServer, "serve_forever", read_instead_of_serve),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(source_cli.main([*options, "setup", "--no-open"]), 0)
                    for error, wizard in zip((None, failure), wizard_reports, strict=True):
                        with contextlib.ExitStack() as stack:
                            if error:
                                stack.enter_context(patch.object(Config, "load", side_effect=error))
                            stdout = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                            self.assertEqual(source_cli.main([*options, "doctor"]), int(bool(error)))
                            text = stdout.getvalue()
                            stdout.seek(0)
                            stdout.truncate(0)
                            self.assertEqual(
                                source_cli.main([*options, "doctor", "--json"]), int(bool(error))
                            )
                            report = json.loads(stdout.getvalue())
                        self.assertEqual((wizard["text"], wizard["report"]), (text, report))
                        self.assertEqual(report["data"]["source"], source)
                        self.assertNotIn("root", report["data"])
                        self.assertEqual(report["data"]["status"], "error" if error else "ok")
                        for private in (profile, folder, address):
                            self.assertNotIn(private, text + json.dumps(report))

    def test_root_source(self):
        self.assertEqual(doctor.root_source("x", None, {}), "--root")
        self.assertEqual(doctor.root_source(None, "p", {}), "--profile")
        self.assertEqual(
            doctor.root_source(None, None, {"FAMILIEKILDER_ROOT": "/x"}), "FAMILIEKILDER_ROOT"
        )
        self.assertEqual(doctor.root_source(None, None, {}), "standard")


if __name__ == "__main__":
    unittest.main()
