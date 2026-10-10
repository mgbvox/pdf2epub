"""Document model produced by structure analysis and consumed by the HTML writer."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import geometry as G
from .extract import Glyph, Link, Row
from .geometry import Box
from .layout import Region


@dataclass(eq=False)
class Line:
    """A visual line of flowing text (one or more row segments glued together)."""

    glyphs: list[Glyph]
    baseline: float
    size: float
    page: int
    box: Box
    row_id: int = -1
    links: list[Link] = field(default_factory=list)
    frame: tuple[float, float] = (0.0, 0.0)  # left/right reference of its text block

    @classmethod
    def from_segments(cls, segs: list[Row], links: list[Link]) -> "Line":
        glyphs = [g for s in segs for g in s.glyphs]
        glyphs.sort(key=lambda g: (g.x0, -g.level))
        box = G.union(s.box for s in segs)
        return cls(glyphs, segs[0].baseline, segs[0].size, segs[0].page, box, segs[0].row_id, links)

    @property
    def x0(self) -> float:
        return self.box[0]

    @property
    def x1(self) -> float:
        return self.box[2]

    @property
    def core(self) -> Box:
        return (self.box[0], self.baseline - 0.72 * self.size, self.box[2], self.baseline + 0.24 * self.size)

    @property
    def text(self) -> str:
        from .extract import glyph_text

        return glyph_text(self.glyphs, self.size)


@dataclass(eq=False)
class Block:
    page: int
    box: Box
    id: str = ""


@dataclass(eq=False)
class Paragraph(Block):
    lines: list[Line] = field(default_factory=list)
    kind: str = "p"  # p | heading | title | caption | li | code | centered | bib | note | author
    level: int = 0  # heading level / list depth
    marker: str = ""  # list bullet / enumerator
    style: str = ""  # extra css class (abstract, continuation, small ...)
    keep_breaks: bool = False  # render visual line breaks


@dataclass(eq=False)
class Visual(Block):
    """A region rendered as an image (figure/table/equation) or a boxed text."""

    region: Region | None = None
    kind: str = "figure"  # figure | table | equation | textbox
    caption: Paragraph | None = None
    caption_above: bool = False
    label: str = ""
    # for text boxes: groups of paragraphs separated by rules/frames
    box_groups: list[list[Paragraph]] = field(default_factory=list)


@dataclass(eq=False)
class FootnoteBlock:
    id: str
    marker: str
    page: int
    box: Box
    paragraph: Paragraph
    chapter: int = -1
    ref_ids: list[str] = field(default_factory=list)


@dataclass(eq=False)
class Document:
    title: str
    authors: list[str]
    language: str
    blocks: list[Block]
    footnotes: list[FootnoteBlock]
    page_count: int
    body_size: float
    source_name: str = ""
