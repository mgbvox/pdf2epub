"""pdf2epub: convert PDFs into reflowable, Kindle-friendly EPUBs without losing fidelity.

Text is re-flowed so it can be read comfortably on a phone, while formulas,
figures and tables are rendered straight from the PDF's vector content so they
stay exact and legible.

Example::

    from pdf2epub import convert, ConvertOptions

    report = convert("paper.pdf", "paper.epub", ConvertOptions(dpi=400))
    print(report.figures, report.equations)
"""

__version__ = "0.1.0"

from .converter import ConversionReport, ConvertOptions, analyze, build_document, convert

__all__ = ["convert", "ConvertOptions", "ConversionReport", "analyze", "build_document", "__version__"]
