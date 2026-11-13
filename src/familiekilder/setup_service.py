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
from .connection_status import auth_module, connection_state, has_local_session
from .live_config import Config
from .private_state import atomic_write_private_bytes, ensure_private_directory
from .setup_clients import check_connection, client_state, install_client
from .sources import REGISTRY


class SetupError(ValueError):
    pass


@dataclass
class Job:
    id: str
    source: str
    phase: str = "working"
    message: str = "Starter …"
    complete: threading.Event = field(default_factory=threading.Event)
    cancel: threading.Event = field(default_factory=threading.Event)


class SetupService:
    def __init__(self, root: Path, *, home: Path | None = None):
        self.root = root
        self.home = Path.home() if home is None else home
        self.lock = threading.RLock()
        self.job: Job | None = None
        self.tested = False
        self.closed = False
        self.worker: threading.Thread | None = None

    def _idle(self):
        if self.closed:
            raise SetupError("Oppsettet er lukket.")
        if self.job and self.job.phase in {"working", "waiting"}:
            raise SetupError("Fullfør eller avbryt det pågående steget først.")

    def _import_directory(self):
        self.root.mkdir(parents=True, exist_ok=True)
        ensure_private_directory(self.root / ".private")
        directory = self.root / ".private/setup-imports"
        ensure_private_directory(directory)
        return directory

    def state(self) -> dict:
        with self.lock:
            rows = []
            config = Config.load(self.root)
            for source in REGISTRY.values():
                try:
                    status = auth_module(source.name).auth_status(self.root)
                    state = connection_state(status)
                    existing = bool(status.get("session_present"))
                except Exception:
                    state, existing = "unavailable", False
                rows.append({
                    "id": source.name, "label": source.label, "description": source.description,
                    "state": state, "enabled": source.name in config.services,
                    "has_session": existing,
                    "automatic_setup": source.name == "spond",
                })
            job = None if self.job is None else {
                "id": self.job.id, "source": self.job.source, "phase": self.job.phase,
                "message": self.job.message,
            }
            return {
                "version": __version__, "services": rows, "job": job, "tested": self.tested,
                "clients": [client_state(client, self.root, self.home) for client in ("claude", "codex")],
                "dependencies": {"brave": Path("/Applications/Brave Browser.app").exists(),
                                 "pdf": bool(shutil.which("pdftotext") and shutil.which("pdftoppm")),
                                 "word": bool(shutil.which("soffice") or Path("/Applications/LibreOffice.app").exists())},
            }

    def import_client(self, source: str, content: str) -> None:
        if (not isinstance(source, str) or source not in REGISTRY
                or not isinstance(content, str) or len(content.encode()) > 1024 * 1024):
            raise SetupError("Velg en gyldig JSON-fil for tjenesten.")
        with self.lock:
            self._idle()
            try:
                if not isinstance(json.loads(content), dict):
                    raise ValueError
                directory = self._import_directory()
                path = directory / f"client-{secrets.token_hex(12)}.json"
                try:
                    atomic_write_private_bytes(path, content.encode())
                    auth_module(source).configure_client(self.root, path)
                finally:
                    path.unlink(missing_ok=True)
            except Exception:
                raise SetupError("Filen kunne ikke brukes. Kontroller at den er klientoppsettet for denne tjenesten.") from None
            self.tested = False

    def _enable(self, source):
        config = Config.load(self.root)
        services = dict(config.services)
        services[source] = frozenset()
        Config(self.root, services).save()
        self.tested = False

    def start(self, action: str, source: str = "", *, consent=False, replace=False) -> None:
        if action not in {"login", "ist-client", "check"} or (action == "login" and source not in REGISTRY):
            raise SetupError("Ukjent oppsettshandling.")
        with self.lock:
            self._idle()
            if action == "login":
                module = auth_module(source)
                status = module.auth_status(self.root)
                if source != "gmail" and consent is not True:
                    raise SetupError("Bekreft at du vil bruke den uoffisielle koblingen.")
                if not status.get("configured") and source == "spond":
                    from .spond import GROUP_FILE_STORAGE_HOST, IMAGE_STORAGE_HOST
                    from .spond_auth import API_ROOT

                    self.import_client(source, json.dumps({
                        "api_root": API_ROOT, "app_version": "web",
                        "attachment_hosts": [GROUP_FILE_STORAGE_HOST, IMAGE_STORAGE_HOST],
                    }))
                    status = module.auth_status(self.root)
                if not status.get("configured"):
                    raise SetupError("Legg til klientoppsettet før du logger inn.")
                if has_local_session(module, self.root) and replace is not True:
                    raise SetupError("Bekreft at en eksisterende innlogging kan erstattes.")
            job = Job(secrets.token_hex(12), source)
            self.job = job
            self.tested = False
            self.worker = threading.Thread(target=self._run, args=(job, action, consent, replace), daemon=True)
            self.worker.start()

    def _run(self, job: Job, action: str, consent: bool, replace: bool):
        try:
            if action == "ist-client":
                from .ist_oauth import configure_client, export_mac_client_config

                directory = self._import_directory()
                path = directory / f"client-{job.id}.json"
                try:
                    export_mac_client_config(path)
                    configure_client(self.root, path)
                finally:
                    path.unlink(missing_ok=True)
                message = "Oppsettet fra IST Home er lagt til. Du kan nå logge inn."
            elif action == "check":
                asyncio.run(check_connection(self.root))
                self.tested = True
                message = "Den lokale forbindelsen virker. Start AI-appen på nytt og velg familiekilder-mcp."
            else:
                self._login(job, replace=replace)
                with self.lock:
                    if job.cancel.is_set():
                        raise SetupError("Innloggingen ble avbrutt.")
                    self._enable(job.source)
                message = "Innloggingen er lagret på denne maskinen."
            with self.lock:
                job.phase = "cancelled" if job.cancel.is_set() else "done"
                job.message = "Steget er avbrutt." if job.cancel.is_set() else message
        except Exception:
            with self.lock:
                job.phase = "cancelled" if job.cancel.is_set() else "error"
                job.message = ("Steget er avbrutt." if job.cancel.is_set() else
                               "Steget kunne ikke fullføres. Kontroller oppsettet og nettleseren, og prøv igjen.")

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
