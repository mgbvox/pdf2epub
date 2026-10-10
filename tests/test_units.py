"""Unit tests for the low-level helpers."""

from __future__ import annotations

import numpy as np
import pymupdf

from pdf2epub import geometry as G
from pdf2epub.cli import _parse_pages
from pdf2epub.extract import build_rows, extract_fragments, split_row
from pdf2epub.fonts import font_info, normalise_font_name
from pdf2epub.render import split_wide, squeeze_gap


def test_geometry_helpers():
    a = (0, 0, 10, 10)
    b = (5, 5, 20, 20)
    assert G.union([a, b]) == (0, 0, 20, 20)
    assert G.overlaps(a, b)
    assert G.intersection_area(a, b) == 25
    assert G.fraction_inside((6, 6, 8, 8), b) == 1.0
    assert G.h_gap((0, 0, 1, 1), (3, 0, 4, 1)) == 2
    assert G.contains(b, (6, 6, 7, 7))


def test_font_classification():
    assert normalise_font_name("ABCDEF+CMMI10") == "CMMI10"
    assert font_info("CMMI10").math_only
    assert font_info("CMSY7").math_only
    assert font_info("LMMathItalic10-Regular").math_only
    assert not font_info("NimbusRomNo9L-Regu").math_only
    assert font_info("NimbusRomNo9L-Medi").bold
    assert font_info("NimbusRomNo9L-ReguItal").italic
    assert font_info("CMTT10").mono and font_info("CMTT10").tex_family
    assert font_info("CMR10").tex_family and not font_info("CMR10").math_only


def test_subscript_attached_to_row():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "The value x", fontsize=10, fontname="tiro")
    x = 72 + pymupdf.get_text_length("The value x", fontname="tiro", fontsize=10)
    page.insert_text((x, 102.5), "i", fontsize=7, fontname="tiro")  # subscript
    page.insert_text((x + 3, 96), "2", fontsize=7, fontname="tiro")  # superscript
    page.insert_text((x + 8, 100), " is positive.", fontsize=10, fontname="tiro")
    frags, _ = extract_fragments(page)
    rows = build_rows(frags, 0)
    assert len(rows) == 1
    levels = {g.c: g.level for g in rows[0].glyphs}
    assert levels["i"] == -1 or levels["2"] == 1
    assert rows[0].size == 10


def test_split_row_on_column_gutter():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "left column text here", fontsize=10, fontname="tiro")
    page.insert_text((320, 100), "right column text here", fontsize=10, fontname="tiro")
    frags, _ = extract_fragments(page)
    rows = build_rows(frags, 0)
    assert len(rows) == 1
    segs = split_row(rows[0])
    assert [s.text for s in segs] == ["left column text here", "right column text here"]


def test_squeeze_and_split_images():
    arr = np.full((20, 400, 3), 255, dtype=np.uint8)
    arr[5:15, 10:60] = 0  # equation body
    arr[5:15, 300:320] = 0  # equation number far to the right
    out = squeeze_gap(arr, min_gap_px=100, keep_px=30)
    assert out.shape[1] == 400 - (300 - 60) + 30
    wide = np.full((20, 400, 3), 255, dtype=np.uint8)
    wide[5:15, 0:150] = 0
    wide[5:15, 250:400] = 0
    parts = split_wide(wide, max_px=200, min_gap_px=50, min_part_px=60)
    assert len(parts) == 2


def test_parse_pages():
    assert _parse_pages("1-3,5") == [0, 1, 2, 4]
    assert _parse_pages(None) is None
