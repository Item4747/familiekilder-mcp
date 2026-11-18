"""A pre-flight check of this computer for logins: ``familiekilder-mcp doctor``.

The report is meant to be pasted into a public issue, so it never contains
passwords, tokens, session details, user or account names, e-mail addresses,
anything read from a service, a machine ID or the home folder in clear text.
The home folder is shown as ``~``, and a path segment equal to the user name
is replaced. The data root is described by source and status only. Nothing
here logs in, reads a stored password or contacts a service. Trust assessment
by Gatekeeper and Authenticode is deferred until login.

The report covers the program and system, each browser and whether a login
may start it, password storage, the data root and its private folder, the AI
clients that start this installation, a screen on Linux and the services
that are turned on. Every refusal or failure comes with advice. The text uses
only characters that both cp850 and cp1252 have, so it prints in an ordinary
Windows console.

Internal API: the command line and the setup page call `run` and `render`.
"""

from __future__ import annotations

import os
import platform as platform_module
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import __version__
from . import browsers

PROG = "familiekilder-mcp"

# Where the data root came from, as the report names it.
ROOT_DEFAULT = "standard"
ROOT_OPTION = "--root"
ROOT_PROFILE = "--profile"
ROOT_ENVIRONMENT = "FAMILIEKILDER_ROOT"

_ROOT_SOURCE_TEXT = {
    ROOT_DEFAULT: "standard",
    ROOT_OPTION: "valgt med --root",
    ROOT_PROFILE: "valgt med --profile",
    ROOT_ENVIRONMENT: "valgt med FAMILIEKILDER_ROOT",
}

_CLIENT_NAMES = {"claude": "Claude Desktop", "codex": "ChatGPT / Codex"}

# Status tags in the text report.
_TAGS = {
    "selected": "BRUKES",
    "approved": "OK",
    "candidate": "KANDIDAT",
    "pending": "UAVKLART",
    "rejected": "AVVIST",
    "missing": "IKKE FUNNET",
    "ok": "OK",
    "absent": "OK",
    "note": "MERK",
    "error": "FEIL",
}
_TAG_WIDTH = 12


class Redactor:
    """Removes the home folder and the user name from text that leaves the machine."""

    def __init__(
        self,
        *,
        home: str | None,
        username: str | None,
        windows: bool,
        local_app_data: str | None = None,
    ) -> None:
        self._windows = windows
        flags = re.IGNORECASE if windows else 0
        self._prefixes: list[tuple[re.Pattern[str], str]] = []
        # The home folder first, so a local app folder below it reads ~\AppData\Local.
        for prefix, label in ((home, "~"), (local_app_data, "%LOCALAPPDATA%")):
            for variant in self._variants(prefix):
                pattern = re.compile(re.escape(variant) + r"(?=[\\/]|$|[^\w.-])", flags)
                self._prefixes.append((pattern, label))
        self._username = None
        if username and username.strip():
            self._username = re.compile(
                r"(?<=[\\/])" + re.escape(username.strip()) + r"(?=[\\/]|$|[^\w.-])",
                re.IGNORECASE,
            )

    def _variants(self, prefix: str | None) -> list[str]:
        if not prefix:
            return []
        trimmed = prefix.rstrip("\\/") or prefix
        if len(trimmed) < 2:
            return []
        variants = [trimmed]
        if self._windows:
            variants.append(trimmed.replace("\\", "/"))
            variants.append(trimmed.replace("/", "\\"))
        # Longest first, so a longer spelling is never cut by a shorter one.
        return sorted(set(variants), key=len, reverse=True)

    def text(self, value: str) -> str:
        for pattern, label in self._prefixes:
            value = pattern.sub(lambda _match, label=label: label, value)
        if self._username is not None:
            value = self._username.sub("<bruker>", value)
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {key: self.value(item) for key, item in value.items()}
        return value


