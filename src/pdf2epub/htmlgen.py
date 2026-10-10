"""Serialise the document model into EPUB XHTML chapters (Kindle friendly)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import numpy as np

from . import geometry as G
from .extract import Link, PageData
from .geometry import Box
from .inline import InlineContext, LineBreak, MathImage, NoteRef, TextRun, Token, tokenize_paragraph
from .layout import SUBCAPTION_RE, PageLayout, Region, column_of
from .model import Document, FootnoteBlock, Paragraph, Visual
from .render import ImageAsset, Renderer, ink_box, is_photographic, split_wide, squeeze_gap


@dataclass
class Options:
    dpi: int = 400
    inline_math: str = "auto"  # auto | image
    split_panels: bool = True
    rotate_wide: bool = False
    image_background: str = "white"
    max_equation_em: float = 22.0  # split wider multi-part equations for narrow screens


@dataclass
class Chapter:
    file: str
    title: str
    body: list[str] = field(default_factory=list)
    toc: list[tuple[int, str, str]] = field(default_factory=list)  # (level, text, anchor)


_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]")


def esc(s: str) -> str:
    return html.escape(_BAD_XML.sub("", s), quote=True)


class HtmlWriter:
    def __init__(self, document: Document, pages: list[PageData], layouts: list[PageLayout], renderer: Renderer, options: Options, vocab):
        self.d = document
        self.pages = pages
        self.layouts = layouts
        self.r = renderer
        self.o = options
        self.body = document.body_size
        markers: dict[int, set[str]] = {}
        for fn in document.footnotes:
            if fn.marker:
                markers.setdefault(fn.page, set()).add(fn.marker)
        self.ctx = InlineContext(
            graphics_by_page={p.index: p.graphics for p in pages},
            vocab=vocab, body_size=self.body, math_mode=options.inline_math, footnote_markers=markers,
        )
        self.chapters: list[Chapter] = []
        self.block_chapter: dict[str, int] = {}
        self.targets: dict[int, list[tuple[Box, str]]] = {}
        self.fn_by_id = {f.id: f for f in document.footnotes}
        self.fn_targets: dict[int, list[FootnoteBlock]] = {}
        for f in document.footnotes:
            self.fn_targets.setdefault(f.page, []).append(f)
        self._page_glyph_index: dict[int, list] = {}
        self._fn_refcount: dict[str, int] = {}
        self.toc: list[tuple[int, str, str]] = []  # (level, text, file#anchor)

    # ------------------------------------------------------------------
    def write(self) -> list[Chapter]:
        self._split_chapters()
        self._register_targets()
        self._assign_footnotes()
        for ci, ch in enumerate(self.chapters):
            pass
        for ci, (ch, blocks) in enumerate(zip(self.chapters, self._chapter_blocks)):
            self._cur_chapter = ci
            out = ch.body
            depth = 0  # open <ul> levels; each open level has an open <li>
            for b in blocks:
                if isinstance(b, Paragraph) and b.kind == "li":
                    lvl = max(1, b.level)
                    if lvl > depth:
                        while depth < lvl:
                            out.append("<ul>" if depth == 0 else "<ul>")
                            depth += 1
                            if depth < lvl:
                                out.append("<li>")
                    else:
                        out.append("</li>")
                        while depth > lvl:
                            out.append("</ul></li>")
                            depth -= 1
                    out.append(f'<li id="{b.id}"><p class="li">{self._inline(b)}</p>')
                    continue
                if depth and isinstance(b, (Paragraph, Visual)) and getattr(b, "style", "").startswith("in-list"):
                    out.append(self._block(b))
                    continue
                if depth and isinstance(b, Visual) and b.kind == "equation":
                    out.append(self._block(b))
                    continue
                while depth > 0:
                    out.append("</li></ul>")
                    depth -= 1
                out.append(self._block(b))
            while depth > 0:
                out.append("</li></ul>")
                depth -= 1
            notes = [f for f in self.d.footnotes if f.chapter == ci]
            if notes:
                out.append('<div class="notes"><hr class="notes"/>')
                for f in notes:
                    back = f.ref_ids[0] if f.ref_ids else None
                    mark = esc(_marker_text(f.marker)) if f.marker else ""
                    lead = f'<a href="#{back}">{mark or "↩"}</a> ' if back else (f"{mark} " if mark else "")
                    out.append(f'<aside epub:type="footnote" id="{f.id}" class="footnote"><p class="note">{lead}{self._inline(f.paragraph)}</p></aside>')
                out.append("</div>")
        return self.chapters

    # ------------------------------------------------------------------
    def _split_chapters(self) -> None:
        levels = [b.level for b in self.d.blocks if isinstance(b, Paragraph) and b.kind == "heading"]
        top = min(levels) if levels else 1
        self._chapter_blocks: list[list] = [[]]
        titles = ["Title page"]
        for b in self.d.blocks:
            if isinstance(b, Paragraph) and b.kind == "heading" and b.level == top and self._chapter_blocks[-1]:
                self._chapter_blocks.append([])
                titles.append(_plain(b))
            self._chapter_blocks[-1].append(b)
        if not self._chapter_blocks[0]:
            self._chapter_blocks.pop(0)
            titles.pop(0)
        for i, t in enumerate(titles):
            first = self._chapter_blocks[i][0] if self._chapter_blocks[i] else None
            if i == 0 and isinstance(first, Paragraph) and first.kind == "heading":
                t = _plain(first)
            self.chapters.append(Chapter(file=f"text/ch{i:03d}.xhtml", title=t or f"Part {i + 1}"))
        for i, blocks in enumerate(self._chapter_blocks):
            for b in blocks:
                self.block_chapter[b.id] = i
                if isinstance(b, Visual) and b.caption is not None:
                    self.block_chapter[b.caption.id] = i
                if isinstance(b, Paragraph) and b.kind in ("heading", "title"):
                    lvl = 0 if b.kind == "title" else b.level
                    self.toc.append((lvl, _plain(b), f"{self.chapters[i].file}#{b.id}"))

    def _register_targets(self) -> None:
        for i, blocks in enumerate(self._chapter_blocks):
            for b in blocks:
                if isinstance(b, Paragraph):
                    for l in b.lines:
                        self.targets.setdefault(l.page, []).append((l.box, b.id))
                elif isinstance(b, Visual):
                    self.targets.setdefault(b.page, []).append((b.box, b.id))
        for lst in self.targets.values():
            lst.sort(key=lambda t: (t[0][1], t[0][0]))

    def _assign_footnotes(self) -> None:
        # chapter of first reference, else chapter of its page
        first_ref: dict[str, int] = {}
        for i, blocks in enumerate(self._chapter_blocks):
            for b in blocks:
                paras = [b] if isinstance(b, Paragraph) else ([b.caption] if isinstance(b, Visual) and b.caption else [])
                for p in paras:
                    for l in p.lines:
                        for ln in l.links:
                            if ln.name and ln.name.lower().startswith("hfootnote") and ln.target_page is not None:
                                f = self._footnote_at(ln.target_page, ln.target_point)
                                if f and f.id not in first_ref:
                                    first_ref[f.id] = i
        page_chapter: dict[int, int] = {}
        for i, blocks in enumerate(self._chapter_blocks):
            for b in blocks:
                page_chapter.setdefault(b.page, i)
        for f in self.d.footnotes:
            if f.id in first_ref:
                f.chapter = first_ref[f.id]
            else:
                cands = [c for p, c in page_chapter.items() if p <= f.page]
                f.chapter = page_chapter.get(f.page, max(cands) if cands else 0)

    def _footnote_at(self, page: int, point) -> FootnoteBlock | None:
        cands = sorted(self.fn_targets.get(page, []), key=lambda f: f.box[1])
        if not cands:
            return None
        if point is None:
            return cands[0]
        x, y = point
        # hyperref puts footnote anchors about one line above the note text
        below = [f for f in cands if f.box[1] >= y - 3]
        if below:
            return below[0]
        return min(cands, key=lambda f: abs(f.box[3] - y))

    def _href(self, ln: Link) -> str | None:
        if ln.uri:
            return ln.uri
        if ln.target_page is None:
            return None
        if ln.name and ln.name.lower().startswith("hfootnote"):
            f = self._footnote_at(ln.target_page, ln.target_point)
            if f is not None:
                return self._file_ref(f.chapter, f.id)
        tid = self._resolve(ln.target_page, ln.target_point)
        if tid is None:
            return None
        return self._file_ref(self.block_chapter.get(tid, 0), tid)

    def _file_ref(self, chapter: int, anchor: str) -> str:
        if chapter == getattr(self, "_cur_chapter", -1):
            return f"#{anchor}"
        return f"{self.chapters[chapter].file.split('/')[-1]}#{anchor}"

    def _resolve(self, page: int, point) -> str | None:
        for p in range(page, min(page + 2, len(self.pages))):
            cands = self.targets.get(p, [])
            if not cands:
                continue
            if point is None or p != page:
                return cands[0][1]
            x, y = point
            best, best_s = None, 1e9
            for box, tid in cands:
                if box[1] - 4 <= y <= box[3]:
                    s = 0.0
                elif box[1] > y:
                    s = box[1] - y
                else:
                    continue
                if not (box[0] - 25 <= x <= box[2] + 25):
                    s += 200
                if s < best_s:
                    best, best_s = tid, s
            if best is not None:
                return best
        return None

    # ------------------------------------------------------------------
    def _block(self, b) -> str:
        if isinstance(b, Visual):
            return self._visual(b)
        p: Paragraph = b
        inner = self._inline(p)
        if p.kind == "title":
            return f'<h1 class="title" id="{p.id}">{inner}</h1>'
        if p.kind == "heading":
            n = min(6, p.level + 1)
            return f'<h{n} id="{p.id}">{inner}</h{n}>'
        cls = {
            "p": "", "caption": "caption", "centered": "center", "code": "code", "bib": "bib",
            "note": "note", "boxline": "boxline",
        }.get(p.kind, "")
        classes = " ".join(c for c in [cls] + p.style.split() if c)
        if "in-list" in classes:
            classes = classes.replace("in-list", "inlist")
        cattr = f' class="{classes}"' if classes else ""
        return f'<p id="{p.id}"{cattr}>{inner}</p>'

    def _visual(self, v: Visual) -> str:
        parts: list[str] = []
        cap = ""
        if v.caption is not None:
            cap = f'<p class="caption" id="{v.caption.id}">{self._inline(v.caption)}</p>'
        if v.kind == "equation":
            img = self._equation_image(v)
            if img is None:
                return ""
            return f'<div class="eq" id="{v.id}">{img}</div>'
        if v.kind == "textbox":
            parts.append(f'<div class="boxwrap" id="{v.id}">')
            if v.caption_above and cap:
                parts.append(cap)
            for gi, group in enumerate(v.box_groups):
                parts.append('<div class="box">')
                for p in group:
                    parts.append(self._block(p))
                parts.append("</div>")
            if not v.caption_above and cap:
                parts.append(cap)
            parts.append("</div>")
            return "\n".join(parts)
        imgs = self._figure_images(v)
        cls = "figure table" if v.kind == "table" else "figure"
        parts.append(f'<div class="{cls}" id="{v.id}">')
        if v.caption_above and cap:
            parts.append(cap)
        for im in imgs:
            parts.append(f'<div class="img">{im}</div>')
        if not v.caption_above and cap:
            parts.append(cap)
        parts.append("</div>")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # images

    def _page_index(self, pno: int) -> list:
        if pno not in self._page_glyph_index:
            pd = self.pages[pno]
            items = []
            for row in pd.rows:
                items.append((row.box, [g.box for g in row.glyphs], [id(g) for g in row.glyphs]))
            self._page_glyph_index[pno] = items
        return self._page_glyph_index[pno]

    def _foreign(self, pno: int, area: Box, member_ids: set[int], member_graphics: set[int]) -> list[Box]:
        out = []
        for rbox, boxes, ids in self._page_index(pno):
            if not G.overlaps(rbox, area):
                continue
            for b, i in zip(boxes, ids):
                if i not in member_ids and G.overlaps(b, area):
                    out.append(b)
        for gr in self.pages[pno].graphics:
            if id(gr) not in member_graphics and gr.visible and G.overlaps(gr.box, area):
                if G.contains(gr.box, area):
                    continue  # a background behind the whole region
                out.append(gr.box)
        for gl in self.pages[pno].rotated:
            if id(gl) not in member_ids and G.overlaps(gl.box, area):
                out.append(gl.box)
        return out

    def _img_tag(self, asset: ImageAsset, cls: str, width_em: float, alt: str, extra_style: str = "") -> str:
        style = f"width:{width_em:.2f}em;{extra_style}"
        return f'<img class="{cls}" src="../{asset.name}" alt="{esc(alt)}" style="{style}"/>'

    def _equation_image(self, v: Visual) -> str | None:
        reg = v.region
        members = [g.box for s in reg.segs for g in s.glyphs] + [g.box for s in reg.eqnum_segs for g in s.glyphs]
        members += [g.box for g in reg.graphics]
        ids = {id(g) for s in reg.segs + reg.eqnum_segs for g in s.glyphs}
        gids = {id(g) for g in reg.graphics}
        area = G.expand(G.union(members), 0.6 * self.body)
        foreign = self._foreign(reg.page, area, ids, gids)
        allow = [ink_box(g) for s in reg.segs + reg.eqnum_segs for g in s.glyphs] + [g.box for g in reg.graphics]
        res = self.r.crop(reg.page, members, foreign, pad=0.5 * self.body, margin=0.8, allow_boxes=allow)
        if res is None:
            return None
        arr, ink = res
        scale = self.r.page_scale(reg.page)
        em = self.body * scale
        arr = squeeze_gap(arr, int(3 * em), int(1.5 * em))
        parts = split_wide(arr, int(self.o.max_equation_em * em), int(1.0 * em), int(4 * em))
        alt = " ".join(s.text for s in sorted(reg.segs, key=lambda s: (s.baseline, s.x0)))
        alt = (f"Equation {v.label}: " if v.label else "Equation: ") + alt
        tags = []
        for k, part in enumerate(parts):
            asset = self.r.save(part, ink, "eq", scale=scale)
            width_em = part.shape[1] / scale / self.body
            a = alt if len(parts) == 1 else f"{alt} (part {k + 1} of {len(parts)})"
            tags.append(self._img_tag(asset, "eq", width_em, a[:400]))
        return "<br/>".join(tags)

    def _figure_images(self, v: Visual) -> list[str]:
        reg = v.region
        glyphs = [g for s in reg.segs for g in s.glyphs] + list(reg.rotated)
        items: list[tuple[Box, int, bool]] = [(g.box, id(g), True) for g in glyphs] + [(gr.box, id(gr), False) for gr in reg.graphics]
        if not items:
            return []
        panels = [G.union(b for b, _, _ in items)]
        if self.o.split_panels and v.kind == "figure":
            panels = self._panels(reg, items)
        out = []
        scale = self.r.page_scale(reg.page)
        for k, pbox in enumerate(panels):
            mem = [(b, i, is_g) for b, i, is_g in items if G.fraction_inside(b, G.expand(pbox, 0.5)) > 0.5]
            if not mem:
                continue
            members = [b for b, _, _ in mem]
            ids = {i for _, i, is_g in mem if is_g}
            gids = {i for _, i, is_g in mem if not is_g}
            # glyphs/graphics of the same region but other panels are foreign here
            area = G.expand(G.union(members), 0.5 * self.body)
            foreign = self._foreign(reg.page, area, ids, gids)
            gl_by_id = {id(g): g for g in glyphs}
            allow = [ink_box(gl_by_id[i]) if is_g and i in gl_by_id else b for b, i, is_g in mem]
            res = self.r.crop(
                reg.page, members, foreign, pad=0.4 * self.body, margin=1.5,
                limit=G.expand(pbox, 0.45 * self.body) if len(panels) > 1 else None, allow_boxes=allow,
            )
            if res is None:
                continue
            arr, ink = res
            photo = any(gr.kind == "image" for gr in reg.graphics) and is_photographic(arr)
            rotate = False
            width_em = arr.shape[1] / scale / self.body
            if self.o.rotate_wide and width_em > 30 and arr.shape[1] > 2.2 * arr.shape[0]:
                arr = np.rot90(arr, 1).copy()
                rotate = True
                width_em = arr.shape[1] / scale / self.body
            asset = self.r.save(arr, ink, "tab" if v.kind == "table" else "fig", photo=photo, scale=scale)
            label = v.label or ("Table" if v.kind == "table" else "Figure")
            alt = label + (f" (part {k + 1} of {len(panels)})" if len(panels) > 1 else "")
            if rotate:
                alt += " (rotated)"
            out.append(self._img_tag(asset, "fig", width_em, alt))
        return out

    def _panels(self, reg: Region, items: list[tuple[Box, int, bool]]) -> list[Box]:
        """Split side-by-side sub-figures so they can be stacked on a narrow screen."""
        full = G.union(b for b, _, _ in items)
        col = column_of(full, self.layouts[reg.page].columns)
        if G.width(full) < 0.55 * (col[1] - col[0]) and G.width(full) < 25 * self.body:
            return [full]
        boxes = [b for b, _, _ in items]
        graphic_boxes = [b for b, _, is_g in items if not is_g]
        has_subcaps = any(SUBCAPTION_RE.match(s.text) for s in reg.segs)

        def cuts(bs: list[Box], axis: int, min_gap: float) -> list[float]:
            lo, hi = (0, 2) if axis == 0 else (1, 3)
            iv = sorted((b[lo], b[hi]) for b in bs)
            out = []
            cur = iv[0][1]
            for a, b in iv[1:]:
                if a - cur >= min_gap:
                    out.append((a + cur) / 2)
                cur = max(cur, b)
            return out

        def split(box: Box, bs: list[Box], axis: int, min_gap: float) -> list[Box]:
            inside = [b for b in bs if G.fraction_inside(b, G.expand(box, 0.5)) > 0.5]
            if not inside:
                return [box]
            cs = cuts(inside, axis, min_gap)
            if not cs:
                return [box]
            edges = [box[axis]] + cs + [box[axis + 2]]
            parts = []
            for a, b in zip(edges, edges[1:]):
                pb = (a, box[1], b, box[3]) if axis == 0 else (box[0], a, box[2], b)
                mem = [x for x in inside if G.fraction_inside(x, G.expand(pb, 0.5)) > 0.5]
                if mem:
                    parts.append(G.union(mem))
            return parts

        def valid_columns(parts: list[Box], whole: Box) -> bool:
            if len(parts) < 2 or len(parts) > 4:
                return False
            for p in parts:
                if G.width(p) < 0.2 * G.width(whole):
                    return False
                if not any(G.fraction_inside(g, G.expand(p, 0.5)) > 0.5 and G.area(g) > 4 for g in graphic_boxes):
                    return False
            return True

        vgap = 1.2 * self.body
        if has_subcaps:
            cols = split(full, boxes, 0, vgap)
            return cols if valid_columns(cols, full) else [full]
        rows = split(full, boxes, 1, 1.0 * self.body)
        result: list[Box] = []
        any_split = False
        for rb in rows:
            cols = split(rb, boxes, 0, vgap)
            if valid_columns(cols, rb):
                result.extend(cols)
                any_split = True
            else:
                result.append(rb)
        if not any_split:
            return [full]
        # rows that are only text (shared titles/legends) stay attached to neighbours
        return result

    def _math_image(self, t: MathImage) -> str:
        members = [g.box for g in t.glyphs] + [gr.box for gr in t.graphics]
        ids = {id(g) for g in t.glyphs}
        gids = {id(gr) for gr in t.graphics}
        area = G.expand(G.union(members), 0.5 * t.size)
        foreign = self._foreign(t.page, area, ids, gids)
        allow = [ink_box(g) for g in t.glyphs] + [gr.box for gr in t.graphics]
        res = self.r.crop(t.page, members, foreign, pad=0.35 * t.size, margin=0.0, allow_boxes=allow)
        if res is None:
            return esc(t.alt)
        arr, ink = res
        scale = self.r.page_scale(t.page)
        axis = t.baseline - 0.25 * t.size
        up = axis - ink[1]
        down = ink[3] - axis
        h = max(up, down) + 0.06 * t.size
        top_pad = int(round((h - up) * scale))
        bot_pad = int(round((h - down) * scale))
        side = int(round(0.04 * t.size * scale))
        arr = np.pad(arr, ((max(0, top_pad), max(0, bot_pad)), (side, side), (0, 0)), constant_values=255)
        asset = self.r.save(arr, ink, "m", scale=scale)
        height_em = arr.shape[0] / scale / t.size
        width_em = arr.shape[1] / scale / t.size
        return (
            f'<img class="m" src="../{asset.name}" alt="{esc(t.alt)}" '
            f'style="height:{height_em:.3f}em;width:{width_em:.3f}em"/>'
        )

    # ------------------------------------------------------------------
    # inline

    def _inline(self, p: Paragraph) -> str:
        tokens = tokenize_paragraph(p, self.ctx)
        if p.kind in ("heading", "title"):
            for t in tokens:
                if isinstance(t, TextRun) and not t.math:
                    t.bold = False
        return self._serialize(tokens, p)

    def _serialize(self, tokens: list[Token], p: Paragraph) -> str:
        out: list[str] = []
        open_link: str | None = None
        open_link_obj = None

        def close_link() -> None:
            nonlocal open_link, open_link_obj
            if open_link is not None:
                out.append("</a>")
                open_link = None
                open_link_obj = None

        for t in tokens:
            link = getattr(t, "link", None) if not isinstance(t, NoteRef) else None
            if isinstance(t, NoteRef):
                close_link()
                out.append(self._noteref(t))
                continue
            if isinstance(t, LineBreak):
                close_link()
                out.append("<br/>")
                continue
            if isinstance(t, TextRun) and link is None and open_link is not None and not t.text.strip():
                out.append(_run_html(t))  # keep spaces between parts of the same link inside it
                continue
            if link is not open_link_obj:
                href = self._href(link) if link is not None else None
                if href is not None and href == open_link:
                    open_link_obj = link
                else:
                    close_link()
                    if href:
                        out.append(f'<a href="{esc(href)}">')
                        open_link = href
                        open_link_obj = link
            if isinstance(t, MathImage):
                out.append(self._math_image(t))
            elif isinstance(t, TextRun):
                out.append(_run_html(t))
        close_link()
        s = "".join(out).strip()
        s = re.sub(r"\s+(</a>)", r"\1 ", s)
        return re.sub(r"\s{2,}", " ", s).strip()

    def _noteref(self, t: NoteRef) -> str:
        f = None
        if t.link is not None and t.link.target_page is not None:
            f = self._footnote_at(t.link.target_page, t.link.target_point)
        if f is None:
            for cand in self.fn_targets.get(t.page, []):
                if cand.marker and cand.marker == t.marker:
                    f = cand
                    break
        mark = esc(_marker_text(t.marker))
        if f is None:
            return f"<sup>{mark}</sup>"
        n = self._fn_refcount.get(f.id, 0) + 1
        self._fn_refcount[f.id] = n
        rid = f"{f.id}-ref{n}"
        f.ref_ids.append(rid)
        href = self._file_ref(f.chapter, f.id)
        return f'<a epub:type="noteref" class="noteref" id="{rid}" href="{esc(href)}"><sup>{mark}</sup></a>'


def _marker_text(m: str) -> str:
    return m.replace("∗", "*")


def _plain(p: Paragraph) -> str:
    return re.sub(r"\s+", " ", " ".join(l.text for l in p.lines)).strip()


def _run_html(r: TextRun) -> str:
    text = esc(r.text)
    if not text:
        return ""
    if r.mono:
        text = f'<span class="mono">{text}</span>'
    if r.italic:
        text = f"<i>{text}</i>"
    if r.bold:
        text = f"<b>{text}</b>"
    style = []
    if r.color:
        style.append(f"color:#{r.color:06x}")
    if r.scale != 1.0 and r.vpos == 0:
        style.append(f"font-size:{r.scale:.2f}em")
    if style:
        text = f'<span style="{";".join(style)}">{text}</span>'
    if r.vpos == 1:
        text = f"<sup>{text}</sup>"
    elif r.vpos == -1:
        text = f"<sub>{text}</sub>"
    return text
