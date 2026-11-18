"""Install the local package and open its browser-based setup, on any platform.

    macOS and Linux:  python3 scripts/setup.py
    Windows:          py scripts\\setup.py

Does what scripts/setup does, without a shell: creates .private/venv with
the versions pinned in pyproject.toml, installs this folder in it and starts
`familiekilder-mcp setup`. Standard library only, so any Python 3.11 or newer
can run it. Run it again after pulling new code.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import venv
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PRIVATE = PROJECT / ".private"
VENV = PRIVATE / "venv"
RESTRICT_PRIVATE = """
import sys
from familiekilder.windows_acl import restrict

restrict(sys.argv[1])
"""


def protect_output() -> None:
    """Escape what a legacy console encoding cannot show, instead of failing.

    A redirected run on Windows writes cp1252, and a folder name can hold any
    character. familiekilder.console does the same for the installed commands;
    this script runs before the package is installed and cannot import it.
    """

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass


def fail(message: str) -> None:
    print(f"familiekilder-mcp-oppsett: {message}", file=sys.stderr)
    raise SystemExit(2)


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def real_directory(path: Path, name: str) -> None:
    """A real folder, never a link: .private also holds sessions and tokens."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    attributes = getattr(metadata, "st_file_attributes", 0)
    if stat.S_ISLNK(metadata.st_mode) or attributes & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
    ):
        fail(f"{name} kan ikke være en symbolsk lenke")
    if not stat.S_ISDIR(metadata.st_mode):
        fail(f"{name} må være en mappe")


def main() -> int:
    protect_output()
    if sys.version_info < (3, 11):
        fail("Python 3.11 eller nyere kreves")
    os.umask(0o077)
    real_directory(PRIVATE, ".private")
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    if os.name != "nt":
        PRIVATE.chmod(0o700)
    real_directory(VENV, ".private/venv")
    python = venv_python()
    if not python.exists():
        try:
            venv.EnvBuilder(with_pip=True).create(VENV)
        except Exception:  # noqa: BLE001 - one fixed message for the user
            fail("det lokale Python-miljøet kunne ikke opprettes")
    if not python.exists():
        fail("det lokale Python-miljøet er ufullstendig")
    installed = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--editable",
            str(PROJECT),
        ],
        check=False,
    )
    if installed.returncode != 0:
        fail("familiekilder-mcp og de låste avhengighetene kunne ikke installeres")
    checked = subprocess.run(
        [str(python), "-I", "-c", "import familiekilder.source_cli"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if checked.returncode != 0:
        fail("familiekilder kan ikke importeres i det nye miljøet")
    if os.name == "nt":
        # Windows ignores the mode; give .private the user-only access list.
        restricted = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                RESTRICT_PRIVATE,
                str(PRIVATE),
            ],
            check=False,
        )
        if restricted.returncode != 0:
            fail(".private kunne ikke sikres")
    print(f"familiekilder-mcp er klart i {VENV}")
    command = [str(python), "-I", "-m", "familiekilder", "setup", *sys.argv[1:]]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
