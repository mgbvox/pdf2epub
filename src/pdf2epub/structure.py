"""Turn per-page layouts into a linear document: headings, paragraphs, lists, floats, notes."""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

import pymupdf

from . import geometry as G
from .analysis import DocStats
from .extract import Link, PageData, Row, sort_glyphs
from .geometry import Box
from .layout import BULLET_CHARS, PageLayout, Region, column_of, top_level_frames
from .model import Block, Document, FootnoteBlock, Line, Paragraph, Visual

_NUM_PREFIX = re.compile(r"^\s*((\d+|[A-Z]|[IVXLC]+)(\.\d+)*\.?)\s+(?=\S)")
_REF_HEAD = re.compile(r"^(references|bibliography|works cited|literature cited|literature|reference list)$", re.I)
_ABSTRACT_HEAD = re.compile(r"^abstract$", re.I)
_SENT_END = re.compile(r"[.?!:]['\"”’)]*$")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.replace("``", "").replace("''", "")
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass
class _Entry:
    line: Line | None = None
    region: Region | None = None
    heading: int = 0  # heading level (0 = none)
    title: bool = False


@dataclass
class _Open:
    para: Paragraph
    last: Line
    first_indented: bool = False
    item_x: float = 0.0


class StructureBuilder:
    def __init__(self, doc: pymupdf.Document, pages: list[PageData], layouts: list[PageLayout], stats: DocStats):
        self.doc = doc
        self.pages = pages
        self.layouts = layouts
        self.layout_by_page = {lay.page: lay for lay in layouts}
        self.stats = stats
        self.body = stats.body_size
        self.step = stats.line_step
        self.blocks: list[Block] = []
        self.footnotes: list[FootnoteBlock] = []
        self._ids: dict[str, int] = {}
        self.indent_style = False
        self._front_matter = True
        self.title = ""
        self.authors: list[str] = []

    # ------------------------------------------------------------------
    def new_id(self, prefix: str) -> str:
        self._ids[prefix] = self._ids.get(prefix, 0) + 1
        return f"{prefix}{self._ids[prefix]}"

    def build(self) -> Document:
        per_page = [self._entries(lay) for lay in self.layouts]
        # rows split into several cells *within one column* (author blocks, aligned lists);
        # rows merely split by a column gutter are ordinary text lines
        self._grid_rows: dict[int, set[int]] = {}
        for lay, entries in zip(self.layouts, per_page):
            cells: dict[tuple[int, tuple[float, float]], int] = {}
            for e in entries:
                if e.line is not None:
                    key = (e.line.row_id, column_of(e.line.box, lay.columns))
                    cells[key] = cells.get(key, 0) + 1
            self._grid_rows[lay.page] = {rid for (rid, _), k in cells.items() if k > 1}
        for lay, entries in zip(self.layouts, per_page):
            self._frames(lay, entries)
        self._detect_headings(per_page)
        self._detect_title(per_page)
        self.indent_style = self._uses_indents(per_page)
        self._assemble(per_page)
        self._post_process()
        self._build_footnotes()
        meta = self.doc.metadata or {}
        meta_title = (meta.get("title") or "").strip()
        if _junk_title(meta_title) and self.title:
            meta_title = ""
        title = meta_title or self.title or "Untitled"
        authors = [a.strip() for a in re.split(r"[;,]| and ", meta.get("author") or "") if a.strip()] or self.authors
        return Document(
            title=title, authors=authors, language="en", blocks=self.blocks, footnotes=self.footnotes,
            page_count=len(self.pages), body_size=self.body,
        )

    # ------------------------------------------------------------------
    def _entries(self, lay: PageLayout) -> list[_Entry]:
        links = self.pages[lay.page].links
        out: list[_Entry] = []
        for it in lay.items:
            if isinstance(it, Region):
                out.append(_Entry(region=it))
                continue
            prev = out[-1].line if out and out[-1].line is not None else None
            if prev is not None and prev.row_id == it.row_id:
                prev.glyphs.extend(it.glyphs)
                sort_glyphs(prev.glyphs)
                prev.box = G.union([prev.box, it.box])
                prev.links = _links_for(prev.box, links)
                continue
            out.append(_Entry(line=Line.from_segments([it], _links_for(it.box, links))))
        return out

    def _frames(self, lay: PageLayout, entries: list[_Entry]) -> None:
        """Assign each line the left/right reference edges of its local text block."""
        block: list[Line] = []

        def flush() -> None:
            if not block:
                return
            left = min(l.x0 for l in block)
            styles = [(l.text_size, _is_bold_line(l)) for l in block]

            def alike(a: tuple, b: tuple) -> bool:
                return a[1] == b[1] and abs(a[0] - b[0]) <= 0.15 * max(a[0], b[0])

            right_all = max(l.x1 for l in block)
            for k, l in enumerate(block):
                # right edge reached by earlier lines; for running text only lines of the same
                # style count (a long heading above a paragraph wrapped beside a figure must not
                # widen the paragraph's frame), bold lines (headings) see the whole block
                any_style = max(o.x1 for o in block[:k]) if k else right_all
                if styles[k][1]:
                    ref = any_style
                else:
                    seen = [o.x1 for o, st in zip(block[:k], styles[:k]) if alike(st, styles[k])]
                    pool = [o.x1 for o, st in zip(block, styles) if alike(st, styles[k])]
                    ref = max(seen) if seen else min(max(pool), any_style)
                l.frame = (left, max(ref, l.x1))
            block.clear()

        for e in entries:
            l = e.line
            if l is None:
                flush()
                continue
            if block:
                p = block[-1]
                gap = l.core[1] - p.core[3]
                ov = G.h_overlap(l.box, p.box)
                if not (gap < 1.2 * self.step and gap > -0.5 * l.size and ov > 0.3 * min(G.width(l.box), G.width(p.box))):
                    flush()
            block.append(l)
        flush()

    # ------------------------------------------------------------------
    def _detect_headings(self, per_page: list[list[_Entry]]) -> None:
        toc = []
        try:
            toc = self.doc.get_toc(simple=False)
        except Exception:  # pragma: no cover
            toc = []
        matched_sizes: dict[tuple, int] = {}
        for entry in toc:
            level, title, pno = entry[0], entry[1], entry[2] - 1
            if not (0 <= pno < len(per_page)) or not title.strip():
                continue
            tnorm = _norm(_NUM_PREFIX.sub("", title))
            if not tnorm:
                continue
            best = None
            best_score = 0.0
            cands = per_page[pno]
            for i, e in enumerate(cands):
                if e.line is None or e.heading:
                    continue
                lnorm = _norm(_NUM_PREFIX.sub("", e.line.text))
                if not lnorm:
                    continue
                if lnorm == tnorm or (len(lnorm) >= 4 and tnorm.startswith(lnorm)) or lnorm.startswith(tnorm):
                    score = 1.0
                else:
                    score = difflib.SequenceMatcher(None, lnorm[: len(tnorm)], tnorm).ratio()
                if score < 0.82:
                    continue
                # prefer prominent lines
                if _is_bold_line(e.line) or e.line.size > 1.1 * self.body:
                    score += 0.2
                if score > best_score:
                    best, best_score = i, score
            if best is None:
                continue
            e = cands[best]
            e.heading = max(1, min(level, 6))
            matched_sizes[_style_key(e.line)] = e.heading
            # wrapped headings: following lines that continue the title
            acc = _norm(_NUM_PREFIX.sub("", e.line.text))
            j = best + 1
            while j < len(cands) and cands[j].line is not None and len(acc) < len(tnorm):
                nxt = _norm(cands[j].line.text)
                if nxt and tnorm.startswith(acc + nxt[: max(1, len(nxt) // 2)]):
                    cands[j].heading = e.heading
                    acc += nxt
                    j += 1
                else:
                    break

        # heuristic headings (unnumbered sections, documents without outline)
        for entries in per_page:
            for i, e in enumerate(entries):
                l = e.line
                if l is None or e.heading:
                    continue
                if not self._heading_like(entries, i):
                    if self._heading_wrap(entries, i):
                        e.heading = entries[i - 1].heading
                    continue
                key = _style_key(l)
                level = matched_sizes.get(key)
                num = re.match(r"^((\d+|[A-Z])(\.\d+)*)\.?\s", l.text.strip())
                if level is None and num and not matched_sizes:
                    level = min(4, num.group(1).count(".") + 1)
                if level is None:
                    if l.size >= 1.35 * self.body:
                        level = 1
                    elif l.size >= 1.12 * self.body:
                        level = 1 if not matched_sizes else min(matched_sizes.values())
                    else:
                        level = max(matched_sizes.values(), default=2) if matched_sizes else 3
                        level = max(level, 2)
                e.heading = level

    def _heading_wrap(self, entries: list[_Entry], i: int) -> bool:
        """Second line of a wrapped heading: same style, tight below a heading line."""
        if i == 0:
            return False
        prev, l = entries[i - 1], entries[i].line
        if not prev.heading or prev.title or prev.line is None or prev.line.page != l.page:
            return False
        if _style_key(prev.line) != _style_key(l) or not (_is_bold_line(l) or l.size >= 1.12 * self.body):
            return False
        text = l.text.strip()
        if not (0 < len(text) <= 80 and 0 <= l.core[1] - prev.line.core[3] < 0.6 * l.size and not text.endswith((".", ",", ";"))):
            return False
        if len(prev.line.text.split()) + len(text.split()) > 16:
            return False  # a run of emphasised prose, not a heading
        nxt = entries[i + 1].line if i + 1 < len(entries) else None
        # a heading ends after its wrapped line; a third tight line of the same style means prose
        return not (nxt is not None and nxt.page == l.page and _style_key(nxt) == _style_key(l)
                    and 0 <= nxt.core[1] - l.core[3] < 0.6 * l.size)

    def _heading_like(self, entries: list[_Entry], i: int) -> bool:
        l = entries[i].line
        if l.row_id in self._grid_rows.get(l.page, ()):
            return False
        text = l.text.strip()
        if not text or len(text) > 110 or text.endswith((",", ";")):
            return False
        if text[0] in BULLET_CHARS or re.match(r"^(Figure|Fig\.|Table)\s*\d", text):
            return False
        glyphs = [g for g in l.glyphs if g.c.isalpha()]
        if len(glyphs) < 3:
            return False
        bold = sum(1 for g in glyphs if g.info and g.info.bold) / len(glyphs)
        big = sum(1 for g in glyphs if g.size >= 1.12 * self.body) / len(glyphs)
        caps = all(g.c.isupper() for g in glyphs) and len(glyphs) >= 4
        numbered = bool(re.match(r"^(\d+(\.\d+)*|[A-Z](\.\d+)*)\.?\s+\S", text))
        smallcaps_heading = caps and len(text) <= 80 and (numbered or l.size >= 1.12 * self.body)
        if bold < 0.95 and big < 0.9 and not smallcaps_heading:
            return False
        if big < 0.9 and l.size < 0.97 * self.body and not smallcaps_heading:
            return False  # bold small text: table headers, labels
        # must stand alone: gap before or block start, and next line not a continuation
        prev = entries[i - 1].line if i > 0 else None
        nxt = entries[i + 1].line if i + 1 < len(entries) else None
        if prev is not None and prev.page == l.page:
            gap = l.core[1] - prev.core[3]
            if gap < 0.35 * self.body and _is_bold_line(prev) == (bold >= 0.95) and abs(prev.size - l.size) < 0.5:
                return False
        if nxt is not None and nxt.page == l.page:
            gap = nxt.core[1] - l.core[3]
            same_style = _style_key(nxt) == _style_key(l)
            if gap < 0.3 * self.body and not same_style and nxt.x0 < l.x1 and G.width(l.box) > 0.85 * (l.frame[1] - l.frame[0]):
                return False
        return True

    def _detect_title(self, per_page: list[list[_Entry]]) -> None:
        for entries in per_page[:2]:
            lines = [e for e in entries if e.line is not None]
            if not lines:
                continue
            biggest = max(e.line.size for e in lines)
            if biggest < 1.3 * self.body:
                continue
            title_entries = []
            for e in lines:
                if abs(e.line.size - biggest) < 0.6:
                    title_entries.append(e)
                elif title_entries:
                    break
            if title_entries and title_entries[0].line.box[1] < 0.5 * self.pages[title_entries[0].line.page].height:
                for e in title_entries:
                    e.title = True
                    e.heading = 0
                self.title = " ".join(e.line.text.strip() for e in title_entries)
                self.authors = self._guess_authors(entries, title_entries[-1])
                return

    def _guess_authors(self, entries: list[_Entry], last_title: _Entry) -> list[str]:
        out: list[str] = []
        started = False
        for e in entries:
            if e is last_title:
                started = True
                continue
            if not started or e.line is None:
                if started and e.region is not None:
                    break
                continue
            if e.heading:
                break
            l = e.line
            names = []
            cur = []
            prev = None
            for g in l.glyphs:
                if not (g.info and g.info.bold) or g.level != 0:
                    if cur:
                        names.append(cur)
                        cur = []
                    prev = None
                    continue
                if prev is not None and g.x0 - prev.x1 > 1.2 * l.size:
                    names.append(cur)
                    cur = []
                cur.append(g)
                prev = g
            if cur:
                names.append(cur)
            for gs in names:
                from .extract import glyph_text

                t = glyph_text(gs, l.size).strip(" ,*∗†‡§¶0123456789")
                if 3 <= len(t) <= 40 and " " in t and not re.search(r"\d|@|university|institute", t, re.I):
                    out.append(t)
        return out[:30]

    def _uses_indents(self, per_page: list[list[_Entry]]) -> bool:
        indented = flush = 0
        for entries in per_page:
            for a, b in zip(entries, entries[1:]):
                la, lb = a.line, b.line
                if la is None or lb is None or la.page != lb.page:
                    continue
                if not self.stats.is_body_size(lb.size):
                    continue
                if la.x1 < la.frame[1] - 2 * la.size:  # previous paragraph ended short
                    if lb.x0 > lb.frame[0] + 0.7 * lb.size:
                        indented += 1
                    else:
                        flush += 1
        return indented > flush

    # ------------------------------------------------------------------
    def _assemble(self, per_page: list[list[_Entry]]) -> None:
        self._next_line: dict[int, Line] = {}
        flat = [e.line for entries in per_page for e in entries if e.line is not None]
        for a, b in zip(flat, flat[1:]):
            self._next_line[id(a)] = b
        cur: _Open | None = None
        deferred: list[Visual] = []
        after_equation = False
        list_stack: list[float] = []

        def close() -> None:
            nonlocal cur
            if cur is not None:
                self._finish_paragraph(cur.para)
                self.blocks.append(cur.para)
                cur = None
            if deferred:
                self.blocks.extend(deferred)
                deferred.clear()

        for entries in per_page:
            for e in entries:
                if e.region is not None:
                    vis = self._visual(e.region)
                    if vis.kind == "equation":
                        close()
                        self.blocks.append(vis)
                        after_equation = True
                    elif cur is not None and self._may_continue(cur):
                        deferred.append(vis)
                    else:
                        close()
                        self.blocks.append(vis)
                        after_equation = False
                    continue
                line = e.line
                if e.title or e.heading:
                    if cur is not None and cur.para.kind in ("heading", "title") and cur.para.level == e.heading and (
                        cur.last.page == line.page and line.core[1] - cur.last.core[3] < 0.6 * line.size
                    ):
                        cur.para.lines.append(line)
                        cur.last = line
                        continue
                    close()
                    if e.heading and not e.title:
                        self._front_matter = False
                    kind = "title" if e.title else "heading"
                    para = Paragraph(page=line.page, box=line.box, lines=[line], kind=kind, level=e.heading)
                    cur = _Open(para, line)
                    after_equation = False
                    list_stack.clear()
                    continue
                bullet = _bullet(line)
                if cur is not None and cur.para.kind not in ("heading", "title") and not bullet and self._continues(cur, line):
                    cur.para.lines.append(line)
                    cur.last = line
                    continue
                close()
                para = Paragraph(page=line.page, box=line.box, lines=[line])
                if bullet:
                    para.kind = "li"
                    para.marker = bullet
                    bx = line.x0
                    while list_stack and bx < list_stack[-1] - 0.5 * line.size:
                        list_stack.pop()
                    if not list_stack or bx > list_stack[-1] + 0.5 * line.size:
                        list_stack.append(bx)
                    para.level = len(list_stack)
                else:
                    if list_stack and line.x0 <= list_stack[0] - 0.3 * line.size:
                        list_stack.clear()
                    if list_stack:
                        # text continuing inside a list item after a float/equation
                        para.style = "in-list"
                        para.level = len(list_stack)
                    if after_equation and not self._looks_new_paragraph(line):
                        para.style = (para.style + " cont").strip()
                cur = _Open(para, line, first_indented=line.x0 > line.frame[0] + 0.7 * line.size)
                if bullet:
                    cur.item_x = _item_text_x(line)
                after_equation = False
        close()

    def _may_continue(self, cur: _Open) -> bool:
        if cur.para.kind in ("heading", "title"):
            return False
        last = cur.last
        return last.x1 >= last.frame[1] - 1.5 * last.size and not last.text.rstrip().endswith(":")

    def _looks_new_paragraph(self, line: Line) -> bool:
        return self.indent_style and line.x0 > line.frame[0] + 0.7 * line.size

    def _continues(self, cur: _Open, line: Line) -> bool:
        prev = cur.last
        size = line.size
        ps, ls = prev.text_size, line.text_size
        if abs(ps - ls) > 0.15 * max(ps, ls):
            return False
        if line.row_id in self._grid_rows.get(line.page, ()) or prev.row_id in self._grid_rows.get(prev.page, ()):
            if not (line.page == prev.page and line.core[1] > prev.core[1] and G.h_overlap(line.box, prev.box) > 0):
                return False
        if ended_short(prev, line):
            return False
        same_flow = line.page == prev.page and line.core[1] > prev.core[1] and line.core[1] - prev.core[3] < 2.5 * self.step
        if cur.para.kind == "li":
            if same_flow and line.core[1] - prev.core[3] < 0.5 * self.step + 2 and line.x0 >= cur.item_x - 1.0 * size:
                return True
            if not same_flow and line.x0 >= cur.item_x - 1.0 * size:
                return True
            return False
        if same_flow and G.h_overlap(line.box, prev.box) > 0:
            gap = line.core[1] - prev.core[3]
            # tall inline math (radicals, fractions) pushes lines apart without starting a paragraph
            gap -= max(0.0, line.core[1] - line.box[1] - 0.3 * size) + max(0.0, prev.box[3] - prev.core[3] - 0.15 * size)
            normal = self.step - 0.96 * size
            if gap > normal + 0.35 * size:
                return False
            cw = line.frame[1] - line.frame[0]
            col = column_of(line.box, self.layout_by_page[line.page].columns)
            if (
                abs((line.x0 + line.x1) / 2 - (prev.x0 + prev.x1) / 2) < 0.5 * size
                and G.width(line.box) < 0.7 * (col[1] - col[0])
                and G.width(prev.box) < 0.7 * (col[1] - col[0])
                and abs(line.x0 - prev.x0) > 0.3 * size
            ):
                return True  # centred block (author cells, centred captions)
            _ = cw
            if line.x0 > line.frame[0] + 0.7 * size and prev.x0 <= prev.frame[0] + 0.3 * size:
                # Indented line after a full line: first-line indent of a new paragraph, or the
                # continuation of a hanging-indent entry (bibliographies)?  Look ahead.
                nxt = self._next_line.get(id(line))
                if self.indent_style and nxt is not None and nxt.page == line.page:
                    back_left = abs(nxt.x0 - prev.x0) < 0.4 * size
                    nxt_gap = nxt.core[1] - line.core[3]
                    if back_left and nxt_gap <= normal + 0.35 * size:
                        return False
            if line.x0 < prev.x0 - 0.7 * size:
                # outdent: hanging-indent lists/bibliographies start a new entry,
                # unless the previous line was an indented first line.
                if not (len(cur.para.lines) == 1 and cur.first_indented):
                    return False
            return True
        # column or page change
        if line.x0 > line.frame[0] + 0.7 * size and self.indent_style:
            return False
        text = line.text.lstrip()
        if not text:
            return False
        if not self.indent_style:
            if _SENT_END.search(prev.text.rstrip()) and (text[0].isupper() or text[0].isdigit()):
                return False
        return True

    def _finish_paragraph(self, para: Paragraph) -> None:
        para.box = G.union(l.box for l in para.lines)
        if para.kind in ("heading", "title"):
            para.id = self.new_id("h")
            return
        para.id = self.new_id("p")
        lines = para.lines
        if len(lines) >= 2:
            centers = [(l.x0 + l.x1) / 2 for l in lines]
            lefts = [l.x0 for l in lines]
            col = column_of(para.box, self.layout_by_page[para.page].columns)
            narrow = max(G.width(l.box) for l in lines) < 0.85 * (col[1] - col[0])
            if narrow and max(centers) - min(centers) < 0.5 * lines[0].size and max(lefts) - min(lefts) > 4:
                para.kind = "centered"
                para.keep_breaks = True
        if para.kind == "p" and all(l.row_id in self._grid_rows.get(l.page, ()) for l in lines):
            para.keep_breaks = True
            if self._front_matter:
                para.kind = "centered"
        if para.kind == "p" and all(_mono_line(l) for l in lines) and len(lines) >= 2:
            para.kind = "code"
            para.keep_breaks = True
        if para.kind == "p" and len(lines) == 1:
            l = lines[0]
            col = column_of(l.box, self.layout_by_page[l.page].columns)
            cw = col[1] - col[0]
            if abs((l.x0 + l.x1) / 2 - (col[0] + col[1]) / 2) < 3 and G.width(l.box) < 0.8 * cw and l.x0 > col[0] + 2 * l.size:
                para.kind = "centered"
        if para.kind == "p":
            col = column_of(para.box, self.layout_by_page[para.page].columns)
            if para.box[0] > col[0] + 1.5 * self.body and para.box[2] < col[1] - 1.5 * self.body and len(lines) >= 2:
                para.style = (para.style + " inset").strip()
            if lines[0].size < 0.9 * self.body:
                para.style = (para.style + " small").strip()

    # ------------------------------------------------------------------
    def _visual(self, reg: Region) -> Visual:
        prefix = {"figure": "fig", "table": "tab", "equation": "eq", "textbox": "box", "algorithm": "alg"}.get(reg.kind, "fig")
        vis = Visual(page=reg.page, box=reg.outer_box, region=reg, kind=reg.kind, label=reg.label, caption_above=reg.caption_above)
        vis.id = self.new_id(prefix)
        links = self.pages[reg.page].links
        if reg.caption:
            lines = _lines_from_segments(reg.caption, links)
            cap = Paragraph(page=reg.page, box=G.union(l.box for l in lines), lines=lines, kind="caption")
            cap.id = self.new_id("cap")
            vis.caption = cap
        if reg.kind == "textbox":
            vis.box_groups = self._textbox_groups(reg, links)
        elif reg.kind == "algorithm":
            lines = _lines_from_segments(reg.segs, links, split_far=False)
            left = min((l.x0 for l in lines), default=0.0)
            paras = []
            for l in lines:
                p = Paragraph(page=reg.page, box=l.box, lines=[l], kind="algline")
                p.level = int(round((l.x0 - left) / self.body * 10))  # indent in tenths of an em
                p.id = self.new_id("al")
                paras.append(p)
            vis.box_groups = [paras]
        return vis

    def _textbox_groups(self, reg: Region, links: list[Link]) -> list[list[Paragraph]]:
        lines = _lines_from_segments(reg.segs, links)
        frames = top_level_frames([g for g in reg.graphics if g.closed_rect]) or []
        seps = sorted(reg.separators)
        groups: dict[tuple, list[Line]] = {}
        for l in lines:
            fi = next((k for k, f in enumerate(frames) if G.fraction_inside(l.core, G.expand(f.box, 2)) > 0.5), -1)
            si = sum(1 for y in seps if y < l.baseline - 0.3 * l.size)
            groups.setdefault((fi, si), []).append(l)
        out: list[list[Paragraph]] = []
        for key in sorted(groups):
            gl = sorted(groups[key], key=lambda l: (l.baseline, l.x0))
            fbox = frames[key[0]].box if key[0] >= 0 else G.union(l.box for l in gl)
            right = max(l.x1 for l in gl)
            left = min(l.x0 for l in gl)
            for l in gl:
                l.frame = (left, right)
            paras: list[Paragraph] = []
            prev = None
            for l in gl:
                new = prev is None
                if prev is not None:
                    gap = l.core[1] - prev.core[3]
                    short = ended_short(prev, l)
                    new = short or gap > 0.6 * l.size or l.x0 < prev.x0 - 2 * l.size
                if new:
                    p = Paragraph(page=reg.page, box=l.box, lines=[l], kind="boxline")
                    if prev is not None and l.core[1] - prev.core[3] > 0.8 * l.size:
                        p.style = "gap"
                    p.id = self.new_id("bl")
                    paras.append(p)
                else:
                    paras[-1].lines.append(l)
                prev = l
            for p in paras:
                p.box = G.union(x.box for x in p.lines)
            out.append(paras)
            _ = fbox
        return out

    # ------------------------------------------------------------------
    def _post_process(self) -> None:
        in_refs = None
        in_abstract = False
        for b in self.blocks:
            if isinstance(b, Paragraph) and b.kind == "heading":
                text = _NUM_PREFIX.sub("", " ".join(l.text for l in b.lines)).strip()
                if _REF_HEAD.match(text):
                    in_refs = b.level
                elif in_refs is not None and b.level <= in_refs:
                    in_refs = None
                in_abstract = bool(_ABSTRACT_HEAD.match(text))
                continue
            if isinstance(b, Paragraph) and b.kind == "p":
                if in_refs is not None:
                    b.kind = "bib"
                elif in_abstract:
                    b.style = (b.style + " abstract").strip()

    def _build_footnotes(self) -> None:
        n = 0
        for lay in self.layouts:
            links = self.pages[lay.page].links
            for fn in lay.footnotes:
                n += 1
                lines = [Line.from_segments([r], _links_for(r.box, links)) for r in fn.rows]
                if fn.marker and lines:
                    # strip the leading marker glyphs from the first line
                    first = lines[0]
                    k = 0
                    while k < len(first.glyphs) and k < len(fn.marker) and first.glyphs[k].c == fn.marker[k]:
                        k += 1
                    first.glyphs = first.glyphs[k:]
                para = Paragraph(page=lay.page, box=G.union(r.box for r in fn.rows), lines=[l for l in lines if l.glyphs], kind="note")
                para.id = f"fn{n}"
                self.footnotes.append(FootnoteBlock(f"fn{n}", fn.marker, lay.page, para.box, para))


# ---------------------------------------------------------------------------
# helpers


def ended_short(prev: Line, line: Line) -> bool:
    """Did ``prev`` end before the margin although the next word would have fitted?"""
    remaining = prev.frame[1] - prev.x1
    if remaining < 1.0 * prev.size:
        return False
    pg, lg = prev.glyphs, line.glyphs
    if len(pg) >= 2 and pg[-1].c in "-\u2010" and pg[-2].c.isalpha() and lg and lg[0].c.islower():
        return False  # a hyphenated word continues on the next line
    word = 0.0
    gs = line.glyphs
    if gs:
        end = gs[0].x1
        for a, b in zip(gs, gs[1:]):
            if b.x0 - a.x1 > 0.2 * line.size:
                break
            end = b.x1
        word = end - gs[0].x0
    word *= prev.size / max(line.size, 0.1)
    return remaining > word + 0.45 * prev.size


def _junk_title(t: str) -> bool:
    """PDF metadata titles that are file names or tool defaults rather than the paper's title."""
    t = t.strip()
    if not t or len(t.split()) <= 1:
        return True
    low = t.lower()
    return bool(
        re.search(r"\.(pdf|docx?|tex|dvi|ps)$", low)
        or low.startswith(("microsoft word", "untitled", "arxiv", "paper", "main", "manuscript"))
        or re.fullmatch(r"[\w\-. ]*\d{4}[\w\-. ]*", low) and len(t.split()) <= 2
    )


def _links_for(box: Box, links: list[Link]) -> list[Link]:
    return [ln for ln in links if G.overlaps(ln.box, box)]


def _lines_from_segments(segs: list[Row], links: list[Link], split_far: bool = True) -> list[Line]:
    by_row: dict[int, list[Row]] = {}
    for s in segs:
        by_row.setdefault(s.row_id, []).append(s)
    lines = []
    for rid, group in by_row.items():
        group.sort(key=lambda s: s.x0)
        # keep far-apart segments of the same row separate (e.g. side-by-side sub-captions)
        cur = [group[0]]
        for s in group[1:]:
            if split_far and s.x0 - cur[-1].x1 > 3 * s.size:
                lines.append(Line.from_segments(cur, _links_for(G.union(c.box for c in cur), links)))
                cur = [s]
            else:
                cur.append(s)
        lines.append(Line.from_segments(cur, _links_for(G.union(c.box for c in cur), links)))
    lines.sort(key=lambda l: (round(l.baseline), l.x0))
    return lines


def _is_bold_line(l: Line) -> bool:
    gs = [g for g in l.glyphs if g.c.isalpha()]
    return bool(gs) and sum(1 for g in gs if g.info and g.info.bold) / len(gs) > 0.9


def _style_key(l: Line) -> tuple:
    return (round(l.size * 2) / 2, _is_bold_line(l))


def _mono_line(l: Line) -> bool:
    gs = [g for g in l.glyphs if not g.c.isspace()]
    return bool(gs) and all(g.info and g.info.mono for g in gs)


def _bullet(line: Line) -> str:
    if not line.glyphs:
        return ""
    g = line.glyphs[0]
    if g.c not in BULLET_CHARS or g.c in "-*" and g.level != 0:
        return ""
    if g.c in "-–—*" and len(line.glyphs) > 1 and line.glyphs[1].x0 - g.x1 < 0.25 * line.size:
        return ""
    if line.x0 < line.frame[0] - 1 or (line.x0 - column_left_guess(line)) < 0.3 * line.size and g.c in "-–—":
        return ""
    return g.c


def column_left_guess(line: Line) -> float:
    return line.frame[0]


def _item_text_x(line: Line) -> float:
    if len(line.glyphs) > 1:
        return line.glyphs[1].x0
    return line.x0
