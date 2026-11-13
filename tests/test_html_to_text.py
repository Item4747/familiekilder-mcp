"""Pins the shared HTML converters to the per-provider copies they replaced.

Stored content hashes are built from this text, so any difference would show
unchanged provider messages as changed.
"""

import html
import re
import unittest
from html.parser import HTMLParser

from familiekilder.core import html_to_text, html_to_text_unescaped


class _Parts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def _old_spond_skolemelding(value):
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if "<" not in candidate and "&" not in candidate:
        return candidate
    parser = _Parts()
    try:
        parser.feed(candidate)
        parser.close()
    except Exception:
        return html.unescape(re.sub(r"<[^>]+>", " ", candidate)).strip() or None
    return "\n".join(parser.parts).strip() or html.unescape(candidate).strip() or None


def _old_vigilo(value):
    if not isinstance(value, str):
        return ""
    parser = _Parts()
    try:
        parser.feed(value)
    except Exception:
        return html.unescape(value).strip()
    return html.unescape("\n".join(parser.parts)).strip()


def _old_ist_home(value):
    parser = _Parts()
    parser.feed(value or "")
    return html.unescape("\n".join(parser.parts)).strip()


SAMPLES = [
    None,
    "",
    "   ",
    "Bare tekst",
    "  Tom & Jerry  ",
    "<p>Tom &amp;amp; Jerry</p>",
    "<p><br></p>",
    "<div>Hei<br>på<b> deg</b></div>",
    "a < b og c > d",
    "<p>Uavsluttet &amp",
    "Trailing &nbsp",
    "<script>x()</script><p>Synlig</p>",
    "<p>Æ &oslash; å &#229;</p>",
    "<![CDATA[x]]><p>y</p>",
]


class HtmlToTextCompatibilityTest(unittest.TestCase):
    def test_matches_spond_and_skolemelding_copies(self) -> None:
        for sample in SAMPLES + [123, {"x": 1}]:
            with self.subTest(sample=sample):
                self.assertEqual(html_to_text(sample), _old_spond_skolemelding(sample))

    def test_matches_vigilo_copy(self) -> None:
        for sample in SAMPLES + [123, {"x": 1}]:
            with self.subTest(sample=sample):
                self.assertEqual(html_to_text_unescaped(sample), _old_vigilo(sample))

    def test_matches_ist_home_copy(self) -> None:
        for sample in SAMPLES:
            with self.subTest(sample=sample):
                self.assertEqual(html_to_text_unescaped(sample), _old_ist_home(sample))

    def test_the_two_converters_differ_where_hashes_depend_on_it(self) -> None:
        self.assertEqual(html_to_text_unescaped("<p>Tom &amp;amp; Jerry</p>"), "Tom & Jerry")
        self.assertEqual(html_to_text("<p>Tom &amp;amp; Jerry</p>"), "Tom &amp; Jerry")
        self.assertEqual(html_to_text_unescaped("<p><br></p>"), "")


if __name__ == "__main__":
    unittest.main()
