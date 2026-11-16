"""The built-in client setup: each entry passes its own source's validation."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from importlib import resources
from pathlib import Path

from familiekilder.client_defaults import DATA_FILE, default_client_config
from familiekilder.connection_status import auth_module
from familiekilder.private_state import atomic_write_private_bytes
from familiekilder.sources import REGISTRY


def shipped_sources() -> list[str]:
    text = resources.files("familiekilder").joinpath("data", DATA_FILE).read_text(encoding="utf-8")
    return sorted(json.loads(text))


def comparable(value):
    # The auth modules store lists sorted; the order in the file does not matter.
    return sorted(value) if isinstance(value, list) else value


class ClientDefaultsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def configure(self, source: str, config: dict) -> Path:
        root = self.directory / source / "data"
        root.mkdir(parents=True)
        client_file = self.directory / source / ".private/client.json"
        atomic_write_private_bytes(client_file, json.dumps(config).encode())
        auth_module(source).configure_client(root, client_file)
        return root

    def test_file_covers_known_sources_only(self):
        sources = shipped_sources()
        self.assertIn("spond", sources)
        self.assertTrue(set(sources) <= set(REGISTRY), sources)
        self.assertNotIn("flyt", sources)
        self.assertNotIn("gmail", sources)

    def test_every_default_passes_its_own_validation_in_an_empty_root(self):
        for source in shipped_sources():
            with self.subTest(source=source):
                config = default_client_config(source)
                root = self.configure(source, config)
                module = auth_module(source)
                self.assertTrue(module.auth_status(root)["configured"])
                stored = module.load_client_config(root).as_private_json()
                for key, value in config.items():
                    self.assertEqual(comparable(stored[key]), comparable(value), key)

    def test_defaults_only_hold_fields_the_source_allows(self):
        for source in shipped_sources():
            with self.subTest(source=source):
                config_class = auth_module(source).ClientConfig
                allowed = {field.name for field in dataclasses.fields(config_class)}
                self.assertTrue(allowed, source)
                self.assertEqual(set(default_client_config(source)) - allowed, set())

    def test_sources_without_built_in_setup_give_none(self):
        for source in ("flyt", "gmail", "unknown-source", "", None, 7):
            with self.subTest(source=source):
                self.assertIsNone(default_client_config(source))

    def test_each_call_returns_a_fresh_copy(self):
        first = default_client_config("spond")
        first["attachment_hosts"].append("changed.example.invalid")
        first["app_version"] = "changed"
        second = default_client_config("spond")
        self.assertNotIn("changed.example.invalid", second["attachment_hosts"])
        self.assertEqual(second["app_version"], "web")

    def test_spond_default_is_the_public_web_profile(self):
        from familiekilder.spond import GROUP_FILE_STORAGE_HOST, IMAGE_STORAGE_HOST
        from familiekilder.spond_auth import API_ROOT

        self.assertEqual(
            default_client_config("spond"),
            {
                "api_root": API_ROOT,
                "app_version": "web",
                "attachment_hosts": [GROUP_FILE_STORAGE_HOST, IMAGE_STORAGE_HOST],
            },
        )


if __name__ == "__main__":
    unittest.main()
