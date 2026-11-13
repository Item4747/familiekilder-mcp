"""Disposable document rendering for MCP attachment requests."""

from pathlib import Path
import os
import re
import time
import shutil
import subprocess

MAX_PAGES = 100
TIMEOUT = 60
MAX_ARCHIVE_TEXT = 2 * 1024 * 1024
TEXT_PAGE_SIZE = 16000
# Rendered pages are durable cache data; changes to their contract need a new key.
# 7: a multi-picture JPEG (MPO) is one page, its primary picture.
LEGACY_RENDER_VERSION = 5
RENDER_VERSION = 7
from .core import FamiliekilderError, extract_text
from .private_state import ensure_private_directory, atomic_write_private_bytes


def _run(args):
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=TIMEOUT,
            env={**os.environ, "LC_ALL": "C"},
            umask=0o077,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FamiliekilderError(
            f"{Path(args[0]).name}: {type(exc).__name__}"
        ) from None
    if result.returncode:
        # Tool diagnostics may contain untrusted source content. Keep errors structural.
        raise FamiliekilderError(
            f"{Path(args[0]).name} avsluttet med {result.returncode}"
        )
    return result.stdout.decode("utf-8", errors="replace")


def _executable(name):
    found = shutil.which(name)
    if not found and name == "soffice":
        candidate = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        found = str(candidate) if candidate.is_file() else None
    if not found:
        raise FamiliekilderError(f"{name} er ikke installert")
    return found


def _append_text_pages(pages, text, *, label=''):
    """Keep every returned text page bounded, including archive member labels."""
    capacity = TEXT_PAGE_SIZE - len(label)
    if capacity <= 0:
        raise FamiliekilderError('Vedleggets tekst overskrider leserens grenser')
    total = sum(len(page['text'].encode('utf-8')) for page in pages)
    for offset in range(0, max(1, len(text)), capacity):
        content = label + text[offset:offset + capacity]
        total += len(content.encode('utf-8'))
        if len(pages) >= MAX_PAGES or total > MAX_ARCHIVE_TEXT:
            raise FamiliekilderError('Vedleggets tekst overskrider leserens grenser')
        pages.append({'page': len(pages) + 1, 'text': content, 'image_path': None})


def _bounded_text_bytes(source):
    with source.open('rb') as stream:
        data = stream.read(MAX_ARCHIVE_TEXT + 1)
    if len(data) > MAX_ARCHIVE_TEXT:
        raise FamiliekilderError('Vedleggets tekst overskrider leserens grenser')
    return data


def archive_text_pages(source):
    """Read bounded text/XML reports without extraction or interpreting XML.

    Unsupported members fail the whole read: a file listing is not evidence
    that attachment contents were read. No nested archives, passwords, external
    entities, filesystem paths, executable content or partial-success receipts.
    """
    import stat
    import zipfile
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            files = [member for member in members if not member.is_dir()]
            if (not files or len(members) > 20
                    or sum(member.file_size for member in files) > MAX_ARCHIVE_TEXT):
                raise ValueError()
            pages, total = [], 0
            for member in files:
                path = Path(member.filename)
                mode = member.external_attr >> 16
                if (path.is_absolute() or '..' in path.parts or '\\' in member.filename
                        or stat.S_ISLNK(mode) or member.flag_bits & 1
                        or path.suffix.lower() not in {'.xml', '.txt', '.csv', '.json'}):
                    raise ValueError()
                with archive.open(member) as stream:
                    data = stream.read(MAX_ARCHIVE_TEXT - total + 1)
                total += len(data)
                if total > MAX_ARCHIVE_TEXT or len(data) != member.file_size:
                    raise ValueError()
                text = data.decode('utf-8-sig')
                if '\x00' in text:
                    raise ValueError()
                # Empty files are still read; the explicit label retains their
                # existence. Text is untrusted data, never parsed as XML/code.
                _append_text_pages(pages, text, label=f'Fil i ZIP: {path.name}\n')
            return pages
    except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
        raise FamiliekilderError('ZIP-vedlegget krever komplette tekst/XML-filer innenfor leserens grenser') from None


