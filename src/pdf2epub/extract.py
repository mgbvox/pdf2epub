"""Low-level extraction of glyphs, vector graphics, images and links from PDF pages."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

import pymupdf

from . import geometry as G
from .fonts import FontInfo, font_info
from .geometry import Box

LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st", "Ꜳ": "AA", "Ꜵ": "AO", "Ꜷ": "AU", "Ꜹ": "AV", "Ꜽ": "AY", "Œ": "Œ"}

TEXT_FLAGS = (
    pymupdf.TEXT_PRESERVE_LIGATURES
    | pymupdf.TEXT_PRESERVE_WHITESPACE
    | pymupdf.TEXT_MEDIABOX_CLIP
    | pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE
)


@dataclass(slots=True, eq=False)
class Glyph:
    c: str
    x0: float
    y0: float
    x1: float
    y1: float
    ox: float  # origin x (pen position)
    oy: float  # baseline y
    size: float
    font: str
    color: int
    flags: int
    space_before: bool = False  # the PDF / MuPDF put a space before this glyph
    info: FontInfo | None = None
    math: bool = False  # set by document analysis
    level: int = 0  # 0 = base, 1 = superscript, -1 = subscript (relative to row)

    @property
    def box(self) -> Box:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass(slots=True, eq=False)
class Fragment:
    """A run of glyphs MuPDF considered one line (may include scripts)."""

    glyphs: list[Glyph]
    size: float
    baseline: float
    box: Box
    main_box: Box  # box of the glyphs set in the main size (scripts excluded)


@dataclass(eq=False)
class Row:
    """A visual text line: a main baseline plus attached sub/superscripts."""

    glyphs: list[Glyph]
    baseline: float
    size: float
    page: int
    box: Box = G.EMPTY
    row_id: int = -1  # identifies the parent row for segments
    seg_index: int = 0
    # Filled in by layout analysis
    role: str = "text"

    def __post_init__(self) -> None:
        if self.glyphs and G.is_empty(self.box):
            self.box = G.union(g.box for g in self.glyphs)

    @property
    def x0(self) -> float:
        return self.box[0]

    @property
    def x1(self) -> float:
        return self.box[2]

    @property
    def y0(self) -> float:
        return self.box[1]

    @property
    def y1(self) -> float:
        return self.box[3]

    @property
    def core(self) -> Box:
        """Box spanning the main font's ascent/descent (ignores tall scripts)."""
        return (self.box[0], self.baseline - 0.72 * self.size, self.box[2], self.baseline + 0.24 * self.size)

    @property
    def text(self) -> str:
        return glyph_text(self.glyphs, self.size)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Row(p{self.page} {self.box[0]:.0f},{self.box[1]:.0f}-{self.box[2]:.0f},{self.box[3]:.0f} s={self.size:.1f} {self.text[:50]!r})"


def needs_space(prev: Glyph, g: Glyph, size: float) -> bool:
    gap = g.x0 - prev.x1
    if gap < 0.04 * size:
        return False
    if g.space_before:
        return True
    return gap > 0.2 * min(size, max(prev.size, g.size))


def glyph_text(glyphs: list[Glyph], size: float) -> str:
    out: list[str] = []
    prev = None
    for g in glyphs:
        if prev is not None and needs_space(prev, g, size):
            out.append(" ")
        out.append(g.c)
        prev = g
    return "".join(out)


@dataclass(eq=False)
class Graphic:
    box: Box
    kind: str  # "path" | "image"
    fill: tuple | None = None
    stroke: tuple | None = None
    width: float = 0.0
    items: str = ""
    n_items: int = 0
    visible: bool = True
    hrule: bool = False
    vrule: bool = False
    closed_rect: bool = False  # rectangle / rounded rectangle outline or fill
    role: str = ""  # assigned during layout


@dataclass(eq=False)
class Link:
    box: Box
    uri: str | None = None
    target_page: int | None = None
    target_point: tuple[float, float] | None = None  # top-left origin coordinates
    name: str | None = None


@dataclass(eq=False)
class PageData:
    index: int
    width: float
    height: float
    rows: list[Row] = field(default_factory=list)
    rotated: list[Glyph] = field(default_factory=list)  # non-horizontal text
    graphics: list[Graphic] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)

    @property
    def box(self) -> Box:
        return (0.0, 0.0, self.width, self.height)


# ---------------------------------------------------------------------------
# glyphs


