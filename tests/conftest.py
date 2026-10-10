"""Helpers that synthesise small PDFs with known structure for the tests."""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pymupdf
import pytest

LOREM = (
    "Reflowable books adapt their text to the size of the screen so that reading on a phone "
    "remains comfortable. Scientific papers are usually typeset for paper and therefore use "
    "fixed pages with narrow columns, small fonts and floating figures. Converting them well "
    "requires recovering the reading order, joining lines into paragraphs and keeping every "
    "formula and diagram exactly as the authors drew it."
)


class PdfBuilder:
    """Tiny layout engine: greedy line wrapping with the base-14 fonts."""

    def __init__(self, width: float = 612, height: float = 792):
        self.doc = pymupdf.open()
        self.width = width
        self.height = height
        self.page = None
        self.new_page()

    def new_page(self) -> pymupdf.Page:
        self.page = self.doc.new_page(width=self.width, height=self.height)
        return self.page

    def text(self, x: float, y: float, s: str, size: float = 10, font: str = "tiro") -> float:
        """Write one line with its baseline at y; returns the x after the text."""
        self.page.insert_text((x, y), s, fontsize=size, fontname=font)
        return x + pymupdf.get_text_length(s, fontname=font, fontsize=size)

    def paragraph(self, x: float, y: float, width: float, s: str, size: float = 10, leading: float = 12,
                  font: str = "tiro", indent: float = 0.0) -> float:
        """Greedy-wrap ``s`` into lines no wider than ``width``; returns the next baseline."""
        words = s.split()
        line: list[str] = []
        first = True
        while words:
            avail = width - (indent if first else 0)
            line = [words.pop(0)]
            while words and pymupdf.get_text_length(" ".join(line + [words[0]]), fontname=font, fontsize=size) <= avail:
                line.append(words.pop(0))
            self.text(x + (indent if first else 0), y, " ".join(line), size, font)
            first = False
            y += leading
        return y

    def rect(self, r, color=(0, 0, 0), fill=None, width: float = 0.8) -> None:
        self.page.draw_rect(pymupdf.Rect(*r), color=color, fill=fill, width=width)

    def line(self, p1, p2, width: float = 0.6, color=(0, 0, 0)) -> None:
        self.page.draw_line(pymupdf.Point(*p1), pymupdf.Point(*p2), color=color, width=width)

    def save(self, path: Path) -> Path:
        self.doc.save(str(path))
        return path


@pytest.fixture
def builder() -> PdfBuilder:
    return PdfBuilder()


def epub_texts(path: Path) -> dict[str, str]:
    """Return {name: xhtml} for all chapter files, asserting they are well-formed XML."""
    out = {}
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            if name.endswith(".xhtml"):
                data = z.read(name).decode("utf-8")
                ET.fromstring(data.encode("utf-8"))  # raises on malformed XML
                out[name] = data
    return out


def body_text(path: Path) -> str:
    texts = epub_texts(path)
    return "\n".join(v for k, v in texts.items() if "/text/" in k)