def _operating_system(name: str) -> tuple[str, str, str]:
    """Name, version and architecture of this computer, without its network name."""

    architecture = platform_module.machine() or "ukjent"
    if name == browsers.MACOS:
        return "macOS", platform_module.mac_ver()[0] or "ukjent", architecture
    if name == browsers.WINDOWS:
        release = platform_module.release() or "ukjent"
        build = platform_module.version()
        return "Windows", f"{release} ({build})" if build else release, architecture
    if name == browsers.LINUX:
        try:
            release = platform_module.freedesktop_os_release()
        except OSError:
            release = {}
        distribution = release.get("PRETTY_NAME") or release.get("NAME") or "Linux"
        return distribution, f"kjerne {platform_module.release()}", architecture
    return name, platform_module.release() or "ukjent", architecture


def _username(environ: Mapping[str, str]) -> str | None:
    for key in ("USERNAME", "USER", "LOGNAME"):
        if environ.get(key):
            return environ[key]
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001 - only used to hide the name
        return None


def run(
    root: Path,
    *,
    root_source: str = ROOT_DEFAULT,
    system: browsers.System | None = None,
    home: Path | None = None,
    username: str | None = None,
    operating_system: tuple[str, str, str] | None = None,
    python: tuple[str, bool] | None = None,
    password_backend: Callable[[str], Any] | None = None,
    client_home: Path | None = None,
) -> dict[str, Any]:
    """Check this computer and return the report, already without private details.

    Tests pass made-up parts; by default each part is read from this computer.
    Nothing is created or changed, also not the data root. The `ok` field
    means no local blocker was found, including when trust remains pending.
    """

    system = system or browsers.System()
    environ = system.environ()
    home_text = str(home) if home is not None else system.home()
    client_home = client_home or Path(home_text)
    if username is None:
        username = _username(environ)
    redactor = Redactor(
        home=home_text,
        username=username,
        windows=system.platform == browsers.WINDOWS,
        local_app_data=system.local_app_data() if system.platform == browsers.WINDOWS else None,
    )
    if operating_system is None:
        operating_system = _operating_system(system.platform)
    if python is None:
        python = (platform_module.python_version(), sys.prefix != sys.base_prefix)

    os_name, os_version, architecture = operating_system
    report: dict[str, Any] = {
        "program": {"name": PROG, "version": __version__},
        "system": {"name": os_name, "version": os_version, "architecture": architecture},
        "python": {"version": python[0], "venv": python[1]},
    }
    config = _load_config(root)
    report["browsers"] = _browsers(root, system)
    report["passwords"] = _passwords(system.platform, password_backend)
    report["data"] = _data(root, root_source, config is not None)
    report["clients"] = _clients(root, client_home)
    report["display"] = _display(system)
    report["services"] = _services(root, config)
    problems = _problems(report)
    report = {"ok": not problems, "problems": problems, **report}
    return redactor.value(report)


def _load_config(root: Path) -> Any:
    from .core import FamiliekilderError
    from .live_config import Config

    try:
        return Config.load(root)
    except (FamiliekilderError, OSError, ValueError):
        # Exception text can contain an arbitrary path or configuration value.
        return None


