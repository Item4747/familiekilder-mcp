"""Personal profiles in a temporary data root, with a keychain in memory.

Tests never touch the real macOS keychain or the user's own data folder.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from familiekilder import login_passwords as passwords


class MemoryKeychain:
    def __init__(self):
        self.values = {}

    def set_password(self, service, account, password):
        self.values[service, account] = password

    def get_password(self, service, account):
        return self.values.get((service, account))

    def delete_password(self, service, account):
        del self.values[service, account]


class PersonalProfileFixture:
    """A temporary FAMILIEKILDER_ROOT and a keychain in memory for each test."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.environment = patch.dict(
            os.environ, {"FAMILIEKILDER_ROOT": str(self.base)}, clear=True
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.keychain = MemoryKeychain()
        self.backend = patch.object(passwords, "_backend", return_value=self.keychain)
        self.backend_mock = self.backend.start()
        self.addCleanup(self.backend.stop)
