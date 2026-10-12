"""Web article -> EPUB on a synthetic local page (needs Playwright and Chromium)."""

from __future__ import annotations

import re
import zipfile

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from pdf2epub.web import WebOptions, convert_web, find_chromium  # noqa: E402

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>A Synthetic Web Article</title>
<style>
  body { font: 17px/1.5 serif; background: #f6f6f6; }
  main { max-width: 700px; margin: auto; }
  .meta { display: flex; gap: 12px; }
  .cite-card { display: none; }
  aside { position: fixed; right: 0; }
</style></head>
<body>
<nav>SITE NAVIGATION SHOULD NOT APPEAR</nav>
<main>
  <header><h1>A Synthetic Web Article</h1>
    <p class="authors">Ada Lovelace, Alan Turing</p>
    <p class="meta"><span>May 2026</span><a href="mailto:x@example.org">x@example.org</a></p>
  </header>
  <nav class="contents">CONTENTS LIST SHOULD NOT APPEAR</nav>
  <aside>SIDEBAR SHOULD NOT APPEAR</aside>
  <section id="intro"><h2>1 Introduction</h2>
    <p>Simple math <math><semantics><mrow><msub><mi>x</mi><mi>i</mi></msub><mo>=</mo><mn>2</mn></mrow>
      <annotation encoding="application/x-tex">x_i = 2</annotation></semantics></math> stays text, while
      <math><semantics><mfrac><mi>a</mi><mi>b</mi></mfrac><annotation encoding="application/x-tex">\\frac{a}{b}</annotation></semantics></math>
      becomes an image. A citation
      <span class="cite"><a href="#ref-one">(One, 2020)</a><span class="cite-card">HOVER CARD SHOULD NOT APPEAR</span></span>
      links to the references.</p>
    <h3 id="sub">1.1 Details</h3>
    <ul><li>first item</li><li>second item with <em>emphasis</em></li></ul>
  </section>
  <section id="results"><h2>2 Results</h2>
    <figure id="fig-1"><svg width="400" height="160" viewBox="0 0 400 160"><rect x="10" y="10" width="380" height="140" fill="#3366cc"/></svg>
      <figcaption>Figure 1: A blue rectangle.</figcaption></figure>
    <table id="tab-1"><caption>Table 1: Numbers.</caption>
      <tr><th>Model</th><th style="text-align:right">Score</th></tr>
      <tr><td>Base</td><td style="text-align:right">71.2</td></tr></table>
    <p class="back"><a href="/">← Back to home</a></p>
  </section>
  <section id="references"><h2>References</h2>
    <p id="ref-one" class="bibitem">A. One. A paper. 2020.</p>
  </section>
</main></body></html>
"""


@pytest.fixture(scope="module")
def book(tmp_path_factory) -> dict[str, str]:
    if find_chromium() is None:
        try:
            with playwright.sync_playwright() as pw:
                pw.chromium.launch().close()
        except Exception:
            pytest.skip("no Chromium available for Playwright")
    d = tmp_path_factory.mktemp("web")
    page = d / "article.html"
    page.write_text(PAGE, encoding="utf-8")
    out = d / "article.epub"
    rep = convert_web([page.as_uri()], out, WebOptions(width=700, scale=2.0))
    assert rep.title == "A Synthetic Web Article"
    assert rep.authors == ["Ada Lovelace", "Alan Turing"]
    with zipfile.ZipFile(out) as z:
        files = {n: z.read(n) for n in z.namelist()}
    text = "\n".join(v.decode("utf-8") for n, v in files.items() if n.endswith(".xhtml") and "/text/" in n)
    return {"text": text, "names": "\n".join(files), "css": files["OEBPS/style.css"].decode()}


def test_navigation_hover_cards_and_back_links_are_dropped(book):
    for junk in ("SITE NAVIGATION", "CONTENTS LIST", "SIDEBAR", "HOVER CARD", "Back to home"):
        assert junk not in book["text"], junk


def test_structure_text_and_links(book):
    t = book["text"]
    assert re.search(r'<h1 class="title"[^>]*>A Synthetic Web Article</h1>', t)
    assert re.search(r"<h2[^>]*>1 Introduction</h2>", t) and re.search(r"<h3[^>]*>1\.1 Details</h3>", t)
    assert "May 2026 " in t  # flex items are separated
    assert "<li>second item with <i>emphasis</i></li>" in t
    assert re.search(r'<a href="(ch\d+\.xhtml)?#ref-one">\(One, 2020\)</a>', t)
    assert 'id="ref-one"' in t


def test_simple_math_is_text_and_complex_math_an_aligned_image(book):
    t = book["text"]
    assert "<i>x</i><sub><i>i</i></sub> = 2" in t
    m = re.search(r'<img class="m"[^>]*alt="\\frac\{a\}\{b\}"[^>]*style="height:[\d.]+em;width:[\d.]+em;vertical-align:-?[\d.]+em"', t)
    assert m, "fraction should be an inline image with its TeX as alt text"


def test_figure_is_an_image_with_text_caption_and_table_is_a_real_table(book):
    t = book["text"]
    assert re.search(r'<div class="figure" id="fig-1"><div class="img"><img class="fig"[^>]*alt="Figure 1"', t)
    assert '<p class="caption">Figure 1: A blue rectangle.</p>' in t
    assert re.search(r'<div class="table" id="tab-1"><table class="data"><tr><th>Model</th><th class="r">Score</th></tr>', t)
    assert '<td class="r">71.2</td>' in t and '<p class="caption">Table 1: Numbers.</p>' in t
    assert "table.data" in book["css"]
