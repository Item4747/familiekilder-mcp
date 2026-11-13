"""Google desktop OAuth with a short-lived loopback callback and PKCE."""
import base64
import hashlib
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .core import FamiliekilderError
from .gmail import Client, SCOPE
from .private_lock import private_source_lock
from .private_state import credential_path, read_private_json, atomic_write_private_json


def client_config(root):
    path = credential_path(root, "gmail-client.json")
    if not path.exists():
        raise FamiliekilderError("Importer en Google OAuth-klient av typen Desktop app")
    return read_private_json(path)


def configure_client(root, path):
    # Google's downloaded desktop client JSON; never accept a caller's token URL.
    raw = read_private_json(Path(path))
    value = raw.get("installed", raw)
    if ("web" in raw or not isinstance(value, dict)
            or not all(isinstance(value.get(k), str) and value[k] for k in ("client_id", "client_secret"))
            or not value["client_id"].endswith(".apps.googleusercontent.com")):
        raise FamiliekilderError("Google-klienten må være en Desktop app-klient")
    atomic_write_private_json(credential_path(root, "gmail-client.json"), {k: value[k] for k in ("client_id", "client_secret")})
    return {"configured": True}


def auth_status(root):
    path = credential_path(root, "gmail.json")
    configured = credential_path(root, "gmail-client.json").exists() or path.exists()
    value = read_private_json(path) if path.exists() else {}
    blocked = bool(value.get("refresh_blocked"))
    return {"configured": configured, "authenticated": bool(not blocked and value.get("refresh_token") and SCOPE in value.get("scope", "").split()),
            "refresh_blocked": blocked,
            "session_present": bool(value), "access_expired": bool(value and value.get("expires_at", 0) <= time.time()),
            "read_only": True}


def complete_login(root, config, redirect, verifier, expected_state, callback, *, client=None):
    parsed = urlsplit(callback)
    target = urlsplit(redirect)
    values = parse_qs(parsed.query)
    if (parsed.scheme, parsed.netloc, parsed.path) != (target.scheme, target.netloc, target.path):
        raise FamiliekilderError("Uventet Google-retur")
    if len(values.get("state", [])) != 1 or not secrets.compare_digest(values["state"][0], expected_state):
        raise FamiliekilderError("Ugyldig Google-returtilstand")
    if values.get("error") or len(values.get("code", [])) != 1:
        raise FamiliekilderError("Google-innloggingen ble ikke godkjent")
    own = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=False)
    try:
        response = client.post("https://oauth2.googleapis.com/token", data=config | {
            "grant_type": "authorization_code", "code": values["code"][0],
            "redirect_uri": redirect, "code_verifier": verifier})
        if response.status_code != 200:
            raise FamiliekilderError("Google-innloggingen kunne ikke fullføres")
        tokens = response.json() | {"_client_id": config["client_id"], "_client_secret": config["client_secret"]}
        return Client(root, client=client).grant(tokens)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise FamiliekilderError("Gmail-innloggingen kunne ikke verifiseres") from None
    finally:
        if own:
            client.close()


def login(root, *, cancelled=lambda: False, open_browser=webbrowser.open, timeout=600):
    config = dict(client_config(root))
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    callback = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            values = parse_qs(parsed.query)
            if (parsed.path != "/oauth/callback" or self.headers.get("Host") != authority
                    or values.get("state") != [state]):
                self.send_error(400)
                return
            callback.append(redirect + "?" + parsed.query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write("Google-returen er mottatt. Du kan lukke dette vinduet.".encode())

    # Serialize token replacement with all other Gmail traffic.
    with private_source_lock(root, "gmail"), HTTPServer(("127.0.0.1", 0), Handler) as server:
        authority = "127.0.0.1:" + str(server.server_port)
        redirect = "http://" + authority + "/oauth/callback"
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": config["client_id"], "redirect_uri": redirect, "response_type": "code",
            "scope": SCOPE, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent", "include_granted_scopes": "false"})
        open_browser(url)
        server.timeout = .5
        deadline = time.monotonic() + timeout
        while not callback and time.monotonic() < deadline and not cancelled():
            server.handle_request()
        if cancelled() or not callback:
            raise FamiliekilderError("Gmail-innloggingen ble avbrutt eller tok for lang tid")
        return complete_login(root, config, redirect, verifier, state, callback[0])


def run(args, root):
    from .auth_cli import _json
    if args.auth_command == "configure":
        result = configure_client(root, args.client_file)
    elif args.auth_command == "status":
        result = auth_status(root)
    elif args.auth_command in {"login", "browser-login"}:
        result = login(root)
    elif args.auth_command == "refresh":
        with private_source_lock(root, "gmail"):
            client = Client(root)
            try:
                client.token(force=not args.if_needed)
                client.get("/profile")
                result = auth_status(root)
            finally:
                client.close()
    elif args.auth_command == "logout":
        with private_source_lock(root, "gmail"):
            credential_path(root, "gmail.json").unlink(missing_ok=True)
            result = {"authenticated": False, "remote_revoked": False}
    else:
        raise FamiliekilderError("Gmail støtter configure, login, browser-login, status, refresh og logout")
    _json(result)
    return 0
