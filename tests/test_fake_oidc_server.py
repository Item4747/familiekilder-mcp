from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from familiekilder import ist_oauth as oauth
from familiekilder import ist_oauth_protocol as protocol


SCOPE = (
    "openid offline_access notification miniforum profile "
    "skolid.full-access consent live-translate-service"
)
CLIENT_ID = urllib.parse.urlsplit(oauth.REDIRECT_URI).scheme
CLIENT_SECRET = "local-fake-client-secret"
SUBJECT = "local-subject"
VALIDATION_TIME = 2_000_000_000


class LocalOidcServer:
    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key()))
        jwk.update({"kid": "local-key", "use": "sig", "alg": "RS256"})
        self.jwks = {"keys": [jwk]}
        self.expected_challenge = ""
        self.current_nonce = ""
        self.token_mode = "success"
        self.access_token = ""
        self.token_forms: list[dict[str, str]] = []
        self.userinfo_calls = 0
        self.context_calls = 0
        self.context_delays: list[float] = []
        self.redirect_target_hits = 0

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
                    if oauth.pkce_challenge(verifier) != state.expected_challenge:
                        self._json(
                            400,
                            {
                                "error": "invalid_grant",
                                "error_description": "server-leak-marker",
                            },
                        )
                        return
                    access_claims = {
                        "iss": state.base_url,
                        "aud": "miniforum",
                        "sub": SUBJECT,
                        "client_id": CLIENT_ID,
                        "scope": "openid miniforum",
                        "nbf": VALIDATION_TIME - 1,
                        "exp": VALIDATION_TIME + 600,
                    }
                    id_claims = {
                        "iss": state.base_url,
                        "aud": CLIENT_ID,
                        "azp": CLIENT_ID,
                        "sub": SUBJECT,
                        "nonce": state.current_nonce,
                        "nbf": VALIDATION_TIME - 1,
                        "exp": VALIDATION_TIME + 600,
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
                    if "getContext" not in str(value.get("query")):
                        self._json(400, {"errors": [{"message": "wrong query"}]})
                        return
                    if state.context_delays:
                        time.sleep(state.context_delays.pop(0))
                    state.context_calls += 1
                    try:
                        self._json(
                            200,
                            {"data": {"getContext": {"serverTime": "2033-05-18T03:33:20Z"}}},
                        )
                    except OSError:
                        pass  # The client gave up waiting.
                    return
                self._json(404, {"error": "not_found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> LocalOidcServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class FakeRevocation:
    def post_form(self, url, fields, *, expect_json=True):
        return None


class LocalOidcIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _callback(authorization_url: str, issuer: str) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(authorization_url).query)
        return (
            f"{oauth.REDIRECT_URI}?code=local-code"
            f"&state={urllib.parse.quote(query['state'][0])}"
            f"&iss={urllib.parse.quote(issuer, safe='')}"
        )

    def test_real_http_form_pkce_rs256_jwks_and_remote_checks(self) -> None:
        with LocalOidcServer() as provider:
            endpoints = {
                "ISSUER": provider.base_url,
                "AUTHORITY": provider.base_url,
                "AUTHORIZATION_ENDPOINT": f"{provider.base_url}/authorize",
                "TOKEN_ENDPOINT": f"{provider.base_url}/token",
                "USERINFO_ENDPOINT": f"{provider.base_url}/userinfo",
                "REVOCATION_ENDPOINT": f"{provider.base_url}/revoke",
                "JWKS_URI": f"{provider.base_url}/jwks",
                "IST_GRAPHQL_ENDPOINT": f"{provider.base_url}/graphql",
            }
            pinned = frozenset(
                {
                    endpoints["TOKEN_ENDPOINT"],
                    endpoints["USERINFO_ENDPOINT"],
                    endpoints["REVOCATION_ENDPOINT"],
                    endpoints["JWKS_URI"],
                    endpoints["IST_GRAPHQL_ENDPOINT"],
                }
            )
            with (
                patch.multiple(oauth, **endpoints),
                patch.multiple(protocol, **endpoints, _PINNED_NETWORK_URLS=pinned),
            ):
                client_file = self.root / "client.json"
                client_file.write_text(
                    json.dumps(
                        {
                            "authority": provider.base_url,
                            "client_id": CLIENT_ID,
                            "client_secret": CLIENT_SECRET,
                            "redirect_uri": oauth.REDIRECT_URI,
                            "scope": SCOPE,
                        }
                    ),
                    encoding="utf-8",
                )
                oauth.configure_client(self.root, client_file)
                oauth.record_consent(self.root, at=VALIDATION_TIME)

                # Deliberately break PKCE.  The provider must reject it and the
                # one-shot transaction must be consumed without leaking its body.
                values = iter((b"s" * 32, b"n" * 32, b"v" * 32))
                url, transaction = oauth.begin_login(
                    self.root,
                    now=100,
                    random_bytes=lambda _length: next(values),
                )
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                provider.expected_challenge = query["code_challenge"][0]
                provider.current_nonce = query["nonce"][0]
                transaction.code_verifier = "A" * 43
                with self.assertRaises(oauth.OAuthError) as rejected:
                    oauth.login_with_callback(
                        self.root,
                        transaction,
                        self._callback(url, provider.base_url),
                        now=101,
                        wall_time=VALIDATION_TIME,
                    )
                self.assertTrue(transaction.consumed)
                self.assertNotIn("server-leak-marker", str(rejected.exception))
                self.assertFalse(oauth.session_path(self.root).exists())

                # Exercise fatal HTTP OAuth classification through the default
                # urllib transport, again without exposing error_description.
                provider.token_mode = "invalid-client"
                values = iter((b"a" * 32, b"b" * 32, b"c" * 32))
                fatal_url, fatal_transaction = oauth.begin_login(
                    self.root,
                    now=200,
                    random_bytes=lambda _length: next(values),
                )
                fatal_query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(fatal_url).query
                )
                provider.expected_challenge = fatal_query["code_challenge"][0]
                provider.current_nonce = fatal_query["nonce"][0]
                with self.assertRaises(oauth.OAuthFatalConfigurationError) as fatal:
                    oauth.login_with_callback(
                        self.root,
                        fatal_transaction,
                        self._callback(fatal_url, provider.base_url),
                        now=201,
                        wall_time=VALIDATION_TIME,
                    )
                self.assertNotIn("server-leak-marker", str(fatal.exception))
                self.assertFalse(oauth.session_path(self.root).exists())

                provider.token_mode = "success"
                oauth.configure_client(self.root, client_file)
                values = iter((b"d" * 32, b"e" * 32, b"f" * 32))
                good_url, good_transaction = oauth.begin_login(
                    self.root,
                    now=300,
                    random_bytes=lambda _length: next(values),
                )
                good_query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(good_url).query
                )
                provider.expected_challenge = good_query["code_challenge"][0]
                provider.current_nonce = good_query["nonce"][0]
                result = oauth.login_with_callback(
                    self.root,
                    good_transaction,
                    self._callback(good_url, provider.base_url),
                    now=301,
                    wall_time=VALIDATION_TIME,
                )

                self.assertTrue(result["authenticated"])
                self.assertTrue(result["id_token_verified"])
                self.assertEqual(provider.userinfo_calls, 1)
                self.assertEqual(provider.context_calls, 1)
                form = provider.token_forms[-1]
                self.assertEqual(form["grant_type"], "authorization_code")
                self.assertEqual(form["client_id"], CLIENT_ID)
                self.assertEqual(form["client_secret"], CLIENT_SECRET)
                self.assertEqual(form["redirect_uri"], oauth.REDIRECT_URI)
                self.assertEqual(form["scope"], SCOPE)
                self.assertEqual(
                    oauth.pkce_challenge(form["code_verifier"]),
                    provider.expected_challenge,
                )
                stored = json.loads(oauth.session_path(self.root).read_text())
                self.assertNotIn("id_token", stored)
                self.assertEqual(stored["client_id"], CLIENT_ID)

    def _login_over_http(self, provider: LocalOidcServer, transport) -> tuple[object, list[str]]:
        endpoints = {
            "ISSUER": provider.base_url,
            "AUTHORITY": provider.base_url,
            "AUTHORIZATION_ENDPOINT": f"{provider.base_url}/authorize",
            "TOKEN_ENDPOINT": f"{provider.base_url}/token",
            "USERINFO_ENDPOINT": f"{provider.base_url}/userinfo",
            "REVOCATION_ENDPOINT": f"{provider.base_url}/revoke",
            "JWKS_URI": f"{provider.base_url}/jwks",
            "IST_GRAPHQL_ENDPOINT": f"{provider.base_url}/graphql",
        }
        pinned = frozenset(
            value
            for key, value in endpoints.items()
            if key not in {"ISSUER", "AUTHORITY", "AUTHORIZATION_ENDPOINT"}
        )
        with (
            patch.multiple(oauth, **endpoints, CONTEXT_RETRY_DELAY_SECONDS=0),
            patch.multiple(protocol, **endpoints, _PINNED_NETWORK_URLS=pinned),
        ):
            client_file = self.root / "client.json"
            client_file.write_text(
                json.dumps(
                    {
                        "authority": provider.base_url,
                        "client_id": CLIENT_ID,
                        "client_secret": CLIENT_SECRET,
                        "redirect_uri": oauth.REDIRECT_URI,
                        "scope": SCOPE,
                    }
                ),
                encoding="utf-8",
            )
            oauth.configure_client(self.root, client_file)
            oauth.record_consent(self.root, at=VALIDATION_TIME)
            url, transaction = oauth.begin_login(self.root, now=100)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            provider.expected_challenge = query["code_challenge"][0]
            provider.current_nonce = query["nonce"][0]
            try:
                result = oauth.login_with_callback(
                    self.root,
                    transaction,
                    self._callback(url, provider.base_url),
                    transport=transport,
                    now=101,
                    wall_time=VALIDATION_TIME,
                )
            except oauth.OAuthError as exc:
                result = exc
        history = json.loads(
            (self.root / ".private/credentials/ist-home-auth-history.json").read_text()
        )
        return result, [event["event"] for event in history["events"]]

    def test_context_check_waits_longer_than_skolid_and_asks_again_after_silence(self) -> None:
        # 10 Nov 2026: getContext gave no answer within the 30 s SkolID timeout.
        with LocalOidcServer() as provider:
            provider.context_delays = [0.6]
            transport = protocol.UrllibOAuthTransport(timeout=0.3, context_timeout=2.0)
            result, events = self._login_over_http(provider, transport)
            self.assertTrue(result["context_verified"])
            self.assertEqual(provider.context_calls, 1)
            self.assertEqual(events, ["response_received", "login_succeeded"])

        oauth.logout(self.root, transport=FakeRevocation())
        with LocalOidcServer() as provider:
            provider.context_delays = [1.0]
            transport = protocol.UrllibOAuthTransport(timeout=0.3, context_timeout=0.3)
            with self.assertLogs("familiekilder", "WARNING") as logged:
                result, events = self._login_over_http(provider, transport)
            self.assertTrue(result["context_verified"])
            self.assertEqual(
                events[-3:], ["response_received", "request_uncertain", "login_succeeded"]
            )
            # A bare TimeoutError: the request was sent and no answer came.
            self.assertRegex(
                logged.output[0],
                r"ist_oauth_request_failed host=127\.0\.0\.1 error=TimeoutError reason=- "
                r"elapsed_ms=\d+ timeout_s=0\.3$",
            )
            self.assertRegex(logged.output[1], r"attempt=1 outcome=no_answer")
            self.assertRegex(logged.output[2], r"attempt=2 outcome=answered")
            for line in logged.output:
                self.assertNotIn(provider.access_token, line)

    def test_default_transport_rejects_redirects_and_oversized_json(self) -> None:
        with LocalOidcServer() as provider:
            redirect = f"{provider.base_url}/redirect"
            with patch.object(protocol, "_PINNED_NETWORK_URLS", frozenset({redirect})):
                with self.assertRaises(oauth.OAuthError):
                    oauth.UrllibOAuthTransport().get_json(redirect)
            self.assertEqual(provider.redirect_target_hits, 0)

            large = f"{provider.base_url}/large"
            with patch.object(protocol, "_PINNED_NETWORK_URLS", frozenset({large})):
                with self.assertRaisesRegex(oauth.OAuthError, "for mye data"):
                    oauth.UrllibOAuthTransport(max_bytes=64).get_json(large)

            jwks = f"{provider.base_url}/jwks"
            hostile_proxy = "http://127.0.0.1:1"
            with (
                patch.object(protocol, "_PINNED_NETWORK_URLS", frozenset({jwks})),
                patch.dict(
                    os.environ,
                    {
                        "HTTP_PROXY": hostile_proxy,
                        "http_proxy": hostile_proxy,
                        "NO_PROXY": "",
                        "no_proxy": "",
                    },
                    clear=False,
                ),
            ):
                self.assertEqual(
                    oauth.UrllibOAuthTransport().get_json(jwks),
                    provider.jwks,
                )


if __name__ == "__main__":
    unittest.main()
