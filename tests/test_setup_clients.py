from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest

import tomlkit

from familiekilder.setup_clients import (
    ClientSetupError,
    client_state,
    config_path,
    install_client,
    server_entry,
)


class ClientRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / "profile"

    def test_server_ignores_a_familiekilder_module_in_the_working_directory(self):
        entry = server_entry(self.root)
        self.assertEqual(entry["args"][:3], ["-I", "-m", "familiekilder"])
        project = self.home / "project"
        project.mkdir()
        (project / "familiekilder.py").write_text("raise SystemExit('shadow module ran')\n")
        import subprocess

        result = subprocess.run(
            [entry["command"], "-I", "-m", "familiekilder", "--version"],
            cwd=project, capture_output=True, text=True, timeout=60,
        )
        self.assertNotIn("shadow module ran", result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_claude_preserves_other_servers_and_settings(self):
        path = config_path("claude", self.home)
        path.parent.mkdir(parents=True)
        original = {"mcpServers": {"other": {"command": "fictional", "env": {"TOKEN": "not-a-real-token"}}}, "preferences": {"theme": "dark"}}
        path.write_text(json.dumps(original))
        install_client("claude", self.root, self.home)
        result = json.loads(path.read_text())
        self.assertEqual(result["mcpServers"]["other"], original["mcpServers"]["other"])
        self.assertEqual(result["preferences"], original["preferences"])
        self.assertTrue(client_state("claude", self.root, self.home)["configured"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        once = path.read_bytes()
        install_client("claude", self.root, self.home)
        self.assertEqual(path.read_bytes(), once)

    def test_codex_preserves_comments_and_other_servers(self):
        path = config_path("codex", self.home)
        path.parent.mkdir()
        path.write_text('# Keep this comment\nmodel = "fictional"\n[mcp_servers.other]\ncommand = "other"\n')
        install_client("codex", self.root, self.home)
        document = tomlkit.parse(path.read_text())
        self.assertEqual(document["model"], "fictional")
        self.assertEqual(document["mcp_servers"]["other"]["command"], "other")
        self.assertIn("# Keep this comment", path.read_text())
        self.assertTrue(client_state("codex", self.root, self.home)["configured"])
        install_client("codex", self.root, self.home)
        self.assertTrue(client_state("codex", self.root, self.home)["configured"])

    def test_malformed_config_is_not_overwritten(self):
        for client, text in (("claude", '{"broken":'), ("codex", "broken = [")):
            with self.subTest(client=client):
                path = config_path(client, self.home)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                with self.assertRaises(ClientSetupError):
                    install_client(client, self.root, self.home)
                self.assertEqual(path.read_text(), text)

    def test_symlink_is_rejected(self):
        path = config_path("codex", self.home)
        path.parent.mkdir()
        target = self.home / "outside.toml"
        target.write_text('model = "fictional"\n')
        path.symlink_to(target)
        with self.assertRaises(ClientSetupError):
            install_client("codex", self.root, self.home)
        self.assertEqual(target.read_text(), 'model = "fictional"\n')


if __name__ == "__main__":
    unittest.main()
