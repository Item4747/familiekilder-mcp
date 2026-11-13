from __future__ import annotations

import base64
import hashlib
import hmac
import json
import tempfile
import threading
import unittest
import urllib.parse
from unittest.mock import patch
from pathlib import Path

from familiekilder.api_transport import ApiHTTPError, ApiTransportError
import familiekilder.skolemelding_auth as skolemelding_auth
from familiekilder.skolemelding_auth import (
    APP_TOKEN_ENDPOINT,
    DEVICE_INFO_ENDPOINT,
    PORTAL_TOKEN_ENDPOINT,
    REDIRECT_URI,
    SkolemeldingAuthError,
    SkolemeldingReauthenticationRequired,
    SkolemeldingRefreshUncertain,
    auth_status,
    begin_login,
    configure_client,
    device_path,
    local_session_for_use,
    login_with_callback,
    logout,
    pkce_challenge,
    record_consent,
    refresh_block_path,
    refresh_session,
)


AUTHORIZATION_ENDPOINT = "https://login.idporten.no/authorize"
OIDC_TOKEN_ENDPOINT = "https://login.idporten.no/token"


class DeterministicRandom:
    def __init__(self) -> None:
        self.counter = 0

    def __call__(self, size: int) -> bytes:
        self.counter += 1
        return bytes([self.counter]) * size


class FakeAuthTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.refresh_response: object = {
            "access_token": "portal-access-2",
            "refresh_token": "portal-refresh-2",
            "userName": "guardian-test",
            "expires_in": 7200,
        }

    def get_json(self, url: str, **kwargs: object) -> object:
        self.calls.append(("GET", url, dict(kwargs)))
        raise AssertionError("discovery should not be needed with configured endpoints")

    def post_json(
        self, url: str, payload: dict[str, object], **kwargs: object
    ) -> object:
        self.calls.append(("POST_JSON", url, dict(payload)))
        if url == DEVICE_INFO_ENDPOINT:
            self.device_registration_options = dict(kwargs)
            return None
        if url != APP_TOKEN_ENDPOINT:
            raise AssertionError("unexpected JSON endpoint")
        return {"authCode": "one-time-portal-code"}

    def post_form(
        self, url: str, fields: dict[str, str], **kwargs: object
    ) -> object:
        self.calls.append(("POST_FORM", url, dict(fields)))
        if url == OIDC_TOKEN_ENDPOINT:
            return {"access_token": "idporten-access"}
        if url != PORTAL_TOKEN_ENDPOINT:
            raise AssertionError("unexpected form endpoint")
        if fields["grant_type"] == "refresh_token":
            if isinstance(self.refresh_response, Exception):
                raise self.refresh_response
            return self.refresh_response
        return {
            "access_token": "portal-access-1",
            "refresh_token": "portal-refresh-1",
            "userName": "guardian-test",
            "expires_in": 3600,
        }


class ConcurrentLoginTransport(FakeAuthTransport):
    def __init__(self, barrier: threading.Barrier, label: str) -> None:
        super().__init__()
        self.barrier = barrier
        self.label = label

    def post_form(
        self, url: str, fields: dict[str, str], **kwargs: object
    ) -> object:
        if url == PORTAL_TOKEN_ENDPOINT and fields["grant_type"] == "password":
            self.calls.append(("POST_FORM", url, dict(fields)))
            self.barrier.wait(timeout=5)
            return {
                "access_token": f"concurrent-access-{self.label}",
                "refresh_token": f"concurrent-refresh-{self.label}",
                "userName": "guardian-test",
                "expires_in": 3600,
            }
        return super().post_form(url, fields, **kwargs)


