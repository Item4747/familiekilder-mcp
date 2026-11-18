"""A refused browser: the next one is tried when the user has not chosen, and the reason is told.

Without a choice, a browser that fails its check, or that is installed in a
way the catalog does not accept, is skipped. A browser the user chose is the
only one tried, and the error says precisely why it cannot be used. A login
server and a Mac with Brave behave as before (see test_desktop_platforms.py).
Every test runs on every platform with the platform passed in.
"""

from __future__ import annotations

import contextlib
import io
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from familiekilder import browser_bootstrap
from familiekilder import browsers
from familiekilder.browsers import BrowserBootstrapError, select_browser, survey
from familiekilder_testing.browsers import (
    FILE,
    PROGRAM_FILES,
    PROGRAM_FILES_X86,
    FakeBrowserSystem,
    stat_result,
)

HOME = "/hjem/oppdiktet"
WINDOWS_HOME = r"C:\Brukere\oppdiktet"
LOCAL = WINDOWS_HOME + r"\AppData\Local"

MAC_BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

WIN_BRAVE = PROGRAM_FILES + r"\BraveSoftware\Brave-Browser\Application\brave.exe"
WIN_CHROME = PROGRAM_FILES + r"\Google\Chrome\Application\chrome.exe"
WIN_CHROME_X86 = PROGRAM_FILES_X86 + r"\Google\Chrome\Application\chrome.exe"
WIN_EDGE = PROGRAM_FILES_X86 + r"\Microsoft\Edge\Application\msedge.exe"
USER_BRAVE = LOCAL + r"\BraveSoftware\Brave-Browser\Application\brave.exe"
USER_CHROME = LOCAL + r"\Google\Chrome\Application\chrome.exe"

LINUX_BRAVE = "/opt/brave.com/brave/brave"
LINUX_CHROME = "/opt/google/chrome/chrome"
LINUX_EDGE = "/opt/microsoft/msedge/msedge"
ROOT_FILE = stat_result(FILE, uid=0)

PER_USER_CHROME = (
    "Google Chrome er bare installert for din bruker, under AppData. Installer den for alle "
    "brukere (kjør installasjonsprogrammet som administrator), eller velg Microsoft Edge med "
    "«familiekilder-mcp browser edge»."
)


def windows(files):
    return FakeBrowserSystem(
        browsers.WINDOWS,
        files,
        home=WINDOWS_HOME,
        environ={"SystemRoot": r"C:\Windows"},
        local_app_data=LOCAL,
    )


def mac(files):
    return FakeBrowserSystem(browsers.MACOS, files, home=HOME)


def linux(files, environ=None):
    return FakeBrowserSystem(browsers.LINUX, files, home=HOME, environ=environ)


