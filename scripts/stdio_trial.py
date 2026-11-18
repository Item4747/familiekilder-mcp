"""Start the installed MCP server the way an AI client does, with a fake Spond.

Part of scripts/install_trial.py, which runs it after the setup wizard has
written the client configurations. Standard library only.

1. Reads the server command from the Claude Desktop configuration the wizard
   wrote in the trial home folder, and checks that the Codex configuration has
   the same command.
2. Puts a fake Spond into the trial's own environment (the seam, below),
   stores the built-in Spond setup and a fictional session, and turns Spond on
   with `familiekilder-mcp configure spond`.
3. Starts the server with exactly that command, stdin and stdout as binary
   pipes and an environment without PYTHONUTF8 or PYTHONIOENCODING, and sends
   raw JSON-RPC: initialize, notifications/initialized, tools/list,
   family_connections, spond_list_items, spond_get_item and
   spond_read_attachment. Every line from the server must be valid UTF-8 and
   valid JSON, and the fictional texts must come back identical.
4. On Windows it does the same with the console code page set to 850 and
   1252 first (`chcp`).

The seam
--------
The installed package has no switch for fake sources. The trial writes two
files into site-packages of the copied project's `.private/venv`, which the
trial created and deletes afterwards; never into another environment:

- `familiekilder_trial.pth`, one line: `import familiekilder_trial_activate`.
  Python runs it at every start of that environment, also with `-I`.
- `familiekilder_trial_activate.py`, which puts the copied project's
  `tests/support` on sys.path and calls
  `familiekilder_testing.installed_trial.activate(<fixture file>)`.

`activate()` blocks every name lookup and connection that does not stay on
this machine, and replaces `_default_transport` in `familiekilder.spond` and
`familiekilder.spond_auth` with the shared fake transports in
`tests/support/familiekilder_testing/`. Everything else is the installed
code: the session check, the Spond reader, pacing, the read cache, the
attachment download and text extraction, and the MCP tools.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVER_NAME = "familiekilder-mcp"
START_FILE = "familiekilder_trial.pth"
ACTIVATE_MODULE = "familiekilder_trial_activate"

# Markers from familiekilder_testing.installed_trial, on the server's stderr.
ACTIVE = "@@trial-active"
NETWORK_BLOCKED = "@@trial-network-blocked"
UNKNOWN_ADDRESS = "@@trial-unknown-address"
NORWEGIAN_STDERR = "@@trial-norwegian-stderr"
REQUEST = "@@trial-request"
# What one session must ask the fake Spond for: list, detail and attachment.
EXPECTED_REQUESTS = (
    "/core/v1/posts",
    "/core/v1/posts/innlegg-1",
    "/core/v1/group/gruppe-1/filesToken",
    "/storage/upload/group/fil-1",
    "download /falsk/fil-1.txt",
)

# Fictional family, team and messages. The characters are the point: æøå,
# «», en dash, an emoji in and one outside the Basic Multilingual Plane.
GROUP_NAME = "Håndball J10 – «Lynet»"
CHILD_NAME = "Åsmund Ødegård"
TITLE = "Treningen flyttes – «Lynet» spiller i Ørsta 🎉"
BODY = (
    "Hei alle sammen! Tirsdagens trening er flyttet til Æsøy hall – ta med "
    "drikkeflaske og godt humør 😀. Hilsen trener Åse 𝄞"
)
ATTACHMENT_NAME = "Kampoppsett høst – «Lynet».txt"
ATTACHMENT_TEXT = (
    "Kampoppsett høsten 2026\n"
    "Lørdag: «Lynet» – Ørsta IL 🎉\n"
    "Søndag: Ålesund – Bærum 𝄞\n"
    "Husk drikkeflaske og godt humør!"
)
STDERR_TEXT = "Prøve: norsk tekst på stderr æøå ÆØÅ «» – 🎉"
CLIENT_NAME = "Prøveklient – æøå 🎉"

REQUEST_SECONDS = 180


class TrialFailure(Exception):
    """A failed check, with a Norwegian message for the summary."""


def fixture(now: datetime) -> dict[str, Any]:
    """The fake Spond's answers; timestamps an hour before `now`."""

    posted = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "profile": {"id": "profil-1", "firstName": "Kari", "lastName": "Nordmann"},
        "groups": [
            {
                "id": "gruppe-1",
                "name": GROUP_NAME,
                "members": [
                    {
                        "id": "medlem-1",
                        "firstName": "Åsmund",
                        "lastName": "Ødegård",
                        "guardians": [
                            {
                                "id": "foresatt-1",
                                "profileId": "profil-1",
                                "firstName": "Kari",
                                "lastName": "Nordmann",
                            }
                        ],
                    }
                ],
            }
        ],
        "post": {
            "id": "innlegg-1",
            "type": "PLAIN",
            "timestamp": posted,
            "title": TITLE,
            "body": BODY,
            "groupId": "gruppe-1",
            "creatorId": "foresatt-1",
            "comments": [],
            "attachments": [
                {
                    "id": "vedlegg-1",
                    "url": "https://spond.com/storage/upload/group/fil-1",
                    "name": ATTACHMENT_NAME,
                    "mimeType": "text/plain",
                }
            ],
        },
        "files_token": "falskt-filtoken",
        "file_id": "fil-1",
        "storage_url": "https://spond-secure-attachments.s3.eu-west-1.amazonaws.com/falsk/fil-1.txt",
        "attachment_text": ATTACHMENT_TEXT,
        "stderr_text": STDERR_TEXT,
        "session_export": {
            "access_token": "falskt-tilgangstoken",
            "access_expires_at": (now + timedelta(days=30)).timestamp(),
            "refresh_token": "falskt-fornyingstoken",
            "refresh_expires_at": (now + timedelta(days=60)).timestamp(),
        },
    }


