"""Install the project the way the guide tells a new user, then use it as a client does.

    python scripts/install_trial.py            from the project folder
    python scripts/install_trial.py --keep     keep the trial folder afterwards

Standard library only: it runs before the package is installed. Nothing here
logs in anywhere; the only network traffic is pip installing the pinned
dependencies.

A. Installation
   1. Makes an empty, temporary home folder whose path has spaces and æøå,
      and copies this project into a folder with spaces and æøå under it,
      as if the user had unpacked the ZIP file in "Nedlastinger".
   2. Builds a minimal environment from scratch: HOME, USERPROFILE, APPDATA,
      LOCALAPPDATA and XDG_* point into the trial home, PATH is kept, and
      PYTHONUTF8, PYTHONIOENCODING, PYTHONPATH, PYTHONHOME and FAMILIEKILDER_*
      are gone.
   3. Takes the commands word for word from the code blocks in
      docs/kom-i-gang.md: the PowerShell block on Windows, run in Windows
      PowerShell 5.1; the sh block on macOS and Linux, run with sh and
      followed by `familiekilder-mcp setup` from "Oppsett i nettleseren".
      Only the placeholder path in the cd line is replaced (quoted, since it
      has spaces) and `--no-open` is added to the setup command. When the
      guide changes, the trial follows it or stops with a clear message.
   4. Drives the setup wizard over HTTP like its page does: reads the token,
      checks the version and the services, connects Claude Desktop and Codex,
      runs the MCP connection test and closes the wizard.
   5. Runs POST_SETUP_COMMANDS in the same shell after the PATH line.

B. The server as a client starts it: see scripts/stdio_trial.py.

The summary lists each step with its duration. Output never shows the real
home folder and survives a cp1252 or cp850 console.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

PROJECT = Path(__file__).resolve().parents[1]
GUIDE = Path("docs") / "kom-i-gang.md"

INSTALL_SECTION = "### Last ned og installer"
SETUP_SECTION = "## Oppsett i nettleseren"
PLACEHOLDERS = {
    "sh": "/sti/til/familiekilder-mcp",
    "powershell": "C:\\sti\\til\\familiekilder-mcp",
}
SETUP_COMMAND = "familiekilder-mcp setup"
NO_OPEN = "--no-open"

# Run in the same shell after the guide's commands, so the PATH line applies.
POST_SETUP_COMMANDS = [
    "familiekilder-mcp --version",
]

# The preflight report a tester pastes into an issue, as it looks right after
# the install. It is shown, not judged: it exits 1 when, say, the machine has
# no screen, and the platform job checks the report on its own.
# On Windows the log reader expects UTF-8. Set it only for this report, so
# setup and the separate stdio trial still use the user's default encoding.
REPORT_COMMAND = {
    "powershell": (
        "$env:PYTHONIOENCODING = 'utf-8'; familiekilder-mcp doctor; "
        "Remove-Item Env:PYTHONIOENCODING; $global:LASTEXITCODE = 0"
    ),
    "sh": "familiekilder-mcp doctor || true",
}

HOME_NAME = "prøvehjem med æøå"
UNPACKED = Path("Nedlastinger") / "familiekilder-mcp med æøå"
COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".private", "__pycache__", "dist", "build", "*.egg-info", ".venv", ".pytest_cache"
)
DROPPED_VARIABLES = {"PYTHONUTF8", "PYTHONIOENCODING", "PYTHONPATH", "PYTHONHOME"}
URL = re.compile(rb"http://127\.0\.0\.1:(\d+)")

INSTALL_SECONDS = 10 * 60
CHECK_SECONDS = 180
SHELL_END_SECONDS = 5 * 60


class DocsChanged(Exception):
    """docs/kom-i-gang.md no longer has the commands this trial expects."""


class TrialFailure(Exception):
    """A failed check, with a Norwegian message for the summary."""


# --- The guide ---------------------------------------------------------------


def section(markdown: str, heading: str) -> str:
    lines = markdown.splitlines()
    try:
        start = lines.index(heading)
    except ValueError:
        raise DocsChanged(f"overskriften «{heading}» finnes ikke") from None
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        hashes = len(lines[index]) - len(lines[index].lstrip("#"))
        if 0 < hashes <= level and lines[index][hashes : hashes + 1] == " ":
            end = index
            break
    return "\n".join(lines[start + 1 : end])


def code_blocks(text: str, language: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if current is None and line.strip() == f"```{language}":
            current = []
        elif current is not None and line.strip() == "```":
            blocks.append(current)
            current = None
        elif current is not None:
            current.append(line)
    return blocks


def guide_commands(markdown: str, shell: str) -> list[str]:
    """The guide's install commands for `shell` ("sh" or "powershell"), word for word.

    The first command is the cd line with the placeholder path. The setup
    command gets NO_OPEN; on macOS and Linux it is the command from the
    browser setup section, after the block.
    """

    blocks = code_blocks(section(markdown, INSTALL_SECTION), shell)
    if len(blocks) != 1:
        raise DocsChanged(f"«{INSTALL_SECTION}» har {len(blocks)} {shell}-blokker, ventet 1")
    commands = [line.strip() for line in blocks[0] if line.strip()]
    if not commands or not commands[0].startswith("cd ") or PLACEHOLDERS[shell] not in commands[0]:
        raise DocsChanged(f"{shell}-blokken begynner ikke med cd {PLACEHOLDERS[shell]}")
    if shell == "powershell":
        setup = [index for index, line in enumerate(commands) if "scripts\\setup.py" in line]
        if len(setup) != 1:
            raise DocsChanged("powershell-blokken har ikke nøyaktig én linje med scripts\\setup.py")
        commands[setup[0]] += f" {NO_OPEN}"
    else:
        browser_setup = section(markdown, SETUP_SECTION)
        if f"`{SETUP_COMMAND}`" not in browser_setup:
            raise DocsChanged(f"«{SETUP_SECTION}» nevner ikke `{SETUP_COMMAND}`")
        commands.append(f"{SETUP_COMMAND} {NO_OPEN}")
    return commands


def with_path(command: str, shell: str, path: Path) -> str:
    """Put the real path in the cd line, quoted because it has spaces."""

    text = str(path)
    if "'" in text:
        raise TrialFailure("prøvestien kan ikke inneholde apostrof")
    return command.replace(PLACEHOLDERS[shell], f"'{text}'")


def shell_script(commands: list[str], shell: str) -> str:
    """The commands with a marker before each; stops at the first failure."""

    if shell == "powershell":
        # A program sets $LASTEXITCODE; judge it by that alone, since Windows
        # PowerShell 5.1 can turn $? false when a program only writes to
        # stderr. A cmdlet such as cd leaves it unset and is judged by $?.
        lines = []
        for number, command in enumerate(commands, start=1):
            lines += [
                f"Write-Output '@@step {number}'",
                "$global:LASTEXITCODE = $null",
                command,
                "$trialOk = $?",
                "if ($null -ne $LASTEXITCODE) {",
                '    if ($LASTEXITCODE -ne 0) { Write-Output "@@failed $LASTEXITCODE"; exit $LASTEXITCODE }',
                "} elseif (-not $trialOk) {",
                "    Write-Output '@@failed 1'; exit 1",
                "}",
            ]
        return "\r\n".join([*lines, "Write-Output '@@done'", "exit 0", ""])
    lines = ["set -e", "trap 'code=$?; [ \"$code\" = 0 ] || echo \"@@failed $code\"' EXIT"]
    for number, command in enumerate(commands, start=1):
        lines += [f"echo '@@step {number}'", command]
    return "\n".join([*lines, "echo '@@done'", ""])


# --- Trial bookkeeping -------------------------------------------------------


@dataclass
class Step:
    name: str
    seconds: float
    ok: bool
    detail: str = ""


class Trial:
    def __init__(self, base: Path, keep: bool) -> None:
        self.base = base
        self.keep = keep
        self.home = base / HOME_NAME
        self.project = self.home / UNPACKED
        self.windows = os.name == "nt"
        self.env: dict[str, str] = {}
        self.steps: list[Step] = []
        self.shell_steps: list[Step] = []
        self.shell_tail: list[str] = []
        self.notes: list[str] = []
        self.real_home = str(Path.home())

    def redact(self, text: str) -> str:
        for path, name in (
            (str(self.home), "<prøvehjem>"),
            (str(self.base), "<prøvemappe>"),
            (self.real_home, "<hjemmemappe>"),
        ):
            for variant in (path, path.replace("\\", "\\\\"), path.replace("\\", "/")):
                text = text.replace(variant, name)
        return text

    def log(self, text: str) -> None:
        print(self.redact(text), flush=True)

    def note(self, text: str) -> None:
        """Log a finding and repeat it in the summary."""

        self.log(text)
        self.notes.append(self.redact(text))

    @contextlib.contextmanager
    def step(self, name: str) -> Iterator[None]:
        self.log(f"\n== {name}")
        started = time.monotonic()
        try:
            yield
        except Exception as error:
            message = str(error) if isinstance(error, (TrialFailure, DocsChanged)) else repr(error)
            self.steps.append(Step(name, time.monotonic() - started, False, self.redact(message)))
            raise
        self.steps.append(Step(name, time.monotonic() - started, True))

    def run_checked(self, argv: list[str], label: str, *, timeout: float = 300) -> str:
        """Run one command in the trial environment; its output, or a failure."""

        result = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=self.env,
            cwd=self.home,
            timeout=timeout,
            check=False,
        )
        output = result.stdout.decode("utf-8", "backslashreplace")
        for line in output.splitlines():
            self.log(f"  | {line}")
        if result.returncode != 0:
            raise TrialFailure(f"{label} avsluttet med {result.returncode}")
        return output


def minimal_environment(home: Path) -> dict[str, str]:
    """A fresh user's environment: system variables and PATH, the trial home folder."""

    keep = {"PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM", "USER", "LOGNAME", "SHELL"}
    if os.name == "nt":
        keep |= {
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "OS",
            "USERNAME",
            "USERDOMAIN",
            "COMPUTERNAME",
            "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE",
            "PROCESSOR_IDENTIFIER",
            "PROCESSOR_LEVEL",
            "PROCESSOR_REVISION",
            "PROGRAMDATA",
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "PROGRAMW6432",
            "COMMONPROGRAMFILES",
            "COMMONPROGRAMFILES(X86)",
            "COMMONPROGRAMW6432",
            "ALLUSERSPROFILE",
            "PUBLIC",
        }
    env = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in keep
        and name.upper() not in DROPPED_VARIABLES
        and not name.upper().startswith("FAMILIEKILDER_")
    }
    if os.name == "nt":
        roaming = home / "AppData" / "Roaming"
        local = home / "AppData" / "Local"
        temp = local / "Temp"
        for folder in (roaming, local, temp):
            folder.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "USERPROFILE": str(home),
                "HOMEDRIVE": home.drive,
                "HOMEPATH": str(home)[len(home.drive) :],
                "APPDATA": str(roaming),
                "LOCALAPPDATA": str(local),
                "TEMP": str(temp),
                "TMP": str(temp),
            }
        )
    else:
        temp = home / "tmp"
        temp.mkdir(mode=0o700, exist_ok=True)
        env.update(
            {
                "HOME": str(home),
                "TMPDIR": str(temp),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
            }
        )
    return env


