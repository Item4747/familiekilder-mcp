"""Command line for the open source-connector side.

Covers the global options, ``configure``, ``auth``, ``ist-auth``, ``onboard
--bundle``, ``connections``, ``password``, ``android-device`` and ``mcp serve``.
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
    root from being created before the handler runs.
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
    parser.add_argument(
        "--root", help=f"Datarot; standard ~/Library/Application Support/{default_directory}"
    )
    parser.add_argument("--profile", help="Separat personlig profil, uten arv fra standardprofilen")
    sub = parser.add_subparsers(dest="command", required=True)

    before("android-device")
    device = sub.add_parser("android-device", help="Bind din egen Android-enhet til valgt profil")
    device.set_defaults(handler=_run_android_device)
    extend("android-device", device)
    device.add_argument("device_command", choices=("bind",))
    device.add_argument("--adb-serial", required=True)

    before("password")
    passwords = sub.add_parser("password", help="Profilens passord i macOS-nøkkelringen")
    passwords.set_defaults(handler=_run_password)
    extend("password", passwords)
    passwords.add_argument("password_command", choices=("set", "status", "delete"))
    passwords.add_argument("password_kind", choices=KINDS)

    before("mcp")
    mcp = sub.add_parser("mcp").add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp.add_parser("serve")
    mcp_serve.set_defaults(handler=_run_mcp_serve)
    extend("mcp serve", mcp_serve)

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
    _add_auth_arguments(auth)

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
        setup = sub.add_parser("setup", help="Åpne lokalt oppsett i nettleseren")
        setup.set_defaults(handler=_run_setup)
        setup.add_argument("--port", type=int, default=0, help="Lokal port; standard velger en ledig")
        setup.add_argument("--no-open", action="store_true", help="Vis adressen uten å åpne nettleseren")

    for extension in extensions:
        extension.after_commands(sub)
    return parser


def _add_auth_arguments(auth: argparse.ArgumentParser) -> None:
    auth.add_argument("source", choices=API_AUTH_SOURCES, metavar="SOURCE")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_configure = auth_commands.add_parser(
        "configure",
        help="Importer privat klientmetadata som aldri skal ligge i Git",
    )
    auth_configure.add_argument("--client-file", required=True)
    auth_commands.add_parser("login", help="Start manuell leverandørinnlogging der dette støttes")
    auth_commands.add_parser(
        "browser-login",
        help="Fang custom-scheme callback lokalt i en isolert Brave-profil",
    )
    auth_android = auth_commands.add_parser(
        "android-login",
        help="Fang providerens custom-scheme-callback privat fra Android via ADB",
    )
    auth_android.add_argument(
        "--adb-serial",
        help="Eksakt ADB-enhet; påkrevd bare når flere enheter er tilkoblet",
    )
    refresh_parser = auth_commands.add_parser("refresh", help="Forny og valider API-økten nå")
    refresh_parser.add_argument("--if-needed", action="store_true", help="Forny bare ved behov")
    auth_commands.add_parser("health", help="Vis hemmelighetsfri levetidshistorikk uten nettverk")
    auth_commands.add_parser(
        "maintain", help="Gjenopprett lagret svar eller forny nær kjent refresh-utløp"
    )
    auth_commands.add_parser(
        "verify-direct",
        help="Kjør én Hoopit Firebase-refresh og ett lesende API-kall uten retry",
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
        help="Importer økten direkte fra en isolert, midlertidig Brave-profil",
    )
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


def _run_android_device(args: argparse.Namespace, config: Config) -> int:
    from .android_oauth import _android_tools, _select_device
    from .android_passwords import claim_device

    serial = _select_device(_android_tools()["adb"], args.adb_serial)
    claim_device(config.root, serial, bind=True)
    _json({"bound": True})
    return 0


def _run_password(args: argparse.Namespace, config: Config) -> int:
    from .login_passwords import run_password_command

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

    if args.ist_auth_command != "export-mac-client":
        config.require("ist-home")
    return run_ist_auth(args, config.root)


def _run_auth(args: argparse.Namespace, config: Config) -> int:
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
    build_server, _ = source_server_api()
    server, runtime = build_server(config, read_only=True)
    return serve_stdio(server, runtime)


def _run_setup(args: argparse.Namespace, config: Config) -> int:
    from .setup_web import serve

    return serve(config.root, port=args.port, open_browser=not args.no_open)


def run(args: argparse.Namespace) -> Any:
    """Run a parsed command line; returns the handler's exit code."""

    options = {"profile": getattr(args, "profile", None)}
    if args.program.allow_setup:
        options["default_root"] = Path(
            os.environ.get("FAMILIEKILDER_ROOT")
            or Path.home() / "Library/Application Support/familiekilder-mcp"
        )
    config = Config.load(args.root, **options)
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
