"""Command line for the open source-connector side.

Covers the global options, ``configure``, ``auth``, ``ist-auth``, ``onboard
--bundle``, ``connections``, ``password`` and ``mcp serve``, and with setup
also ``browser``, ``doctor``, ``setup``, ``remote`` and ``deploy``.
A downstream CLI adds its own commands and flags through ``CliExtension``
without this module knowing about them; nothing here imports such a CLI.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .auth_cli import API_AUTH_SOURCES, _json, _run_generic_auth
from .console import protect_console
from .core import FamiliekilderError
from .live_config import CATEGORIES, SOURCES, Config
from .login_passwords import KINDS

PROG = "familiekilder-mcp"
DESCRIPTION = "Lokale kildeoppslag via MCP"


@dataclass(frozen=True)
class Program:
    """How messages outside argparse help name the running program.

    ``name`` appears in sentences ("... enn familiekilder-mcp bruker"), ``command``
    in commands to run next ("familiekilder-mcp auth spond browser-bootstrap").
    Handlers find it as ``args.program``.
    """

    name: str = PROG
    command: str = PROG
    allow_writes: bool = False
    allow_setup: bool = True


PROGRAM = Program()


class CliExtension:
    """Hooks for a CLI built on top of this one; override what you need.

    ``build_parser`` calls the hooks in the order the commands appear in help:
    ``before_command`` just before each of this module's commands is added,
    ``extend_command`` once that command's parser exists (before this module
    adds its own arguments, so extension arguments come first in help), and
    ``after_commands`` after the last one.

    A hook registers how its commands run with
    ``parser.set_defaults(handler=...)``; a handler is called as
    ``handler(args, config)`` and its return value becomes the exit code.
    Setting a handler in ``extend_command`` replaces this module's handler for
    that command. ``parser.set_defaults(create_root=False)`` keeps the data
    root from being created before the handler runs, and
    ``parser.set_defaults(load_config=False)`` gives the handler a config with
    the data root and no services, without reading the stored configuration.
    """

    def before_command(self, name: str, subparsers: Any) -> None:
        """Add commands that should be listed just before ``name``."""

    def extend_command(
        self,
        name: str,
        parser: argparse.ArgumentParser,
        groups: Mapping[str, Any],
    ) -> None:
        """Add flags to command ``name`` ("mcp serve" for the MCP server).

        ``groups`` holds the command's named argument groups, e.g. the
        mutually exclusive ``"source"`` group of ``onboard``.
        """

    def after_commands(self, subparsers: Any) -> None:
        """Add commands that should be listed after all of this module's."""


def source_server_api() -> tuple[Any, Any]:
    """Return ``(build_server, Runtime)`` for the local source MCP server."""

    from .source_server import Runtime, build_server

    return build_server, Runtime


def serve_stdio(server: Any, runtime: Any) -> int:
    """Run an MCP server on stdio and close its runtime afterwards."""

    try:
        server.run(transport="stdio")
    finally:
        runtime.close()
    return 0


