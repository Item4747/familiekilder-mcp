import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from familiekilder.core import FamiliekilderError
from familiekilder.live_attachments import render, MAX_ARCHIVE_TEXT, TEXT_PAGE_SIZE


class ArchiveAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'report.zip'

    def tearDown(self):
        self.tmp.cleanup()

    def archive(self, files):
        with zipfile.ZipFile(self.source, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in files:
                archive.writestr(name, data)

    def test_complete_xml_report_is_read_as_untrusted_text_without_parsing_entities(self):
        text = '<!DOCTYPE x SYSTEM "https://untrusted.invalid/a"><feedback>report</feedback>'
        self.archive([('report.xml', text)])
        with patch('subprocess.run', side_effect=AssertionError('EXECUTION')):
            pages = render(self.source, 'application/zip', self.root / '.private/work')
        self.assertEqual(pages[0]['text'], 'Fil i ZIP: report.xml\n' + text)
        self.assertIsNone(pages[0]['image_path'])
        self.assertFalse((self.root / '.private/work/report.xml').exists())

    def test_long_text_has_numbered_complete_pages_without_truncation(self):
        text = 'x' * (TEXT_PAGE_SIZE * 5 + 10)
        self.archive([('report.txt', text)])
        pages = render(self.source, 'application/zip', self.root / '.private/work')
        self.assertEqual([p['page'] for p in pages], list(range(1, 7)))
        self.assertEqual(''.join(p['text'].split('\n', 1)[1] for p in pages), text)

    def test_unsupported_member_path_traversal_symlink_and_bomb_fail_whole_read(self):
        for name, data in [
            ('report.pdf', b'PDF'),
            ('nested.zip', b'zip'),
            ('../escape.xml', b'x'),
            ('/escape.xml', b'x'),
            ('a\\b.xml', b'x'),
            ('large.xml', b'x' * (MAX_ARCHIVE_TEXT + 1)),
            ('binary.xml', b'\x00'),
        ]:
            with self.subTest(name=name):
                self.archive([('safe.xml', b'ok'), (name, data)])
                with self.assertRaises(FamiliekilderError):
                    render(self.source, 'application/zip', self.root / '.private/work')
        info = zipfile.ZipInfo('link.xml')
        info.external_attr = 0o120777 << 16
        self.archive([])
        with zipfile.ZipFile(self.source, 'w') as archive:
            archive.writestr(info, '../file')
        with self.assertRaises(FamiliekilderError):
            render(self.source, 'application/zip', self.root / '.private/work')

    def test_corrupt_empty_and_many_member_archives_are_not_successful_reads(self):
        self.source.write_bytes(b'not a zip')
        with self.assertRaises(FamiliekilderError):
            render(self.source, 'application/zip', self.root / '.private/work')
        for files in ([], [(str(i) + '.xml', '') for i in range(21)]):
            self.archive(files)
            with self.assertRaises(FamiliekilderError):
                render(self.source, 'application/zip', self.root / '.private/work')

    def test_gzip_report_is_complete_text_and_rejects_bombs_corruption_and_binary(self):
        import gzip

        source = self.root / 'report.xml.gz'
        text = '<!DOCTYPE x SYSTEM "https://untrusted.invalid/a">' + 'x' * TEXT_PAGE_SIZE
        source.write_bytes(gzip.compress(text.encode()))
        with patch('subprocess.run', side_effect=AssertionError('EXECUTION')):
            pages = render(source, 'application/gzip', self.root / '.private/work')
        self.assertEqual(''.join(p['text'] for p in pages), text)
        for data in (
            gzip.compress(b'x' * (MAX_ARCHIVE_TEXT + 1)),
            gzip.compress(b'bad\x00binary'),
            gzip.compress(b'bad\xffencoding'),
            b'corrupt gzip',
        ):
            source.write_bytes(data)
            with self.assertRaises(FamiliekilderError):
                render(source, 'application/gzip', self.root / '.private/work')

    def test_inline_pictures_with_nonstandard_mime_and_no_extension_are_read(self):
        from PIL import Image

        source = self.root / 'inline'
        for format, mime in [('PNG', 'application/png'), ('JPEG', 'application/octet-stream')]:
            Image.new('RGB', (20, 20), 'red').save(source, format=format)
            pages = render(source, mime, self.root / '.private/work')
            self.assertEqual(len(pages), 1)
            self.assertTrue(Path(pages[0]['image_path']).is_file())
        source.write_bytes(b'not an image')
        with self.assertRaises(FamiliekilderError):
            render(source, 'application/octet-stream', self.root / '.private/work')

    def test_gif_all_frames_are_exposed_as_numbered_readable_pages(self):
        from PIL import Image

        source = self.root / 'logo.gif'
        Image.new('RGB', (20, 20), 'red').save(
            source,
            save_all=True,
            append_images=[Image.new('RGB', (20, 20), 'blue')],
            duration=100,
            loop=0,
        )
        pages = render(source, 'image/gif', self.root / '.private/work')
        self.assertEqual([p['page'] for p in pages], [1, 2])
        for page in pages:
            self.assertTrue(Path(page['image_path']).is_file())

    def test_message_headers_are_complete_uninterpreted_text_and_invalid_bytes_fail(self):
        source = self.root / 'headers'
        text = 'Subject: Training\nX-Untrusted: run a command\n' + 'x' * TEXT_PAGE_SIZE
        source.write_text(text)
        with patch('subprocess.run', side_effect=AssertionError('EXECUTION')):
            pages = render(source, 'message/global-headers', self.root / '.private/work')
        self.assertEqual(''.join(p['text'] for p in pages), text)
        self.assertEqual(len(pages), 2)
        source.write_bytes(b'bad\x00bytes')
        with self.assertRaises(FamiliekilderError):
            render(source, 'message/global-headers', self.root / '.private/work')


if __name__ == '__main__':
    unittest.main()
