"""Local setup actions; no message writes or secret-bearing UI responses."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import secrets
import shutil
import threading
import time

from . import __version__
from .browsers import installed as installed_browsers, save_browser, saved_browser
from .client_defaults import (
    RenewalRefused,
    default_client_config,
    import_client_text,
    refresh_stored_default,
    renewal_pending,
    store_default,
)
from .connection_status import auth_module, connection_state, has_local_session
from .session_move import moved_sources
from .live_config import Config
from .setup_clients import check_connection, client_state, install_client
from .sources import REGISTRY


class SetupError(ValueError):
    pass


class _JobFailure(SetupError):
    """A job failure whose message is safe and more useful than the generic text."""


# How the wizard sets up a source that has no client setup yet.
_SETUP_WITHOUT_DEFAULT = {"flyt": "municipality", "gmail": "file"}
MAX_MUNICIPALITY_LENGTH = 200


def setup_kind(source: str) -> str:
    if REGISTRY[source].login == "token":
        return "token"
    if default_client_config(source) is not None:
        return "automatic"
    return _SETUP_WITHOUT_DEFAULT.get(source, "unavailable")


@dataclass
class Job:
    id: str
    source: str
    phase: str = "working"
    message: str = "Starter …"
    complete: threading.Event = field(default_factory=threading.Event)
    cancel: threading.Event = field(default_factory=threading.Event)
    # Set when the job logged out to take a newer built-in setup.
    logged_out: bool = False


class SetupService:
    def __init__(self, root: Path, *, home: Path | None = None, root_source: str | None = None):
        from .doctor import root_source as find_root_source

        self.root = root
        self.home = Path.home() if home is None else home
        # How the data root was chosen, for the pre-flight report.
        self.root_source = root_source or find_root_source(None, None)
        self.lock = threading.RLock()
        self.job: Job | None = None
        self.tested = False
        self.closed = False
        self.worker: threading.Thread | None = None
        # The public Flyt list is fetched once per wizard; a failure is retried.
        self._municipalities_lock = threading.Lock()
        self._municipalities: list[dict[str, str]] | None = None

    def _idle(self):
        if self.closed:
            raise SetupError("Oppsettet er lukket.")
        if self.job and self.job.phase in {"working", "waiting"}:
            raise SetupError("Fullfør eller avbryt det pågående steget først.")

    def state(self) -> dict:
        with self.lock:
            rows = []
            config = Config.load(self.root)
            moved = moved_sources(self.root)
            for source in REGISTRY.values():
                status = {}
                try:
                    status = auth_module(source.name).auth_status(self.root)
                    state = connection_state(status)
                    existing = bool(status.get("session_present"))
                except Exception:
                    state, existing = "unavailable", False
                if source.name in moved:
                    state = "moved"
                row = {
                    "id": source.name, "label": source.label, "description": source.description,
                    "state": state, "enabled": source.name in config.services,
                    "has_session": existing,
                    **({"moved_to": moved[source.name].get("moved_to")} if source.name in moved else {}),
                    "setup": self._setup_kind(source.name),
                    # A newer built-in setup the source refuses while logged in.
                    "renewal_pending": self._renewal_pending(source.name),
                }
                if source.name == "flyt":
                    row["municipality"] = self._stored_municipality()
                if status.get("detail") and existing and state != "connected":
                    # Fixed source text when a stored login needs attention,
                    # for example that a token was withdrawn.
                    row["detail"] = status["detail"]
                rows.append(row)
            job = None if self.job is None else {
                "id": self.job.id, "source": self.job.source, "phase": self.job.phase,
                "message": self.job.message,
            }
            return {
                "version": __version__, "services": rows, "job": job, "tested": self.tested,
                "clients": [client_state(client, self.root, self.home) for client in ("claude", "codex")],
                "browsers": self._browsers(),
                "dependencies": {"browser": bool(installed_browsers()),
                                 "pdf": bool(shutil.which("pdftotext") and shutil.which("pdftoppm")),
                                 "word": bool(shutil.which("soffice") or Path("/Applications/LibreOffice.app").exists())},
            }

    def _browsers(self) -> dict:
        """The browsers found on this machine and the one logins open in."""

        found = installed_browsers()
        chosen = saved_browser(self.root)
        used = chosen if chosen else (found[0].id if found else None)
        return {
            "installed": [{"id": app.id, "name": app.name} for app in found],
            "chosen": chosen or "auto",
            "used": used,
        }

    def doctor(self) -> dict:
        """The pre-flight report, as text and as data; the same as «familiekilder-mcp doctor».

        Runs the browser checks without logging in, storing data or contacting the sources.
        """

        from .doctor import render, run

        report = run(self.root, root_source=self.root_source, client_home=self.home)
        return {"text": render(report), "report": report}

    def set_browser(self, browser):
        if browser not in {"auto", "brave", "chrome", "edge"}:
            raise SetupError("Velg Brave, Google Chrome, Microsoft Edge eller automatisk.")
        with self.lock:
            self._idle()
            save_browser(self.root, None if browser == "auto" else browser)

    def _stored_municipality(self) -> dict[str, str] | None:
        """The stored Flyt municipality as id and name; nothing else from the setup."""

        from .flyt_auth import load_client_config

        try:
            stored = load_client_config(self.root).municipality
        except Exception:
            return None
        return {"id": stored.organization_number, "name": stored.display_name}

    def _renewal_pending(self, source: str) -> bool:
        """A renewal waits and the source has a session, so a re-login logs out first."""

        try:
            module = auth_module(source)
            return renewal_pending(self.root, source, module=module) and has_local_session(
                module, self.root
            )
        except Exception:
            return False

    @staticmethod
    def _setup_kind(source: str) -> str:
        # A broken defaults file must not take the whole wizard down.
        try:
            return setup_kind(source)
        except Exception:
            return "unavailable"

    def flyt_municipalities(self) -> list[dict[str, str]]:
        """The public Flyt municipality list as id and name, sorted by name."""

        # A separate lock, so a slow fetch does not hold up the state polling.
        with self._municipalities_lock:
            if self._municipalities is None:
                from .flyt_auth import list_municipalities

                try:
                    listed = list_municipalities()
                except Exception:
                    raise SetupError("Kommunelisten fra Flyt kunne ikke hentes. Prøv igjen om litt.") from None
                rows = [{"id": item["organization_number"], "name": item["display_name"]} for item in listed]
                self._municipalities = sorted(rows, key=lambda row: (row["name"].casefold(), row["id"]))
            return [dict(row) for row in self._municipalities]

    def import_client(self, source: str, content: str) -> None:
        """Import a client file the user chose; only for sources set up with a file."""

        if (not isinstance(source, str) or source not in REGISTRY or setup_kind(source) != "file"
                or not isinstance(content, str) or len(content.encode()) > 1024 * 1024):
            raise SetupError("Velg en gyldig JSON-fil for tjenesten.")
        with self.lock:
            self._idle()
            # The file is replaced only before a login; a session keeps its client.
            try:
                session = has_local_session(auth_module(source), self.root)
            except Exception:
                session = True
            if session:
                raise SetupError(
                    f"{REGISTRY[source].label} har en lagret innlogging, så filen byttes ikke. "
                    f"Logg ut med «familiekilder-mcp auth {source} logout» først."
                )
            try:
                self._configure_client(source, content)
            except Exception:
                raise SetupError("Filen kunne ikke brukes. Kontroller at den er klientoppsettet for denne tjenesten.") from None
            self.tested = False

    def _configure_client(self, source: str, content: str) -> None:
        import_client_text(self.root, auth_module(source), content)

    def _enable(self, source):
        config = Config.load(self.root)
        services = dict(config.services)
        services[source] = frozenset()
        Config(self.root, services).save()
        self.tested = False

    def connect_token(self, source: str, token, *, replace=False) -> None:
        """Check and store a pasted personal access token in a job (Hoopit).

        The token goes straight to the source module; no state or message
        here ever contains it.
        """

        if not isinstance(source, str) or source not in REGISTRY or REGISTRY[source].login != "token":
            raise SetupError("Velg en tjeneste som kobles til med token.")
        if not isinstance(token, str) or len(token) > 4096:
            raise SetupError("Lim inn tokenet fra tjenesten.")
        with self.lock:
            self._idle()
            module = auth_module(source)
            if module.auth_status(self.root).get("session_present") and replace is not True:
                raise SetupError("Bekreft at tokenet som er lagret, kan erstattes.")
            job = Job(secrets.token_hex(12), source, message="Kontrollerer tokenet …")
            self.job = job
            self.tested = False
            self.worker = threading.Thread(
                target=self._run_token, args=(job, token, replace is True), daemon=True
            )
            self.worker.start()

    def _run_token(self, job: Job, token: str, replace: bool) -> None:
        from .core import FamiliekilderError

        try:
            auth_module(job.source).connect_token(self.root, token, replace_existing=replace)
            with self.lock:
                self._enable(job.source)
                job.phase = "done"
                job.message = "Tokenet virker og er lagret på denne maskinen."
        except FamiliekilderError as error:
            # The source's own texts say what to do and never contain the token.
            with self.lock:
                job.phase = "error"
                job.message = self._sentence(error)
        except Exception:
            with self.lock:
                job.phase = "error"
                job.message = "Tokenet kunne ikke kontrolleres. Prøv igjen om litt."

    def start(self, action: str, source: str = "", *, consent=False, replace=False, municipality=None) -> None:
        if action not in {"login", "check"} or (action == "login" and source not in REGISTRY):
            raise SetupError("Ukjent oppsettshandling.")
        if action == "login" and REGISTRY[source].login == "token":
            raise SetupError("Denne tjenesten kobles til med et token. Lim inn tokenet i veiviseren.")
        if municipality is not None and (
                action != "login" or source != "flyt" or not isinstance(municipality, str)
                or len(municipality) > MAX_MUNICIPALITY_LENGTH):
            raise SetupError("Velg en kommune fra listen.")
        municipality = (municipality or "").strip() or None
        with self.lock:
            self._idle()
            if action == "login":
                module = auth_module(source)
                status = module.auth_status(self.root)
                if source != "gmail" and consent is not True:
                    raise SetupError("Bekreft at du vil bruke den uoffisielle koblingen.")
                if status.get("configured"):
                    municipality = self._municipality_change(module, municipality, replace=replace)
                elif self._has_stored_client(module):
                    # Stored but unreadable: never replace it with the default.
                    raise SetupError(
                        "Det lagrede oppsettet for tjenesten kan ikke leses, og veiviseren "
                        f"bytter det ikke ut. Importer det på nytt med «familiekilder-mcp auth {source} "
                        "configure --client-file», eller slett det (se docs/klientoppsett.md)."
                    )
                else:
                    self._configure_default(source)
                    status = module.auth_status(self.root)
                if not status.get("configured"):
                    kind = setup_kind(source)
                    if kind == "municipality" and municipality is None:
                        raise SetupError("Velg kommune før du logger inn.")
                    if kind == "file":
                        raise SetupError("Velg filen med Google-klienten før du logger inn.")
                    if kind != "municipality":
                        raise SetupError("Denne tjenesten kan ikke settes opp i veiviseren ennå. Bruk kommandolinjen.")
                if has_local_session(module, self.root) and replace is not True:
                    raise SetupError("Bekreft at en eksisterende innlogging kan erstattes.")
            job = Job(secrets.token_hex(12), source)
            self.job = job
            self.tested = False
            self.worker = threading.Thread(
                target=self._run, args=(job, action, consent, replace),
                kwargs={"municipality": municipality}, daemon=True,
            )
            self.worker.start()

    def _municipality_change(self, module, municipality: str | None, *, replace) -> str | None:
        """The Flyt municipality to look up, or None to keep the stored one.

        The same organisation number needs no lookup and no rewrite. While Flyt
        has a session, a new municipality needs the confirmation that the login
        may be replaced; the worker then logs out before it stores the choice.
        """

        if municipality is None:
            return None
        stored = self._stored_municipality()
        if stored is not None and municipality == stored["id"]:
            return None
        if has_local_session(module, self.root) and replace is not True:
            raise SetupError(
                "Flyt har en lagret innlogging. Vil du bytte kommune, krysser du av for "
                "«Erstatt den eksisterende innloggingen». Da logges du ut av Flyt med en gang."
            )
        return municipality

    def _has_stored_client(self, module) -> bool:
        path_factory = getattr(module, "client_config_path", None)
        if not callable(path_factory):
            return False
        try:
            return path_factory(self.root).exists()
        except OSError:
            return True

    def _configure_default(self, source: str) -> None:
        """Store the built-in client setup; only called when none is stored."""

        default = default_client_config(source)
        if default is None:
            return
        try:
            # The mark lets a newer package renew this setup later.
            store_default(self.root, source, default, module=auth_module(source))
        except Exception:
            raise SetupError(
                "Det innebygde oppsettet for tjenesten kunne ikke brukes. "
                "Oppdater familiekilder-mcp og prøv igjen."
            ) from None
        self.tested = False

    def _configure_municipality(self, job: Job, municipality: str, *, replace: bool) -> None:
        """Look up the Flyt municipality, then store it; runs in the worker.

        The lookup is the only network step, runs without the setup lock and
        changes nothing, so a failed lookup leaves login and municipality as
        they were. Cancellation is checked under the setup lock right before
        the first change; from there the logout and the new municipality are
        stored together and stand even if the login that follows is cancelled.
        """

        from . import flyt_auth
        from .private_lock import PrivateLockBusy, private_source_lock
        from .source_adapter import SourceUnavailable

        with self.lock:
            job.message = "Henter kommunen fra Flyt …"
        # A new profile has no data folder yet; the auth modules expect one.
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            listed = [flyt_auth.parse_municipality(item) for item in flyt_auth.list_municipalities()]
            chosen = flyt_auth.select_municipality(listed, municipality)
        except flyt_auth.FlytAuthError as error:
            # These messages are fixed Norwegian text about the public list,
            # for example that no municipality matched.
            raise _JobFailure(self._sentence(error)) from None
        except SourceUnavailable:
            raise _JobFailure(
                "Kommunelisten fra Flyt kunne ikke hentes. Kontroller nettforbindelsen og prøv igjen."
            ) from None
        try:
            # The Flyt lock keeps other Flyt operations out between logout and store.
            with private_source_lock(self.root, flyt_auth.SOURCE, wait=True, timeout=30):
                with self.lock:
                    if job.cancel.is_set():
                        raise SetupError("Innloggingen ble avbrutt.")
                    self._store_municipality(job, chosen, replace=replace)
        except PrivateLockBusy:
            raise _JobFailure(
                "En annen Flyt-handling pågår, så ingenting er endret. Prøv igjen om litt."
            ) from None

    def _store_municipality(self, job: Job, chosen, *, replace: bool) -> None:
        """Log out of Flyt if confirmed, then store the chosen municipality.

        Called with the setup lock and the Flyt lock held; only local files change.
        """

        from . import flyt_auth

        try:
            stored = flyt_auth.load_client_config(self.root).municipality
        except Exception:
            stored = None
        if stored == chosen:
            return
        # A session belongs to its municipality, so it cannot be kept.
        logged_out = False
        if replace and has_local_session(flyt_auth, self.root):
            job.message = "Logger ut av Flyt …"
            flyt_auth.logout(self.root)
            logged_out = True
        try:
            content = json.dumps(flyt_auth.ClientConfig(chosen).as_private_json())
            self._configure_client(flyt_auth.SOURCE, content)
        except Exception as error:
            if logged_out:
                raise _JobFailure(
                    "Du er logget ut av Flyt, men den nye kommunen kunne ikke lagres. Prøv igjen."
                ) from None
            if isinstance(error, flyt_auth.FlytAuthError):
                raise _JobFailure(self._sentence(error)) from None
            raise

    def _renew_default(self, job: Job, *, replace: bool) -> None:
        """Take a newer built-in setup before the login; runs in the worker.

        A setup the wizard stored is usually renewed when the wizard opens.
        A source refuses a new app identity while it has a session. Only on
        that refusal, and when the user has confirmed that the login may be
        replaced, the worker logs out first so the new login uses the new
        setup. Any other failure leaves the stored setup for the login.
        Cancellation is checked under the setup lock right before the logout.
        """

        from .private_lock import PrivateLockBusy, private_source_lock

        source = job.source
        label = REGISTRY[source].label
        module = auth_module(source)
        try:
            if not renewal_pending(self.root, source, module=module):
                return
        except Exception:
            return
        try:
            with private_source_lock(self.root, source, wait=True, timeout=30):
                try:
                    refresh_stored_default(self.root, source, module=module)
                    return
                except RenewalRefused:
                    pass
                except Exception:
                    return
                if not replace:
                    return
                with self.lock:
                    if job.cancel.is_set():
                        raise SetupError("Innloggingen ble avbrutt.")
                    job.message = f"Logger ut av {label} før det nye oppsettet tas i bruk …"
                module.logout(self.root)
                with self.lock:
                    job.logged_out = True
                try:
                    refresh_stored_default(self.root, source, module=module)
                except Exception:
                    raise _JobFailure(
                        f"Du er logget ut av {label}, men det nye oppsettet kunne ikke lagres. "
                        "Prøv igjen."
                    ) from None
        except PrivateLockBusy:
            raise _JobFailure(
                f"En annen {label}-handling pågår, så ingenting er endret. Prøv igjen om litt."
            ) from None

    @staticmethod
    def _sentence(error: Exception) -> str:
        text = str(error).strip()
        return text if text.endswith(".") else f"{text}."

    def _run(self, job: Job, action: str, consent: bool, replace: bool, *, municipality=None):
        try:
            if action == "check":
                asyncio.run(check_connection(self.root))
                self.tested = True
                message = "Den lokale forbindelsen virker. Start AI-appen på nytt og velg familiekilder-mcp."
            else:
                if municipality is not None:
                    self._configure_municipality(job, municipality, replace=replace)
                if job.cancel.is_set():
                    raise SetupError("Innloggingen ble avbrutt.")
                self._renew_default(job, replace=replace)
                self._login(job, replace=replace)
                with self.lock:
                    if job.cancel.is_set():
                        raise SetupError("Innloggingen ble avbrutt.")
                    self._enable(job.source)
                message = "Innloggingen er lagret på denne maskinen."
            with self.lock:
                job.phase = "cancelled" if job.cancel.is_set() else "done"
                job.message = self._cancelled_message(job) if job.cancel.is_set() else message
        except Exception as error:
            with self.lock:
                job.phase = "cancelled" if job.cancel.is_set() else "error"
                if job.cancel.is_set():
                    job.message = self._cancelled_message(job)
                elif isinstance(error, _JobFailure):
                    job.message = str(error)
                elif job.logged_out:
                    job.message = (
                        f"Innloggingen ble ikke fullført, og du er logget ut av "
                        f"{REGISTRY[job.source].label}. Prøv å logge inn på nytt."
                    )
                else:
                    job.message = "Steget kunne ikke fullføres. Kontroller oppsettet og nettleseren, og prøv igjen."

    @staticmethod
    def _cancelled_message(job: Job) -> str:
        if job.logged_out:
            return (
                f"Steget er avbrutt, og du er logget ut av {REGISTRY[job.source].label}. "
                "Logg inn på nytt for å bruke tjenesten."
            )
        return "Steget er avbrutt."

    def _login(self, job: Job, *, replace: bool):
        from .browser_bootstrap import bootstrap_source, capture_oauth_callback

        source = job.source
        job.message = "Fullfør innloggingen i nettleservinduet. BankID godkjenner du selv."
        if source == "gmail":
            from .gmail_auth import login

            login(self.root, cancelled=job.cancel.is_set)
        elif "browser-bootstrap" in REGISTRY[source].auth_commands:
            def wait(_prompt):
                with self.lock:
                    job.phase = "waiting"
                    job.message = "Logg inn i nettleservinduet. Trykk «Jeg er logget inn» når tjenesten er lastet."
                deadline = time.monotonic() + 600
                while not job.complete.wait(0.2):
                    if job.cancel.is_set() or time.monotonic() >= deadline:
                        raise SetupError("Innloggingen ble avbrutt.")
                if job.cancel.is_set():
                    raise SetupError("Innloggingen ble avbrutt.")
                job.phase = "working"

            bootstrap_source(self.root, source, replace_existing=replace, wait_for_user=wait)
        else:
            module = auth_module(source)
            if not module.consent_status(self.root)["current"]:
                module.record_consent(self.root)
            url, transaction = module.begin_login(self.root)
            config = module.load_client_config(self.root)
            callback = capture_oauth_callback(
                self.root, authorization_url=url, expected_redirect_uri=config.redirect_uri,
                cancelled=job.cancel.is_set,
            )
            if job.cancel.is_set():
                raise SetupError("Innloggingen ble avbrutt.")
            module.login_with_callback(self.root, transaction, callback, replace_existing=replace)

    def signal(self, job_id: str, *, cancel: bool):
        with self.lock:
            if self.job is None or self.job.id != job_id or self.job.phase not in {"working", "waiting"}:
                raise SetupError("Dette steget er ikke lenger aktivt.")
            if cancel:
                self.job.cancel.set()
            elif self.job.phase != "waiting":
                raise SetupError("Steget venter ikke på en bekreftelse.")
            self.job.complete.set()

    def connect_client(self, client: str):
        with self.lock:
            self._idle()
            install_client(client, self.root, self.home)

    def set_enabled(self, source: str, enabled: bool):
        if not isinstance(source, str) or source not in REGISTRY or not isinstance(enabled, bool):
            raise SetupError("Velg en gyldig tjeneste.")
        with self.lock:
            self._idle()
            if enabled:
                if connection_state(auth_module(source).auth_status(self.root)) != "connected":
                    raise SetupError("Logg inn før du aktiverer tjenesten.")
                self._enable(source)
            else:
                config = Config.load(self.root)
                services = dict(config.services)
                services.pop(source, None)
                Config(self.root, services).save()
                self.tested = False

    def close(self):
        with self.lock:
            self.closed = True
            if self.job:
                self.job.cancel.set()
                self.job.complete.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=10)
