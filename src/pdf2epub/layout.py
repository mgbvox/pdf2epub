"""Page layout analysis.

Turns the rows/graphics of one page into an ordered list of *items*:

* text segments (``Row``) that belong to the reflowable text flow, and
* ``Region`` objects (figures, tables, boxed text, display equations) that are
  either rendered as faithful images or as specially styled HTML.

Footnotes are separated out so they can become pop-up notes.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from . import geometry as G
from .analysis import DocStats, math_glyph_ratio
from .extract import Glyph, Graphic, PageData, Row, split_row
from .geometry import Box

CAPTION_RE = re.compile(
    r"^\s*(?P<kind>Supplementary\s+Figure|Extended\s+Data\s+Fig(?:ure|\.)?|Figure|FIGURE|Fig\.?|FIG\.?|"
    r"Table|TABLE|Tab\.|Algorithm|ALGORITHM|Listing|LISTING|Scheme|Chart|Exhibit|Box|Plate)"
    r"\s*~?(?P<num>[A-Z]?\d+(?:\.\d+)*[a-z]?|[IVXLC]+)\s*(?P<delim>[:.|—–]|\s[-–—]\s)"
)
EQNUM_RE = re.compile(r"^\(\s*[A-Za-z]{0,3}[\d]+(?:\.\d+)*[a-z]?['′]?\s*\)$|^\(\s*[A-Z]\.\d+\s*\)$|^\(\s*[ivx]+\s*\)$")
SUBCAPTION_RE = re.compile(r"^\(([a-z]|[ivx]{1,4})\)\s")
BULLET_CHARS = set("•◦▪▫‣∙·●○■□–—-*")


@dataclass(eq=False)
class Region:
    kind: str  # figure | table | textbox | equation | rule
    page: int
    box: Box  # union of member content
    segs: list[Row] = field(default_factory=list)
    graphics: list[Graphic] = field(default_factory=list)
    rotated: list[Glyph] = field(default_factory=list)
    caption: list[Row] = field(default_factory=list)
    caption_above: bool = False
    label: str = ""  # "Figure 3", "Table 2", "(4)"
    eqnum_segs: list[Row] = field(default_factory=list)
    separators: list[float] = field(default_factory=list)  # y of rules inside text boxes
    id: str = ""

    def finalize(self) -> None:
        boxes = [s.box for s in self.segs] + [g.box for g in self.graphics] + [g.box for g in self.rotated]
        boxes += [s.box for s in self.eqnum_segs] if self.kind != "equation" else []
        if boxes:
            self.box = G.union(boxes)

    @property
    def outer_box(self) -> Box:
        boxes = [self.box] + [r.box for r in self.caption]
        return G.union(boxes)


@dataclass(eq=False)
class Footnote:
    page: int
    marker: str
    rows: list[Row]


@dataclass(eq=False)
class PageLayout:
    page: int
    items: list  # Row | Region in reading order
    footnotes: list[Footnote]
    columns: list[tuple[float, float]]
    width: float
    height: float
    dropped: list[Row] = field(default_factory=list)


# ---------------------------------------------------------------------------
# helpers


def seg_text(segs: list[Row]) -> str:
    return " ".join(s.text for s in segs)


def max_gap(segs: list[Row]) -> float:
    gaps = [b.x0 - a.x1 for a, b in zip(segs, segs[1:])]
    return max(gaps) if gaps else 0.0


def find_columns(segs: list[Row], stats: DocStats, page_w: float) -> list[tuple[float, float]]:
    body = [s for s in segs if stats.is_body_size(s.size, 0.2) and len(s.glyphs) >= 4]
    if not body:
        if segs:
            return [(min(s.x0 for s in segs), max(s.x1 for s in segs))]
        return [(0.1 * page_w, 0.9 * page_w)]
    lo = int(min(s.x0 for s in body))
    hi = int(max(s.x1 for s in body)) + 1
    hist = np.zeros(hi - lo + 2)
    for s in body:
        hist[int(s.x0) - lo : int(s.x1) - lo + 1] += 1
    top = np.percentile(hist[hist > 0], 90) if np.any(hist > 0) else 0
    low = hist <= max(0.0, 0.08 * top)
    gutters: list[tuple[int, int]] = []
    x = 0
    n = len(hist)
    min_w = 0.8 * stats.body_size
    while x < n:
        if low[x]:
            start = x
            while x < n and low[x]:
                x += 1
            if start > 0 and x < n and (x - start) >= min_w:
                left_cov = hist[max(0, start - 30) : start].max(initial=0)
                right_cov = hist[x : x + 30].max(initial=0)
                if left_cov >= 0.3 * top and right_cov >= 0.3 * top:
                    gutters.append((start + lo, x + lo))
        else:
            x += 1
    bounds = [lo] + [v for g in gutters for v in g] + [hi]
    cols = []
    for i in range(0, len(bounds), 2):
        a, b = bounds[i], bounds[i + 1]
        members = [s for s in body if s.x0 >= a - 2 and s.x1 <= b + 2]
        if members:
            left = _supported_edge([s.x0 for s in members], low=True)
            right = _supported_edge([s.x1 for s in members], low=False)
            cols.append((left, max(right, left + 10)))
        else:
            cols.append((float(a), float(b)))
    return cols


def _supported_edge(values: list[float], low: bool) -> float:
    """Leftmost (or rightmost) edge position shared by a decent share of the rows."""
    need = max(2, int(0.15 * len(values)))
    vals = sorted(values, reverse=not low)
    for v in vals:
        support = sum(1 for w in values if abs(w - v) <= 1.5)
        if support >= need:
            return float(v)
    return float(statistics.median(values))


def column_of(box: Box, columns: list[tuple[float, float]]) -> tuple[float, float]:
    best = columns[0]
    best_ov = -1e9
    for c in columns:
        ov = min(box[2], c[1]) - max(box[0], c[0])
        if ov > best_ov:
            best, best_ov = c, ov
    if len(columns) > 1 and G.width(box) > 1.5 * (best[1] - best[0]):
        return (min(c[0] for c in columns), max(c[1] for c in columns))
    return best


# ---------------------------------------------------------------------------
# graphics clustering


def cluster_graphics(items: list[Graphic], dist: float) -> list[list[Graphic]]:
    n = len(items)
    if n == 0:
        return []
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cell = max(dist * 4, 24.0)
    grid: dict[tuple[int, int], list[int]] = {}
    for i, g in enumerate(items):
        b = G.expand(g.box, dist / 2)
        for cx in range(int(b[0] // cell), int(b[2] // cell) + 1):
            for cy in range(int(b[1] // cell), int(b[3] // cell) + 1):
                grid.setdefault((cx, cy), []).append(i)
    for members in grid.values():
        if len(members) > 4000:
            members = members[:4000]
        for a_i in range(len(members)):
            a = members[a_i]
            ba = items[a].box
            for b in members[a_i + 1 :]:
                if find(a) == find(b):
                    continue
                if G.distance(ba, items[b].box) <= dist:
                    parent[find(a)] = find(b)
    groups: dict[int, list[Graphic]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(items[i])
    return list(groups.values())


@dataclass(eq=False)
class Cluster:
    graphics: list[Graphic]
    box: Box
    kind: str  # ink | frame | rules | grid
    used: bool = False


def classify_cluster(gs: list[Graphic]) -> str:
    if all(g.hrule or g.vrule for g in gs):
        if any(g.vrule for g in gs) and any(g.hrule for g in gs):
            return "grid"
        return "rules"
    non_rule = [g for g in gs if not (g.hrule or g.vrule)]
    if non_rule and all(g.kind == "path" and g.closed_rect for g in non_rule):
        # frame(s) plus optional separator rules inside them
        frames = top_level_frames(non_rule)
        if all(any(G.contains(G.expand(f.box, 2), g.box) for f in frames) for g in gs):
            return "frame"
    return "ink"


def top_level_frames(gs: list[Graphic]) -> list[Graphic]:
    """Closed rectangles not nested inside another one (border+fill pairs collapse)."""
    out: list[Graphic] = []
    for g in sorted((g for g in gs if g.closed_rect), key=lambda g: -G.area(g.box)):
        if not any(G.contains(G.expand(f.box, 2), g.box) for f in out):
            out.append(g)
    return sorted(out, key=lambda g: (g.box[1], g.box[0]))


# ---------------------------------------------------------------------------
# main entry


class PageAnalyzer:
    def __init__(self, pd: PageData, stats: DocStats):
        self.pd = pd
        self.stats = stats
        self.body = stats.body_size
        rows = [r for r in pd.rows if r.role != "furniture"]
        self.dropped = [r for r in pd.rows if r.role == "furniture"]
        self.rows = rows
        self.segs: list[Row] = []
        self.segs_of: dict[int, list[Row]] = {}
        self._split_rows(None)
        self.columns = find_columns(self.segs, stats, pd.width)
        if len(self.columns) > 1:
            gutters = [(a[1] + b[0]) / 2 for a, b in zip(self.columns, self.columns[1:])]
            self._split_rows(gutters)
            self.columns = find_columns(self.segs, stats, pd.width)
        self.assigned: dict[int, object] = {}  # id(seg) -> owner
        vis = [g for g in pd.graphics if g.visible and g.role != "furniture"]
        self.graphics = vis
        self.regions: list[Region] = []
        self.footnotes: list[Footnote] = []

    def _split_rows(self, cuts: list[float] | None) -> None:
        self.segs = []
        self.segs_of = {}
        for i, r in enumerate(self.rows):
            r.row_id = i
            parts = split_row(r, cuts)
            for p in parts:
                p.row_id = i
            self.segs.extend(parts)
            self.segs_of[i] = parts

    # -- utilities -------------------------------------------------------
    def free(self, seg: Row) -> bool:
        return id(seg) not in self.assigned

    def take(self, seg: Row, owner: object) -> None:
        self.assigned[id(seg)] = owner

    def col(self, box: Box) -> tuple[float, float]:
        return column_of(box, self.columns)

    def paragraph_like(self, row_id: int) -> bool:
        segs = self.segs_of[row_id]
        row = self.rows[row_id]
        if any(SUBCAPTION_RE.match(s.text) for s in segs):
            return False
        c = self.col(row.box)
        if not self.stats.is_body_size(row.size):
            return False
        if G.width(row.box) < 0.6 * (c[1] - c[0]):
            return False
        return max_gap(segs) < 2.5 * row.size

    # -- pipeline --------------------------------------------------------
    def run(self) -> PageLayout:
        self.detect_footnotes()
        captions = self.detect_captions()
        clusters = self.make_clusters()
        tables = self.detect_rule_tables(clusters)
        for cap in captions:
            self.region_for_caption(cap, clusters, tables)
        self.leftover_regions(clusters, tables)
        self.detect_equations()
        items = self.reading_order()
        return PageLayout(
            page=self.pd.index, items=items, footnotes=self.footnotes, columns=self.columns,
            width=self.pd.width, height=self.pd.height, dropped=self.dropped,
        )

    # -- footnotes -------------------------------------------------------
    def detect_footnotes(self) -> None:
        h = self.pd.height
        best = None
        for g in self.graphics:
            if not g.hrule or g.box[1] < 0.45 * h:
                continue
            c = self.col(g.box)
            cw = c[1] - c[0]
            w = G.width(g.box)
            if not (0.08 * cw <= w <= 0.55 * cw) or abs(g.box[0] - c[0]) > 0.06 * cw:
                continue
            below = [
                s for s in self.segs
                if s.box[1] > g.box[1] and s.x0 >= c[0] - 6 and s.x1 <= c[1] + 6 and self.free(s)
            ]
            if not below:
                continue
            first = min(below, key=lambda s: s.box[1])
            if first.box[1] - g.box[3] > 2.5 * self.body or first.size > 0.97 * self.body:
                continue
            if best is None or g.box[1] < best[0].box[1]:
                best = (g, below, c)
        if best is None:
            return
        rule, below, c = best
        rule.role = "footnote-rule"
        rows_by_id: dict[int, list[Row]] = {}
        for s in below:
            rows_by_id.setdefault(s.row_id, []).append(s)
        ordered = sorted(rows_by_id.items(), key=lambda kv: self.rows[kv[0]].baseline)
        notes: list[Footnote] = []
        prev_row = None
        for rid, segs in ordered:
            row = self.rows[rid]
            for s in segs:
                self.take(s, "footnote")
            marker = ""
            lead = []
            for gl in row.glyphs:
                if gl.level == 1 or (gl.size < 0.8 * row.size and gl.oy < row.baseline - 0.15 * row.size):
                    lead.append(gl)
                else:
                    break
            if lead and len(lead) <= 4:
                marker = "".join(g.c for g in lead)
            gap = row.box[1] - prev_row.box[3] if prev_row is not None else 0
            if marker or not notes or gap > 0.8 * row.size:
                notes.append(Footnote(self.pd.index, marker, [row]))
            else:
                notes[-1].rows.append(row)
            prev_row = row
        self.footnotes = notes

    # -- captions --------------------------------------------------------
    def detect_captions(self) -> list[dict]:
        caps = []
        used_rows: set[int] = set()
        order = sorted(range(len(self.rows)), key=lambda i: (self.rows[i].baseline, self.rows[i].x0))
        for idx, rid in enumerate(order):
            if rid in used_rows:
                continue
            segs = [s for s in self.segs_of[rid] if self.free(s)]
            if not segs:
                continue
            m = None
            for k, sg in enumerate(segs):
                m = CAPTION_RE.match(sg.text)
                if m:
                    segs = segs[k:]
                    break
            if not m:
                continue
            first = segs[0]
            # must start a block: previous overlapping row is far above or ended short
            prev = None
            for j in range(idx - 1, -1, -1):
                pr = self.rows[order[j]]
                if pr.baseline < first.baseline - 0.5 * first.size:
                    psegs = [x for x in self.segs_of[order[j]] if G.h_overlap(x.box, first.box) > 0]
                    if psegs:
                        prev = Row([g for x in psegs for g in x.glyphs], pr.baseline, pr.size, pr.page)
                        break
            if prev is not None:
                gap = first.core[1] - prev.core[3]
                c = self.col(prev.box)
                ended_short = prev.x1 < c[1] - 2 * prev.size
                if gap < 0.35 * first.size and not ended_short:
                    continue
            segs = [x for x in segs if x is first or G.h_gap(x.box, first.box) < 2 * first.size]
            cap_segs = list(segs)
            used_rows.add(rid)
            last = first
            # following lines of the caption paragraph
            for j in range(idx + 1, len(order)):
                nr_id = order[j]
                nr = self.rows[nr_id]
                if nr.baseline <= last.baseline + 0.3 * last.size:
                    continue
                if G.h_overlap(nr.box, first.box) <= 0:
                    continue
                nsegs = [s for s in self.segs_of[nr_id] if self.free(s) and G.h_overlap(s.box, first.box) > 0]
                if not nsegs:
                    break
                gap = nr.core[1] - last.core[3]
                if gap > 0.45 * last.size or abs(nr.size - first.size) > 0.12 * first.size:
                    break
                if CAPTION_RE.match(nsegs[0].text):
                    break
                if nsegs[0].x0 < first.x0 - 0.6 * first.size and G.width(nr.box) > 1.3 * G.width(G.union(s.box for s in cap_segs)):
                    break
                cap_segs.extend(nsegs)
                used_rows.add(nr_id)
                last = nr
            kind_word = m.group("kind").lower()
            kind = "table" if kind_word.startswith("tab") else "figure"
            label = f"{m.group('kind').rstrip('.').title() if not kind_word.startswith('fig') else 'Figure'} {m.group('num')}"
            if kind_word.startswith(("algorithm", "listing", "box")):
                label = f"{m.group('kind').title()} {m.group('num')}"
            for s in cap_segs:
                self.take(s, "caption")
            caps.append({"segs": cap_segs, "kind": kind, "label": label, "box": G.union(s.box for s in cap_segs)})
        return caps

    # -- graphics --------------------------------------------------------
    def make_clusters(self) -> list[Cluster]:
        gs = [g for g in self.graphics if g.role != "footnote-rule"]
        out = []
        for group in cluster_graphics(gs, 0.6 * self.body):
            box = G.union(g.box for g in group)
            kind = classify_cluster(group)
            has_image = any(g.kind == "image" for g in group)
            if kind == "ink" and G.height(box) < 1.3 * self.body and not has_image:
                # underlines, highlights, thick decorative rules
                for g in group:
                    g.role = "decoration"
                continue
            if kind == "ink" and G.width(box) < 0.8 * self.body and G.height(box) < 3 * self.body and not has_image:
                continue
            out.append(Cluster(group, box, kind))
        return out

    def detect_rule_tables(self, clusters: list[Cluster]) -> list[Region]:
        rules = []
        for cl in clusters:
            if cl.kind == "rules":
                rules.extend(g for g in cl.graphics if g.hrule)
        rules.sort(key=lambda g: g.box[1])
        stacks: list[list[Graphic]] = []
        for r in rules:
            placed = False
            for st in stacks:
                ref = max(st, key=lambda g: G.width(g.box))
                last = st[-1]
                compatible = (
                    (abs(r.box[0] - ref.box[0]) < 6 and abs(r.box[2] - ref.box[2]) < 6)
                    or (r.box[0] >= ref.box[0] - 3 and r.box[2] <= ref.box[2] + 3)
                    or (ref.box[0] >= r.box[0] - 3 and ref.box[2] <= r.box[2] + 3)
                )
                if not compatible:
                    continue
                if self._only_table_rows_between(last.box[3], r.box[1], G.union([ref.box, r.box]), clusters):
                    st.append(r)
                    placed = True
                    break
            if not placed:
                stacks.append([r])
        tables = []
        for st in stacks:
            if len(st) < 2:
                continue
            x0 = min(g.box[0] for g in st)
            x1 = max(g.box[2] for g in st)
            y0 = st[0].box[1]
            y1 = st[-1].box[3]
            inner = [
                s for s in self.segs
                if self.free(s) and s.core[1] >= y0 - 2 and s.core[3] <= y1 + 2 and s.x0 >= x0 - self.body and s.x1 <= x1 + self.body
            ]
            if not inner:
                continue
            row_ids = {s.row_id for s in inner}
            multi = sum(1 for rid in row_ids if len(self.segs_of[rid]) >= 2)
            small = sum(1 for rid in row_ids if self.rows[rid].size <= self.body * 1.05)
            if multi < 1 and small < 2:
                continue
            reg = Region("table", self.pd.index, G.union([g.box for g in st] + [s.box for s in inner]))
            reg.graphics = list(st)
            reg.segs = inner
            for s in inner:
                self.take(s, reg)
            for cl in clusters:
                if cl.kind == "rules" and all(g in st or not g.hrule for g in cl.graphics):
                    if any(g in st for g in cl.graphics):
                        cl.used = True
            # include partial rules (cmidrules) that fall inside
            for cl in clusters:
                if cl.kind == "rules" and not cl.used and G.contains(G.expand(reg.box, 2), cl.box):
                    cl.used = True
                    reg.graphics.extend(cl.graphics)
            tables.append(reg)
        return tables

    def _only_table_rows_between(self, ya: float, yb: float, xbox: Box, clusters: list) -> bool:
        band = (xbox[0], ya, xbox[2], yb)
        for cl in clusters:
            if cl.kind != "rules" and G.v_overlap(cl.box, band) > 2 and G.h_overlap(cl.box, band) > 0:
                return False
        for sg in self.segs:
            if not self.free(sg) and G.fraction_inside(sg.core, band) > 0.5:
                return False
        for rid, row in enumerate(self.rows):
            if row.core[1] >= ya - 1 and row.core[3] <= yb + 1 and G.h_overlap(row.box, xbox) > 0:
                if self.paragraph_like(rid) and not any(not self.free(s) for s in self.segs_of[rid]):
                    if len(self.segs_of[rid]) == 1:
                        return False
        return yb - ya < 40 * self.body

    # -- caption driven regions ------------------------------------------
    def region_for_caption(self, cap: dict, clusters: list[Cluster], tables: list[Region]) -> None:
        cbox = cap["box"]
        reg = None
        directions = ["below", "above"] if cap["kind"] == "table" else ["above", "below"]
        for d in directions:
            reg = self._attach_table(cap, tables, d) if cap["kind"] == "table" else None
            if reg is None:
                reg = self._region_near(cap, clusters, tables, d)
            if reg is not None:
                reg.caption_above = d == "below"
                break
        if reg is None:
            # Caption without detectable content: keep it as an ordinary paragraph.
            for s in cap["segs"]:
                self.assigned.pop(id(s), None)
            return
        reg.caption = cap["segs"]
        reg.label = cap["label"]
        if reg.kind == "figure" and cap["kind"] == "table":
            reg.kind = "table"
        if reg not in self.regions:
            self.regions.append(reg)

    def _attach_table(self, cap: dict, tables: list[Region], direction: str) -> Region | None:
        cbox = cap["box"]
        best = None
        best_d = 4 * self.body
        for t in tables:
            if t.caption or G.h_overlap(t.box, cbox) <= 0:
                continue
            d = t.box[1] - cbox[3] if direction == "below" else cbox[1] - t.box[3]
            if -2 <= d < best_d:
                best, best_d = t, d
        if best is not None:
            self._grow_text(best, small_only=True)
        return best

    def _region_near(self, cap: dict, clusters: list[Cluster], tables: list[Region], direction: str) -> Region | None:
        cbox = cap["box"]
        c = self.col(cbox)
        up = direction == "above"

        def vdist(b: Box) -> float:
            return cbox[1] - b[3] if up else b[1] - cbox[3]

        cands = []
        for cl in clusters:
            if cl.used or cl.kind == "rules":
                continue
            if G.h_overlap(cl.box, G.union([cbox, (c[0], cbox[1], c[1], cbox[3])])) <= 0:
                continue
            d = vdist(cl.box)
            if -0.5 * self.body <= d <= 12 * self.body and not self._paragraph_between(cl.box, cbox):
                cands.append((d, cl))
        for t in tables:
            if t.caption:
                continue
            d = vdist(t.box)
            if -0.5 * self.body <= d <= 5 * self.body and G.h_overlap(t.box, cbox) > 0:
                cands.append((d, t))
        if not cands:
            return self._textual_region(cap, direction)
        cands.sort(key=lambda x: x[0])
        d0, first = cands[0]
        if isinstance(first, Region):
            self._grow_text(first, small_only=True)
            return first
        reg = Region("figure", self.pd.index, first.box)
        members = [first]
        first.used = True
        # add side-by-side panels and stacked parts above the caption
        changed = True
        while changed:
            changed = False
            for cl in clusters:
                if cl.used or cl.kind == "rules" and not G.contains(G.expand(reg.box, 2), cl.box):
                    continue
                if cl.used:
                    continue
                vb = G.v_overlap(cl.box, reg.box)
                side = vb > 0.3 * min(G.height(cl.box), G.height(reg.box)) and G.h_gap(cl.box, reg.box) < 6 * self.body
                stacked = (
                    G.h_overlap(cl.box, G.union([reg.box, cbox])) > 0
                    and (
                        (up and cl.box[3] <= cbox[1] + 2 and G.v_gap(cl.box, reg.box) < 8 * self.body)
                        or (not up and cl.box[1] >= cbox[3] - 2 and G.v_gap(cl.box, reg.box) < 8 * self.body)
                    )
                    and not self._paragraph_between(cl.box, reg.box)
                )
                inside = G.contains(G.expand(reg.box, 2), cl.box)
                if side or stacked or inside:
                    if side and self._paragraph_between(cl.box, reg.box, horizontal=True):
                        continue
                    members.append(cl)
                    cl.used = True
                    reg.box = G.union([reg.box, cl.box])
                    changed = True
        for cl in members:
            reg.graphics.extend(cl.graphics)
        if all(cl.kind == "frame" for cl in members):
            reg.kind = "textbox"
        # absorb text between the content and the caption (sub-captions, labels)
        lo_y, hi_y = (reg.box[3], cbox[1]) if up else (cbox[3], reg.box[1])
        for s in self.segs:
            if not self.free(s):
                continue
            if s.core[1] >= lo_y - 2 and s.core[3] <= hi_y + 2 and G.h_overlap(s.box, reg.box) > 0.5 * G.width(s.box):
                reg.segs.append(s)
                self.take(s, reg)
        self._grow_text(reg, small_only=reg.kind != "textbox")
        return reg

    def _paragraph_between(self, a: Box, b: Box, horizontal: bool = False) -> bool:
        if horizontal:
            lo, hi = min(a[2], b[2]), max(a[0], b[0])
            band = (lo, max(a[1], b[1]), hi, min(a[3], b[3]))
        else:
            lo, hi = min(a[3], b[3]), max(a[1], b[1])
            band = (min(a[0], b[0]), lo, max(a[2], b[2]), hi)
        if G.width(band) <= 0 or G.height(band) <= 0:
            return False
        for sg in self.segs:
            if self.assigned.get(id(sg)) == "caption" and G.fraction_inside(sg.core, band) > 0.5:
                return True
        for rid, row in enumerate(self.rows):
            if G.fraction_inside(row.core, band) > 0.6 and self.paragraph_like(rid):
                if any(self.free(s) for s in self.segs_of[rid]):
                    return True
        return False

    def _textual_region(self, cap: dict, direction: str) -> Region | None:
        """A captioned float without graphics (e.g. a table drawn without rules)."""
        cbox = cap["box"]
        up = direction == "above"
        order = sorted(
            {s.row_id for s in self.segs if self.free(s)},
            key=lambda rid: self.rows[rid].baseline,
            reverse=up,
        )
        picked: list[int] = []
        edge = cbox[1] if up else cbox[3]
        for rid in order:
            row = self.rows[rid]
            if up and row.core[3] > cbox[1] + 1:
                continue
            if not up and row.core[1] < cbox[3] - 1:
                continue
            if G.h_overlap(row.box, cbox) <= 0 and G.h_overlap(row.box, (self.col(cbox)[0], 0, self.col(cbox)[1], 1)) <= 0:
                continue
            gap = (edge - row.core[3]) if up else (row.core[1] - edge)
            if gap > (2.0 if not picked else 1.2) * self.body:
                break
            if self.paragraph_like(rid) and len(self.segs_of[rid]) == 1:
                break
            if row.size > 1.15 * self.body:
                break
            picked.append(rid)
            edge = row.core[1] if up else row.core[3]
        if len(picked) < 2 and cap["kind"] == "table":
            return None
        if not picked:
            return None
        multi = sum(1 for rid in picked if len(self.segs_of[rid]) >= 2)
        if cap["kind"] == "table" and multi == 0:
            return None
        segs = [s for rid in picked for s in self.segs_of[rid] if self.free(s)]
        reg = Region(cap["kind"], self.pd.index, G.union(s.box for s in segs), segs=segs)
        for s in segs:
            self.take(s, reg)
        return reg

    def _grow_text(self, reg: Region, small_only: bool) -> None:
        """Pull in labels/legends/tick marks that belong to a graphic."""
        # text inside the region box
        changed = True
        rounds = 0
        while changed and rounds < 6:
            changed = False
            rounds += 1
            grow_box = G.expand(reg.box, 0.9 * self.body)
            for s in self.segs:
                if not self.free(s):
                    continue
                inside = G.fraction_inside(s.core, reg.box) > 0.5
                near = G.overlaps(s.core, grow_box)
                small = s.size < 0.93 * self.body
                if inside or (near and small and G.width(s.box) < 0.75 * G.width(reg.box) + self.body):
                    if not inside and self._is_flow_neighbor(s, reg):
                        continue
                    reg.segs.append(s)
                    self.take(s, reg)
                    reg.box = G.union([reg.box, s.box])
                    changed = True
        for gl in self.pd.rotated:
            if G.overlaps(gl.box, G.expand(reg.box, 1.2 * self.body)):
                reg.rotated.append(gl)
        if reg.rotated:
            reg.box = G.union([reg.box] + [g.box for g in reg.rotated if G.overlaps(g.box, G.expand(reg.box, 1.2 * self.body))])

    def _is_flow_neighbor(self, s: Row, reg: Region) -> bool:
        """Small text that is actually part of the surrounding flow (footnote-sized paragraphs)."""
        row = self.rows[s.row_id]
        c = self.col(row.box)
        return G.width(row.box) > 0.7 * (c[1] - c[0]) and len(self.segs_of[s.row_id]) == 1

    def leftover_regions(self, clusters: list[Cluster], tables: list[Region]) -> None:
        for t in tables:
            if t not in self.regions:
                self.regions.append(t)
        for cl in clusters:
            if cl.used or cl.kind == "rules":
                continue
            if cl.kind == "grid" and G.height(cl.box) < 1.5 * self.body:
                continue
            kind = {"frame": "textbox", "grid": "table"}.get(cl.kind, "figure")
            reg = Region(kind, self.pd.index, cl.box, graphics=list(cl.graphics))
            cl.used = True
            self._grow_text(reg, small_only=kind == "figure")
            if kind == "textbox" and not reg.segs:
                reg.kind = "figure"
            if kind == "figure" and G.width(reg.box) < 1.5 * self.body and G.height(reg.box) < 1.5 * self.body:
                # tiny glyph-like drawing inside running text (e.g. a check mark)
                for s in reg.segs:
                    self.assigned.pop(id(s), None)
                continue
            self.regions.append(reg)
        for reg in self.regions:
            if reg.kind == "textbox":
                frame = max((g for g in reg.graphics if g.closed_rect), key=lambda g: G.area(g.box), default=None)
                if frame is not None:
                    reg.separators = sorted(
                        g.box[1] for g in reg.graphics
                        if g.hrule and G.width(g.box) > 0.7 * G.width(frame.box)
                    )

    # -- equations -------------------------------------------------------
    def detect_equations(self) -> None:
        body = self.body
        free_rows = []
        for rid, row in enumerate(self.rows):
            segs = [s for s in self.segs_of[rid] if self.free(s)]
            if segs and len(segs) == len(self.segs_of[rid]):
                free_rows.append(rid)
        cand: dict[int, list[Row]] = {}
        eqnums: dict[int, list[Row]] = {}
        for rid in free_rows:
            row = self.rows[rid]
            segs = self.segs_of[rid]
            nums = [s for s in segs[1:] if EQNUM_RE.match(s.text.strip())] if len(segs) > 1 else []
            if len(segs) == 1 and EQNUM_RE.match(segs[0].text.strip()):
                c = self.col(row.box)
                if row.x1 > c[1] - 3 * body:
                    eqnums[rid] = segs
                continue
            content = [s for s in segs if s not in nums]
            glyphs = [g for s in content for g in s.glyphs]
            if not glyphs or glyphs[0].c in BULLET_CHARS and glyphs[0].c not in "-–—":
                continue
            base = [g for g in glyphs if g.level == 0 and g.c not in ".,;:"]
            ratio = max(math_glyph_ratio(glyphs), math_glyph_ratio(base))
            big_op = any(g.font.upper().startswith(("CMEX", "LMEX")) for g in glyphs)
            c = self.col(row.box)
            cbox = G.union(s.box for s in content)
            indent = cbox[0] - c[0]
            centred = abs((cbox[0] + cbox[2]) / 2 - (c[0] + c[1]) / 2) < 2 * body and G.width(cbox) < 0.92 * (c[1] - c[0])
            display_pos = indent > 1.2 * body or centred or bool(nums)
            if (ratio >= 0.45 or (big_op and ratio >= 0.25)) and display_pos:
                if not nums and not centred and self._continues_text_above(rid, cbox, c):
                    continue
                cand[rid] = nums
        if not cand:
            return
        # group candidates (plus attached small rows) by vertical proximity
        ids = sorted(cand, key=lambda r: self.rows[r].box[1])
        groups: list[list[int]] = []
        for rid in ids:
            row = self.rows[rid]
            if groups:
                last = groups[-1]
                gbox = G.union(self.rows[r].box for r in last)
                if row.box[1] - gbox[3] < 0.5 * body and G.h_overlap(row.box, gbox) > -2 * body:
                    last.append(rid)
                    continue
            groups.append([rid])
        for grp in groups:
            gbox = G.union(self.rows[r].box for r in grp)
            members = set(grp)
            # attach small rows (limits, fractions, scripts) and eq-number rows
            changed = True
            while changed:
                changed = False
                for rid in free_rows:
                    if rid in members:
                        continue
                    row = self.rows[rid]
                    if rid in eqnums:
                        if G.v_overlap(row.box, gbox) > 0 or abs(row.baseline - (gbox[1] + gbox[3]) / 2) < 0.7 * G.height(gbox):
                            members.add(rid)
                            changed = True
                        continue
                    near = G.v_gap(row.box, gbox) < 0.35 * body
                    inside_x = row.x0 >= gbox[0] - 1.5 * body and row.x1 <= gbox[2] + 1.5 * body
                    mathy = math_glyph_ratio(row.glyphs) >= 0.3 or row.size < 0.85 * body
                    if near and inside_x and mathy and len(row.glyphs) < 60:
                        members.add(rid)
                        gbox = G.union([gbox, row.box])
                        changed = True
            segs = [s for rid in members for s in self.segs_of[rid]]
            nums = [s for rid in members for s in (cand.get(rid) or [])]
            nums += [s for rid in members if rid in eqnums for s in eqnums[rid]]
            content = [s for s in segs if s not in nums]
            if not content:
                continue
            box = G.union(s.box for s in content)
            reg = Region("equation", self.pd.index, box, segs=content, eqnum_segs=nums)
            for g in self.graphics:
                if g.role:
                    continue
                if (g.hrule or g.kind == "path") and G.contains(G.expand(box, 2), g.box) and G.height(g.box) < 0.4 * G.height(box) + 2:
                    reg.graphics.append(g)
                    g.role = "equation"
            for s in segs:
                self.take(s, reg)
            reg.label = " ".join(n.text.strip() for n in sorted(nums, key=lambda s: s.baseline))
            self.regions.append(reg)

    def _continues_text_above(self, rid: int, cbox: Box, c: tuple[float, float]) -> bool:
        """Row looks like the wrapped tail of the text line above (e.g. inside a list item)."""
        row = self.rows[rid]
        prev = None
        for j, r in enumerate(self.rows):
            if j == rid or r.baseline >= row.baseline - 0.5 * row.size:
                continue
            if G.h_overlap(r.box, cbox) <= 0 or row.core[1] - r.core[3] > 0.9 * self.stats.line_step:
                continue
            if prev is None or r.baseline > prev.baseline:
                prev = r
        if prev is None or not self.stats.is_body_size(prev.size):
            return False
        if math_glyph_ratio(prev.glyphs) > 0.6:
            return False
        full = prev.x1 >= c[1] - 1.5 * row.size
        return full and cbox[0] <= prev.x0 + 2.5 * row.size

    # -- reading order ----------------------------------------------------
    def reading_order(self) -> list:
        for reg in self.regions:
            reg.finalize()
        items: list = [s for s in self.segs if self.free(s)]
        items.extend(self.regions)
        boxes = {id(it): (it.outer_box if isinstance(it, Region) else it.box) for it in items}
        return xy_cut(items, boxes, self.body)


def xy_cut(items: list, boxes: dict, body: float) -> list:
    if len(items) <= 1:
        return list(items)

    def gaps(axis: int) -> list[tuple[float, float]]:
        lo_i, hi_i = (0, 2) if axis == 0 else (1, 3)
        iv = sorted((boxes[id(it)][lo_i], boxes[id(it)][hi_i]) for it in items)
        out = []
        cur_hi = iv[0][1]
        for a, b in iv[1:]:
            if a > cur_hi:
                out.append((a - cur_hi, (a + cur_hi) / 2))
            cur_hi = max(cur_hi, b)
        return out

    vg = [g for g in gaps(0) if g[0] >= 0.8 * body]
    hg = gaps(1)
    best_h = max(hg, default=None)
    best_v = max(vg, default=None)
    if best_v is not None and (best_h is None or best_h[0] < 1.5 * body):
        cut = best_v[1]
        left = [it for it in items if (boxes[id(it)][0] + boxes[id(it)][2]) / 2 < cut]
        right = [it for it in items if (boxes[id(it)][0] + boxes[id(it)][2]) / 2 >= cut]
        if left and right:
            return xy_cut(left, boxes, body) + xy_cut(right, boxes, body)
    if best_h is not None:
        cut = best_h[1]
        top = [it for it in items if (boxes[id(it)][1] + boxes[id(it)][3]) / 2 < cut]
        bottom = [it for it in items if (boxes[id(it)][1] + boxes[id(it)][3]) / 2 >= cut]
        if top and bottom:
            return xy_cut(top, boxes, body) + xy_cut(bottom, boxes, body)
    return sorted(items, key=lambda it: (boxes[id(it)][1], boxes[id(it)][0]))


def analyze_page(pd: PageData, stats: DocStats) -> PageLayout:
    return PageAnalyzer(pd, stats).run()
