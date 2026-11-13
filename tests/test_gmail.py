import base64
import unittest
from unittest.mock import patch

import httpx

from familiekilder import gmail, gmail_auth
from familiekilder.core import FamiliekilderError
from familiekilder.live_config import Config
from familiekilder.private_state import atomic_write_private_json, read_private_json
from familiekilder.source_adapter import SourceLoginRequired, SourceUnavailable
from familiekilder_testing.gmail import (
    GmailClientFixture,
    mail,
)


class GmailTests(GmailClientFixture, unittest.TestCase):
    def test_mime_alternatives_headers_charset_and_attachment_identity(self):
        raw = mail(attachments=True)
        raw["payload"]["headers"][0]["value"] = "=?UTF-8?B?UGFya291cg==?="
        text = raw["payload"]["parts"].pop(0)
        raw["payload"]["parts"].insert(0, {"mimeType": "multipart/alternative", "parts": [text,
            {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>duplicate</p>").decode()}}]})
        item = gmail.normalize(raw)
        self.assertEqual(item["title"], "Parkour")
        self.assertNotIn("duplicate", item["body"])
        self.assertTrue(item["content_complete"])
        self.assertEqual(item["attachments"][0]["filename"], "ukeplan.pdf")
        self.assertEqual(item["attachments"][0]["attachment_id"], "part:1")
        raw = mail()
        raw["payload"]["parts"][0] = {"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}],
            "body": {"data": base64.urlsafe_b64encode("Øving".encode("latin1")).decode()}}
        self.assertEqual(gmail.normalize(raw)["body"], "Øving")

    def test_cp1252_undefined_bytes_are_preserved_without_replacement_or_lost_body(self):
        raw = mail()
        part = raw['payload']['parts'][0]
        part['headers'] = [{'name': 'Content-Type', 'value': 'text/plain; charset="Windows-1252"'}]
        part['body']['data'] = base64.urlsafe_b64encode(b'Ol\xe9 \x81 tekst').decode()
        item = gmail.normalize(raw)
        self.assertTrue(item['content_complete'])
        self.assertIn('Olé \\x81 tekst', item['body'])
        self.assertNotIn('\ufffd', item['body'])

    def test_missing_message_has_a_distinct_error_without_retry(self):
        calls = []
        def handler(req):
            calls.append(req.url.path)
            return httpx.Response(404, text='private response')
        client = self.client(handler)
        with self.assertRaises(gmail.GmailNotFound) as error:
            client.item({'kind': 'message', 'id': 'gone'})
        self.assertNotIn('private response', str(error.exception))
        self.assertEqual(calls, ['/gmail/v1/users/me/messages/gone'])

    def test_html_snippet_truncation_and_invalid_mime(self):
        raw = mail()
        raw["payload"]["parts"][0]["mimeType"] = "text/html"
        raw["payload"]["parts"][0]["body"]["data"] = base64.urlsafe_b64encode(b"<p>Training &amp; equipment</p>").decode()
        self.assertEqual(gmail.normalize(raw)["body"], "Training & equipment")
        raw["payload"]["parts"] = []
        raw["snippet"] = "Partial preview"
        self.assertFalse(gmail.normalize(raw)["content_complete"])
        self.assertFalse(gmail.normalize(mail(text="x"*(gmail.MAX_TEXT+1)))["content_complete"])
        with self.assertRaises(SourceUnavailable):
            gmail.decode("not valid base64!")

    def test_refresh_rotation_401_once_and_sanitized_provider_errors(self):
        calls = []
        def handler(req):
            calls.append((req.method, str(req.url)))
            if req.method == "POST":
                return httpx.Response(200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600})
            return httpx.Response(401 if len(calls) == 1 else 200, json={"emailAddress": "fake@example.invalid"})
        client = self.client(handler)
        self.assertTrue(client.get("/profile"))
        self.assertEqual(read_private_json(client.path)["refresh_token"], "rotated")
        self.assertEqual([m for m, _ in calls], ["GET", "POST", "GET"])
        client.http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(429, text="secret mail")))
        self.addCleanup(client.http.close)
        with self.assertRaisesRegex(SourceUnavailable, "Gmail-hastighetsgrense") as error:
            client.get("/profile")
        self.assertNotIn("secret", str(error.exception))


    def test_read_only_grant_rejects_broad_scope_and_preserves_old_session(self):
        client = self.client(lambda req: httpx.Response(403))
        before = client.path.read_bytes()
        tokens = {"scope": gmail.SCOPE + " https://www.googleapis.com/auth/gmail.send", "refresh_token": "r"}
        with self.assertRaises(FamiliekilderError):
            client.grant(tokens)
        tokens |= {"scope": gmail.SCOPE, "access_token": "new", "_client_id": "c", "_client_secret": "s"}
        with self.assertRaises(SourceLoginRequired):
            client.grant(tokens)
        self.assertEqual(client.path.read_bytes(), before)
        with self.assertRaises(FamiliekilderError):
            Config(self.root, {"gmail": frozenset({"messages"})}).save()

    def test_rejected_refresh_persists_reconnect_state_until_verified_new_grant(self):
        from familiekilder.connection_status import connection_state
        for response in (httpx.Response(400, json={"error": "invalid_grant"}),
                         httpx.Response(401),
                         httpx.Response(200, json={"scope": "openid"})):
            with self.subTest(status=response.status_code):
                calls = []
                def handler(request):
                    calls.append(request)
                    return response
                client = self.client(handler, expired=True)
                with self.assertRaises(SourceLoginRequired):
                    client.get("/profile")
                status = gmail_auth.auth_status(self.root)
                self.assertFalse(status["authenticated"])
                self.assertTrue(status["refresh_blocked"])
                self.assertTrue(status["session_present"])
                self.assertEqual(connection_state(status), "attention_required")
                # The block survives a new client and even an explicit refresh.
                with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                    reopened = gmail.Client(self.root, client=http)
                    with self.assertRaises(SourceLoginRequired):
                        reopened.token(force=True)
                self.assertEqual(len(calls), 1)
                tokens = {"scope": gmail.SCOPE, "access_token": "new", "refresh_token": "new-r",
                          "_client_id": "c", "_client_secret": "s"}
                with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(403))) as http:
                    with self.assertRaises(SourceLoginRequired):
                        gmail.Client(self.root, client=http).grant(tokens)
                self.assertTrue(gmail_auth.auth_status(self.root)["refresh_blocked"])
                with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
                        200, json={"emailAddress": "fake@example.invalid"}))) as http:
                    gmail.Client(self.root, client=http).grant(tokens)
                status = gmail_auth.auth_status(self.root)
                self.assertTrue(status["authenticated"])
                self.assertFalse(status["refresh_blocked"])
                self.assertEqual(connection_state(status), "connected")

    def test_transient_refresh_failure_preserves_retryable_session(self):
        client = self.client(lambda req: httpx.Response(503), expired=True)
        before = client.path.read_bytes()
        with self.assertRaises(SourceUnavailable):
            client.token()
        self.assertEqual(client.path.read_bytes(), before)
        self.assertFalse(gmail_auth.auth_status(self.root)["refresh_blocked"])



    def test_attachments_resolved_fresh_and_downloaded_without_other_hosts(self):
        calls = []
        def handler(req):
            calls.append(req)
            return httpx.Response(200, json={"data": base64.urlsafe_b64encode(b"%PDF-fake").decode()} if "/attachments/" in req.url.path else mail(attachments=True))
        client = self.client(handler)
        item = client.item({"kind": "message", "id": "a1"})
        path, mime = client.download(item, "part:1", self.root)
        self.assertEqual(path.name, "ukeplan.pdf")
        self.assertEqual(mime, "application/pdf")
        self.assertEqual(path.read_bytes(), b"%PDF-fake")
        self.assertTrue(all(req.method == "GET" and req.url.host == "gmail.googleapis.com" for req in calls))
        for value in ("../me", "https://evil.invalid", "", "a/b"):
            with self.assertRaises(FamiliekilderError):
                client.item({"kind": "message", "id": value})

