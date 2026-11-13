"""Loopback-only web setup with origin, host and per-process CSRF checks."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit
import webbrowser

from .setup_clients import ClientSetupError
from .setup_service import SetupError, SetupService

ASSETS = Path(__file__).parent / "data/setup"
MAX_BODY_BYTES = 2 * 1024 * 1024


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root: Path, *, port=0, service=None):
        self.service = service or SetupService(root)
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def server_close(self):
        self.service.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_args):
        pass

    def _reply(self, status, payload, mime="application/json; charset=utf-8"):
        body = json.dumps(payload, ensure_ascii=False).encode() if isinstance(payload, dict) else payload
        self.send_response(status)
        for key, value in {
            "Content-Type": mime, "Content-Length": str(len(body)), "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Cross-Origin-Resource-Policy": "same-origin", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                       "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                                       "frame-ancestors 'none'; form-action 'self'",
        }.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _guard(self, *, api=False):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self._reply(403, {"error": "Oppsettet er bare tilgjengelig på denne maskinen."})
            return False
        origin = self.headers.get("Origin")
        if (origin is not None and origin != self.server.origin) or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._reply(403, {"error": "Åpne oppsettet i det opprinnelige nettleservinduet."})
            return False
        if api and not hmac.compare_digest(self.headers.get("X-Setup-Token", ""), self.server.token):
            self._reply(403, {"error": "Oppsettsvinduet er utløpt. Last siden på nytt."})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self._guard(api=path.startswith("/api/")):
            return
        if path == "/api/state":
            try:
                self._reply(200, self.server.service.state())
            except Exception:
                self._reply(500, {"error": "Den lokale profilen kunne ikke leses."})
        elif path in {"/", "/setup.js", "/setup.css"}:
            name = "index.html" if path == "/" else path[1:]
            payload = (ASSETS / name).read_bytes().replace(b"__SETUP_TOKEN__", self.server.token.encode())
            mime = {"index.html": "text/html", "setup.js": "text/javascript", "setup.css": "text/css"}[name]
            self._reply(200, payload, mime + "; charset=utf-8")
        elif path == "/favicon.ico":
            self._reply(204, b"", "image/x-icon")
        elif path == "/help/client":
            help_file = ASSETS / "client-help.txt"
            self._reply(200, help_file.read_bytes(), "text/plain; charset=utf-8")
        else:
            self._reply(404, {"error": "Siden finnes ikke."})

    def do_POST(self):
        if not self._guard(api=True):
            return
        if self.headers.get("Origin") != self.server.origin:
            self._reply(403, {"error": "Forespørselen må komme fra oppsettsvinduet."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY_BYTES or self.headers.get("Transfer-Encoding"):
            self._reply(413, {"error": "Filen er tom eller for stor."})
            return
        if self.headers.get_content_type() != "application/json":
            self._reply(415, {"error": "Oppsettet forventer JSON."})
            return
        try:
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._reply(400, {"error": "Forespørselen kunne ikke leses."})
            return
        path = urlsplit(self.path).path
        service = self.server.service
        try:
            if path == "/api/client-file":
                service.import_client(value.get("source"), value.get("content"))
            elif path == "/api/start":
                service.start(value.get("action"), value.get("source", ""),
                              consent=value.get("consent") is True, replace=value.get("replace") is True)
            elif path == "/api/job":
                if value.get("action") not in {"continue", "cancel"}:
                    raise SetupError("Ukjent oppsettshandling.")
                service.signal(value.get("id"), cancel=value["action"] == "cancel")
            elif path == "/api/connect":
                service.connect_client(value.get("client"))
            elif path == "/api/service":
                service.set_enabled(value.get("source"), value.get("enabled"))
            elif path == "/api/close":
                service.close()
                self._reply(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                self._reply(404, {"error": "Handlingen finnes ikke."})
                return
            self._reply(200, {"ok": True})
        except (SetupError, ClientSetupError) as error:
            self._reply(400, {"error": str(error)})
        except Exception:
            self._reply(500, {"error": "Steget kunne ikke fullføres. Ingen innloggingsdata vises her."})


def serve(root: Path, *, port=0, open_browser=True) -> int:
    with SetupServer(root, port=port) as server:
        print(f"Åpne lokalt oppsett: {server.origin}", flush=True)
        print("Lukk med «Avslutt oppsett» eller Ctrl+C.", flush=True)
        if open_browser:
            webbrowser.open(server.origin)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
    return 0
