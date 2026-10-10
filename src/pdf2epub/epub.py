"""Minimal, standards-compliant EPUB 3 writer (with EPUB 2 NCX for older readers)."""

from __future__ import annotations

import datetime as _dt
import html
import uuid
import zipfile
from pathlib import Path

from .htmlgen import Chapter
from .render import ImageAsset

# Kindle notes: never force font family, size, colour or alignment on body text
# (that would override the reader's settings); size images in em so formulas
# scale with the chosen font size, and cap them at the screen width.
CSS = """\
@charset "utf-8";
body { margin: 0; padding: 0 0.2em; overflow-wrap: break-word; word-wrap: break-word; }
p { margin: 0.55em 0 0 0; text-indent: 0; line-height: 1.4; orphans: 2; widows: 2; }
p.cont { margin-top: 0.25em; }
p.center { text-align: center; }
p.small { font-size: 0.9em; }
p.inset, p.abstract { margin-left: 1.2em; margin-right: 1.2em; }
p.inlist { margin-left: 1.8em; }
p.bib { margin-left: 1.6em; text-indent: -1.6em; font-size: 0.92em; margin-top: 0.4em; }
p.caption { font-size: 0.9em; margin: 0.45em 0 0.2em 0; line-height: 1.35; }
p.code { font-family: monospace; font-size: 0.82em; text-align: left; line-height: 1.3; }
p.note { font-size: 0.88em; margin-top: 0.4em; }
h1, h2, h3, h4, h5, h6 { line-height: 1.25; text-indent: 0; page-break-after: avoid; text-align: left; }
h1.title { font-size: 1.45em; text-align: center; margin: 1.2em 0 0.8em 0; }
h1 { font-size: 1.4em; margin: 1.2em 0 0.5em 0; }
h2 { font-size: 1.3em; margin: 1.3em 0 0.4em 0; }
h3 { font-size: 1.12em; margin: 1.1em 0 0.3em 0; }
h4, h5, h6 { font-size: 1.0em; margin: 1em 0 0.25em 0; }
ul { margin: 0.4em 0 0 0; padding-left: 1.4em; }
li { margin-top: 0.3em; }
p.li { margin: 0; }
sup, sub { font-size: 0.72em; line-height: 0; }
.mono { font-family: monospace; overflow-wrap: break-word; word-wrap: break-word; }
p.bib .mono, p.note .mono { word-break: break-all; }
a.noteref { text-decoration: none; }
div.figure { margin: 1.1em 0 0.9em 0; text-align: center; page-break-inside: avoid; }
div.figure div.img { margin: 0.35em 0; text-align: center; }
div.figure p.caption { text-align: left; }
img.fig { max-width: 100%; height: auto; }
div.eq { margin: 0.7em 0; text-align: center; text-indent: 0; }
img.eq { max-width: 100%; height: auto; }
img.m { vertical-align: middle; margin: 0; }
div.boxwrap { margin: 1em 0 0.9em 0; }
div.box { border: 1px solid #888888; padding: 0.25em 0.5em 0.4em 0.5em; margin: 0.3em 0 0 0; }
p.boxline { margin: 0.3em 0 0 0; text-align: left; line-height: 1.35; }
p.boxline.gap { margin-top: 0.9em; }
div.algo div.box { border-left: 0; border-right: 0; padding-left: 0.2em; padding-right: 0.2em; }
p.algline { margin: 0.12em 0 0 0; text-align: left; line-height: 1.35; padding-left: 1.2em; text-indent: -1.2em; }
div.notes { margin-top: 1.5em; }
hr.notes { width: 30%; margin: 0 auto 0 0; border: 0; border-top: 1px solid #888888; }
aside.footnote { margin: 0; }
"""

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _x(s: str) -> str:
    return html.escape(s, quote=True)


