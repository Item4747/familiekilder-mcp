"""Flyt login with invented municipalities, accounts and tokens; no network."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from familiekilder import flyt_auth
from familiekilder.api_transport import ApiHTTPError
from familiekilder.core import FamiliekilderError
from familiekilder.flyt_auth import (
    CLIENT_ID,
    ISSUER,
    MUNICIPALITIES_ENDPOINT,
    REDIRECT_URI,
    SCOPES,
    SESSION_ENDPOINT,
    TOKEN_ENDPOINT,
    FlytAuthError,
    FlytReauthenticationRequired,
    auth_status,
    begin_login,
    configure_municipality,
    list_municipalities,
    local_session_for_use,
    login_with_callback,
    logout,
    parse_municipalities,
    record_consent,
    refresh_session,
    select_municipality,
)
from familiekilder.private_state import atomic_write_private_json, read_private_json

MUNICIPALITIES = [
    {
        "customerName": "fjordvik",
        "displayName": "Fjordvik kommune",
        "organizationNumber": "912345678",
        "vismaConnectClientContext": "Vismaconnect998877665",
    },
    {"customerName": "bergli", "displayName": "Bergli kommune", "organizationNumber": "923456789"},
    {
        "customerName": "nord-bergli",
        "displayName": "Nord-Bergli kommune",
        "organizationNumber": "934567890",
    },
    {"displayName": "Mangler alt"},
]


def jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.signature"


class DeterministicRandom:
    def __init__(self) -> None:
        self.counter = 0

    def __call__(self, size: int) -> bytes:
        self.counter += 1
        return bytes([self.counter]) * size


class FakeAuthTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.token_reply: object = {
            "access_token": jwt({"sub": "konto-1", "exp": 4_000_000_000}),
            "refresh_token": "refresh-1",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "profile email openid nnin offline_access",
        }
        # Like Visma Connect, a code exchange adds an ID token with the
        # authorize request's nonce, unless a test supplies its own reply.
        self.id_claims: dict | None = {"sub": "konto-1", "iss": ISSUER, "aud": CLIENT_ID}
        self.nonce: str | None = None
        # Shape of a live reply: per-system status only, no session ID.
        self.session_reply: object = {"school": 3, "kindergarten": 1}
        self.failure: Exception | None = None

    def get_json(self, url, **kwargs):
        self.calls.append(("GET", url, {}, kwargs))
        if url != MUNICIPALITIES_ENDPOINT:
            raise AssertionError("unexpected GET")
        return MUNICIPALITIES

    def post_form(self, url, fields, **kwargs):
        self.calls.append(("FORM", url, dict(fields), kwargs))
        if url != TOKEN_ENDPOINT:
            raise AssertionError("unexpected form endpoint")
        if self.failure is not None:
            raise self.failure
        reply = self.token_reply
        if (
            fields.get("grant_type") == "authorization_code"
            and self.id_claims is not None
            and isinstance(reply, dict)
            and "id_token" not in reply
        ):
            reply = {**reply, "id_token": jwt({**self.id_claims, "nonce": self.nonce})}
        return reply

    def post_json(self, url, payload, **kwargs):
        self.calls.append(("JSON", url, dict(payload), kwargs))
        if url != SESSION_ENDPOINT:
            raise AssertionError("unexpected JSON endpoint")
        return self.session_reply


class FlytAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.transport = FakeAuthTransport()

    def configure(self, query: str = "912345678") -> None:
        configure_municipality(self.root, query, transport=self.transport)

    def login(self) -> dict:
        self.configure()
        record_consent(self.root, at=1_800_000_000)
        url, transaction = begin_login(self.root, now=100, random_bytes=DeterministicRandom())
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        state = params["state"][0]
        self.transport.nonce = params["nonce"][0]
        callback = f"{REDIRECT_URI}?code=kode-1&state={state}&session_state=x&scope=openid"
        return login_with_callback(
            self.root,
            transaction,
            callback,
            transport=self.transport,
            now=101,
            wall_time=1_800_000_000,
        )

    def test_municipality_selection_by_number_name_and_ambiguity(self) -> None:
        municipalities = parse_municipalities(MUNICIPALITIES)
        self.assertEqual(len(municipalities), 3)
        self.assertEqual(select_municipality(municipalities, "923456789").customer_name, "bergli")
        self.assertEqual(select_municipality(municipalities, "bergli").customer_name, "bergli")
        self.assertEqual(
            select_municipality(municipalities, "Fjordvik kommune").customer_name, "fjordvik"
        )
        with self.assertRaisesRegex(FlytAuthError, "Flere kommuner passer"):
            select_municipality(municipalities, "berg")
        with self.assertRaisesRegex(FlytAuthError, "Fant ingen kommune"):
            select_municipality(municipalities, "Ukjentby")

    def test_public_list_and_stored_client_context(self) -> None:
        listed = list_municipalities(transport=self.transport)
        self.assertEqual(
            [row["customer_name"] for row in listed], ["bergli", "fjordvik", "nord-bergli"]
        )
        self.configure("Fjordvik")
        stored = read_private_json(flyt_auth.client_config_path(self.root))
        # The list's context is used verbatim, including its casing.
        self.assertEqual(stored["client_context"], "Vismaconnect998877665")
        self.assertEqual(stored["customer_name"], "fjordvik")
        bergli = select_municipality(parse_municipalities(MUNICIPALITIES), "bergli")
        self.assertEqual(bergli.client_context, "VismaConnect923456789")

    def test_authorize_url_has_the_app_parameters_and_pkce(self) -> None:
        self.configure()
        record_consent(self.root, at=1_800_000_000)
        url, transaction = begin_login(self.root, now=100, random_bytes=DeterministicRandom())
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(
            (parsed.scheme, parsed.netloc, parsed.path),
            ("https", "connect.visma.com", "/connect/authorize"),
        )
        params = dict(urllib.parse.parse_qsl(parsed.query))
        verifier_digest = hashlib.sha256(transaction.code_verifier.encode()).digest()
        self.assertEqual(
            params,
            {
                "client_id": "min-skole-guardian-mobile-app",
                "redirect_uri": "com.visma.vfsparentapp://redirect",
                "response_mode": "query",
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "client_context": "Vismaconnect998877665",
                "acr_values": "urn:idp:id-porten",
                "onbehalfof": "vismaflytskole-912345678",
                "nonce": transaction.nonce,
                "state": transaction.state,
                "code_challenge_method": "S256",
                "code_challenge": base64.urlsafe_b64encode(verifier_digest).rstrip(b"=").decode(),
                "prompt": "login",
                "max_age": "0",
            },
        )
        self.assertNotIn(transaction.code_verifier, url)
        self.assertNotIn("code_verifier", repr(transaction))

    def test_login_requires_consent(self) -> None:
        self.configure()
        with self.assertRaisesRegex(FlytAuthError, "samtykke"):
            begin_login(self.root)

    def test_login_exchanges_code_and_opens_the_guardian_session(self) -> None:
        result = self.login()
        self.assertTrue(result["authenticated"])
        self.assertNotIn("refresh", json.dumps(result))
        token_call, session_call = [call for call in self.transport.calls if call[0] != "GET"]
        self.assertEqual(token_call[2]["grant_type"], "authorization_code")
        self.assertEqual(token_call[2]["code"], "kode-1")
        self.assertEqual(token_call[2]["redirect_uri"], REDIRECT_URI)
        self.assertEqual(len(token_call[2]["code_verifier"]), 43)
        device = read_private_json(flyt_auth.device_path(self.root))["device_unique_id"]
        self.assertEqual(session_call[2], {"deviceId": device, "osType": 1})
        self.assertEqual(
            session_call[3]["headers"], {"Customer-Key": "fjordvik", "Accept-Language": "nb-NO"}
        )
        self.assertTrue(session_call[3]["bearer_token"].startswith("ey"))
        stored = read_private_json(flyt_auth.session_path(self.root))
        self.assertEqual(stored["subject"], hashlib.sha256(b"flyt:konto-1").hexdigest())
        self.assertEqual((stored["kindergarten_status"], stored["school_status"]), (1, 3))
        self.assertNotIn("session_id", stored)
        self.assertNotIn("id_token", stored)
        self.assertNotIn("konto-1", json.dumps(stored["subject"]))
        self.assertTrue(auth_status(self.root)["authenticated"])

    def test_callback_with_wrong_state_or_reuse_is_refused(self) -> None:
        self.configure()
        record_consent(self.root, at=1_800_000_000)
        _url, transaction = begin_login(self.root, now=100, random_bytes=DeterministicRandom())
        self.transport.nonce = transaction.nonce
        with self.assertRaisesRegex(FlytAuthError, "state"):
            login_with_callback(
                self.root,
                transaction,
                f"{REDIRECT_URI}?code=k&state=feil",
                transport=self.transport,
                now=101,
            )
        callback = f"{REDIRECT_URI}?code=k&state={transaction.state}"
        login_with_callback(
            self.root,
            transaction,
            callback,
            transport=self.transport,
            now=101,
            wall_time=1_800_000_000,
        )
        with self.assertRaisesRegex(FlytAuthError, "allerede brukt"):
            login_with_callback(
                self.root,
                transaction,
                callback,
                transport=self.transport,
                now=101,
                replace_existing=True,
            )

    def test_failed_guardian_session_stores_nothing(self) -> None:
        self.transport.session_reply = ["ikke", "et", "objekt"]
        with self.assertRaisesRegex(FlytAuthError, "ugyldig økt"):
            self.login()
        self.assertFalse(flyt_auth.session_path(self.root).exists())

    def test_login_without_account_identity_is_refused(self) -> None:
        self.transport.token_reply = {"access_token": "ugjennomsiktig", "refresh_token": "r"}
        self.transport.id_claims = None
        with self.assertRaisesRegex(FlytAuthError, "kontoidentitet"):
            self.login()

    def test_login_id_token_must_match_issuer_app_and_this_login(self) -> None:
        good = {"sub": "konto-1", "iss": ISSUER, "aud": CLIENT_ID}
        cases = [
            ("uten ID-token", None, "gyldig ID-token"),
            ("annen utsteder", {**good, "iss": "https://connect.example.test"}, "Visma Connect"),
            ("uten utsteder", {k: v for k, v in good.items() if k != "iss"}, "Visma Connect"),
            ("annen app", {**good, "aud": "en-annen-app"}, "Flyt-appen"),
            ("uten mottaker", {k: v for k, v in good.items() if k != "aud"}, "Flyt-appen"),
            ("feil nonce", {**good, "nonce": "en-annen-innlogging"}, "annen innlogging"),
            ("uten nonce", {**good, "nonce": None}, "annen innlogging"),
        ]
        for name, claims, message in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as root:
                self.root = Path(root)
                self.transport = FakeAuthTransport()
                if claims is None:
                    # The access token still names the account.
                    self.transport.id_claims = None
                elif "nonce" in claims:
                    self.transport.token_reply["id_token"] = jwt(
                        {k: v for k, v in claims.items() if v is not None}
                    )
                else:
                    self.transport.id_claims = claims
                with self.assertRaisesRegex(FlytReauthenticationRequired, message):
                    self.login()
                self.assertFalse(flyt_auth.session_path(self.root).exists())
                # No guardian session is opened with a refused login.
                self.assertFalse(any(call[1] == SESSION_ENDPOINT for call in self.transport.calls))
        # A list audience that names the app is accepted.
        with tempfile.TemporaryDirectory() as root:
            self.root = Path(root)
            self.transport = FakeAuthTransport()
            self.transport.id_claims = {**good, "aud": [CLIENT_ID, "en-annen-api"]}
            self.assertTrue(self.login()["authenticated"])

    def test_refresh_rotates_and_keeps_an_unrotated_grant(self) -> None:
        self.login()
        self.transport.token_reply = {
            "access_token": jwt({"sub": "konto-1", "exp": 4_000_000_100}),
            "refresh_token": "refresh-2",
            "expires_in": 3600,
        }
        result = refresh_session(self.root, transport=self.transport, at=1_800_000_100)
        self.assertTrue(result["refresh_token_rotated"])
        form = self.transport.calls[-1][2]
        self.assertEqual(
            form,
            {
                "client_id": "min-skole-guardian-mobile-app",
                "grant_type": "refresh_token",
                "refresh_token": "refresh-1",
            },
        )
        self.assertEqual(local_session_for_use(self.root)["refresh_token"], "refresh-2")
        self.transport.token_reply = {"access_token": jwt({"sub": "konto-1"}), "expires_in": 60}
        result = refresh_session(self.root, transport=self.transport, at=1_800_000_200)
        self.assertFalse(result["refresh_token_rotated"])
        self.assertEqual(local_session_for_use(self.root)["refresh_token"], "refresh-2")
        self.assertFalse(flyt_auth.refresh_block_path(self.root).exists())

    def test_refresh_to_another_account_needs_new_login(self) -> None:
        self.login()
        self.transport.token_reply = {
            "access_token": "a",
            "refresh_token": "b",
            "id_token": jwt({"sub": "konto-2"}),
        }
        with self.assertRaises(FlytReauthenticationRequired):
            refresh_session(self.root, transport=self.transport, at=1_800_000_100)

    def test_rejected_refresh_blocks_the_grant(self) -> None:
        self.login()
        self.transport.failure = ApiHTTPError(400, "invalid_grant")
        with self.assertRaises(FlytReauthenticationRequired):
            refresh_session(self.root, transport=self.transport, at=1_800_000_100)
        with self.assertRaises(FlytReauthenticationRequired):
            local_session_for_use(self.root)
        status = auth_status(self.root)
        self.assertEqual(
            (status["authenticated"], status["refresh_block_reason"]), (False, "invalid-grant")
        )

    def test_unclear_refresh_pauses_and_a_saved_reply_is_recovered(self) -> None:
        self.login()
        self.transport.failure = ApiHTTPError(502)
        with self.assertRaises(flyt_auth.FlytRefreshUncertain):
            refresh_session(self.root, transport=self.transport, at=1_800_000_100)
        with self.assertRaises(flyt_auth.FlytRefreshUncertain):
            local_session_for_use(self.root)
        session = flyt_auth._read_session(self.root)
        atomic_write_private_json(
            flyt_auth.refresh_block_path(self.root),
            {
                "reason": "refresh-response-received",
                "transaction_id": "transaksjon-1",
                "received_at": 1_800_000_200,
                "session_binding": flyt_auth._session_binding(session),
                "response": {"access_token": jwt({"sub": "konto-1"}), "refresh_token": "refresh-3"},
            },
        )
        self.assertEqual(local_session_for_use(self.root)["refresh_token"], "refresh-3")
        self.assertFalse(flyt_auth.refresh_block_path(self.root).exists())

    def test_status_reads_no_network_and_creates_no_files(self) -> None:
        with patch.object(flyt_auth, "default_transport", side_effect=AssertionError("network")):
            status = auth_status(self.root)
        self.assertEqual(
            {
                key: status[key]
                for key in (
                    "configured",
                    "authenticated",
                    "session_present",
                    "access_expired",
                    "refresh_blocked",
                )
            },
            {
                "configured": False,
                "authenticated": False,
                "session_present": False,
                "access_expired": False,
                "refresh_blocked": False,
            },
        )
        self.assertEqual([path for path in self.root.rglob("*") if path.is_file()], [])

    def test_changing_municipality_requires_logout(self) -> None:
        self.login()
        with self.assertRaisesRegex(FlytAuthError, "Logg ut"):
            self.configure("bergli")
        result = logout(self.root)
        self.assertTrue(result["local_session_removed"])
        self.assertTrue(result["device_id_retained"])
        self.configure("bergli")
        self.assertFalse(auth_status(self.root)["authenticated"])

    def test_cli_configure_takes_a_municipality_name_in_the_client_file(self) -> None:
        import contextlib
        import io

        from familiekilder.auth_cli import _run_generic_auth
        from familiekilder.source_cli import build_parser

        client_file = self.root / ".private" / "flyt-kommune.json"
        atomic_write_private_json(client_file, {"municipality": "Fjordvik"})
        args = build_parser().parse_args(
            ["auth", "flyt", "configure", "--client-file", str(client_file)]
        )
        with (
            patch.object(flyt_auth, "default_transport", return_value=self.transport),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(_run_generic_auth(args, self.root), 0)
        self.assertIn("Fjordvik kommune", output.getvalue())
        stored = read_private_json(flyt_auth.client_config_path(self.root))
        self.assertEqual(stored["organization_number"], "912345678")
        atomic_write_private_json(client_file, {"municipality": "berg"})
        with self.assertRaisesRegex(FamiliekilderError, "Flere kommuner passer"):
            flyt_auth.configure_client(self.root, client_file, transport=self.transport)


if __name__ == "__main__":
    unittest.main()