# --- Client configuration ------------------------------------------------


def claude_config_path(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        return home / "AppData/Roaming/Claude/claude_desktop_config.json"
    return home / ".config/Claude/claude_desktop_config.json"


def client_entry(home: Path) -> tuple[str, list[str]]:
    """The server command from Claude Desktop's configuration, checked against Codex's."""

    claude_path = claude_config_path(home)
    codex_path = home / ".codex/config.toml"
    try:
        claude = json.loads(claude_path.read_text(encoding="utf-8"))
        codex = tomllib.loads(codex_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TrialFailure(f"klientkonfigurasjonen kunne ikke leses: {error}") from None
    entry = claude.get("mcpServers", {}).get(SERVER_NAME)
    codex_entry = codex.get("mcp_servers", {}).get(SERVER_NAME)
    if not isinstance(entry, dict) or not isinstance(codex_entry, dict):
        raise TrialFailure("klientkonfigurasjonene mangler familiekilder-mcp")
    command, args = entry.get("command"), entry.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        raise TrialFailure("Claude-konfigurasjonen har ugyldig kommando")
    if codex_entry.get("command") != command or list(codex_entry.get("args", [])) != args:
        raise TrialFailure("Codex og Claude Desktop har ulik serverkommando")
    if codex_entry.get("enabled") is not True:
        raise TrialFailure("Codex-oppføringen er ikke slått på")
    return command, [str(value) for value in args]


def data_root(args: list[str]) -> Path:
    try:
        return Path(args[args.index("--root") + 1])
    except (ValueError, IndexError):
        raise TrialFailure("serverkommandoen mangler --root") from None


# --- Seam ------------------------------------------------------------------


def site_packages(venv: Path) -> Path:
    candidates = (
        [venv / "Lib" / "site-packages"]
        if os.name == "nt"
        else sorted(venv.glob("lib/python3.*/site-packages"))
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise TrialFailure("fant ikke site-packages i prøvens miljø")


def install_fake_spond(venv: Path, project: Path, fixture_file: Path) -> Path:
    """Write the start file and activation module into the trial's own venv."""

    support = project / "tests" / "support"
    if not (support / "familiekilder_testing" / "installed_trial.py").is_file():
        raise TrialFailure("tests/support/familiekilder_testing mangler i treet")
    target = site_packages(venv)
    # ascii() keeps both files plain ASCII, whatever the paths contain:
    # Python before 3.13 reads .pth files in the locale's encoding.
    module = (
        '"""Install trial only; written by scripts/stdio_trial.py."""\n'
        "import sys\n\n"
        f"SUPPORT = {ascii(str(support))}\n"
        f"FIXTURE = {ascii(str(fixture_file))}\n"
        "if SUPPORT not in sys.path:\n"
        "    sys.path.append(SUPPORT)\n\n"
        "from familiekilder_testing import installed_trial  # noqa: E402\n\n"
        "installed_trial.activate(FIXTURE)\n"
        "seed = installed_trial.seed\n"
    )
    (target / f"{ACTIVATE_MODULE}.py").write_text(module, encoding="ascii")
    (target / START_FILE).write_text(f"import {ACTIVATE_MODULE}\n", encoding="ascii")
    return target


# --- JSON-RPC over stdio --------------------------------------------------


class Session:
    """One server process, driven like a client: one JSON object per line."""

    def __init__(self, command: list[str] | str, env: dict[str, str], cwd: Path) -> None:
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        self.lines: queue.Queue[bytes | None] = queue.Queue()
        self.stderr = bytearray()
        self.raw_lines: list[bytes] = []
        self.invalid: list[str] = []
        self.next_id = 1
        threading.Thread(target=self._read_stdout, daemon=True).start()
        self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_thread.start()

    def _read_stdout(self) -> None:
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _read_stderr(self) -> None:
        while True:
            chunk = self.process.stderr.read1(65536)
            if not chunk:
                return
            self.stderr.extend(chunk)

    def send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        identifier = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + REQUEST_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TrialFailure(f"{method}: intet svar innen {REQUEST_SECONDS} s")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise TrialFailure(f"{method}: serveren avsluttet før svaret")
            message = self._parse(line)
            if isinstance(message, dict) and message.get("id") == identifier:
                if "error" in message:
                    raise TrialFailure(f"{method}: JSON-RPC-feil {message['error']}")
                return message.get("result", {}), line

    def _parse(self, line: bytes) -> Any:
        self.raw_lines.append(line)
        try:
            return json.loads(line.decode("utf-8"))
        except UnicodeDecodeError as error:
            self.invalid.append(f"ugyldig UTF-8 ({error.reason} ved byte {error.start})")
        except ValueError as error:
            self.invalid.append(f"ugyldig JSON ({error})")
        return None

    def close(self) -> int | None:
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            code = self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
            code = None
        while True:
            line = self.lines.get()
            if line is None:
                break
            self._parse(line)
        self.stderr_thread.join(timeout=10)
        return code


def tool_texts(result: dict[str, Any], tool: str) -> list[str]:
    if result.get("isError"):
        texts = " ".join(part.get("text", "") for part in result.get("content", []))
        raise TrialFailure(f"{tool} svarte med feil: {texts[:500]}")
    return [part["text"] for part in result.get("content", []) if part.get("type") == "text"]


def raw_form(raw: bytes, text: str) -> str:
    """How `text` travels in one raw response line."""

    found = []
    if text.encode("utf-8") in raw:
        found.append("UTF-8")
    escaped = json.dumps(text)[1:-1].encode("ascii")
    if escaped in raw or escaped.replace(b"\\", b"\\\\") in raw:
        found.append("\\u-escaper")
    return " og ".join(found) or "ikke funnet"


def stderr_form(stderr: bytes) -> str:
    if STDERR_TEXT.encode("utf-8") in stderr:
        return "UTF-8"
    if "æøå".encode("cp1252") in stderr:
        return "cp1252"
    if "æøå".encode("cp850") in stderr:
        return "cp850"
    if b"\\xe6\\xf8\\xe5" in stderr:
        return "escapet"
    return "ukjent"


def converse(session: Session) -> dict[str, Any]:
    """The client's side of one session; returns what the summary reports."""

    report: dict[str, Any] = {}
    initialized, _ = session.request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "1.0"},
        },
    )
    server = initialized.get("serverInfo", {})
    if server.get("name") != SERVER_NAME:
        raise TrialFailure(f"uventet servernavn {server.get('name')!r}")
    report["version"] = server.get("version")
    report["protocol"] = initialized.get("protocolVersion")
    session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    listed, _ = session.request("tools/list", {})
    names = {tool["name"] for tool in listed.get("tools", [])}
    for required in (
        "family_connections",
        "spond_list_items",
        "spond_get_item",
        "spond_read_attachment",
    ):
        if required not in names:
            raise TrialFailure(f"verktøyet {required} mangler")
    if any(not tool.get("annotations", {}).get("readOnlyHint") for tool in listed["tools"]):
        raise TrialFailure("serveren tilbyr verktøy som ikke bare leser")
    report["tools"] = len(names)

    connections, _ = session.request("tools/call", {"name": "family_connections", "arguments": {}})
    json.loads(tool_texts(connections, "family_connections")[0])

    # refresh and freshness=fresh: every session reads through the fake Spond,
    # not from the read cache an earlier session left in the data root.
    items_result, items_raw = session.request(
        "tools/call", {"name": "spond_list_items", "arguments": {"refresh": True}}
    )
    items = json.loads(tool_texts(items_result, "spond_list_items")[0]).get("items", [])
    matches = [item for item in items if item.get("title") == TITLE]
    if len(matches) != 1:
        titles = [item.get("title") for item in items]
        raise TrialFailure(f"spond_list_items: fant ikke den oppdiktede tittelen; fikk {titles!r}")
    item = matches[0]
    check_item(item, "spond_list_items")
    report["list"] = raw_form(items_raw, TITLE)

    detail_result, detail_raw = session.request(
        "tools/call",
        {
            "name": "spond_get_item",
            "arguments": {"reference": item["reference"], "freshness": "fresh"},
        },
    )
    detail = json.loads(tool_texts(detail_result, "spond_get_item")[0])
    check_item(detail, "spond_get_item")
    report["detail"] = raw_form(detail_raw, BODY)

    attachments = [
        entry for entry in detail.get("attachments", []) if entry.get("filename") == ATTACHMENT_NAME
    ]
    if len(attachments) != 1:
        raise TrialFailure(f"spond_get_item: vedlegget mangler; fikk {detail.get('attachments')!r}")
    read_result, read_raw = session.request(
        "tools/call",
        {
            "name": "spond_read_attachment",
            "arguments": {
                "reference": item["reference"],
                "attachment_id": attachments[0]["attachment_id"],
                "freshness": "fresh",
            },
        },
    )
    text = "\n".join(tool_texts(read_result, "spond_read_attachment"))
    for line in ATTACHMENT_TEXT.splitlines():
        if line not in text:
            raise TrialFailure(f"spond_read_attachment: linjen {line!r} kom ikke tilbake uendret")
    report["attachment"] = raw_form(read_raw, ATTACHMENT_TEXT.splitlines()[1])
    return report


