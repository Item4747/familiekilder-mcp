"""User-Agent for requests to source services.

Requests never name this package or anything built on it. Until the provider
bundle carries each app's own User-Agent, every source sends the User-Agent of
the Chromium browser the logins run in. Brave and Chrome send the same reduced
User-Agent, where only the Chromium major version varies.
"""

from __future__ import annotations

import functools
import plistlib
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from xml.parsers.expat import ExpatError

# Used when no installed Brave or Chrome can be read, as in hosted and test runs.
FALLBACK_CHROMIUM_MAJOR = 156

_MAJOR = re.compile(r"[1-9][0-9]{2}")
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
    from .browser_bootstrap import _BRAVE_PATHS, _CHROME_PATHS

    return (*_BRAVE_PATHS, *_CHROME_PATHS)


def installed_chromium_major(executables: Iterable[Path] | None = None) -> int | None:
    """Read the Chromium major version of the first installed Brave or Chrome.

    Both apps start CFBundleShortVersionString with it: Brave as
    "156.1.98.62", Chrome as "156.0.7444.60".
    """

    for executable in _browser_executables() if executables is None else executables:
        info = executable.parents[1] / "Info.plist"
        try:
            with info.open("rb") as handle:
                version = plistlib.load(handle).get("CFBundleShortVersionString")
        except (OSError, ValueError, ExpatError, AttributeError):
            continue
        major = version.split(".", 1)[0] if isinstance(version, str) else ""
        if _MAJOR.fullmatch(major):
            return int(major)
    return None


@functools.cache
def source_user_agent() -> str:
    """User-Agent for every request to a source service in this process."""

    return chromium_user_agent(installed_chromium_major() or FALLBACK_CHROMIUM_MAJOR)
