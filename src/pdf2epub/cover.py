"""Covers made from a book's key figure.

A paper's first page is a poor cover on a phone shelf: a thumbnail of tiny text.
The figure that best conveys the work (an overview diagram, a teaser, the main
result) makes a recognisable one. Picking that figure is a judgement call, so it
is a collaboration:

* :func:`candidates` lists every figure of a converted EPUB (PDF or web) with
  its label, caption, page and image, and :func:`contact_sheet` draws them as a
  numbered grid that a person or an LLM agent can look at (``pdf2epub figures``);
* the choice, a figure number or label, is passed to :func:`set_cover`
  (``pdf2epub cover BOOK.epub --figure N`` or ``--cover-figure N`` when
  converting), which composes a portrait cover: title, authors and source above
  the figure;
* without a choice, :func:`auto_pick` uses a heuristic: early, overview-like
  figures with a cover-friendly shape.

Covers are always ``OEBPS/images/cover.jpg``, so swapping one never touches the
rest of the book.
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

COVER_NAME = "images/cover.jpg"
COVER_SIZE = (1600, 2560)  # px, Kindle's recommended 1:1.6 portrait

_OPF = "{http://www.idpf.org/2007/opf}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_KEY_WORDS = re.compile(
    r"\b(overview|illustrat\w*|framework|pipeline|architecture|approach|schematic|diagram|summary|teaser|"
    r"we (propose|present|introduce)|our (method|approach|model|framework)|workflow|concept|key idea)\b", re.I)


@dataclass
class FigureCandidate:
    number: int  # 1-based position among the book's figures
    label: str  # "Figure 3" (from alt text / caption), "" if none
    caption: str
    page: int | None  # source page (PDF) when known
    image: bytes  # PNG/JPEG of the whole figure (panels re-joined)
    width: int
    height: int


def _spine(z: zipfile.ZipFile) -> list[str]:
    opf_path = next(n for n in z.namelist() if n.endswith(".opf"))
    root = ET.fromstring(z.read(opf_path))
    base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
    items = {it.get("id"): it.get("href") for it in root.iter(f"{_OPF}item")}
    return [base + items[r.get("idref")] for r in root.iter(f"{_OPF}itemref") if r.get("idref") in items]


def _join_panels(images: list[bytes]) -> tuple[bytes, int, int]:
    """Panels split for narrow screens go back side by side, as on the page."""
    from PIL import Image

    ims = [Image.open(io.BytesIO(b)).convert("RGB") for b in images]
    if len(ims) == 1:
        return images[0], ims[0].width, ims[0].height
    h = max(i.height for i in ims)
    ims = [i.resize((max(1, round(i.width * h / i.height)), h)) for i in ims]
    gap = max(8, h // 40)
    out = Image.new("RGB", (sum(i.width for i in ims) + gap * (len(ims) - 1), h), "white")
    x = 0
    for i in ims:
        out.paste(i, (x, 0))
        x += i.width + gap
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue(), out.width, out.height


def candidates(epub_path: str | Path) -> list[FigureCandidate]:
    """Every ``div.figure`` of the book, in reading order (tables and equations excluded)."""
    from PIL import Image

    out: list[FigureCandidate] = []
    with zipfile.ZipFile(epub_path) as z:
        names = set(z.namelist())
        for chapter in _spine(z):
            text = z.read(chapter).decode("utf-8")
            folder = chapter.rsplit("/", 1)[0]
            for m in re.finditer(r'<div class="figure"([^>]*)>(.*?)</div>\s*(?:<p class="caption"[^>]*>(.*?)</p>)?\s*</div>', text, re.S):
                attrs, body, caption = m.group(1), m.group(2), m.group(3) or ""
                srcs = re.findall(r'<img class="fig" src="([^"]+)"', body)
                blobs = []
                for src in srcs:
                    path = re.sub(r"[^/]+/\.\./", "", f"{folder}/{src}")
                    if path in names:
                        blobs.append(z.read(path))
                if not blobs:
                    continue
                image, w, h = _join_panels(blobs) if len(blobs) > 1 else (blobs[0], *Image.open(io.BytesIO(blobs[0])).size)
                alt = re.search(r'alt="([^"]*)"', body)
                cap = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", caption))).strip()
                label = re.match(r"((Figure|Fig\.|Table)\s*[A-Z]?\d+[a-z]?)", cap or (html.unescape(alt.group(1)) if alt else ""))
                page = re.search(r'data-page="(\d+)"', attrs)
                out.append(FigureCandidate(len(out) + 1, label.group(1) if label else "", cap,
                                           int(page.group(1)) if page else None, image, w, h))
    return out


def auto_pick(cands: list[FigureCandidate]) -> FigureCandidate | None:
    """Heuristic choice when nobody picked: early, overview-like, cover-shaped, not tiny."""
    if not cands:
        return None

    def score(c: FigureCandidate) -> float:
        s = 2.0 * (1 - (c.number - 1) / max(1, len(cands)))  # earlier is better
        if re.match(r"(Figure|Fig\.)\s*1\b", c.label):
            s += 2.0
        elif c.number == 1 and not c.label:
            s += 3.0  # an uncaptioned lead figure above the text: the teaser
        if _KEY_WORDS.search(c.caption[:300]):
            s += 2.0
        aspect = c.width / max(1, c.height)  # wide multi-panel overviews are fine; strips are not
        s += 1.0 if 0.5 <= aspect <= 2.5 else (-1.5 if aspect > 4.5 else 0.0)
        if min(c.width, c.height) < 300:
            s -= 2.0
        if c.page is not None and c.page <= 3:
            s += 0.5
        return s

    return max(cands, key=score)


def resolve(cands: list[FigureCandidate], choice: str | int | None) -> FigureCandidate | None:
    """``choice``: a 1-based number, a label ("Figure 3"), "auto", or "page"/None for no figure."""
    if choice is None or str(choice).lower() in ("page", "none", "off"):
        return None
    if str(choice).lower() == "auto":
        return auto_pick(cands)
    if str(choice).isdigit():
        n = int(choice)
        if not 1 <= n <= len(cands):
            raise ValueError(f"figure {n} does not exist (the book has {len(cands)} figures)")
        return cands[n - 1]
    want = re.sub(r"\s+", " ", str(choice)).strip().lower().replace("fig.", "figure")
    for c in cands:
        if c.label.lower().replace("fig.", "figure") == want:
            return c
    raise ValueError(f"no figure labelled {choice!r}; labels: {', '.join(c.label for c in cands if c.label) or 'none'}")


def compose_cover(title: str, authors: list[str], figure: bytes, source: str = "") -> bytes:
    """A portrait JPEG cover: title, authors and source above the figure, centred as one block."""
    import pymupdf
    from PIL import Image

    w_pt, h_pt = 600.0, 960.0
    margin, gap = 42.0, 26.0
    names = ", ".join(authors[:6]) + (" et al." if len(authors) > 6 else "")
    head = (f'<div class="t">{html.escape(title)}</div>'
            + (f'<div class="a">{html.escape(names)}</div>' if names else "")
            + (f'<div class="s">{html.escape(source)}</div>' if source else ""))
    css = ("* { font-family: serif; text-align: center; margin: 0; }"
           " .t { font-size: 34px; font-weight: bold; line-height: 1.18; color: #111; }"
           " .a { font-size: 17px; margin-top: 16px; color: #333; }"
           " .s { font-size: 12px; margin-top: 10px; color: #777; letter-spacing: 1px; }")
    box_w = w_pt - 2 * margin
    # measure the title block on a scratch page
    scratch = pymupdf.open()
    probe = pymupdf.Rect(0, 0, box_w, 330)
    spare, _ = scratch.new_page(width=w_pt, height=h_pt).insert_htmlbox(probe, head, css=css, scale_low=0.4)
    text_h = probe.height - max(0.0, spare) if spare >= 0 else probe.height
    # the figure at full width, shrunk if it would not fit below the text
    fw, fh = Image.open(io.BytesIO(figure)).size
    room = h_pt - 2 * margin - text_h - 2 * gap
    fig_w = box_w
    fig_h = fig_w * fh / max(1, fw)
    if fig_h > room:
        fig_h = room
        fig_w = fig_h * fw / max(1, fh)
    total = text_h + 2 * gap + fig_h
    top = max(margin + 12, (h_pt - total) / 2 - 20)  # a touch above centre reads better
    doc = pymupdf.open()
    page = doc.new_page(width=w_pt, height=h_pt)
    page.insert_htmlbox(pymupdf.Rect(margin, top, w_pt - margin, top + text_h + 4), head, css=css, scale_low=0.4)
    rule_y = top + text_h + gap
    page.draw_line((w_pt * 0.35, rule_y), (w_pt * 0.65, rule_y), color=(0.6, 0.6, 0.6), width=0.8)
    fx = (w_pt - fig_w) / 2
    fy = rule_y + gap
    page.insert_image(pymupdf.Rect(fx, fy, fx + fig_w, fy + fig_h), stream=figure, keep_proportion=True)
    zoom = COVER_SIZE[0] / w_pt
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


def _metadata(z: zipfile.ZipFile) -> tuple[str, str, list[str], str]:
    opf_path = next(n for n in z.namelist() if n.endswith(".opf"))
    root = ET.fromstring(z.read(opf_path))
    title = (root.findtext(f".//{_DC}title") or "").strip()
    authors = [e.text.strip() for e in root.iter(f"{_DC}creator") if e.text and e.text.strip() and e.text.strip() != "Unknown"]
    desc = (root.findtext(f".//{_DC}description") or "").strip()
    return opf_path, title, authors, desc


def source_line(description: str, title_hint: str = "") -> str:
    """'arXiv 2406.11717v3' or the web site's domain, for the cover's small print."""
    from .naming import arxiv_id

    aid = arxiv_id(description) or arxiv_id(title_hint)
    if aid:
        return f"arXiv {aid}"
    m = re.search(r"https?://([^/\s]+)", description)
    return m.group(1).removeprefix("www.") if m else ""


def set_cover(epub_path: str | Path, choice: str | int | None = "auto", out_path: str | Path | None = None,
              source: str | None = None) -> FigureCandidate | None:
    """Rewrite the book's cover from the chosen figure; returns the figure used (None: cover left as is)."""
    epub_path = Path(epub_path)
    cands = candidates(epub_path)
    fig = resolve(cands, choice)
    if fig is None:
        if out_path and Path(out_path) != epub_path:
            Path(out_path).write_bytes(epub_path.read_bytes())
        return None
    with zipfile.ZipFile(epub_path) as z:
        opf_path, title, authors, desc = _metadata(z)
        cover = compose_cover(title, authors, fig.image, source if source is not None else source_line(desc, epub_path.name))
        entries = [(info, z.read(info.filename)) for info in z.infolist()]
    base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
    target = base + COVER_NAME
    opf = next(data for info, data in entries if info.filename == opf_path).decode("utf-8")
    if 'properties="cover-image"' not in opf:  # a book written without a cover gets one
        opf = opf.replace("</manifest>", f'  <item id="cover-image" href="{COVER_NAME}" media-type="image/jpeg" properties="cover-image"/>\n  </manifest>', 1)
        opf = opf.replace("</metadata>", '  <meta name="cover" content="cover-image"/>\n  </metadata>', 1)
    else:  # any earlier cover (e.g. cover.png) is replaced by cover.jpg
        opf = re.sub(r'<item id="cover-image" href="[^"]*" media-type="[^"]*"', f'<item id="cover-image" href="{COVER_NAME}" media-type="image/jpeg"', opf)
    out = Path(out_path) if out_path else epub_path
    tmp = out.with_suffix(out.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w") as zo:
        for info, data in entries:
            name = info.filename
            if name == opf_path:
                data = opf.encode("utf-8")
            elif re.search(r"/images/cover\.(jpe?g|png)$", "/" + name):
                continue  # the old cover image
            compress = zipfile.ZIP_STORED if name == "mimetype" or name.startswith(base + "images/") else zipfile.ZIP_DEFLATED
            zo.writestr(zipfile.ZipInfo(name) if name == "mimetype" else name, data, compress_type=compress)
        zo.writestr(target, cover, compress_type=zipfile.ZIP_STORED)
    tmp.replace(out)
    return fig


def contact_sheet(cands: list[FigureCandidate], out_png: str | Path, cell: int = 520) -> Path:
    """A numbered grid of the candidates, for picking the cover figure by eye."""
    from PIL import Image, ImageDraw

    cols = 3 if len(cands) > 4 else max(1, len(cands))
    rows = max(1, -(-len(cands) // cols))
    label_h = 44
    sheet = Image.new("RGB", (cols * cell, rows * (cell + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    for k, c in enumerate(cands):
        im = Image.open(io.BytesIO(c.image)).convert("RGB")
        im.thumbnail((cell - 20, cell - 20))
        x, y = (k % cols) * cell, (k // cols) * (cell + label_h)
        sheet.paste(im, (x + (cell - im.width) // 2, y + label_h + (cell - im.height) // 2))
        draw.rectangle([x + 2, y + 2, x + cell - 3, y + cell + label_h - 3], outline=(200, 200, 200))
        tag = f"#{c.number}  {c.label or 'figure'}" + (f"  (p{c.page})" if c.page else "")
        draw.text((x + 10, y + 8), tag, fill=(180, 0, 0))
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_png)
    return Path(out_png)