def _browsers(root: Path, system: browsers.System) -> dict[str, Any]:
    choice = browsers.saved_browser(root)
    try:
        rows = browsers.survey(choice, system=system, offline=True)
    except browsers.BrowserBootstrapError as error:
        rows = ()
        unsupported = str(error)
    else:
        unsupported = None
    if system.platform not in browsers.PLATFORMS:
        unsupported = "Innlogging i nettleser støttes på macOS, Windows og Linux."
    items = [
        {
            "browser": row.browser,
            "name": row.name,
            "status": row.status,
            "path": row.path,
            "reason": row.reason,
            "advice": row.advice,
        }
        for row in rows
    ]
    selected = next((row.browser for row in rows if row.status == browsers.SELECTED), None)
    candidate = next((row.browser for row in rows if row.status == browsers.CANDIDATE), None)
    result: dict[str, Any] = {
        "choice": choice or "auto",
        "selected": selected,
        "candidate": candidate,
        "items": items,
        "status": "ok" if selected else "pending" if candidate else "error",
        "note": None,
        "advice": None,
    }
    if system.platform == browsers.MACOS:
        result["note"] = (
            "Appsti og signaturens oppgitte app- og utgiver-ID er sjekket lokalt. "
            "Gatekeeper avgjør godkjenningen ved innlogging."
        )
    elif system.platform == browsers.WINDOWS:
        result["note"] = (
            "Programsti, lenker og skrivetilgang er sjekket lokalt. "
            "Authenticode avgjør signaturgodkjenningen ved innlogging."
        )
    if unsupported is not None:
        result["advice"] = unsupported
    elif result["status"] == "error" and choice is not None:
        name = browsers.browser_name(choice)
        chosen = next((row for row in rows if row.browser == choice), None)
        others = [row for row in rows if row.status in (browsers.APPROVED, browsers.PENDING)]
        if chosen is not None and chosen.status == browsers.MISSING:
            result["advice"] = f"Du har valgt {name}, men den er ikke installert. " + (
                f"Installer den fra leverandøren, eller velg {others[0].name} med "
                f"«{PROG} browser {others[0].browser}»."
                if others
                else f"Installer den fra leverandøren, eller velg automatisk med «{PROG} browser auto»."
            )
        else:
            result["advice"] = (
                f"Du har valgt {name}, men den kan ikke brukes. Se grunnen og rådet over."
            )
    elif result["status"] == "error":
        result["advice"] = (
            "Ingen nettleser kan brukes til innlogging. Installer Brave, Google Chrome eller "
            "Microsoft Edge fra leverandøren, for alle brukere på maskinen."
        )
    return result


def _passwords(platform: str, backend: Callable[[str], Any] | None) -> dict[str, Any]:
    from . import desktop_platform
    from .core import FamiliekilderError

    backend = backend or desktop_platform.password_backend
    label = desktop_platform.password_storage_label(platform)
    try:
        # Only finds the store; never reads, writes or lists a password.
        backend(platform)
    except FamiliekilderError as error:
        reason = str(error)
    except Exception:  # noqa: BLE001 - a store's own errors may carry details
        reason = f"{label} er utilgjengelig"
    else:
        return {"status": "ok", "store": label, "reason": None, "advice": None}
    if platform == browsers.LINUX:
        advice = (
            "Start en nøkkelring som GNOME Keyring eller KWallet i skrivebordsøkten. Uten den "
            "virker innloggingen likevel, men du skriver passordet i nettleseren hver gang."
        )
    else:
        advice = (
            f"Kontroller at {label} virker for brukeren din. Uten den virker innloggingen "
            "likevel, men du skriver passordet i nettleseren hver gang."
        )
    return {"status": "note", "store": label, "reason": _sentence(reason), "advice": advice}


def _data(root: Path, root_source: str, config_readable: bool) -> dict[str, Any]:
    from .core import FamiliekilderError
    from .private_state import validate_private_parent_tree

    private = root / ".private"
    result: dict[str, Any] = {
        "source": root_source,
        "status": "ok",
        "reason": None,
        "advice": None,
    }
    try:
        root_exists = root.exists() or root.is_symlink()
    except OSError:
        root_exists = True
    if not root_exists:
        result["status"] = "absent"
        result["reason"] = "Datamappen finnes ikke ennå. Den lages ved første innlogging."
        return result
    try:
        # Checks owner, type and access of .private without creating or changing it.
        present = validate_private_parent_tree(private / "doctor-check")
    except FamiliekilderError:
        result["status"] = "error"
        result["reason"] = "Den private mappen har utrygg type, eier eller tilgang."
        result["advice"] = (
            "Mappen .private må være en vanlig mappe som eies av brukeren din, og bare du skal "
            "ha tilgang (på Mac og Linux: «chmod 700» på mappen). Flytt den ellers bort, eller "
            "velg en annen datarot med --root eller FAMILIEKILDER_ROOT, og logg inn på nytt."
        )
        return result
    except OSError:
        result["status"] = "error"
        result["reason"] = "Den private mappen kunne ikke kontrolleres."
        result["advice"] = "Kontroller at brukeren din kan lese datamappen, og prøv igjen."
        return result
    if not present:
        result["status"] = "absent"
        result["reason"] = "Den private mappen finnes ikke ennå. Den lages ved første innlogging."
        return result
    if not config_readable:
        result["status"] = "error"
        result["reason"] = "Oppsettet av tjenestene kan ikke leses."
        result["advice"] = (
            "Slå tjenestene av og på igjen med «familiekilder-mcp configure», eller flytt "
            ".private/mcp-config.json bort og velg tjenestene på nytt i «familiekilder-mcp setup»."
        )
        return result
    result["reason"] = "Den private mappen har riktig eier og tilgang."
    return result


