"""Screenshot EPUB chapters the way a phone reader shows them.

Renders chapters in headless Chromium at a Pixel-sized viewport (412x915 CSS px,
device pixel ratio 2.625, the Pixel 10's 1080 px width) with a reader-like font
size, and saves one PNG per screenful. It is the quickest way to check what a
conversion looks like on a phone without a device. The Kindle renderer itself
can differ.

    pip install -e ".[qa]"            # installs playwright (uses an existing Chromium)
    python tools/phone_preview.py book.epub out/ --chapters ch000 ch003 --screens 2
    python tools/phone_preview.py book.epub out/ --dark      # Kindle-style dark mode

Chromium: set PDF2EPUB_CHROMIUM to an executable, or have one under
/opt/pw-browsers (preinstalled in Claude Code cloud sessions), or run
``playwright install chromium``.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

VIEWPORT = {"width": 412, "height": 915}
DPR = 2.625


def find_chromium() -> str | None:
    env = os.environ.get("PDF2EPUB_CHROMIUM")
    if env:
        return env
    hits = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    return hits[-1] if hits else None


def chapter_names(book_dir: Path) -> list[str]:
    return sorted(p.stem for p in (book_dir / "OEBPS" / "text").glob("*.xhtml"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("epub", type=Path)
    ap.add_argument("out", type=Path, help="directory for the PNG screenshots")
    ap.add_argument("--chapters", nargs="*", help="chapter file stems, e.g. ch000 ch003 (default: all)")
    ap.add_argument("--screens", type=int, default=2, help="screenfuls per chapter (default 2)")
    ap.add_argument("--font-size", type=int, default=19, help="reader font size in CSS px (default 19)")
    ap.add_argument("--dark", action="store_true", help="white text on black, images untouched (like Kindle dark mode)")
    args = ap.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed: pip install -e '.[qa]'", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="pdf2epub-preview-"))
    try:
        with zipfile.ZipFile(args.epub) as z:
            z.extractall(work)
        chapters = args.chapters or chapter_names(work)
        fg, bg = ("#eee", "#000") if args.dark else ("#111", "#fff")
        reader_css = (
            f"html{{font-size:{args.font_size}px;font-family:Georgia,'Noto Serif',serif;background:{bg};color:{fg}}}"
            "body{margin:0 16px !important}"
        )
        args.out.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            exe = find_chromium()
            browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT, device_scale_factor=DPR)
            for ch in chapters:
                f = work / "OEBPS" / "text" / f"{ch}.xhtml"
                if not f.exists():
                    print(f"skip {ch}: no such chapter", file=sys.stderr)
                    continue
                page.goto(f.as_uri())
                page.add_style_tag(content=reader_css)
                page.wait_for_timeout(200)
                height = page.evaluate("document.documentElement.scrollHeight")
                n = max(1, min(args.screens, (height + VIEWPORT["height"] - 1) // VIEWPORT["height"]))
                for i in range(n):
                    clip = {"x": 0, "y": i * VIEWPORT["height"], **VIEWPORT}
                    page.screenshot(path=str(args.out / f"{ch}-{i}.png"), clip=clip, full_page=True)
                wide = page.evaluate("document.documentElement.scrollWidth") > VIEWPORT["width"]
                print(f"{ch}: {height}px tall, {n} screen(s){'  WARNING: horizontal overflow' if wide else ''}")
            browser.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
