"""Lazy physical PDF pages and bounded UTF-8 windows; no source or model access."""

import base64
import hashlib
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .core import FamiliekilderError
from .live_attachments import _executable, MAX_PAGES
from .private_state import ensure_private_directory, atomic_write_private_bytes

TEXT_WINDOW_BYTES = 32768
IMAGE_WINDOW_BYTES = 8 * 1024 * 1024  # Total base64 image payload per response.
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_PAGE_TEXT_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_WORK_BYTES = 128 * 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 65536
PROCESS_SECONDS = 30
WINDOW_SECONDS = 55
CACHE_FORMAT = 'pdf-original-v1'


def is_pdf(path, mime):
    if mime == 'application/pdf' or Path(path).suffix.lower() == '.pdf':
        return True
    with Path(path).open('rb') as stream:
        return stream.read(5) == b'%PDF-'


def cache_original(path):
    """Cache bounded original bytes, never the full set of extracted pages."""
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise FamiliekilderError('PDF-originalen overskrider filgrensen')
    return {'format': CACHE_FORMAT, 'mime': 'application/pdf',
            'original': base64.b64encode(raw).decode('ascii'),
            'sha256': hashlib.sha256(raw).hexdigest()}


def restore_original(data, path):
    try:
        encoded = data['original']
        if not isinstance(encoded, str) or len(encoded) > ((MAX_SOURCE_BYTES + 2) // 3) * 4:
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_SOURCE_BYTES or hashlib.sha256(raw).hexdigest() != data['sha256']:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise FamiliekilderError('Mellomlagret PDF-original kunne ikke valideres') from None
    atomic_write_private_bytes(path, raw)


def _disk_bytes(work):
    return sum(p.stat().st_size for p in work.iterdir() if p.is_file())


def _run_file(args, target, work, *, max_output=None, deadline=None):
    """Keep decoder output on disk and kill the process group on limit/failure."""
    target = Path(target)
    output_limit = MAX_PAGE_TEXT_BYTES if max_output is None else max_output
    try:
        with target.open('wb') as out, tempfile.TemporaryFile(dir=work) as err:
            target.chmod(0o600)
            process = subprocess.Popen(args, stdout=out, stderr=err,
                start_new_session=True, env={**os.environ, 'LC_ALL': 'C'}, umask=0o077)
            try:
                stop_at = time.monotonic() + PROCESS_SECONDS
                if deadline is not None:
                    stop_at = min(stop_at, deadline)
                while process.poll() is None:
                    if (time.monotonic() >= stop_at or target.stat().st_size > output_limit
                            or _disk_bytes(work) > MAX_WORK_BYTES
                            or os.fstat(err.fileno()).st_size > MAX_DIAGNOSTIC_BYTES):
                        raise FamiliekilderError('PDF-siden overskred behandlingsgrensen')
                    time.sleep(.01)
                if os.fstat(err.fileno()).st_size > MAX_DIAGNOSTIC_BYTES:
                    raise FamiliekilderError('PDF-diagnostikken overskrider leserens grense')
                err.seek(0)
                diagnostic = err.read(MAX_DIAGNOSTIC_BYTES)
                if process.returncode or b'Syntax Error' in diagnostic or b'Error:' in diagnostic:
                    raise FamiliekilderError('PDF-siden kunne ikke dekodes')
                if target.stat().st_size > output_limit or _disk_bytes(work) > MAX_WORK_BYTES:
                    raise FamiliekilderError('PDF-siden overskred behandlingsgrensen')
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    except (OSError, subprocess.SubprocessError):
        raise FamiliekilderError('PDF-behandling kunne ikke fullføres') from None


class PDFWindows:
    def __init__(self, source, work, content_sha256, *, deadline=None):
        self.source, self.work = Path(source), Path(work)
        self.content_sha256 = content_sha256
        ensure_private_directory(self.work)
        info = self.work / 'info.txt'
        _run_file([_executable('pdfinfo'), str(self.source)], info, self.work, max_output=65536, deadline=deadline)
        # pdfinfo output is independently bounded and normally much smaller.
        if info.stat().st_size > 65536:
            raise FamiliekilderError('PDF-metadata overskrider leserens grense')
        match = re.search(rb'^Pages:\s+(\d+)', info.read_bytes(), re.M)
        if not match or not 1 <= int(match[1]) <= MAX_PAGES:
            raise FamiliekilderError('Ugyldig eller for stort PDF-sidetall')
        self.page_count = int(match[1])
        self.pages, self.errors = {}, {}

    def _prepare(self, page, deadline=None):
        if type(page) is not int or not 1 <= page <= self.page_count:
            raise FamiliekilderError('Sidevinduet er utenfor vedlegget')
        if page in self.pages:
            return self.pages[page]
        if page in self.errors:
            return None
        text, image = self.work / f'{page}.txt', self.work / f'{page}.png'
        try:
            _run_file([_executable('pdftotext'), '-f', str(page), '-l', str(page),
                       '-r', '150', '-layout', str(self.source), '-'], text, self.work, deadline=deadline)
            _run_file([_executable('pdftoppm'), '-f', str(page), '-l', str(page),
                       '-singlefile', '-scale-to', '1800', '-png', str(self.source),
                       str(image.with_suffix(''))], self.work / f'{page}.render-log', self.work, deadline=deadline)
            image.chmod(0o600)
            if image.stat().st_size > MAX_IMAGE_BYTES or _disk_bytes(self.work) > MAX_WORK_BYTES:
                raise FamiliekilderError('PDF-siden overskrider bilde- eller arbeidsgrensen')
            offsets, chunk_hashes = [0], []
            with text.open('rb') as stream:
                while raw := stream.read(TEXT_WINDOW_BYTES):
                    trim = 0
                    if stream.tell() < text.stat().st_size:
                        # Avoid cutting a UTF-8 codepoint; move only trailing bytes back.
                        for trim in range(4):
                            try:
                                raw[:len(raw)-trim if trim else None].decode('utf-8')
                                break
                            except UnicodeDecodeError:
                                continue
                        else:
                            raise FamiliekilderError('PDF-tekst kunne ikke dekodes som UTF-8')
                        if trim:
                            stream.seek(-trim, 1)
                    else:
                        raw.decode('utf-8')
                    valid = raw[:-trim] if trim else raw
                    chunk_hashes.append(hashlib.sha256(valid).hexdigest())
                    offsets.append(stream.tell())
            if len(offsets) == 1:
                offsets.append(0)
                chunk_hashes.append(hashlib.sha256(b'').hexdigest())
            with image.open('rb') as stream:
                image_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
            self.pages[page] = {'text': text, 'image': image, 'offsets': offsets,
                                'chunk_hashes': chunk_hashes, 'image_sha256': image_sha256}
            return self.pages[page]
        except (FamiliekilderError, OSError, UnicodeError):
            self.errors[page] = 'Siden kunne ikke leses innenfor leserens grenser'
            for path in self.work.glob(f'{page}.*'):
                path.unlink(missing_ok=True)
            return None

    def window(self, page, chunk, *, deadline=None):
        data = self._prepare(page, deadline)
        if data is None:
            if chunk != 0:
                raise FamiliekilderError('En uleselig side har ingen tekstbit')
            return {'page': page, 'chunk': 0, 'text': '', 'readable': False,
                    'error': self.errors[page], 'image_included': False}, None
        count = len(data['offsets']) - 1
        if type(chunk) is not int or not 0 <= chunk < count:
            raise FamiliekilderError('Tekstvinduet er utenfor siden')
        start, end = data['offsets'][chunk:chunk+2]
        try:
            with data['text'].open('rb') as stream:
                stream.seek(start)
                raw = stream.read(end-start)
            if len(raw) != end-start or hashlib.sha256(raw).hexdigest() != data['chunk_hashes'][chunk]:
                raise OSError('text window changed')
            text = raw.decode('utf-8')
        except (OSError, UnicodeError):
            self.errors[page] = 'Det forberedte tekstvinduet kunne ikke valideres'
            self.pages.pop(page, None)
            return {'page': page, 'chunk': 0, 'text': '', 'readable': False,
                    'error': self.errors[page], 'image_included': False}, None
        return {'page': page, 'chunk': chunk, 'chunk_count': count,
                'text': text, 'byte_start': start, 'byte_end': end,
                'readable': True, 'image_included': chunk == 0}, data['image'] if chunk == 0 else None

    def original_matches(self):
        try:
            if self.source.stat().st_size > MAX_SOURCE_BYTES:
                return False
            with self.source.open('rb') as stream:
                return hashlib.file_digest(stream, 'sha256').hexdigest() == self.content_sha256
        except OSError:
            return False

    def coverage(self, delivered):
        complete = []
        for page, data in self.pages.items():
            if {(page, c) for c in range(len(data['offsets']) - 1)} <= delivered:
                complete.append(page)
        return {'complete_pages': sorted(complete),
                'unread_pages': [p for p in range(1, self.page_count+1) if p not in complete],
                'unreadable_pages': sorted(self.errors),
                'complete': len(complete) == self.page_count and not self.errors}
