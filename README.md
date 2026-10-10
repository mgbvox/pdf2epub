# pdf2epub

Convert PDFs, especially scientific papers, into reflowable EPUBs that are pleasant to read on a phone through Kindle, **without losing fidelity**.

- Body text is **reflowed into a single column** at the reader's font size. Two-column layouts, wrapped figures and page breaks are untangled into one reading order, and paragraphs are re-joined across columns and pages.
- **Formulas, figures and tables are rendered straight from the PDF's vector content** at high resolution, so they look exactly like the original. Tap to zoom on Kindle.
- Simple inline math (`x`, `n = 100`, `W_out`) stays as real, searchable text. Anything two-dimensional (fractions, stacked scripts, accents, radicals) becomes a small image cropped from the PDF and aligned to the text baseline.
- Boxed text (prompts, examples, code) and pseudo-code (`Algorithm 1`) are rebuilt as styled text, not screenshots, so they stay readable on a narrow screen. Original colors and line structure are kept.
- Footnotes become Kindle pop-up notes. Citations, figure, table and equation references and URLs become tappable links. The PDF outline becomes the table of contents.

| Inline math as text | Display equation | Boxed example |
| --- | --- | --- |
| ![Inline math rendered on a phone](docs/phone-inline-math.png) | ![Display equation split for a narrow screen](docs/phone-equation.png) | ![Boxed prompt reflowed as text](docs/phone-text-box.png) |

*Pixel-sized renders of [`demo/2406.11717v3.epub`](demo/), converted from [`demo/2406.11717v3.pdf`](demo/).*

## Install

```bash
pip install git+https://github.com/mgbvox/pdf2epub
# or, from a checkout
pip install -e ".[dev]"
```

Requires Python 3.10+. Dependencies: PyMuPDF, Pillow, NumPy, Typer.

## Command line

```bash
pdf2epub convert paper.pdf                     # writes paper.epub next to the PDF
pdf2epub convert paper.pdf -o ~/Books/paper.epub --title "My Title" -a "First Author" -a "Second Author"
pdf2epub convert paper.pdf --pages 1-12        # only some pages
pdf2epub inspect paper.pdf --out debug/        # draw detected layout onto page images
```

| Option | Default | Effect |
| --- | --- | --- |
| `--dpi` | 400 | Resolution of formula, figure and table images. |
| `--inline-math auto\|image` | `auto` | `auto` keeps linear inline math as text. `image` renders all non-trivial inline math as images. |
| `--split-panels / --no-split-panels` | on | Stack side-by-side sub-figures vertically for portrait screens. |
| `--rotate-wide` | off | Rotate very wide tables and figures 90° (read by turning the phone). |
| `--cover / --no-cover` | on | Use the first page as the cover image. |
| `--title`, `--author/-a`, `--language` | from the PDF | Override metadata. |

`pdf2epub inspect` prints a per-page summary (columns, figures, tables, equations, footnotes) and writes annotated PNGs showing every detected region and the reading order. Use it to see why a page converted the way it did.

## Library

```python
from pdf2epub import convert, ConvertOptions

report = convert("paper.pdf", "paper.epub", ConvertOptions(dpi=400, inline_math="auto"))
print(report.figures, report.tables, report.equations, report.footnotes)
```

`pdf2epub.analyze(pdf)` returns the intermediate page layouts and `pdf2epub.build_document(...)` the document model, if you want to plug the analysis into something else.

## Reading on Kindle (Android / Pixel)

The Kindle app does not open `.epub` files tapped in a file manager. Send them with **Send to Kindle** and Amazon converts them for you:

- Android share sheet: long-press the file → Share → Kindle.
- Email the file as an attachment to your `@kindle.com` address.
- Upload it at <https://www.amazon.com/sendtokindle>.

## How it works

1. **Extraction**: glyphs with exact positions, fonts and colors, vector paths, images and links (PyMuPDF).
2. **Line model**: rows are rebuilt from glyphs so sub/superscripts, radicals and inline-fraction parts attach to their base line.
3. **Document statistics**: body text size, which fonts carry math (by name, plus TeX text fonts that mostly appear glued to math), and running heads and page numbers to drop.
4. **Page layout**: column detection, footnote areas, captions, clustering of vector graphics into figures, booktabs-style tables from rule stacks, boxed text, algorithms and display equations (with equation numbers). Reading order comes from a recursive XY-cut that prefers column gutters.
5. **Structure**: headings from the PDF outline, with font-based fallbacks for unnumbered and small-caps headings. Paragraphs are joined across columns and pages, using indentation, spacing and a "would the next word have fit?" test. Also handled: lists, bibliography entries with hanging indents, captions, and floats deferred to the end of the paragraph they interrupt.
6. **Rendering**: each region is cropped from a high-DPI page render. Neighbouring text inside the crop rectangle is masked out using the known glyph positions, and the result is trimmed to its ink. Wide multi-part equations are split at their internal gaps and stacked, and the gap before an equation number is squeezed.
7. **EPUB**: EPUB 3 with an NCX fallback and Kindle-safe CSS. Images are sized in `em` so formulas match the reader's chosen font size and are capped at the screen width. The book is split into chapters at top-level headings.

## Development

```bash
pip install -e ".[dev]"
pytest                                        # synthetic-PDF test suite
EPUBCHECK_JAR=/path/to/epubcheck.jar pytest   # also validate output with EPUBCheck
PDF2EPUB_SAMPLE_PDF=demo/2406.11717v3.pdf pytest   # also convert a real paper
```

The converter has been checked on a mixed corpus of arXiv papers: NeurIPS and ICLR single-column, ACL, CVPR and ICML two-column, math-heavy proofs, and q-bio papers including a Google-Docs-generated PDF. Every output passes EPUBCheck with no errors.

## Limitations

- Scanned PDFs (no text layer) are not supported; run OCR first.
- Tables are kept as faithful images, not rebuilt as HTML tables. Wide tables shrink to the screen width; tap to zoom, or use `--rotate-wide`.
- Layout analysis is heuristic. Unusual templates may misplace a caption or split a figure. `pdf2epub inspect` shows what was detected.
- The Kindle renderer itself could not be tested here. Output was validated with EPUBCheck and visually checked in Chromium at a Pixel-sized viewport (412×915 CSS px).