def build_parser(
    extensions: Iterable[CliExtension] = (),
    *,
    prog: str = PROG,
    description: str = DESCRIPTION,
    program: Program = PROGRAM,
    version: str | None = __version__,
) -> argparse.ArgumentParser:
    extensions = tuple(extensions)

    def before(name: str) -> None:
        for extension in extensions:
            extension.before_command(name, sub)

    def extend(name: str, parser: argparse.ArgumentParser, **groups: Any) -> None:
        for extension in extensions:
            extension.extend_command(name, parser, groups)

    parser = argparse.ArgumentParser(prog=prog, description=description)
    if version is not None:
        parser.add_argument("--version", action="version", version=f"%(prog)s {version}")
    parser.set_defaults(create_root=True, program=program)
    default_directory = "familiekilder-mcp" if program.allow_setup else "Familiekilder"
    root_help = f"Datarot; standard ~/Library/Application Support/{default_directory}"
    if program.allow_setup:
        root_help += (
            " på Mac, %%LOCALAPPDATA%%\\familiekilder-mcp på Windows og "
            "~/.local/share/familiekilder-mcp på Linux"
        )
    parser.add_argument("--root", help=root_help)
    parser.add_argument("--profile", help="Separat personlig profil, uten arv fra standardprofilen")
    sub = parser.add_subparsers(dest="command", required=True)

    before("password")
    password_help = (
        "Profilens passord i systemets nøkkelring"
        if program.allow_setup
        else "Profilens passord i macOS-nøkkelringen"
    )
    passwords = sub.add_parser("password", help=password_help)
    passwords.set_defaults(handler=_run_password)
    extend("password", passwords)
    passwords.add_argument("password_command", choices=("set", "status", "delete"))
    passwords.add_argument("password_kind", choices=KINDS)

    before("mcp")
    mcp = sub.add_parser("mcp").add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp.add_parser("serve")
    mcp_serve.set_defaults(handler=_run_mcp_serve)
    extend("mcp serve", mcp_serve)
    if program.allow_setup:
        _add_http_arguments(mcp_serve)

    before("connections")
    connections = sub.add_parser("connections")
    connections.set_defaults(handler=_run_connections)
    extend("connections", connections)

    before("configure")
    configure = sub.add_parser("configure")
    configure.set_defaults(handler=_run_configure)
    extend("configure", configure)
    configure.add_argument("source", choices=SOURCES)
    configure.add_argument("--disable", action="store_true")
    if program.allow_writes:
        configure.add_argument("--write", nargs="*", choices=CATEGORIES, default=[])
    else:
        configure.set_defaults(write=[])

    before("auth")
    auth = sub.add_parser("auth", help="Administrer en lokal, fornybar API-økt")
    auth.set_defaults(handler=_run_auth)
    extend("auth", auth)
    _add_auth_arguments(auth, browser_choice=program.allow_setup)

    before("onboard")
    onboard = sub.add_parser(
        "onboard",
        help="Verifiser og provisioner signerte providerprofiler",
    )
    onboard.set_defaults(handler=_run_onboard_bundle)
    onboard_source = onboard.add_mutually_exclusive_group(required=True)
    extend("onboard", onboard, source=onboard_source)
    onboard_source.add_argument(
        "--bundle",
        help="Privat 0600-bundlefil under .private som offline reserve",
    )

    before("ist-auth")
    ist_auth = sub.add_parser("ist-auth", help="Konfigurer og administrer IST Home OAuth")
    ist_auth.set_defaults(handler=_run_ist_auth)
    extend("ist-auth", ist_auth)
    _add_ist_auth_arguments(ist_auth)

    if program.allow_setup:
        browser = sub.add_parser(
            "browser",
            help="Vis eller velg nettleseren innloggingene åpnes i",
        )
        browser.set_defaults(handler=_run_browser)
        browser.add_argument(
            "browser_choice",
            nargs="?",
            choices=("brave", "chrome", "edge", "auto"),
            help="auto bruker den første installerte av Brave, Chrome og Edge",
        )

    if program.allow_setup:
        doctor = sub.add_parser(
            "doctor",
            help="Forhåndssjekk av maskinen uten nettverksoppslag",
            description=(
                "Kontrollerer nettlesere, passordlagring, datamappen, AI-klientene og "
                "tjenestene. Rapporten har ingen passord, tokens eller brukernavn og kan limes "
                "inn i en feilmelding. Avslutter med 1 når noe som trengs for innlogging, mangler."
            ),
        )
        doctor.set_defaults(handler=_run_doctor, create_root=False, load_config=False)
        doctor.add_argument("--json", action="store_true", help="Skriv rapporten som JSON")

    if program.allow_setup:
        setup = sub.add_parser("setup", help="Åpne lokalt oppsett i nettleseren")
        setup.set_defaults(handler=_run_setup)
        setup.add_argument("--port", type=int, default=0, help="Lokal port; standard velger en ledig")
        setup.add_argument("--no-open", action="store_true", help="Vis adressen uten å åpne nettleseren")

    if program.allow_setup:
        _add_remote_commands(sub)

    for extension in extensions:
        extension.after_commands(sub)
    return parser


