from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from familiekilder import __version__


class StdioContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_entrypoint_announces_version_and_serves_an_empty_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "familiekilder", "--root", directory, "mcp", "serve"],
                env={
                    "PATH": os.defpath,
                    "HOME": directory,
                    "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
                },
            )
            async with asyncio.timeout(20):
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        handshake = await session.initialize()
                        self.assertEqual(handshake.server_info.name, "familiekilder-mcp")
                        self.assertEqual(handshake.server_info.version, __version__)
                        tools = await session.list_tools()
                        self.assertEqual([tool.name for tool in tools.tools], ["family_connections"])
                        result = await session.call_tool("family_connections", {})
                        self.assertEqual(json.loads(result.content[0].text)["services"], [])


if __name__ == "__main__":
    unittest.main()