def extract_fragments(page: pymupdf.Page) -> tuple[list[Fragment], list[Glyph]]:
    raw = page.get_text("rawdict", flags=TEXT_FLAGS)
    frags: list[Fragment] = []
    rotated: list[Glyph] = []
    for block in raw.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            horizontal = abs(dy) < 0.02 and dx > 0 and line.get("wmode", 0) == 0
            glyphs: list[Glyph] = []
            pending_space = False
            for span in line.get("spans", []):
                size = float(span["size"])
                font = span["font"]
                flags = int(span["flags"])
                info = font_info(font, flags)
                for ch in span.get("chars", []):
                    c = LIGATURES.get(ch["c"], ch["c"])
                    if c.isspace():
                        pending_space = True
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    ox, oy = ch["origin"]
                    if size <= 0.5:
                        continue
                    g = Glyph(
                        c, x0, y0, x1, y1, ox, oy, size, info.name, int(span["color"]), flags,
                        space_before=pending_space, info=info,
                    )
                    pending_space = False
                    glyphs.append(g)
            if not glyphs:
                continue
            if not horizontal:
                rotated.extend(glyphs)
                continue
            frags.extend(_split_fragment(glyphs))
    return frags, rotated


def _split_fragment(glyphs: list[Glyph]) -> list[Fragment]:
    """Turn a MuPDF line into fragments, splitting where glyphs jump backwards."""
    out: list[list[Glyph]] = [[glyphs[0]]]
    for prev, g in zip(glyphs, glyphs[1:]):
        if g.x0 < prev.x0 - 2 * max(g.size, prev.size):
            out.append([g])
        else:
            out[-1].append(g)
    return [_make_fragment(gs) for gs in out]


def _make_fragment(glyphs: list[Glyph]) -> Fragment:
    weights: dict[float, float] = {}
    for g in glyphs:
        weights[round(g.size, 1)] = weights.get(round(g.size, 1), 0.0) + max(g.x1 - g.x0, 0.1)
    main = max(weights.items(), key=lambda kv: (kv[1], kv[0]))[0]
    biggest = max((g.size for g in glyphs if not g.font.upper().startswith(_EXT_FONT)), default=main)
    if biggest > main * 1.15:
        big = [g for g in glyphs if abs(g.size - biggest) < 0.15]
        big_bl = statistics.median(g.oy for g in big)
        small = [g for g in glyphs if g.size < biggest - 0.15]
        # small glyphs that sit off the big glyphs' baseline are scripts of them
        if small and sum(1 for g in small if abs(g.oy - big_bl) > 0.1 * biggest) >= 0.7 * len(small):
            main = round(biggest, 1)
    base = [g.oy for g in glyphs if abs(g.size - main) < 0.15]
    baseline = statistics.median(base) if base else glyphs[0].oy
    main_glyphs = [g for g in glyphs if abs(g.size - main) < 0.15 and abs(g.oy - baseline) < 0.15 * main]
    main_box = G.union(g.box for g in (main_glyphs or glyphs))
    return Fragment(glyphs, main, baseline, G.union(g.box for g in glyphs), main_box)


# ---------------------------------------------------------------------------
# rows


_EXT_FONT = ("CMEX", "LMEX", "EUEX", "TXEX", "PXEX", "STIXSIZE", "KATEX_SIZE", "MTEX", "ESINT", "NTXEXX", "RTXEX")


def is_extension_fragment(f: Fragment) -> bool:
    """Fragment made only of TeX math-extension glyphs (big delimiters, radicals, sums)."""
    return all(g.font.upper().startswith(_EXT_FONT) or g.c in "√∑∏∫∮⋃⋂⨁⨂" for g in f.glyphs)


