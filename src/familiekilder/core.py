"""Shared parsing and private-path primitives; no archive database or sync state."""

from __future__ import annotations

import html
import os
import re
import shutil
import stat
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .sources import REGISTRY

OSLO = ZoneInfo("Europe/Oslo")


SOURCES = tuple(REGISTRY)
# Signed native provider bundles exclude Google's independently authorized API.
PROVIDER_SOURCES = tuple(source for source in SOURCES if REGISTRY[source].provider_bundle)


SOURCE_ALIASES = {alias: s.name for s in REGISTRY.values() for alias in s.aliases}


# Error texts a program built on this package words its own way; see reword_errors.
_REWORDED: dict[str, str] = {}


def reword_errors(texts: dict[str, str]) -> None:
    """Let a program built on this package show its own wording for some error texts.

    ``texts`` maps an exact error text of this package to the program's text.
    It applies to every FamiliekilderError created afterwards in the process,
    so the program's tools, logs and command line all show the same words.
    """
    _REWORDED.update(texts)


class FamiliekilderError(ValueError):
    def __init__(self, *args: Any) -> None:
        if args and isinstance(args[0], str) and args[0] in _REWORDED:
            args = (_REWORDED[args[0]], *args[1:])
        super().__init__(*args)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return "\n".join(self.parts)


def html_to_text(value: Any) -> str | None:
    """Plain text from a provider's HTML or plain-text field, or None if empty."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if "<" not in candidate and "&" not in candidate:
        return candidate
    parser = _TextExtractor()
    try:
        parser.feed(candidate)
        parser.close()
    except Exception:
        return html.unescape(re.sub(r"<[^>]+>", " ", candidate)).strip() or None
    return parser.text().strip() or html.unescape(candidate).strip() or None


def html_to_text_unescaped(value: Any) -> str:
    """Vigilo and IST Home: parsed text unescaped once more, "" when empty.

    Their stored content hashes are built from this exact output, so it must not
    be merged into html_to_text: `&amp;amp;` becomes `&` here and `<p><br></p>`
    becomes "" instead of the raw markup.
    """
    if not isinstance(value, str):
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(value)
    except Exception:
        return html.unescape(value).strip()
    return html.unescape(parser.text()).strip()


@dataclass(frozen=True)
class Paths:
    root: Path
    private: Path


def project_paths(root: Path | str | None = None) -> Paths:
    from .profiles import resolve_root

    project = resolve_root(root)
    private = project / ".private"
    return Paths(project, private)


def _owned_by_current_user(metadata: os.stat_result) -> bool:
    return not hasattr(os, "geteuid") or metadata.st_uid == os.geteuid()


def _ensure_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
            metadata = path.lstat()
        except OSError:
            raise FamiliekilderError(
                "Den private katalogen kunne ikke opprettes sikkert"
            ) from None
    except OSError:
        raise FamiliekilderError(
            "Den private katalogen kunne ikke kontrolleres sikkert"
        ) from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or not _owned_by_current_user(metadata)
    ):
        raise FamiliekilderError("Den private katalogen har utrygg type eller eier")
    try:
        path.chmod(0o700)
    except OSError:
        raise FamiliekilderError("Den private katalogen kunne ikke sikres") from None


def _secure_private_file(path: Path, *, allow_missing: bool = True) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return False
        raise FamiliekilderError("Den private filen mangler") from None
    except OSError:
        raise FamiliekilderError(
            "Den private filen kunne ikke kontrolleres sikkert"
        ) from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or not _owned_by_current_user(metadata)
    ):
        raise FamiliekilderError("Den private filen har utrygg type eller eier")
    try:
        path.chmod(0o600)
    except FileNotFoundError:
        if allow_missing:
            return False
        raise FamiliekilderError("Den private filen mangler") from None
    except OSError:
        raise FamiliekilderError("Den private filen kunne ikke sikres") from None
    return True


def ensure_private_layout(paths: Paths) -> None:
    _ensure_private_directory(paths.private)


def normalize_source(value: str) -> str:
    source = SOURCE_ALIASES.get(value.strip().lower(), value.strip().lower())
    if source not in SOURCES:
        raise FamiliekilderError(
            f"Ukjent kilde {value!r}; forventet en av: {', '.join(SOURCES)}"
        )
    return source


def parse_iso(value: Any, field: str, *, date_only: bool = False) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise FamiliekilderError(f"{field} må være ISO-tekst eller null")
    candidate = value.strip()
    try:
        if date_only and re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
            return date.fromisoformat(candidate).isoformat()
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FamiliekilderError(
            f"{field} har tvetydig/ugyldig dato {value!r}; normaliser til ISO eller null"
        ) from exc
    if parsed.tzinfo is None:
        first = parsed.replace(tzinfo=OSLO, fold=0)
        second = parsed.replace(tzinfo=OSLO, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise FamiliekilderError(
                f"{field} har tvetydig lokal tid {value!r}; oppgi UTC-offset eller null"
            )
        roundtrip = first.astimezone(UTC).astimezone(OSLO).replace(tzinfo=None)
        if roundtrip != parsed:
            raise FamiliekilderError(
                f"{field} har ikke-eksisterende lokal tid {value!r}; oppgi UTC-offset eller null"
            )
        parsed = first
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    text = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def extract_text(path: Path, mime_type: str) -> tuple[str | None, str | None]:
    try:
        if mime_type in {
            "text/html",
            "application/xhtml+xml",
        } or path.suffix.lower() in {".html", ".htm"}:
            parser = _TextExtractor()
            parser.feed(path.read_text(encoding="utf-8", errors="replace"))
            return parser.text().strip(), None
        if mime_type.startswith("text/") or path.suffix.lower() in {
            ".txt",
            ".csv",
            ".md",
        }:
            return path.read_text(encoding="utf-8", errors="replace").strip(), None
        if (
            mime_type
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            or path.suffix.lower() == ".docx"
        ):
            return _extract_docx(path), None
        if mime_type == "application/pdf" or path.suffix.lower() == ".pdf":
            executable = shutil.which("pdftotext")
            if not executable:
                return None, "pdftotext er ikke installert"
            result = subprocess.run(
                [executable, "-layout", str(path), "-"],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                return None, (
                    result.stderr.strip()
                    or f"pdftotext avsluttet med {result.returncode}"
                )[:500]
            return result.stdout.strip(), None
        return None, "Ingen tekstuttrekker for MIME-typen"
    except (OSError, KeyError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        return None, f"Tekstuttrekk feilet: {type(exc).__name__}: {exc}"[:500]