class GmailAuthTests(unittest.TestCase):
    setUp = GmailTests.setUp

    def test_oauth_state_and_redirect_must_match_before_any_request(self):
        with httpx.Client(transport=httpx.MockTransport(lambda req: self.fail("Unexpected traffic"))) as client:
            for callback in ("http://evil.invalid/oauth/callback?state=good&code=x",
                             "http://127.0.0.1:1234/oauth/callback?state=bad&code=x",
                             "http://127.0.0.1:1234/oauth/callback?state=good&code=x&code=y"):
                with self.assertRaises(FamiliekilderError):
                    gmail_auth.complete_login(self.root, {"client_id": "c", "client_secret": "s"},
                        "http://127.0.0.1:1234/oauth/callback", "verifier", "good", callback, client=client)

    def test_authorization_code_and_pkce_exchange_then_verify_profile_and_private_grant(self):
        calls = []
        def handler(req):
            calls.append(req)
            return httpx.Response(200, json={"scope": gmail.SCOPE, "access_token": "a", "refresh_token": "r", "expires_in": 3600} if req.method == "POST" else {"emailAddress": "fake@example.invalid"})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = gmail_auth.complete_login(self.root, {"client_id": "c", "client_secret": "s"},
                "http://127.0.0.1:1234/oauth/callback", "verifier", "good",
                "http://127.0.0.1:1234/oauth/callback?state=good&code=x", client=client)
        self.assertTrue(result["authenticated"])
        self.assertIn(b"code_verifier=verifier", calls[0].content)
        path = self.root/".private/credentials/gmail.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(gmail_auth.auth_status(self.root)["authenticated"])

    def test_desktop_client_required_and_offline_status_does_not_create_state(self):
        with patch("familiekilder.gmail_auth.httpx.Client") as network:
            self.assertFalse(gmail_auth.auth_status(self.root)["configured"])
            network.assert_not_called()
        self.assertFalse((self.root/".private").exists())
        path = self.root/".private/download.json"
        atomic_write_private_json(path, {"web": {"client_id": "fake.apps.googleusercontent.com", "client_secret": "s"}})
        with self.assertRaises(FamiliekilderError):
            gmail_auth.configure_client(self.root, path)
        atomic_write_private_json(path, {"installed": {"client_id": "fake.apps.googleusercontent.com", "client_secret": "s"}})
        self.assertTrue(gmail_auth.configure_client(self.root, path)["configured"])