def build_rows(frags: list[Fragment], page_index: int) -> list[Row]:
    """Group fragments into visual rows, attaching sub/superscripts to their base row."""
    order = sorted(frags, key=lambda f: (-f.size, f.baseline, f.box[0]))
    rows: list[dict] = []  # {"size","baseline","frags"}

    def best_row(f: Fragment) -> tuple[dict | None, bool]:
        best = None
        best_cost = 1e9
        maybe_script = False
        ext = is_extension_fragment(f)
        for r in rows:
            rs = r["size"]
            shift = f.baseline - r["baseline"]
            ratio = f.size / rs
            if ext and r["frags"] and not all(is_extension_fragment(x) for x in r["frags"]):
                # radicals / big operators: origin is not on the text baseline
                if -1.7 * rs <= shift <= 0.9 * rs and any(G.h_gap(f.box, rf.box) < 0.75 * rs for rf in r["frags"]):
                    cost = abs(shift) / rs + 0.2
                    if cost < best_cost:
                        best, best_cost = r, cost
                continue
            if abs(shift) > 1.0 * rs:
                continue
            # horizontal relationship with the row's fragments
            near = False
            overlapping_same_level = False
            for rf in r["frags"]:
                gap = G.h_gap(f.box, rf.box)
                if gap < 0.75 * rs:
                    near = True
                if ratio > 0.88 and abs(f.baseline - rf.baseline) < 0.2 * rs:
                    ov = G.h_overlap(f.main_box, rf.main_box)
                    narrow = min(G.width(f.main_box), G.width(rf.main_box))
                    if ov > max(1.0, 0.3 * narrow):
                        overlapping_same_level = True
            if overlapping_same_level:
                continue
            if ratio > 0.88:
                # same level: baselines must agree closely
                if abs(shift) <= 0.22 * min(rs, f.size):
                    cost = abs(shift) / rs
                    if cost < best_cost:
                        best, best_cost = r, cost
                continue
            # smaller text: same baseline (small caps, footnote marks) or script
            if abs(shift) <= 0.08 * rs:
                cost = 0.05
            elif -0.8 * rs <= shift < 0:  # superscript
                cost = abs(abs(shift) - 0.4 * rs) / rs + 0.1
            elif 0 < shift <= 0.52 * rs:  # subscript
                cost = abs(shift - 0.25 * rs) / rs + 0.1
            else:
                continue
            if not near:
                maybe_script = True
                continue
            if cost < best_cost:
                best, best_cost = r, cost
        return best, maybe_script

    deferred: list[Fragment] = []
    for f in order:
        best, maybe_script = best_row(f)
        if best is not None:
            best["frags"].append(f)
        elif maybe_script:
            deferred.append(f)  # retry once neighbouring fragments have joined rows
        else:
            rows.append({"size": f.size, "baseline": f.baseline, "frags": [f]})
    while deferred:
        progress = False
        remaining = []
        for f in deferred:
            best, _ = best_row(f)
            if best is not None:
                best["frags"].append(f)
                progress = True
            else:
                remaining.append(f)
        deferred = remaining
        if not progress and deferred:
            f = deferred.pop(0)
            rows.append({"size": f.size, "baseline": f.baseline, "frags": [f]})

    out: list[Row] = []
    for r in rows:
        glyphs = [g for f in r["frags"] for g in f.glyphs]
        size = r["size"]
        bl = r["baseline"]
        for g in glyphs:
            shift = g.oy - bl
            if g.font.upper().startswith(_EXT_FONT):
                g.level = 0
            elif g.size < 0.88 * size and shift < -0.1 * size:
                g.level = 1
            elif g.size < 0.88 * size and shift > 0.08 * size:
                g.level = -1
            elif shift < -0.25 * size:
                g.level = 1
            elif shift > 0.2 * size:
                g.level = -1
            else:
                g.level = 0
        glyphs.sort(key=lambda g: (g.x0, -g.level))
        out.append(Row(glyphs, bl, size, page_index))
    out.sort(key=lambda r: (r.baseline, r.box[0]))
    return out


