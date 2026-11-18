"""Output that never stops a command because the console lacks a character.

Python writes to a pipe or a file in the system's legacy encoding unless
UTF-8 mode is on. On Windows that is often cp1252 for a redirected command
and cp850 for a program that reads the console's code page. Neither has
emoji, and cp850 has no en dash or ellipsis. Names, paths and texts from the
sources can hold any character, so a plain ``print`` could stop a command with
``UnicodeEncodeError`` after it had done its work.

`protect_console` is called once where a command starts. It keeps the
stream's encoding, so what a console can show looks as before, and writes a
character it cannot show as an escape such as ``\\U0001f389``. Standard error
already behaves like that in Python. A UTF-8 stream is left as it is.
`json_text` gives JSON that stays valid on such a stream.
"""

from __future__ import annotations

import codecs
import json
import sys
from typing import Any, TextIO


def _utf8(stream: TextIO) -> bool:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return False
    try:
        return codecs.lookup(encoding).name == "utf-8"
    except LookupError:
        return False


def protect_console(*streams: TextIO) -> None:
    """Escape characters standard output or error cannot show, instead of failing."""

    for stream in streams or (sys.stdout, sys.stderr):
        if stream is None or _utf8(stream):
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):
            pass


def json_text(value: Any, stream: TextIO | None = None, **options: Any) -> str:
    """``json.dumps`` with readable letters where the stream can show them.

    On a stream that cannot encode every character, non-ASCII characters are
    written as JSON escapes, so the output stays valid JSON.
    """

    stream = sys.stdout if stream is None else stream
    text = json.dumps(value, ensure_ascii=False, **options)
    if _utf8(stream):
        return text
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return json.dumps(value, ensure_ascii=True, **options)
    return text