class WindowsFallbackTests(unittest.TestCase):
    def test_survey_and_login_skip_a_refused_installation_of_the_same_browser(self):
        system = windows({
            WIN_CHROME: stat_result(FILE),
            WIN_CHROME_X86: stat_result(FILE),
            WIN_EDGE: stat_result(FILE),
        })
        invalid = {"status": "HashMismatch", "subject": "Google LLC", "writers": ["S-1-5-18"]}
        system.windows_reports[WIN_CHROME] = invalid
        for choice in ("chrome", None):
            with self.subTest(choice=choice):
                row = next(row for row in survey(choice, system=system) if row.status == "selected")
                selected = select_browser(choice, system=system)
                self.assertEqual((row.browser, row.path), ("chrome", WIN_CHROME_X86))
                self.assertEqual((selected.app.id, selected.executable), (row.browser, row.path))
                self.assertEqual([item.path for item in selected.rejected], [WIN_CHROME])

        with self.assertRaisesRegex(BrowserBootstrapError, "Google Chrome har ukjent signatur"):
            select_browser("chrome", explicit=WIN_CHROME, system=system)
        system.windows_reports[WIN_CHROME_X86] = invalid
        with self.assertRaisesRegex(BrowserBootstrapError, "Google Chrome har ukjent signatur"):
            select_browser("chrome", system=system)
        self.assertEqual(select_browser(system=system).executable, WIN_EDGE)

    def test_offline_survey_checks_writers_and_defers_authenticode(self):
        system = windows({WIN_CHROME: stat_result(FILE), WIN_CHROME_X86: stat_result(FILE)})
        system.windows_reports[WIN_CHROME] = {
            "status": "Valid", "subject": "Google LLC", "writers": ["S-1-1-0"]
        }
        system.windows_reports[WIN_CHROME_X86] = {
            "status": "HashMismatch", "subject": "Google LLC", "writers": ["S-1-5-18"]
        }
        row = next(
            row for row in survey("chrome", system=system, offline=True) if row.browser == "chrome"
        )
        self.assertEqual((row.status, row.path), ("candidate", WIN_CHROME_X86))
        self.assertEqual(len(system.calls), 2)
        for call in system.calls:
            self.assertIn("Get-Acl", call[-1])
            self.assertNotIn("Get-AuthenticodeSignature", call[-1])
        with self.assertRaises(BrowserBootstrapError):
            select_browser("chrome", system=system)
        self.assertIn("Get-AuthenticodeSignature", system.calls[-1][-1])

    def test_per_user_brave_and_chrome_give_edge_and_say_why(self):
        system = windows(
            {
                USER_BRAVE: stat_result(FILE),
                USER_CHROME: stat_result(FILE),
                WIN_EDGE: stat_result(FILE),
            }
        )
        chosen = select_browser(system=system)
        self.assertEqual((chosen.app.id, chosen.executable), ("edge", WIN_EDGE))
        self.assertEqual(
            [(item.browser, item.kind, item.path) for item in chosen.rejected],
            [("brave", "per-user", USER_BRAVE), ("chrome", "per-user", USER_CHROME)],
        )
        self.assertEqual(
            chosen.rejected[1].reason,
            "Google Chrome er bare installert for din bruker, under AppData.",
        )
        # Only Edge was checked; a per-user browser is never run or checked.
        self.assertEqual(len(system.calls), 1)
        notice = chosen.notice()
        self.assertNotIn("\n", notice)
        self.assertIn("Hopper over Brave: Brave er bare installert for din bruker", notice)
        self.assertIn("Hopper over Google Chrome", notice)
        self.assertIn("Innloggingen bruker Microsoft Edge.", notice)

    def test_survey_shows_each_browser_with_its_reason_and_advice(self):
        system = windows(
            {
                USER_BRAVE: stat_result(FILE),
                USER_CHROME: stat_result(FILE),
                WIN_EDGE: stat_result(FILE),
            }
        )
        rows = {row.browser: row for row in survey(system=system)}
        self.assertEqual(
            {browser: row.status for browser, row in rows.items()},
            {"brave": "rejected", "chrome": "rejected", "edge": "selected"},
        )
        self.assertEqual(rows["edge"].path, WIN_EDGE)
        self.assertEqual(rows["chrome"].path, USER_CHROME)
        self.assertEqual(
            rows["chrome"].advice,
            "Installer den for alle brukere (kjør installasjonsprogrammet som administrator).",
        )
        # With a choice, the advice names an approved browser to choose instead.
        rows = {row.browser: row for row in survey("chrome", system=system)}
        self.assertEqual(rows["edge"].status, "approved")
        self.assertIn(
            "eller velg Microsoft Edge med «familiekilder-mcp browser edge»", rows["chrome"].advice
        )

    def test_a_chosen_browser_only_installed_per_user_gives_a_precise_error(self):
        system = windows({USER_CHROME: stat_result(FILE), WIN_EDGE: stat_result(FILE)})
        with self.assertRaises(BrowserBootstrapError) as raised:
            select_browser("chrome", system=system)
        self.assertEqual(str(raised.exception), PER_USER_CHROME)
        # The choice is never replaced, and nothing was checked or started.
        self.assertEqual(system.calls, [])
        # Edge comes with Windows, so it is named even when it is not found.
        system = windows({USER_CHROME: stat_result(FILE)})
        with self.assertRaises(BrowserBootstrapError) as raised:
            select_browser("chrome", system=system)
        self.assertEqual(str(raised.exception), PER_USER_CHROME)

    def test_brave_failing_its_signature_gives_chrome(self):
        files = {
            WIN_BRAVE: stat_result(FILE),
            WIN_CHROME: stat_result(FILE),
            WIN_EDGE: stat_result(FILE),
        }
        system = windows(files)
        system.windows_reports[WIN_BRAVE] = {"status": "NotSigned", "subject": None, "writers": []}
        chosen = select_browser(system=system)
        self.assertEqual(chosen.app.id, "chrome")
        self.assertEqual([item.kind for item in chosen.rejected], ["check"])
        self.assertEqual(chosen.rejected[0].reason, "Brave har ukjent signatur.")
        self.assertEqual(
            chosen.rejected[0].advice(), "Installer Brave på nytt fra leverandørens nettside."
        )
        self.assertEqual(len(system.calls), 2)

    def test_a_chosen_browser_failing_its_check_is_not_replaced(self):
        files = {WIN_BRAVE: stat_result(FILE), WIN_EDGE: stat_result(FILE)}
        system = windows(files)
        system.windows_reports[WIN_BRAVE] = {
            "status": "Valid",
            "subject": "Brave Software, Inc.",
            "writers": ["S-1-5-18", "S-1-5-32-545"],
        }
        with self.assertRaises(BrowserBootstrapError) as raised:
            select_browser("brave", system=system)
        self.assertEqual(
            str(raised.exception),
            "Brave ligger i en mappe som andre enn administratorer kan endre. Installer Brave på "
            "nytt for alle brukere, eller be en administrator fjerne skrivetilgangen andre har "
            "til mappen, eller velg Microsoft Edge med «familiekilder-mcp browser edge».",
        )
        self.assertEqual(len(system.calls), 1)

    def test_nothing_usable_lists_every_refusal_with_advice(self):
        system = windows({USER_BRAVE: stat_result(FILE), WIN_CHROME: stat_result(FILE)})
        system.windows_reports[WIN_CHROME] = {"status": "HashMismatch", "subject": "Google LLC"}
        with self.assertRaises(BrowserBootstrapError) as raised:
            select_browser(system=system)
        self.assertEqual(
            str(raised.exception).splitlines(),
            [
                "Fant ingen godkjent nettleser.",
                "- Brave er bare installert for din bruker, under AppData. Installer den for alle "
                "brukere (kjør installasjonsprogrammet som administrator).",
                "- Google Chrome har ukjent signatur. Installer Google Chrome på nytt fra "
                "leverandørens nettside.",
            ],
        )

    def test_no_local_app_folder_means_nothing_extra_is_looked_for(self):
        system = windows({USER_CHROME: stat_result(FILE)})
        system.local_folder = None
        with self.assertRaisesRegex(
            BrowserBootstrapError, "^Fant ingen godkjent nettleser. Installer"
        ):
            select_browser(system=system)


