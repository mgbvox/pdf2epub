import re
import subprocess
import uuid
import zipfile
from html import escape
from pathlib import Path

NUM_RE = re.compile(r"^[1-9](\.\d+)*$")
TITLE_RE = re.compile(r"^[A-Z][A-Za-z0-9 ,&'-]*$")
APPENDIX_RE = re.compile(r"^[A-Z](\.\d+)*$")
NOISE_RE = re.compile(r"^(arXiv:\S+.*|†|∗|\*)$")


def pdf_metadata(pdf: Path) -> dict:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True).stdout
    meta = {}
    for line in out.splitlines():
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    return meta


def _title_at(lines, j):
    while j < len(lines) and not lines[j]:
        j += 1
    return j < len(lines) and len(lines[j]) < 80 and lines[j][:1].isupper() \
        and bool(TITLE_RE.match(lines[j])) \
        and not re.search(r"\b(Layer|Score|[A-Z] [A-Z]{2,})\b|\d$", lines[j])


def extract_blocks(pdf: Path) -> list[tuple[str, str]]:
    """Return [(kind, text)] where kind is 'h1' | 'h2' | 'p'."""
    text = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    lines = [l.strip() for l in text.replace("\f", "\n").splitlines()]
    lines = [l for l in lines if not NOISE_RE.match(l)]
    widths = sorted(len(l) for l in lines if l)
    full = widths[int(len(widths) * 0.9)] if widths else 60

    blocks: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush():
        if buf:
            blocks.append(("p", " ".join(buf)))
            buf.clear()

    def add_line(l: str):
        if buf and buf[-1].endswith("-") and l[:1].islower():
            buf[-1] = buf[-1][:-1] + l  # dehyphenate; "-27B" style breaks stay readable
        else:
            buf.append(l)

    i = 0
    while i < len(lines):
        l = lines[i]
        if not l:
            flush()
        elif l in ("Abstract", "References", "Limitations", "Acknowledgments", "Appendix"):
            flush()
            blocks.append(("h1", l))
        elif (NUM_RE.match(l) or APPENDIX_RE.match(l)) and _title_at(lines, i + 1):
            flush()
            j = i + 1
            while not lines[j]:
                j += 1
            blocks.append(("h2" if "." in l else "h1", f"{l} {lines[j]}"))
            i = j
        else:
            add_line(l)
            # short line ending a sentence => paragraph end
            if len(l) < full * 0.7 and l.endswith((".", ":", "?")):
                flush()
        i += 1
    flush()
    return blocks


def pdf_to_epub(pdf, out, title=None, author=None) -> Path:
    pdf, out = Path(pdf), Path(out)
    meta = pdf_metadata(pdf)
    title = title or meta.get("Title") or pdf.stem
    author = author or meta.get("Author") or "Unknown"
    blocks = extract_blocks(pdf)

    # Split into chapters at h1
    chapters: list[tuple[str, list[tuple[str, str]]]] = [("Front Matter", [])]
    for kind, t in blocks:
        if kind == "h1":
            chapters.append((t, []))
        chapters[-1][1].append((kind, t))
    chapters = [c for c in chapters if c[1]]
    # Front matter: title + authors
    chapters[0] = (title, [("h1", title)] + [b for b in chapters[0][1] if b[0] != "h1"])

    def xhtml(name, body):
        return (f'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="utf-8"/>'
                f'<title>{escape(name)}</title></head><body>{body}</body></html>')

    files = []
    for n, (name, items) in enumerate(chapters):
        body = "".join(f"<{k}>{escape(t)}</{k}>\n" if k != "p" else f"<p>{escape(t)}</p>\n"
                       for k, t in items)
        files.append((f"ch{n:02d}.xhtml", name, xhtml(name, body)))

    uid = f"urn:uuid:{uuid.uuid4()}"
    manifest = "".join(f'<item id="c{n}" href="{f}" media-type="application/xhtml+xml"/>'
                       for n, (f, _, _) in enumerate(files))
    spine = "".join(f'<itemref idref="c{n}"/>' for n in range(len(files)))
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">{uid}</dc:identifier><dc:title>{escape(title)}</dc:title>
<dc:creator>{escape(author)}</dc:creator><dc:language>en</dc:language>
<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>
<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>{manifest}</manifest>
<spine>{spine}</spine></package>'''
    nav_items = "".join(f'<li><a href="{f}">{escape(n)}</a></li>' for f, n, _ in files)
    nav = (f'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
           f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head>'
           f'<body><nav epub:type="toc"><ol>{nav_items}</ol></nav></body></html>')
    container = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')

    with zipfile.ZipFile(out, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        for f, _, content in files:
            z.writestr(f"OEBPS/{f}", content, compress_type=zipfile.ZIP_DEFLATED)
    return out