def _add_http_arguments(serve: argparse.ArgumentParser) -> None:
    serve.add_argument(
        "--http",
        action="store_true",
        help="Server over HTTP (streamable HTTP) med innlogging foran, for en alltid-på-vert",
    )
    serve.add_argument("--host", default="127.0.0.1", help="Adresse å lytte på med --http")
    serve.add_argument("--port", type=int, default=8080, help="Port med --http; standard 8080")


def _add_remote_commands(sub: Any) -> None:
    remote = sub.add_parser("remote", help="Alltid-på-vert: oppsett, flytting og tilganger")
    remote_commands = remote.add_subparsers(dest="remote_command", required=True)
    init = remote_commands.add_parser(
        "init", help="Lag eierkode og installasjons-ID for en egen vert (Docker)"
    )
    init.add_argument("--env-file", required=True, help="Ny privat miljøfil for docker --env-file")
    init.add_argument("--public-url", required=True, help="Vertens offentlige HTTPS-adresse")
    init.set_defaults(handler=_run_remote_init, create_root=False)
    move = remote_commands.add_parser(
        "move", help="Flytt innloggingene fra denne maskinen til verten (eierkoden leses fra terminalen)"
    )
    move.add_argument("url", help="Vertens adresse, for eksempel https://navn.fly.dev")
    move.add_argument(
        "--source", action="append", dest="sources", help="Bare denne kilden; kan gjentas"
    )
    move.set_defaults(handler=_run_remote_move)
    clients = remote_commands.add_parser("clients", help="Vis tilkoblede apper på verten")
    clients.set_defaults(handler=_run_remote_clients)
    revoke = remote_commands.add_parser("revoke", help="Trekk tilbake tilgang på verten")
    which = revoke.add_mutually_exclusive_group(required=True)
    which.add_argument("--all", action="store_true", help="Alle apper må koble til på nytt")
    which.add_argument("--client", help="Bare denne appen (client_id fra «remote clients»)")
    revoke.set_defaults(handler=_run_remote_revoke)

    deploy = sub.add_parser("deploy", help="Veiledet oppsett av en alltid-på-vert")
    deploy_targets = deploy.add_subparsers(dest="deploy_target", required=True)
    fly = deploy_targets.add_parser("fly", help="Fly.io med flyctl (nøyaktig én maskin)")
    fly.add_argument("--app", help="Appnavn; standard familiekilder- og tilfeldige tegn")
    fly.add_argument("--region", default="arn", help="Fly-region; standard arn (Stockholm)")
    fly.add_argument("--org", default="personal", help="Fly-organisasjon; standard personal")
    fly.add_argument(
        "--new-owner-code", action="store_true", help="Lag ny eierkode for en eksisterende app"
    )
    fly.add_argument(
        "--word",
        action="store_true",
        help=(
            "Les Word-vedlegg på verten. Bygger med LibreOffice, så bildet blir over "
            "dobbelt så stort. Gjenta valget ved hver utrulling"
        ),
    )
    choice = fly.add_mutually_exclusive_group()
    choice.add_argument("--move", dest="move", action="store_true", default=None,
                        help="Flytt innloggingene etter utrulling uten å spørre")
    choice.add_argument("--no-move", dest="move", action="store_false",
                        help="Ikke flytt innloggingene nå")
    fly.set_defaults(handler=_run_deploy_fly)


def _add_browser_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--browser",
        choices=("brave", "chrome", "edge"),
        help="Nettleser for denne innloggingen; standard er valget fra «browser»",
    )


