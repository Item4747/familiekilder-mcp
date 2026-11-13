import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from familiekilder.android_passwords import AndroidPasswordSession, claim_device
from familiekilder.core import FamiliekilderError


class AndroidProfileTests(unittest.TestCase):
    def test_device_cannot_be_shared_between_profiles(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("pathlib.Path.home", return_value=Path(directory)),
        ):
            claim_device(Path(directory) / "owner", "test-device", bind=True)
            claim_device(Path(directory) / "owner", "test-device")
            with self.assertRaises(FamiliekilderError):
                claim_device(Path(directory) / "neighbor", "test-device")
            with self.assertRaises(FamiliekilderError):
                claim_device(Path(directory) / "neighbor", "neighbors-device")
            claim_device(Path(directory) / "neighbor", "neighbors-device", bind=True)

    def test_unconfigured_profile_does_not_open_adb_or_read_keychain(self):
        with (
            patch("familiekilder.password_autofill.configured", return_value=False),
            patch("familiekilder.android_oauth._adb") as adb,
        ):
            session = AndroidPasswordSession("/neighbor", Path("/adb"), "device")
            self.assertFalse(session.start("https://login.idporten.no/authorize"))
            session.close()
            adb.assert_not_called()

    def test_bridge_removed_when_browser_connection_fails(self):
        with (
            patch("familiekilder.password_autofill.configured", return_value=True),
            patch("familiekilder.android_oauth._adb", return_value="12345") as adb,
            patch("http.client.HTTPConnection", side_effect=OSError("private-context")),
        ):
            session = AndroidPasswordSession("/owner", Path("/adb"), "device")
            self.assertFalse(session.start("https://login.idporten.no/authorize"))
            self.assertIsNone(session.port)
            adb.assert_any_call(
                Path("/adb"), "device", "forward", "--remove", "tcp:12345"
            )


if __name__ == "__main__":
    unittest.main()
