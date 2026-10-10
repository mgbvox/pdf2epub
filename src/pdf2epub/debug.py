"""Debug overlays: draw detected layout structures on top of rendered pages."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from .layout import PageLayout, Region

COLORS = {
    "figure": (0.1, 0.35, 0.95),
    "table": (0.0, 0.65, 0.2),
    "textbox": (0.6, 0.1, 0.8),
    "equation": (1.0, 0.5, 0.0),
    "caption": (0.0, 0.7, 0.8),
    "text": (0.75, 0.75, 0.75),
    "footnote": (0.5, 0.3, 0.1),
    "furniture": (0.9, 0.2, 0.2),
}


def draw_overlays(doc: pymupdf.Document, layouts: list[PageLayout], out_dir: Path, scale: float = 1.5) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    tmp = pymupdf.open()
    for lay in layouts:
        tmp.insert_pdf(doc, from_page=lay.page, to_page=lay.page)
        page = tmp[-1]
        shape = page.new_shape()
        order = 0
        for it in lay.items:
            order += 1
            if isinstance(it, Region):
                _rect(shape, it.box, COLORS.get(it.kind, (0, 0, 0)), 1.2)
                for c in it.caption:
                    _rect(shape, c.box, COLORS["caption"], 0.6)
                for n in it.eqnum_segs:
                    _rect(shape, n.box, (1, 0, 0), 0.6)
                label = f"{order}:{it.kind}{(' ' + it.label) if it.label else ''}"
                shape.insert_text((it.box[0], it.box[1] - 1), label, fontsize=6, color=COLORS.get(it.kind))
            else:
                _rect(shape, it.box, COLORS["text"], 0.4)
                shape.insert_text((it.box[0] - 12, it.box[3]), str(order), fontsize=5, color=(0.8, 0, 0))
        for fn in lay.footnotes:
            for r in fn.rows:
                _rect(shape, r.box, COLORS["footnote"], 0.6)
        for r in lay.dropped:
            _rect(shape, r.box, COLORS["furniture"], 0.6)
        for c0, c1 in lay.columns:
            shape.draw_line((c0, 20), (c0, 40))
            shape.draw_line((c1, 20), (c1, 40))
            shape.finish(color=(1, 0, 1), width=1)
        shape.commit()
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        p = out_dir / f"page-{lay.page + 1:03d}.png"
        pix.save(str(p))
        paths.append(p)
    return paths


def _rect(shape, box, color, width) -> None:
    shape.draw_rect(pymupdf.Rect(*box))
    shape.finish(color=color, width=width)
