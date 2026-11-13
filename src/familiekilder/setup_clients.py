"""Register a local read-only server without replacing other client settings."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

import tomlkit

NAME = "familiekilder-mcp"


class ClientSetupError(ValueError):
    pass


def server_entry(root: Path) -> dict:
    # Keep the venv path: resolving its Python symlink would leave the venv.
    return {"command": sys.executable, "args": ["-m", "familiekilder", "--root", str(root), "mcp", "serve"]}


def config_path(client: str, home: Path) -> Path:
    if client == "claude":
        return home / "Library/Application Support/Claude/claude_desktop_config.json"
    if client == "codex":
        return home / ".codex/config.toml"
    raise ClientSetupError("Velg Claude Desktop eller lokal ChatGPT / Codex.")


def _read(path: Path, home: Path) -> str:
    if not path.is_relative_to(home):
        raise ClientSetupError("Konfigurasjonen må ligge i hjemmemappen.")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ClientSetupError("Konfigurasjonsstien inneholder en symbolsk lenke.")
        if parent == home:
            break
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return ""
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024
                or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022):
            raise ClientSetupError("Konfigurasjonsfilen har utrygg type, eier eller tilgang.")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            return stream.read(1024 * 1024 + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _document(client: str, content: str):
    try:
        document = json.loads(content or "{}") if client == "claude" else tomlkit.parse(content)
        if not isinstance(document, dict):
            raise ValueError
        return document
    except (ValueError, TypeError):
        raise ClientSetupError("Konfigurasjonen kan ikke leses. Den er ikke endret.") from None


def client_state(client: str, root: Path, home: Path) -> dict:
    try:
        document = _document(client, _read(config_path(client, home), home))
        table = document.get("mcpServers" if client == "claude" else "mcp_servers", {})
        entry = table.get(NAME, {})
        expected = server_entry(root)
        active = entry.get("enabled", True) is not False and entry.get("disabled", False) is not True
        return {"id": client, "configured": active and all(entry.get(k) == v for k, v in expected.items()), "error": False}
    except (ClientSetupError, OSError, ValueError, AttributeError):
        return {"id": client, "configured": False, "error": True}


def install_client(client: str, root: Path, home: Path) -> None:
    path = config_path(client, home)
    before = _read(path, home)
    document = _document(client, before)
    key = "mcpServers" if client == "claude" else "mcp_servers"
    table = document.setdefault(key, {})
    if not isinstance(table, dict):
        raise ClientSetupError("Listen over MCP-servere er ugyldig. Filen er ikke endret.")
    entry = table.setdefault(NAME, {})
    if not isinstance(entry, dict):
        raise ClientSetupError("Den eksisterende koblingen er ugyldig. Filen er ikke endret.")
    for field in ("url", "type", "bearer_token_env_var", "http_headers", "env_http_headers", "oauth", "auth"):
        entry.pop(field, None)
    entry.update(server_entry(root))
    if client == "codex":
        entry["enabled"] = True
    else:
        entry.pop("disabled", None)
    content = json.dumps(document, ensure_ascii=False, indent=2) + "\n" if client == "claude" else tomlkit.dumps(document)
    # Validate before touching the existing file. No copies of unrelated secrets.
    _document(client, content)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _read(path, home) != before:
        raise ClientSetupError("Konfigurasjonen ble endret av et annet program. Prøv igjen.")
    descriptor, temporary = tempfile.mkstemp(prefix=".mcp-setup-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


async def check_connection(root: Path) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    entry = server_entry(root)
    parameters = StdioServerParameters(command=entry["command"], args=entry["args"])
    async with asyncio.timeout(25):
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                handshake = await session.initialize()
                tools = await session.list_tools()
                if (handshake.server_info.name != NAME
                        or any(not tool.annotations or not tool.annotations.read_only_hint for tool in tools.tools)):
                    raise ClientSetupError("Serveren tilbyr ikke bare leseverktøy.")
                result = await session.call_tool("family_connections", {})
                if result.is_error:
                    raise ClientSetupError("Serveren kunne ikke lese lokal tilkoblingsstatus.")
                json.loads(result.content[0].text)
                return {"tools": len(tools.tools), "version": handshake.server_info.version}
