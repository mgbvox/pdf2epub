"""Document-wide statistics: body text size, math fonts, page furniture, vocabulary."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .extract import Glyph, PageData, Row
from .fonts import family_key

_WORD = re.compile(r"[A-Za-z][A-Za-z\-']*[A-Za-z]|[A-Za-z]")
_PAGE_NUMBER = re.compile(r"^\s*(page\s*)?(\d{1,4}|[ivxlcdm]{1,7})(\s*(/|of)\s*\d{1,4})?\s*$", re.I)


@dataclass
class DocStats:
    body_size: float = 10.0
    body_families: set[str] = field(default_factory=set)
    math_fonts: set[str] = field(default_factory=set)
    mono_fonts: set[str] = field(default_factory=set)
    line_step: float = 12.0  # typical baseline-to-baseline distance of body text
    vocab: Counter = field(default_factory=Counter)
    page_count: int = 0

    def is_body_size(self, size: float, tol: float = 0.12) -> bool:
        return abs(size - self.body_size) <= tol * self.body_size


def compute_stats(pages: list[PageData]) -> DocStats:
    stats = DocStats(page_count=len(pages))
    size_count: Counter = Counter()
    font_count: Counter = Counter()
    for pd in pages:
        for row in pd.rows:
            for g in row.glyphs:
                size_count[round(g.size * 2) / 2] += 1
                font_count[g.font] += 1
    if size_count:
        stats.body_size = size_count.most_common(1)[0][0]

    # Body families: the families of non-math fonts that carry most of the text
    total_text = sum(n for f, n in font_count.items() if not _is_math_only(pages, f))
    fam_count: Counter = Counter()
    for f, n in font_count.items():
        if not _is_math_only(pages, f):
            fam_count[family_key(f)] += n
    acc = 0
    for fam, n in fam_count.most_common():
        if acc >= 0.8 * total_text and n < 0.05 * total_text:
            break
        stats.body_families.add(fam)
        acc += n

    infos = _font_infos(pages)
    stats.mono_fonts = {f for f, info in infos.items() if info.mono}
    stats.math_fonts = _math_fonts(pages, infos, stats.body_families)

    for pd in pages:
        for row in pd.rows:
            for g in row.glyphs:
                g.math = g.font in stats.math_fonts
            # a TeX text font promoted to math (\mathbf{x}) is also used for text such as
            # bold headings: runs of it that spell words stay text
            if any(g.math and infos[g.font].math_only for g in row.glyphs):
                continue  # \text{MultiHead} inside a formula stays math
            for run, _before, _after in _font_runs(row):
                if run[0].math and not infos[run[0].font].math_only and _WORDY.search("".join(g.c for g in run)):
                    for g in run:
                        g.math = False

    stats.line_step = _line_step(pages, stats.body_size)
    stats.vocab = _vocabulary(pages)
    return stats


_info_cache: dict[int, dict] = {}


def _font_infos(pages: list[PageData]) -> dict:
    key = id(pages)
    if key in _info_cache:
        return _info_cache[key]
    infos = {}
    for pd in pages:
        for row in pd.rows:
            for g in row.glyphs:
                if g.font not in infos and g.info is not None:
                    infos[g.font] = g.info
    _info_cache.clear()
    _info_cache[key] = infos
    return infos


def _is_math_only(pages: list[PageData], font: str) -> bool:
    info = _font_infos(pages).get(font)
    return bool(info and info.math_only)


_WORDY = re.compile(r"[A-Za-z]?[a-z]{2,}")


def _math_fonts(pages: list[PageData], infos: dict, body_families: set[str]) -> set[str]:
    math = {f for f, info in infos.items() if info.math_only}
    candidates = {
        f for f, info in infos.items()
        if info.tex_family and family_key(f) not in body_families and f not in math
    }
    if not candidates:
        return math
    # Iteratively add TeX text fonts that mostly appear glued to known math.
    changed = True
    while changed and candidates:
        changed = False
        touch: Counter = Counter()
        runs: Counter = Counter()
        for pd in pages:
            for row in pd.rows:
                for run, before, after in _font_runs(row):
                    f = run[0].font
                    if f not in candidates:
                        continue
                    runs[f] += 1
                    scripted = any(g.level != 0 for g in run)
                    near = False
                    for nb in (before, after):
                        if nb is None:
                            continue
                        gap = (run[0].x0 - nb.x1) if nb is before else (nb.x0 - run[-1].x1)
                        if nb.font in math and gap < 0.4 * row.size:
                            near = True
                    if near or scripted:
                        touch[f] += 1
        for f in list(candidates):
            if runs[f] and touch[f] / runs[f] >= 0.3:
                math.add(f)
                candidates.discard(f)
                changed = True
    return math


def _font_runs(row: Row):
    gs = row.glyphs
    i = 0
    while i < len(gs):
        j = i
        while j + 1 < len(gs) and gs[j + 1].font == gs[i].font:
            j += 1
        before = gs[i - 1] if i > 0 else None
        after = gs[j + 1] if j + 1 < len(gs) else None
        yield gs[i : j + 1], before, after
        i = j + 1


def _line_step(pages: list[PageData], body: float) -> float:
    steps: Counter = Counter()
    for pd in pages:
        prev = None
        for row in sorted(pd.rows, key=lambda r: r.baseline):
            if abs(row.size - body) > 0.12 * body:
                continue
            if prev is not None:
                d = row.baseline - prev.baseline
                if 0.9 * body < d < 2.0 * body and G_h_overlap(prev, row) > 0:
                    steps[round(d * 4) / 4] += 1
            prev = row
    if not steps:
        return 1.2 * body
    return steps.most_common(1)[0][0]


def G_h_overlap(a: Row, b: Row) -> float:
    return min(a.box[2], b.box[2]) - max(a.box[0], b.box[0])


def _vocabulary(pages: list[PageData]) -> Counter:
    vocab: Counter = Counter()
    for pd in pages:
        for row in pd.rows:
            words = _WORD.findall(row.text)
            # skip the last word: it may be a hyphenation fragment
            for w in words[:-1]:
                vocab[w.lower()] += 1
            if words and not row.text.rstrip().endswith("-"):
                vocab[words[-1].lower()] += 1
    return vocab


# ---------------------------------------------------------------------------
# furniture (running heads, page numbers, repeated decorations)


def mark_furniture(pages: list[PageData], stats: DocStats) -> None:
    n = len(pages)
    if n == 0:
        return
    keys: dict[tuple, set[int]] = defaultdict(set)
    candidates: list[tuple[Row, tuple]] = []
    for pd in pages:
        h = pd.height
        for row in pd.rows:
            in_margin = row.box[3] < 0.11 * h or row.box[1] > 0.89 * h
            if not in_margin:
                continue
            text = row.text.strip()
            if _PAGE_NUMBER.match(text):
                row.role = "furniture"
                continue
            norm = re.sub(r"\d+", "#", text.lower())
            norm = re.sub(r"\s+", " ", norm)
            key = (round(row.box[1] / 6), norm)
            keys[key].add(pd.index)
            candidates.append((row, key))
    threshold = max(3, int(0.3 * n + 0.5)) if n >= 4 else n + 1
    for row, key in candidates:
        if len(keys[key]) >= threshold:
            row.role = "furniture"

    # repeated graphics at identical positions (header bars, logos)
    gkeys: dict[tuple, set[int]] = defaultdict(set)
    for pd in pages:
        for gr in pd.graphics:
            k = tuple(round(v / 3) for v in gr.box) + (gr.kind,)
            gkeys[k].add(pd.index)
    for pd in pages:
        for gr in pd.graphics:
            k = tuple(round(v / 3) for v in gr.box) + (gr.kind,)
            if len(gkeys[k]) >= threshold:
                h = pd.height
                if gr.box[3] < 0.12 * h or gr.box[1] > 0.88 * h or gr.box[2] < 0.12 * pd.width or gr.box[0] > 0.88 * pd.width:
                    gr.role = "furniture"


def math_glyph_ratio(glyphs: list[Glyph]) -> float:
    if not glyphs:
        return 0.0
    return sum(1 for g in glyphs if g.math) / len(glyphs)