def check_item(item: dict[str, Any], tool: str) -> None:
    expected = {"title": TITLE, "topic": GROUP_NAME, "child": CHILD_NAME}
    for field, value in expected.items():
        if item.get(field) != value:
            raise TrialFailure(f"{tool}: {field} er {item.get(field)!r}, ventet {value!r}")
    if BODY not in str(item.get("body")):
        raise TrialFailure(f"{tool}: teksten kom ikke tilbake uendret: {item.get('body')!r}")


def fake_requests(stderr: bytes) -> list[str]:
    prefix = REQUEST.encode() + b" "
    return [
        line[len(prefix) :].decode("utf-8", "backslashreplace").strip()
        for line in stderr.splitlines()
        if line.startswith(prefix)
    ]


def check_stderr(stderr: bytes) -> None:
    if ACTIVE.encode() not in stderr:
        raise TrialFailure("den falske Spond-tjenesten ble ikke aktivert i serveren")
    missing = [path for path in EXPECTED_REQUESTS if path not in fake_requests(stderr)]
    if missing:
        raise TrialFailure(
            f"serveren spurte ikke den falske Spond-tjenesten om {', '.join(missing)}"
        )
    if NETWORK_BLOCKED.encode() in stderr:
        raise TrialFailure("serveren prøvde å nå nettverket")
    if UNKNOWN_ADDRESS.encode() in stderr:
        raise TrialFailure("serveren spurte den falske Spond-tjenesten om en ukjent adresse")
    if NORWEGIAN_STDERR.encode() not in stderr:
        raise TrialFailure("stderr-linjen med norske tegn kom ikke fram")


