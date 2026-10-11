"""Quality gate over every committed conversion in ``demo/``.

Each ``demo/<name>.pdf`` sits next to its accepted ``<name>.epub`` and a
``<name>.quality.json`` baseline (fidelity metrics, lint counts, structure
counts and the conversion options used). The gate re-converts every PDF with
the current code, evaluates it with :mod:`pdf2epub.quality` and fails on any
regression against the baseline.

    python tools/corpus_gate.py                 # check the whole corpus (parallel)
    python tools/corpus_gate.py 2505.19056      # check selected entries
    python tools/corpus_gate.py --show-issues   # list every lint issue of the new output
    python tools/corpus_gate.py --keep out/     # keep the new EPUBs for inspection
    python tools/corpus_gate.py --update        # accept: rewrite EPUBs + baselines (refuses on regressions)
    python tools/corpus_gate.py --add paper.pdf --name 2505.19056 [--title ... -a ...]

Policy: commit a code change only if this gate passes on every entry. When the
change improves the corpus, run ``--update`` and commit the regenerated EPUBs
and baselines together with the code, so the diff of ``*.quality.json`` shows
the improvement.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
import tempfile
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
sys.path.insert(0, str(ROOT / "src"))


def entries(names: list[str] | None = None) -> list[dict]:
    out = []
    for pdf in sorted(DEMO.glob("*.pdf")):
        if names and pdf.stem not in names:
            continue
        base = pdf.with_suffix(".quality.json")
        data = json.loads(base.read_text()) if base.exists() else {}
        out.append({"name": pdf.stem, "pdf": str(pdf), "baseline": data, "options": data.get("options", {})})
    return out


def _epub_title(epub: Path) -> str:
    import re

    with zipfile.ZipFile(epub) as z:
        opf = next(n for n in z.namelist() if n.endswith(".opf"))
        m = re.search(r"<dc:title>(.*?)</dc:title>", z.read(opf).decode("utf-8"), re.S)
    return html.unescape(m.group(1).strip()) if m else ""


def run_one(entry: dict, out_dir: str) -> dict:
    """Convert + evaluate one entry (runs in a worker process)."""
    from pdf2epub import ConvertOptions, convert, quality

    opts = entry["options"]
    out = Path(out_dir) / f"{entry['name']}.epub"
    rep = convert(entry["pdf"], out, ConvertOptions(
        title=opts.get("title"), authors=opts.get("authors"), language=opts.get("language", "en")))
    q = quality.evaluate(entry["pdf"], out)
    data = q.to_json()
    data["options"] = opts
    data["source"] = {"pages": rep.pages, "title": _epub_title(out)}
    issues = [f"[{i.kind}] {i.chapter} p{i.page}: {i.snippet}" for i in q.issues]
    return {"name": entry["name"], "current": data, "epub": str(out), "issues": issues}


def write_index() -> None:
    rows = []
    for e in entries():
        b = e["baseline"]
        if not b:
            continue
        m, c = b.get("metrics", {}), b.get("counts", {})
        issues = sum(b.get("issue_counts", {}).values())
        title = b.get("source", {}).get("title", "").replace("|", "\\|")
        rows.append(
            f"| [{e['name']}]({e['name']}.pdf) / [epub]({e['name']}.epub) | {title} | {b.get('source', {}).get('pages', '')} "
            f"| {c.get('figures', 0)}/{c.get('tables', 0)}/{c.get('equations', 0)} | {m.get('word_recall', 0):.3f} "
            f"| {m.get('order_score', 0):.3f} | {issues} |"
        )
    text = (
        "# Conversion corpus\n\n"
        "Every conversion delivered or used for development is kept here: the input PDF, the accepted EPUB, and a\n"
        "`.quality.json` baseline. `tests/test_corpus.py` and `tools/corpus_gate.py` re-convert each PDF with the\n"
        "current code and fail if any quality metric gets worse. Code changes are committed only when the gate passes,\n"
        "and improvements are accepted with `python tools/corpus_gate.py --update`, which rewrites these files.\n\n"
        "To read one on Kindle, download the `.epub` and use Send to Kindle (Android: Share → Kindle, or email it to\n"
        "your `@kindle.com` address). The Kindle app does not open `.epub` files tapped in a file manager.\n\n"
        "Columns: fig/tab/eq = figures, tables, display equations; recall = share of the PDF's words present in the\n"
        "EPUB (text and alt text); order = reading-order score; issues = lint findings (lower is better).\n\n"
        "| Files | Title | Pages | fig/tab/eq | recall | order | issues |\n"
        "| --- | --- | ---: | :---: | ---: | ---: | ---: |\n"
        + "\n".join(rows) + "\n"
    )
    (DEMO / "README.md").write_text(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*", help="entry names (file stems in demo/); default: all")
    ap.add_argument("--jobs", type=int, default=3, help="parallel conversions (default 3)")
    ap.add_argument("--update", action="store_true", help="accept the current output as the new baseline")
    ap.add_argument("--force", action="store_true", help="with --update: accept even if something regressed")
    ap.add_argument("--keep", type=Path, help="copy the new EPUBs here for inspection")
    ap.add_argument("--show-issues", action="store_true", help="print every lint issue of the new output")
    ap.add_argument("--add", type=Path, help="add a PDF to the corpus (copied into demo/)")
    ap.add_argument("--name", help="with --add: file stem to use in demo/ (default: the PDF's stem)")
    ap.add_argument("--title")
    ap.add_argument("-a", "--author", action="append")
    ap.add_argument("--index", action="store_true", help="only regenerate demo/README.md")
    args = ap.parse_args(argv)

    from pdf2epub import quality

    if args.index:
        write_index()
        return 0
    if args.add:
        name = args.name or args.add.stem
        dest = DEMO / f"{name}.pdf"
        DEMO.mkdir(exist_ok=True)
        if args.add.resolve() != dest.resolve():
            shutil.copyfile(args.add, dest)
        opts = {k: v for k, v in (("title", args.title), ("authors", args.author)) if v}
        res = run_one({"name": name, "pdf": str(dest), "options": opts, "baseline": {}}, str(DEMO))
        dest.with_suffix(".quality.json").write_text(json.dumps(res["current"], indent=2) + "\n")
        write_index()
        print(f"added {name}: " + json.dumps(res["current"]["metrics"]) + f" issues={sum(res['current']['issue_counts'].values())}")
        return 0

    todo = entries(args.names or None)
    tmp = Path(tempfile.mkdtemp(prefix="pdf2epub-gate-"))
    results = []
    try:
        with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futures = {e["name"]: (e, pool.submit(run_one, e, str(tmp))) for e in todo}
            for name, (e, fut) in futures.items():
                try:
                    results.append((e, fut.result()))
                except Exception as exc:  # a crash is the worst regression
                    results.append((e, {"name": name, "error": repr(exc)}))
        n_reg = 0
        for e, res in results:
            if "error" in res:
                n_reg += 1
                print(f"{e['name']:<14} CRASHED    {res['error']}")
                continue
            base, cur = e["baseline"], res["current"]
            regs = quality.compare(base, cur) if base.get("metrics") else []
            imps = quality.improvements(base, cur) if base.get("metrics") else ["(no baseline)"]
            n_reg += len(regs)
            status = "REGRESSED" if regs else ("improved" if imps else "same")
            print(f"{e['name']:<14} {status:<10} " + " ".join(f"{k.split('_')[0][:4]}{k.split('_')[1][:3]}={v:.4f}" for k, v in cur["metrics"].items())
                  + f" issues={sum(cur['issue_counts'].values())}")
            for r in regs:
                print(f"    - {r}")
            for i in imps:
                print(f"    + {i}")
            if args.show_issues:
                for line in res["issues"]:
                    print(f"      {line}")
            if args.keep:
                args.keep.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(res["epub"], args.keep / f"{e['name']}.epub")
        if args.update:
            if n_reg and not args.force:
                print("refusing to update baselines: there are regressions (use --force to override)")
                return 1
            for e, res in results:
                if "error" in res:
                    continue
                pdf = Path(e["pdf"])
                shutil.copyfile(res["epub"], pdf.with_suffix(".epub"))
                pdf.with_suffix(".quality.json").write_text(json.dumps(res["current"], indent=2) + "\n")
            write_index()
            print("baselines updated: commit demo/ together with the code change")
            return 0
        return 1 if n_reg else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
