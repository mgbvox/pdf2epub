"""Command line interface: ``pdf2epub convert paper.pdf``."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer

from . import __version__

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Convert PDFs (scientific papers in particular) into reflowable EPUBs for phones and Kindle.",
)


class MathMode(str, Enum):
    auto = "auto"
    image = "image"


def _parse_pages(spec: str | None) -> list[int] | None:
    if not spec:
        return None
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a) - 1, int(b)))
        else:
            out.append(int(part) - 1)
    return sorted(set(p for p in out if p >= 0))


def _version(value: bool) -> None:
    if value:
        typer.echo(f"pdf2epub {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[Optional[bool], typer.Option("--version", callback=_version, is_eager=True, help="Show version.")] = None,
) -> None:
    """pdf2epub: faithful PDF to EPUB conversion."""


@app.command()
def convert(
    pdf: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="Input PDF file.")],
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Output .epub path (default: next to the PDF; named {title_in_snake_case}-{arxiv_id}.epub when the PDF's name contains an arXiv id).")] = None,
    dpi: Annotated[int, typer.Option(min=150, max=1200, help="Resolution for figures, tables and formulas.")] = 400,
    inline_math: Annotated[MathMode, typer.Option(help="auto: simple inline math as text, complex as images; image: all math as images.")] = MathMode.auto,
    split_panels: Annotated[bool, typer.Option(help="Stack side-by-side sub-figures vertically for narrow screens.")] = True,
    rotate_wide: Annotated[bool, typer.Option(help="Rotate very wide tables/figures 90 degrees.")] = False,
    cover: Annotated[bool, typer.Option(help="Give the book a cover image.")] = True,
    cover_figure: Annotated[str, typer.Option(help="Cover figure: a number or label from 'pdf2epub figures', 'auto', or 'page' (first page).")] = "auto",
    title: Annotated[Optional[str], typer.Option(help="Override the book title.")] = None,
    author: Annotated[Optional[list[str]], typer.Option("--author", "-a", help="Override authors (repeatable).")] = None,
    language: Annotated[str, typer.Option(help="Language code for the EPUB metadata.")] = "en",
    pages: Annotated[Optional[str], typer.Option(help="Page selection, e.g. '1-5,8' (1-based).")] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only print errors.")] = False,
) -> None:
    """Convert a PDF into a Kindle-friendly EPUB."""
    from .converter import ConvertOptions
    from .converter import convert as run

    opts = ConvertOptions(
        dpi=dpi, inline_math=inline_math.value, split_panels=split_panels, rotate_wide=rotate_wide,
        cover=cover, title=title, authors=list(author) if author else None, language=language,
        pages=_parse_pages(pages), cover_figure=cover_figure,
    )
    progress = None if quiet else (lambda msg: typer.echo(f"  - {msg}", err=True))
    report = run(pdf, output, opts, progress=progress)
    if not quiet:
        typer.echo(
            f"Wrote {report.output} ({report.output.stat().st_size / 1e6:.1f} MB) in {report.seconds:.1f}s\n"
            f"  title: {report.title}\n"
            f"  pages: {report.pages}  chapters: {report.chapters}  headings: {report.headings}  paragraphs: {report.paragraphs}\n"
            f"  figures: {report.figures}  tables: {report.tables}  equations: {report.equations}  "
            f"text boxes: {report.text_boxes}  footnotes: {report.footnotes}\n"
            f"  images: {report.images} (inline math: {report.inline_math_images})"
        )


@app.command()
def inspect(
    pdf: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="Input PDF file.")],
    out_dir: Annotated[Path, typer.Option("--out", "-o", help="Directory for annotated page images.")] = Path("pdf2epub-debug"),
    pages: Annotated[Optional[str], typer.Option(help="Page selection, e.g. '1-5,8' (1-based).")] = None,
) -> None:
    """Draw the detected layout (figures, tables, equations, reading order) onto page images."""
    from .converter import analyze
    from .debug import draw_overlays
    from .layout import Region

    doc, page_data, stats, layouts = analyze(pdf, _parse_pages(pages))
    typer.echo(f"body text size {stats.body_size}pt, math fonts: {', '.join(sorted(stats.math_fonts)) or '-'}")
    for lay in layouts:
        kinds: dict[str, int] = {}
        for it in lay.items:
            if isinstance(it, Region):
                kinds[it.kind] = kinds.get(it.kind, 0) + 1
        cols = len(lay.columns)
        summary = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())) or "text only"
        typer.echo(f"page {lay.page + 1}: {cols} column(s); {summary}; {len(lay.footnotes)} footnote(s)")
    paths = draw_overlays(doc, layouts, out_dir)
    typer.echo(f"Wrote {len(paths)} annotated page(s) to {out_dir}")


@app.command()
def web(
    urls: Annotated[list[str], typer.Argument(help="Article URL(s); several pages (e.g. article + appendix) become one book.")],
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Output .epub path (default: {title_in_snake_case}[-{arxiv_id}].epub in the current directory).")] = None,
    title: Annotated[Optional[str], typer.Option(help="Override the book title.")] = None,
    author: Annotated[Optional[list[str]], typer.Option("--author", "-a", help="Override authors (repeatable).")] = None,
    language: Annotated[Optional[str], typer.Option(help="Language code (default: the page's lang attribute, else en).")] = None,
    width: Annotated[int, typer.Option(min=320, max=1600, help="Reading-column width in CSS px used to lay out the page and its figures.")] = 760,
    scale: Annotated[float, typer.Option(min=1.0, max=4.0, help="Pixel density of formula and figure screenshots.")] = 3.0,
    root: Annotated[Optional[str], typer.Option(help="CSS selector of the article element (default: main/article).")] = None,
    cover: Annotated[bool, typer.Option(help="Give the book a cover image.")] = True,
    cover_figure: Annotated[str, typer.Option(help="Cover figure: a number or label from 'pdf2epub figures', 'auto', or 'page' (top of the page).")] = "auto",
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only print errors.")] = False,
) -> None:
    """Convert a web article (HTML, e.g. a research blog post) into a Kindle-friendly EPUB."""
    from .web import WebOptions, convert_web

    progress = None if quiet else (lambda msg: typer.echo(f"  - {msg}", err=True))
    rep = convert_web(urls, output, WebOptions(title=title, authors=author, language=language, width=width, scale=scale, root=root, cover=cover, cover_figure=cover_figure), progress)
    if not quiet:
        typer.echo(f"Wrote {rep.output} ({rep.output.stat().st_size / 1e6:.1f} MB) in {rep.seconds:.1f}s")
        typer.echo(f"  title: {rep.title}")
        typer.echo(f"  pages: {rep.pages}  chapters: {rep.chapters}  headings: {rep.headings}  paragraphs: {rep.paragraphs}")
        typer.echo(f"  figures: {rep.figures}  tables: {rep.tables}  equations: {rep.equations}  inline math: {rep.inline_math_text} as text, {rep.inline_math_images} as images")


@app.command()
def figures(
    epub: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="A converted EPUB.")],
    out_dir: Annotated[Optional[Path], typer.Option("--out", "-o", help="Write figure images and a numbered contact sheet here.")] = None,
) -> None:
    """List the book's figures, to choose the cover figure (then: pdf2epub cover BOOK --figure N)."""
    from .cover import auto_pick, candidates, contact_sheet

    cands = candidates(epub)
    if not cands:
        typer.echo("no figures: the cover stays the first page")
        return
    auto = auto_pick(cands)
    for c in cands:
        star = "*" if c is auto else " "
        where = f"p{c.page}" if c.page else "-"
        typer.echo(f"{star}{c.number:>3}  {c.label or 'figure':<12} {where:<5} {c.width}x{c.height:<6} {c.caption[:110]}")
    typer.echo(f"* = automatic choice (#{auto.number})")
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        for c in cands:
            ext = "jpg" if c.image[:3] == b"\xff\xd8\xff" else "png"
            (out_dir / f"figure-{c.number:02d}.{ext}").write_bytes(c.image)
        sheet = contact_sheet(cands, out_dir / "figures.png")
        typer.echo(f"Wrote {len(cands)} figure image(s) and the contact sheet {sheet}")


