from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any

from .core import FamiliekilderError, SOURCES
from .connection_status import auth_module as _auth_module
from .connection_status import has_local_session as _has_local_session
from .sources import REGISTRY


API_AUTH_SOURCES = SOURCES
_SOURCE_LABELS = {name: source.label for name, source in REGISTRY.items()}
_AUTH_CAPABILITIES = {
    name: source.auth_commands | {"health", "maintain"}
    for name, source in REGISTRY.items()
    if source.auth_commands
}


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _require_hidden_tty() -> None:
    """Fail before authorization if a no-echo callback prompt is unavailable."""

    try:
        import termios

        descriptor = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0))
    except (ImportError, OSError):
        raise FamiliekilderError(
            "Innlogging krever en ekte SSH-terminal; callbacken kan ikke leses skjult her"
        ) from None
    try:
        if not os.isatty(descriptor):
            raise FamiliekilderError(
                "Innlogging krever en ekte SSH-terminal; callbacken kan ikke leses skjult her"
            )
        termios.tcgetattr(descriptor)
    except (OSError, termios.error):
        raise FamiliekilderError(
            "Innlogging krever en terminal med deaktivert ekko for callbacken"
        ) from None
    finally:
        os.close(descriptor)


def _read_hidden_callback() -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass("Callback-URL (skjult): ")
        except (EOFError, OSError, getpass.GetPassWarning):
            raise FamiliekilderError(
                "Callbacken kunne ikke leses uten ekko; start ny innlogging i en ekte SSH-terminal"
            ) from None


def _require_private_session_stdin() -> None:
    if sys.stdin.isatty():
        raise FamiliekilderError(
            "Sesjonsimport krever pipe eller stdin-omdirigering; ikke lim tokens i terminalen"
        )


def _read_bounded_session_stdin() -> str:
    """Read a private session only from a non-interactive pipe/redirection."""

    _require_private_session_stdin()
    payload = sys.stdin.read((1024 * 1024) + 1)
    if len(payload) > 1024 * 1024:
        raise FamiliekilderError("IST-sesjonen på stdin er større enn tillatt")
    return payload


def _run_generic_auth(args: argparse.Namespace, root: Path | None) -> int:
    label = _SOURCE_LABELS[args.source]
    if args.auth_command not in _AUTH_CAPABILITIES[args.source]:
        if (args.auth_command == "login"
                and "browser-bootstrap" in _AUTH_CAPABILITIES[args.source]):
            detail = "bruk browser-bootstrap med en isolert, midlertidig Brave-profil"
        else:
            detail = "kommandoen støttes ikke for denne kilden"
        raise FamiliekilderError(f"{label}: {detail}")

    auth_module = _auth_module(args.source)

    if args.auth_command == "browser-bootstrap":
        existing = _has_local_session(auth_module, root)
        if existing and not args.replace_existing:
            raise FamiliekilderError(
                f"En lokal {label}-økt finnes allerede; bruk --replace-existing "
                "bare når du vil erstatte den gjennom en ny isolert innlogging"
            )
        if existing:
            accepted = input(f"Skriv ERSTATT for å erstatte den lokale {label}-økten: ")
            if accepted != "ERSTATT":
                raise FamiliekilderError(
                    "Nettleserbootstrapen ble avbrutt; eksisterende økt er beholdt"
                )
        from .browser_bootstrap import bootstrap_source

        _json(
            bootstrap_source(
                root,
                args.source,
                replace_existing=existing and args.replace_existing,
            )
        )
        return 0

    if args.auth_command == "configure":
        _json(auth_module.configure_client(root, Path(args.client_file)))
        return 0
    if args.auth_command == "import-session":
        if _has_local_session(auth_module, root):
            if not args.replace_existing:
                replacement = (
                    "en ny eksport av den eksisterende Firebase-økten"
                    if args.source == "hoopit"
                    else "en ny separat økt"
                )
                raise FamiliekilderError(
                    f"En lokal {label}-økt finnes allerede; bruk --replace-existing bare med {replacement}"
                )
            accepted = input(f"Skriv ERSTATT for å erstatte den lokale {label}-økten: ")
            if accepted != "ERSTATT":
                raise FamiliekilderError(
                    "Sesjonsimporten ble avbrutt; eksisterende økt er beholdt"
                )
        _json(
            auth_module.import_session(
                root,
                Path(args.session_file),
                replace_existing=args.replace_existing,
            )
        )
        return 0
    if args.auth_command in ("health", "maintain"):
        from .auth_lifecycle import report, maintenance

        _json(
            report(root, args.source)
            if args.auth_command == "health"
            else maintenance(root, args.source, auth_module)
        )
        return 0
    if args.auth_command == "status":
        _json(auth_module.auth_status(root))
        return 0
    if args.auth_command == "refresh":
        if getattr(args, "if_needed", False):
            from .auth_lifecycle import maintenance

            _json(maintenance(root, args.source, auth_module, access_needed=True))
        else:
            _json(auth_module.refresh_session(root))
        return 0
    if args.auth_command == "verify-direct":
        from .hoopit import verify_direct_access

        _json(verify_direct_access(root))
        return 0
    if args.auth_command == "logout":
        if not REGISTRY[args.source].logout_revokes:
            prompt = (
                f"Skriv LOGG UT for å slette den lokale {label}-økten "
                "(leverandørøkten tilbakekalles ikke): "
            )
        else:
            prompt = f"Skriv LOGG UT for å tilbakekalle og slette {label}-økten: "
        accepted = input(prompt)
        if accepted != "LOGG UT":
            raise FamiliekilderError("Utloggingen ble avbrutt")
        _json(auth_module.logout(root))
        return 0

    replace_existing = False
    if _has_local_session(auth_module, root):
        print(
            f"En lokal {label}-økt finnes allerede. Den beholdes dersom den nye "
            "innloggingen ikke blir fullstendig verifisert."
        )
        accepted = input("Skriv ERSTATT for å starte ny innlogging: ")
        if accepted != "ERSTATT":
            raise FamiliekilderError(
                "Innloggingen ble avbrutt; eksisterende økt er beholdt"
            )
        replace_existing = True

    if not auth_module.consent_status(root)["current"]:
        print(
            f"{label}-API-piloten er uoffisiell. Telefonbekreftelsen i BankID forblir manuell, og den "
            f"lokale økten kan ha bredere rettigheter enn {args.program.name} bruker."
        )
        accepted = input("Skriv JEG FORSTÅR for å fortsette: ")
        if accepted != "JEG FORSTÅR":
            raise FamiliekilderError("Innloggingen ble avbrutt uten samtykke")
        auth_module.record_consent(root)

    if args.auth_command != "browser-login":
        _require_hidden_tty()
    authorization_url, transaction = auth_module.begin_login(root)
    if args.auth_command == "browser-login":
        from .browser_bootstrap import capture_oauth_callback

        config = auth_module.load_client_config(root)
        print(
            f"Start {label}-innloggingen i det isolerte Brave-vinduet og godkjenn på telefonen. "
            f"{args.program.name} fanger callbacken lokalt uten å vise den."
        )
        if args.source == "ist-home":
            print(
                "Velg Norge øverst på SkolID-siden før du starter norsk BankID-innlogging."
            )
        callback_url = capture_oauth_callback(
            root,
            authorization_url=authorization_url,
            expected_redirect_uri=config.redirect_uri,
        )
    else:
        if args.source == "ist-home":
            print(
                "Åpne denne lenken i Windows-nettleseren og fullfør ID-porten manuelt:"
            )
            print(authorization_url)
            print(
                "Når callback-helperen åpnes, kopierer du callbacken og limer den inn under."
            )
        else:
            print(f"Åpne denne lenken i Brave og fullfør {label}-innloggingen manuelt:")
            print(authorization_url)
            print(
                "Når app-callbacken åpnes eller vises, kopierer du hele URL-en "
                "og limer den inn under."
            )
        callback_url = _read_hidden_callback()
    _json(
        auth_module.login_with_callback(
            root,
            transaction,
            callback_url,
            replace_existing=replace_existing,
        )
    )
    return 0


