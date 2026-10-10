"""Inline content: split lines into styled text runs and inline math.

Inline math is kept as real (reflowable, searchable) text when it is *linear*:
plain symbols with at most simple sub/superscripts.  Anything two-dimensional
(fractions, stacked scripts, accents, radicals, big operators) or anything that
needs glyphs a Kindle font may lack becomes a small image cropped from the PDF
so that it is reproduced exactly.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from . import geometry as G
from .extract import Glyph, Graphic, Link, needs_space
from .geometry import Box
from .model import Line, Paragraph

# characters that Kindle's reading fonts reliably provide
SAFE_MATH = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "()[]{}.,;:!?'|/+=<>*-_%#&"
    "αβγδεζηθικλμνξοπρστυφχψωϵϑϕϱςϖΓΔΘΛΞΠΣΥΦΨΩ"
    "−×·±≤≥≠≈∞′″…∼"
)
OPERATORS = set("=+−-×·/<>≤≥≠≈±()[]{}|′'*") | set("0123456789")
MSBM_MAP = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ"}
CHAR_MAP = {"∗": "*", "‐": "-", "‑": "-", "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}
_MATH_ITALIC = re.compile(r"^(CMMI|LMMI|MTMI|TXMI|PXMI|RTXMI|NTXMI|NEWTXMI|NEWPXMI|ZXMI|CMMIB)", re.I)
_CALLIGRAPHIC = re.compile(r"^(CMSY|CMBSY|LMSY|EUSM|EUSB|RSFS|EUFM|EUFB|TXSY|PXSY|MTSY|RTXSY|NTXSY)", re.I)
_EXTENSION = re.compile(r"^(CMEX|LMEX|EUEX|TXEX|PXEX|STIXSIZE|KATEX_SIZE|MTEX|ESINT|NTXEXX|RTXEX)", re.I)
_URLISH = re.compile(r"(https?://|www\.|[/_.#?=&-])$")


@dataclass
class TextRun:
    text: str
    bold: bool = False
    italic: bool = False
    mono: bool = False
    color: int = 0
    scale: float = 1.0
    vpos: int = 0
    link: Link | None = None
    math: bool = False


@dataclass
class MathImage:
    glyphs: list[Glyph]
    page: int
    box: Box
    baseline: float
    size: float
    alt: str
    graphics: list[Graphic] = field(default_factory=list)
    link: Link | None = None
    asset: object | None = None  # filled in by the renderer


@dataclass
class NoteRef:
    marker: str
    page: int
    link: Link | None = None
    target: str | None = None
    ref_id: str = ""


@dataclass
class LineBreak:
    pass


Token = TextRun | MathImage | NoteRef | LineBreak


@dataclass
class InlineContext:
    graphics_by_page: dict[int, list[Graphic]]
    vocab: Counter
    body_size: float
    math_mode: str = "auto"  # auto | image
    footnote_markers: dict[int, set[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------


def tokenize_paragraph(para: Paragraph, ctx: InlineContext) -> list[Token]:
    tokens: list[Token] = []
    code = para.kind == "code"
    min_x = min((l.x0 for l in para.lines), default=0.0)
    for li, line in enumerate(para.lines):
        if li == 0 and para.kind == "li" and para.marker and line.glyphs and line.glyphs[0].c == para.marker:
            line = _strip_first(line)
        line_tokens = tokenize_line(line, ctx)
        if not line_tokens:
            continue
        if tokens:
            if para.keep_breaks:
                tokens.append(LineBreak())
            else:
                _join(tokens, line_tokens, ctx)
        if code:
            cw = _mono_width(line)
            indent = int(round((line.x0 - min_x) / cw)) if cw > 0 else 0
            if indent > 0:
                tokens.append(TextRun(" " * indent, mono=True))
        tokens.extend(line_tokens)
    return _merge_runs(tokens)


def _strip_first(line: Line) -> Line:
    from dataclasses import replace

    return replace(line, glyphs=line.glyphs[1:])


def _mono_width(line: Line) -> float:
    ws = [g.x1 - g.x0 for g in line.glyphs if g.info and g.info.mono and g.c.isalnum()]
    return sum(ws) / len(ws) if ws else 0.0


def _join(tokens: list[Token], nxt: list[Token], ctx: InlineContext) -> None:
    """Glue the next line onto the paragraph, undoing end-of-line hyphenation."""
    last = tokens[-1]
    first = nxt[0]
    if isinstance(last, TextRun) and isinstance(first, TextRun) and not last.math:
        # text at the end of the paragraph so far (may span several styled runs)
        tail_runs = []
        for tk in reversed(tokens):
            if not isinstance(tk, TextRun) or tk.math:
                break
            tail_runs.append(tk)
            if " " in tk.text:
                break
        t = "".join(r.text for r in reversed(tail_runs))
        head = "".join(r.text for r in _leading_runs(nxt))
        if t.endswith("-") and len(t) >= 2 and t[-2].isalpha() and head[:1].isalpha():
            w1 = re.search(r"([A-Za-z]+)-$", t)
            w2 = re.match(r"([A-Za-z]+)", head)
            if w1 and w2:
                a, b = w1.group(1), w2.group(1)
                joined, hyph = (a + b).lower(), (a + "-" + b).lower()
                vj, vh = ctx.vocab.get(joined, 0), ctx.vocab.get(hyph, 0)
                keep = vh > 0 and vh >= vj
                if not keep and vj == 0 and b[0].isupper() and not a[-1].isupper():
                    keep = True
                if not keep:
                    last.text = last.text[:-1]
                    if not last.text:
                        tokens.pop()
                return
        if t.endswith(("—", "–", "/")) and not t.endswith(" /"):
            return
        if last.mono and first.mono and _URLISH.search(t.split(" ")[-1] if t else ""):
            tail = t.split(" ")[-1]
            if "/" in tail or "://" in tail or "_" in tail or "." in tail:
                return
    tokens.append(TextRun(" "))


def _leading_runs(tokens: list[Token]) -> list[TextRun]:
    out = []
    for tk in tokens:
        if not isinstance(tk, TextRun) or tk.math:
            break
        out.append(tk)
        if " " in tk.text:
            break
    return out


def _merge_runs(tokens: list[Token]) -> list[Token]:
    out: list[Token] = []
    for t in tokens:
        if (
            isinstance(t, TextRun) and out and isinstance(out[-1], TextRun)
            and _style(out[-1]) == _style(t)
        ):
            out[-1].text += t.text
        else:
            out.append(t)
    return out


def _style(r: TextRun) -> tuple:
    return (r.bold, r.italic, r.mono, r.color, r.scale, r.vpos, id(r.link) if r.link else None, r.math)


# ---------------------------------------------------------------------------


def _link_at(g: Glyph, links: list[Link]) -> Link | None:
    cx = (g.x0 + g.x1) / 2
    cy = (g.y0 + g.y1) / 2
    for ln in links:
        b = ln.box
        if b[0] <= cx <= b[2] and b[1] - 0.5 <= cy <= b[3] + 0.5:
            return ln
    return None


PUNCT = set(".,;:!?")


def mark_math(line: Line, blocked: set[int] | None = None) -> list[bool]:
    gs = line.glyphs
    n = len(gs)
    blocked = blocked or set()
    is_math = [g.math and i not in blocked for i, g in enumerate(gs)]
    size = line.size
    changed = True
    while changed:
        changed = False
        for i, g in enumerate(gs):
            if is_math[i] or i in blocked:
                continue
            if g.c in PUNCT:
                # punctuation only belongs to math when math continues on both sides
                l_ok = i > 0 and is_math[i - 1] and g.x0 - gs[i - 1].x1 < 0.3 * size
                r_ok = i + 1 < n and is_math[i + 1] and gs[i + 1].x0 - g.x1 < 0.3 * size
                if l_ok and r_ok and g.level == gs[i - 1].level:
                    is_math[i] = changed = True
                continue
            for j in (i - 1, i + 1):
                if not (0 <= j < n) or not is_math[j]:
                    continue
                h = gs[j]
                gap = g.x0 - h.x1 if j < i else h.x0 - g.x1
                if g.level != 0 and gap < 0.35 * size and not (h.level == 0 and h.c in PUNCT):
                    is_math[i] = changed = True
                    break
                if g.c in OPERATORS and gap < 0.45 * size and not _inside_word(gs, i, size):
                    is_math[i] = changed = True
                    break
    return is_math


def _inside_word(gs: list[Glyph], i: int, size: float) -> bool:
    """True if glyph i is glued to text letters on both sides (e.g. hyphen in a word)."""
    left = gs[i - 1] if i > 0 else None
    right = gs[i + 1] if i + 1 < len(gs) else None
    ok_l = left is not None and not left.math and left.c.isalpha() and gs[i].x0 - left.x1 < 0.1 * size
    ok_r = right is not None and not right.math and right.c.isalpha() and right.x0 - gs[i].x1 < 0.1 * size
    return ok_l and ok_r


def tokenize_line(line: Line, ctx: InlineContext) -> list[Token]:
    gs = line.glyphs
    if not gs:
        return []
    links = line.links
    glinks = [_link_at(g, links) for g in gs]
    blocked = {i for i, ln in enumerate(glinks) if ln is not None and ln.name and ln.name.lower().startswith("hfootnote")}
    markers = ctx.footnote_markers.get(line.page, set())
    for i, g in enumerate(gs):
        if g.level == 1 and g.c in markers and i > 0 and _is_marker_context(gs, i):
            blocked.add(i)
    is_math = mark_math(line, blocked)
    tokens: list[Token] = []
    i = 0
    n = len(gs)
    size = line.size
    while i < n:
        g = gs[i]
        space = i > 0 and needs_space(gs[i - 1], g, size)
        ln = glinks[i]
        # footnote references
        if ln is not None and ln.name and ln.name.lower().startswith("hfootnote"):
            j = i
            while j < n and glinks[j] is ln:
                j += 1
            if space:
                tokens.append(TextRun(" "))
            tokens.append(NoteRef("".join(x.c for x in gs[i:j]).strip(), line.page, ln))
            i = j
            continue
        if i in blocked and not space:
            j = i
            while j < n and gs[j].level == 1 and j in blocked:
                j += 1
            m = "".join(x.c for x in gs[i:j])
            if m in markers:
                tokens.append(NoteRef(m, line.page, None))
                i = j
                continue
        if is_math[i]:
            j = i
            while j + 1 < n and is_math[j + 1]:
                j += 1
            run = gs[i : j + 1]
            if space:
                tokens.append(TextRun(" "))
            tokens.extend(_math_tokens(run, line, ctx, glinks[i : j + 1]))
            i = j + 1
            continue
        if space:
            tokens.append(TextRun(" ", link=ln if (i > 0 and glinks[i - 1] is ln) else None))
        tokens.append(_text_run(g, line, ln))
        i += 1
    return _merge_runs(tokens)


def _is_marker_context(gs: list[Glyph], i: int) -> bool:
    prev = gs[i - 1]
    return prev.level == 0 and (prev.c.isalnum() or prev.c in ".,;:)”’\"")


def _text_run(g: Glyph, line: Line, ln: Link | None) -> TextRun:
    info = g.info
    c = CHAR_MAP.get(g.c, g.c)
    scale = 1.0
    if g.level == 0:
        ratio = g.size / line.size
        if abs(ratio - 1) > 0.1:
            scale = round(ratio * 20) / 20
    color = g.color if g.color not in (0,) else 0
    if ln is not None:
        color = 0  # let the reader style links
    if color and min((color >> 16) & 255, (color >> 8) & 255, color & 255) > 235:
        color = 0
    return TextRun(
        c, bold=bool(info and info.bold), italic=bool(info and info.italic), mono=bool(info and info.mono),
        color=color, scale=scale, vpos=g.level, link=ln,
    )


# ---------------------------------------------------------------------------
# math


def _math_tokens(run: list[Glyph], line: Line, ctx: InlineContext, links: list) -> list[Token]:
    link = Counter(id(x) for x in links if x is not None)
    best_link = None
    if link:
        lid = link.most_common(1)[0][0]
        best_link = next(x for x in links if x is not None and id(x) == lid)
    graphics = _graphics_in(run, line, ctx)
    if _needs_image(run, line, ctx, graphics):
        box = G.union([g.box for g in run] + [gr.box for gr in graphics])
        return [MathImage(run, line.page, box, line.baseline, line.size, math_alt(run, line), graphics, best_link)]
    out: list[Token] = []
    prev = None
    for g, ln in zip(run, links):
        if prev is not None and needs_space(prev, g, line.size):
            out.append(TextRun(" ", math=True, vpos=0 if g.level == 0 or prev.level != g.level else g.level))
        out.append(_math_glyph_run(g, ln if ln is not None else best_link))
        prev = g
    return out


def _math_glyph_run(g: Glyph, ln: Link | None) -> TextRun:
    font = g.font
    c = g.c
    if font.upper().startswith(("MSBM", "BBOLD", "BBM", "DSROM")):
        c = MSBM_MAP.get(c, c)
    c = CHAR_MAP.get(c, c)
    italic = bool(_MATH_ITALIC.match(font)) and (c.isalpha())
    if g.info and g.info.italic and not g.info.math_only:
        italic = True
    bold = bool(g.info and g.info.bold)
    mono = bool(g.info and g.info.mono)
    return TextRun(c, bold=bold, italic=italic, mono=mono, vpos=g.level, link=ln, math=True)


def _graphics_in(run: list[Glyph], line: Line, ctx: InlineContext) -> list[Graphic]:
    box = G.union(g.box for g in run)
    out = []
    for gr in ctx.graphics_by_page.get(line.page, []):
        if not gr.visible or gr.role in ("furniture",):
            continue
        if gr.kind != "path" or G.width(gr.box) > G.width(box) + 2 * line.size:
            continue
        if G.overlaps(gr.box, G.expand(box, 0.5, 0.15 * line.size)):
            out.append(gr)
    return out


def _needs_image(run: list[Glyph], line: Line, ctx: InlineContext, graphics: list[Graphic]) -> bool:
    if graphics:
        return True
    size = line.size
    has_script = False
    for g in run:
        f = g.font
        if _EXTENSION.match(f):
            return True
        c = g.c
        if f.upper().startswith(("MSBM", "BBOLD", "BBM", "DSROM")):
            if c not in MSBM_MAP:
                return True
            continue
        if _CALLIGRAPHIC.match(f) and c.isalpha():
            return True  # calligraphic / script / fraktur letters
        if f.upper().startswith(("EUFM", "EUFB", "EURM", "EURB")) and c.isalpha() and c.isascii():
            return True
        if CHAR_MAP.get(c, c) not in SAFE_MATH and not (len(CHAR_MAP.get(c, c)) > 1):
            return True
        if g.level != 0:
            has_script = True
            if g.size < 0.6 * size:
                return True
    # stacked glyphs (accents, sub+sup pairs, limits)
    for a_i in range(len(run)):
        a = run[a_i]
        for b in run[a_i + 1 : a_i + 6]:
            ov = min(a.x1, b.x1) - max(a.x0, b.x0)
            narrow = min(a.x1 - a.x0, b.x1 - b.x0)
            if narrow > 0 and ov > 0.35 * narrow:
                return True
    if ctx.math_mode == "image":
        plain = all(g.level == 0 for g in run) and all(CHAR_MAP.get(g.c, g.c) in SAFE_MATH and g.c.isascii() for g in run)
        if not plain or len(run) > 3:
            return True
    # nested script levels (script of a script)
    if has_script:
        levels = {g.level for g in run}
        if 1 in levels and -1 in levels:
            # sub and sup next to each other on the same base -> stacked visually
            for a, b in zip(run, run[1:]):
                if a.level * b.level == -1 and b.x0 - a.x1 < 0.15 * size:
                    return True
    return False


def math_alt(run: list[Glyph], line: Line) -> str:
    """A TeX-like linear transcription used as alt text."""
    def ch(g: Glyph) -> str:
        c = g.c
        if g.font.upper().startswith(("MSBM", "BBOLD")):
            c = MSBM_MAP.get(c, c)
        c = CHAR_MAP.get(c, c)
        return c if c.isprintable() else ""

    out = []
    sup: list[str] = []
    sub: list[str] = []

    def flush() -> None:
        if sub:
            out.append("_{" + "".join(sub) + "}")
        if sup:
            out.append("^{" + "".join(sup) + "}")
        sub.clear()
        sup.clear()

    for g in sorted(run, key=lambda g: (g.x0, -g.level)):
        if g.level == 0:
            flush()
            out.append(ch(g))
        elif g.level > 0:
            sup.append(ch(g))
        else:
            sub.append(ch(g))
    flush()
    s = "".join(out)
    return re.sub(r"\s+", " ", s).strip()[:300]
