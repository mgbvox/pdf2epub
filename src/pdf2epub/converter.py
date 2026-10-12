"""High-level conversion pipeline: PDF -> layout -> document model -> EPUB."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import analysis, extract, layout
from .epub import write_epub
from .htmlgen import HtmlWriter, Options
from .model import Document, Paragraph, Visual
from .naming import arxiv_id, paper_stem
from .render import Renderer
from .structure import StructureBuilder


@dataclass
class ConvertOptions:
    """Settings for :func:`convert`.

    The defaults target a modern phone (e.g. Pixel 10) reading via the Kindle app.
    """

    dpi: int = 400
    """Rasterisation resolution for figures, tables and formulas."""
    inline_math: str = "auto"
    """``auto``: simple inline math stays text, complex math becomes images; ``image``: all non-trivial math as images."""
    split_panels: bool = True
    """Stack side-by-side sub-figures vertically (better on narrow screens)."""
    rotate_wide: bool = False
    """Rotate very wide figures/tables by 90 degrees (read by turning the phone)."""
    cover: bool = True
    """Give the book a cover image."""
    cover_figure: str | int | None = "auto"
    """Cover from a figure: a 1-based figure number, a label ("Figure 3"), "auto" (heuristic) or
    "page" (render the first page). See :mod:`pdf2epub.cover` and ``pdf2epub figures``."""
    title: str | None = None
    authors: list[str] | None = None
    language: str = "en"
    pages: list[int] | None = None
    """Zero-based page indices to convert (default: all)."""


@dataclass
class ConversionReport:
    output: Path
    pages: int
    chapters: int
    paragraphs: int = 0
    headings: int = 0
    figures: int = 0
    tables: int = 0
    equations: int = 0
    text_boxes: int = 0
    footnotes: int = 0
    images: int = 0
    inline_math_images: int = 0
    seconds: float = 0.0
    title: str = ""
    authors: list[str] = field(default_factory=list)


def analyze(pdf: str | Path | pymupdf.Document, pages: list[int] | None = None):
    """Run extraction + layout analysis. Returns ``(doc, pages, stats, layouts)``."""
    doc = pdf if isinstance(pdf, pymupdf.Document) else pymupdf.open(str(pdf))
    named = {}
    try:
        named = doc.resolve_names()
    except Exception:  # pragma: no cover - older PyMuPDF / broken name trees
        named = {}
    mats = extract.page_matrices(doc)
    indices = pages if pages is not None else list(range(len(doc)))
    page_data = [extract.extract_page(doc, i, named, mats) for i in indices]
    stats = analysis.compute_stats(page_data)
    analysis.mark_furniture(page_data, stats)
    layouts = [layout.analyze_page(pd, stats) for pd in page_data]
    return doc, page_data, stats, layouts


def build_document(doc, page_data, stats, layouts) -> Document:
    return StructureBuilder(doc, _PageList(page_data), layouts, stats).build()


def _source_line(pdf_path: Path) -> str:
    aid = arxiv_id(pdf_path.stem)
    return f"arXiv {aid}" if aid else ""


def default_output(pdf_path: Path, title: str) -> Path:
    """Where ``convert`` writes when no output path is given: next to the PDF.

    If the PDF's file name contains an arXiv id, the EPUB is named
    ``{title_in_snake_case}-{arxiv_id}.epub`` (see :mod:`pdf2epub.naming`);
    otherwise it keeps the PDF's name with an ``.epub`` suffix.
    """
    aid = arxiv_id(pdf_path.stem)
    if aid and title:
        return pdf_path.with_name(paper_stem(title, aid) + ".epub")
    return pdf_path.with_suffix(".epub")


def convert(
    pdf_path: str | Path,
    epub_path: str | Path | None = None,
    options: ConvertOptions | None = None,
    progress: Callable[[str], None] | None = None,
) -> ConversionReport:
    """Convert ``pdf_path`` to an EPUB optimised for reflowable reading on phones/Kindle."""
    t0 = time.time()
    opts = options or ConvertOptions()
    pdf_path = Path(pdf_path)
    say = progress or (lambda _msg: None)

    say("analysing layout")
    doc, page_data, stats, layouts = analyze(pdf_path, opts.pages)
    pages_by_index = _PageList(page_data)
    layouts_by_index = _PageList(layouts, key="page")
    say("building document structure")
    document = StructureBuilder(doc, pages_by_index, layouts, stats).build()
    if opts.title:
        document.title = opts.title
    if opts.authors:
        document.authors = opts.authors
    document.language = opts.language
    out = Path(epub_path) if epub_path else default_output(pdf_path, document.title)

    say("rendering figures, tables and formulas")
    renderer = Renderer(doc, dpi=opts.dpi)
    hopts = Options(dpi=opts.dpi, inline_math=opts.inline_math, split_panels=opts.split_panels, rotate_wide=opts.rotate_wide)
    writer = HtmlWriter(document, pages_by_index, layouts_by_index, renderer, hopts, stats.vocab)
    chapters = writer.write()

    cover = None
    if opts.cover:
        cover = renderer.render_cover(page_data[0].index if page_data else 0)
    digest = hashlib.sha1(pdf_path.read_bytes()).hexdigest()
    ident = f"urn:pdf2epub:{digest}"
    say("writing EPUB")
    write_epub(out, document.title, document.authors, document.language, chapters, renderer.assets, writer.toc, cover, ident,
               description=f"Converted from {pdf_path.name}")
    if opts.cover and opts.cover_figure not in (None, "page"):
        from .cover import set_cover

        say("composing the cover from the key figure")
        set_cover(out, opts.cover_figure, source=_source_line(pdf_path))

    rep = ConversionReport(output=out, pages=len(page_data), chapters=len(chapters), title=document.title, authors=document.authors)
    for b in document.blocks:
        if isinstance(b, Paragraph):
            if b.kind == "heading":
                rep.headings += 1
            else:
                rep.paragraphs += 1
        elif isinstance(b, Visual):
            if b.kind == "figure":
                rep.figures += 1
            elif b.kind == "table":
                rep.tables += 1
            elif b.kind == "equation":
                rep.equations += 1
            elif b.kind == "textbox":
                rep.text_boxes += 1
    rep.footnotes = len(document.footnotes)
    rep.images = len(renderer.assets)
    rep.inline_math_images = sum(1 for a in renderer.assets if a.name.startswith("images/m-"))
    rep.seconds = time.time() - t0
    return rep


class _PageList:
    """List-like access by original page index for possibly sparse page subsets."""

    def __init__(self, items, key: str = "index"):
        self._items = {getattr(it, key): it for it in items}
        self._len = (max(self._items) + 1) if self._items else 0

    def __getitem__(self, i):
        return self._items[i]

    def __len__(self) -> int:
        return self._len

    def __iter__(self):
        return iter(self._items.values())