def _configure(root: Path) -> None:
    private = root / ".private"
    private.mkdir(mode=0o700, exist_ok=True)
    private.chmod(0o700)
    bootstrap = private / "bootstrap"
    bootstrap.mkdir(mode=0o700, parents=True, exist_ok=True)
    bootstrap.chmod(0o700)
    metadata = bootstrap / "skolemelding-client-metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "oidc_client_id": "test-public-oidc-client",
                "portal_client_id": "test-public-portal-client",
                "app_version": "0.0-test",
                "issuer": "https://idporten.no",
                "redirect_uri": REDIRECT_URI,
                "scopes": ["openid", "profile"],
                "authorization_endpoint": AUTHORIZATION_ENDPOINT,
                "oidc_token_endpoint": OIDC_TOKEN_ENDPOINT,
                "attachment_hosts": ["portal.skoleplattform.no"],
                "attachment_path_prefixes": ["/api/files/"],
            }
        ),
        encoding="utf-8",
    )
    metadata.chmod(0o600)
    result = configure_client(root, metadata)
    if result != {
        "configured": True,
        "issuer": "https://idporten.no",
        "redirect_uri": REDIRECT_URI,
        "scopes_validated": True,
    }:
        raise AssertionError("unexpected configure result")
    record_consent(root, at=1_700_000_000)


def _login(
    root: Path, transport: FakeAuthTransport, random: DeterministicRandom | None = None
) -> tuple[object, dict[str, object]]:
    random = random or DeterministicRandom()
    _url, transaction = begin_login(root, now=10, random_bytes=random)
    callback = f"{REDIRECT_URI}?code=authorization-code&state={transaction.state}"
    result = login_with_callback(
        root,
        transaction,
        callback,
        transport=transport,
        now=20,
        wall_time=1_700_000_100,
    )
    return transaction, result


def _assert_secret_free(test: unittest.TestCase, value: object) -> None:
    encoded = json.dumps(value, sort_keys=True)
    for secret in (
        "portal-access-1",
        "portal-refresh-1",
        "guardian-test",
        "one-time-portal-code",
        "authorization-code",
        "idporten-access",
    ):
        test.assertNotIn(secret, encoded)


class SkolemeldingAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_device_registration_uses_new_access_token_and_actual_os_without_push(self):
        _configure(self.root)
        transport = FakeAuthTransport()
        with patch.object(skolemelding_auth.platform, "system", return_value="Darwin"), patch.object(
            skolemelding_auth.platform, "release", return_value="25.0.0"
        ):
            _, result = _login(self.root, transport)
        self.assertTrue(result["device_registered"])
        self.assertEqual(transport.calls[-1], (
            "POST_JSON", DEVICE_INFO_ENDPOINT,
            {"OsVersion": "darwin|25.0.0", "AppVersion": "0.0-test"},
        ))
        self.assertEqual(transport.device_registration_options,
                         {"bearer_token": "portal-access-1", "allow_empty": True})
        self.assertIn(DEVICE_INFO_ENDPOINT, skolemelding_auth._default_transport().allowed_urls)

    def test_device_registration_failure_does_not_activate_partial_login(self):
        _configure(self.root)
        for failure in (ApiHTTPError(400), ApiHTTPError(500), ApiTransportError("timeout"), ApiHTTPError(400, "invalid_client")):
            transport = FakeAuthTransport()
            original = transport.post_json
            def post(url, payload, **kwargs):
                if url == DEVICE_INFO_ENDPOINT:
                    raise failure
                return original(url, payload, **kwargs)
            transport.post_json = post
            with self.subTest(failure=type(failure).__name__), self.assertRaises(SkolemeldingAuthError):
                _login(self.root, transport)
            self.assertFalse(skolemelding_auth.session_path(self.root).exists())
            self.assertFalse(refresh_block_path(self.root).exists())

    def test_failed_registration_preserves_previous_session_and_refresh_barrier(self):
        _configure(self.root)
        _login(self.root, FakeAuthTransport())
        previous = skolemelding_auth.session_path(self.root).read_bytes()
        transport = FakeAuthTransport()
        transport.refresh_response = ApiHTTPError(400, "invalid_grant")
        with self.assertRaises(SkolemeldingReauthenticationRequired):
            refresh_session(self.root, transport=transport)
        barrier = refresh_block_path(self.root).read_bytes()
        _, transaction = begin_login(self.root, now=10)
        transport.post_json = lambda *args, **kwargs: {"authCode": "new-code"}
        original = transport.post_json
        def post(url, payload, **kwargs):
            if url == DEVICE_INFO_ENDPOINT:
                raise ApiTransportError("timeout")
            return original(url, payload, **kwargs)
        transport.post_json = post
        with self.assertRaises(SkolemeldingAuthError):
            login_with_callback(
                self.root, transaction,
                f"{REDIRECT_URI}?code=new-code&state={transaction.state}",
                transport=transport, now=20, replace_existing=True,
            )
        self.assertEqual(skolemelding_auth.session_path(self.root).read_bytes(), previous)
        self.assertEqual(refresh_block_path(self.root).read_bytes(), barrier)

    def test_state_pkce_exchange_bootstrap_and_secret_free_results(self) -> None:
        _configure(self.root)
        transport = FakeAuthTransport()
        random = DeterministicRandom()

        authorization_url, transaction = begin_login(
            self.root, now=10, random_bytes=random
        )
        parsed = urllib.parse.urlsplit(authorization_url)
        parameters = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}", AUTHORIZATION_ENDPOINT
        )
        self.assertEqual(parameters["response_type"], ["code"])
        self.assertEqual(parameters["scope"], ["openid profile"])
        self.assertEqual(parameters["prompt"], ["login"])
        self.assertEqual(parameters["code_challenge_method"], ["S256"])
        self.assertEqual(
            parameters["code_challenge"], [pkce_challenge(transaction.code_verifier)]
        )
        self.assertEqual(parameters["state"], [transaction.state])

        wrong_callback = f"{REDIRECT_URI}?code=authorization-code&state=wrong"
        with self.assertRaisesRegex(SkolemeldingAuthError, "state"):
            login_with_callback(
                self.root,
                transaction,
                wrong_callback,
                transport=transport,
                now=20,
                wall_time=1_700_000_100,
            )
        self.assertFalse(transaction.consumed)
        self.assertEqual(transport.calls, [])

        callback = (
            f"{REDIRECT_URI}?code=authorization-code&state={transaction.state}"
            "&iss=https%3A%2F%2Fidporten.no"
        )
        result = login_with_callback(
            self.root,
            transaction,
            callback,
            transport=transport,
            now=20,
            wall_time=1_700_000_100,
        )
        _assert_secret_free(self, result)
        self.assertTrue(transaction.consumed)
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["provider"], "idporten")

        oidc_call, app_exchange, portal_bootstrap, device_registration = transport.calls
        self.assertEqual(oidc_call[1], OIDC_TOKEN_ENDPOINT)
        self.assertEqual(oidc_call[2]["grant_type"], "authorization_code")
        self.assertEqual(oidc_call[2]["code_verifier"], transaction.code_verifier)
        self.assertEqual(
            app_exchange,
            (
                "POST_JSON",
                APP_TOKEN_ENDPOINT,
                {"accessToken": "idporten-access", "provider": "idporten"},
            ),
        )
        self.assertEqual(portal_bootstrap[1], PORTAL_TOKEN_ENDPOINT)
        self.assertEqual(portal_bootstrap[2]["grant_type"], "password")
        self.assertEqual(portal_bootstrap[2]["username"], "one-time-portal-code")
        self.assertEqual(portal_bootstrap[2]["scope"], transaction.device_unique_id)
        # Literal vendor protocol marker, never a user password.
        self.assertEqual(portal_bootstrap[2]["password"], "code")
        self.assertTrue(
            all(
                "pin" not in url.lower() and "biometric" not in url.lower()
                for _method, url, _payload in transport.calls
            )
        )

        session = local_session_for_use(self.root)
        self.assertEqual(session["access_token"], "portal-access-1")
        self.assertEqual(session["refresh_token"], "portal-refresh-1")
        self.assertEqual(session["userName"], "guardian-test")
        self.assertEqual(session["device_unique_id"], transaction.device_unique_id)
        self.assertEqual(session["access_expires_at"], 1_700_003_700)
        status = auth_status(self.root)
        _assert_secret_free(self, status)
        self.assertTrue(status["session_present"])
        self.assertTrue(status["authenticated"])
        self.assertIsNotNone(status["expires_at"])

    def test_mutable_profile_metadata_updates_without_reauthentication(self) -> None:
        _configure(self.root)
        _login(self.root, FakeAuthTransport())
        bootstrap = self.root / ".private" / "bootstrap"

        def write_profile(name: str, **changes: object) -> Path:
            value = {
                "oidc_client_id": "test-public-oidc-client",
                "portal_client_id": "test-public-portal-client",
                "app_version": "0.1-test",
                "issuer": "https://idporten.no",
                "redirect_uri": REDIRECT_URI,
                "scopes": ["openid", "profile"],
                "authorization_endpoint": AUTHORIZATION_ENDPOINT,
                "oidc_token_endpoint": OIDC_TOKEN_ENDPOINT,
                "attachment_hosts": ["portal.skoleplattform.no"],
                "attachment_path_prefixes": ["/api/files/"],
            }
            value.update(changes)
            path = bootstrap / name
            path.write_text(json.dumps(value), encoding="utf-8")
            path.chmod(0o600)
            return path

        configure_client(
            self.root,
            write_profile(
                "mutable.json",
                attachment_hosts=["files.skoleplattform.no"],
                attachment_path_prefixes=["/download/"],
            ),
        )
        configured = skolemelding_auth.load_client_config(self.root)
        self.assertEqual(configured.app_version, "0.1-test")
        self.assertEqual(configured.attachment_hosts, ("files.skoleplattform.no",))
        self.assertEqual(configured.attachment_path_prefixes, ("/download/",))
        self.assertEqual(
            local_session_for_use(self.root)["access_token"], "portal-access-1"
        )

        with self.assertRaisesRegex(SkolemeldingAuthError, "auth-identitet"):
            configure_client(
                self.root,
                write_profile(
                    "immutable.json", portal_client_id="different-portal-client"
                ),
            )

    def test_device_identifier_survives_local_logout_and_new_login(self) -> None:
        _configure(self.root)
        transport = FakeAuthTransport()
        first, _result = _login(self.root, transport)
        logout_result = logout(self.root)
        self.assertTrue(logout_result["device_id_retained"])

        second_random = DeterministicRandom()
        _url, second = begin_login(self.root, now=30, random_bytes=second_random)
        self.assertEqual(second.device_unique_id, first.device_unique_id)
        self.assertNotEqual(second.state, first.state)

    def test_attachment_routes_fail_closed_unless_both_lists_are_safe(self) -> None:
        _configure(self.root)
        current = skolemelding_auth.load_client_config(self.root).as_private_json()
        bootstrap = self.root / ".private" / "bootstrap"

        for name, changes in (
            ("host-only.json", {"attachment_path_prefixes": []}),
            ("prefix-only.json", {"attachment_hosts": []}),
            ("encoded-prefix.json", {"attachment_path_prefixes": ["/api/%66iles/"]}),
        ):
            candidate = {**current, **changes}
            path = bootstrap / name
            path.write_text(json.dumps(candidate), encoding="utf-8")
            path.chmod(0o600)
            with self.subTest(name=name):
                with self.assertRaises(SkolemeldingAuthError):
                    configure_client(self.root, path)

    def test_status_never_recreates_a_missing_device_identifier(self) -> None:
        _configure(self.root)
        _login(self.root, FakeAuthTransport())
        device_path(self.root).unlink()
        before = sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
        )

        status = auth_status(self.root)

        after = sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
        )
        self.assertEqual(before, after)
        self.assertFalse(device_path(self.root).exists())
        self.assertFalse(status["device_id_present"])
        self.assertFalse(status["authenticated"])

        with self.assertRaises(SkolemeldingReauthenticationRequired):
            local_session_for_use(self.root)
        self.assertFalse(device_path(self.root).exists())

    def test_concurrent_callbacks_cannot_overwrite_first_committed_session(self) -> None:
        _configure(self.root)
        random = DeterministicRandom()
        _first_url, first = begin_login(self.root, now=10, random_bytes=random)
        _second_url, second = begin_login(self.root, now=10, random_bytes=random)
        barrier = threading.Barrier(2)
        transports = {
            "first": ConcurrentLoginTransport(barrier, "first"),
            "second": ConcurrentLoginTransport(barrier, "second"),
        }
        transactions = {"first": first, "second": second}
        successes: list[str] = []
        failures: list[Exception] = []
        result_lock = threading.Lock()

        def complete(label: str) -> None:
            transaction = transactions[label]
            callback = (
                f"{REDIRECT_URI}?code=authorization-code&state={transaction.state}"
            )
            try:
                login_with_callback(
                    self.root,
                    transaction,
                    callback,
                    transport=transports[label],
                    now=20,
                    wall_time=1_700_000_100,
                )
            except Exception as exc:
                with result_lock:
                    failures.append(exc)
            else:
                with result_lock:
                    successes.append(label)

        threads = [
            threading.Thread(target=complete, args=(label,), daemon=True)
            for label in ("first", "second")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], SkolemeldingAuthError)
        session = local_session_for_use(self.root)
        expected_access_token = f"concurrent-access-{successes[0]}"
        expected_refresh_token = f"concurrent-refresh-{successes[0]}"
        self.assertTrue(
            hmac.compare_digest(session["access_token"], expected_access_token)
        )
        self.assertTrue(
            hmac.compare_digest(session["refresh_token"], expected_refresh_token)
        )

    def test_refresh_rotates_tokens_and_clears_inflight_marker(self) -> None:
        _configure(self.root)
        transport = FakeAuthTransport()
        _login(self.root, transport)
        transport.calls.clear()

        result = refresh_session(self.root, transport=transport, at=1_700_000_200)
        _assert_secret_free(self, result)
        self.assertTrue(result["refreshed"])
        self.assertTrue(result["refresh_token_rotated"])
        self.assertFalse(refresh_block_path(self.root).exists())
        session = local_session_for_use(self.root)
        self.assertEqual(session["access_token"], "portal-access-2")
        self.assertEqual(session["refresh_token"], "portal-refresh-2")
        call = transport.calls[-1]
        self.assertEqual(call[1], PORTAL_TOKEN_ENDPOINT)
        self.assertEqual(
            call[2],
            {
                "grant_type": "refresh_token",
                "refresh_token": "portal-refresh-1",
                "client_id": "test-public-portal-client",
                "app_version": "0.0-test",
                "username": "guardian-test",
            },
        )

    def test_failed_refresh_blocks_automatic_reuse(self) -> None:
        cases = (
            (ApiHTTPError(400, "invalid_grant"), "invalid-grant", SkolemeldingReauthenticationRequired),
            (ApiHTTPError(400, "invalid_refresh_token"), "invalid-grant", SkolemeldingReauthenticationRequired),
            (ApiTransportError("sanitized network failure"), "refresh-in-flight", SkolemeldingRefreshUncertain),
        )
        for failure, reason, error_type in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _configure(root)
                transport = FakeAuthTransport()
                _login(root, transport)
                transport.refresh_response = failure

                with self.assertRaises(error_type):
                    refresh_session(root, transport=transport, at=1_700_000_300)
                self.assertTrue(refresh_block_path(root).is_file())
                status = auth_status(root)
                _assert_secret_free(self, status)
                self.assertTrue(status["refresh_blocked"])
                self.assertEqual(status["refresh_block_reason"], reason)
                self.assertFalse(status["authenticated"])
                with self.assertRaises(error_type):
                    local_session_for_use(root)

    def test_received_refresh_survives_crash_before_session_write(self) -> None:
        _configure(self.root)
        transport = FakeAuthTransport()
        _login(self.root, transport)
        original_write = skolemelding_auth.atomic_write_private_json

        def crash_on_session(path, value, **kwargs):
            if path == skolemelding_auth.session_path(self.root):
                raise skolemelding_auth.PrivateStateError("simulated disk failure")
            return original_write(path, value, **kwargs)

        with patch.object(skolemelding_auth, "atomic_write_private_json", side_effect=crash_on_session):
            with self.assertRaises(skolemelding_auth.PrivateStateError):
                refresh_session(self.root, transport=transport, at=1_700_000_200)
        calls = len(transport.calls)
        before = refresh_block_path(self.root).read_bytes()
        state = auth_status(self.root)
        _assert_secret_free(self, state)
        self.assertTrue(state["refresh_response_saved"])
        self.assertEqual(refresh_block_path(self.root).read_bytes(), before)
        session = local_session_for_use(self.root)
        self.assertEqual(session["refresh_token"], "portal-refresh-2")
        self.assertEqual(session["access_expires_at"], 1_700_007_400)
        self.assertEqual(len(transport.calls), calls)
        self.assertFalse(refresh_block_path(self.root).exists())

    def test_invalid_refresh_response_is_retained_without_exposing_tokens(self) -> None:
        _configure(self.root)
        transport = FakeAuthTransport()
        _login(self.root, transport)
        transport.refresh_response = {"access_token": "portal-access-2", "refresh_token": "portal-refresh-2"}
        with self.assertRaises(SkolemeldingRefreshUncertain):
            refresh_session(self.root, transport=transport, at=1_700_000_200)
        state = auth_status(self.root)
        _assert_secret_free(self, state)
        self.assertTrue(state["refresh_response_saved"])
        calls = len(transport.calls)
        with self.assertRaises(SkolemeldingRefreshUncertain):
            local_session_for_use(self.root)
        self.assertEqual(len(transport.calls), calls)

    def test_unknown_http_failure_retains_sanitized_diagnostic(self) -> None:
        _configure(self.root)
        transport = FakeAuthTransport()
        _login(self.root, transport)
        transport.refresh_response = ApiHTTPError(502)
        with self.assertRaises(SkolemeldingRefreshUncertain):
            refresh_session(self.root, transport=transport, at=1_700_000_200)
        state = auth_status(self.root)
        _assert_secret_free(self, state)
        self.assertEqual(state["refresh_diagnostic"], {"stage": "request", "kind": "http", "http_status": 502})

    def test_refresh_cannot_switch_portal_identity(self) -> None:
        _configure(self.root)
        transport = FakeAuthTransport()
        _login(self.root, transport)
        original = local_session_for_use(self.root)
        transport.refresh_response = {
            "access_token": "different-access",
            "refresh_token": "different-refresh",
            "userName": "different-guardian",
            "expires_in": 7200,
        }

        with self.assertRaises(SkolemeldingReauthenticationRequired):
            refresh_session(self.root, transport=transport, at=1_700_000_300)

        with self.assertRaises(SkolemeldingReauthenticationRequired):
            local_session_for_use(self.root)

        self.assertTrue(refresh_block_path(self.root).is_file())
        # Inspect the on-disk object directly because the durable in-flight
        # marker correctly blocks the public secret-bearing accessor.
        stored = json.loads(
            (self.root / ".private/credentials/skolemelding-session.json").read_text()
        )
        self.assertTrue(
            hmac.compare_digest(stored["access_token"], original["access_token"])
        )
        self.assertTrue(
            hmac.compare_digest(stored["userName"], original["userName"])
        )

    def test_refresh_journal_cannot_be_applied_to_another_session(self) -> None:
        _configure(self.root)
        transport = FakeAuthTransport()
        _login(self.root, transport)
        session = local_session_for_use(self.root)
        skolemelding_auth.atomic_write_private_json(refresh_block_path(self.root), {
            "reason": "refresh-response-received", "transaction_id": "synthetic-transaction",
            "received_at": 1_700_000_200, "session_binding": "0" * 64,
            "response": transport.refresh_response,
        })
        with self.assertRaises(SkolemeldingRefreshUncertain):
            local_session_for_use(self.root)
        stored = skolemelding_auth._read_session(self.root)
        self.assertEqual(stored["refresh_token"], session["refresh_token"])

    def test_pkce_is_s256(self) -> None:
        verifier = "a" * 43
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        self.assertEqual(pkce_challenge(verifier), expected)

    def test_discovered_token_endpoint_gets_an_exact_transport_allowlist(self) -> None:
        transport = skolemelding_auth._default_transport(
            allowed_idporten_urls=(OIDC_TOKEN_ENDPOINT,)
        )
        self.assertEqual(transport._validate_url(OIDC_TOKEN_ENDPOINT), OIDC_TOKEN_ENDPOINT)
        with self.assertRaises(ApiTransportError):
            transport._validate_url("https://login.idporten.no/not-the-token-endpoint")


if __name__ == "__main__":
    unittest.main()
