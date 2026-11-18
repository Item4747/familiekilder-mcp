"""Word attachments: the LibreOffice call, with fake converters only."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from familiekilder import live_attachments


class WordConversionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "oppdiktet-skriv.docx"
        self.source.write_bytes(b"fictional docx bytes")
        self.work = self.root / ".private/work"
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_run(self, args, **kwargs):
        name = Path(args[0]).name
        self.calls.append((name, kwargs["env"]))
        stdout = b""
        if name == "soffice":
            temp = Path(kwargs["env"]["TMPDIR"])
            self.assertTrue(temp.is_dir())
            self.assertEqual(stat.S_IMODE(temp.stat().st_mode), 0o700)
            outdir = Path(args[args.index("--outdir") + 1])
            (outdir / (self.source.stem + ".pdf")).write_bytes(b"%PDF-fictional")
        elif name == "pdfinfo":
            stdout = b"Pages:          1\n"
        elif name == "pdftoppm":
            Path(args[-1]).with_suffix(".png").write_bytes(b"fictional png")
        elif name == "pdftotext":
            stdout = "Oppdiktet juleverksted på fredag".encode()
        return subprocess.CompletedProcess(args, 0, stdout, b"")

    def test_libreoffice_gets_a_private_temp_directory_inside_the_work_directory(self):
        with (
            patch.object(live_attachments, "_executable", side_effect=lambda name: name),
            patch.object(live_attachments.subprocess, "run", side_effect=self.fake_run),
        ):
            pages = live_attachments.render(
                self.source,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                self.work,
            )
        self.assertEqual(pages[0]["text"], "Oppdiktet juleverksted på fredag")
        environments = dict(self.calls)
        self.assertEqual(environments["soffice"]["TMPDIR"], str(self.work / "tmp"))
        # Only LibreOffice gets its own TMPDIR; the Poppler tools keep the environment.
        self.assertEqual(environments["pdfinfo"].get("TMPDIR"), os.environ.get("TMPDIR"))


if __name__ == "__main__":
    unittest.main()
