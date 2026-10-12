"""Convert a web article (an HTML page) into the same Kindle-friendly EPUB as a PDF.

A web article already has what the PDF pipeline has to reconstruct: headings,
paragraphs, lists, links, and the TeX source of every formula. So instead of
printing it to a PDF, the page is rendered in headless Chromium (so client-side
math such as KaTeX runs), its article DOM is walked, and:

* text, headings, lists, links and citations become real XHTML text (hover
  cards, navigation, buttons and hidden elements are dropped);
* inline math stays text when it is a simple linear formula built from
  Kindle-safe characters, and otherwise becomes a high-resolution screenshot of
  the rendered formula, aligned to the text baseline, with its TeX as alt text;
* display equations, figures (SVG, canvas, images) and tables become
  screenshots, with captions and equation numbers kept as text;
* internal links (``#ref-...``, sections, figures) are rewritten to the
  chapter files, also across several pages converted into one book (e.g. an
  article and its appendix page).

Requires Playwright (``pip install -e ".[web]"``) and a Chromium build:
``PDF2EPUB_CHROMIUM``, ``/opt/pw-browsers`` or ``playwright install chromium``.
"""

from __future__ import annotations

import glob
import hashlib
import html
import io
import os
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from .epub import write_epub
from .htmlgen import Chapter
from .inline import SAFE_MATH
from .naming import arxiv_id, paper_stem
from .render import ImageAsset

# characters that may stay as text in simple inline math (besides SAFE_MATH)
_TEXT_MATH_EXTRA = set("   −×·≤≥≠≈±∞→←′ℓ")

# ---------------------------------------------------------------------------
# in-page extraction (runs in Chromium)

