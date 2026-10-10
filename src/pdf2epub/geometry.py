"""Tiny axis-aligned box helpers.

Boxes are plain ``(x0, y0, x1, y1)`` tuples in PDF points with a top-left
origin (the PyMuPDF convention).  Plain tuples keep the hot loops of the
layout analysis fast and make the data easy to serialise for debugging.
"""

from __future__ import annotations

from collections.abc import Iterable

Box = tuple[float, float, float, float]

EMPTY: Box = (float("inf"), float("inf"), float("-inf"), float("-inf"))


def union(boxes: Iterable[Box]) -> Box:
    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    for b in boxes:
        if b[0] < x0:
            x0 = b[0]
        if b[1] < y0:
            y0 = b[1]
        if b[2] > x1:
            x1 = b[2]
        if b[3] > y1:
            y1 = b[3]
    return (x0, y0, x1, y1)


def is_empty(b: Box) -> bool:
    return b[2] < b[0] or b[3] < b[1]


def width(b: Box) -> float:
    return b[2] - b[0]


def height(b: Box) -> float:
    return b[3] - b[1]


def area(b: Box) -> float:
    if is_empty(b):
        return 0.0
    return (b[2] - b[0]) * (b[3] - b[1])


def intersect(a: Box, b: Box) -> Box:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def intersection_area(a: Box, b: Box) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return w * h


def overlaps(a: Box, b: Box, tol: float = 0.0) -> bool:
    return a[0] < b[2] + tol and b[0] < a[2] + tol and a[1] < b[3] + tol and b[1] < a[3] + tol


def contains(outer: Box, inner: Box, tol: float = 0.0) -> bool:
    return (
        inner[0] >= outer[0] - tol
        and inner[1] >= outer[1] - tol
        and inner[2] <= outer[2] + tol
        and inner[3] <= outer[3] + tol
    )


def fraction_inside(inner: Box, outer: Box) -> float:
    """Share of ``inner``'s area that lies inside ``outer``."""
    a = area(inner)
    if a <= 0:
        # Degenerate (zero-height rule etc.): fall back to a point/segment test.
        cx = (inner[0] + inner[2]) / 2
        cy = (inner[1] + inner[3]) / 2
        return 1.0 if outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3] else 0.0
    return intersection_area(inner, outer) / a


def expand(b: Box, dx: float, dy: float | None = None) -> Box:
    if dy is None:
        dy = dx
    return (b[0] - dx, b[1] - dy, b[2] + dx, b[3] + dy)


def h_overlap(a: Box, b: Box) -> float:
    return min(a[2], b[2]) - max(a[0], b[0])


def v_overlap(a: Box, b: Box) -> float:
    return min(a[3], b[3]) - max(a[1], b[1])


def h_gap(a: Box, b: Box) -> float:
    """Horizontal distance between boxes (negative when they overlap)."""
    return max(a[0], b[0]) - min(a[2], b[2])


def v_gap(a: Box, b: Box) -> float:
    """Vertical distance between boxes (negative when they overlap)."""
    return max(a[1], b[1]) - min(a[3], b[3])


def distance(a: Box, b: Box) -> float:
    dx = max(0.0, h_gap(a, b))
    dy = max(0.0, v_gap(a, b))
    return (dx * dx + dy * dy) ** 0.5


def clip(b: Box, bounds: Box) -> Box:
    return intersect(b, bounds)
