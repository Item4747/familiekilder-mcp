"""Requests to source services carry a browser User-Agent, never the package name."""

import ast
import plistlib
import re
import tempfile
import unittest
from pathlib import Path

import familiekilder
from familiekilder.api_transport import ApiTransport
from familiekilder.user_agent import (
    FALLBACK_CHROMIUM_MAJOR,
    chromium_user_agent,
    installed_chromium_major,
    source_user_agent,
)

_NAMED_AGENT = re.compile(r"familie(?:innboks|kilder)[\w-]*/\d", re.IGNORECASE)


def _app(root: Path, name: str, info: bytes | None) -> Path:
    contents = root / f"{name}.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    if info is not None:
        (contents / "Info.plist").write_bytes(info)
    return contents / "MacOS" / name


class ChromiumUserAgentTest(unittest.TestCase):
    def test_reduced_user_agent_per_platform(self):
        self.assertEqual(
            chromium_user_agent(156, "darwin"),
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        )
        self.assertEqual(
            chromium_user_agent(156, "linux"),
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        )
        self.assertIn("(Macintosh;", chromium_user_agent(156, "unknown"))


class InstalledVersionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_reads_brave_and_chrome_version_formats(self):
        brave = _app(
            self.root, "Brave", plistlib.dumps({"CFBundleShortVersionString": "156.1.98.62"})
        )
        chrome = _app(
            self.root, "Chrome", plistlib.dumps({"CFBundleShortVersionString": "157.0.7500.1"})
        )
        self.assertEqual(installed_chromium_major([brave, chrome]), 156)
        self.assertEqual(installed_chromium_major([chrome]), 157)

    def test_skips_missing_and_invalid_installations(self):
        missing = _app(self.root, "Missing", None)
        broken = _app(self.root, "Broken", b"<plist><dict><key>")
        not_a_dict = _app(self.root, "List", plistlib.dumps(["156.1.98.62"]))
        no_major = _app(self.root, "Odd", plistlib.dumps({"CFBundleShortVersionString": "1.98.62"}))
        valid = _app(
            self.root, "Brave", plistlib.dumps({"CFBundleShortVersionString": "155.1.97.1"})
        )
        self.assertIsNone(installed_chromium_major([missing, broken, not_a_dict, no_major]))
        self.assertEqual(
            installed_chromium_major([missing, broken, not_a_dict, no_major, valid]), 155
        )


class SourceRequestTest(unittest.TestCase):
    def test_transport_defaults_to_browser_user_agent(self):
        agent = ApiTransport(allowed_hosts={"example.test"}).user_agent
        self.assertEqual(agent, source_user_agent())
        self.assertRegex(
            agent, r"\AMozilla/5\.0 \(.+\) Chrome/[1-9][0-9]{2}\.0\.0\.0 Safari/537\.36\Z"
        )
        self.assertIsNone(_NAMED_AGENT.search(agent))
        self.assertGreaterEqual(FALLBACK_CHROMIUM_MAJOR, 100)

    def test_no_source_module_names_itself_in_a_user_agent(self):
        package = Path(familiekilder.__file__).parent
        offenders = []
        for path in sorted(package.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if _NAMED_AGENT.search(node.value):
                        offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