EXTRACT_JS = r"""
(opts) => {
  const root = (opts.root && document.querySelector(opts.root)) || document.querySelector('main article')
    || document.querySelector('article') || document.querySelector('main') || document.body;
  const shots = [];
  const blocks = [];
  const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const SKIP = new Set(['script', 'style', 'noscript', 'template', 'button', 'input', 'select', 'textarea', 'form',
                        'nav', 'aside', 'footer', 'dialog', 'iframe', 'object', 'embed', 'audio', 'video', 'source']);
  const hidden = el => {
    if (el.closest && el.closest('[hidden]')) return true;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return true;
    // hover cards / tooltips: absolutely positioned and invisible until hovered
    if ((cs.position === 'absolute' || cs.position === 'fixed') && (cs.opacity === '0' || el.matches('[class*=card],[class*=tooltip],[class*=popover]'))) return true;
    if (el.matches('.cite-card, [class*=tooltip], [role=tooltip], .katex-mathml, .sr-only, .visually-hidden')) return true;
    return false;
  };
  const fontPx = el => parseFloat(getComputedStyle(el).fontSize) || 16;
  const bodyFs = (() => { const p = root.querySelector('p'); return p ? fontPx(p) : fontPx(document.body); })();
  const mark = (el, kind, extra) => {
    const n = shots.length;
    el.setAttribute('data-p2e', String(n));
    shots.push(Object.assign({ n, kind }, extra || {}));
    return n;
  };
  // baseline of the line an inline element sits on: a zero-size probe inside it (so it is on
  // the same line even when the element starts a new line) aligned to the baseline
  const baselineY = el => {
    const probe = document.createElement('span');
    probe.style.cssText = 'display:inline-block;width:0;height:0;vertical-align:baseline;';
    // an HTML probe is not laid out inside MathML: put it just before a native <math>
    if (el.namespaceURI === 'http://www.w3.org/1998/Math/MathML') el.parentNode.insertBefore(probe, el);
    else el.insertBefore(probe, el.firstChild);
    const y = probe.getBoundingClientRect().bottom;
    probe.remove();
    return y;
  };
  const texOf = el => {
    const a = el.querySelector('annotation[encoding="application/x-tex"]');
    if (a) return a.textContent.trim();
    const s = el.querySelector('script[type^="math/tex"]');
    if (s) return s.textContent.trim();
    return (el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\s+/g, ' ');
  };
  // MathML -> simple inline HTML, or null when the formula is two-dimensional
  const REL = new Set(['=', '<', '>', '≤', '≥', '≠', '≈', '∼', '→', '←', '∈', '+', '−', '×', '±', '·']);
  const mml = (n, depth) => {
    if (n.nodeType === 3) return esc(n.nodeValue.replace(/\s+/g, ''));
    if (n.nodeType !== 1) return '';
    const t = n.tagName.toLowerCase();
    const kids = [...n.children];
    const join = list => { let out = ''; for (const k of list) { const r = mml(k, depth); if (r === null) return null; out += r; } return out; };
    const txt = n.textContent;
    switch (t) {
      case 'math': case 'mrow': case 'mstyle': case 'mpadded':
        if (t === 'mstyle' && n.getAttribute('displaystyle') === 'true') return null;
        return join(kids);
      case 'semantics': return kids.length ? mml(kids[0], depth) : '';
      case 'mi': {
        const v = n.getAttribute('mathvariant');
        if (v && !['normal', 'italic'].includes(v)) return null;
        if (txt.length === 1 && /[A-Za-z]/.test(txt) && v !== 'normal') return '<i>' + esc(txt) + '</i>';
        return esc(txt);
      }
      case 'mn': case 'mtext': return esc(txt);
      case 'mo': {
        if (/^[⁡-⁤]$/.test(txt)) return '';
        return REL.has(txt) ? ' ' + esc(txt) + ' ' : esc(txt);
      }
      case 'mspace': return ' ';
      case 'msub': case 'msup': {
        if (depth > 0 || kids.length !== 2) return null;
        const b = mml(kids[0], depth), s = mml(kids[1], depth + 1);
        if (b === null || s === null) return null;
        return b + (t === 'msub' ? '<sub>' + s + '</sub>' : '<sup>' + s + '</sup>');
      }
      default: return null;  // mfrac, msqrt, mover (accents), munder, msubsup, mtable, ...
    }
  };
  const mathInline = el => {
    const target = el.querySelector('.katex-html') || el;
    const r = target.getBoundingClientRect();
    const fs = fontPx(el.parentElement || el);
    const base = baselineY(el);
    const m = el.matches('math') ? el : el.querySelector('math');
    const n = mark(target, 'm', { tex: texOf(el), simple: m ? mml(m, 0) : null, fs, h: r.height, w: r.width, descent: r.bottom - base });
    return '[[P2E:' + n + ']]';
  };
  const isMathEl = el => el.matches('.katex, mjx-container, .MathJax, math');
  const isDisplayMath = el => el.matches('.katex-display, mjx-container[display="true"], .MathJax_Display, math[display="block"]');
  const inline = n => {
    if (n.nodeType === 3) return esc(n.nodeValue);
    if (n.nodeType !== 1) return '';
    const el = n, t = el.tagName.toLowerCase();
    if (SKIP.has(t) || hidden(el)) return '';
    if (isMathEl(el)) return mathInline(el);
    if (t === 'svg' || t === 'img' || t === 'canvas') {
      const r = el.getBoundingClientRect();
      if (r.height < 2.2 * fontPx(el.parentElement || el)) return '';  // icons
      const n2 = mark(el, 'fig', { fs: fontPx(document.body), w: r.width, h: r.height, alt: el.getAttribute('alt') || '' });
      return '[[P2E:' + n2 + ']]';
    }
    const inner = joinChildren(el);
    switch (t) {
      case 'a': {
        const href = el.getAttribute('href');
        if (!href || href.startsWith('javascript:')) return inner;
        return '<a href="' + esc(el.href) + '">' + inner + '</a>';
      }
      case 'em': case 'i': case 'cite': case 'var': case 'dfn': return '<i>' + inner + '</i>';
      case 'strong': case 'b': return '<b>' + inner + '</b>';
      case 'code': case 'kbd': case 'samp': case 'tt': return '<span class="mono">' + inner + '</span>';
      case 'sub': return '<sub>' + inner + '</sub>';
      case 'sup': return '<sup>' + inner + '</sup>';
      case 'br': return '<br/>';
      case 'ul': case 'ol': return '<' + t + '>' + [...el.children].filter(c => c.tagName === 'LI' && !hidden(c)).map(c => '<li>' + inline(c).trim() + '</li>').join('') + '</' + t + '>';
      case 'p': case 'div': return inner + ' ';
      default: return inner;
    }
  };
  // children spaced apart by CSS (margins, flex gaps) rather than by text get a space between them
  const rectOf = c => {
    if (c.nodeType === 1) return c.getBoundingClientRect();
    if (c.nodeType === 3 && c.nodeValue.trim()) { const r = document.createRange(); r.selectNodeContents(c); const rs = r.getClientRects(); return rs.length ? rs[rs.length - 1] : null; }
    return null;
  };
  const firstRectOf = c => {
    if (c.nodeType === 1) { const rs = c.getClientRects(); return rs.length ? rs[0] : null; }
    if (c.nodeType === 3 && c.nodeValue.trim()) { const r = document.createRange(); r.selectNodeContents(c); const rs = r.getClientRects(); return rs.length ? rs[0] : null; }
    return null;
  };
  function joinChildren(el) {
    const fs = fontPx(el);
    let out = '', prev = null;
    for (const c of el.childNodes) {
      const piece = inline(c);
      if (!piece) continue;
      const first = firstRectOf(c);
      if (prev && first && out && !/\s$/.test(out) && !/^\s/.test(piece)
          && Math.abs(first.top - prev.top) < 0.6 * fs && first.left - prev.right > 0.25 * fs) out += ' ';
      out += piece;
      prev = rectOf(c) || prev;
    }
    return out;
  }
  const BLOCK_TAGS = new Set(['p', 'div', 'section', 'article', 'header', 'main', 'figure', 'ul', 'ol', 'table', 'pre', 'blockquote',
                              'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'dl', 'details', 'figcaption']);
  const hasBlockChild = el => [...el.children].some(c => BLOCK_TAGS.has(c.tagName.toLowerCase()) || isDisplayMath(c));
  const textLen = el => (el.innerText || '').replace(/\s+/g, '').length;
  const navLike = el => {
    // "← Back to ..." style links
    const a = el.querySelectorAll('a');
    const tx = (el.innerText || '').trim();
    return a.length === 1 && tx.length < 50 && /^[←→«»‹›]|[←→«»‹›]$/.test(tx);
  };
  // a data table (few cell colours) is kept as a real table; a heatmap is a picture
  const plainTable = tbl => {
    const colours = new Set();
    for (const cell of tbl.querySelectorAll('td, th')) {
      const bg = getComputedStyle(cell).backgroundColor;
      if (bg && !/rgba\(0, 0, 0, 0\)|transparent|rgb\(255, 255, 255\)/.test(bg)) colours.add(bg);
    }
    return colours.size <= 3;
  };
  const tableBlock = (tbl, captionEl, id) => {
    const rows = [];
    for (const tr of tbl.querySelectorAll('tr')) {
      if (hidden(tr)) continue;
      const cells = [];
      for (const cell of tr.children) {
        if (!/^T[DH]$/.test(cell.tagName) || hidden(cell)) continue;
        const cs = getComputedStyle(cell);
        cells.push({ th: cell.tagName === 'TH' || cell.closest('thead') !== null, html: joinChildren(cell).trim(),
                     colspan: cell.colSpan || 1, rowspan: cell.rowSpan || 1, right: /right|end/.test(cs.textAlign),
                     // browsers centre <th> by default; keep centring only for spanning group headers and <td>
                     center: /center/.test(cs.textAlign) && (cell.tagName === 'TD' || (cell.colSpan || 1) > 1),
                     italic: cs.fontStyle === 'italic' });
      }
      if (cells.length) rows.push(cells);
    }
    if (captionEl) captionEl.setAttribute('data-p2e-hide', '1');
    blocks.push({ type: 'table', id: id || tbl.id || '', rows, caption: captionEl ? inline(captionEl).trim() : '' });
  };
  const figureShot = (el, captionEl, id) => {
    const cap = captionEl ? inline(captionEl).trim() : '';
    if (captionEl) captionEl.setAttribute('data-p2e-hide', '1');
    const r = el.getBoundingClientRect();
    // alt is the figure's label ("Figure 3"); the caption itself follows as text
    const label = ((captionEl ? captionEl.innerText : '').match(/^\s*((Figure|Fig\.|Table|Algorithm|Listing)\s*[A-Z]?\d+[a-z]?)/i) || [])[1];
    const n = mark(el, 'fig', { fs: bodyFs, w: r.width, h: r.height, alt: label || (el.tagName === 'TABLE' ? 'Table' : 'Figure') });
    blocks.push({ type: 'figure', id, shot: n, caption: cap });
  };
  const walk = el => {
    for (const c of el.children) {
      const t = c.tagName.toLowerCase();
      if (SKIP.has(t) || hidden(c)) continue;
      const id = c.id || '';
      if (/^h[1-6]$/.test(t)) { blocks.push({ type: 'heading', level: +t[1], id, html: inline(c).trim(), text: c.innerText.trim() }); continue; }
      if (isDisplayMath(c) || c.matches('.eq, .equation') && c.querySelector('.katex-display, .katex, mjx-container, math')) {
        const box = c.matches('.katex-display, mjx-container, math') ? c : (c.querySelector('.katex-display, .math, mjx-container, math') || c);
        // the rendered formula itself, not its full-width block
        const holder = box.querySelector('.katex-html, mjx-math') || box.querySelector('.katex') || box;
        const noEl = c.querySelector('.eqno, .eqn-num, .tag');
        const r = holder.getBoundingClientRect();
        const n = mark(holder, 'eq', { tex: texOf(box), fs: fontPx(c), w: r.width, h: r.height });
        blocks.push({ type: 'eq', id, shot: n, eqno: noEl ? noEl.innerText.trim() : '' });
        continue;
      }
      if (t === 'figure') {
        const tbl = c.querySelector('table');
        if (tbl && !c.querySelector('svg, canvas, img, picture') && plainTable(tbl)) { tableBlock(tbl, c.querySelector('figcaption'), id); continue; }
        figureShot(c, c.querySelector('figcaption'), id); continue;
      }
      if (t === 'table') {
        if (plainTable(c)) tableBlock(c, c.querySelector('caption'), id); else figureShot(c, c.querySelector('caption'), id);
        continue;
      }
      if (c.querySelector(':scope > table') && !c.querySelector('svg, canvas, img, picture') && textLen(c) - textLen(c.querySelector(':scope > table')) < 20) {
        const tbl = c.querySelector(':scope > table');  // a scroll wrapper around a table
        if (plainTable(tbl)) tableBlock(tbl, tbl.querySelector('caption'), id || tbl.id); else figureShot(c, tbl.querySelector('caption'), id);
        continue;
      }
      if (t === 'svg' || t === 'img' || t === 'canvas' || t === 'picture') { figureShot(c, null, id); continue; }
      if (t === 'hr') continue;
      if (t === 'pre') { blocks.push({ type: 'pre', id, text: c.innerText.replace(/\s+$/, '') }); continue; }
      if (t === 'ul' || t === 'ol') { blocks.push({ type: 'html', id, html: inline(c) }); continue; }
      if (t === 'blockquote') { blocks.push({ type: 'p', id, cls: 'inset', html: inline(c).trim() }); continue; }
      if (t === 'p' || !hasBlockChild(c)) {
        if (navLike(c)) continue;
        if (!textLen(c) && !c.querySelector('img, svg, canvas, .katex')) continue;
        // a bare graphic container (a plot div) is a figure
        if (t !== 'p' && textLen(c) < 400 && c.querySelector('svg, canvas, img') && c.querySelector('svg, canvas, img').getBoundingClientRect().height > 80) { figureShot(c, null, id); continue; }
        blocks.push({ type: 'p', id, cls: (typeof c.className === 'string' ? c.className : ''), html: inline(c).trim() });
        continue;
      }
      if (id) blocks.push({ type: 'anchor', id });
      walk(c);
    }
  };
  walk(root);
  const metaAll = name => [...document.querySelectorAll('meta[name="' + name + '"]')].map(m => m.content.trim()).filter(Boolean);
  let authors = metaAll('citation_author');
  if (!authors.length) authors = metaAll('author');
  if (!authors.length) {
    const a = root.querySelector('.authors, .author, [class*=byline]');
    if (a) authors = a.innerText.replace(/\s+and\s+/g, ', ').split(/\s*,\s*/).map(s => s.trim()).filter(s => s && s.length < 60);
  }
  const h1 = root.querySelector('h1');
  return { title: (metaAll('citation_title')[0] || (h1 && h1.innerText.trim()) || document.title || '').replace(/\s+/g, ' '),
           authors, lang: document.documentElement.lang || '', url: location.href, blocks, shots };
}
"""