class MacosFallbackTests(unittest.TestCase):
    def test_brave_failing_its_signature_gives_chrome(self):
        system = mac({MAC_BRAVE: stat_result(FILE), MAC_CHROME: stat_result(FILE)})
        system.signatures["Brave Browser.app"] = (
            "Identifier=com.example.Fake\nTeamIdentifier=KL8N8XSYF4\n"
        )
        chosen = select_browser(system=system)
        self.assertEqual((chosen.app.id, chosen.executable), ("chrome", MAC_CHROME))
        self.assertEqual(chosen.rejected[0].reason, "Brave har ukjent signatur.")
        self.assertEqual(
            system.calls,
            [
                [
                    "/usr/sbin/spctl",
                    "--assess",
                    "--type",
                    "execute",
                    "/Applications/Brave Browser.app",
                ],
                ["/usr/bin/codesign", "-dv", "--verbose=4", MAC_BRAVE],
                [
                    "/usr/sbin/spctl",
                    "--assess",
                    "--type",
                    "execute",
                    "/Applications/Google Chrome.app",
                ],
                ["/usr/bin/codesign", "-dv", "--verbose=4", MAC_CHROME],
            ],
        )

    def test_brave_that_passes_is_used_as_before_without_a_notice(self):
        system = mac({MAC_BRAVE: stat_result(FILE), MAC_CHROME: stat_result(FILE)})
        chosen = select_browser(system=system)
        self.assertEqual(chosen.app.id, "brave")
        self.assertEqual(chosen.rejected, ())
        self.assertIsNone(chosen.notice())
        self.assertEqual(len(system.calls), 2)

    def test_a_chosen_browser_failing_its_check_names_another(self):
        system = mac({MAC_BRAVE: stat_result(FILE), MAC_CHROME: stat_result(FILE)})
        system.spctl = 3
        with self.assertRaises(BrowserBootstrapError) as raised:
            select_browser("brave", system=system)
        self.assertEqual(
            str(raised.exception),
            "Brave har ukjent signatur. Installer Brave på nytt fra leverandørens nettside, i "
            "Programmer-mappen, eller velg Google Chrome med «familiekilder-mcp browser chrome».",
        )
        self.assertEqual(len(system.calls), 2)

    def test_a_login_server_never_moves_on(self):
        system = mac({MAC_BRAVE: stat_result(FILE), MAC_CHROME: stat_result(FILE)})
        system.env = {"FAMILIEKILDER_HOSTED": "1"}
        system.spctl = 3
        for oauth_callback in (False, True):
            with self.subTest(oauth_callback=oauth_callback):
                system.calls.clear()
                with self.assertRaises(BrowserBootstrapError) as raised:
                    select_browser(system=system, oauth_callback=oauth_callback)
                # Exactly the message from before the catalog had advice.
                self.assertEqual(str(raised.exception), "Brave har ukjent signatur")
                self.assertEqual(len(system.calls), 2)


