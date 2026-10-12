"""Objective quality metrics for a conversion, and the regression gate built on them.

Two kinds of signal, both computed from the source PDF and the produced EPUB:

* **Fidelity metrics** (higher is better), comparing the words of the PDF with
  the words a reader gets from the EPUB (reflowed text plus image alt text):
  ``word_recall`` / ``word_precision`` catch dropped or duplicated text,
  ``bigram_recall`` / ``bigram_precision`` catch broken local word order, and
  ``order_score`` catches blocks read in the wrong order (columns swapped,
  floats dropped into the middle of a sentence).
* **Defect lint** (lower is better): patterns that almost always mean a
  conversion bug, reported with chapter, source page and a snippet so they can
  be traced back with ``pdf2epub inspect --pages N``.

``compare`` turns a baseline report and a new report into a list of
regressions; the corpus gate (``tests/test_corpus.py``, ``tools/corpus_gate.py``)
fails when that list is not empty.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

XHTML = "{http://www.w3.org/1999/xhtml}"
EPUB_NS = "{http://www.idpf.org/2007/ops}"
OPF = "{http://www.idpf.org/2007/opf}"

# metrics where a decrease is a regression (tolerance absorbs float noise only)
HIGHER_IS_BETTER = ("word_recall", "word_precision", "bigram_recall", "bigram_precision", "order_score")
TOLERANCE = 0.002

_WORD = re.compile(r"[A-Za-z0-9]+")
_SENTENCE_END = re.compile(r"[.?!:;)\]\"”’]\s*$")
_CAPTION = re.compile(r"^\s*(Figure|Fig\.|Table|Algorithm)\s*([A-Z]?\d+)", re.I)


@dataclass
class Issue:
    kind: str
    chapter: str
    page: int | None
    block: str
    snippet: str


@dataclass
class QualityReport:
    metrics: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def issue_counts(self) -> dict:
        return dict(sorted(Counter(i.kind for i in self.issues).items()))

    def to_json(self) -> dict:
        return {"metrics": self.metrics, "counts": self.counts, "issue_counts": self.issue_counts}

    def summary(self) -> str:
        m = ", ".join(f"{k} {v:.4f}" for k, v in self.metrics.items())
        c = ", ".join(f"{k} {v}" for k, v in self.counts.items())
        ic = ", ".join(f"{k} {v}" for k, v in self.issue_counts.items()) or "none"
        return f"metrics: {m}\ncounts: {c}\nissues: {ic}"


# ---------------------------------------------------------------------------
# reading the EPUB


@dataclass
class _Block:
    chapter: str
    tag: str
    cls: str
    id: str
    page: int | None
    text: str
    html_has_br: bool
    alts: list[str]
    imgs: list[dict]


def _spine(z: zipfile.ZipFile) -> list[str]:
    opf_path = next(n for n in z.namelist() if n.endswith(".opf"))
    root = ET.fromstring(z.read(opf_path))
    base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
    items = {it.get("id"): it.get("href") for it in root.iter(f"{OPF}item")}
    return [base + items[ref.get("idref")] for ref in root.iter(f"{OPF}itemref") if ref.get("idref") in items]


def _text(el: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _blocks(z: zipfile.ZipFile) -> list[_Block]:
    out: list[_Block] = []
    for name in _spine(z):
        root = ET.fromstring(z.read(name))
        body = root.find(f"{XHTML}body")
        if body is None:
            continue
        chapter = name.rsplit("/", 1)[-1]

        def visit(el: ET.Element, page: int | None, chapter: str = chapter) -> None:
            tag = el.tag.replace(XHTML, "")
            pg = el.get("data-page")
            page = int(pg) if pg and pg.isdigit() else page
            if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6") or (tag == "div" and "eq" in (el.get("class") or "").split()):
                imgs = []
                for img in el.iter(f"{XHTML}img"):
                    imgs.append({"src": img.get("src", ""), "cls": img.get("class", ""), "style": img.get("style", ""), "alt": img.get("alt", "")})
                out.append(_Block(
                    chapter, tag, el.get("class") or "", el.get("id") or "", page, _text(el),
                    el.find(f".//{XHTML}br") is not None, [i["alt"] for i in imgs], imgs,
                ))
                return
            if tag == "div" and "figure" in (el.get("class") or "").split():
                for img in el.iter(f"{XHTML}img"):
                    out.append(_Block(chapter, "img", img.get("class", ""), el.get("id") or "", page, "", False,
                                      [img.get("alt", "")], [{"src": img.get("src", ""), "cls": img.get("class", ""), "style": img.get("style", ""), "alt": img.get("alt", "")}]))
            for child in el:
                visit(child, page, chapter)

        visit(body, None)
    return out


def _em(style: str, prop: str) -> float | None:
    m = re.search(rf"{prop}:\s*([\d.]+)em", style or "")
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# lint


def lint(epub_path: str | Path) -> list[Issue]:
    issues: list[Issue] = []
    with zipfile.ZipFile(epub_path) as z:
        blocks = _blocks(z)
        names = set(z.namelist())
        image_data = {n: z.read(n) for n in names if n.startswith("OEBPS/images/") and not n.endswith("cover.jpg")}

    def add(kind: str, b: _Block, snippet: str | None = None) -> None:
        issues.append(Issue(kind, b.chapter, b.page, b.id, (snippet if snippet is not None else b.text)[:120]))

    body_kinds = {"", "cont", "small", "inset", "abstract", "inlist", "cont small", "small cont"}
    seen_text: dict[str, _Block] = {}
    captions: dict[str, list[int]] = {}
    prev: _Block | None = None
    for k, b in enumerate(blocks):
        nxt = blocks[k + 1] if k + 1 < len(blocks) else None
        plain_p = b.tag == "p" and b.cls in body_kinds
        if plain_p:
            if not b.text and not b.imgs:
                add("empty-paragraph", b, "")
            words = b.text.split()
            lead_in = nxt is not None and nxt.tag == "div" and nxt.chapter == b.chapter  # text leading into a display equation
            if b.text and len(words) < 4 and len(b.text) < 25 and not b.text.endswith((":", ",")) and not lead_in:
                add("stray-fragment", b)
            if b.html_has_br:
                add("hard-breaks", b)
            if re.search(r"\b[a-z]{2,}- [a-z]{2,}\b", b.text):
                add("hyphen-leftover", b, re.search(r"\S*\b[a-z]{2,}- [a-z]{2,}\S*", b.text).group(0))
            if (
                prev is not None and prev.tag == "p" and prev.cls in body_kinds and prev.chapter == b.chapter
                and prev.text and b.text and not _SENTENCE_END.search(prev.text) and b.text[0].islower()
            ):
                add("split-paragraph", b, f"…{prev.text[-40:]} ¶ {b.text[:40]}…")
            if len(b.text) > 60:
                key = b.text
                if key in seen_text:
                    add("duplicate-paragraph", b)
                seen_text.setdefault(key, b)
            for w in words:
                if len(w) > 45 and "/" not in w and "@" not in w:
                    add("long-word", b, w)
        if b.cls == "caption" or (b.tag == "p" and "caption" in b.cls):
            m = _CAPTION.match(b.text)
            if m:
                kind = m.group(1).lower().rstrip(".")
                kind = "figure" if kind.startswith("fig") else kind
                num = m.group(2)
                if num.isdigit():
                    captions.setdefault(kind, []).append(int(num))
        for img in b.imgs:
            if "m" in img["cls"].split():
                h, w = _em(img["style"], "height"), _em(img["style"], "width")
                if h is not None and h > 3.2:
                    add("tall-inline-math", b, f"{img['alt'][:60]} (height {h}em)")
                if w is not None and w > 18:
                    add("wide-inline-math", b, f"{img['alt'][:60]} (width {w}em)")
            src = "OEBPS/" + img["src"].replace("../", "")
            data = image_data.get(src)
            if data is not None and _blank(data):
                add("blank-image", b, img["src"])
        if b.tag == "p" or b.tag.startswith("h"):
            prev = b
        elif b.tag == "div":
            prev = None  # a sentence may run on after a display equation
    for kind, nums in captions.items():
        nums = sorted(set(nums))
        if nums:
            missing = sorted(set(range(1, nums[-1] + 1)) - set(nums))
            # appendix numbering restarts or uses letters; only flag short gaps in the main sequence
            for m in missing[:20]:
                issues.append(Issue("caption-gap", "", None, "", f"{kind} {m} has no caption"))
    return issues


def _blank(data: bytes) -> bool:
    try:
        from PIL import Image
        import numpy as np

        img = Image.open(io.BytesIO(data)).convert("L")
        arr = np.asarray(img)
        return arr.size == 0 or float((arr < 200).mean()) < 0.0005
    except Exception:
        return False


# ---------------------------------------------------------------------------
# fidelity metrics


def _norm_words(words: list[str]) -> list[str]:
    out: list[str] = []
    for w in words:
        for t in _WORD.findall(w.lower()):
            if len(t) >= 2:
                out.append(t)
    return out


def pdf_words(pdf_path: str | Path, pages: list[int] | None = None) -> list[str]:
    """Words of the PDF in content-stream order, with end-of-line hyphenation undone."""
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    raw: list[str] = []
    for i, page in enumerate(doc):
        if pages is not None and i not in pages:
            continue
        ws = [w[4] for w in page.get_text("words", sort=False)]
        j = 0
        while j < len(ws):
            w = ws[j]
            if w.endswith("-") and len(w) > 2 and w[-2].isalpha() and j + 1 < len(ws) and ws[j + 1][:1].islower():
                raw.append(w[:-1] + ws[j + 1])
                j += 2
                continue
            raw.append(w)
            j += 1
    return _norm_words(raw)


_INLINE = {"a", "span", "i", "b", "em", "strong", "sub", "sup", "small", "code", "var", "abbr", "cite", "u", "s"}


def epub_words(epub_path: str | Path) -> list[str]:
    """Words of the EPUB in spine order: running text plus image alt text.

    Inline markup does not split words (small caps are written as
    ``M<span>ASK</span>``); block elements and images do.
    """
    with zipfile.ZipFile(epub_path) as z:
        parts: list[str] = []

        def walk(el: ET.Element) -> None:
            tag = el.tag.replace(XHTML, "")
            inline = tag in _INLINE
            if not inline:
                parts.append(" ")
            if tag == "img":
                parts.append(" " + el.get("alt", "") + " ")
            if el.text:
                parts.append(el.text)
            for child in el:
                walk(child)
                if child.tail:
                    parts.append(child.tail)
            if not inline:
                parts.append(" ")

        for name in _spine(z):
            root = ET.fromstring(z.read(name))
            body = root.find(f"{XHTML}body")
            if body is not None:
                walk(body)
    return _norm_words("".join(parts).split())


def _overlap(a: Counter, b: Counter) -> int:
    return sum(min(v, b[k]) for k, v in a.items())


def _order_score(src: list[str], dst: list[str], chunk: int = 50, probe: int = 6) -> float:
    """Share of consecutive source chunks found in increasing order in the output."""
    if len(src) < 2 * chunk or not dst:
        return 1.0
    index: dict[tuple, list[int]] = {}
    for i in range(len(dst) - probe + 1):
        index.setdefault(tuple(dst[i : i + probe]), []).append(i)
    positions = []
    for start in range(0, len(src) - probe, chunk):
        pos = None
        for off in range(0, min(chunk - probe, 30)):
            key = tuple(src[start + off : start + off + probe])
            hits = index.get(key)
            if hits and len(hits) == 1:  # unique anchor
                pos = hits[0]
                break
        positions.append(pos)
    pairs = [(a, b) for a, b in zip(positions, positions[1:]) if a is not None and b is not None]
    if not pairs:
        return 0.0
    return sum(1 for a, b in pairs if b > a) / len(pairs)


def fidelity(pdf_path: str | Path, epub_path: str | Path, pages: list[int] | None = None) -> dict:
    return _fidelity_words(pdf_words(pdf_path, pages), epub_words(epub_path))


def text_fidelity(reference_text: str, epub_path: str | Path) -> dict:
    """The same metrics against a reference text (a web page's visible article text)."""
    return _fidelity_words(_norm_words(reference_text.split()), epub_words(epub_path))


def _fidelity_words(src: list[str], dst: list[str]) -> dict:
    cs, cd = Counter(src), Counter(dst)
    bs, bd = Counter(zip(src, src[1:])), Counter(zip(dst, dst[1:]))
    common, bcommon = _overlap(cs, cd), _overlap(bs, bd)
    return {
        "word_recall": round(common / max(1, sum(cs.values())), 4),
        "word_precision": round(common / max(1, sum(cd.values())), 4),
        "bigram_recall": round(bcommon / max(1, sum(bs.values())), 4),
        "bigram_precision": round(bcommon / max(1, sum(bd.values())), 4),
        "order_score": round(_order_score(src, dst), 4),
    }


# ---------------------------------------------------------------------------


def structure_counts(epub_path: str | Path) -> dict:
    with zipfile.ZipFile(epub_path) as z:
        blocks = _blocks(z)
        html = "".join(z.read(n).decode("utf-8", "replace") for n in _spine(z))
        n_images = sum(1 for n in z.namelist() if n.startswith("OEBPS/images/") and not n.endswith("cover.jpg"))
    return {
        "paragraphs": sum(1 for b in blocks if b.tag == "p"),
        "headings": sum(1 for b in blocks if b.tag in ("h2", "h3", "h4", "h5", "h6")),
        "figures": html.count('<div class="figure"'),
        # table images (PDF pipeline) and real tables (web pipeline)
        "tables": html.count('<div class="figure table"') + html.count('<div class="table"'),
        "equations": html.count('<div class="eq"'),
        "text_boxes": html.count('<div class="boxwrap"'),
        "algorithms": html.count('<div class="boxwrap algo"'),
        "footnotes": html.count('epub:type="footnote"'),
        "inline_math_images": html.count('class="m"'),
        "images": n_images,
    }


def evaluate(pdf_path: str | Path, epub_path: str | Path, pages: list[int] | None = None) -> QualityReport:
    return QualityReport(
        metrics=fidelity(pdf_path, epub_path, pages),
        counts=structure_counts(epub_path),
        issues=lint(epub_path),
    )


def evaluate_web(reference_text: str, epub_path: str | Path) -> QualityReport:
    """Score a web-article EPUB against the pages' visible text (see :mod:`pdf2epub.web`)."""
    return QualityReport(
        metrics=text_fidelity(reference_text, epub_path),
        counts=structure_counts(epub_path),
        issues=lint(epub_path),
    )


def compare(baseline: dict, current: dict) -> list[str]:
    """Regressions of ``current`` against ``baseline`` (both from ``QualityReport.to_json``)."""
    problems = []
    for k in HIGHER_IS_BETTER:
        b, c = baseline.get("metrics", {}).get(k), current.get("metrics", {}).get(k)
        if b is not None and c is not None and c < b - TOLERANCE:
            problems.append(f"{k} dropped {b:.4f} -> {c:.4f}")
    b_issues = baseline.get("issue_counts", {})
    c_issues = current.get("issue_counts", {})
    if sum(c_issues.values()) > sum(b_issues.values()):
        problems.append(f"lint issues rose {sum(b_issues.values())} -> {sum(c_issues.values())}: {c_issues}")
    for k, v in c_issues.items():
        if v > b_issues.get(k, 0) and k in ("blank-image", "duplicate-paragraph", "hyphen-leftover"):
            problems.append(f"{k} rose {b_issues.get(k, 0)} -> {v}")
    return problems


def improvements(baseline: dict, current: dict) -> list[str]:
    out = []
    for k in HIGHER_IS_BETTER:
        b, c = baseline.get("metrics", {}).get(k), current.get("metrics", {}).get(k)
        if b is not None and c is not None and c > b + TOLERANCE:
            out.append(f"{k} {b:.4f} -> {c:.4f}")
    bi, ci = sum(baseline.get("issue_counts", {}).values()), sum(current.get("issue_counts", {}).values())
    if ci < bi:
        out.append(f"lint issues {bi} -> {ci}")
    return out


def issues_as_dicts(report: QualityReport) -> list[dict]:
    return [asdict(i) for i in report.issues]