@app.command()
def cover(
    epub: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="A converted EPUB.")],
    figure: Annotated[str, typer.Option("--figure", "-f", help="Figure number or label (see pdf2epub figures), or 'auto'.")] = "auto",
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Write a new EPUB instead of updating in place.")] = None,
    preview: Annotated[Optional[Path], typer.Option(help="Also save the cover image here (.jpg) to look at it.")] = None,
) -> None:
    """Make the book's cover from one of its figures: title, authors and source above the figure."""
    import zipfile

    from .cover import COVER_NAME, set_cover

    fig = set_cover(epub, figure, output)
    target = output or epub
    if fig is None:
        typer.echo("cover unchanged")
        return
    typer.echo(f"cover of {target} now shows figure #{fig.number} {fig.label}".rstrip())
    if preview:
        with zipfile.ZipFile(target) as z:
            data = next(z.read(n) for n in z.namelist() if n.endswith(COVER_NAME))
        preview.write_bytes(data)
        typer.echo(f"preview: {preview}")


@app.command()
def check(
    pdf: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="Source PDF file.")],
    epub: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="EPUB converted from it.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print the full report as JSON.")] = False,
) -> None:
    """Score a conversion: fidelity metrics and defect lint with the PDF page of each issue."""
    import json

    from . import quality

    rep = quality.evaluate(pdf, epub)
    if as_json:
        data = rep.to_json()
        data["issues"] = quality.issues_as_dicts(rep)
        typer.echo(json.dumps(data, indent=2))
        return
    typer.echo("  ".join(f"{k}={v:.4f}" for k, v in rep.metrics.items()))
    typer.echo("  ".join(f"{k}={v}" for k, v in rep.counts.items()))
    typer.echo(f"{len(rep.issues)} issue(s)" + (": " + ", ".join(f"{k} {v}" for k, v in sorted(rep.issue_counts.items())) if rep.issues else ""))
    for i in rep.issues:
        where = f"p{i.page}" if i.page is not None else "-"
        typer.echo(f"  [{i.kind}] {where} {i.chapter}#{i.block}: {i.snippet}")


if __name__ == "__main__":  # pragma: no cover
    app()