def render(source, mime, work):
    source = Path(source)
    ensure_private_directory(work)
    suffix = source.suffix.lower()
    if suffix == '.gz' or mime in {'application/gzip', 'application/x-gzip'}:
        import gzip
        try:
            with gzip.open(source, 'rb') as compressed:
                data = compressed.read(MAX_ARCHIVE_TEXT + 1)
            if len(data) > MAX_ARCHIVE_TEXT or b'\x00' in data:
                raise ValueError()
            text = data.decode('utf-8-sig')
        except (OSError, EOFError, ValueError):
            raise FamiliekilderError('GZIP-vedlegget må inneholde komplett tekst innenfor leserens grenser') from None
        pages = []
        _append_text_pages(pages, text)
        return pages
    if mime in {'application/png', 'application/octet-stream'}:
        # Some mail senders label inline pictures as applications and omit the
        # extension. Only recognized image signatures select an image decoder.
        with source.open('rb') as file:
            signature = file.read(12)
        if signature.startswith(b'\x89PNG\r\n\x1a\n'):
            mime = 'image/png'
        elif signature.startswith(b'\xff\xd8\xff'):
            mime = 'image/jpeg'
        elif signature.startswith((b'GIF87a', b'GIF89a')):
            mime = 'image/gif'
        elif signature.startswith(b'RIFF') and signature[8:12] == b'WEBP':
            mime = 'image/webp'
    if suffix == '.zip' or mime in {'application/zip', 'application/x-zip-compressed'}:
        return archive_text_pages(source)
    document = source
    if suffix in {".doc", ".docx"}:
        profile = work / "profile"
        ensure_private_directory(profile / "user")
        atomic_write_private_bytes(
            profile / "user/registrymodifications.xcu",
            b"""<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item></oor:items>""",
        )
        _run(
            [
                _executable("soffice"),
                f"-env:UserInstallation={profile.as_uri()}",
                "--headless",
                "--norestore",
                "--convert-to",
                "pdf:writer_pdf_Export",
                "--outdir",
                str(work),
                str(source),
            ]
        )
        document = work / (source.stem + ".pdf")
        mime = "application/pdf"
        if not document.is_file():
            raise FamiliekilderError("Dokumentkonvertering feilet")
    pages = []
    started = time.monotonic()
    if mime == "application/pdf" or suffix == ".pdf":
        match = re.search(
            r"^Pages:\s+(\d+)", _run([_executable("pdfinfo"), str(document)]), re.M
        )
        if not match or not 1 <= int(match[1]) <= MAX_PAGES:
            raise FamiliekilderError("Ugyldig eller for stort sidetall")
        for n in range(1, int(match[1]) + 1):
            if time.monotonic() - started > 300:
                raise FamiliekilderError("Vedlegget overskred behandlingstiden")
            prefix = work / f"page-{n}"
            _run(
                [
                    _executable("pdftoppm"),
                    "-f",
                    str(n),
                    "-l",
                    str(n),
                    "-scale-to",
                    "1800",
                    "-singlefile",
                    "-png",
                    str(document),
                    str(prefix),
                ]
            )
            text = _run(
                [
                    _executable("pdftotext"),
                    "-f",
                    str(n),
                    "-l",
                    str(n),
                    "-layout",
                    str(document),
                    "-",
                ]
            )
            pages.append(
                {"page": n, "text": text, "image_path": str(prefix.with_suffix(".png"))}
            )
    elif mime in {"image/jpeg", "image/png", "image/gif", "image/webp"} or suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        from PIL import Image

        with Image.open(source) as img:
            frames = getattr(img, 'n_frames', 1)
            if img.format == 'MPO':
                # Phone cameras store an HDR gain map, depth map or preview as
                # extra MPF pictures in an ordinary photo. Only the primary
                # picture is the page; the rest are not separate pages.
                frames = 1
            if not 1 <= frames <= MAX_PAGES:
                raise FamiliekilderError('Bildet har for mange animasjonsbilder')
            for frame in range(frames):
                img.seek(frame)
                picture = img.convert('RGB')
                picture.thumbnail((1800, 1800))
                target = work / f'image-{frame + 1}.png'
                picture.save(target)
                pages.append({'page': frame + 1, 'text': '', 'image_path': str(target)})
    elif mime in {'message/global-headers', 'text/rfc822-headers', 'message/delivery-status',
                  'message/global-delivery-status'}:
        data = _bounded_text_bytes(source)
        try:
            text = data.decode('utf-8-sig')
        except UnicodeError:
            raise FamiliekilderError('Meldingsvedlegget kunne ikke leses som komplett tekst') from None
        if '\x00' in text:
            raise FamiliekilderError('Meldingsvedlegget inneholder binærdata')
        _append_text_pages(pages, text)
    else:
        # Bound the source before extract_text reads TXT/HTML or another text
        # format in full. UTF-8 output and page count are checked separately.
        _bounded_text_bytes(source)
        text, error = extract_text(source, mime)
        if error or not text:
            raise FamiliekilderError("Vedlegget kunne ikke leses som tekst")
        _append_text_pages(pages, text)
    for directory, _, files in os.walk(work):
        Path(directory).chmod(0o700)
        for f in files:
            (Path(directory) / f).chmod(0o600)
    return pages
