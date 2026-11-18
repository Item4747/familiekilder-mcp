"""User-Agent for requests to source services.

Requests never name this package or anything built on it. Until the provider
bundle carries each app's own User-Agent, every source sends the User-Agent of
the Chromium browser the logins run in. Brave, Chrome and Edge send the same
reduced User-Agent, where only the Chromium major version varies.
"""

from __future__ import annotations

import functools
import plistlib
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from xml.parsers.expat import ExpatError

# Used when no installed browser can be read, as in hosted and test runs and on
# Linux, where the packages have no version file to read.
FALLBACK_CHROMIUM_MAJOR = 156

_MAJOR = re.compile(r"[1-9][0-9]{2}")
_WINDOWS_VERSION_FOLDER = re.compile(r"([1-9][0-9]{2})\.[0-9]+\.[0-9]+\.[0-9]+")
_PLATFORM_TOKENS = {
    "darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "linux": "X11; Linux x86_64",
    "win32": "Windows NT 10.0; Win64; x64",
}


def chromium_user_agent(major: int, platform: str | None = None) -> str:
    """Return the reduced User-Agent a desktop Chromium browser sends."""

    token = _PLATFORM_TOKENS.get(platform or sys.platform, _PLATFORM_TOKENS["darwin"])
    return (
        f"Mozilla/5.0 ({token}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def _browser_executables() -> tuple[Path, ...]:
    from .browsers import catalog

    return tuple(Path(path) for app in catalog() for path in app.executables)


def _macos_major(executable: Path) -> int | None:
    info = executable.parents[1] / "Info.plist"
    try:
        with info.open("rb") as handle:
            version = plistlib.load(handle).get("CFBundleShortVersionString")
    except (OSError, ValueError, ExpatError, AttributeError, IndexError):
        return None
    major = version.split(".", 1)[0] if isinstance(version, str) else ""
    return int(major) if _MAJOR.fullmatch(major) else None


def _windows_major(executable: Path) -> int | None:
    """Windows keeps each version in a folder next to the .exe, as 156.0.8078.80."""

    try:
        names = [entry.name for entry in executable.parent.iterdir() if entry.is_dir()]
    except OSError:
        return None
    majors = [
        int(found.group(1))
        for found in map(_WINDOWS_VERSION_FOLDER.fullmatch, names)
        if found
    ]
    return max(majors, default=None)


def installed_chromium_major(executables: Iterable[Path] | None = None) -> int | None:
    """Read the Chromium major version of the first installed catalog browser.

    On macOS each app starts CFBundleShortVersionString with it: Brave as
    "156.1.98.62", Chrome as "156.0.7444.60". On Windows the version folder
    next to the .exe starts with it.
    """

    for executable in _browser_executables() if executables is None else executables:
        if executable.suffix.lower() == ".exe":
            major = _windows_major(executable)
        else:
            major = _macos_major(executable)
        if major is not None:
            return major
    return None


@functools.cache
def source_user_agent() -> str:
    """User-Agent for every request to a source service in this process."""

    return chromium_user_agent(installed_chromium_major() or FALLBACK_CHROMIUM_MAJOR)