# --- The shell ---------------------------------------------------------------


class ShellRun:
    """The guide's commands in one shell; collects output lines and step markers."""

    def __init__(self, trial: Trial, commands: list[str], script: Path) -> None:
        self.trial = trial
        self.commands = commands
        self.lines: list[bytes] = []
        self.marks: list[tuple[int, float]] = []
        self.failed: int | None = None
        self.done = False
        self.url: queue.Queue[str] = queue.Queue()
        if trial.windows:
            powershell = Path(os.environ.get("SYSTEMROOT", "C:\\Windows")) / (
                "System32/WindowsPowerShell/v1.0/powershell.exe"
            )
            argv = [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ]
            options: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            argv = ["sh", str(script)]
            options = {"start_new_session": True}
        self.started = time.monotonic()
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=trial.env,
            cwd=trial.home,
            **options,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        for raw in self.process.stdout:
            line = raw.rstrip(b"\r\n")
            self.lines.append(line)
            text = line.decode("utf-8", "backslashreplace")
            if text.startswith("@@step "):
                number = int(text.split()[1])
                self.marks.append((number, time.monotonic()))
                self.trial.log(f"  > {self.commands[number - 1]}")
                continue
            if text.startswith("@@failed "):
                self.failed = int(text.split()[1])
                continue
            if text == "@@done":
                self.done = True
                continue
            self.trial.log(f"  | {text}")
            match = URL.search(line)
            if match:
                self.url.put(match.group(0).decode("ascii"))

    def wait_for_url(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return self.url.get(timeout=1)
            except queue.Empty:
                if self.process.poll() is not None and not self.reader.is_alive():
                    raise TrialFailure(
                        f"skallet avsluttet med {self.process.returncode} før veiviseren viste adressen"
                    ) from None
        raise TrialFailure(f"veiviseren viste ingen adresse innen {timeout:.0f} s")

    def wait(self, timeout: float) -> int:
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TrialFailure(f"skallet ble ikke ferdig innen {timeout:.0f} s") from None
        self.reader.join(timeout=30)
        return code

    def stop(self) -> None:
        """Stop the shell and everything it started (the wizard, pip)."""

        if self.process.poll() is not None:
            return
        if self.trial.windows:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(self.process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=20)

    def output_for(self, number: int) -> list[str]:
        """Output lines between the marker of command `number` and the next marker."""

        result: list[str] = []
        inside = False
        for line in self.lines:
            text = line.decode("utf-8", "backslashreplace")
            if text.startswith("@@"):
                inside = text == f"@@step {number}"
                continue
            if inside:
                result.append(text)
        return result

    def timings(self, end: float) -> list[Step]:
        steps = []
        for index, (number, started) in enumerate(self.marks):
            finished = self.marks[index + 1][1] if index + 1 < len(self.marks) else end
            last = index + 1 == len(self.marks)
            ok = not (last and (self.failed is not None or not self.done))
            steps.append(Step(self.commands[number - 1], finished - started, ok))
        return steps


# --- The wizard --------------------------------------------------------------


class Wizard:
    """The setup page's requests: same Host, Origin and token as the browser."""

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.token = ""
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _send(self, request: urllib.request.Request) -> tuple[int, bytes]:
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            raise TrialFailure(
                f"{request.get_method()} {request.selector}: HTTP {error.code} {body}"
            ) from None

    def page(self) -> None:
        _, body = self._send(urllib.request.Request(self.origin + "/"))
        match = re.search(rb'<meta name="setup-token" content="([^"]+)"', body)
        if not match:
            raise TrialFailure("siden har ingen oppsettsnøkkel")
        self.token = match.group(1).decode("ascii")

    def state(self) -> dict[str, Any]:
        request = urllib.request.Request(
            self.origin + "/api/state", headers={"X-Setup-Token": self.token}
        )
        return json.loads(self._send(request)[1])

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.origin + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "X-Setup-Token": self.token,
                "Origin": self.origin,
                "Content-Type": "application/json",
            },
        )
        return json.loads(self._send(request)[1])


