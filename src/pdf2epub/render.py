"""Rasterise PDF regions into crisp, tightly cropped images.

Every image is rendered straight from the PDF's vector content, so formulas,
diagrams and tables are reproduced exactly.  Neighbouring content that happens
to fall inside a crop rectangle is masked out using the known positions of the
region's own glyphs/paths, then the image is trimmed to its ink.
"""

from __future__ import annotations

import io
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import pymupdf
from PIL import Image

from . import geometry as G
from .geometry import Box

WHITE_THRESHOLD = 246


@dataclass
class ImageAsset:
    name: str  # file name inside the EPUB (images/...)
    data: bytes
    media_type: str
    width_px: int
    height_px: int
    width_pt: float  # physical size of the cropped area in PDF points
    height_pt: float
    ink_box: Box  # page coordinates of the cropped area


class Renderer:
    def __init__(self, doc: pymupdf.Document, dpi: int = 400, max_page_pixels: int = 60_000_000, cache_pages: int = 2):
        self.doc = doc
        self.dpi = dpi
        self.scale = dpi / 72.0
        self.max_page_pixels = max_page_pixels
        self._cache: OrderedDict[int, tuple[np.ndarray, float]] = OrderedDict()
        self._cache_pages = cache_pages
        self._counter = 0
        self.assets: list[ImageAsset] = []

    # -- page pixels -----------------------------------------------------
    def _page(self, pno: int) -> tuple[np.ndarray, float]:
        if pno in self._cache:
            self._cache.move_to_end(pno)
            return self._cache[pno]
        page = self.doc[pno]
        scale = self.scale
        px = page.rect.width * page.rect.height * scale * scale
        if px > self.max_page_pixels:
            scale = (self.max_page_pixels / (page.rect.width * page.rect.height)) ** 0.5
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, colorspace=pymupdf.csRGB, annots=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()
        self._cache[pno] = (arr, scale)
        while len(self._cache) > self._cache_pages:
            self._cache.popitem(last=False)
        return arr, scale

    # -- cropping --------------------------------------------------------
    def crop(
        self,
        pno: int,
        members: list[Box],
        foreign: list[Box],
        pad: float = 2.0,
        margin: float = 1.0,
        limit: Box | None = None,
        allow_boxes: list[Box] | None = None,
        pad_x: float | None = None,
    ) -> tuple[np.ndarray, Box] | None:
        """Crop the ink belonging to ``members``; pixels covered only by ``foreign`` boxes are erased.

        ``allow_boxes`` (default: ``members``) mark where member ink may overlap foreign boxes.
        """
        arr, scale = self._page(pno)
        h, w = arr.shape[:2]
        mbox = G.union(members)
        if G.is_empty(mbox):
            return None
        search = G.expand(mbox, pad if pad_x is None else pad_x, pad)
        page_box = (0.0, 0.0, w / scale, h / scale)
        search = G.intersect(search, page_box)
        if limit is not None:
            search = G.intersect(search, limit)
        x0, y0 = int(max(0, search[0] * scale)), int(max(0, search[1] * scale))
        x1, y1 = int(min(w, search[2] * scale + 1)), int(min(h, search[3] * scale + 1))
        if x1 <= x0 or y1 <= y0:
            return None
        img = arr[y0:y1, x0:x1].copy()
        allow = np.zeros(img.shape[:2], dtype=bool)
        for b in (allow_boxes if allow_boxes is not None else members):
            bx0, by0 = int(b[0] * scale) - x0 - 1, int(b[1] * scale) - y0 - 1
            bx1, by1 = int(b[2] * scale) - x0 + 2, int(b[3] * scale) - y0 + 2
            allow[max(0, by0) : max(0, by1), max(0, bx0) : max(0, bx1)] = True
        for b in foreign:
            if not G.overlaps(b, search):
                continue
            bh = b[3] - b[1]
            # glyph ink can overshoot its advance box (f hooks, italics): widen small boxes
            ey = 0.06 * bh if bh < 30 else 0.0
            ex = 0.12 * bh if bh < 30 else 0.0
            bx0, by0 = int((b[0] - ex) * scale) - x0 - 1, int((b[1] - ey) * scale) - y0
            bx1, by1 = int((b[2] + ex) * scale) - x0 + 2, int((b[3] + ey) * scale) - y0 + 1
            sl = (slice(max(0, by0), max(0, by1)), slice(max(0, bx0), max(0, bx1)))
            region = img[sl]
            keep = allow[sl]
            region[~keep] = 255
        ink = img.min(axis=2) < WHITE_THRESHOLD
        rows = np.where(ink.any(axis=1))[0]
        cols = np.where(ink.any(axis=0))[0]
        if len(rows) == 0 or len(cols) == 0:
            return None
        m = int(round(margin * scale))
        r0, r1 = rows[0], rows[-1] + 1
        c0, c1 = cols[0], cols[-1] + 1
        out = img[r0:r1, c0:c1]
        out = np.pad(out, ((m, m), (m, m), (0, 0)), constant_values=255)
        ink_box = ((x0 + c0) / scale, (y0 + r0) / scale, (x0 + c1) / scale, (y0 + r1) / scale)
        return out, ink_box

    # -- encoding --------------------------------------------------------
    def save(self, arr: np.ndarray, ink_box: Box, prefix: str, photo: bool = False, scale: float | None = None) -> ImageAsset:
        scale = scale or self._page_scale_guess()
        img = Image.fromarray(arr)
        diff = int(arr.max(axis=2).astype(int).max() - 0) if arr.size else 0
        rgb_spread = np.abs(arr.astype(np.int16) - arr.mean(axis=2, keepdims=True)).max() if arr.size else 0
        if rgb_spread <= 6:
            img = img.convert("L")
        max_side = 3200
        if max(img.size) > max_side:
            f = max_side / max(img.size)
            img = img.resize((max(1, int(img.width * f)), max(1, int(img.height * f))), Image.LANCZOS)
        buf = io.BytesIO()
        if photo:
            img.convert("RGB" if img.mode != "L" else "L").save(buf, "JPEG", quality=88, optimize=True)
            ext, mt = "jpg", "image/jpeg"
        else:
            img.save(buf, "PNG", optimize=True)
            ext, mt = "png", "image/png"
        self._counter += 1
        name = f"images/{prefix}-{self._counter:04d}.{ext}"
        _ = diff
        asset = ImageAsset(
            name=name, data=buf.getvalue(), media_type=mt, width_px=img.width, height_px=img.height,
            width_pt=arr.shape[1] / scale, height_pt=arr.shape[0] / scale, ink_box=ink_box,
        )
        self.assets.append(asset)
        return asset

    def _page_scale_guess(self) -> float:
        if self._cache:
            return next(reversed(self._cache.values()))[1]
        return self.scale

    def page_scale(self, pno: int) -> float:
        return self._page(pno)[1]

    def render_cover(self, pno: int = 0, height_px: int = 1600) -> ImageAsset:
        page = self.doc[pno]
        z = height_px / page.rect.height
        pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False, colorspace=pymupdf.csRGB, annots=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85, optimize=True)
        asset = ImageAsset("images/cover.jpg", buf.getvalue(), "image/jpeg", img.width, img.height,
                           page.rect.width, page.rect.height, (0, 0, page.rect.width, page.rect.height))
        return asset


