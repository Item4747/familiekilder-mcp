from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from familiekilder.private_state import (
    PrivateStateError,
    atomic_write_private_bytes,
    atomic_write_private_json,
    credential_path,
    credentials_directory,
    read_private_bytes,
    read_private_json,
    remove_private_file,
)


class PrivateStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_credentials_are_private_bounded_and_validated(self) -> None:
        directory = credentials_directory(self.root)
        target = credential_path(self.root, "vigilo-session.json")
        atomic_write_private_json(target, {"version": 1, "token": "private"})
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

        def validate(value):
            if value.get("version") != 1:
                raise PrivateStateError("invalid")
            return value["token"]

        self.assertEqual(read_private_json(target, validator=validate), "private")
        with self.assertRaisesRegex(PrivateStateError, "større"):
            read_private_bytes(target, max_bytes=2)
        with self.assertRaises(PrivateStateError):
            credential_path(self.root, "../outside.json")

    def test_read_rejects_symlink_and_broad_permissions(self) -> None:
        external = self.root / "external.json"
        external.write_text('{"token":"must-survive"}', encoding="utf-8")
        external.chmod(0o600)
        credentials_directory(self.root)
        link = credential_path(self.root, "linked.json")
        try:
            link.symlink_to(external)
        except (NotImplementedError, OSError):
            self.skipTest("symbolske lenker støttes ikke")
        with self.assertRaises(PrivateStateError):
            read_private_json(link)
        self.assertIn("must-survive", external.read_text(encoding="utf-8"))

        broad = credential_path(self.root, "broad.json")
        broad.write_text("{}", encoding="utf-8")
        broad.chmod(0o644)
        if os.name != "nt":
            with self.assertRaisesRegex(PrivateStateError, "brede"):
                read_private_json(broad)

    def test_atomic_failure_removes_secret_temporary_and_preserves_target(self) -> None:
        target = credential_path(self.root, "session.json")
        atomic_write_private_bytes(target, b"old-private")
        with patch(
            "familiekilder.private_state.os.replace",
            side_effect=OSError("synthetic"),
        ):
            with self.assertRaises(PrivateStateError):
                atomic_write_private_bytes(target, b"new-private")
        self.assertEqual(target.read_bytes(), b"old-private")
        self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_no_overwrite_and_safe_remove_do_not_follow_symlinks(self) -> None:
        target = credential_path(self.root, "session.json")
        atomic_write_private_json(target, {"version": 1})
        with self.assertRaisesRegex(PrivateStateError, "finnes allerede"):
            atomic_write_private_json(target, {"version": 2}, overwrite=False)
        self.assertEqual(read_private_json(target)["version"], 1)
        self.assertTrue(remove_private_file(target))
        self.assertFalse(remove_private_file(target))

        external = self.root / "external-secret"
        external.write_text("survives", encoding="utf-8")
        link = credential_path(self.root, "orphan-link")
        try:
            link.symlink_to(external)
        except (NotImplementedError, OSError):
            self.skipTest("symbolske lenker støttes ikke")
        self.assertTrue(remove_private_file(link))
        self.assertEqual(external.read_text(encoding="utf-8"), "survives")


if __name__ == "__main__":
    unittest.main()
