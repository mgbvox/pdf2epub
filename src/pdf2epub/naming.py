"""File names for converted papers: ``{title_in_snake_case}-{arxiv_id}.{ext}``.

Example: ``refusal_in_language_models_is_mediated_by_a_single_direction-2406.11717v3.epub``.
The corpus in ``demo/PDF`` uses this for the PDF, the EPUB and the quality baseline,
and ``pdf2epub convert`` uses it for its default output name when the input file
name contains an arXiv id.
"""

from __future__ import annotations

import re
import unicodedata

# new-style arXiv ids (YYMM.NNNNN, optional version), not glued to other digits
ARXIV_ID_RE = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5}(?:v\d+)?)(?![\d])")


def snake_title(title: str) -> str:
    """Lowercase ASCII words joined by underscores; apostrophes vanish ("Seurat's" -> "seurats")."""
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"['’`]", "", text.lower())
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def arxiv_id(text: str) -> str | None:
    """The last arXiv id found in ``text`` (a file name, URL or stem), or None."""
    found = ARXIV_ID_RE.findall(text)
    return found[-1] if found else None


def paper_stem(title: str, arxiv: str | None) -> str:
    """``{snake_title}-{arxiv}``; just the snake-case title when there is no arXiv id."""
    snake = snake_title(title) or "untitled"
    return f"{snake}-{arxiv}" if arxiv else snake
