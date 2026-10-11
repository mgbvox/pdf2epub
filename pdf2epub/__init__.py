"""Minimal PDF -> EPUB converter (text only; uses poppler's pdftotext + stdlib)."""
from .convert import pdf_to_epub

__all__ = ["pdf_to_epub"]
