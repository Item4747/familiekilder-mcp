"""MCP over streamable HTTP, with the built-in OAuth server in front.

Used by ``familiekilder-mcp mcp serve --http``. The tool catalog and the
read-only runtime are the same as over stdio; only the transport and the
login in front differ. TLS ends in front of this process (Fly.io, a tunnel or
a reverse proxy); the process itself listens on plain HTTP.

Routes:

- ``/mcp``: the MCP endpoint; requires a bearer token from this server.
- ``/.well-known/oauth-protected-resource[/mcp]`` and
  ``/.well-known/oauth-authorization-server``: OAuth metadata.
- ``/register``, ``/authorize``, ``/token``, ``/revoke``: OAuth endpoints from
  the MCP SDK.
- ``/godkjenn``: the approval page that asks for the owner code.
- ``/flytt/nokkel`` and ``/flytt/importer``: receive moved logins (owner code
  required; see ``session_move``).
- ``/healthz``: liveness for the host, without data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.handlers.metadata import MetadataHandler, ProtectedResourceMetadataHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import RequestBodyLimitMiddleware, TransportSecuritySettings
from mcp.shared.auth import ProtectedResourceMetadata
from starlette.applications import Starlette
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route, request_response

from . import __version__
from .hosting import HEALTH_PATH, MCP_PATH, HostSettings
from .live_config import Config
from .remote_auth import APPROVAL_PATH, SCOPE, VALID_SCOPES, OwnerAuthorizationProvider

FORM_BODY_LIMIT = 16 * 1024
AUTHORIZATION_METADATA_PATH = "/.well-known/oauth-authorization-server"
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"

logger = logging.getLogger(__name__)


@dataclass
class HttpApp:
    app: Starlette
    server: Any
    runtime: Any
    provider: OwnerAuthorizationProvider


class LiveCatalogServer(MCPServer):
    """Refresh read tools from persisted configuration before each MCP request."""

    def __init__(self, *args, runtime, **kwargs):
        super().__init__(*args, **kwargs)
        self.runtime = runtime
        self._source_tools: dict[str, set[str]] = {}

    def _sync_catalog(self):
        from .live_actions import register_source_context_reads
        from .source_server import register_gmail_search, register_source_reads

        enabled = self.runtime.config.services
        for source in list(self._source_tools):
            if source not in enabled:
                for name in self._source_tools.pop(source):
                    self.remove_tool(name)
        for source in enabled:
            if source in self._source_tools:
                continue
            before = {tool.name for tool in self._tool_manager.list_tools()}
            register_source_reads(self, self.runtime, source)
            register_source_context_reads(self, self.runtime, source)
            if source == 'gmail':
                register_gmail_search(self, self.runtime)
            self._source_tools[source] = {
                tool.name for tool in self._tool_manager.list_tools()
            } - before

    async def list_tools(self):
        self._sync_catalog()
        return await super().list_tools()

    async def call_tool(self, name, arguments, context=None):
        self._sync_catalog()
        return await super().call_tool(name, arguments, context)


def auth_settings(settings: HostSettings) -> AuthSettings:
    return AuthSettings(
        issuer_url=settings.issuer_url,
        resource_server_url=settings.resource_url,
        validate_token_resource=True,
        required_scopes=[SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=list(VALID_SCOPES), default_scopes=list(VALID_SCOPES)
        ),
        revocation_options=RevocationOptions(enabled=True),
    )


def remote_server_factory(settings: HostSettings, provider: OwnerAuthorizationProvider):
    """A `build_server` factory: the same server as stdio, plus OAuth."""

    from .source_server import SOURCE_INSTRUCTIONS

    def factory(runtime: Any) -> MCPServer:
        return LiveCatalogServer(
            "familiekilder-mcp",
            runtime=runtime,
            version=__version__,
            instructions=SOURCE_INSTRUCTIONS,
            log_level="WARNING",
            auth_server_provider=provider,
            auth=auth_settings(settings),
        )

    return factory


def _limited(handler, limit: int = FORM_BODY_LIMIT):
    return RequestBodyLimitMiddleware(request_response(handler), limit)


class PublicClientRevocationHandler(RevocationHandler):
    """Work around MCP 2.2.0's required nullable client_secret form field.

    RFC 7009 public clients have no secret. Keep the SDK's authentication,
    validation and revocation, supplying only the omitted nullable field.
    """

    async def handle(self, request: Request) -> Response:
        form = await request.form()
        if "client_secret" not in form:
            request._form = FormData([*form.multi_items(), ("client_secret", None)])
        return await super().handle(request)


async def _health(request: Request) -> Response:
    return PlainTextResponse("ok", headers={"Cache-Control": "no-store"})


def _authorization_metadata(settings: HostSettings, options: AuthSettings):
    metadata = build_metadata(
        options.issuer_url,
        None,
        options.client_registration_options,
        options.revocation_options,
    )
    # Public clients (no secret) are allowed, and the approval redirect carries
    # `iss` (RFC 9207), which ChatGPT uses to pick its stable callback address.
    methods = ["client_secret_post", "client_secret_basic", "none"]
    return metadata.model_copy(
        update={
            "token_endpoint_auth_methods_supported": methods,
            "revocation_endpoint_auth_methods_supported": methods,
            "authorization_response_iss_parameter_supported": True,
        }
    )


def build_http_app(
    config: Config,
    settings: HostSettings,
    *,
    runtime: Any = None,
    extra_redirect_hosts: str | None = None,
) -> HttpApp:
    """Build the ASGI app. The caller closes ``runtime`` when done."""

    from .session_move import MoveReceiver
    from .source_server import ReadOnlyRuntime, build_server

    provider = OwnerAuthorizationProvider(
        config.root, settings, extra_redirect_hosts=extra_redirect_hosts
    )
    runtime = runtime or ReadOnlyRuntime(config)
    server, runtime = build_server(
        Config(config.root, {}),
        runtime=runtime,
        server_factory=remote_server_factory(settings, provider),
        read_only=True,
    )
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[settings.public_host],
        allowed_origins=[settings.public_url],
    )
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        transport_security=security,
        host="0.0.0.0",
    )
    options = auth_settings(settings)
    metadata = _authorization_metadata(settings, options)
    resource_metadata = ProtectedResourceMetadata(
        resource=settings.resource_url,
        authorization_servers=[settings.issuer_url],
        scopes_supported=[SCOPE],
        resource_name="familiekilder-mcp",
    )
    routes = [
        route
        for route in app.router.routes
        if getattr(route, "path", None) not in {
            AUTHORIZATION_METADATA_PATH, RESOURCE_METADATA_PATH + MCP_PATH, "/revoke"
        }
    ]
    for route in routes:
        if getattr(route, "path", None) == "/register":
            route.app = RequestBodyLimitMiddleware(route.app, FORM_BODY_LIMIT)
    receiver = MoveReceiver(config.root, settings, provider.gate)
    routes += [
        Route(
            AUTHORIZATION_METADATA_PATH,
            endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        ),
        # Also at the root path, for clients that look there first.
        Route(
            RESOURCE_METADATA_PATH,
            endpoint=cors_middleware(
                ProtectedResourceMetadataHandler(resource_metadata).handle, ["GET", "OPTIONS"]
            ),
            methods=["GET", "OPTIONS"],
        ),
        Route(
            RESOURCE_METADATA_PATH + MCP_PATH,
            endpoint=cors_middleware(
                ProtectedResourceMetadataHandler(resource_metadata).handle, ["GET", "OPTIONS"]
            ),
            methods=["GET", "OPTIONS"],
        ),
        Route(
            "/revoke",
            endpoint=RequestBodyLimitMiddleware(
                cors_middleware(
                    PublicClientRevocationHandler(provider, ClientAuthenticator(provider)).handle,
                    ["POST", "OPTIONS"],
                ),
                FORM_BODY_LIMIT,
            ),
            methods=["POST", "OPTIONS"],
        ),
        Route(HEALTH_PATH, endpoint=_health, methods=["GET"]),
        Route(APPROVAL_PATH, endpoint=_limited(provider.approval_page), methods=["GET"]),
        Route(APPROVAL_PATH, endpoint=_limited(provider.approval_submit), methods=["POST"]),
        *receiver.routes(),
    ]
    app.router.routes[:] = routes
    return HttpApp(app, server, runtime, provider)


def serve_http(config: Config, *, host: str, port: int) -> int:
    """Run the HTTP server until stopped. Refuses to start without OAuth set up."""

    import os

    import uvicorn

    from .hosting import host_settings

    settings = host_settings()
    built = build_http_app(
        config, settings, extra_redirect_hosts=os.environ.get("FAMILIEKILDER_REDIRECT_HOSTS")
    )
    server = uvicorn.Server(
        uvicorn.Config(
            built.app,
            host=host,
            port=port,
            # Request lines can carry OAuth parameters; never log them.
            access_log=False,
            log_level="warning",
            server_header=False,
            timeout_graceful_shutdown=5,
        )
    )
    try:
        server.run()
    finally:
        built.runtime.close()
    return 0