# ---------------------------------------------------------------------------


@dataclass
class WebOptions:
    title: str | None = None
    authors: list[str] | None = None
    language: str | None = None
    width: int = 760  # CSS px of the reading column used for layout and screenshots
    scale: float = 3.0  # device pixel ratio of the screenshots
    cover: bool = True
    root: str | None = None  # CSS selector of the article element (default: main/article)


@dataclass
class WebReport:
    output: Path
    title: str = ""
    authors: list[str] = field(default_factory=list)
    pages: int = 0
    chapters: int = 0
    headings: int = 0
    paragraphs: int = 0
    figures: int = 0
    tables: int = 0
    equations: int = 0
    inline_math_text: int = 0
    inline_math_images: int = 0
    images: int = 0
    seconds: float = 0.0


def find_chromium() -> str | None:
    env = os.environ.get("PDF2EPUB_CHROMIUM")
    if env:
        return env
    hits = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    return hits[-1] if hits else None


def default_web_output(title: str, url: str, directory: Path = Path(".")) -> Path:
    return directory / (paper_stem(title, arxiv_id(url)) + ".epub")


def _page_key(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    path = re.sub(r"(/index)?\.html?$", "", u.path).rstrip("/")
    return f"{u.netloc}{path}"


def _xml_id(raw: str, prefix: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]", "-", raw)
    if not s or not re.match(r"[A-Za-z_]", s):
        s = "x" + s
    return prefix + s


def _safe_text_math(simple: str) -> bool:
    text = html.unescape(re.sub(r"<[^>]+>", "", simple))
    return bool(text.strip()) and all(ch in SAFE_MATH or ch in _TEXT_MATH_EXTRA for ch in text)


class _Book:
    def __init__(self) -> None:
        self.assets: list[ImageAsset] = []
        self.chapters: list[Chapter] = []
        self.toc: list[tuple[int, str, str]] = []
        self.id_file: dict[str, str] = {}  # xml id -> chapter file
        self.page_first_file: dict[str, str] = {}  # page key -> first chapter file
        self.page_prefix: dict[str, str] = {}  # page key -> id prefix

    def add_image(self, png: bytes, kind: str) -> ImageAsset:
        from PIL import Image

        im = Image.open(io.BytesIO(png))
        im = im.convert("RGB") if im.mode in ("RGBA", "P", "LA") else im
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        name = f"images/{kind}-{len(self.assets):04d}.png"
        asset = ImageAsset(name, buf.getvalue(), "image/png", im.width, im.height, 0.0, 0.0, (0.0, 0.0, 0.0, 0.0))
        self.assets.append(asset)
        return asset


def _screenshot(page, n: int, pad: float, viewport_h: int) -> bytes:
    loc = page.locator(f'[data-p2e="{n}"]').first
    page.evaluate("(n) => document.querySelector(`[data-p2e=\"${n}\"]`).querySelectorAll('[data-p2e-hide]').forEach(e => e.style.display = 'none')", n)
    try:
        loc.scroll_into_view_if_needed(timeout=5000)
        bb = loc.bounding_box()
        if bb is None or bb["width"] < 1 or bb["height"] < 1:
            return b""
        if bb["height"] + 2 * pad > viewport_h - 4:
            return loc.screenshot(animations="disabled")
        clip = {"x": max(0.0, bb["x"] - pad), "y": max(0.0, bb["y"] - pad), "width": bb["width"] + 2 * pad, "height": bb["height"] + 2 * pad}
        return page.screenshot(clip=clip, animations="disabled")
    finally:
        page.evaluate("(n) => document.querySelector(`[data-p2e=\"${n}\"]`).querySelectorAll('[data-p2e-hide]').forEach(e => e.style.display = '')", n)


def convert_web(
    urls: list[str] | str,
    epub_path: str | Path | None = None,
    options: WebOptions | None = None,
    progress: Callable[[str], None] | None = None,
) -> WebReport:
    """Convert one web article (or several pages, e.g. an article and its appendix) into one EPUB."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError('web conversion needs Playwright: pip install -e ".[web]"') from exc

    t0 = time.time()
    urls = [urls] if isinstance(urls, str) else list(urls)
    opts = options or WebOptions()
    say = progress or (lambda _m: None)
    book = _Book()
    pages_data: list[dict] = []
    viewport_h = 1600

    with sync_playwright() as pw:
        exe = find_chromium()
        browser = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": opts.width, "height": viewport_h}, device_scale_factor=opts.scale)
        page = ctx.new_page()
        for pi, url in enumerate(urls):
            say(f"rendering {url}")
            page.goto(url, wait_until="networkidle", timeout=120_000)
            page.wait_for_timeout(1500)
            # white page: crops carry no page tint onto the reader's background
            page.add_style_tag(content=(
                "html, body, main, article { background: #fff !important; }"
                # a formula wrapped over two lines has no single box to capture
                " .katex, .katex-html, mjx-container, math { white-space: nowrap !important; }"
            ))
            page.wait_for_timeout(300)
            data = page.evaluate(EXTRACT_JS, {"root": opts.root})
            key = _page_key(data["url"])
            book.page_prefix[key] = "" if pi == 0 else f"p{pi}-"
            say(f"capturing {len(data['shots'])} formula and figure images")
            images: dict[int, tuple[ImageAsset, dict]] = {}
            for shot in data["shots"]:
                if shot["kind"] == "m" and shot.get("simple") is not None and _safe_text_math(shot["simple"]):
                    continue  # stays text
                pad = 1.0 if shot["kind"] == "m" else 4.0
                png = _screenshot(page, shot["n"], pad, viewport_h)
                if png and shot["kind"] in ("eq", "fig"):
                    from PIL import Image

                    before = Image.open(io.BytesIO(png)).size
                    png = _trim(png, round(4 * opts.scale))
                    after = Image.open(io.BytesIO(png)).size
                    # keep the CSS size in step with the trimmed pixels
                    shot["w"] = shot["w"] * after[0] / before[0]
                    shot["h"] = shot["h"] * after[1] / before[1]
                if png:
                    images[shot["n"]] = (book.add_image(png, {"m": "m", "eq": "eq"}.get(shot["kind"], "fig")), shot)
            cover = None
            if pi == 0 and opts.cover:
                cover_png = page.screenshot(clip={"x": 0, "y": 0, "width": opts.width, "height": round(opts.width * 1.5)})
                cover = book.add_image(cover_png, "cover")
                book.assets.remove(cover)
                cover.name = "images/cover.png"
                data["_cover"] = cover
            data["_images"] = images
            data["_key"] = key
            pages_data.append(data)
        browser.close()

    first = pages_data[0]
    title = opts.title or first["title"] or "Untitled"
    authors = opts.authors or first["authors"]
    language = opts.language or (first["lang"] or "en").split("-")[0]
    report = WebReport(output=Path("."), title=title, authors=authors, pages=len(pages_data))

    # --- build chapters
    for pi, data in enumerate(pages_data):
        _build_page(book, data, pi, report)
    _fix_links(book, pages_data)

    out = Path(epub_path) if epub_path else default_web_output(title, urls[0])
    report.output = out
    ident = "urn:pdf2epub:web:" + hashlib.sha1("|".join(urls).encode()).hexdigest()
    say("writing EPUB")
    write_epub(out, title, authors, language, book.chapters, book.assets, book.toc, first.get("_cover"), ident,
               description=f"Converted from {urls[0]}", extra_css=WEB_CSS)
    report.chapters = len(book.chapters)
    report.images = len(book.assets)
    report.seconds = time.time() - t0
    return report


def _img_tag(asset: ImageAsset, shot: dict, kind: str) -> str:
    fs = shot.get("fs") or 16.0
    if kind == "m":
        h, w = shot["h"] + 2.0, shot["w"] + 2.0  # 1 px padding each side
        valign = -(shot["descent"] + 1.0) / fs
        alt = html.escape(shot.get("tex", ""), quote=True)
        return f'<img class="m" src="../{asset.name}" alt="{alt}" style="height:{h / fs:.3f}em;width:{w / fs:.3f}em;vertical-align:{valign:.3f}em"/>'
    cls = "eq" if kind == "eq" else "fig"
    w = shot["w"]
    alt = html.escape(shot.get("tex") or shot.get("alt") or "", quote=True)
    return f'<img class="{cls}" src="../{asset.name}" alt="{alt}" style="width:{w / fs:.2f}em"/>'


_P2E = re.compile(r"\[\[P2E:(\d+)\]\]")

WEB_CSS = """
div.table { margin: 1em 0 0.9em 0; overflow-x: auto; }
table.data.wide { font-size: 0.68em; }
table.data.wide th, table.data.wide td { padding: 0.15em 0.25em; }
table.data { border-collapse: collapse; margin: 0.3em auto 0 auto; font-size: 0.82em; line-height: 1.3; }
table.data th, table.data td { padding: 0.18em 0.45em; border-bottom: 1px solid #bbbbbb; text-align: left; vertical-align: top; }
table.data th { font-weight: bold; border-bottom: 1px solid #555555; }
table.data td.r, table.data th.r { text-align: right; }
table.data td.c, table.data th.c { text-align: center; }
div.table p.caption { text-align: left; }
span.eqno { white-space: nowrap; }
"""


def _trim(png: bytes, pad_px: int) -> bytes:
    """Cut uniform background margins off a screenshot, keeping ``pad_px`` around the ink."""
    from PIL import Image, ImageChops

    im = Image.open(io.BytesIO(png)).convert("RGB")
    bg = Image.new("RGB", im.size, im.getpixel((0, 0)))
    diff = ImageChops.difference(im, bg).convert("L").point(lambda v: 255 if v > 18 else 0)
    box = diff.getbbox()
    if not box:
        return png
    box = (max(0, box[0] - pad_px), max(0, box[1] - pad_px), min(im.width, box[2] + pad_px), min(im.height, box[3] + pad_px))
    if box == (0, 0, im.width, im.height):
        return png
    buf = io.BytesIO()
    im.crop(box).save(buf, "PNG")
    return buf.getvalue()


def _build_page(book: _Book, data: dict, pi: int, report: WebReport) -> None:
    prefix = "" if pi == 0 else f"p{pi}-"
    images: dict[int, tuple[ImageAsset, dict]] = data["_images"]
    shots = {s["n"]: s for s in data["shots"]}
    title_level = min((b["level"] for b in data["blocks"] if b["type"] == "heading"), default=1)
    chapter_level = min((b["level"] for b in data["blocks"] if b["type"] == "heading" and b["level"] > title_level), default=title_level + 1)
    current: Chapter | None = None
    has_heading = False
    pending: list[str] = []

    def new_chapter(title: str) -> Chapter:
        ch = Chapter(f"text/ch{len(book.chapters):03d}.xhtml", title)
        book.chapters.append(ch)
        book.page_first_file.setdefault(data["_key"], ch.file)
        return ch

    def fill(fragment: str) -> str:
        def sub(m: re.Match) -> str:
            n = int(m.group(1))
            if n in images:
                asset, shot = images[n]
                if shot["kind"] == "m":
                    report.inline_math_images += 1
                return _img_tag(asset, shot, "m" if shot["kind"] == "m" else "fig")
            shot = shots.get(n, {})
            if shot.get("kind") == "m" and shot.get("simple") is not None:
                report.inline_math_text += 1
                return shot["simple"].strip()
            return ""
        return _P2E.sub(sub, fragment)

    def ident(raw: str) -> str:
        if not raw:
            return ""
        xid = _xml_id(raw, prefix)
        book.id_file[xid] = current.file if current else ""
        return xid

    def flush_pending() -> None:
        while pending:
            x = ident(pending.pop(0))
            current.body.append(f'<div id="{x}"></div>')

    def id_attr(raw: str) -> str:
        x = ident(raw)
        return f' id="{x}"' if x else ""

    for b in data["blocks"]:
        kind = b["type"]
        if kind == "heading":
            level = b["level"]
            if current is None or (level <= chapter_level and has_heading):
                current = new_chapter(b["text"])
            elif not has_heading:
                current.title = b["text"]  # preamble (a site label, a date) stays with the title
            has_heading = True
            flush_pending()
            hid = _xml_id(b["id"] or f"h{len(book.toc)}", prefix)
            book.id_file[hid] = current.file
            html_level = 1 if level == title_level else min(6, 2 + level - chapter_level)
            cls = ' class="title"' if level == title_level and pi == 0 else ""
            current.body.append(f'<h{html_level}{cls} id="{hid}">{fill(b["html"])}</h{html_level}>')
            # TOC: the article's sections at the top level; a later page's sections nest under its title
            toc_level = 1 if level == title_level else level - chapter_level + 1 + (1 if pi else 0)
            book.toc.append((toc_level, b["text"], f"{current.file}#{hid}"))
            report.headings += 1
            continue
        if current is None:
            current = new_chapter(data["title"] or "Start")
        if kind != "anchor":
            flush_pending()
        if kind == "anchor":
            if b["id"]:
                pending.append(b["id"])  # a section's id belongs to its first block (usually its heading)
            continue
        if kind == "p":
            content = re.sub(r"(\s*[·•|]\s*)+$", "", fill(b["html"]).strip())
            content = re.sub(r"\s{2,}", " ", content)
            if not content:
                continue
            cls = []
            raw_cls = b.get("cls", "") or ""
            if re.search(r"bib|reference", raw_cls):
                cls.append("bib")
            elif re.search(r"author|meta|byline|label|date", raw_cls):
                cls.append("center")
            elif b.get("cls") == "inset":
                cls.append("inset")
            c = f' class="{" ".join(cls)}"' if cls else ""
            current.body.append(f"<p{c}{id_attr(b['id'])}>{content}</p>")
            report.paragraphs += 1
        elif kind == "html":
            frag = fill(b["html"])
            frag = re.sub(r"<ol>", '<ul style="list-style-type:decimal">', frag).replace("</ol>", "</ul>")
            current.body.append(f'<div{id_attr(b["id"])}>{frag}</div>' if b["id"] else frag)
        elif kind == "pre":
            lines = [html.escape(x).replace(" ", " ") or " " for x in b["text"].split("\n")]
            current.body.append(f'<p class="code"{id_attr(b["id"])}>' + "<br/>".join(lines) + "</p>")
        elif kind == "eq":
            if b["shot"] not in images:
                continue
            asset, shot = images[b["shot"]]
            no = f' <span class="eqno">{html.escape(b["eqno"])}</span>' if b.get("eqno") else ""
            current.body.append(f'<div class="eq"{id_attr(b["id"])}>{_img_tag(asset, shot, "eq")}{no}</div>')
            report.equations += 1
        elif kind == "table":
            rows = []
            for r in b["rows"]:
                cells = []
                for c in r:
                    tag = "th" if c["th"] else "td"
                    attrs = ""
                    if c["colspan"] > 1:
                        attrs += f' colspan="{c["colspan"]}"'
                    if c["rowspan"] > 1:
                        attrs += f' rowspan="{c["rowspan"]}"'
                    if c["right"] or c["center"]:
                        attrs += f' class="{"r" if c["right"] else "c"}"'
                    body = fill(c["html"]).strip()
                    cells.append(f"<{tag}{attrs}>{'<i>' + body + '</i>' if c.get('italic') and body else body}</{tag}>")
                rows.append("<tr>" + "".join(cells) + "</tr>")
            cap = fill(b.get("caption", "")).strip()
            cap_html = f'<p class="caption">{cap}</p>' if cap else ""
            ncols = max((sum(c["colspan"] for c in r) for r in b["rows"]), default=0)
            tcls = "data wide" if ncols >= 7 else "data"  # many columns: compact so they fit a phone
            current.body.append(f'<div class="table"{id_attr(b["id"])}><table class="{tcls}">{"".join(rows)}</table>{cap_html}</div>')
            report.tables += 1
        elif kind == "figure":
            if b["shot"] not in images:
                continue
            asset, shot = images[b["shot"]]
            cap = fill(b.get("caption", "")).strip()
            cap_html = f'<p class="caption">{cap}</p>' if cap else ""
            current.body.append(f'<div class="figure"{id_attr(b["id"])}><div class="img">{_img_tag(asset, shot, "fig")}</div>{cap_html}</div>')
            report.figures += 1


def _fix_links(book: _Book, pages_data: list[dict]) -> None:
    keys = {d["_key"]: i for i, d in enumerate(pages_data)}

    def resolve(href: str, here: str) -> str:
        u = urllib.parse.urlsplit(html.unescape(href))
        if u.scheme not in ("http", "https", "file", ""):
            return href
        key = _page_key(urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, "", "")))
        if key not in keys:
            return href
        prefix = "" if keys[key] == 0 else f"p{keys[key]}-"
        if u.fragment:
            xid = _xml_id(urllib.parse.unquote(u.fragment), prefix)
            target = book.id_file.get(xid)
            if target:
                return (f"#{xid}" if target == here else f"{Path(target).name}#{xid}")
        first = book.page_first_file.get(key)
        return Path(first).name if first else href

    for ch in book.chapters:
        ch.body = [re.sub(r'href="([^"]*)"', lambda m, here=ch.file: f'href="{html.escape(resolve(m.group(1), here), quote=True)}"', part)
                   for part in ch.body]
        _check_xml(ch)  # every chapter must be well-formed XHTML


def _check_xml(ch: Chapter) -> None:
    body = "\n".join(ch.body)
    try:
        ET.fromstring(f"<body>{body}</body>")
    except ET.ParseError as exc:  # pragma: no cover - would be a bug in the walker
        raise ValueError(f"{ch.file}: generated XHTML is not well formed: {exc}") from exc
