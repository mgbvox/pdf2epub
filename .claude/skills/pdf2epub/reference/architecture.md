# pdf2epub architecture and heuristics map

Coordinates are PyMuPDF page space: points, origin top-left, y grows downwards. Sizes and gaps are compared
in multiples of the body font size (`stats.body_size`, `PageAnalyzer.body`) or the body line pitch
(`stats.line_step`). Page numbers are 0-based in code (`PageData.index`, `PageLayout.page`) and 1-based in
the CLI (`--pages`) and in the EPUB's `data-page` attributes.

## Pipeline

```
converter.convert(pdf, epub, ConvertOptions)
  analyze()                                    converter.py
    extract.extract_page      -> PageData      glyphs -> Fragments -> Rows; graphics; links
    analysis.compute_stats    -> DocStats      body size, line pitch, math fonts, vocabulary
    analysis.mark_furniture                    running heads, page numbers (role = "furniture")
    layout.analyze_page       -> PageLayout    regions + text segments in reading order
  StructureBuilder.build()    -> Document      structure.py: headings, paragraphs, lists, floats, notes
  HtmlWriter.write()          -> chapters      htmlgen.py + inline.py + render.py: XHTML and image crops
  epub.write_epub()                            EPUB 3 + NCX, Kindle CSS
quality.evaluate(pdf, epub)                    metrics + lint (CLI `check`, tools/corpus_gate.py)
```

`converter.analyze(pdf, pages)` and `converter.build_document(...)` expose the intermediate stages for
debugging. `convert()` builds the `StructureBuilder` inline, so a change to how the document is built must
be made in both places.

## extract.py: glyphs to rows

- `extract_fragments` reads `page.get_text("rawdict", TEXT_FLAGS)`. Ligatures are expanded through
  `LIGATURES`, so `Glyph.c` may be several characters sharing one bbox; never assume `len(g.c) == 1`.
  TeX extension fonts without a ToUnicode map arrive as raw codes. ASCII letters in CMEX/LMEX are
  translated through `CMEX_LETTERS` (`P`→∑, `q`→√, …). Whitespace never becomes a glyph: it sets
  `space_before` on the next glyph. Each glyph gets `seq`, its order in the content stream.
- `build_rows` groups fragments into page-wide `Row`s. Large fragments seed rows; sub- and superscripts
  attach by baseline shift. Extension-font fragments (radicals, big operators; `is_extension_fragment`)
  are placed last, and their cost is the glyph centre against the row's math axis, because TeX hangs
  those glyphs from their origin. Every glyph gets `level` (0 base, 1 superscript, -1 subscript).
- `sort_glyphs` is the only correct way to order a row's glyphs: x0, with content-stream order for
  near-ties. Use it everywhere; a plain x sort turns "suffix" into "suffxi".
- `split_row` cuts rows into segments at large gaps and at column gutters.
- `Row.core` (baseline −0.72·size to +0.24·size) is the box for vertical gap tests: tall scripts and
  big delimiters do not inflate it.
- `text_size(glyphs)` is the running-text size, robust to small caps and math: the largest size of
  level-0 lowercase non-math glyphs. Use it, not `Row.size`/`Line.size`, when comparing lines for
  "same paragraph".
- `needs_space` decides word spaces from geometry.

## analysis.py, fonts.py

- `fonts.font_info` classifies fonts (math-only, TeX text family, bold, italic, mono) by name regexes on
  the compacted name, which has underscores and spaces removed. Patterns containing `_` therefore never
  match (a known issue for KaTeX names).
- `analysis.compute_stats` derives the body size and pitch, and promotes TeX text fonts that mostly
  touch math to math fonts. It sets `Glyph.math`. Body-family fonts (e.g. CMR in a Computer Modern
  paper) never count as math, so digits and `=` inside formulas often have `math=False`.
- `mark_furniture` drops repeated page heads and footers and page numbers.

## layout.py: one page into regions and reading order

`PageAnalyzer.run()` passes, **in order** (ownership is first-come, via `take()` / `free()`):

1. `detect_footnotes`: needs a short rule at the column's left edge in the lower part of the page.
2. `detect_captions`: `CAPTION_RE` ("Figure 3:", "Table 2.", "Algorithm 1"…).
3. `mark_math_strokes`, `clusters` (vector graphics grouped into figures), `rule_tables` (booktabs
   stacks of horizontal rules).
4. `region_for_caption(cap)`, once per caption:
   - `_algorithm_region` for "Algorithm N". Rows below the caption down to the closing rule, **only
     segments inside the caption's column**.
   - Then three passes, each trying both directions: graphics or ruled tables (`_attach_table`,
     `_region_near`); content framed by two rules (`_ruled_block`, giving a text box or table with
     `style="ruled"`); a stricter text-only fallback (`_textual_region`, which never takes headings or
     the tail line of a paragraph). The usual side (tables: caption above the content; figures: caption
     below) wins unless the other side's content is clearly closer (`_nearest_content`), as in papers
     that set table captions below.
   - A caption with no content is released back into the text flow.
5. Text boxes (framed clusters that are mostly text), leftover figures.
6. `detect_equations`: candidates are column units (`_units`: a row's segments within one column, or
   spanning the gutter). A unit qualifies if it is math-dense **and** in display position (indented,
   centred, carries an equation number, or has a display operator with limits underneath). Units are
   grouped per column, then nearby script rows and right-aligned equation numbers are attached
   (`EQNUM_RE`). Only rows whose segments are all still free are considered, so math inside figures or
   boxes is never an equation.
7. `reading_order`: recursive XY-cut over items. Vertical cuts are accepted only at column gutters or
   where the two sides share few rows (`_vertical_cut_ok`), so a run-in heading is not cut away from
   its line.

