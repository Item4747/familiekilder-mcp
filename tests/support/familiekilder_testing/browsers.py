"""A made-up computer for the browser catalog: files, signatures and folders.

`FakeBrowserSystem` stands in for `familiekilder.browsers.System`. Files are
given as a mapping from path to stat result; a folder on the way to a known
file exists (owned by root outside Windows). Signatures are the vendors' own
unless a test replaces one. Nothing here runs a program or touches a disk.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import types
from pathlib import PurePosixPath

from familiekilder import browsers

FILE = stat.S_IFREG | 0o755
DIRECTORY = stat.S_IFDIR | 0o755
LINK = stat.S_IFLNK | 0o777

PROGRAM_FILES = r"C:\Program Files"
PROGRAM_FILES_X86 = r"C:\Program Files (x86)"

# What `codesign -dv` prints for each vendor's app bundle, in short.
MAC_SIGNATURES = {
    "Brave Browser.app": "Identifier=com.brave.Browser\nTeamIdentifier=KL8N8XSYF4\n",
    "Google Chrome.app": "Identifier=com.google.Chrome\nTeamIdentifier=EQHXZ8M8AV\n",
    "Microsoft Edge.app": "Identifier=com.microsoft.edgemac\nTeamIdentifier=UBF8T346G9\n",
}

# The certificate subject for each vendor folder below a Windows program folder.
_WINDOWS_PUBLISHERS = {
    "BraveSoftware": "Brave Software, Inc.",
    "Google": "Google LLC",
    "Microsoft": "Microsoft Corporation",
}


def stat_result(mode: int, uid: int = 501, attributes: int = 0) -> os.stat_result:
    if attributes:
        # st_file_attributes exists only on Windows; a namespace stands in.
        return types.SimpleNamespace(  # type: ignore[return-value]
            st_mode=mode, st_uid=uid, st_file_attributes=attributes
        )
    return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))


class FakeBrowserSystem(browsers.System):
    """One made-up computer. `calls` records every program the catalog runs."""

    def __init__(
        self,
        platform: str,
        files: dict[str, os.stat_result] | None = None,
        *,
        home: str,
        environ: dict[str, str] | None = None,
        local_app_data: str | None = None,
    ) -> None:
        self.platform = platform
        self.files = dict(files or {})
        self.home_folder = home
        self.env = dict(environ or {})
        self.local_folder = local_app_data
        self.calls: list[list[str]] = []
        self.signatures = dict(MAC_SIGNATURES)
        self.spctl = 0
        # Windows: a check report per executable path, instead of the vendor's own.
        self.windows_reports: dict[str, dict] = {}
        self.links: dict[str, str] = {}

    def lstat(self, path: str) -> os.stat_result:
        if path in self.files:
            return self.files[path]
        if self.platform != browsers.WINDOWS:
            if any(known.startswith(path.rstrip("/") + "/") for known in self.files):
                return stat_result(DIRECTORY, uid=0)
        else:
            lowered = path.lower().rstrip("\\") + "\\"
            if any(known.lower().startswith(lowered) for known in self.files):
                return stat_result(DIRECTORY)
        raise FileNotFoundError(path)

    def executable(self, path: str) -> bool:
        return path in self.files and bool(self.files[path].st_mode & 0o111)

    def listdir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted(
            {known[len(prefix) :].split("/")[0] for known in self.files if known.startswith(prefix)}
        )

    def readlink(self, path: str) -> str:
        return self.links[path]

    def run(self, argv, *, env=None, timeout=15):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "/usr/sbin/spctl":
            return subprocess.CompletedProcess(argv, self.spctl, "", "")
        if argv[0] == "/usr/bin/codesign":
            bundle = PurePosixPath(argv[-1]).parents[2].name
            return subprocess.CompletedProcess(argv, 0, self.signatures.get(bundle, ""), "")
        if argv[0].endswith("powershell.exe"):
            path = (env or {})["FAMILIEKILDER_BROWSER_CHECK"]
            report = self.windows_reports.get(path) or self._vendor_report(path)
            return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")
        raise AssertionError(argv)

    @staticmethod
    def _vendor_report(path: str) -> dict:
        subject = next(
            (name for folder, name in _WINDOWS_PUBLISHERS.items() if f"\\{folder}\\" in path),
            None,
        )
        return {"status": "Valid", "subject": subject, "writers": ["S-1-5-18", "S-1-5-32-544"]}

    def home(self) -> str:
        return self.home_folder

    def program_folders(self) -> tuple[str, ...]:
        return (PROGRAM_FILES, PROGRAM_FILES_X86)

    def system_folder(self) -> str:
        return r"C:\Windows\System32"

    def environ(self):
        return self.env

    def local_app_data(self) -> str | None:
        return self.local_folder
