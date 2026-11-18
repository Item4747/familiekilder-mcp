"""Every command keeps working on a console without UTF-8.

Windows writes a redirected command's output in cp1252, and programs that
read the console's code page often expect cp850. The subprocess tests run
each command once with the platform's own setting (cp1252 on Windows CI, UTF-8
elsewhere) and once each with cp850 and cp1252 put in place, always without
Python's UTF-8 mode.
"""

from __future__ import annotations

import io
import json
import locale
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from familiekilder.console import json_text, protect_console
from familiekilder.source_cli import build_parser

# Characters cp850 or cp1252 lack, next to ones both have.
SAMPLE = "Æ ø å «sitat» – tankestrek … og feiring 🎉 𝄞"
LEGACY = ("cp850", "cp1252")


def _stream(encoding: str) -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="\n")


def _written(stream: io.TextIOWrapper) -> bytes:
    stream.flush()
    return stream.buffer.getvalue()


class ProtectConsoleTests(unittest.TestCase):
    def test_a_legacy_stream_escapes_what_it_cannot_show(self) -> None:
        for encoding in LEGACY:
            with self.subTest(encoding=encoding):
                stream = _stream(encoding)
                with self.assertRaises(UnicodeEncodeError):
                    print(SAMPLE, file=stream)
                stream = _stream(encoding)
                protect_console(stream)
                print(SAMPLE, file=stream)
                text = _written(stream).decode(encoding)
                self.assertIn("Æ ø å «sitat»", text)
                self.assertIn("\\U0001f389", text)
                self.assertEqual(stream.encoding, encoding)

    def test_a_utf8_stream_is_left_alone(self) -> None:
        stream = _stream("utf-8")
        protect_console(stream)
        self.assertEqual(stream.errors, "strict")
        print(SAMPLE, file=stream)
        self.assertEqual(_written(stream).decode("utf-8"), SAMPLE + "\n")

    def test_json_stays_valid_and_unchanged_on_utf8(self) -> None:
        value = {"tekst": SAMPLE, "tall": 3}
        old = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        self.assertEqual(json_text(value, _stream("utf-8"), indent=2, sort_keys=True), old)
        for encoding in LEGACY:
            with self.subTest(encoding=encoding):
                stream = _stream(encoding)
                protect_console(stream)
                print(json_text(value, stream, indent=2, sort_keys=True), file=stream)
                self.assertEqual(json.loads(_written(stream).decode(encoding)), value)
        plain = {"tekst": "Æ ø å «sitat»"}
        self.assertEqual(json_text(plain, _stream("cp850")), json.dumps(plain, ensure_ascii=False))


def _commands(parser, prefix=()):
    """Every command path in the parser, such as ("auth", "spond", "status")."""

    found = []
    for action in parser._actions:  # noqa: SLF001 - argparse has no public walk
        choices = getattr(action, "choices", None)
        if not isinstance(choices, dict):
            continue
        for name, child in choices.items():
            # auth takes the source first; one source shows every auth command.
            path = (*prefix, "spond", name) if prefix == ("auth",) else (*prefix, name)
            found.append(path)
            found.extend(_commands(child, path))
    return found


def _environment(home: Path, encoding: str | None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONUTF8", "PYTHONIOENCODING", "FAMILIEKILDER_ROOT"}
    }
    environment.update(HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home))
    if encoding:
        environment["PYTHONIOENCODING"] = encoding
    return environment


class CommandOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        # A data root whose name the console cannot show in full.
        self.home = Path(directory.name)
        self.root = self.home / "rot – 🎉"

    def _run(self, encoding: str | None, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-X", "utf8=0", "-m", "familiekilder", *arguments],
            env=_environment(self.home, encoding),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
            check=False,
        )

    def _check(self, result: subprocess.CompletedProcess, label: str) -> None:
        for name, output in (("stdout", result.stdout), ("stderr", result.stderr)):
            self.assertNotIn(b"Traceback", output, f"{label} {name}")
            self.assertNotIn(b"codec can't encode", output, f"{label} {name}")

    def test_help_of_every_command(self) -> None:
        commands = _commands(build_parser())
        self.assertIn(("auth", "spond", "browser-login"), commands)
        for encoding in (None, *LEGACY):
            for command in [(), *commands]:
                with self.subTest(encoding=encoding, command=command):
                    result = self._run(encoding, *command, "--help")
                    self._check(result, " ".join(command))
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_commands_that_print_paths_json_and_errors(self) -> None:
        root = str(self.root)
        runs = [
            (0, "--root", root, "configure", "spond"),
            (0, "--root", root, "connections"),
            (0, "--root", root, "browser"),
            (0, "--root", root, "auth", "spond", "status"),
            (0, "--root", root, "password", "status", "bankid"),
            (2, "--root", root, "auth", "vigilo", "status"),
            (2, "--root", root, "ist-auth", "status"),
        ]
        for encoding in (None, *LEGACY):
            for expected, *arguments in runs:
                with self.subTest(encoding=encoding, command=arguments[2:]):
                    result = self._run(encoding, *arguments)
                    self._check(result, " ".join(arguments[2:]))
                    self.assertEqual(result.returncode, expected, result.stderr)
                    if expected == 0:
                        json.loads(result.stdout.decode(encoding or locale.getencoding()))

    def test_a_path_the_console_cannot_show_is_still_printed(self) -> None:
        # remote init prints the new file's path and where the owner code is.
        # Before protect_console, cp1252 stopped it after the file was made,
        # so the owner code was never shown.
        # On Windows the owner code file must be under a .private folder.
        for encoding in LEGACY:
            with self.subTest(encoding=encoding):
                folder = self.home / encoding / ".private" / "miljø – 🎉"
                folder.mkdir(parents=True, mode=0o700)
                result = self._run(
                    encoding,
                    "--root",
                    str(self.root),
                    "remote",
                    "init",
                    "--env-file",
                    str(folder / "vert.env"),
                    "--public-url",
                    "https://vert.example",
                )
                self._check(result, "remote init")
                self.assertEqual(result.returncode, 0, result.stderr)
                output = result.stdout.decode(encoding)
                self.assertIn("Miljøfilen er laget", output)
                self.assertIn("\\U0001f389", output)


if __name__ == "__main__":
    unittest.main()