def _clients(root: Path, home: Path) -> list[dict[str, Any]]:
    from .setup_clients import client_state, config_path

    rows = []
    for client, name in _CLIENT_NAMES.items():
        state = client_state(client, root, home)
        row: dict[str, Any] = {
            "client": client,
            "name": name,
            "config": str(config_path(client, home)),
            "status": "ok",
            "reason": None,
            "advice": None,
        }
        if state.get("error"):
            row["status"] = "note"
            row["reason"] = "Konfigurasjonsfilen kunne ikke leses."
            row["advice"] = (
                "Kontroller at filen er gyldig, eies av brukeren din og ikke er en lenke. "
                "Koble til på nytt i «familiekilder-mcp setup»."
            )
        elif state.get("configured"):
            row["reason"] = "Registrert, og starter denne installasjonen med denne datamappen."
        elif state.get("registered"):
            row["status"] = "note"
            row["reason"] = (
                "Registrert, men starter en annen installasjon eller en annen datamappe."
            )
            row["advice"] = (
                "Velg «Oppdater koblingen» i «familiekilder-mcp setup» med samme datarot som "
                "du logger inn med, og start AI-appen på nytt."
            )
        else:
            row["status"] = "missing"
            row["reason"] = "Ikke registrert."
            row["advice"] = (
                "Koble til i steg 2 i «familiekilder-mcp setup» hvis du bruker denne appen."
            )
        rows.append(row)
    return rows


def _display(system: browsers.System) -> dict[str, Any] | None:
    from .browser_bootstrap import _require_display

    if system.platform != browsers.LINUX:
        return None
    try:
        _require_display(system.platform, system.environ())
    except browsers.BrowserBootstrapError as error:
        return {
            "status": "error",
            "reason": "Fant ingen skjerm (verken DISPLAY eller WAYLAND_DISPLAY er satt).",
            "advice": _sentence(str(error)),
        }
    return {"status": "ok", "reason": "Skjerm funnet for innloggingsvinduet.", "advice": None}


def _services(root: Path, config: Any) -> dict[str, Any]:
    from .connection_status import auth_module, has_local_session
    from .sources import REGISTRY

    if config is None:
        return {"status": "note", "items": [], "advice": "Se Datamappe over."}
    items = []
    for name, source in REGISTRY.items():
        if name not in config.services:
            continue
        try:
            stored = has_local_session(auth_module(name), root)
        except Exception:  # noqa: BLE001 - only yes, no or unknown is shown
            stored = None
        items.append({"service": name, "name": source.label, "stored_login": stored})
    if not items:
        return {
            "status": "note",
            "items": [],
            "advice": "Ingen tjenester er slått på ennå. Velg dem i «familiekilder-mcp setup».",
        }
    return {"status": "ok", "items": items, "advice": None}


def _problems(report: Mapping[str, Any]) -> int:
    """The failures that stop a login: no browser, no screen or an unsafe data folder."""

    count = 0
    if report["browsers"]["status"] == "error":
        count += 1
    if report["data"]["status"] == "error":
        count += 1
    if report["display"] is not None and report["display"]["status"] == "error":
        count += 1
    return count


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _line(tag: str, text: str) -> str:
    return f"  {tag.ljust(_TAG_WIDTH)}{text}".rstrip()


def _detail(label: str, text: str | None) -> list[str]:
    if not text:
        return []
    indent = " " * (2 + _TAG_WIDTH)
    return [f"{indent}{label}{text}"]


