# pdf2epub

Convert PDFs, especially scientific papers, into reflowable EPUBs that are pleasant to read on a phone through Kindle, **without losing fidelity**.

- Body text is **reflowed into a single column** at the reader's font size. Two-column layouts, wrapped figures and page breaks are untangled into one reading order, and paragraphs are re-joined across columns and pages.
- **Formulas, figures and tables are rendered straight from the PDF's vector content** at high resolution, so they look exactly like the original. Tap to zoom on Kindle.
- Simple inline math (`x`, `n = 100`, `W_out`) stays as real, searchable text. Anything two-dimensional (fractions, stacked scripts, accents, radicals) becomes a small image cropped from the PDF and aligned to the text baseline.
- Boxed text (prompts, examples, code) and pseudo-code (`Algorithm 1`) are rebuilt as styled text, not screenshots, so they stay readable on a narrow screen. Original colors and line structure are kept.
- Footnotes become Kindle pop-up notes. Hyperlinks already in the PDF (hyperref citations, figure, table and equation references, URLs) become tappable links. The table of contents is built from the detected headings, matched against the PDF outline when there is one.

| Inline math as text | Display equation | Boxed example |
| --- | --- | --- |
| ![Inline math rendered on a phone](docs/phone-inline-math.png) | ![Display equation split for a narrow screen](docs/phone-equation.png) | ![Boxed prompt reflowed as text](docs/phone-text-box.png) |

*Pixel-sized renders of the [EPUB](demo/refusal_in_language_models_is_mediated_by_a_single_direction-2406.11717v3.epub) converted from [arXiv 2406.11717v3](demo/refusal_in_language_models_is_mediated_by_a_single_direction-2406.11717v3.pdf). More papers, with their quality scores, are in [`demo/`](demo/README.md).*

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
pdf2epub convert 1706.03762.pdf                # arXiv id in the name: writes attention_is_all_you_need-1706.03762.epub
pdf2epub convert paper.pdf -o ~/Books/paper.epub --title "My Title" -a "First Author" -a "Second Author"
pdf2epub convert paper.pdf --pages 1-12        # only some pages
pdf2epub inspect paper.pdf --out debug/        # draw detected layout onto page images
pdf2epub check paper.pdf paper.epub            # score a conversion: fidelity metrics + defect lint
```

| Option | Default | Effect |
| --- | --- | --- |
| `--dpi` | 400 | Resolution of formula, figure and table images. |
| `--inline-math auto\|image` | `auto` | `auto` keeps linear inline math as text. `image` renders all non-trivial inline math as images. |
| `--split-panels / --no-split-panels` | on | Stack side-by-side sub-figures vertically for portrait screens. |
| `--rotate-wide` | off | Rotate very wide tables and figures 90° (read by turning the phone). |
| `--cover / --no-cover` | on | Use the first page as the cover image. |
| `--output/-o` | next to the PDF | Output `.epub` path. When the PDF's file name contains an arXiv id, the default is `{title_in_snake_case}-{arxiv_id}.epub`. |
| `--pages` | all | Page selection, 1-based, e.g. `1-5,8`. |
| `--title`, `--author/-a` | PDF metadata, else detected from the first page | Override title and authors. |
| `--language` | `en` | Language code written to the EPUB metadata. |
| `--quiet/-q` | off | Only print errors. |

`pdf2epub inspect` prints a per-page summary (columns, figures, tables, equations, footnotes) and writes annotated PNGs (`page-NNN.png`) showing every detected region and the reading order. Use it to see why a page converted the way it did. Colours: figure blue, table green, text box purple, equation orange, algorithm black, caption cyan, text grey with reading-order numbers, footnote brown, dropped running heads red, column bounds magenta.

`pdf2epub check` compares the EPUB's text with the PDF (word and bigram recall/precision, reading-order score) and lists likely defects (split paragraphs, stray fragments, unjoined hyphens, oversized inline formulas, blank images, missing captions…) with the PDF page of each, so they can be traced with `inspect --pages N`.

## Web articles

Research blog posts and other HTML articles convert directly, without a detour through PDF:

```bash
pip install -e ".[web]"                        # Playwright; uses an installed Chromium
pdf2epub web https://example.org/post          # writes {title_in_snake_case}.epub here
pdf2epub web https://example.org/post https://example.org/post/appendix.html -o post.epub   # several pages, one book
```

The page is rendered in headless Chromium, so client-side math (KaTeX, MathJax, MathML) and plots are drawn,
then the article's DOM is walked. Text, headings, lists, links and citations become real text, and site
navigation, sidebars, buttons and hover cards are dropped. Simple inline formulas stay text. Others become
sharp images aligned to the baseline, with their TeX as alt text. Display equations and figures become
trimmed high-resolution screenshots, with captions and equation numbers as text. Plain data tables
become real tables. Links between the converted pages point into the book.

## Library

```python
from pdf2epub import convert, ConvertOptions

