from __future__ import annotations

import shlex
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from familiekilder.android_oauth import (
    _android_jar,
    _android_tools,
    _receiver_sources,
    _view_command,
)


class AndroidOAuthTests(unittest.TestCase):
    def test_configured_linux_sdk_supplies_tools_and_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory) / "sdk"
            expected = {}
            for name in ("adb", "aapt2", "d8", "zipalign", "apksigner"):
                relative = "platform-tools" if name == "adb" else "build-tools/36.1.0"
                tool = sdk / relative / name
                tool.parent.mkdir(parents=True, exist_ok=True)
                tool.write_text("#!/bin/sh\nexit 0\n")
                tool.chmod(0o700)
                expected[name] = tool.resolve()
            jar = sdk / "platforms/android-35/android.jar"
            jar.parent.mkdir(parents=True)
            jar.touch()
            with patch.dict(os.environ, {"ANDROID_HOME": str(sdk)}, clear=True), patch(
                "familiekilder.android_oauth.shutil.which", return_value="/usr/bin/true"
            ):
                tools = _android_tools()
                self.assertEqual({name: tools[name] for name in expected}, expected)
                self.assertEqual(_android_jar(), jar.resolve())

    def test_legacy_sdk_environment_finds_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory)
            jar = sdk / "platforms/android-35/android.jar"
            jar.parent.mkdir(parents=True)
            jar.touch()
            with patch.dict(os.environ, {"ANDROID_SDK_ROOT": str(sdk)}, clear=True):
                self.assertEqual(_android_jar(), jar.resolve())

    def test_existing_macos_sdk_remains_available(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            jar = home / "Library/Android/sdk/platforms/android-35/android.jar"
            jar.parent.mkdir(parents=True)
            jar.touch()
            with patch.dict(os.environ, {}, clear=True), patch(
                "familiekilder.android_oauth.Path.home", return_value=home
            ):
                self.assertEqual(_android_jar(), jar.resolve())

    def test_receiver_sources_are_part_of_the_python_package(self):
        import xml.etree.ElementTree as ET

        sources = _receiver_sources()
        manifest = ET.parse(sources / "AndroidManifest.xml").getroot()
        self.assertEqual(manifest.attrib["package"], "no.familiekilder.oauthreceiver")
        java = sources / "src/no/familiekilder/oauthreceiver/MainActivity.java"
        self.assertIn("class MainActivity", java.read_text())

    def test_remote_shell_command_preserves_complete_oauth_url(self) -> None:
        authorization_url = (
            "https://auth.prod.vigilo-oas.no/connect/authorize?"
            "client_id=client&redirect_uri=app%3A%2F%2Fcallback&"
            "scope=openid+offline_access&state=private-state"
        )

        arguments = shlex.split(_view_command(authorization_url))

        self.assertEqual(
            arguments[arguments.index("-n") + 1],
            "no.familiekilder.oauthreceiver/.MainActivity",
        )
        self.assertEqual(
            arguments[arguments.index("-a") + 1],
            "no.familiekilder.oauthreceiver.AUTHORIZE",
        )
        self.assertEqual(arguments[-2], "-d")
        self.assertEqual(arguments[-1], authorization_url)
        self.assertEqual(arguments.count(authorization_url), 1)


if __name__ == "__main__":
    unittest.main()
