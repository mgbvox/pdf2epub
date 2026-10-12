"""End-to-end tests on synthetic PDFs with known structure."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pymupdf
import pytest
from typer.testing import CliRunner

from pdf2epub import ConvertOptions, analyze, convert
from pdf2epub.cli import app
from pdf2epub.layout import Region

from .conftest import LOREM, PdfBuilder, body_text, epub_texts

LEFT, RIGHT, GUTTER_L, GUTTER_R = 72, 540, 296, 316


def _two_column_pdf(tmp_path: Path) -> Path:
    b = PdfBuilder()
    b.text(150, 80, "A Synthetic Two Column Paper", size=18, font="hebo")
    y_l = b.paragraph(LEFT, 130, GUTTER_L - LEFT, "ALPHA " + LOREM + " ALPHAEND")
    b.paragraph(LEFT, y_l + 8, GUTTER_L - LEFT, "BRAVO " + LOREM + " BRAVOEND")
    y_r = b.paragraph(GUTTER_R, 130, RIGHT - GUTTER_R, "CHARLIE " + LOREM + " CHARLIEEND")
    b.paragraph(GUTTER_R, y_r + 8, RIGHT - GUTTER_R, "DELTA " + LOREM + " DELTAEND")
    return b.save(tmp_path / "twocol.pdf")


def _paragraphs(xhtml: str) -> list[str]:
    paras = re.findall(r"<p[^>]*>(.*?)</p>", xhtml, flags=re.S)
    return [re.sub(r"<[^>]+>", "", p) for p in paras]


def test_two_columns_become_one_flow(tmp_path):
    pdf = _two_column_pdf(tmp_path)
    out = tmp_path / "twocol.epub"
    rep = convert(pdf, out, ConvertOptions(cover=False))
    assert out.exists() and rep.pages == 1
    text = body_text(out)
    order = [text.index(w) for w in ("ALPHA ", "BRAVO ", "CHARLIE ", "DELTA ")]
    assert order == sorted(order), "columns must be read left column first, then right"
    paras = _paragraphs(text)
    for start, end in (("ALPHA ", "ALPHAEND"), ("BRAVO ", "BRAVOEND"), ("CHARLIE ", "CHARLIEEND"), ("DELTA ", "DELTAEND")):
        holder = [p for p in paras if start in p]
        assert len(holder) == 1 and end in holder[0], f"paragraph {start!r} was split or merged wrongly"
    assert "A Synthetic Two Column Paper" in text


def test_figure_table_and_equation_regions(tmp_path):
    b = PdfBuilder()
    y = b.paragraph(LEFT, 80, RIGHT - LEFT, "Intro paragraph. " + LOREM)
    # --- a vector chart
    top = y + 10
    b.line((120, top), (120, top + 140))
    b.line((120, top + 140), (480, top + 140))
    for i, h in enumerate([40, 90, 60, 120, 75]):
        b.rect((140 + 65 * i, top + 140 - h, 175 + 65 * i, top + 140), fill=(0.2, 0.4, 0.8), width=0.4)
    for i in range(5):
        b.text(143 + 65 * i, top + 150, f"lab{i}", size=7, font="helv")
    b.page.draw_bezier((120, top + 100), (250, top), (350, top + 130), (480, top + 20), color=(0.9, 0.1, 0.1))
    b.text(160, top + 172, "Figure 1: A synthetic bar chart with a curve.", size=10)
    y = b.paragraph(LEFT, top + 200, RIGHT - LEFT, "Middle paragraph. " + LOREM)
    # --- a booktabs table
    b.text(220, y + 12, "Table 1: Synthetic results.", size=10)
    t0 = y + 24
    b.line((150, t0), (460, t0), width=0.8)
    for j, h in enumerate(["Model", "Accuracy", "Loss"]):
        b.text(160 + 110 * j, t0 + 12, h, size=10)
    b.line((150, t0 + 16), (460, t0 + 16), width=0.4)
    for i, row in enumerate([("Base", "71.2", "0.93"), ("Large", "78.4", "0.71")]):
        for j, cell in enumerate(row):
            b.text(160 + 110 * j, t0 + 30 + 12 * i, cell, size=10)
    b.line((150, t0 + 50), (460, t0 + 50), width=0.8)
    # --- a display equation (Symbol font) with a number
    y = b.paragraph(LEFT, t0 + 72, RIGHT - LEFT, "We define the quantity as follows:")
    b.text(270, y + 6, "a + b = g", size=10, font="symb")
    b.text(520, y + 6, "(1)", size=10)
    b.paragraph(LEFT, y + 26, RIGHT - LEFT, "where the symbols are as above. " + LOREM)
    pdf = b.save(tmp_path / "regions.pdf")

    doc, pages, stats, layouts = analyze(pdf)
    kinds = {}
    for it in layouts[0].items:
        if isinstance(it, Region):
            kinds[it.kind] = it
    assert "figure" in kinds and kinds["figure"].label == "Figure 1"
    assert "table" in kinds and kinds["table"].label == "Table 1"
    assert "equation" in kinds and kinds["equation"].label == "(1)"

    out = tmp_path / "regions.epub"
    convert(pdf, out, ConvertOptions(cover=False))
    text = body_text(out)
    assert 'class="figure"' in text and "Figure 1: A synthetic bar chart" in text
    assert 'class="figure table"' in text and "Table 1: Synthetic results." in text
    assert 'class="eq"' in text
    # labels and cells live inside the faithful images, not in the reflowed text
    assert "lab3" not in re.sub(r'alt="[^"]*"', "", text)
    assert "78.4" not in re.sub(r'alt="[^"]*"', "", text)
    with zipfile.ZipFile(out) as z:
        images = [n for n in z.namelist() if n.startswith("OEBPS/images/")]
    assert len(images) >= 3


def test_footnotes_headings_and_hyphenation(tmp_path):
    b = PdfBuilder()
    b.text(72, 80, "1 Introduction", size=12, font="hebo")
    lines = [
        "Fine-tuned models are common. This sentence continues and ends with a multi-",
        "ple word, and a fine-",
        "tuned compound follows on this line to finish the paragraph nicely here.",
    ]
    y = 100
    for ln in lines:
        b.text(72, y, ln, size=10)
        y += 12
    x = b.text(72, y, "A sentence with a note", size=10)
    b.text(x, y - 3.5, "1", size=7)
    b.text(x + 4, y, " in it.", size=10)
    b.line((72, 690), (180, 690), width=0.4)
    b.text(84, 700, "1", size=6)
    b.text(88, 703, "This is the footnote text.", size=8)
    pdf = tmp_path / "notes.pdf"
    b.doc.set_toc([[1, "Introduction", 1]])
    b.save(pdf)

    out = tmp_path / "notes.epub"
    convert(pdf, out, ConvertOptions(cover=False))
    text = body_text(out)
    plain = re.sub(r"<[^>]+>", "", text)
    assert "multiple word" in plain
    assert "fine-tuned compound" in plain
    assert re.search(r"<h2 id=\"h\d+\"[^>]*>1 Introduction</h2>", text)
    assert 'epub:type="footnote"' in text and "This is the footnote text." in text
    assert 'epub:type="noteref"' in text
    nav = epub_texts(out)["OEBPS/nav.xhtml"]
    assert "Introduction" in nav


def test_epub_container_layout(tmp_path):
    pdf = _two_column_pdf(tmp_path)
    out = tmp_path / "book.epub"
    convert(pdf, out, ConvertOptions(title="My Title", authors=["A. Author"]))
    with zipfile.ZipFile(out) as z:
        infos = z.infolist()
        assert infos[0].filename == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
        names = z.namelist()
        assert "META-INF/container.xml" in names and "OEBPS/toc.ncx" in names
        opf = z.read("OEBPS/content.opf").decode()
    assert "<dc:title>My Title</dc:title>" in opf
    assert "A. Author" in opf and 'properties="cover-image"' in opf


def test_cli_convert_and_inspect(tmp_path):
    pdf = _two_column_pdf(tmp_path)
    runner = CliRunner()
    out = tmp_path / "cli.epub"
    res = runner.invoke(app, ["convert", str(pdf), "-o", str(out), "--quiet"])
    assert res.exit_code == 0, res.output
    assert out.exists()
    dbg = tmp_path / "dbg"
    res = runner.invoke(app, ["inspect", str(pdf), "--out", str(dbg)])
    assert res.exit_code == 0, res.output
    assert "2 column(s)" in res.output
    assert list(dbg.glob("page-*.png"))


@pytest.mark.skipif(not os.environ.get("EPUBCHECK_JAR") or not shutil.which("java"), reason="epubcheck not available")
def test_epubcheck_clean(tmp_path):
    pdf = _two_column_pdf(tmp_path)
    out = tmp_path / "check.epub"
    convert(pdf, out)
    res = subprocess.run(["java", "-jar", os.environ["EPUBCHECK_JAR"], str(out)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.skipif(not os.environ.get("PDF2EPUB_SAMPLE_PDF"), reason="set PDF2EPUB_SAMPLE_PDF to run on a real paper")
def test_real_paper(tmp_path):
    src = Path(os.environ["PDF2EPUB_SAMPLE_PDF"])
    rep = convert(src, tmp_path / "real.epub")
    assert rep.paragraphs > 10
    doc = pymupdf.open(str(src))
    assert rep.pages == len(doc)


def test_cover_from_key_figure_and_cli(tmp_path):
    import zipfile

    from pdf2epub.cover import candidates

    b = PdfBuilder()
    b.text(150, 80, "A Paper With One Figure", size=18, font="hebo")
    y = b.paragraph(LEFT, 120, RIGHT - LEFT, "Intro. " + LOREM)
    b.line((120, y + 10), (120, y + 150))
    b.line((120, y + 150), (480, y + 150))
    for i, h in enumerate([40, 90, 60]):
        b.rect((140 + 80 * i, y + 150 - h, 190 + 80 * i, y + 150), fill=(0.8, 0.2, 0.2), width=0.4)
    b.text(160, y + 175, "Figure 1: Overview of the method.", size=10)
    pdf = b.save(tmp_path / "fig.pdf")
    page_cover = tmp_path / "page.epub"
    fig_cover = tmp_path / "fig.epub"
    convert(pdf, page_cover, ConvertOptions(cover_figure="page"))
    convert(pdf, fig_cover, ConvertOptions(cover_figure="auto"))
    cands = candidates(fig_cover)
    assert len(cands) == 1 and cands[0].label == "Figure 1"
    with zipfile.ZipFile(page_cover) as a, zipfile.ZipFile(fig_cover) as c:
        assert a.read("OEBPS/images/cover.jpg") != c.read("OEBPS/images/cover.jpg")
        assert 'properties="cover-image"' in c.read("OEBPS/content.opf").decode()
    runner = CliRunner()
    res = runner.invoke(app, ["figures", str(fig_cover), "--out", str(tmp_path / "figs")])
    assert res.exit_code == 0, res.output
    assert "Figure 1" in res.output and (tmp_path / "figs" / "figures.png").exists()
    res = runner.invoke(app, ["cover", str(page_cover), "--figure", "1", "--preview", str(tmp_path / "c.jpg")])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "c.jpg").read_bytes()[:3] == b"\xff\xd8\xff"
