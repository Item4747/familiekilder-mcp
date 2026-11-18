"""A local, made-up IST Home sign-in service: OIDC endpoints and GraphQL on 127.0.0.1.

It signs real RS256 tokens with a key made for each server, checks PKCE and
answers the GraphQL queries the login check and the reader send. Nothing in it
is a real account, token or answer.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from familiekilder import ist_oauth as oauth


SCOPE = (
    "openid offline_access notification miniforum profile "
    "skolid.full-access consent live-translate-service"
)
CLIENT_ID = urllib.parse.urlsplit(oauth.REDIRECT_URI).scheme
CLIENT_SECRET = "local-fake-client-secret"
SUBJECT = "local-subject"
VALIDATION_TIME = 2_000_000_000


class LocalOidcServer:
    """OIDC and GraphQL over plain HTTP on 127.0.0.1.

    `validation_time` is the time the tokens are valid around and the
    server time GraphQL reports. When `expected_code` is set, the token
    endpoint accepts only that authorization code. `children` and `posts`
    answer the reader's context and post queries.
    """

    def __init__(self, *, validation_time: int = VALIDATION_TIME) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key()))
        jwk.update({"kid": "local-key", "use": "sig", "alg": "RS256"})
        self.jwks = {"keys": [jwk]}
        self.validation_time = validation_time
        self.expected_challenge = ""
        self.current_nonce = ""
        self.expected_code: str | None = None
        self.token_mode = "success"
        self.access_token = ""
        self.token_forms: list[dict[str, str]] = []
        self.userinfo_calls = 0
        self.context_calls = 0
        self.context_delays: list[float] = []
        self.redirect_target_hits = 0
        self.children: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []
        self.posts_calls = 0

        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _json(self, status: int, value: object) -> None:
                body = json.dumps(value, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> bytes:
                length = int(self.headers.get("Content-Length", "0"))
                return self.rfile.read(length)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                path = urllib.parse.urlsplit(self.path).path
                if path == "/jwks":
                    self._json(200, state.jwks)
                    return
                if path == "/userinfo":
                    if self.headers.get("Authorization") != f"Bearer {state.access_token}":
                        self._json(401, {"error": "invalid_token"})
                        return
                    state.userinfo_calls += 1
                    self._json(200, {"sub": SUBJECT, "name": "must-not-leave-server"})
                    return
                if path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", f"{state.base_url}/redirect-target")
                    self.end_headers()
                    return
                if path == "/redirect-target":
                    state.redirect_target_hits += 1
                    self._json(200, {"followed": True})
                    return
                if path == "/large":
                    body = json.dumps({"value": "x" * 512}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._json(404, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                path = urllib.parse.urlsplit(self.path).path
                raw = self._body()
                if path == "/token":
                    parsed = urllib.parse.parse_qs(
                        raw.decode("ascii"), keep_blank_values=True, strict_parsing=True
                    )
                    form = {key: values[0] for key, values in parsed.items()}
                    state.token_forms.append(form)
                    if state.token_mode == "invalid-client":
                        self._json(
                            400,
                            {
                                "error": "invalid_client",
                                "error_description": "server-leak-marker",
                            },
                        )
                        return
                    verifier = form.get("code_verifier", "")
                    if oauth.pkce_challenge(verifier) != state.expected_challenge or (
                        state.expected_code is not None and form.get("code") != state.expected_code
                    ):
                        self._json(
                            400,
                            {
                                "error": "invalid_grant",
                                "error_description": "server-leak-marker",
                            },
                        )
                        return
                    valid_from = state.validation_time
                    access_claims = {
                        "iss": state.base_url,
                        "aud": "miniforum",
                        "sub": SUBJECT,
                        "client_id": CLIENT_ID,
                        "scope": "openid miniforum",
                        "nbf": valid_from - 1,
                        "exp": valid_from + 600,
                    }
                    id_claims = {
                        "iss": state.base_url,
                        "aud": CLIENT_ID,
                        "azp": CLIENT_ID,
                        "sub": SUBJECT,
                        "nonce": state.current_nonce,
                        "nbf": valid_from - 1,
                        "exp": valid_from + 600,
                    }
                    headers = {"kid": "local-key", "typ": "JWT"}
                    state.access_token = jwt.encode(
                        access_claims,
                        state.private_key,
                        algorithm="RS256",
                        headers=headers,
                    )
                    id_token = jwt.encode(
                        id_claims,
                        state.private_key,
                        algorithm="RS256",
                        headers=headers,
                    )
                    self._json(
                        200,
                        {
                            "access_token": state.access_token,
                            "refresh_token": "local-refresh-token",
                            "id_token": id_token,
                            "token_type": "Bearer",
                            "expires_in": 600,
                            "scope": SCOPE,
                        },
                    )
                    return
                if path == "/graphql":
                    if self.headers.get("Authorization") != f"Bearer {state.access_token}":
                        self._json(401, {"errors": [{"message": "unauthorized"}]})
                        return
                    value = json.loads(raw)
                    query = str(value.get("query"))
                    if "getBlogPosts" in query:
                        state.posts_calls += 1
                        self._json(
                            200,
                            {
                                "data": {
                                    "getBlogPosts": {
                                        "nodes": state.posts,
                                        "page": {
                                            "startCursor": None,
                                            "endCursor": None,
                                            "hasNextPage": False,
                                        },
                                        "minRetentionTime": 30,
                                    }
                                }
                            },
                        )
                        return
                    if "getContext" not in query:
                        self._json(400, {"errors": [{"message": "wrong query"}]})
                        return
                    if state.context_delays:
                        time.sleep(state.context_delays.pop(0))
                    state.context_calls += 1
                    server_time = datetime.fromtimestamp(state.validation_time, UTC)
                    context: dict[str, Any] = {
                        "serverTime": server_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                    }
                    # Like GraphQL, answer only the fields the query asks for.
                    if "children" in query:
                        context["children"] = state.children
                    try:
                        self._json(200, {"data": {"getContext": context}})
                    except OSError:
                        pass  # The client gave up waiting.
                    return
                self._json(404, {"error": "not_found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def endpoints(self) -> dict[str, str]:
        """The IST Home endpoint constants, pointed at this server."""

        return {
            "ISSUER": self.base_url,
            "AUTHORITY": self.base_url,
            "AUTHORIZATION_ENDPOINT": f"{self.base_url}/authorize",
            "TOKEN_ENDPOINT": f"{self.base_url}/token",
            "USERINFO_ENDPOINT": f"{self.base_url}/userinfo",
            "REVOCATION_ENDPOINT": f"{self.base_url}/revoke",
            "JWKS_URI": f"{self.base_url}/jwks",
            "IST_GRAPHQL_ENDPOINT": f"{self.base_url}/graphql",
        }

    def __enter__(self) -> LocalOidcServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
