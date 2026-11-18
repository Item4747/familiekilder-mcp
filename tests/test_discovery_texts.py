"""README, package keywords and the prompt page name every service in REGISTRY.

The service lists in these texts are written by hand, so these tests stop a new
service from being added to REGISTRY without them.
"""

import asyncio
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

from familiekilder.live_config import Config
from familiekilder.source_server import build_server
from familiekilder.sources import REGISTRY

PROJECT = Path(__file__).resolve().parents[1]
README = PROJECT / "README.md"
PROMPTS = PROJECT / "docs/oppskrifter.md"
SERVICE_LINE_MARKER = "<!-- Tjenestelisten er hentet fra sources.REGISTRY. -->"
# A tool name in backticks, such as `spond_list_chats`.
TOOL_NAME = re.compile(r"`([a-z]+(?:_[a-z]+)+)`")


def flat(text: str) -> str:
    """Text with every run of white space as one space."""
    return " ".join(text.split())


def unofficial_sources():
    """Services read without an official API. Only Google sign-in is official."""
    return [source for source in REGISTRY.values() if source.login != "google"]


def quoted_prompts(text: str) -> list[str]:
    """Each block quote in a Markdown text, as one flat string."""
    prompts, current = [], []
    for line in text.splitlines() + [""]:
        if line.startswith(">"):
            current.append(line[1:].strip())
        elif current:
            prompts.append(flat(" ".join(current)))
            current = []
    return prompts


def read_only_tool_names() -> set[str]:
    with tempfile.TemporaryDirectory() as directory:
        config = Config(Path(directory), {name: frozenset() for name in REGISTRY})
        server, runtime = build_server(config, read_only=True)
        try:
            tools = asyncio.run(server.list_tools())
        finally:
            runtime.close()
    return {tool.name for tool in tools}


class DiscoveryTextTests(unittest.TestCase):
    def test_readme_service_line_names_every_service(self):
        lines = README.read_text().splitlines()
        service_line = lines[lines.index(SERVICE_LINE_MARKER) + 1]
        for source in REGISTRY.values():
            with self.subTest(source=source.name):
                self.assertIn(source.label, service_line)

    def test_readme_search_words_name_every_unofficial_service(self):
        readme = flat(README.read_text())
        self.assertIn("uoffisiell", readme)
        self.assertIn("macOS", readme)
        self.assertIn("Brave", readme)
        for source in unofficial_sources():
            with self.subTest(source=source.name):
                self.assertIn(f"{source.label} API", readme)

    def test_package_keywords_name_mcp_and_every_service(self):
        project = tomllib.loads((PROJECT / "pyproject.toml").read_text())["project"]
        keywords = project["keywords"]
        self.assertIn("mcp", keywords)
        for source in REGISTRY.values():
            with self.subTest(source=source.name):
                self.assertIn(source.label.casefold(), keywords)

    def test_prompt_page_keeps_every_readme_prompt(self):
        readme = README.read_text()
        self.assertIn("](docs/oppskrifter.md)", readme)
        page = quoted_prompts(PROMPTS.read_text())
        prompts = quoted_prompts(readme)
        self.assertGreaterEqual(len(prompts), 4)
        for prompt in prompts:
            with self.subTest(prompt=prompt[:40]):
                self.assertIn(prompt, page)

    def test_prompt_page_names_only_tools_the_server_offers(self):
        named = set(TOOL_NAME.findall(PROMPTS.read_text()))
        self.assertIn("family_connections", named)
        self.assertEqual(named - read_only_tool_names(), set())


if __name__ == "__main__":
    unittest.main()