def split_row(row: Row, cuts: list[float] | None = None) -> list[Row]:
    """Split a row at large horizontal gaps (column gutters, table cells, wrapped floats).

    The threshold adapts to the row's own word spacing so that loosely justified
    lines are not torn apart, while ``cuts`` (known column gutters) always split.
    """
    if len(row.glyphs) < 2:
        return [row]
    size = row.size
    gaps: list[tuple[float, float, int]] = []  # (gap, x_mid, index of right glyph)
    right = row.glyphs[0].x1
    for i, g in enumerate(row.glyphs[1:], start=1):
        gap = g.x0 - right
        if gap > 0.12 * size:
            gaps.append((gap, (g.x0 + right) / 2, i))
        right = max(right, g.x1)
    if not gaps:
        return [row]
    word_gaps = sorted(gp for gp, _, _ in gaps)
    median = word_gaps[len(word_gaps) // 2]
    thr = max(0.8 * size, min(2.5 * median, 1.6 * size))
    split_at = set()
    for gp, mid, i in gaps:
        if gp > thr:
            split_at.add(i)
        elif cuts and gp > 0.45 * size and any(abs(mid - c) < gp / 2 + 1 for c in cuts):
            split_at.add(i)
    if not split_at:
        return [row]
    groups: list[list[Glyph]] = [[]]
    for i, g in enumerate(row.glyphs):
        if i in split_at:
            groups.append([])
        groups[-1].append(g)
    return [Row(gs, row.baseline, row.size, row.page, seg_index=k) for k, gs in enumerate(groups) if gs]


# ---------------------------------------------------------------------------
# graphics


def _is_white(c: tuple | None) -> bool:
    return c is not None and len(c) >= 3 and min(c[:3]) > 0.97


def extract_graphics(page: pymupdf.Page) -> list[Graphic]:
    out: list[Graphic] = []
    pbox = (page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y1)
    try:
        drawings = page.get_drawings()
    except Exception:  # pragma: no cover - broken content streams
        drawings = []
    for d in drawings:
        r = d["rect"]
        box = (r.x0, r.y0, r.x1, r.y1)
        if not G.overlaps(box, pbox, tol=1):
            continue
        fill = d.get("fill")
        stroke = d.get("color")
        width = float(d.get("width") or 0.0)
        items = "".join(it[0] for it in d.get("items", []))
        dtype = d.get("type", "")
        has_fill = "f" in dtype and fill is not None and d.get("fill_opacity", 1) not in (0, 0.0)
        has_stroke = "s" in dtype and stroke is not None and d.get("stroke_opacity", 1) not in (0, 0.0)
        visible = (has_fill and not _is_white(fill)) or (has_stroke and not _is_white(stroke))
        # effective extent including stroke width for degenerate lines
        w = box[2] - box[0]
        h = box[3] - box[1]
        thick_h = max(h, width if has_stroke else 0.0)
        thick_w = max(w, width if has_stroke else 0.0)
        hrule = visible and thick_h <= 1.6 and w >= 6 and set(items) <= {"l", "r", "e", "q", "u"}
        vrule = visible and thick_w <= 1.6 and h >= 6 and set(items) <= {"l", "r", "e", "q", "u"}
        closed = (
            set(items) <= {"r", "e", "l", "c", "q", "u"}
            and len(items) <= 16
            and w > 20
            and h > 10
            and ("re" in items or "qu" in items or items.count("c") >= 4 or (items.count("l") >= 3 and has_fill))
        )
        out.append(
            Graphic(
                box=box, kind="path", fill=fill if has_fill else None, stroke=stroke if has_stroke else None,
                width=width, items=items, n_items=len(d.get("items", [])), visible=visible,
                hrule=hrule, vrule=vrule, closed_rect=closed,
            )
        )
    try:
        infos = page.get_image_info()
    except Exception:  # pragma: no cover
        infos = []
    for info in infos:
        b = info.get("bbox")
        if not b:
            continue
        box = G.intersect((b[0], b[1], b[2], b[3]), pbox)
        if G.width(box) < 1 or G.height(box) < 1:
            continue
        out.append(Graphic(box=box, kind="image", visible=True))
    return out


# ---------------------------------------------------------------------------
# links


def extract_links(page: pymupdf.Page, named: dict, matrices: list[pymupdf.Matrix]) -> list[Link]:
    out: list[Link] = []
    for ln in page.get_links():
        r = ln.get("from")
        if r is None:
            continue
        box = (r.x0, r.y0, r.x1, r.y1)
        kind = ln.get("kind")
        if kind == pymupdf.LINK_URI:
            out.append(Link(box, uri=ln.get("uri")))
            continue
        name = ln.get("nameddest") or None
        tpage = ln.get("page")
        point = None
        if name and name in named:
            dest = named[name]
            tpage = dest.get("page", tpage)
            to = dest.get("to")
            if to is not None and tpage is not None and 0 <= tpage < len(matrices):
                # Named destinations are expressed in PDF user space (origin bottom-left).
                p = pymupdf.Point(float(to[0]), float(to[1])) * matrices[tpage]
                point = (p.x, p.y)
        elif ln.get("to") is not None and tpage is not None and tpage >= 0:
            to = ln["to"]
            point = (float(to.x), float(to.y))
        if tpage is None or tpage < 0:
            continue
        out.append(Link(box, target_page=int(tpage), target_point=point, name=name))
    return out


# ---------------------------------------------------------------------------


def extract_page(doc: pymupdf.Document, index: int, named: dict, matrices: list) -> PageData:
    page = doc[index]
    rect = page.rect
    frags, rotated = extract_fragments(page)
    rows = build_rows(frags, index)
    pd = PageData(index=index, width=rect.width, height=rect.height, rows=rows, rotated=rotated)
    pd.graphics = extract_graphics(page)
    pd.links = extract_links(page, named, matrices)
    return pd


def page_matrices(doc: pymupdf.Document) -> list[pymupdf.Matrix]:
    """Per-page matrices mapping PDF user space to MuPDF's top-left page space."""
    return [p.transformation_matrix for p in doc]