Region kinds: `figure`, `table`, `textbox`, `algorithm`, `equation`. `Region.box` excludes the caption;
`outer_box` includes it. Equation numbers are in `eqnum_segs`, outside `box`.

`find_columns` detects columns from prose rows with a shared left margin. Math-heavy pages used to fake
a second column, so keep that restriction.

## structure.py: lines into a document

`build()` order matters: `_entries` (segments to `Line`s, merging same-row segments) → `_frames`
(left/right reference edges per line) → `_detect_headings` → `_detect_title` → `_uses_indents` →
`_assemble` → `_post_process` → footnotes.

- `_frames`: a line's right reference is the furthest right edge reached by earlier lines **of the
  same style** in its block. A long heading must not widen the frame of a paragraph wrapped beside a
  figure. Bold lines see the whole block.
- `ended_short(prev, line)`: "would the next word have fitted on the previous line?" If yes, the
  paragraph ended. A hyphenated end-of-line always continues.
- `_continues(cur, line)`: size match (via `text_size`), grid rows, `ended_short`, vertical gap
  (discounting the extra space tall inline math adds), indentation, list items, and column or page
  changes (sentence end plus capital starts a new paragraph when the document doesn't use indents).
- Headings: PDF outline entries are matched to lines first (with wrapped-title continuation). Then
  `_heading_like` (bold or large or small caps, standing alone), and `_heading_wrap` for the second
  line of a wrapped heading. Numbered small-caps headings take their level from the numbering depth.
- Floats that interrupt a paragraph are deferred to the end of the paragraph.
- `_junk_title`: PDF metadata titles like "arXiv", file names, "Microsoft Word - …" lose to the
  detected title.

## htmlgen.py, inline.py, render.py, epub.py: output

- `inline.tokenize_paragraph` splits lines into text runs, math runs, note refs and line breaks. A math
  run stays text (`TextRun(math=True)`) if every glyph is in `SAFE_MATH` with simple scripts; otherwise it
  becomes one `MathImage`. The decision is all-or-nothing per run, and inline images are never split.
- `Renderer.crop(page, members, foreign, allow_boxes)` cuts from a cached high-DPI page render. It
  masks foreign glyphs inside the rectangle and trims to ink (`ink_box`). `squeeze_gap` shortens the
  gap before an equation number. `split_wide` stacks wide equations at internal gaps. `_panels` splits
  side-by-side sub-figures (2–6 parts) for portrait screens.
- `HtmlWriter` writes `data-page` on every block. It splits chapters at the top heading level and sizes
  images in `em` so formulas follow the reader's font size. Footnotes become `<aside epub:type="footnote">`
  at the end of the chapter.
- `epub.write_epub`: `mimetype` is stored first. The CSS must never force a font family, size or colour
  on body text.

## quality.py: the yardstick

- `pdf_words` gives the PDF's words in content-stream order with end-of-line hyphens joined.
  `epub_words` gives running text plus image alt text, joined across inline markup (small caps as
  `M<span>ASK</span>` is one word).
- Metrics: `word_recall`/`word_precision`, `bigram_recall`/`bigram_precision` (multiset overlaps), and
  `order_score` (share of unique 6-gram anchors from each 50-word PDF chunk found in ascending order in
  the EPUB).
- `lint()` kinds are listed in the skill. `compare()` gives the regression rules; `improvements()` is
  the opposite.
- Changing anything here changes every baseline: `tools/corpus_gate.py --rescore` and a separate commit.

## Symptom → where to look

| Symptom in the EPUB | Likely cause |
| --- | --- |
| Paragraph broken into one-line paragraphs | `_frames` (frame too wide) or `ended_short`; inspect `line.frame` |
| Paragraph broken where a formula sits in a line | `_continues` gap test; tall inline math |
| Two papers' columns interleaved, text from the other column inside a box | a region taking segments across the gutter (`_algorithm_region`, `_textual_region`) or `reading_order` cuts |
| Heading missing or in the body | `_heading_like` stand-alone tests, `_heading_wrap`, outline matching |
| Caption on the wrong figure or table, or "Figure N has no caption" | `region_for_caption` direction and pass order |
| Equation cut in half, or part of it inline as a wide image | `detect_equations` candidate test (display position), unit grouping |
| Strange characters in alt text or text (`q`, `P`, `X` for √, ∑) | extension-font codes (`CMEX_LETTERS`), font classification |
| Letters swapped inside a word | glyph sort (`sort_glyphs`), ligatures |
| Prompt or template box rendered as an image or lost | text box detection, `_ruled_block` |

## Known open issues (backlog)

- `get_drawings` reports clipped paths at full size, so plots drawn with a clip path can form one huge
  "ink" cluster (2002.05709 p5: a cluster spanning both columns swallows Figure 7). The same paper's
  side-by-side captions on p15 (Figure B.3 / B.4) are read interleaved. Fix: honour clip rectangles
  (`get_drawings(extended=True)`); then a figure caption can be limited to content in its own column,
  as table captions already are (`_region_near`, `same_column`).

- `htmlgen` collapses runs of NBSP, so indentation in code paragraphs is lost.
- KaTeX font names containing `_` never match the `fonts.py` math patterns.
- Footnotes are detected only with a separator rule. Numbered lists stay plain paragraphs; only bullet
  glyphs become `<ul>`.
- Outline heading matching uses `per_page[pno]`, which is wrong for `--pages` subsets that do not start
  at page 1.
- Tables are images, not HTML tables, by design for fidelity. Wide tables rely on tap-to-zoom or
  `--rotate-wide`.