def run_ist_auth(args, root):
    from .ist_oauth import (
        auth_status,
        begin_login,
        configure_client,
        consent_status,
        export_mac_client_config,
        import_session,
        login_with_callback,
        logout,
        record_consent,
        refresh_session,
        session_path,
    )

    if args.ist_auth_command == "configure":
        _json(configure_client(root, Path(args.client_file)))
    elif args.ist_auth_command == "export-mac-client":
        _json(export_mac_client_config(Path(args.output)))
    elif args.ist_auth_command == "login":
        replace_existing = False
        if session_path(root).exists():
            print(
                "En lokal IST Home-økt finnes allerede. Den beholdes dersom den nye "
                "innloggingen ikke blir fullstendig verifisert."
            )
            accepted = input("Skriv ERSTATT for å starte ny innlogging: ")
            if accepted != "ERSTATT":
                raise FamiliekilderError(
                    "Innloggingen ble avbrutt; eksisterende økt er beholdt"
                )
            replace_existing = True
        if not consent_status(root)["current"]:
            print(
                "IST OAuth-piloten er uoffisiell. Telefonbekreftelsen i BankID forblir manuell, og OAuth-tokenet kan ha "
                f"bredere IST-rettigheter enn {args.program.name} bruker."
            )
            accepted = input("Skriv JEG FORSTÅR for å fortsette: ")
            if accepted != "JEG FORSTÅR":
                raise FamiliekilderError("Innloggingen ble avbrutt uten samtykke")
            record_consent(root)
        _require_hidden_tty()
        authorization_url, transaction = begin_login(root)
        print("Åpne denne lenken i Windows-nettleseren og fullfør ID-porten manuelt:")
        print(authorization_url)
        print(
            "Når callback-helperen åpnes, kopierer du callbacken og limer den inn under."
        )
        callback_url = _read_hidden_callback()
        _json(
            login_with_callback(
                root,
                transaction,
                callback_url,
                replace_existing=replace_existing,
            )
        )
    elif args.ist_auth_command == "refresh":
        _json(refresh_session(root))
    elif args.ist_auth_command == "status":
        _json(auth_status(root))
    elif args.ist_auth_command == "logout":
        accepted = input("Skriv LOGG UT for å tilbakekalle og slette IST-økten: ")
        if accepted != "LOGG UT":
            raise FamiliekilderError("Utloggingen ble avbrutt")
        _json(logout(root))
    elif args.ist_auth_command == "import-session":
        _require_private_session_stdin()
        if not consent_status(root)["current"]:
            if not args.accept_unofficial_pilot:
                raise FamiliekilderError(
                    "Førstegangsimport krever --accept-unofficial-pilot etter at advarselen er lest"
                )
            print(
                "IST OAuth-piloten er uoffisiell. Telefonbekreftelsen i BankID forblir manuell, og OAuth-tokenet kan ha "
                f"bredere IST-rettigheter enn {args.program.name} bruker."
            )
            record_consent(root)
        payload = _read_bounded_session_stdin()
        _json(
            import_session(
                root,
                payload,
                replace_existing=args.replace_existing,
            )
        )
    return 0