# ---------------------------------------------------------------------------
# image post-processing helpers


_X_HEIGHT = set("acemnorsuvwxzıȷ")
_ASCENDER = set("bdfhkltß")
_DESCENDER = set("gpqy")
_CAPS_DIGITS = set("ABCDEFGHIJKLMNOPRSTUVWXYZ0123456789")


def ink_box(g) -> tuple[float, float, float, float]:
    """A tight estimate of where a glyph's ink can be (tighter than font metrics).

    Used to decide which pixels a region may keep where its glyph boxes overlap
    those of neighbouring text (e.g. a subscript vs. the next line's ascenders).
    """
    c, s, oy = g.c, g.size, g.oy
    italic_pad = 0.12 * s if (g.info and g.info.italic) else 0.04 * s
    if c in _X_HEIGHT:
        top, bot = 0.5, 0.03
    elif c in _ASCENDER:
        top, bot = 0.74, 0.03
    elif c in "ij":
        top, bot = 0.7, 0.03 if c == "i" else 0.24
    elif c in _DESCENDER:
        top, bot = 0.5, 0.24
    elif c in _CAPS_DIGITS:
        top, bot = 0.74, 0.03
    elif c == "Q":
        top, bot = 0.74, 0.22
    elif c in ".,;:":
        top, bot = 0.5, 0.22
    else:
        return g.box
    if g.info and g.info.italic and c == "f":
        bot = 0.24
    return (g.x0 - italic_pad, oy - top * s, g.x1 + italic_pad, oy + bot * s)


def split_wide(arr: np.ndarray, max_px: int, min_gap_px: int, min_part_px: int) -> list[np.ndarray]:
    """Split an over-wide image at blank vertical bands (e.g. two equations on one line)."""
    if arr.shape[1] <= max_px:
        return [arr]
    ink_cols = (arr.min(axis=2) < WHITE_THRESHOLD).any(axis=0)
    gaps = []
    start = None
    for x, v in enumerate(ink_cols):
        if not v and start is None:
            start = x
        elif v and start is not None:
            if x - start >= min_gap_px:
                gaps.append((x - start, start, x))
            start = None
    w = arr.shape[1]
    gaps = [g for g in gaps if g[1] >= min_part_px and w - g[2] >= min_part_px]
    if not gaps:
        return [arr]
    _, a, b = max(gaps)
    left = arr[:, :a]
    right = arr[:, b:]
    pad = np.full((arr.shape[0], max(2, min_gap_px // 6), arr.shape[2]), 255, dtype=arr.dtype)
    return split_wide(np.concatenate([left, pad], axis=1), max_px, min_gap_px, min_part_px) + split_wide(
        np.concatenate([pad, right], axis=1), max_px, min_gap_px, min_part_px
    )


def squeeze_gap(arr: np.ndarray, min_gap_px: int, keep_px: int) -> np.ndarray:
    """Shrink the widest fully-blank vertical band (e.g. before an equation number)."""
    ink_cols = (arr.min(axis=2) < WHITE_THRESHOLD).any(axis=0)
    best = (0, 0)
    start = None
    for x, v in enumerate(ink_cols):
        if not v and start is None:
            start = x
        elif v and start is not None:
            if x - start > best[1] - best[0]:
                best = (start, x)
            start = None
    a, b = best
    if b - a < min_gap_px or a == 0:
        return arr
    half = keep_px // 2
    return np.concatenate([arr[:, : a + half], arr[:, b - (keep_px - half) :]], axis=1)


def is_photographic(arr: np.ndarray) -> bool:
    """Heuristic: many distinct colours and few pure-white pixels -> JPEG."""
    small = arr[:: max(1, arr.shape[0] // 200), :: max(1, arr.shape[1] // 200)]
    flat = small.reshape(-1, small.shape[-1])
    white = (flat.min(axis=1) > 245).mean()
    uniq = len(np.unique((flat // 8).astype(np.uint8), axis=0))
    return white < 0.3 and uniq > 1500