def _add_auth_arguments(
    auth: argparse.ArgumentParser, *, browser_choice: bool = False
) -> None:
    auth.add_argument("source", choices=API_AUTH_SOURCES, metavar="SOURCE")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_configure = auth_commands.add_parser(
        "configure",
        help="Importer privat klientmetadata som aldri skal ligge i Git",
    )
    auth_configure.add_argument("--client-file", required=True)
    auth_commands.add_parser("login", help="Start manuell leverandørinnlogging der dette støttes")
    browser_login = auth_commands.add_parser(
        "browser-login",
        help="Fang custom-scheme callback lokalt i en isolert nettleserprofil",
    )
    if browser_choice:
        _add_browser_argument(browser_login)
    refresh_parser = auth_commands.add_parser("refresh", help="Forny og valider API-økten nå")
    refresh_parser.add_argument("--if-needed", action="store_true", help="Forny bare ved behov")
    auth_commands.add_parser("health", help="Vis hemmelighetsfri levetidshistorikk uten nettverk")
    auth_commands.add_parser(
        "maintain", help="Gjenopprett lagret svar eller forny nær kjent refresh-utløp"
    )
    token = auth_commands.add_parser(
        "token",
        help="Koble til med et personlig tilgangstoken (Hoopit); leses skjult eller fra stdin",
    )
    token.add_argument(
        "--stdin", action="store_true", help="Les tokenet fra stdin, for eksempel fra en fil"
    )
    token.add_argument(
        "--replace-existing", action="store_true", help="Bytt et token som allerede er lagret"
    )
    auth_commands.add_parser("status", help="Vis lokal auth-status uten nettverkskall")
    auth_commands.add_parser(
        "logout",
        help="Slett lokalt; tilbakekall også der leverandøren støtter det",
    )
    auth_import = auth_commands.add_parser(
        "import-session",
        help="Importer en separat privat API-økt der dette støttes",
    )
    auth_import.add_argument(
        "--session-file",
        required=True,
        help="Privat JSON-fil under .private; tokenverdier skal aldri stå i argumentet",
    )
    auth_import.add_argument(
        "--replace-existing",
        action="store_true",
        help="Erstatt en eksisterende økt etter eksakt bekreftelse",
    )
    auth_browser = auth_commands.add_parser(
        "browser-bootstrap",
        help="Importer økten direkte fra en isolert, midlertidig nettleserprofil",
    )
    if browser_choice:
        _add_browser_argument(auth_browser)
    auth_browser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Erstatt en eksisterende økt etter eksakt bekreftelse",
    )


def _add_ist_auth_arguments(ist_auth: argparse.ArgumentParser) -> None:
    ist_auth_commands = ist_auth.add_subparsers(dest="ist_auth_command", required=True)
    configure = ist_auth_commands.add_parser(
        "configure", help="Importer privat IST-klientkonfigurasjon"
    )
    configure.add_argument(
        "--client-file",
        required=True,
        help="Privat JSON-fil som aldri skal ligge i Git",
    )
    export = ist_auth_commands.add_parser(
        "export-mac-client",
        help="Eksporter bare klientkonfigurasjonen fra IST Home-appen på Mac",
    )
    export.add_argument(
        "--output",
        required=True,
        help="Ny privat målfil; eksisterende filer overskrives ikke",
    )
    ist_auth_commands.add_parser("login", help="Start manuell ID-porten-innlogging med PKCE")
    ist_auth_commands.add_parser("refresh", help="Forny og fullvalider IST-økten nå")
    ist_auth_commands.add_parser("status", help="Vis lokal IST OAuth-status uten nettverkskall")
    ist_auth_commands.add_parser("logout", help="Tilbakekall og slett den lokale IST-økten")
    import_session_parser = ist_auth_commands.add_parser(
        "import-session",
        help="Importer og valider en privat IST Home-økt fra stdin",
    )
    import_session_parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Tillat erstatning først etter full fjernvalidering",
    )
    import_session_parser.add_argument(
        "--accept-unofficial-pilot",
        action="store_true",
        help="Registrer eksplisitt førstegangssamtykke for uoffisiell IST OAuth",
    )


