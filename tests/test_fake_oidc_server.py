from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from familiekilder import ist_oauth as oauth
from familiekilder import ist_oauth_protocol as protocol
from familiekilder_testing.oidc_server import (
    CLIENT_ID,
    CLIENT_SECRET,
    SCOPE,
    VALIDATION_TIME,
    LocalOidcServer,
)


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
