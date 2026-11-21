"""Run the login path with a real browser against made-up local services only.

    python scripts/login_trial.py --browser chrome

Run it from the project root with PYTHONPATH=src:tests/support (on Windows
src;tests/support), and on Linux under xvfb-run -a. It needs the browser and
the package's own dependencies, nothing else.

What runs exactly as in a login from the setup wizard:

1. IST Home, as the wizard's `_login` does it: `ist_oauth.begin_login` makes the
   authorization URL, and `capture_oauth_callback` opens it in a disposable
   profile below the trial's own new data root. The catalog picks and checks
   the browser (`select_browser`), `_launch_brave` starts it over the DevTools
   pipe, and the password helper runs on a root without saved passwords.
2. The sign-in page is served over HTTPS from 127.0.0.1. When the trial's
   "user" presses Logg inn, the page answers with a 302 to the app's own
   custom-scheme address under a form-action CSP, as ID-porten does, and
   `capture_oauth_callback` catches the callback from the browser's events.
3. `ist_oauth.login_with_callback` exchanges the code over HTTP with a made-up
   OIDC service on 127.0.0.1 (PKCE, RS256 tokens, JWKS, userinfo and the
   GraphQL context check) and stores the session. The trial checks the session
   file with the product's own private-file checks.
4. `live_readers.list_live` reads IST Home through the real reader, from the
   same local service.
5. Spond, as the wizard does it: `bootstrap_source` opens the Spond login
   page in a new disposable profile, `_spond_payload` reads the four session
   fields from exactly one page on the expected HTTPS origin, and
   `spond_auth.import_session` stores the session after a check against a
   fake Spond transport.

Where the trial differs from a real login (the seams):

- The service addresses are module constants. The trial points them at
  127.0.0.1 for its own run, as tests/test_fake_oidc_server.py does: the IST
  endpoints at the local OIDC service, the IST authorization endpoint at the
  local HTTPS page, and the Spond origin at https://127.0.0.1. The checks that
  use them (exact HTTPS origin, custom-scheme callback, pinned endpoints)
  run unchanged.
- The trial's `browser_factory` wraps the real `_launch_brave` and adds only
  the trial's own DevTools calls. For IST Home it tells the browser to accept
  the trial's self-signed certificate (Security.setIgnoreCertificateErrors),
  loads the page again and presses Logg inn. For Spond it starts on
  about:blank, sends every request through DevTools (Fetch) before the first
  one, and answers the login page itself, so the browser never sends a
  request anywhere. The product never does any of this.
- The Spond profile check and the IST first read use local made-up answers
  instead of the services.

Nothing can leave the machine: a Python audit hook refuses every socket
connection and name lookup that is not loopback, the authorization page and
the Spond origin are loopback addresses, and the password store is replaced
by one that refuses and counts calls. All tokens and names are made up.

Prints one JSON line and exits 0 only when every step passed, the browser
profiles were gone afterwards, the session files were private and nothing
touched the password store or tried to leave the machine.
"""

from __future__ import annotations

import argparse
import functools
import html
import http.server
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from unittest.mock import patch

LOOPBACK = "127.0.0.1"
# Spond pages get this origin in the trial: port 443, which the
# exact-origin check requires, on an address that cannot leave the machine.
TRIAL_ORIGIN = f"https://{LOOPBACK}"
SIGN_IN_PATH = "/connect/authorize"
SIGN_IN_ACTION = "/connect/authorize/callback"
BUTTON_ID = "logg-inn"
PAGE_CSP = "default-src 'none'; form-action 'self'"


class TrialError(Exception):
    """A step did not do what a real login must do."""


# ----------------------------------------------------------------------------
# Guards: nothing leaves the machine and nothing reaches the password store.


