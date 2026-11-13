import contextlib
import io
import os
import shutil
import unittest
from unittest.mock import patch

from familiekilder import login_passwords as passwords
from familiekilder import source_cli
from familiekilder.core import FamiliekilderError, project_paths
from familiekilder.live_config import Config
from familiekilder.private_state import atomic_write_private_json
from familiekilder.profiles import resolve_root
from familiekilder_testing.profiles import PersonalProfileFixture


class PersonalProfileTests(PersonalProfileFixture, unittest.TestCase):
    def test_profiles_isolate_services_sessions_and_passwords(self):
        owner = Config.load(profile="owner")
        neighbor = Config.load(profile="neighbor")
        Config(owner.root, {"spond": frozenset()}).save()
        atomic_write_private_json(
            owner.private / "credentials/spond-session.json", {"token": "owner-session"}
        )
        passwords.set_password(owner.root, "bankid", "owner-password")
        self.assertEqual(Config.load(profile="neighbor").services, {})
        self.assertFalse((neighbor.private / "credentials/spond-session.json").exists())
        self.assertIsNone(passwords.get_password(neighbor.root, "bankid"))
        passwords.set_password(neighbor.root, "bankid", "neighbor-password")
        self.assertEqual(passwords.get_password(owner.root, "bankid"), "owner-password")
        self.assertEqual(
            passwords.get_password(neighbor.root, "bankid"), "neighbor-password"
        )
        passwords.delete_password(neighbor.root, "bankid")
        self.assertEqual(passwords.get_password(owner.root, "bankid"), "owner-password")
        self.assertNotIn(
            "owner-password", (owner.private / "login-passwords.json").read_text()
        )
        self.assertEqual(
            (owner.private / "login-passwords.json").stat().st_mode & 0o777, 0o600
        )

    def test_copy_does_not_reuse_password_identity(self):
        owner = resolve_root(profile="owner")
        neighbor = resolve_root(profile="neighbor")
        passwords.set_password(owner, "bankid", "owner-password")
        shutil.copytree(owner, neighbor)
        with self.assertRaises(FamiliekilderError):
            passwords.get_password(neighbor, "bankid")

    def test_no_default_password_fallback_for_a_service_or_profile(self):
        passwords.set_password(self.base, "bankid", "default-password")
        self.assertIsNone(passwords.get_password(self.base, "spond"))
        self.assertIsNone(
            passwords.get_password(resolve_root(profile="neighbor"), "bankid")
        )

    def test_profile_environment_applies_consistently_once(self):
        with patch.dict(os.environ, {"FAMILIEKILDER_PROFILE": "neighbor"}):
            root = self.base / "profiles/neighbor"
            self.assertEqual(Config.load().root, root)
            self.assertEqual(project_paths().root, root)
            self.assertEqual(project_paths(root).root, root)
            self.assertEqual(Config.load(root).root, root)

    def test_working_directory_is_not_a_data_root(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("pathlib.Path.home", return_value=self.base),
        ):
            expected = self.base / "Library/Application Support/familiekilder-mcp"
            self.assertEqual(project_paths().root, expected)
            self.assertEqual(Config.load().root, expected)

    def test_invalid_names_and_ambiguous_arguments_are_rejected(self):
        for name in ("", "../owner", "/owner", "x/y", "x\\y", ".", "a" * 65):
            with self.subTest(name=name), self.assertRaises(FamiliekilderError):
                resolve_root(profile=name)
        with self.assertRaises(FamiliekilderError):
            resolve_root(self.base, profile="neighbor")

    def test_profile_symlink_cannot_alias_owner(self):
        owner = resolve_root(profile="owner")
        owner.mkdir(parents=True)
        (owner.parent / "neighbor").symlink_to(owner, target_is_directory=True)
        with self.assertRaises(FamiliekilderError):
            Config.load(profile="neighbor")

    def test_cli_selects_profile_without_touching_default(self):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"FAMILIEKILDER_ROOT": str(self.base)}),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(source_cli.main(["--profile", "neighbor", "configure", "hoopit"]), 0)
        self.assertEqual(set(Config.load(profile="neighbor").services), {"hoopit"})
        self.assertFalse((self.base / ".private").exists())

    def test_standalone_default_root_uses_public_directory(self):
        home = self.base / "home"
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.home", return_value=home), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(source_cli.main(["configure", "spond"]), 0)
        root = home / "Library/Application Support/familiekilder-mcp"
        self.assertEqual(Config.load(root).services, {"spond": frozenset()})
        self.assertFalse((self.base / ".private").exists())

    def test_keychain_failure_is_sanitized_and_no_file_fallback(self):
        self.backend_mock.side_effect = RuntimeError("private-password")
        with self.assertRaises(FamiliekilderError) as error:
            passwords.set_password(self.base, "bankid", "private-password")
        self.assertNotIn("private-password", str(error.exception))
        self.assertFalse(passwords._path(self.base).exists())

    def test_noninteractive_set_refuses_password_arguments_and_stdin(self):
        with (
            patch("sys.stdin.isatty", return_value=False),
            self.assertRaises(FamiliekilderError),
        ):
            passwords.run_password_command(self.base, "bankid", "set")

    def test_missing_keychain_entry_does_not_fall_back(self):
        passwords.set_password(self.base, "bankid", "password")
        self.keychain.values.clear()
        with self.assertRaises(FamiliekilderError):
            passwords.get_password(self.base, "bankid")


if __name__ == "__main__":
    unittest.main()