def xhtml_page(title: str, body: str, lang: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">\n'
        f"<head>\n<meta charset=\"utf-8\"/>\n<title>{_x(title)}</title>\n"
        '<link rel="stylesheet" type="text/css" href="../style.css"/>\n</head>\n'
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def write_epub(
    path: Path,
    title: str,
    authors: list[str],
    language: str,
    chapters: list[Chapter],
    assets: list[ImageAsset],
    toc: list[tuple[int, str, str]],
    cover: ImageAsset | None = None,
    identifier: str | None = None,
    description: str = "",
) -> None:
    ident = identifier or f"urn:uuid:{uuid.uuid4()}"
    modified = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    spine = []
    for i, ch in enumerate(chapters):
        manifest.append(f'<item id="c{i}" href="{ch.file}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')
    for i, a in enumerate(assets):
        manifest.append(f'<item id="img{i}" href="{a.name}" media-type="{a.media_type}"/>')
    cover_meta = ""
    if cover is not None:
        manifest.append(f'<item id="cover-image" href="{cover.name}" media-type="{cover.media_type}" properties="cover-image"/>')
        cover_meta = '<meta name="cover" content="cover-image"/>'
    creators = "\n    ".join(f'<dc:creator id="creator{i}">{_x(a)}</dc:creator>' for i, a in enumerate(authors)) or "<dc:creator>Unknown</dc:creator>"
    desc = f"<dc:description>{_x(description)}</dc:description>" if description else ""
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="{language}">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{_x(ident)}</dc:identifier>
    <dc:title>{_x(title)}</dc:title>
    {creators}
    <dc:language>{language}</dc:language>
    {desc}
    <meta property="dcterms:modified">{modified}</meta>
    {cover_meta}
  </metadata>
  <manifest>
    {chr(10).join('    ' + m for m in manifest).strip()}
  </manifest>
  <spine toc="ncx">
    {chr(10).join('    ' + s for s in spine).strip()}
  </spine>
</package>
"""
    nav = _nav(title, toc, language)
    ncx = _ncx(title, ident, toc)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", CSS, compress_type=zipfile.ZIP_DEFLATED)
        for ch in chapters:
            z.writestr(f"OEBPS/{ch.file}", xhtml_page(ch.title, "\n".join(ch.body), language), compress_type=zipfile.ZIP_DEFLATED)
        for a in assets:
            z.writestr(f"OEBPS/{a.name}", a.data, compress_type=zipfile.ZIP_STORED)
        if cover is not None:
            z.writestr(f"OEBPS/{cover.name}", cover.data, compress_type=zipfile.ZIP_STORED)


def _nested(toc: list[tuple[int, str, str]]):
    """Normalise levels so that nesting never skips a level."""
    if not toc:
        return []
    out = []
    stack: list[int] = []
    for lvl, text, href in toc:
        while stack and stack[-1] >= lvl:
            stack.pop()
        depth = len(stack)
        stack.append(lvl)
        out.append((depth, text, href))
    return out


def _nav(title: str, toc: list[tuple[int, str, str]], lang: str) -> str:
    items = _nested(toc)
    parts = []
    depth = -1
    for d, text, href in items:
        if d > depth:
            parts.append("<ol>" * (d - depth))
        else:
            parts.append("</li>")
            if d < depth:
                parts.append("</ol></li>" * (depth - d))
        parts.append(f'<li><a href="{_x(href)}">{_x(text)}</a>')
        depth = d
    if depth >= 0:
        parts.append("</li>" + "</ol></li>" * depth + "</ol>")
    body = "".join(parts) or "<ol><li><a href=\"text/ch000.xhtml\">Start</a></li></ol>"
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">\n'
        f"<head><meta charset=\"utf-8\"/><title>{_x(title)}</title></head>\n<body>\n"
        f'<nav epub:type="toc" id="toc"><h1>Contents</h1>{body}</nav>\n</body>\n</html>\n'
    )


def _ncx(title: str, ident: str, toc: list[tuple[int, str, str]]) -> str:
    items = _nested(toc)
    parts = []
    depth = -1
    n = 0
    for d, text, href in items:
        n += 1
        if d <= depth:
            parts.append("</navPoint>" * (depth - d + 1))
        parts.append(f'<navPoint id="np{n}" playOrder="{n}"><navLabel><text>{_x(text)}</text></navLabel><content src="{_x(href)}"/>')
        depth = d
    if depth >= 0:
        parts.append("</navPoint>" * (depth + 1))
    if not parts:
        parts.append('<navPoint id="np1" playOrder="1"><navLabel><text>Start</text></navLabel><content src="text/ch000.xhtml"/></navPoint>')
    max_depth = max((d for d, _, _ in items), default=0) + 1
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'<head><meta name="dtb:uid" content="{_x(ident)}"/><meta name="dtb:depth" content="{max_depth}"/>'
        '<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>\n'
        f"<docTitle><text>{_x(title)}</text></docTitle>\n<navMap>{''.join(parts)}</navMap>\n</ncx>\n"
    )
