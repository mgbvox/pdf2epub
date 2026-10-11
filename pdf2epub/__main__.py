import sys
from pathlib import Path

from .convert import pdf_to_epub

if __name__ == "__main__":
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".epub")
    print(pdf_to_epub(src, dst))
