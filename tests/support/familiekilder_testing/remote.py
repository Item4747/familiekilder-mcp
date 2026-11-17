"""Shared helpers for HTTP mode: a real server in a thread and an OAuth client.

The client is the MCP SDK's own `OAuthClientProvider`, as an assistant would
use it: it registers itself, opens the approval page, and the "browser" here
types the owner code. All values are made up.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
import httpx2
import uvicorn
from mcp import ClientSession
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from familiekilder.hosting import hash_owner_code, new_owner_code

OWNER_CODE = new_owner_code()
OWNER_CODE_HASH = hash_owner_code(OWNER_CODE)
INSTALLATION_ID = "oppdiktet-installasjon-0123456789abcdef"
REDIRECT_URI = "http://localhost:3030/callback"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def host_environment(port: int) -> dict[str, str]:
    return {
        "FAMILIEKILDER_PUBLIC_URL": f"http://127.0.0.1:{port}",
        "FAMILIEKILDER_OWNER_CODE_HASH": OWNER_CODE_HASH,
        "FAMILIEKILDER_INSTALLATION_ID": INSTALLATION_ID,
    }


@contextlib.contextmanager
def running(app, port: int) -> Iterator[str]:
    """Serve ``app`` on 127.0.0.1:port until the block ends; yields the base URL."""

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("Test server did not start")
        time.sleep(0.02)
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class MemoryStorage(TokenStorage):
    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client = client_info


class Browser:
    """Follows the authorize redirect and submits the approval form."""

    def __init__(self, owner_code: str) -> None:
        self.owner_code = owner_code
        self.callback: str | None = None
        self.pages: list[str] = []

    async def redirect(self, authorization_url: str) -> None:
        async with httpx2.AsyncClient(follow_redirects=False, timeout=30) as client:
            response = await client.get(authorization_url)
            assert response.status_code == 302, response.text
            page_url = urljoin(authorization_url, response.headers["location"])
            page = await client.get(page_url)
            self.pages.append(page.text)
            request_id = parse_qs(urlsplit(page_url).query)["foresporsel"][0]
            answer = await client.post(
                urljoin(page_url, "/godkjenn"),
                data={"foresporsel": request_id, "eierkode": self.owner_code, "valg": "godkjenn"},
            )
            assert answer.status_code == 303, answer.text
            self.callback = answer.headers["location"]

    async def callback_result(self) -> AuthorizationCodeResult:
        query = parse_qs(urlsplit(self.callback or "").query)
        return AuthorizationCodeResult(
            code=query["code"][0], state=query.get("state", [None])[0], iss=query.get("iss", [None])[0]
        )


def oauth_provider(base: str, storage: MemoryStorage, owner_code: str = OWNER_CODE) -> OAuthClientProvider:
    browser = Browser(owner_code)
    return OAuthClientProvider(
        server_url=base + "/mcp",
        client_metadata=OAuthClientMetadata(
            client_name="Oppdiktet assistent",
            redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="familiekilder offline_access",
        ),
        storage=storage,
        redirect_handler=browser.redirect,
        callback_handler=browser.callback_result,
    )


@contextlib.asynccontextmanager
async def session(base: str, storage: MemoryStorage | None = None) -> AsyncIterator[ClientSession]:
    storage = storage or MemoryStorage()
    auth = oauth_provider(base, storage)
    async with httpx2.AsyncClient(auth=auth, timeout=30) as client:
        async with streamable_http_client(base + "/mcp", http_client=client) as (read, write):
            async with ClientSession(read, write) as connected:
                await connected.initialize()
                yield connected


def register(base: str, redirect_uri: str = REDIRECT_URI, **extra) -> dict:
    response = httpx.post(
        base + "/register",
        json={
            "client_name": "Oppdiktet klient",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            **extra,
        },
    )
    return response.json() | {"status": response.status_code}