def render(report: Mapping[str, Any]) -> str:
    """The report as plain text, ready to paste into an issue."""

    lines = [f"{PROG} doctor"]
    problems = report["problems"]
    if problems == 0 and report["browsers"]["status"] == "pending":
        lines.append("Resultat: Ingen lokale feil funnet. Signaturen sjekkes ved innlogging.")
    elif problems == 0:
        lines.append("Resultat: OK. Alt som trengs for innlogging, er i orden.")
    elif problems == 1:
        lines.append("Resultat: 1 problem må rettes før innlogging. Se FEIL og råd nedenfor.")
    else:
        lines.append(
            f"Resultat: {problems} problemer må rettes før innlogging. Se FEIL og råd nedenfor."
        )
    lines.append(
        "Rapporten har ingen passord, tokens, brukernavn eller innhold fra tjenestene. "
        "Hjemmemappen vises som ~. Datarotens sti og profilnavn vises ikke."
    )

    program = report["program"]
    system = report["system"]
    python = report["python"]
    lines += [
        "",
        "Program",
        f"  {program['name']} {program['version']}",
        f"  System: {system['name']} {system['version']}, {system['architecture']}",
        "  Python: {}, {}".format(
            python["version"],
            "i et virtuelt miljø (venv)" if python["venv"] else "ikke i et virtuelt miljø",
        ),
    ]

    browser_report = report["browsers"]
    lines += ["", "Nettleser for innlogging"]
    if browser_report["choice"] == "auto":
        lines.append(
            "  Valg: automatisk (den første godkjente av Brave, Google Chrome og Microsoft Edge)"
        )
    else:
        lines.append(
            f"  Valg: {browsers.browser_name(browser_report['choice'])} (valgt av deg med "
            f"«{PROG} browser {browser_report['choice']}» eller i oppsettet)"
        )
    for item in browser_report["items"]:
        status = item["status"]
        lines.append(_line(_TAGS[status], item["name"]))
        lines += _detail("Sti: ", item["path"])
        lines += _detail("Grunn: ", item["reason"])
        lines += _detail("Råd: ", item["advice"])
    if browser_report["status"] == "error":
        lines.append(_line("FEIL", "Ingen nettleser kan brukes til innlogging."))
        lines += _detail("Råd: ", browser_report["advice"])
    if browser_report["note"]:
        lines.append(f"  Merk: {browser_report['note']}")

    passwords = report["passwords"]
    lines += ["", "Passordlagring"]
    if passwords["status"] == "ok":
        lines.append(_line("OK", f"{passwords['store']} er tilgjengelig."))
    else:
        lines.append(_line("MERK", f"{passwords['store']} er ikke tilgjengelig."))
        lines += _detail("Grunn: ", passwords["reason"])
        lines += _detail("Råd: ", passwords["advice"])

    data = report["data"]
    lines += [
        "",
        "Datamappe",
        f"  Datarot: {_ROOT_SOURCE_TEXT[data['source']]}",
        _line(_TAGS[data["status"]], data["reason"] or ""),
    ]
    lines += _detail("Råd: ", data["advice"])

    lines += ["", "AI-klienter"]
    for client in report["clients"]:
        tag = "-" if client["status"] == "missing" else _TAGS[client["status"]]
        lines.append(_line(tag, f"{client['name']}: {client['reason']}"))
        lines += _detail("Fil: ", client["config"])
        lines += _detail("Råd: ", client["advice"])

    display = report["display"]
    if display is not None:
        lines += ["", "Skjerm", _line(_TAGS[display["status"]], display["reason"])]
        lines += _detail("Råd: ", display["advice"])

    services = report["services"]
    lines += ["", "Tjenester som er slått på"]
    for item in services["items"]:
        stored = {True: "ja", False: "nei", None: "ukjent"}[item["stored_login"]]
        lines.append(f"  {item['name']}: lagret innlogging: {stored}")
    if services["advice"]:
        lines.append(f"  {services['advice']}")
    return "\n".join(lines) + "\n"


def root_source(
    root_option: str | None, profile: str | None, environ: Mapping[str, str] | None = None
) -> str:
    """Where the data root comes from, in the order the command line uses."""

    environ = os.environ if environ is None else environ
    if root_option is not None:
        return ROOT_OPTION
    if profile is not None:
        return ROOT_PROFILE
    if environ.get("FAMILIEKILDER_ROOT"):
        return ROOT_ENVIRONMENT
    return ROOT_DEFAULT


__all__ = ["Redactor", "render", "root_source", "run"]
