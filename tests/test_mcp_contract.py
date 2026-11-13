from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from familiekilder.live_config import Config
from familiekilder.source_server import Runtime, ReadOnlyRuntime, build_server
from familiekilder.sources import REGISTRY


class ReadOnlyContractTests(unittest.TestCase):
    def test_standalone_read_mode_ignores_persisted_write_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Config(root, {"spond": frozenset({"messages", "participation"})}).save()
            before = (root / ".private/mcp-config.json").read_bytes()
            server, runtime = build_server(Config.load(root), read_only=True)
            try:
                self.assertIsInstance(runtime, ReadOnlyRuntime)
                self.assertFalse(runtime.config.services["spond"])
                tools = asyncio.run(server.list_tools())
                self.assertTrue(all(tool.annotations.read_only_hint for tool in tools))
                self.assertNotIn("spond_send_message", {tool.name for tool in tools})
            finally:
                runtime.close()
            self.assertEqual((root / ".private/mcp-config.json").read_bytes(), before)

    def test_all_sources_offer_only_read_tools_without_write_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(Path(directory), {name: frozenset() for name in REGISTRY})
            server, runtime = build_server(config)
            try:
                tools = asyncio.run(server.list_tools())
            finally:
                runtime.close()
        names = {tool.name for tool in tools}
        self.assertIn("family_connections", names)
        for source in REGISTRY.values():
            prefix = source.name.replace("-", "_")
            with self.subTest(source=source.name):
                self.assertIn(f"{prefix}_get_item", names)
                self.assertIn(f"{prefix}_read_attachment", names)
                self.assertIn("gmail_search" if source.kind == "mail" else f"{prefix}_list_items", names)
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)

    def test_list_and_detail_work_with_a_fictional_source(self) -> None:
        item = {
            "source": "spond",
            "source_id": "post:fictional",
            "title": "Oppdiktet trening",
            "body": "Ta med vannflaske.",
            "reference": {"kind": "post", "id": "fictional"},
            "attachments": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            config = Config(Path(directory), {"spond": frozenset()})
            runtime = Runtime(
                config,
                reader=lambda *_: ([dict(item)], True),
                getter=lambda *_: dict(item),
            )
            server, runtime = build_server(config, runtime=runtime)
            try:
                listed = asyncio.run(server.call_tool("spond_list_items", {}))
                detail = asyncio.run(
                    server.call_tool("spond_get_item", {"reference": item["reference"]})
                )
            finally:
                runtime.close()
        self.assertEqual(json.loads(listed.content[0].text)["items"][0]["source_id"], item["source_id"])
        self.assertEqual(json.loads(detail.content[0].text)["body"], item["body"])


if __name__ == "__main__":
    unittest.main()