report = convert("paper.pdf", "paper.epub", ConvertOptions(dpi=400, inline_math="auto"))
print(report.figures, report.tables, report.equations, report.footnotes)
```

`ConvertOptions` also takes `pages` (a list of **0-based** page indices), `split_panels`, `rotate_wide`, `cover`, `title`, `authors` and `language`; `convert()` accepts a `progress` callback. To plug the analysis into something else, `doc, pages, stats, layouts = pdf2epub.analyze(pdf, pages=None)` runs extraction and layout analysis, and `pdf2epub.build_document(doc, pages, stats, layouts)` returns the document model (`pdf2epub.model.Document`). `pdf2epub.quality.evaluate(pdf, epub)` scores a conversion.

## Reading on Kindle (Android / Pixel)

The Kindle app does not open `.epub` files tapped in a file manager. Send them with **Send to Kindle** and Amazon converts them for you:

- Android share sheet: long-press the file → Share → Kindle.
- Email the file as an attachment to your `@kindle.com` address.
- Upload it at <https://www.amazon.com/sendtokindle>.

To get a file onto the phone in the first place, a direct download link works well: every EPUB committed to this repository is downloadable at `https://github.com/mgbvox/pdf2epub/raw/main/demo/<name>.epub`, where `<name>` is `{title_in_snake_case}-{arxiv_id}` (see [`demo/`](demo/README.md)).

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
pip install -e ".[dev,qa]"                    # qa: playwright for phone previews
pytest -m "not corpus"                        # fast synthetic-PDF test suite
pytest -m corpus                              # the corpus quality gate (slow, see below)
EPUBCHECK_JAR=/path/to/epubcheck.jar pytest   # also validate output with EPUBCheck (needs java)
PDF2EPUB_SAMPLE_PDF=demo/refusal_in_language_models_is_mediated_by_a_single_direction-2406.11717v3.pdf pytest   # also convert a real paper
```

### Quality gate and corpus

[`demo/`](demo/README.md) keeps every conversion that was delivered or used for development: the input PDF, the accepted EPUB and a `.quality.json` baseline with its fidelity metrics and lint counts, all named `{title_in_snake_case}-{arxiv_id}` (e.g. `attention_is_all_you_need-1706.03762.pdf`). It currently holds 12 CS and q-bio arXiv papers in NeurIPS, ICLR, ACL, CVPR and ICML layouts, including a Google-Docs PDF and math-heavy appendices.

**Policy: a change to the converter is committed only if it does not make any of these conversions worse, and makes at least one better.** The gate re-converts every PDF with the current code and compares it with its baseline:

```bash
python tools/corpus_gate.py                   # check every entry (parallel); non-zero exit on regression
python tools/corpus_gate.py --keep /tmp/new   # also keep the new EPUBs
python tools/epub_diff.py demo/X.epub /tmp/new/X.epub --out /tmp/diff   # what exactly changed (text + images)
python tools/corpus_gate.py --update          # accept: rewrite demo/ EPUBs and baselines (refuses on regressions)
python tools/corpus_gate.py --add 2505.19056.pdf  # add a new conversion (named {title_in_snake_case}-{arxiv_id})
python tools/corpus_gate.py --rescore         # after changing quality.py itself: re-score without converting
```

Commit `--update` output together with the code change, so the baseline diff shows what improved. A regression is any metric dropping by more than 0.002, more lint findings in total, or any more blank images, duplicate paragraphs or unjoined hyphens.

Other tools: `tools/phone_preview.py` screenshots chapters at a Pixel-sized viewport (light or `--dark`), and `tools/run_corpus.py` batch-converts arbitrary arXiv ids into `.corpus/` for exploration.

### For AI agents

[`.claude/skills/pdf2epub/SKILL.md`](.claude/skills/pdf2epub/SKILL.md) is a skill for coding agents. It covers converting a paper, the quality-review and refinement pass, changing the converter under the corpus gate, and delivering an EPUB to a phone. [`reference/architecture.md`](.claude/skills/pdf2epub/reference/architecture.md) maps the modules, heuristics and known traps.

## Limitations

- Scanned PDFs (no text layer) are not supported; run OCR first.
- Tables are kept as faithful images, not rebuilt as HTML tables. Wide tables shrink to the screen width; tap to zoom, or use `--rotate-wide`.
- Layout analysis is heuristic. Unusual templates may misplace a caption or split a figure. `pdf2epub inspect` shows what was detected.
- Footnotes are detected only when a separator rule sits above them. Only bullet-glyph lists become list markup; numbered lists stay paragraphs. Plain-text citations without link annotations are not linked.
- The Kindle renderer itself could not be tested here. Output was validated with EPUBCheck and visually checked in Chromium at a Pixel-sized viewport (412×915 CSS px).
