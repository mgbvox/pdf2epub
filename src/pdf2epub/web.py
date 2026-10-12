"""Convert a web article (an HTML page) into the same Kindle-friendly EPUB as a PDF.

A web article already has what the PDF pipeline has to reconstruct: headings,
paragraphs, lists, links, and the TeX source of every formula. So instead of
printing it to a PDF, the page is rendered in headless Chromium (so client-side
math such as KaTeX or MathJax runs), its article DOM is walked, and:

* text, headings, lists, links and citations become real XHTML text; site
  chrome (navigation, sidebars, tables of contents, edit links, buttons, hover
  cards, anything hidden on screen or in print) is dropped;
* inline math stays text when it is a simple linear formula built from
  Kindle-safe characters, and otherwise becomes a high-resolution screenshot of
  the rendered formula, aligned to the text baseline, with its TeX as alt text;
* display equations and figures (SVG, canvas, images) become trimmed
  screenshots, with captions and equation numbers kept as text; plain data
  tables become real tables, colour-coded ones (heatmaps) stay pictures;
* internal links (references, sections, figures, notes) are rewritten to the
  chapter files, also across several pages converted into one book (e.g. an
  article and its appendix page).

Pages can also be converted from MHTML snapshots (:func:`snapshot_pages`), which
is how the web corpus in ``demo/web`` stays reproducible.

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

NBSP = " "
# characters that may stay as text in simple inline math (besides SAFE_MATH)
_TEXT_MATH_EXTRA = set("   −×·≤≥≠≈±∓∞→←⇒↦≡∝′ℓ")

# ---------------------------------------------------------------------------
# in-page extraction (runs in Chromium)

# Site chrome that is never article content (Wikipedia, Distill, common blog themes).
SKIP_SELECTOR = ", ".join([
    ".mw-editsection", ".navbox", ".vertical-navbox", ".sidebar", ".ambox", ".metadata", ".noprint",
    ".authority-control", ".catlinks", "#siteSub", "#contentSub", ".mw-jump-link", ".hatnote",
    ".shortdescription", ".mw-empty-elt", ".toc", "#toc", ".table-of-contents", "#table-of-contents",
    "details.toc", "dt-header", "distill-header", "dt-footer", "distill-footer", "d-byline", "dt-byline",
    ".cite-card", "[class*=tooltip]", "[role=tooltip]", ".katex-mathml", ".sr-only", ".visually-hidden",
    "[data-p2e-chrome]",
])

PRINT_HIDDEN_JS = r"""
() => {
  // elements a site hides when printing are chrome (menus, edit links, share bars); keep any that
  // hold figures, math or tables, and give up if printing would hide most of the text
  const root = document.body;
  const total = (root.innerText || '').length || 1;
  let hiddenText = 0;
  const marked = [];
  for (const el of document.querySelectorAll('body *')) {
    if (getComputedStyle(el).display !== 'none') continue;
    if (el.closest('[data-p2e-printhidden]')) continue;
    if (el.querySelector('svg, canvas, img, math, table, figure, .katex, mjx-container, h1, h2, h3')) continue;
    marked.push(el);
  }
  for (const el of marked) { el.setAttribute('data-p2e-printhidden', '1'); }
  return marked.length;
}
"""

UNMARK_PRINT_JS = r"""
(screenHidden) => {
  // what is hidden on screen anyway does not count; undo everything if most text would go
  const els = [...document.querySelectorAll('[data-p2e-printhidden]')];
  let txt = 0;
  for (const el of els) txt += (el.innerText || el.textContent || '').length;
  const total = (document.body.innerText || '').length || 1;
  if (txt > 0.4 * total) { els.forEach(e => e.removeAttribute('data-p2e-printhidden')); return 0; }
  els.forEach(e => e.setAttribute('data-p2e-chrome', '1'));
  return els.length;
}
"""

EXTRACT_JS = r"""
(opts) => {
  const pick = sel => sel && document.querySelector(sel);
  const root = pick(opts.root) || pick('.mw-parser-output') || pick('main article') || pick('article')
    || pick('d-article') || pick('dt-article') || pick('main') || pick('[role=main]') || document.body;
  const roots = [root];
  if (root.matches('d-article, dt-article')) roots.push(...document.querySelectorAll('d-appendix, dt-appendix'));
  const shots = [];
  const blocks = [];
  const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const SKIP = new Set(['script', 'style', 'noscript', 'template', 'button', 'input', 'select', 'textarea', 'form',
                        'nav', 'aside', 'footer', 'dialog', 'iframe', 'object', 'embed', 'audio', 'video', 'source']);
  const SKIP_SEL = opts.skip;
  const hidden = el => {
    if (el.matches(SKIP_SEL)) return true;
    if (el.closest && el.closest('[hidden]')) return true;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return true;
    // hover cards / tooltips: absolutely positioned and invisible until hovered
    if ((cs.position === 'absolute' || cs.position === 'fixed') && (cs.opacity === '0' || el.matches('[class*=card],[class*=popover]'))) return true;
    return false;
  };
  const fontPx = el => parseFloat(getComputedStyle(el).fontSize) || 16;
  // body text size: the most common font size of the article's paragraphs
  const bodyFs = (() => {
    const count = new Map();
    for (const p of root.querySelectorAll('p')) {
      const n = (p.innerText || '').length;
      if (n < 40) continue;
      const f = fontPx(p);
      count.set(f, (count.get(f) || 0) + n);
    }
    let best = fontPx(document.body), bestN = 0;
    for (const [f, n] of count) if (n > bestN) { best = f; bestN = n; }
    return best;
  })();
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
    // an HTML probe is not laid out inside MathML or replaced elements: put it just before them
    if (el.namespaceURI === 'http://www.w3.org/1998/Math/MathML' || /^(IMG|SVG|CANVAS)$/i.test(el.tagName)) el.parentNode.insertBefore(probe, el);
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
    const m = el.matches('math') ? el : el.querySelector('math');
    if (m && m.getAttribute('alttext')) return m.getAttribute('alttext').trim();
    try {
      const items = window.MathJax && MathJax.startup && MathJax.startup.document && MathJax.startup.document.getMathItemsWithin(el);
      if (items && items.length && typeof items[0].math === 'string') return items[0].math.trim();
    } catch (e) { /* not MathJax 3 */ }
    const img = el.querySelector('img[alt]');
    if (img) return img.getAttribute('alt').trim();
    return (el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\s+/g, ' ');
  };
  // MathML -> simple inline HTML, or null when the formula is two-dimensional
  const REL = new Set(['=', '<', '>', '≤', '≥', '≠', '≈', '∼', '→', '←', '∈', '⇒', '↦', '≡', '∝', ':=']);
  const BIN = new Set(['+', '−', '-', '×', '±', '∓', '·']);
  const NB = ' ';
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
        const prev = n.previousElementSibling, next = n.nextElementSibling;
        if (txt === '⁡') return NB;                       // function application: ln V
        if (/^[⁢-⁤]$/.test(txt)) return '';
        if (txt === ',' || txt === ';') {
          // a list comma is followed by a space; 1{,}024 is marked rspace="0em" (or braced alone)
          const tight = /^0(em|ex|px)?$/.test(n.getAttribute('rspace') || '') || n.parentElement.children.length === 1;
          return tight ? txt : txt + NB;
        }
        if (REL.has(txt)) return prev && next ? NB + esc(txt) + NB : esc(txt);
        if (BIN.has(txt)) {
          // binary only between two operands: a leading minus or a postfix "120×" gets no spaces
          const operand = prev && !(prev.tagName.toLowerCase() === 'mo' && !/^[)\]}|]$/.test(prev.textContent));
          return operand && next ? NB + esc(txt) + NB : esc(txt);
        }
        return esc(txt);
      }
      case 'mspace': return NB;
      case 'msub': case 'msup': {
        if (depth > 0 || kids.length !== 2) return null;
        const b = mml(kids[0], depth), s = mml(kids[1], depth + 1);
        if (b === null || s === null) return null;
        return b + (t === 'msub' ? '<sub>' + s + '</sub>' : '<sup>' + s + '</sup>');
      }
      default: return null;  // mfrac, msqrt, mover (accents), munder, msubsup, mtable, ...
    }
  };
  const MATH_SEL = '.katex, mjx-container, .MathJax, math, .mwe-math-element';
  const DISPLAY_SEL = '.katex-display, mjx-container[display="true"], .MathJax_Display, math[display="block"], dt-math[block], d-math[block], .mwe-math-element-block';
  const isMathEl = el => el.matches(MATH_SEL);
  const isDisplayMath = el => el.matches(DISPLAY_SEL) || (el.matches('dt-math, d-math') && el.querySelector('.katex-display'));
  const mathTarget = el => el.querySelector('img.mwe-math-fallback-image-inline, img.mwe-math-fallback-image-display')
    || el.querySelector('.katex-html, mjx-math') || el;
  const mathInline = el => {
    const target = mathTarget(el);
    const r = target.getBoundingClientRect();
    const fs = fontPx(el.parentElement || el);
    const base = baselineY(el.matches('.mwe-math-element') ? target : el);
    const m = el.matches('math') ? el : el.querySelector('math');
    const n = mark(target, 'm', { tex: texOf(el), simple: m ? mml(m, 0) : null, fs, mfs: fontPx(target.closest('.katex, mjx-container') || el),
                                   h: r.height, w: r.width, descent: r.bottom - base });
    return '[[P2E:' + n + ']]';
  };
  const eqShot = (box, noEl) => {
    const holder = box.querySelector('img.mwe-math-fallback-image-display') || box.querySelector('.katex-html, mjx-math') || box.querySelector('.katex') || box;
    const r = holder.getBoundingClientRect();
    return mark(holder, 'eq', { tex: texOf(box), fs: bodyFs, w: r.width, h: r.height, eqno: noEl ? noEl.innerText.trim() : '' });
  };
  const idMark = el => (el.id ? '[[P2EID:' + esc(el.id) + ']]' : '');
  const inline = n => {
    if (n.nodeType === 3) return esc(n.nodeValue);
    if (n.nodeType !== 1) return '';
    const el = n, t = el.tagName.toLowerCase();
    if (SKIP.has(t) || hidden(el)) return '';
    if (isDisplayMath(el)) {
      // a display equation inside running text: the paragraph is split around it
      const box = el.matches('.katex-display, mjx-container, math, .mwe-math-element') ? el : (el.querySelector('.katex-display, mjx-container, math') || el);
      return '[[P2EBLOCK:' + eqShot(box, el.querySelector('.eqno, .eqn-num, .tag')) + ']]';
    }
    if (isMathEl(el)) return idMark(el) + mathInline(el);
    if (t === 'svg' || t === 'img' || t === 'canvas') {
      const r = el.getBoundingClientRect(), fs = fontPx(el.parentElement || el);
      if (r.height < 2.2 * fs && r.width < 2.2 * fs) return '';  // icons
      if (r.height < 2.2 * fs) {                                  // a small inline graphic: keep it on the line
        const n2 = mark(el, 'm', { tex: el.getAttribute('alt') || '', simple: null, fs, mfs: fs, h: r.height, w: r.width, descent: r.bottom - baselineY(el) });
        return '[[P2E:' + n2 + ']]';
      }
      const n3 = mark(el, 'fig', { fs: bodyFs, w: r.width, h: r.height, alt: el.getAttribute('alt') || 'Figure', raster: t === 'img' });
      return '[[P2E:' + n3 + ']]';
    }
    if (t === 'ul' || t === 'ol') {
      const items = [...el.children].filter(c => c.tagName === 'LI' && !hidden(c)).map(c => '<li>' + idMark(c) + joinChildren(c).trim() + '</li>').join('');
      if (t === 'ol') {
        const lst = getComputedStyle(el).listStyleType;
        const start = el.start && el.start !== 1 ? ' start="' + el.start + '"' : '';
        return '<ol style="list-style-type:' + esc(lst || 'decimal') + '"' + start + '>' + items + '</ol>';
      }
      return '<ul>' + items + '</ul>';
    }
    const inner = joinChildren(el);
    switch (t) {
      case 'a': {
        const href = el.getAttribute('href');
        if (!inner.replace(/<[^>]+>/g, '').trim() && !/<img/.test(inner)) return idMark(el);
        if (!href || href.startsWith('javascript:')) return idMark(el) + inner;
        return idMark(el) + '<a href="' + esc(el.href) + '">' + inner + '</a>';
      }
      case 'em': case 'i': case 'cite': case 'var': case 'dfn': return idMark(el) + '<i>' + inner + '</i>';
      case 'strong': case 'b': return idMark(el) + '<b>' + inner + '</b>';
      case 'code': case 'kbd': case 'samp': case 'tt': return '<span class="mono">' + inner + '</span>';
      case 'sub': return '<sub>' + inner + '</sub>';
      case 'sup': return idMark(el) + '<sup>' + inner + '</sup>';
      case 'br': return '<br/>';
      case 'p': case 'div': case 'dd': case 'dt': case 'li': return idMark(el) + inner + ' ';
      default: return idMark(el) + inner;
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
                              'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'dl', 'details', 'figcaption', 'd-figure', 'dt-figure']);
  const hasBlockChild = el => [...el.children].some(c => BLOCK_TAGS.has(c.tagName.toLowerCase()) || isDisplayMath(c));
  const textLen = el => (el.innerText || '').replace(/\s+/g, '').length;
  const navLike = el => {
    const a = el.querySelectorAll('a');
    const tx = (el.innerText || '').trim();
    if (a.length !== 1 || tx.length > 60) return false;
    if (/^[←→«»‹›]|[←→«»‹›]$/.test(tx)) return true;   // "← Back to ..."
    // a lone link to the site's home page (a site label above the title)
    try { const u = new URL(a[0].href); return (u.pathname === '/' || u.pathname === '') && a[0].innerText.trim() === tx; } catch (e) { return false; }
  };
  // a list whose links all jump to headings on this page is the site's own table of contents
  const tocLike = el => {
    const links = [...el.querySelectorAll('a[href^="#"]')];
    if (links.length < 3 || links.length < el.querySelectorAll('a').length) return false;
    const ids = new Set([...root.querySelectorAll('h1[id], h2[id], h3[id], h4[id], section[id]')].map(h => h.id));
    return links.every(a => ids.has(decodeURIComponent(a.getAttribute('href').slice(1))));
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
  const ruled = (cell, side) => {
    const cs = getComputedStyle(cell);
    return parseFloat(cs['border' + side + 'Width']) > 0 && cs['border' + side + 'Style'] !== 'none';
  };
  const tableBlock = (tbl, captionEl, id) => {
    const rows = [];
    for (const tr of tbl.querySelectorAll('tr')) {
      if (hidden(tr)) continue;
      const cells = [];
      let top = false, bottom = false;
      for (const cell of tr.children) {
        if (!/^T[DH]$/.test(cell.tagName) || hidden(cell)) continue;
        const cs = getComputedStyle(cell);
        top = top || ruled(cell, 'Top') || ruled(tr, 'Top');
        bottom = bottom || ruled(cell, 'Bottom') || ruled(tr, 'Bottom');
        cells.push({ th: cell.tagName === 'TH' || cell.closest('thead') !== null, html: joinChildren(cell).trim(),
                     colspan: cell.colSpan || 1, rowspan: cell.rowSpan || 1, right: /right|end/.test(cs.textAlign),
                     // browsers centre <th> by default; keep centring only for spanning group headers and <td>
                     center: /center/.test(cs.textAlign) && (cell.tagName === 'TD' || (cell.colSpan || 1) > 1),
                     italic: cs.fontStyle === 'italic', bold: parseInt(cs.fontWeight, 10) >= 600 && cell.tagName === 'TD' });
      }
      if (cells.length) rows.push({ cells, top, bottom });
    }
    let capFirst = false;
    if (captionEl) {
      captionEl.setAttribute('data-p2e-hide', '1');
      capFirst = captionEl.tagName === 'CAPTION' ? getComputedStyle(captionEl).captionSide !== 'bottom'
        : !!(captionEl.compareDocumentPosition(tbl) & Node.DOCUMENT_POSITION_FOLLOWING);
    }
    blocks.push({ type: 'table', id: id || tbl.id || '', rows, caption: captionEl ? inline(captionEl).trim() : '', capFirst });
  };
  const figureShot = (el, captionEl, id) => {
    const cap = captionEl ? inline(captionEl).trim() : '';
    if (captionEl) captionEl.setAttribute('data-p2e-hide', '1');
    const r = el.getBoundingClientRect();
    // alt is the figure's label ("Figure 3"); the caption itself follows as text
    const label = ((captionEl ? captionEl.innerText : '').match(/^\s*((Figure|Fig\.|Table|Algorithm|Listing)\s*[A-Z]?\d+[a-z]?)/i) || [])[1];
    const imgs = el.querySelectorAll('img'), vec = el.querySelectorAll('svg, canvas');
    const n = mark(el, 'fig', { fs: bodyFs, w: r.width, h: r.height, alt: label || (el.tagName === 'TABLE' ? 'Table' : 'Figure'),
                                raster: imgs.length > 0 && vec.length === 0 });
    blocks.push({ type: 'figure', id, shot: n, caption: cap });
  };
  const walk = el => {
    for (const c of el.children) {
      const t = c.tagName.toLowerCase();
      if (SKIP.has(t) || hidden(c)) continue;
      const id = c.id || '';
      if (/^h[1-6]$/.test(t)) { blocks.push({ type: 'heading', level: +t[1], id, html: joinChildren(c).trim() }); continue; }
      if (isDisplayMath(c) || c.matches('.eq, .equation') && c.querySelector('.katex-display, .katex, mjx-container, math')) {
        const box = c.matches('.katex-display, mjx-container, math') ? c : (c.querySelector('.katex-display, .math, mjx-container, math') || c);
        const n = eqShot(box, c.querySelector('.eqno, .eqn-num, .tag'));
        blocks.push({ type: 'eq', id, shot: n });
        continue;
      }
      if (t === 'figure' || t === 'd-figure' || t === 'dt-figure') {
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
      if (t === 'ul' || t === 'ol') { if (!tocLike(c)) blocks.push({ type: 'html', id, html: inline(c) }); continue; }
      if (t === 'blockquote') { blocks.push({ type: 'p', id, cls: 'inset', html: joinChildren(c).trim() }); continue; }
      if (t === 'p' || !hasBlockChild(c)) {
        if (navLike(c) || tocLike(c)) continue;
        if (!textLen(c) && !c.querySelector('img, svg, canvas, .katex, math')) continue;
        // a bare graphic container (a plot div) is a figure
        const g = c.querySelector('svg, canvas, img');
        if (t !== 'p' && textLen(c) < 400 && g && g.getBoundingClientRect().height > 80 && !c.querySelector(MATH_SEL)) { figureShot(c, null, id); continue; }
        blocks.push({ type: 'p', id, cls: (typeof c.className === 'string' ? c.className : ''), html: joinChildren(c).trim() });
        continue;
      }
      if (id) blocks.push({ type: 'anchor', id });
      walk(c);
    }
  };
  roots.forEach(walk);
  const metaAll = name => [...document.querySelectorAll('meta[name="' + name + '"]')].map(m => m.content.trim()).filter(Boolean);
  let authors = metaAll('citation_author');
  // "Last, First" (citation_author convention) -> "First Last"
  if (authors.length && authors.every(a => /^[^,]+,\s*[^,]+$/.test(a))) authors = authors.map(a => a.split(/,\s*/).reverse().join(' '));
  if (!authors.length) authors = metaAll('author');
  if (!authors.length) {
    const a = root.querySelector('.authors, .author, [class*=byline]');
    if (a) authors = a.innerText.replace(/\s+and\s+/g, ', ').split(/\s*,\s*/).map(s => s.trim()).filter(s => s && s.length < 60);
  }
  const h1 = root.querySelector('h1') || document.querySelector('h1');
  // reference text for fidelity checks: the article's visible text without its chrome
  const ref = (() => {
    const parts = [];
    for (const r of roots) {
      const c = r.cloneNode(true);
      c.querySelectorAll('nav, aside, footer, button, script, style, noscript, svg, [hidden], [data-p2e-chrome], ' + SKIP_SEL).forEach(e => e.remove());
      parts.push(c.innerText || c.textContent || '');
    }
    return parts.join('\n');
  })();
  return { title: (metaAll('citation_title')[0] || (h1 && h1.textContent.trim()) || document.title || '').replace(/\s+/g, ' '),
           authors, lang: document.documentElement.lang || '', url: location.href, blocks, shots, reference: ref };
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
    canonical_urls: list[str] | None = None  # original URLs when converting snapshots (for cross-page links)
    cover_figure: str | int | None = "auto"  # figure number, label, "auto" or "page" (top of the page)


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
    reference_text: str = ""  # the pages' visible article text, for fidelity checks


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


def _launch(pw):
    exe = find_chromium()
    return pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()


def _load(page, url: str) -> None:
    # "networkidle" never arrives on pages that keep a connection open; wait for the load event,
    # then give late scripts (math typesetting, plots) a short grace period
    page.goto(url, wait_until="load", timeout=120_000)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(1500)


def snapshot_pages(urls: list[str], out_paths: list[Path], width: int = 760) -> None:
    """Save each rendered page (scripts already run, so math is typeset) as an MHTML snapshot."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_context(viewport={"width": width, "height": 1600}).new_page()
        for url, out in zip(urls, out_paths):
            _load(page, url)
            cdp = page.context.new_cdp_session(page)
            data = cdp.send("Page.captureSnapshot", {"format": "mhtml"})["data"]
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(data, encoding="utf-8")
        browser.close()


class _Book:
    def __init__(self) -> None:
        self.assets: list[ImageAsset] = []
        self.chapters: list[Chapter] = []
        self.toc: list[tuple[int, str, str]] = []
        self.id_file: dict[str, str] = {}  # xml id -> chapter file
        self.page_first_file: dict[str, str] = {}  # page key -> first chapter file

    def add_image(self, png: bytes, kind: str, photo: bool = False) -> ImageAsset:
        from PIL import Image

        im = Image.open(io.BytesIO(png))
        im = im.convert("RGB") if im.mode in ("RGBA", "P", "LA") else im
        buf = io.BytesIO()
        # photographs compress far better as JPEG; plots, formulas and tables stay lossless
        if photo and im.resize((min(im.width, 200), min(im.height, 200))).getcolors(6000) is None:  # > 6000 colours
            im.save(buf, "JPEG", quality=88, optimize=True)
            ext, mt = "jpg", "image/jpeg"
        else:
            im.save(buf, "PNG", optimize=True)
            ext, mt = "png", "image/png"
        name = f"images/{kind}-{len(self.assets):04d}.{ext}"
        asset = ImageAsset(name, buf.getvalue(), mt, im.width, im.height, 0.0, 0.0, (0.0, 0.0, 0.0, 0.0))
        self.assets.append(asset)
        return asset


_TOGGLE_HIDE_JS = """([n, value]) => {
  const el = document.querySelector(`[data-p2e="${n}"]`);
  if (el) el.querySelectorAll('[data-p2e-hide]').forEach(e => e.style.display = value);
  return !!el;
}"""


def _screenshot(page, n: int, pad: float, viewport: dict) -> bytes:
    if not page.evaluate(_TOGGLE_HIDE_JS, [n, "none"]):
        return b""
    try:
        loc = page.locator(f'[data-p2e="{n}"]').first
        loc.scroll_into_view_if_needed(timeout=5000)
        bb = loc.bounding_box()
        if bb is None or bb["width"] < 1 or bb["height"] < 1:
            return b""
        too_big = bb["height"] + 2 * pad > viewport["height"] - 4
        off_screen = bb["x"] - pad < 0 or bb["x"] + bb["width"] + pad > viewport["width"]
        if too_big or off_screen:
            return loc.screenshot(animations="disabled")  # captures beyond the viewport
        clip = {"x": bb["x"] - pad, "y": max(0.0, bb["y"] - pad), "width": bb["width"] + 2 * pad, "height": bb["height"] + 2 * pad}
        return page.screenshot(clip=clip, animations="disabled")
    except Exception:  # an element that cannot be captured is left out rather than failing the book
        return b""
    finally:
        page.evaluate(_TOGGLE_HIDE_JS, [n, ""])


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


PAGE_CSS = (
    "html, body, main, article { background: #fff !important; }"
    # a formula wrapped over two lines has no single box to capture
    " .katex, .katex-html, mjx-container, math { white-space: nowrap !important; }"
    # formulas in grey captions would look faded next to the book's black caption text
    " .katex, mjx-container { color: #000 !important; }"
)


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
    viewport = {"width": opts.width, "height": 1600}

    with sync_playwright() as pw:
        browser = _launch(pw)
        ctx = browser.new_context(viewport=viewport, device_scale_factor=opts.scale)
        page = ctx.new_page()
        for pi, url in enumerate(urls):
            say(f"rendering {url}")
            _load(page, url)
            page.emulate_media(media="print")
            page.evaluate(PRINT_HIDDEN_JS)
            page.emulate_media(media="screen")
            page.evaluate(UNMARK_PRINT_JS, None)
            # (add_style_tag waits for a load event that never comes in MHTML snapshots)
            page.evaluate("css => { const s = document.createElement('style'); s.textContent = css; document.head.appendChild(s); }", PAGE_CSS)
            page.wait_for_timeout(300)
            cover = None
            if pi == 0 and opts.cover:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(100)
                cover = book.add_image(page.screenshot(clip={"x": 0, "y": 0, "width": opts.width, "height": round(opts.width * 1.5)}), "cover", photo=True)
                book.assets.remove(cover)
                if cover.media_type != "image/jpeg":  # covers are always images/cover.jpg
                    from PIL import Image

                    buf = io.BytesIO()
                    Image.open(io.BytesIO(cover.data)).convert("RGB").save(buf, "JPEG", quality=88)
                    cover.data, cover.media_type = buf.getvalue(), "image/jpeg"
                cover.name = "images/cover.jpg"
            data = page.evaluate(EXTRACT_JS, {"root": opts.root, "skip": SKIP_SELECTOR})
            canonical = (opts.canonical_urls or [])[pi] if opts.canonical_urls and pi < len(opts.canonical_urls) else data["url"]
            say(f"capturing {len(data['shots'])} formula and figure images")
            images: dict[int, tuple[ImageAsset, dict]] = {}
            for shot in data["shots"]:
                if shot["kind"] == "m" and shot.get("simple") is not None and _safe_text_math(shot["simple"]):
                    continue  # stays text
                pad = 1.0 if shot["kind"] == "m" else 4.0
                png = _screenshot(page, shot["n"], pad, viewport)
                if png and shot["kind"] in ("eq", "fig"):
                    from PIL import Image

                    before = Image.open(io.BytesIO(png)).size
                    png = _trim(png, round(4 * opts.scale))
                    after = Image.open(io.BytesIO(png)).size
                    # keep the CSS size in step with the trimmed pixels
                    shot["w"] = shot["w"] * after[0] / before[0]
                    shot["h"] = shot["h"] * after[1] / before[1]
                if png:
                    kind = {"m": "m", "eq": "eq"}.get(shot["kind"], "fig")
                    images[shot["n"]] = (book.add_image(png, kind, photo=bool(shot.get("raster"))), shot)
            data.update(_images=images, _key=_page_key(canonical), _cover=cover)
            pages_data.append(data)
        browser.close()

    first = pages_data[0]
    title = opts.title or first["title"] or "Untitled"
    authors = opts.authors or first["authors"]
    language = opts.language or (first["lang"] or "en").split("-")[0]
    report = WebReport(output=Path("."), title=title, authors=authors, pages=len(pages_data),
                       reference_text="\n".join(d.get("reference", "") for d in pages_data))

    for pi, data in enumerate(pages_data):
        _build_page(book, data, pi, report)
    _fix_links(book, pages_data)

    out = Path(epub_path) if epub_path else default_web_output(title, (opts.canonical_urls or urls)[0])
    report.output = out
    ident = "urn:pdf2epub:web:" + hashlib.sha1("|".join(opts.canonical_urls or urls).encode()).hexdigest()
    say("writing EPUB")
    write_epub(out, title, authors, language, book.chapters, book.assets, book.toc, first.get("_cover"), ident,
               description=f"Converted from {(opts.canonical_urls or urls)[0]}", extra_css=WEB_CSS)
    if opts.cover and opts.cover_figure not in (None, "page"):
        from .cover import set_cover

        say("composing the cover from the key figure")
        set_cover(out, opts.cover_figure, source=urllib.parse.urlsplit((opts.canonical_urls or urls)[0]).netloc.removeprefix("www."))
    report.chapters = len(book.chapters)
    report.images = len(book.assets)
    report.seconds = time.time() - t0
    return report


def _img_tag(asset: ImageAsset, shot: dict, kind: str) -> str:
    fs = shot.get("fs") or 16.0
    if kind == "m":
        # math the site sets larger than its text (KaTeX 1.21em, MathJax) is brought close to the
        # text size, like the formulas that stay text
        k = min(1.0, 1.08 * fs / shot["mfs"]) if shot.get("mfs") else 1.0
        h, w = (shot["h"] + 2.0) * k, (shot["w"] + 2.0) * k  # 1 px padding each side
        valign = -(shot["descent"] + 1.0) * k / fs
        alt = html.escape(shot.get("tex", ""), quote=True)
        return f'<img class="m" src="../{asset.name}" alt="{alt}" style="height:{h / fs:.3f}em;width:{w / fs:.3f}em;vertical-align:{valign:.3f}em"/>'
    cls = "eq" if kind == "eq" else "fig"
    alt = html.escape(shot.get("tex") or shot.get("alt") or "", quote=True)
    return f'<img class="{cls}" src="../{asset.name}" alt="{alt}" style="width:{shot["w"] / fs:.2f}em"/>'


_P2E = re.compile(r"\[\[P2E:(\d+)\]\]")
_P2E_ID = re.compile(r"\[\[P2EID:([^\]]*)\]\]")
_P2E_BLOCK = re.compile(r"\[\[P2EBLOCK:(\d+)\]\]")

WEB_CSS = """
div.table { margin: 1em 0 0.9em 0; overflow-x: auto; }
table.data { border-collapse: collapse; margin: 0.3em auto 0 auto; font-size: 0.82em; line-height: 1.3;
             border-top: 1px solid #555555; border-bottom: 1px solid #555555; }
table.data.wide { font-size: 0.68em; }
table.data th, table.data td { padding: 0.18em 0.45em; text-align: left; vertical-align: top; }
table.data.wide th, table.data.wide td { padding: 0.15em 0.25em; }
table.data tr.head > * { border-bottom: 1px solid #555555; }
table.data tr.rt > * { border-top: 1px solid #999999; }
table.data tr.rb > * { border-bottom: 1px solid #999999; }
table.data th { font-weight: bold; }
table.data .r { text-align: right; }
table.data .c { text-align: center; }
div.table p.caption { text-align: left; }
div.eq img.eq { vertical-align: middle; }
span.eqno { white-space: nowrap; vertical-align: middle; margin-left: 0.6em; }
span.tm { white-space: nowrap; }
ol { margin: 0.4em 0 0 0; padding-left: 1.8em; }
"""


def _plain_text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def _build_page(book: _Book, data: dict, pi: int, report: WebReport) -> None:
    prefix = "" if pi == 0 else f"p{pi}-"
    images: dict[int, tuple[ImageAsset, dict]] = data["_images"]
    shots = {s["n"]: s for s in data["shots"]}
    headings = [b for b in data["blocks"] if b["type"] == "heading"]
    # the title is the first heading that matches the page title (else the first h1); the
    # chapter level comes from the other headings, which may use the same tag (h1 sections)
    norm = re.sub(r"\W+", "", (data.get("title") or "").lower())
    title_block = next((b for b in headings if norm and re.sub(r"\W+", "", _plain_text(b["html"]).lower()) == norm), None)
    title_block = title_block or next((b for b in headings if b["level"] == 1), None)
    others = [b["level"] for b in headings if b is not title_block]
    chapter_level = min(others, default=2)
    current: Chapter | None = None
    content = False  # the current chapter has more than headings
    pending: list[str] = []
    used: set[str] = set(book.id_file)

    def new_chapter(title: str) -> Chapter:
        ch = Chapter(f"text/ch{len(book.chapters):03d}.xhtml", title)
        book.chapters.append(ch)
        book.page_first_file.setdefault(data["_key"], ch.file)
        return ch

    def ident(raw: str) -> str:
        if not raw:
            return ""
        xid = _xml_id(raw, prefix)
        if xid in used:
            return ""
        used.add(xid)
        book.id_file[xid] = current.file if current else ""
        return xid

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
                return f'<span class="tm">{shot["simple"].strip()}</span>'
            return ""

        def anchor(m: re.Match) -> str:
            x = ident(html.unescape(m.group(1)))
            return f'<span id="{x}"></span>' if x else ""

        return _P2E_ID.sub(anchor, _P2E.sub(sub, fragment))

    def flush_pending() -> None:
        while pending:
            x = ident(pending.pop(0))
            if x:
                current.body.append(f'<div id="{x}"></div>')

    def id_attr(raw: str) -> str:
        x = ident(raw)
        return f' id="{x}"' if x else ""

    def equation(n: int, raw_id: str = "") -> None:
        if n not in images:
            return
        asset, shot = images[n]
        no = f'<span class="eqno">{html.escape(shot["eqno"])}</span>' if shot.get("eqno") else ""
        current.body.append(f'<div class="eq"{id_attr(raw_id)}>{_img_tag(asset, shot, "eq")}{no}</div>')
        report.equations += 1

    def paragraph(fragment: str, cls: str, raw_id: str) -> None:
        text = re.sub(r"(\s*[·•|]\s*)+$", "", fill(fragment).strip())
        text = re.sub(r"[ \t\r\n]{2,}", " ", text)
        if not _plain_text(text) and "<img" not in text:
            if raw_id:
                pending.append(raw_id)
            return
        c = f' class="{cls}"' if cls else ""
        current.body.append(f"<p{c}{id_attr(raw_id)}>{text}</p>")
        report.paragraphs += 1

    for b in data["blocks"]:
        kind = b["type"]
        if kind == "anchor":
            if b["id"]:
                pending.append(b["id"])  # a section's id belongs to its first block (usually its heading)
            continue
        if kind == "heading":
            is_title = b is title_block
            level = b["level"]
            text = _plain_text(fill(b["html"])) or "Section"
            if current is None or (not is_title and level <= chapter_level and content):
                current = new_chapter(text)
                content = False
            elif not content and not current.body:
                current.title = text
            flush_pending()
            hid = ident(b["id"]) or ident(f"h{len(book.toc)}-{len(book.chapters)}")
            html_level = 1 if is_title else min(6, 2 + max(0, level - chapter_level))
            cls = ' class="title"' if is_title and pi == 0 else ""
            current.body.append(f'<h{html_level}{cls} id="{hid}">{fill(b["html"])}</h{html_level}>')
            # TOC: the article's sections at the top level; a later page's sections nest under its title
            toc_level = 1 if is_title else max(1, level - chapter_level + 1) + (1 if pi else 0)
            book.toc.append((toc_level, text, f"{current.file}#{hid}"))
            report.headings += 1
            continue
        if current is None:
            current = new_chapter(data.get("title") or "Start")
        flush_pending()
        content = True
        if kind == "p":
            raw_cls = b.get("cls", "") or ""
            cls = ""
            if re.search(r"bib|reference", raw_cls):
                cls = "bib"
            elif re.search(r"author|meta|byline|label|date", raw_cls):
                cls = "center"
            elif raw_cls == "inset":
                cls = "inset"
            # display equations met inside the paragraph split it
            parts = _P2E_BLOCK.split(b["html"])
            for k, part in enumerate(parts):
                if k % 2:
                    equation(int(part))
                else:
                    paragraph(part, cls if k == 0 else (cls + " cont").strip(), b["id"] if k == 0 else "")
        elif kind == "html":
            frag = fill(_P2E_BLOCK.sub("", b["html"]))
            current.body.append(f'<div{id_attr(b["id"])}>{frag}</div>' if b["id"] else frag)
        elif kind == "pre":
            lines = [html.escape(x).replace(" ", NBSP) or NBSP for x in b["text"].split("\n")]
            current.body.append(f'<p class="code"{id_attr(b["id"])}>' + "<br/>".join(lines) + "</p>")
        elif kind == "eq":
            equation(b["shot"], b["id"])
        elif kind == "table":
            current.body.append(_table_html(b, fill, id_attr(b["id"])))
            report.tables += 1
        elif kind == "figure":
            if b["shot"] not in images:
                continue
            asset, shot = images[b["shot"]]
            cap = fill(b.get("caption", "")).strip()
            cap_html = f'<p class="caption">{cap}</p>' if cap else ""
            current.body.append(f'<div class="figure"{id_attr(b["id"])}><div class="img">{_img_tag(asset, shot, "fig")}</div>{cap_html}</div>')
            report.figures += 1


def _table_html(b: dict, fill: Callable[[str], str], id_attr: str) -> str:
    rows = b["rows"]
    # header cells without their own alignment follow their column's data (numbers right-aligned)
    col_align: dict[int, str] = {}
    votes: dict[int, list[str]] = {}
    for r in rows:
        col = 0
        for c in r["cells"]:
            if not c["th"]:
                votes.setdefault(col, []).append("r" if c["right"] else "c" if c["center"] else "l")
            col += c["colspan"]
    for col, v in votes.items():
        best = max(set(v), key=v.count)
        if v.count(best) >= 0.6 * len(v):
            col_align[col] = best
    out = []
    for i, r in enumerate(rows):
        cells, col = [], 0
        for c in r["cells"]:
            tag = "th" if c["th"] else "td"
            attrs = ""
            if c["colspan"] > 1:
                attrs += f' colspan="{c["colspan"]}"'
            if c["rowspan"] > 1:
                attrs += f' rowspan="{c["rowspan"]}"'
            align = "r" if c["right"] else "c" if c["center"] else ""
            if not align and c["th"] and c["colspan"] == 1:
                align = {"r": "r", "c": "c"}.get(col_align.get(col, ""), "")
            if align:
                attrs += f' class="{align}"'
            body = fill(c["html"]).strip()
            if body and c.get("italic"):
                body = f"<i>{body}</i>"
            if body and c.get("bold"):
                body = f"<b>{body}</b>"
            cells.append(f"<{tag}{attrs}>{body}</{tag}>")
            col += c["colspan"]
        is_head = all(c["th"] for c in r["cells"]) and (i + 1 < len(rows) and not all(c["th"] for c in rows[i + 1]["cells"]))
        cls = " ".join(x for x, on in (("head", is_head), ("rt", r.get("top") and i > 0), ("rb", r.get("bottom") and not is_head and i + 1 < len(rows))) if on)
        out.append(f'<tr class="{cls}">' + "".join(cells) + "</tr>" if cls else "<tr>" + "".join(cells) + "</tr>")
    cap = fill(b.get("caption", "")).strip()
    cap_html = f'<p class="caption">{cap}</p>' if cap else ""
    ncols = max((sum(c["colspan"] for c in r["cells"]) for r in rows), default=0)
    tcls = "data wide" if ncols >= 7 else "data"  # many columns: compact so they fit a phone
    table = f'<table class="{tcls}">{"".join(out)}</table>'
    inner = cap_html + table if b.get("capFirst") else table + cap_html
    return f'<div class="table"{id_attr}>{inner}</div>'


def _fix_links(book: _Book, pages_data: list[dict]) -> None:
    keys = {d["_key"]: i for i, d in enumerate(pages_data)}
    actual = {_page_key(d["url"]): i for i, d in enumerate(pages_data)}  # snapshots: file URLs

    def resolve(href: str, here: str) -> str:
        href = html.unescape(href)
        u = urllib.parse.urlsplit(href)
        if u.scheme not in ("http", "https", "file", ""):
            return href
        key = _page_key(urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, "", "")))
        idx = keys.get(key, actual.get(key))
        if idx is None:
            return href
        prefix = "" if idx == 0 else f"p{idx}-"
        if u.fragment:
            xid = _xml_id(urllib.parse.unquote(u.fragment), prefix)
            target = book.id_file.get(xid)
            if target:
                return f"#{xid}" if target == here else f"{Path(target).name}#{xid}"
            # an anchor that is not in the book (it was in dropped chrome): keep the web link
            canonical = [k for k, v in keys.items() if v == idx][0]
            return href if u.scheme in ("http", "https") else f"https://{canonical}#{u.fragment}"
        first = book.page_first_file.get(pages_data[idx]["_key"])
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
