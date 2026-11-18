"""The install trial follows the guide and writes a UTF-8 log.

scripts/install_trial.py runs the guide's commands word for word in CI.
"""

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, PROJECT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


install_trial = load("install_trial")
stdio_trial = load("stdio_trial")
GUIDE = (PROJECT / "docs" / "kom-i-gang.md").read_text(encoding="utf-8")


class GuideCommandTests(unittest.TestCase):
    def test_powershell_block_has_cd_setup_and_path_lines(self):
        commands = install_trial.guide_commands(GUIDE, "powershell")
        self.assertEqual(commands[0], "cd C:\\sti\\til\\familiekilder-mcp")
        self.assertIn("py scripts\\setup.py --no-open", commands)
        self.assertTrue(commands[-1].startswith("$env:Path = "))

    def test_sh_block_is_followed_by_the_setup_command(self):
        commands = install_trial.guide_commands(GUIDE, "sh")
        self.assertEqual(commands[0], "cd /sti/til/familiekilder-mcp")
        self.assertIn("scripts/bootstrap-local-venv", commands)
        self.assertTrue(any(line.startswith("export PATH=") for line in commands))
        self.assertEqual(commands[-1], "familiekilder-mcp setup --no-open")

    def test_a_changed_guide_stops_the_trial(self):
        changed = GUIDE.replace("cd /sti/til/familiekilder-mcp", "cd ~/familiekilder-mcp")
        with self.assertRaises(install_trial.DocsChanged):
            install_trial.guide_commands(changed, "sh")
        without_heading = GUIDE.replace(install_trial.INSTALL_SECTION, "### Installer")
        with self.assertRaises(install_trial.DocsChanged):
            install_trial.guide_commands(without_heading, "powershell")

    def test_only_the_placeholder_is_replaced(self):
        path = Path("/tmp") / "prøvehjem med æøå" / "familiekilder-mcp"
        line = install_trial.with_path("cd /sti/til/familiekilder-mcp", "sh", path)
        self.assertEqual(line, f"cd '{path}'")

    def test_each_command_gets_a_marker_and_a_failure_check(self):
        script = install_trial.shell_script(
            ["cd 'x'", "py scripts\\setup.py --no-open"], "powershell"
        )
        self.assertIn(
            "Write-Output '@@step 2'\r\n$global:LASTEXITCODE = $null\r\npy scripts\\setup.py --no-open",
            script,
        )
        self.assertEqual(script.count("exit $LASTEXITCODE"), 2)
        posix = install_trial.shell_script(["cd 'x'", "scripts/bootstrap-local-venv"], "sh")
        self.assertTrue(posix.startswith("set -e\n"))
        self.assertIn("echo '@@step 2'\nscripts/bootstrap-local-venv\n", posix)


class TrialLogTests(unittest.TestCase):
    def test_log_is_utf8_with_a_legacy_console_encoding(self):
        result = subprocess.run(
            [sys.executable, str(PROJECT / "scripts" / "install_trial.py"), "--help"],
            env={**os.environ, "PYTHONIOENCODING": "cp1252"},
            capture_output=True,
            check=True,
            timeout=10,
        )
        self.assertIn(
            "Prøv installasjonen slik veiledningen beskriver den.", result.stdout.decode("utf-8")
        )


class StdioTrialTests(unittest.TestCase):
    def test_codepage_command_keeps_chcp_output_off_stdout(self):
        command = stdio_trial.windows_codepage_command(
            "C:\\a b\\python.exe", ["-I", "-m", "familiekilder", "--root", "C:\\æ ø"], "850"
        )
        self.assertIn('/d /s /c "chcp 850 1>&2 & "C:\\a b\\python.exe" "-I"', command)
        self.assertTrue(command.endswith('"C:\\æ ø""'))

    def test_raw_form_tells_utf8_from_escapes(self):
        self.assertEqual(stdio_trial.raw_form('{"t":"Ørsta"}'.encode("utf-8"), "Ørsta"), "UTF-8")
        self.assertEqual(stdio_trial.raw_form(b'{"t":"\\u00d8rsta"}', "Ørsta"), "\\u-escaper")


if __name__ == "__main__":
    unittest.main()