# Commands that log in or store passwords; they belong on the user's computer.
_LOGIN_COMMANDS = frozenset({"login", "browser-login", "browser-bootstrap", "import-session"})


def _run_password(args: argparse.Namespace, config: Config) -> int:
    from .hosting import refuse_on_remote_host
    from .login_passwords import run_password_command

    if args.password_command == "set":
        refuse_on_remote_host("Lagring av passord")

    _json(run_password_command(config.root, args.password_kind, args.password_command))
    return 0


def _run_configure(args: argparse.Namespace, config: Config) -> int:
    services = dict(config.services)
    if args.disable:
        services.pop(args.source, None)
    else:
        services[args.source] = frozenset(args.write)
    config = Config(config.root, services)
    config.save()
    _json({"services": {s: sorted(w) for s, w in services.items()}})
    return 0


def _run_onboard_bundle(args: argparse.Namespace, config: Config) -> int:
    from .provider_bundle import onboard_from_file

    _json(onboard_from_file(config.root, Path(args.bundle), command=args.program.command))
    return 0


def _run_ist_auth(args: argparse.Namespace, config: Config) -> int:
    from .auth_cli import run_ist_auth

    if args.ist_auth_command in _LOGIN_COMMANDS:
        from .hosting import refuse_on_remote_host

        refuse_on_remote_host("Innlogging")
    if args.ist_auth_command != "export-mac-client":
        config.require("ist-home")
    return run_ist_auth(args, config.root)


def _run_auth(args: argparse.Namespace, config: Config) -> int:
    if args.auth_command in _LOGIN_COMMANDS:
        from .hosting import refuse_on_remote_host

        refuse_on_remote_host("Innlogging")
    if args.auth_command != "configure" or not args.program.allow_setup:
        config.require(args.source)
    if args.source == "gmail":
        from .gmail_auth import run as gmail_auth

        return gmail_auth(args, config.root)
    return _run_generic_auth(args, config.root)


def _run_connections(args: argparse.Namespace, config: Config) -> int:
    from .source_server import ReadOnlyRuntime

    runtime_class = ReadOnlyRuntime
    runtime = runtime_class(config)
    try:
        _json(runtime.connections())
    finally:
        runtime.close()
    return 0


def _run_mcp_serve(args: argparse.Namespace, config: Config) -> int:
    from .client_defaults import refresh_stored_defaults

    if getattr(args, "http", False):
        from .hosting import host_settings

        # Refuse before anything else when the login in front is not set up.
        host_settings()
    # A newer built-in setup replaces one the setup wizard stored, so a package
    # update reaches the user at the next start. Never raises; logs to stderr.
    refresh_stored_defaults(config.root)
    if getattr(args, "http", False):
        from .remote_http import serve_http

        return serve_http(config, host=args.host, port=args.port)
    build_server, _ = source_server_api()
    server, runtime = build_server(config, read_only=True)
    return serve_stdio(server, runtime)


def _read_owner_code() -> str:
    """From the terminal without echo, or one line on stdin for an agent."""

    if sys.stdin.isatty():
        from getpass import getpass

        return getpass("Eierkode: ")
    return sys.stdin.readline().strip()


def _run_remote_init(args: argparse.Namespace, config: Config) -> int:
    from .deploy_fly import show_owner_code
    from .hosting import (
        INSTALLATION_ID_ENV,
        OWNER_CODE_HASH_ENV,
        PUBLIC_URL_ENV,
        hash_owner_code,
        new_installation_id,
        new_owner_code,
        normalize_public_url,
    )

    url = normalize_public_url(args.public_url)
    target = Path(args.env_file).expanduser()
    code = new_owner_code()
    lines = (
        f"{PUBLIC_URL_ENV}={url}\n"
        f"{OWNER_CODE_HASH_ENV}={hash_owner_code(code)}\n"
        f"{INSTALLATION_ID_ENV}={new_installation_id()}\n"
    )
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise FamiliekilderError("Miljøfilen finnes allerede; ingenting er overskrevet") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(lines)
    sys.stdout.write(f"Miljøfilen er laget: {target}\n")
    show_owner_code(
        code,
        out=sys.stdout,
        interactive=sys.stdout.isatty(),
        fallback=target.with_name(target.name + ".eierkode"),
    )
    return 0