class LinuxFallbackTests(unittest.TestCase):
    def test_snap_and_flatpak_are_explained_and_the_package_is_used(self):
        system = linux(
            {
                "/snap/bin/brave": stat_result(stat.S_IFLNK | 0o777, uid=0),
                HOME + "/.local/share/flatpak/exports/bin/com.google.Chrome": stat_result(FILE),
                LINUX_EDGE: ROOT_FILE,
            }
        )
        chosen = select_browser(system=system)
        self.assertEqual((chosen.app.id, chosen.executable), ("edge", LINUX_EDGE))
        self.assertEqual([item.kind for item in chosen.rejected], ["snap", "flatpak"])
        self.assertEqual(
            chosen.rejected[0].explain(),
            "Brave er installert som Snap, som ikke kan kontrolleres på samme måte. Installer "
            "Brave fra leverandørens .deb- eller .rpm-pakke (brave-browser), som legger den i "
            "/opt/brave.com/brave.",
        )
        self.assertEqual(
            chosen.rejected[1].path, HOME + "/.local/share/flatpak/exports/bin/com.google.Chrome"
        )

    def test_a_browser_elsewhere_and_chromium_are_explained_when_nothing_is_usable(self):
        system = linux(
            {
                "/usr/local/bin/google-chrome": stat_result(FILE),
                "/usr/bin/chromium-browser": ROOT_FILE,
            }
        )
        with self.assertRaises(BrowserBootstrapError) as raised:
            select_browser(system=system)
        lines = str(raised.exception).splitlines()
        self.assertEqual(lines[0], "Fant ingen godkjent nettleser.")
        self.assertEqual(
            lines[1],
            "- Google Chrome ligger i /usr/local/bin/google-chrome, ikke i leverandørens "
            "pakkesti /opt/google/chrome/chrome. Installer Google Chrome fra leverandørens "
            ".deb- eller .rpm-pakke (google-chrome-stable), som legger den i /opt/google/chrome.",
        )
        self.assertEqual(
            lines[2],
            "- Chromium fra distribusjonen støttes ikke. Installer Brave, Google Chrome eller "
            "Microsoft Edge fra leverandørens .deb- eller .rpm-pakke.",
        )
        rows = {row.browser: row.status for row in survey(system=system)}
        self.assertEqual(
            rows,
            {"brave": "missing", "chrome": "rejected", "edge": "missing", "chromium": "rejected"},
        )

    def test_a_user_owned_package_moves_on_with_advice(self):
        system = linux({LINUX_BRAVE: stat_result(FILE, uid=1000), LINUX_CHROME: ROOT_FILE})
        chosen = select_browser(system=system)
        self.assertEqual(chosen.app.id, "chrome")
        self.assertEqual(
            chosen.rejected[0].explain(),
            "Brave er ikke installert som en administrert pakke. Installer Brave fra "
            "leverandørens .deb- eller .rpm-pakke (brave-browser), som legger den i "
            "/opt/brave.com/brave.",
        )

    def test_a_login_server_keeps_its_old_messages(self):
        system = linux(
            {LINUX_BRAVE: stat_result(FILE, uid=1000), "/snap/bin/brave": ROOT_FILE},
            environ={"FAMILIEKILDER_HOSTED": "1"},
        )
        with self.assertRaises(BrowserBootstrapError) as raised:
            select_browser(system=system)
        self.assertEqual(
            str(raised.exception), "Brave er ikke installert som en administrert pakke"
        )
        system = linux({"/snap/bin/brave": ROOT_FILE}, environ={"FAMILIEKILDER_HOSTED": "1"})
        with self.assertRaises(BrowserBootstrapError) as raised:
            select_browser(system=system)
        self.assertEqual(
            str(raised.exception),
            "Fant ingen godkjent nettleser. Installer Brave, Google Chrome eller Microsoft Edge "
            "fra leverandøren.",
        )


class LoginNoticeTests(unittest.TestCase):
    def test_the_login_writes_one_line_to_stderr_about_skipped_browsers(self):
        system = windows({USER_BRAVE: stat_result(FILE), WIN_EDGE: stat_result(FILE)})
        selection = select_browser(system=system)
        stderr = io.StringIO()
        stdout = io.StringIO()
        with (
            patch.object(browser_bootstrap, "select_browser", return_value=selection),
            patch("familiekilder.hosting.is_remote_host", return_value=False),
            contextlib.redirect_stderr(stderr),
            contextlib.redirect_stdout(stdout),
        ):
            path = browser_bootstrap._official_brave_path()
        self.assertEqual(path, Path(WIN_EDGE))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(),
            "Hopper over Brave: Brave er bare installert for din bruker, under AppData. "
            "Innloggingen bruker Microsoft Edge. Se «familiekilder-mcp doctor».\n",
        )

    def test_nothing_is_written_when_the_first_browser_is_used(self):
        system = mac({MAC_BRAVE: stat_result(FILE)})
        selection = select_browser(system=system)
        stderr = io.StringIO()
        with (
            patch.object(browser_bootstrap, "select_browser", return_value=selection),
            patch("familiekilder.hosting.is_remote_host", return_value=False),
            contextlib.redirect_stderr(stderr),
        ):
            browser_bootstrap._official_brave_path()
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
