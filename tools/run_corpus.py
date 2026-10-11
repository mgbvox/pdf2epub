"""Regression run over a corpus of arXiv papers.

Downloads each paper once (cached), converts it with pdf2epub, optionally
validates the EPUB with EPUBCheck and draws layout overlays, then prints one
summary row per paper. Run it before and after any change to the layout
heuristics and compare the counts.

    python tools/run_corpus.py                         # default CS + bio corpus
    python tools/run_corpus.py 2406.11717 1810.04805   # specific arXiv ids
    EPUBCHECK_JAR=/path/epubcheck.jar python tools/run_corpus.py --inspect

Exit status is non-zero if any conversion crashes or EPUBCheck reports errors.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

# Mixed layouts: NeurIPS/ICLR single column, ACL/CVPR/ICML two column, a
# math-heavy proof appendix, LaTeX q-bio papers and a Google-Docs PDF.
DEFAULT_CORPUS = [
    "2406.11717",  # NeurIPS, text boxes, many inline formulas (the demo paper)
    "1706.03762",  # NeurIPS single column, tables, equations
    "1810.04805",  # ACL two column, footnotes, wide figure
    "1512.03385",  # CVPR two column, inline math, many tables
    "2002.05709",  # ICML two column, Algorithm block, sub-figures
    "1412.6980",   # ICLR, small-caps headings, long display derivations
    "2610.10482",  # q-bio.GN
    "2610.11729",  # q-bio.BM, Google Docs generated (no LaTeX fonts)
    "2610.12426",  # q-bio.NC, Latin Modern math, 100+ equations
    "2610.10913",  # q-bio.QM, pure Computer Modern body text
    "2610.08937",  # q-bio.BM, long, many tables
]


def fetch(arxiv_id: str, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{arxiv_id}.pdf"
    if not path.exists() or path.stat().st_size == 0:
        req = urllib.request.Request(f"https://arxiv.org/pdf/{arxiv_id}", headers={"User-Agent": "pdf2epub-corpus/0.1"})
        with urllib.request.urlopen(req, timeout=120) as r:
            path.write_bytes(r.read())
        time.sleep(3)  # be polite to arXiv
    return path


def epubcheck(epub: Path) -> str:
    jar = os.environ.get("EPUBCHECK_JAR")
    if not jar:
        return "-"
    res = subprocess.run(["java", "-jar", jar, str(epub)], capture_output=True, text=True)
    for line in (res.stdout + res.stderr).splitlines():
        if line.startswith("Messages:"):
            return line.replace("Messages:", "").strip()
    return f"exit {res.returncode}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ids", nargs="*", help="arXiv ids (default: built-in corpus)")
    ap.add_argument("--cache", type=Path, default=Path(".corpus/pdf"), help="where PDFs are cached")
    ap.add_argument("--out", type=Path, default=Path(".corpus/out"), help="where EPUBs (and overlays) go")
    ap.add_argument("--inspect", action="store_true", help="also write layout overlays per paper")
    args = ap.parse_args(argv)

    from pdf2epub import ConvertOptions, convert

    failures = 0
    args.out.mkdir(parents=True, exist_ok=True)
    header = f"{'paper':<12} {'pages':>5} {'fig':>4} {'tab':>4} {'eq':>4} {'box':>4} {'fn':>4} {'head':>5} {'para':>5} {'sec':>6}  epubcheck"
    print(header)
    print("-" * len(header))
    for arxiv_id in args.ids or DEFAULT_CORPUS:
        try:
            pdf = fetch(arxiv_id, args.cache)
            t0 = time.time()
            rep = convert(pdf, args.out / f"{arxiv_id}.epub", ConvertOptions())
            check = epubcheck(rep.output)
            if check not in ("-",) and "0 errors" not in check:
                failures += 1
            print(
                f"{arxiv_id:<12} {rep.pages:>5} {rep.figures:>4} {rep.tables:>4} {rep.equations:>4} {rep.text_boxes:>4} "
                f"{rep.footnotes:>4} {rep.headings:>5} {rep.paragraphs:>5} {time.time() - t0:>5.1f}s  {check}"
            )
            if args.inspect:
                from pdf2epub.converter import analyze
                from pdf2epub.debug import draw_overlays

                doc, _, _, layouts = analyze(pdf)
                draw_overlays(doc, layouts, args.out / f"{arxiv_id}-overlays")
        except Exception:  # keep going: one broken paper must not hide the rest
            failures += 1
            print(f"{arxiv_id:<12} FAILED", file=sys.stderr)
            traceback.print_exc()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