def _run_remote_move(args: argparse.Namespace, config: Config) -> int:
    from .hosting import refuse_on_remote_host
    from .session_move import move_to

    refuse_on_remote_host("Flytting av innlogginger")
    _json(move_to(config.root, args.url, _read_owner_code(), sources=args.sources))
    return 0


def _run_remote_clients(args: argparse.Namespace, config: Config) -> int:
    from .remote_auth import clients_command

    _json(clients_command(config.root))
    return 0


def _run_remote_revoke(args: argparse.Namespace, config: Config) -> int:
    from .remote_auth import revoke_command

    _json(revoke_command(config.root, client_id=None if args.all else args.client))
    return 0


def _run_deploy_fly(args: argparse.Namespace, config: Config) -> int:
    from .deploy_fly import deploy
    from .hosting import refuse_on_remote_host

    refuse_on_remote_host("Utrulling")
    result = deploy(
        root=config.root,
        app=args.app,
        region=args.region,
        org=args.org,
        move=args.move,
        new_code=args.new_owner_code,
        word=args.word,
    )
    _json(result)
    return 0


def _run_browser(args: argparse.Namespace, config: Config) -> int:
    from .browsers import installed, save_browser, saved_browser

    if args.browser_choice is not None:
        save_browser(config.root, None if args.browser_choice == "auto" else args.browser_choice)
    found = [app.id for app in installed()]
    chosen = saved_browser(config.root)
    _json({
        "chosen": chosen or "auto",
        "installed": found,
        "used": chosen if chosen else (found[0] if found else None),
    })
    return 0


def _run_doctor(args: argparse.Namespace, config: Config) -> int:
    import json

    from .doctor import render, root_source
    from .doctor import run as run_doctor

    report = run_doctor(
        config.root,
        root_source=root_source(args.root, args.profile),
    )
    if args.json:
        # ASCII only, so any console code page can show it.
        print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        sys.stdout.write(render(report))
    sys.stdout.flush()
    return 0 if report["ok"] else 1


def _run_setup(args: argparse.Namespace, config: Config) -> int:
    from .doctor import root_source
    from .hosting import refuse_on_remote_host
    from .setup_web import serve

    refuse_on_remote_host("Oppsett og innlogging")
    return serve(
        config.root,
        port=args.port,
        open_browser=not args.no_open,
        root_source=root_source(args.root, args.profile),
    )


def run(args: argparse.Namespace) -> Any:
    """Run a parsed command line; returns the handler's exit code."""

    options = {"profile": getattr(args, "profile", None)}
    if args.program.allow_setup:
        from .hosting import default_data_root

        options["default_root"] = Path(os.environ.get("FAMILIEKILDER_ROOT") or default_data_root())
    if getattr(args, "load_config", True):
        config = Config.load(args.root, **options)
    else:
        from .profiles import resolve_root

        config = Config(resolve_root(args.root, **options), {})
    if args.create_root:
        config.root.mkdir(parents=True, exist_ok=True)
    return args.handler(args, config)


def main(
    argv: list[str] | None = None,
    *,
    extensions: Iterable[CliExtension] = (),
    prog: str = PROG,
    description: str = DESCRIPTION,
    program: Program = PROGRAM,
    version: str | None = __version__,
) -> Any:
    # Before anything is printed, including help and argument errors.
    protect_console()
    parser = build_parser(
        extensions, prog=prog, description=description, program=program, version=version
    )
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (FamiliekilderError, ValueError) as exc:
        print(f"{parser.prog}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
