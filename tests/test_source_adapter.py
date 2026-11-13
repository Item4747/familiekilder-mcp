from __future__ import annotations

import unittest
from unittest.mock import patch

from familiekilder.core import FamiliekilderError
from familiekilder.source_adapter import adapter_sources, get_adapter


class SourceAdapterTests(unittest.TestCase):
    def test_registry_lookup_is_lazy_and_covers_every_source(self) -> None:
        self.assertEqual(
            adapter_sources(),
            ("vigilo", "skolemelding", "spond", "hoopit", "ist-home", "gmail"),
        )
        with patch("familiekilder.source_adapter.importlib.import_module") as imported:
            adapter = get_adapter("vigilo")
            imported.assert_not_called()
        self.assertEqual(adapter.source, "vigilo")
        with self.assertRaisesRegex(FamiliekilderError, "Ukjent kilde"):
            get_adapter("ukjent")


if __name__ == "__main__":
    unittest.main()
