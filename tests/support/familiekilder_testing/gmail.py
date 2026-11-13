"""A fake Gmail message, and a fixture with a Gmail client on a mock transport."""

import base64
import tempfile
import time
from pathlib import Path

import httpx

from familiekilder import gmail
from familiekilder.private_state import atomic_write_private_json


def mail(ident="a1", text="Trening torsdag. Ta med hjelm.", *, attachments=False):
    parts = [
        {
            "partId": "0",
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
        }
    ]
    if attachments:
        parts.append(
            {
                "partId": "1",
                "mimeType": "application/pdf",
                "filename": "../../ukeplan.pdf",
                "body": {"attachmentId": "attach", "size": 12},
            }
        )
    return {
        "id": ident,
        "threadId": "t1",
        "internalDate": "1790000000000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "Allidrett"},
                {"name": "From", "value": "Klubben <test@example.invalid>"},
            ],
            "parts": parts,
        },
    }


class GmailClientFixture:
    """A temporary folder, a fake clock and a Gmail client on a mock HTTP transport."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = time.time()

    def sleep(self, seconds):
        self.now += seconds

    def client(self, handler, *, expired=False):
        client = gmail.Client(
            self.root,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            clock=lambda: self.now,
            sleep=self.sleep,
            jitter=lambda: 0,
        )
        self.addCleanup(client.close)
        atomic_write_private_json(
            client.path,
            {
                "access_token": "fake",
                "refresh_token": "fake-r",
                "scope": gmail.SCOPE,
                "client_id": "fake-client",
                "client_secret": "fake-secret",
                "expires_at": 0 if expired else time.time() + 3600,
            },
        )
        return client