def windows_codepage_command(command: str, args: list[str], codepage: str) -> str:
    """cmd.exe sets the console code page, then starts the server unchanged.

    chcp's own output goes to stderr, never into the JSON-RPC stream.
    """

    for value in (command, *args):
        if any(character in value for character in '"%^\r\n'):
            raise TrialFailure("serverkommandoen har tegn som cmd.exe ikke kan sitere trygt")
    quoted = " ".join(f'"{value}"' for value in (command, *args))
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    return f'"{comspec}" /d /s /c "chcp {codepage} 1>&2 & {quoted}"'


def run(trial: Any) -> None:
    """Steps B1–B5 of the install trial; `trial` is install_trial.Trial."""

    home: Path = trial.home
    with trial.step("Les serverkommandoen fra klientkonfigurasjonene"):
        command, args = client_entry(home)
        root = data_root(args)
        for path, name in ((Path(command), "kommandoen"), (root, "dataroten")):
            if not path.is_relative_to(home):
                raise TrialFailure(f"{name} ligger utenfor prøvehjemmet: {trial.redact(str(path))}")
        trial.log(f"kommando: {trial.redact(command)} {trial.redact(' '.join(args))}")
        trial.note(f"datarot fra veiviseren: {trial.redact(str(root))}")

    venv = Path(command).parents[1]
    executable = venv / (
        "Scripts/familiekilder-mcp.exe" if os.name == "nt" else "bin/familiekilder-mcp"
    )
    with trial.step("Legg falsk Spond inn i prøvens eget miljø"):
        fixture_file = home / "falsk spond – æøå.json"
        fixture_file.write_text(
            json.dumps(fixture(datetime.now(UTC)), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        target = install_fake_spond(venv, trial.project, fixture_file)
        trial.log(f"oppstartsfil: {trial.redact(str(target / START_FILE))}")
        seeded = trial.run_checked(
            [
                command,
                "-I",
                "-c",
                f"import sys, {ACTIVATE_MODULE} as a; a.seed(sys.argv[1])",
                str(root),
            ],
            "lagre innebygd Spond-oppsett og oppdiktet økt",
        )
        if ACTIVE not in seeded:
            raise TrialFailure("oppstartsfilen ble ikke lest i prøvens miljø")
        configured = trial.run_checked(
            [str(executable), "configure", "spond"], "familiekilder-mcp configure spond"
        )
        if '"spond"' not in configured:
            raise TrialFailure("configure spond slo ikke på Spond")

    variants: list[tuple[str, list[str] | str]] = [("som klienten starter den", [command, *args])]
    if os.name == "nt":
        for codepage in ("850", "1252"):
            variants.append(
                (
                    f"med tegntabell {codepage} (chcp)",
                    windows_codepage_command(command, args, codepage),
                )
            )
    for label, launch in variants:
        with trial.step(f"MCP over stdio {label}"):
            session = Session(launch, trial.env, home)
            try:
                report = converse(session)
            finally:
                code = session.close()
                stderr = bytes(session.stderr)
                trial.log("stderr fra serveren, uten forespørslene til den falske Spond-tjenesten:")
                for line in stderr.decode("utf-8", "backslashreplace").splitlines():
                    if not line.startswith(REQUEST):
                        trial.log(f"  ! {trial.redact(line)}")
                trial.log("falsk Spond fikk: " + ", ".join(fake_requests(stderr)))
            if session.invalid:
                raise TrialFailure(
                    "ugyldige linjer fra serveren: " + "; ".join(session.invalid[:3])
                )
            check_stderr(stderr)
            if code != 0:
                raise TrialFailure(f"serveren avsluttet med {code} da stdin ble lukket")
            trial.log(
                f"server {report['version']}, protokoll {report['protocol']}, {report['tools']} verktøy, "
                f"{len(session.raw_lines)} linjer, alle gyldig UTF-8 og JSON"
            )
            trial.note(
                f"{label}: tegn i rå byte: liste {report['list']}; detalj {report['detail']}; "
                f"vedlegg {report['attachment']}; stderr {stderr_form(stderr)}"
            )
            if os.name == "nt" and not isinstance(launch, list):
                chcp = [
                    line
                    for line in stderr.decode("utf-8", "backslashreplace").splitlines()
                    if not line.startswith("@@")
                ][:1]
                trial.note(f"{label}: chcp sa: {chcp[0] if chcp else '(ingenting)'}")