def drive_wizard(trial: Trial, wizard: Wizard, venv_python: Path) -> str:
    """Steps A4: the version the wizard reports."""

    with trial.step("Veiviseren: hent siden og nøkkelen"):
        wizard.page()
    with trial.step("Veiviseren: versjon og tjenester"):
        state = wizard.state()
        expected_version = tomllib.loads(
            (trial.project / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        if state.get("version") != expected_version:
            raise TrialFailure(
                f"veiviseren viser versjon {state.get('version')}, ventet {expected_version}"
            )
        registry = json.loads(
            trial.run_checked(
                [
                    str(venv_python),
                    "-I",
                    "-c",
                    "import json; from familiekilder.sources import REGISTRY; print(json.dumps(sorted(REGISTRY)))",
                ],
                "les tjenestelisten",
            )
            .strip()
            .splitlines()[-1]
        )
        shown = {row["id"]: row for row in state.get("services", [])}
        missing = sorted(set(registry) - set(shown))
        if missing:
            raise TrialFailure(f"veiviseren mangler tjenestene {', '.join(missing)}")
        broken = sorted(name for name, row in shown.items() if row.get("state") == "unavailable")
        if broken:
            raise TrialFailure(f"veiviseren kan ikke lese status for {', '.join(broken)}")
        trial.log(
            f"versjon {state['version']}; tjenester: "
            + ", ".join(f"{name} ({row['state']})" for name, row in sorted(shown.items()))
        )
        trial.log(
            f"nettlesere: {state.get('browsers')}; avhengigheter: {state.get('dependencies')}"
        )
    for client in ("claude", "codex"):
        with trial.step(f"Veiviseren: koble til {client}"):
            wizard.post("/api/connect", {"client": client})
            rows = {row["id"]: row for row in wizard.state()["clients"]}
            if not rows[client]["configured"] or rows[client]["error"]:
                raise TrialFailure(f"{client} står ikke som koblet til: {rows[client]}")
    with trial.step("Veiviseren: test MCP-forbindelsen"):
        wizard.post("/api/start", {"action": "check"})
        deadline = time.monotonic() + CHECK_SECONDS
        while True:
            state = wizard.state()
            job = state.get("job") or {}
            if job.get("phase") not in {"working", "waiting"}:
                break
            if time.monotonic() > deadline:
                raise TrialFailure(f"testen ble ikke ferdig innen {CHECK_SECONDS} s")
            time.sleep(0.5)
        trial.log(f"testen: {job.get('phase')}: {job.get('message')}")
        if job.get("phase") != "done" or state.get("tested") is not True:
            raise TrialFailure(f"MCP-testen feilet: {job.get('message')}")
    with trial.step("Veiviseren: avslutt oppsett"):
        wizard.post("/api/close", {})
    return state["version"]


# --- Running it --------------------------------------------------------------


def interpreter_report(trial: Trial) -> None:
    probe = "import sys; print(sys.version.split()[0], sys.executable)"
    if trial.windows:
        with contextlib.suppress(TrialFailure, OSError, subprocess.SubprocessError):
            trial.run_checked(["py", "-0p"], "py -0p", timeout=60)
        argv = ["py", "-c", probe]
    else:
        argv = ["python3", "-c", probe]
    output = trial.run_checked(argv, " ".join(argv[:1]), timeout=60).strip().splitlines()
    version = output[-1].split()[0] if output else "?"
    trial.note(f"{argv[0]} er Python {version}")


def remove_tree(path: Path) -> None:
    def retry(function, target, _info) -> None:
        with contextlib.suppress(OSError):
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def run(trial: Trial) -> None:
    shell = "powershell" if trial.windows else "sh"
    with trial.step(f"Finn {shell}-kommandoene i {GUIDE.as_posix()}"):
        commands = guide_commands((PROJECT / GUIDE).read_text(encoding="utf-8"), shell)
    with trial.step("Lag prøvehjem og pakk ut prosjektet"):
        trial.home.mkdir(mode=0o700)
        trial.project.parent.mkdir(parents=True)
        shutil.copytree(PROJECT, trial.project, ignore=COPY_IGNORE)
        trial.env = minimal_environment(trial.home)
        trial.log(f"prosjektet: {trial.project}")
    with trial.step("Python som veiledningen bruker"):
        interpreter_report(trial)

    commands = [
        with_path(commands[0], shell, trial.project),
        *commands[1:],
        *POST_SETUP_COMMANDS,
        REPORT_COMMAND[shell],
    ]
    script = trial.home / ("prøve.ps1" if trial.windows else "prøve.sh")
    content = shell_script(commands, shell)
    # Windows PowerShell 5.1 reads a script without BOM as ANSI.
    script.write_text(content, encoding="utf-8-sig" if trial.windows else "utf-8")
    trial.log(
        "\nKommandoene:\n"
        + "\n".join(f"  {number}. {command}" for number, command in enumerate(commands, start=1))
    )
    venv = trial.project / ".private" / "venv"
    venv_python = venv / ("Scripts/python.exe" if trial.windows else "bin/python")

    shell_run = ShellRun(trial, commands, script)
    wizard: Wizard | None = None
    try:
        with trial.step("Installer og start veiviseren"):
            origin = shell_run.wait_for_url(INSTALL_SECONDS)
            trial.log(f"veiviseren: {origin}")
        wizard = Wizard(origin)
        version = drive_wizard(trial, wizard, venv_python)
        wizard = None
        with trial.step("Kommandoene etter oppsettet"):
            code = shell_run.wait(SHELL_END_SECONDS)
            if code != 0 or not shell_run.done:
                raise TrialFailure(f"skallet avsluttet med {code}")
            version_line = shell_run.output_for(commands.index("familiekilder-mcp --version") + 1)
            if not any(line.strip() == f"familiekilder-mcp {version}" for line in version_line):
                raise TrialFailure(f"familiekilder-mcp --version viste {version_line!r}")
    finally:
        if wizard is not None:
            with contextlib.suppress(Exception):
                wizard.post("/api/close", {})
        shell_run.stop()
        trial.shell_steps = shell_run.timings(time.monotonic())
        trial.shell_tail = [
            line.decode("utf-8", "backslashreplace") for line in shell_run.lines[-30:]
        ]

    stdio_trial = load_stdio_trial()
    try:
        stdio_trial.run(trial)
    except stdio_trial.TrialFailure as error:
        raise TrialFailure(str(error)) from None


def load_stdio_trial():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "stdio_trial", Path(__file__).with_name("stdio_trial.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def summary(trial: Trial, ok: bool, seconds: float) -> None:
    trial.log("\n================ Oversikt ================")
    trial.log(f"System: {platform.platform()} ({sys.platform})")
    trial.log(f"Prøveskriptets Python: {sys.version.split()[0]}")
    if trial.notes:
        trial.log("Funn:")
        for note in trial.notes:
            trial.log(f"  {note}")
    trial.log("Kommandoene i skallet:")
    for step in trial.shell_steps:
        trial.log(f"  {'OK  ' if step.ok else 'FEIL'} {step.seconds:7.1f} s  {step.name}")
    trial.log("Stegene:")
    for step in trial.steps:
        trial.log(f"  {'OK  ' if step.ok else 'FEIL'} {step.seconds:7.1f} s  {step.name}")
        if step.detail:
            trial.log(f"         {step.detail}")
    if not ok and trial.shell_tail:
        trial.log("Siste utdata fra skallet:")
        for line in trial.shell_tail:
            trial.log(f"  | {line}")
    trial.log(f"Resultat: {'bestått' if ok else 'FEILET'} på {seconds:.0f} s")


def main(argv: list[str] | None = None) -> int:
    # CI reads the trial log as UTF-8. The child processes still get the
    # environment of a fresh user, independent of the trial's own log.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(
        description="Prøv installasjonen slik veiledningen beskriver den."
    )
    parser.add_argument("--keep", action="store_true", help="behold prøvemappen etterpå")
    parser.add_argument(
        "--keep-on-failure", action="store_true", help="behold prøvemappen hvis prøven feiler"
    )
    parser.add_argument(
        "--base", type=Path, help="mappe prøvemappen lages i (standard: systemets midlertidige)"
    )
    options = parser.parse_args(argv)

    # Resolved, so the shell's pwd, sys.executable and the trial agree on one spelling.
    base = Path(tempfile.mkdtemp(prefix="fk-", dir=options.base)).resolve()
    trial = Trial(base, options.keep)
    started = time.monotonic()
    ok = False
    try:
        run(trial)
        ok = True
    except (TrialFailure, DocsChanged) as error:
        trial.log(f"\nFEIL: {error}")
    except Exception:  # noqa: BLE001 - report everything, then fail
        import traceback

        trial.log("\nUventet feil:\n" + traceback.format_exc())
    finally:
        summary(trial, ok, time.monotonic() - started)
        if options.keep or (options.keep_on_failure and not ok):
            # Only the real home folder is hidden, so the folder can be found.
            print(f"Prøvemappen er beholdt: {str(base).replace(trial.real_home, '~')}", flush=True)
        else:
            remove_tree(base)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
