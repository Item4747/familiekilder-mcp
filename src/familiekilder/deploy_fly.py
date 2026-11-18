"""``familiekilder-mcp deploy fly``: a guided always-on host on Fly.io.

Wraps ``flyctl`` so the user, or the user's coding agent, can set up the host
with one command:

1. Check that ``flyctl`` is installed and logged in.
2. Create the app and one 1 GB volume (first run only).
3. Store the owner-code hash and the installation ID as Fly secrets. They go
   to ``flyctl`` on stdin, never as arguments.
4. Deploy the container image with exactly one machine that stops when idle
   and starts on the next request. ``word=True`` builds it with LibreOffice
   for Word attachments; every deploy states whether Word is on, since the
   choice is not remembered between deploys.
5. Show the address and, on first run, the owner code once.
6. Offer to move the logins from this computer.

Nothing here touches the user's Fly account except through ``flyctl``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .core import FamiliekilderError
from .hosting import (
    HEALTH_PATH,
    INSTALLATION_ID_ENV,
    MCP_PATH,
    OWNER_CODE_HASH_ENV,
    hash_owner_code,
    new_installation_id,
    new_owner_code,
)

VOLUME_NAME = "familiekilder_data"
VOLUME_GB = 1
DEFAULT_REGION = "arn"
DEFAULT_ORG = "personal"
MACHINE_MEMORY = "256mb"
DATA_ROOT = "/data/familiekilder"
INTERNAL_PORT = 8080
_APP_NAME = re.compile(r"[a-z][a-z0-9-]{2,62}\Z")
_REGION = re.compile(r"[a-z]{3}\Z")
# Dockerfile build argument that installs LibreOffice for Word attachments.
WORD_BUILD_ARG = "WITH_LIBREOFFICE"
# LibreOffice does not fit in the machine's memory next to the server: on a
# real 256 MB machine the kernel killed it. Swap costs nothing extra and is
# only touched during a conversion.
WORD_SWAP_MB = 512

INSTALL_HELP = (
    "Fant ikke flyctl. Installer den med «brew install flyctl» (eller se "
    "https://fly.io/docs/flyctl/install/), lag en konto og logg inn med «flyctl auth login». "
    "Kjør så kommandoen igjen."
)
LOGIN_HELP = (
    "flyctl er ikke logget inn. Kjør «flyctl auth login» (eller «flyctl auth signup» for "
    "ny konto) i et terminalvindu, og kjør så kommandoen igjen."
)


class DeployError(FamiliekilderError):
    """The guided setup stopped; the message says what to do."""


@dataclass
class Flyctl:
    executable: str
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run

    def __call__(
        self,
        *args: str,
        stdin: str | None = None,
        check: bool = True,
        stream: bool = False,
        timeout: float = 600,
    ) -> subprocess.CompletedProcess:
        try:
            result = self.run(
                [self.executable, *args],
                input=stdin,
                text=True,
                stdout=None if stream else subprocess.PIPE,
                stderr=None if stream else subprocess.PIPE,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            raise DeployError(f"flyctl {args[0]} kunne ikke kjøres") from None
        if check and result.returncode != 0:
            # A subprocess can echo stdin (secrets) in its error output.
            raise DeployError(f"flyctl {' '.join(args[:2])} feilet")
        return result

    def json(self, *args: str) -> Any:
        result = self(*args, "--json")
        try:
            return json.loads(result.stdout or "null")
        except ValueError:
            raise DeployError(f"flyctl {' '.join(args[:2])} ga ukjent svar") from None


def find_flyctl() -> Flyctl:
    executable = shutil.which("flyctl") or shutil.which("fly")
    if not executable:
        raise DeployError(INSTALL_HELP)
    return Flyctl(executable)


def _names(items: Any) -> set[str]:
    if not isinstance(items, list):
        return set()
    return {
        str(item.get("Name") or item.get("name"))
        for item in items
        if isinstance(item, dict) and (item.get("Name") or item.get("name"))
    }


def fly_toml(
    app: str, region: str, dockerfile: str, build_args: dict[str, str], *, swap_mb: int = 0
) -> str:
    """The app configuration: one volume, HTTPS, stop when idle, health check.

    ``dockerfile`` must be relative to the directory that holds fly.toml.
    ``swap_mb`` above zero gives the machine that much swap space.
    """

    # An anchor marks an absolute path. On Windows it also catches /name and
    # C:name, which are not absolute there but are not relative either.
    if Path(dockerfile).anchor:
        raise ValueError("dockerfile must be relative to the fly.toml directory")

    lines = [
        f'app = "{app}"',
        f'primary_region = "{region}"',
    ]
    if swap_mb:
        lines.append(f"swap_size_mb = {swap_mb}")
    lines += [
        "",
        "[deploy]",
        '  strategy = "immediate"',
        "",
        "[build]",
        f'  dockerfile = "{dockerfile}"',
    ]
    if build_args:
        lines.append("  [build.args]")
        lines += [f'    {key} = "{value}"' for key, value in sorted(build_args.items())]
    lines += [
        "",
        "[env]",
        f'  FAMILIEKILDER_ROOT = "{DATA_ROOT}"',
        f'  FAMILIEKILDER_PUBLIC_URL = "https://{app}.fly.dev"',
        "",
        "[[mounts]]",
        f'  source = "{VOLUME_NAME}"',
        '  destination = "/data"',
        "",
        "[http_service]",
        f"  internal_port = {INTERNAL_PORT}",
        "  force_https = true",
        '  auto_stop_machines = "stop"',
        "  auto_start_machines = true",
        "  min_machines_running = 0",
        "",
        "  [[http_service.checks]]",
        '    grace_period = "15s"',
        '    interval = "60s"',
        '    method = "GET"',
        f'    path = "{HEALTH_PATH}"',
        '    timeout = "5s"',
        "",
        "[[vm]]",
        '  size = "shared-cpu-1x"',
        f'  memory = "{MACHINE_MEMORY}"',
        "",
    ]
    return "\n".join(lines)


def build_context(directory: Path) -> dict[str, str]:
    """Fill ``directory`` with a build context and return the build args.

    From a source checkout the image is built from that source; only the
    files the package needs are copied, never ``.private``. Otherwise the
    image installs this exact version from PyPI.
    """

    package = Path(__file__).resolve().parent
    shutil.copyfile(package / "data/container/Dockerfile", directory / "Dockerfile")
    project = package.parents[1]
    pyproject = project / "pyproject.toml"
    if pyproject.is_file() and 'name = "familiekilder-mcp"' in pyproject.read_text(encoding="utf-8"):
        for name in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in"):
            shutil.copyfile(project / name, directory / name)
        shutil.copytree(
            project / "src",
            directory / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", ".private"),
        )
        return {}
    return {"FAMILIEKILDER_PACKAGE": f"familiekilder-mcp=={__version__}"}


def show_owner_code(code: str, *, out: TextIO, interactive: bool, fallback: Path) -> None:
    """Show the owner code once: on the terminal, or in a private file for an agent."""

    if interactive:
        out.write(
            "\nEierkode (vises bare nå): " + code + "\n"
            "Lagre den i passordbehandleren. Du trenger den når du kobler Claude eller "
            "ChatGPT til verten, og når du flytter innlogginger.\n\n"
        )
        return
    fallback.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        # Windows ignores the mode; give .private its user-only access list.
        from .private_state import ensure_private_directory

        ensure_private_directory(fallback.parent)
    descriptor = os.open(fallback, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(code + "\n")
    out.write(
        f"\nEierkoden er lagret i {fallback} (bare lesbar for deg). Be brukeren åpne filen, "
        "lagre koden i passordbehandleren og slette filen.\n\n"
    )


def _wait_until_up(url: str, *, attempts: int = 30, delay: float = 2.0, get=None) -> bool:
    import httpx

    get = get or (lambda target: httpx.get(target, timeout=10))
    for _ in range(attempts):
        try:
            if get(url + HEALTH_PATH).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(delay)
    return False


def deploy(
    *,
    root: Path,
    app: str | None,
    region: str,
    org: str,
    move: bool | None,
    new_code: bool,
    word: bool = False,
    out: TextIO = sys.stdout,
    interactive: bool | None = None,
    flyctl: Flyctl | None = None,
    ask: Callable[[str], str] = input,
    mover: Callable[..., dict[str, Any]] | None = None,
    wait_until_up: Callable[[str], bool] = _wait_until_up,
) -> dict[str, Any]:
    interactive = sys.stdin.isatty() and sys.stdout.isatty() if interactive is None else interactive
    if not _REGION.fullmatch(region):
        raise DeployError("Regionen må være en kode på tre bokstaver, for eksempel arn")
    fly = flyctl or find_flyctl()
    fly("version")
    if fly("auth", "whoami", check=False).returncode != 0:
        raise DeployError(LOGIN_HELP)

    app = app or "familiekilder-" + "".join(
        secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(8)
    )
    if not _APP_NAME.fullmatch(app):
        raise DeployError("Appnavnet må være 3–63 små bokstaver, tall eller bindestrek")
    url = f"https://{app}.fly.dev"

    existing = app in _names(fly.json("apps", "list"))
    if not existing:
        out.write(f"Lager appen {app} i {org} ...\n")
        fly("apps", "create", app, "--org", org)
    machines = fly.json("machines", "list", "--app", app)
    if not isinstance(machines, list) or len(machines) > 1:
        raise DeployError("Appen må ha høyst én maskin før utrulling; ingen hemmeligheter er endret")
    if VOLUME_NAME not in _names(fly.json("volumes", "list", "--app", app)):
        out.write(f"Lager én disk på {VOLUME_GB} GB i {region} ...\n")
        fly(
            "volumes", "create", VOLUME_NAME, "--app", app, "--region", region,
            "--size", str(VOLUME_GB), "--yes",
        )

    secret_names = _names(fly.json("secrets", "list", "--app", app))
    owner_code = None
    staged = []
    if new_code or OWNER_CODE_HASH_ENV not in secret_names:
        owner_code = new_owner_code()
        staged.append(f"{OWNER_CODE_HASH_ENV}={hash_owner_code(owner_code)}")
        # Deliver before changing any remote hash. Failed deploys are retryable
        # with the already delivered code. Unique paths also allow rotations.
        show_owner_code(
            owner_code,
            out=out,
            interactive=interactive,
            fallback=root / ".private" / "remote" / f"{app}-eierkode-{secrets.token_hex(8)}.txt",
        )
        out.flush()
    # Never replace an existing installation ID: the moved logins are bound to it.
    if INSTALLATION_ID_ENV not in secret_names:
        staged.append(f"{INSTALLATION_ID_ENV}={new_installation_id()}")
    if staged:
        fly("secrets", "import", "--app", app, "--stage", stdin="\n".join(staged) + "\n")

    with tempfile.TemporaryDirectory(prefix="familiekilder-fly-") as directory:
        context = Path(directory) / "context"
        context.mkdir()
        build_args = build_context(context)
        if word:
            build_args[WORD_BUILD_ARG] = "1"
        config = Path(directory) / "fly.toml"
        # flyctl joins [build] dockerfile onto the directory of fly.toml, even
        # when the value is absolute, and ignores --dockerfile when fly.toml
        # names one. So the Dockerfile is named here only, relative to fly.toml.
        dockerfile = (context / "Dockerfile").relative_to(config.parent).as_posix()
        config.write_text(
            fly_toml(
                app, region, dockerfile, build_args, swap_mb=WORD_SWAP_MB if word else 0
            ),
            encoding="utf-8",
        )
        out.write("Bygger og ruller ut (dette tar noen minutter) ...\n")
        out.flush()
        fly(
            "deploy", str(context), "--app", app, "--config", str(config),
            "--ha=false", "--remote-only", "--strategy", "immediate",
            stream=True, timeout=1800,
        )

    # Exactly one machine: two would renew the same tokens.
    machines = fly.json("machines", "list", "--app", app)
    if not isinstance(machines, list) or len(machines) != 1:
        fly("scale", "count", "1", "--app", app, "--yes")
        machines = fly.json("machines", "list", "--app", app)
        if not isinstance(machines, list) or len(machines) != 1:
            raise DeployError(
                "Appen har ikke nøyaktig én maskin. Kjør «flyctl scale count 1 --app "
                f"{app}» og sjekk med «flyctl machines list --app {app}»."
            )

    out.write(f"\nVerten er klar: {url}{MCP_PATH}\n")
    if word:
        out.write(
            "Word-vedlegg er på. Bruk --word også neste gang du ruller ut, ellers "
            "forsvinner støtten.\n"
        )
    else:
        out.write("Word-vedlegg er av. Bruk --word for å lese Word-vedlegg på verten.\n")
    if owner_code is None:
        out.write("Eierkoden er uendret. Bruk --new-owner-code for å lage en ny.\n")

    if move is None and interactive:
        answer = ask("Flytte innloggingene fra denne maskinen til verten nå? [j/N] ")
        move = answer.strip().lower() in {"j", "ja", "y", "yes"}
    moved = None
    if move:
        if owner_code is None:
            from getpass import getpass

            owner_code = getpass("Eierkode: ") if interactive else sys.stdin.readline().strip()
        if not wait_until_up(url):
            raise DeployError(
                f"Verten svarer ikke ennå. Prøv flyttingen senere med «familiekilder-mcp remote move {url}»."
            )
        from .session_move import move_to

        moved = (mover or move_to)(root, url, owner_code)
        out.write("Flyttet: " + ", ".join(moved["moved"]) + "\n")
    else:
        out.write(f"Flytt innloggingene senere med «familiekilder-mcp remote move {url}».\n")
    return {
        "app": app,
        "url": url + MCP_PATH,
        "region": region,
        "word": word,
        "moved": moved and moved["moved"],
    }