def _loopback(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


class LoopbackGuard:
    """An audit hook that refuses every network connection that is not loopback."""

    def __init__(self) -> None:
        self.refused: list[str] = []

    def __call__(self, event: str, args: tuple[Any, ...]) -> None:
        if event == "socket.connect":
            sock, address = args[0], args[1]
            family = getattr(sock, "family", None)
            if family in (socket.AF_INET, socket.AF_INET6) and not _loopback(address[0]):
                self._refuse(address[0])
        elif event in ("socket.getaddrinfo", "socket.gethostbyname"):
            host = args[0]
            if host is not None and not _loopback(host):
                self._refuse(host)

    def _refuse(self, host: Any) -> None:
        self.refused.append(str(host)[:253])
        raise ConnectionRefusedError("Innloggingsprøven slipper ikke ut av maskinen")


class RefusingPasswordStore:
    """Stands in for the system's password store and counts every use."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> Any:
        self.calls += 1
        raise TrialError("Innloggingsprøven skal ikke bruke passordlageret")


# ----------------------------------------------------------------------------
# The made-up HTTPS sign-in page on 127.0.0.1.


def _self_signed_certificate(directory: Path) -> tuple[Path, Path]:
    """A throwaway certificate for 127.0.0.1, valid for one hour."""

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Familiekilder innloggingsprove")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(LOOPBACK))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "trial-certificate.pem"
    key_path = directory / "trial-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(key_path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return certificate_path, key_path


class _TlsServer(http.server.ThreadingHTTPServer):
    """Hands each connection to its own thread before the TLS handshake."""

    daemon_threads = True

    def __init__(self, handler: type, context: ssl.SSLContext) -> None:
        super().__init__((LOOPBACK, 0), handler)
        self.context = context
        self.handshake_failures = 0

    def get_request(self) -> tuple[socket.socket, Any]:
        connection, address = self.socket.accept()
        wrapped = self.context.wrap_socket(
            connection, server_side=True, do_handshake_on_connect=False
        )
        return wrapped, address

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The browser drops the first connection when it meets the
        # self-signed certificate; that is expected and not printed.
        self.handshake_failures += 1


class SignInPage:
    """The authorization endpoint of a made-up sign-in service.

    GET shows a one-button sign-in form and records what the browser carried.
    POST answers with a 302 to the app's custom-scheme callback, under a
    form-action CSP like ID-porten's.
    """

    def __init__(self, directory: Path, *, client_id: str, redirect_uri: str) -> None:
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.issuer = ""
        self.authorization: dict[str, str] | None = None
        self.code: str | None = None
        self.on_authorization: Callable[[Mapping[str, str]], None] = lambda _query: None
        self.form_shown = 0
        self.callbacks_sent = 0
        self.problems: list[str] = []
        certificate, key = _self_signed_certificate(directory)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        page = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def _send(self, status: int, body: bytes, headers: Mapping[str, str]) -> None:
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Security-Policy", PAGE_CSP)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
                parts = urllib.parse.urlsplit(self.path)
                if parts.path != SIGN_IN_PATH:
                    self._send(404, b"", {})
                    return
                query = {
                    key: values[0]
                    for key, values in urllib.parse.parse_qs(parts.query).items()
                    if len(values) == 1
                }
                problem = page._check_authorization(query)
                if problem:
                    page.problems.append(problem)
                    self._send(400, problem.encode("utf-8"), {"Content-Type": "text/plain"})
                    return
                page.authorization = query
                page.on_authorization(query)
                page.form_shown += 1
                self._send(200, page._form(), {"Content-Type": "text/html; charset=utf-8"})

            def do_POST(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
                length = int(self.headers.get("Content-Length", "0") or 0)
                self.rfile.read(min(length, 4096))
                if (
                    urllib.parse.urlsplit(self.path).path != SIGN_IN_ACTION
                    or not page.authorization
                ):
                    page.problems.append("innsending uten autorisasjon")
                    self._send(400, b"", {})
                    return
                page.code = "prove-kode-" + secrets.token_urlsafe(16)
                callback = (
                    f"{page.redirect_uri}?code={page.code}"
                    f"&state={urllib.parse.quote(page.authorization['state'], safe='')}"
                    f"&iss={urllib.parse.quote(page.issuer, safe='')}"
                )
                page.callbacks_sent += 1
                self._send(302, b"", {"Location": callback})

        self.server = _TlsServer(Handler, context)
        self.origin = f"https://{LOOPBACK}:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _check_authorization(self, query: Mapping[str, str]) -> str | None:
        expected = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "code_challenge_method": "S256",
        }
        for key, value in expected.items():
            if query.get(key) != value:
                return f"autorisasjonen mangler riktig {key}"
        for key in ("state", "nonce", "code_challenge", "scope"):
            if not query.get(key):
                return f"autorisasjonen mangler {key}"
        return None

    @staticmethod
    def _form() -> bytes:
        return (
            "<!doctype html><meta charset=utf-8><title>Oppdiktet innlogging</title>"
            "<p>Oppdiktet innloggingsside for prøven.</p>"
            f'<form method=post action="{html.escape(SIGN_IN_ACTION)}">'
            f'<button id="{BUTTON_ID}" type=submit>Logg inn</button></form>'
        ).encode("utf-8")

    def __enter__(self) -> SignInPage:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


# ----------------------------------------------------------------------------
# The trial's browser factories: the real launch plus the trial's own calls.


def _require_origin(url: str, origin: str) -> None:
    """Refuse to open anything but the trial's own loopback page."""

    parts = urllib.parse.urlsplit(url)
    expected = urllib.parse.urlsplit(origin)
    if (
        parts.scheme != "https"
        or parts.hostname != LOOPBACK
        or parts.port != expected.port
        or expected.hostname != LOOPBACK
    ):
        raise TrialError("Prøven åpner bare sin egen side på 127.0.0.1")


def _page_session(cdp: Any, timeout: float) -> str:
    """Attach the trial's own session to the one page the browser opened."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        targets = cdp.call("Target.getTargets").get("targetInfos", [])
        pages = [target for target in targets if target.get("type") == "page"]
        if len(pages) == 1:
            attached = cdp.call(
                "Target.attachToTarget", {"targetId": pages[0]["targetId"], "flatten": True}
            )
            return str(attached["sessionId"])
        if len(pages) > 1:
            raise TrialError("Nettleseren åpnet mer enn én side")
        time.sleep(0.1)
    raise TrialError("Nettleseren åpnet ingen side")


def _evaluate(cdp: Any, session: str, expression: str) -> Any:
    try:
        response = cdp.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "silent": True},
            session_id=session,
        )
    except Exception:  # noqa: BLE001 - a navigating page answers on the next try
        return None
    return response.get("result", {}).get("value")


class _TrialCdp:
    """The browser's DevTools client, with one action before the first event wait.

    `capture_oauth_callback` enables the Page and Network events and then waits
    for the callback. The action runs there, so the trial's "user" signs in
    only when the login is listening, as a person would.
    """

    def __init__(self, inner: Any, before_first_wait: Callable[[], None]) -> None:
        self._inner = inner
        self._before_first_wait: Callable[[], None] | None = before_first_wait
        self.events: list[Mapping[str, Any]] = []

    def call(self, method: str, params: Mapping[str, Any] | None = None, **options: Any) -> Any:
        return self._inner.call(method, params, **options)

    def wait_for_event(self, methods: frozenset[str], *, timeout: float) -> Any:
        action, self._before_first_wait = self._before_first_wait, None
        if action is not None:
            action()
        event = self._inner.wait_for_event(methods, timeout=timeout)
        if event is not None:
            self.events.append(event)
        return event

    def callback_events(self, expected_redirect_uri: str) -> list[str]:
        """Which DevTools events carried the callback to the login."""

        from familiekilder import browser_bootstrap

        return [
            str(event.get("method"))
            for event in self.events
            if browser_bootstrap._matches_callback(
                browser_bootstrap._event_url(event), expected_redirect_uri
            )
        ]


class _TrialHandle:
    def __init__(self, browser: Any, cdp: Any) -> None:
        self._browser = browser
        self._cdp = cdp

    @property
    def cdp(self) -> Any:
        return self._cdp

    def close(self) -> None:
        self._browser.close()


class Trial:
    def __init__(self, browser: str | None, timeout: float) -> None:
        self.browser = browser
        self.timeout = timeout
        self.product: str | None = None
        self.run_paths: list[Path] = []
        self.blocked_requests: list[str] = []
        self.oauth_cdp: _TrialCdp | None = None

    def _launch(self, run_path: Path, start_url: str, brave_path: Any, **options: Any) -> Any:
        from familiekilder import browser_bootstrap

        self.run_paths.append(run_path)
        browser = browser_bootstrap._launch_brave(
            run_path, start_url, brave_path, browser=self.browser, **options
        )
        try:
            version = browser.cdp.call("Browser.getVersion")
            self.product = str(version.get("product") or "") or None
        except BaseException:
            browser.close()
            raise
        return browser

    def oauth_factory(self, sign_in: SignInPage) -> Callable[..., Any]:
        def factory(run_path: Path, start_url: str, brave_path: Any) -> Any:
            _require_origin(start_url, sign_in.origin)
            browser = self._launch(run_path, start_url, brave_path, oauth_callback=True)
            try:
                cdp = browser.cdp
                # Only the trial accepts its own self-signed certificate.
                cdp.call("Security.setIgnoreCertificateErrors", {"ignore": True})
                session = _page_session(cdp, self.timeout)
                # The first load met the certificate error; load the page again.
                cdp.call("Page.navigate", {"url": start_url}, session_id=session)
            except BaseException:
                browser.close()
                raise
            self.oauth_cdp = _TrialCdp(cdp, lambda: self._press_sign_in(cdp, session))
            return _TrialHandle(browser, self.oauth_cdp)

        return factory

    def _press_sign_in(self, cdp: Any, session: str) -> None:
        expression = (
            f"(() => {{ const b = document.getElementById({json.dumps(BUTTON_ID)});"
            " if (!b || document.readyState !== 'complete') return false;"
            " b.click(); return true; })()"
        )
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if _evaluate(cdp, session, expression) is True:
                return
            time.sleep(0.2)
        raise TrialError("Innloggingssiden viste ikke knappen Logg inn")

    def page_factory(self, page: bytes) -> tuple[Callable[..., Any], Callable[[str], None]]:
        """A factory and a wait for `bootstrap_source` that answer the login page.

        The browser starts on about:blank. Before the first request every
        request from its pages is sent through DevTools (Fetch) to the trial,
        which answers the login page and refuses everything else, so the
        browser sends no request anywhere. The page stores made-up session
        fields and sets `proveKlar` when they are stored.
        """

        state: dict[str, Any] = {}

        def factory(run_path: Path, start_url: str, brave_path: Any) -> Any:
            _require_origin(start_url, TRIAL_ORIGIN)
            browser = self._launch(run_path, "about:blank", brave_path)
            try:
                cdp = browser.cdp
                cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
                session = _page_session(cdp, self.timeout)
                _evaluate(
                    cdp,
                    session,
                    f"setTimeout(() => {{ location.href = {json.dumps(start_url)}; }}, 0); true",
                )
            except BaseException:
                browser.close()
                raise
            state.update(cdp=cdp, session=session, start_url=start_url, served=0)
            return browser

        def wait_for_user(_prompt: str) -> None:
            state["served"] = self._serve_page(
                state["cdp"], state["session"], state["start_url"], page
            )

        factory.state = state  # type: ignore[attr-defined]
        return factory, wait_for_user

    def _serve_page(self, cdp: Any, session: str, start_url: str, page: bytes) -> int:
        import base64

        body = base64.b64encode(page).decode("ascii")
        ready = "document.readyState === 'complete' && window.proveKlar === true"
        served = 0
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            event = cdp.wait_for_event(frozenset({"Fetch.requestPaused"}), timeout=0.5)
            if event is None:
                if served and _evaluate(cdp, session, ready) is True:
                    return served
                continue
            params = event.get("params") or {}
            request = params.get("request") or {}
            options = {"session_id": event["sessionId"]} if event.get("sessionId") else {}
            if request.get("url") == start_url and request.get("method") == "GET":
                served += 1
                cdp.call(
                    "Fetch.fulfillRequest",
                    {
                        "requestId": params["requestId"],
                        "responseCode": 200,
                        "responseHeaders": [
                            {"name": "Content-Type", "value": "text/html; charset=utf-8"},
                            {
                                "name": "Content-Security-Policy",
                                "value": "default-src 'none'; script-src 'unsafe-inline'",
                            },
                        ],
                        "body": body,
                    },
                    **options,
                )
            else:
                self.blocked_requests.append(_origin_of(request.get("url")))
                cdp.call(
                    "Fetch.failRequest",
                    {"requestId": params["requestId"], "errorReason": "BlockedByClient"},
                    **options,
                )
        raise TrialError("Innloggingssiden ble ikke ferdig lastet")


def _origin_of(url: Any) -> str:
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        return "ugyldig"
    return f"{parts.scheme}://{parts.hostname or ''}"


# ----------------------------------------------------------------------------
# Checks after each step.


def _private_session_file(path: Path) -> dict[str, Any]:
    """Check a session file with the product's own private-file checks."""

    from familiekilder import browser_bootstrap
    from familiekilder.private_state import read_private_bytes, validate_private_parent_tree

    metadata = path.lstat()
    result: dict[str, Any] = {
        "owner_and_mode_ok": browser_bootstrap._private_file_ok(metadata),
        "parents_ok": validate_private_parent_tree(path),
    }
    if os.name == "nt":
        from familiekilder.windows_acl import file_is_private, is_private

        # A file inherits .private's user-only list, so its own list is not
        # protected; is_private is the check for the .private folder itself.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            result["acl_private"] = file_is_private(descriptor)
        finally:
            os.close(descriptor)
        private = next(parent for parent in path.parents if parent.name == ".private")
        result["private_folder_acl"] = is_private(private)
        result["acl_private"] = result["acl_private"] and result["private_folder_acl"]
    else:
        result["mode"] = oct(stat.S_IMODE(metadata.st_mode))
    read_private_bytes(path)
    result["read_by_private_reader"] = True
    result["ok"] = (
        result["owner_and_mode_ok"]
        and result["parents_ok"]
        and (result.get("acl_private", True) if os.name == "nt" else result["mode"] == "0o600")
    )
    return result


def _bootstrap_folder_empty(root: Path) -> bool:
    folder = root / ".private" / "browser-bootstrap"
    return not folder.exists() or not any(folder.iterdir())


class Steps:
    def __init__(self) -> None:
        self.results: dict[str, dict[str, Any]] = {}

    def run(self, name: str, action: Callable[[], dict[str, Any] | None]) -> bool:
        started = time.monotonic()
        entry: dict[str, Any] = {}
        try:
            details = action() or {}
            entry.update(details)
            entry["ok"] = bool(details.get("ok", True))
        except Exception as error:  # noqa: BLE001 - the report shows the failure
            entry["ok"] = False
            entry["error"] = f"{type(error).__name__}: {error}"
        entry["seconds"] = round(time.monotonic() - started, 1)
        self.results[name] = entry
        return entry["ok"]


# ----------------------------------------------------------------------------
# IST Home: browser, callback, token exchange, private session, first read.


def _ist_client_file(directory: Path, authority: str) -> Path:
    from familiekilder import ist_oauth
    from familiekilder_testing.oidc_server import CLIENT_ID, CLIENT_SECRET, SCOPE

    path = directory / "ist-client.json"
    path.write_text(
        json.dumps(
            {
                "authority": authority,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": ist_oauth.REDIRECT_URI,
                "scope": SCOPE,
            }
        ),
        encoding="utf-8",
    )
    return path


def _made_up_posts(now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    children = [
        {
            "id": "prove-barn-1",
            "firstName": "Ada",
            "lastName": "Prøvesen",
            "placements": [{"id": "prove-plassering-1"}],
        }
    ]
    posts = [
        {
            "id": "prove-innlegg-1",
            "publishedAt": (now - timedelta(hours=2)).isoformat(),
            "editedAt": None,
            "title": "Oppdiktet foreldremøte",
            "content": "<p>Dette innlegget finnes bare i innloggingsprøven.</p>",
            "read": False,
            "pinned": False,
            "categoryId": None,
            "scope": "school",
            "placements": [{"id": "prove-plassering-1"}],
            "publishedBy": {"id": "prove-larer-1", "firstName": "Lærer", "lastName": "Oppdiktet"},
            "files": [],
            "images": [],
        }
    ]
    return children, posts


def ist_trial(trial: Trial, root: Path, work: Path, steps: Steps) -> None:
    from familiekilder import browser_bootstrap, ist_home, ist_oauth, live_readers
    from familiekilder import ist_oauth_protocol as protocol
    from familiekilder_testing.oidc_server import LocalOidcServer

    now = datetime.now(UTC)
    with ExitStack() as stack:
        provider = stack.enter_context(LocalOidcServer(validation_time=int(now.timestamp())))
        provider.children, provider.posts = _made_up_posts(now)
        sign_in = stack.enter_context(
            SignInPage(
                work,
                client_id=urllib.parse.urlsplit(ist_oauth.REDIRECT_URI).scheme,
                redirect_uri=ist_oauth.REDIRECT_URI,
            )
        )
        sign_in.issuer = provider.base_url

        def authorized(query: Mapping[str, str]) -> None:
            # The local token service checks PKCE and nonce against what the
            # browser carried to the sign-in page.
            provider.expected_challenge = query["code_challenge"]
            provider.current_nonce = query["nonce"]

        sign_in.on_authorization = authorized
        endpoints = provider.endpoints()
        endpoints["AUTHORIZATION_ENDPOINT"] = f"{sign_in.origin}{SIGN_IN_PATH}"
        pinned = frozenset(
            value
            for key, value in endpoints.items()
            if key not in {"ISSUER", "AUTHORITY", "AUTHORIZATION_ENDPOINT"}
        )
        stack.enter_context(patch.multiple(ist_oauth, **endpoints))
        stack.enter_context(patch.multiple(protocol, **endpoints, _PINNED_NETWORK_URLS=pinned))
        stack.enter_context(patch.object(ist_home, "ENDPOINT", endpoints["IST_GRAPHQL_ENDPOINT"]))

        ist_oauth.configure_client(root, _ist_client_file(work, provider.base_url))
        ist_oauth.record_consent(root)
        url, transaction = ist_oauth.begin_login(root)
        captured: dict[str, str] = {}

        def browser_step() -> dict[str, Any]:
            _require_origin(url, sign_in.origin)
            captured["callback"] = browser_bootstrap.capture_oauth_callback(
                root,
                authorization_url=url,
                expected_redirect_uri=ist_oauth.REDIRECT_URI,
                timeout=trial.timeout,
                browser_factory=trial.oauth_factory(sign_in),
                login_browser=trial.browser,
            )
            assert trial.oauth_cdp is not None
            return {
                "sign_in_page_shown": sign_in.form_shown,
                "authorization_checked": sign_in.authorization is not None,
                "callbacks_sent": sign_in.callbacks_sent,
                "certificate_refusals": sign_in.server.handshake_failures,
                "callback_seen_in": trial.oauth_cdp.callback_events(ist_oauth.REDIRECT_URI),
                "problems": sign_in.problems,
                "ok": sign_in.form_shown >= 1
                and sign_in.callbacks_sent == 1
                and not sign_in.problems,
            }

        def callback_step() -> dict[str, Any]:
            if "callback" not in captured:
                raise TrialError("Nettleserdelen ga ingen tilbakekalling")
            parts = urllib.parse.urlsplit(captured["callback"])
            query = urllib.parse.parse_qs(parts.query)
            assert transaction.state is not None
            return {
                "scheme": parts.scheme,
                "code_matches": query.get("code") == [sign_in.code],
                "state_matches": query.get("state") == [sign_in.authorization["state"]],
                "ok": f"{parts.scheme}://{parts.netloc}{parts.path}" == ist_oauth.REDIRECT_URI
                and query.get("code") == [sign_in.code]
                and query.get("state") == [sign_in.authorization["state"]],
            }

        def exchange_step() -> dict[str, Any]:
            provider.expected_code = sign_in.code
            result = ist_oauth.login_with_callback(root, transaction, captured["callback"])
            return {
                "authenticated": result.get("authenticated"),
                "id_token_verified": result.get("id_token_verified"),
                "context_verified": result.get("context_verified"),
                "token_requests": len(provider.token_forms),
                "ok": result.get("authenticated") is True
                and result.get("id_token_verified") is True
                and len(provider.token_forms) == 1,
            }

        def session_step() -> dict[str, Any]:
            return _private_session_file(ist_oauth.session_path(root))

        def read_step() -> dict[str, Any]:
            since = (now - timedelta(days=7)).isoformat()
            items, complete = live_readers.list_live(root, "ist-home", since)
            titles = [item.get("title") for item in items]
            return {
                "items": len(items),
                "complete": complete,
                "post_requests": provider.posts_calls,
                "ok": titles == ["Oppdiktet foreldremøte"] and complete is True,
            }

        if steps.run("ist_browser_sign_in", browser_step):
            steps.results["ist_browser_sign_in"]["profile_removed"] = not any(
                path.exists() for path in trial.run_paths
            ) and _bootstrap_folder_empty(root)
        for name, action in (
            ("ist_callback_captured", callback_step),
            ("ist_token_exchange", exchange_step),
            ("ist_session_private", session_step),
            ("ist_first_read", read_step),
        ):
            if not steps.run(name, action):
                break


# ----------------------------------------------------------------------------
# Spond: the session read from the login page in the browser.


def _page_html(title: str, script: str) -> bytes:
    return (
        f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
        f"<p>Oppdiktet side for prøven.</p><script>{script}</script>"
    ).encode("utf-8")


def _spond_page(fields: Mapping[str, str]) -> bytes:
    lines = "".join(
        f"localStorage.setItem({json.dumps(key)}, {json.dumps(value)});"
        for key, value in fields.items()
    )
    return _page_html("Oppdiktet Spond", lines + "window.proveKlar = true;")


def _profiles_removed(trial: Trial, before: int, root: Path) -> bool:
    profiles = trial.run_paths[before:]
    return (
        bool(profiles)
        and not any(path.exists() for path in profiles)
        and _bootstrap_folder_empty(root)
    )


def spond_trial(trial: Trial, root: Path, steps: Steps) -> None:
    from familiekilder import browser_bootstrap, spond_auth
    from familiekilder.private_state import atomic_write_private_json
    from familiekilder_testing.spond_auth import FakeAuthTransport

    now = datetime.now(UTC)
    fields = {
        "token": "prove-spond-tilgang-" + secrets.token_urlsafe(12),
        "tokenExpiration": (now + timedelta(hours=1)).isoformat(),
        "refreshToken": "prove-spond-fornyelse-" + secrets.token_urlsafe(12),
        "refreshTokenExpiration": (now + timedelta(days=30)).isoformat(),
    }
    transport = FakeAuthTransport()
    factory, wait_for_user = trial.page_factory(_spond_page(fields))
    before = len(trial.run_paths)

    def browser_step() -> dict[str, Any]:
        atomic_write_private_json(
            spond_auth.client_config_path(root),
            {"api_root": spond_auth.API_ROOT, "app_version": "1.0.0", "attachment_hosts": []},
        )
        with (
            patch.object(browser_bootstrap, "SPOND_ORIGIN", TRIAL_ORIGIN),
            patch.object(browser_bootstrap, "SPOND_LOGIN_URL", f"{TRIAL_ORIGIN}/client"),
        ):
            imported = browser_bootstrap.bootstrap_source(
                root,
                "spond",
                browser_factory=factory,
                wait_for_user=wait_for_user,
                importer=functools.partial(spond_auth.import_session, transport=transport),
                login_browser=trial.browser,
            )
        tokens_seen = [token for _url, token in transport.gets]
        served = factory.state.get("served", 0)  # type: ignore[attr-defined]
        return {
            "page_served": served,
            "remote_verified": imported.get("remote_verified"),
            "token_from_page_used": tokens_seen == [fields["token"]],
            "profile_removed": _profiles_removed(trial, before, root),
            "ok": served == 1
            and imported.get("authenticated") is True
            and tokens_seen == [fields["token"]],
        }

    def session_step() -> dict[str, Any]:
        result = _private_session_file(spond_auth.session_path(root))
        stored = spond_auth.session_path(root).read_text(encoding="utf-8")
        result["has_page_token"] = fields["token"] in stored
        result["ok"] = result["ok"] and result["has_page_token"]
        return result

    if steps.run("spond_browser_session_read", browser_step):
        steps.run("spond_session_private", session_step)


# ----------------------------------------------------------------------------


def _write_report(report: Mapping[str, Any]) -> None:
    # UTF-8 on every system; a Windows console may otherwise use cp850.
    line = json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n"
    sys.stdout.flush()
    sys.stdout.buffer.write(line.encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--browser", choices=("brave", "chrome", "edge"))
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--only-ist", action="store_true", help="Prøv bare IST Home")
    args = parser.parse_args()

    guard = LoopbackGuard()
    sys.addaudithook(guard)

    from familiekilder import desktop_platform, login_passwords
    from familiekilder.browsers import select_browser

    started = time.monotonic()
    report: dict[str, Any] = {"requested": args.browser}
    work = Path(tempfile.mkdtemp(prefix="familiekilder-login-trial-"))
    root = work / "datarot"
    root.mkdir(mode=0o700)
    store = RefusingPasswordStore()
    trial = Trial(args.browser, args.timeout)
    steps = Steps()
    try:
        with (
            patch.dict(os.environ, {"FAMILIEKILDER_ROOT": str(root)}),
            patch.object(desktop_platform, "password_backend", store),
        ):
            chosen = select_browser(args.browser)
            report["browser"] = chosen.app.id
            report["passwords_configured"] = login_passwords.configured(root, "bankid")
            ist_trial(trial, root, work, steps)
            if not args.only_ist:
                spond_trial(trial, root, steps)
    except Exception as error:  # noqa: BLE001 - the report shows the failure
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        report["product"] = trial.product
        report["steps"] = steps.results
        report["profiles_started"] = len(trial.run_paths)
        report["profiles_removed"] = bool(trial.run_paths) and not any(
            path.exists() for path in trial.run_paths
        )
        report["password_store_calls"] = store.calls
        report["outside_connections_refused"] = sorted(set(guard.refused))
        report["browser_requests_refused"] = sorted(set(trial.blocked_requests))
        shutil.rmtree(work, ignore_errors=True)
        report["trial_root_removed"] = not work.exists()
        report["seconds"] = round(time.monotonic() - started, 1)
    expected_steps = 5 if args.only_ist else 8
    report["ok"] = (
        "error" not in report
        and len(steps.results) == expected_steps
        and all(entry["ok"] for entry in steps.results.values())
        and report["profiles_removed"] is True
        and report.get("passwords_configured") is False
        and store.calls == 0
        and not guard.refused
        and report["trial_root_removed"] is True
    )
    _write_report(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
