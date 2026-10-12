"""Quality gate over every committed conversion in ``demo/``.

Each ``demo/PDF/<name>.pdf`` sits next to its accepted ``<name>.epub`` and a
``<name>.quality.json`` baseline (fidelity metrics, lint counts, structure
counts and the conversion options used). ``<name>`` is
``{title_in_snake_case}-{arxiv_id}`` (see :mod:`pdf2epub.naming`), e.g.
``attention_is_all_you_need-1706.03762``. The gate re-converts every PDF with
the current code, evaluates it with :mod:`pdf2epub.quality` and fails on any
regression against the baseline.

    python tools/corpus_gate.py                 # check the whole corpus (parallel)
    python tools/corpus_gate.py 2505.19056      # check selected entries (arXiv id or full name)
    python tools/corpus_gate.py --show-issues   # list every lint issue of the new output
    python tools/corpus_gate.py --keep out/     # keep the new EPUBs for inspection
    python tools/corpus_gate.py --update        # accept: rewrite EPUBs + baselines (refuses on regressions)
    python tools/corpus_gate.py --add 2505.19056.pdf [--arxiv 2505.19056] [--title ... -a ...]
    python tools/corpus_gate.py --rescore       # metric change only: re-score the committed EPUBs
    python tools/corpus_gate.py --add-web URL [URL ...] [--title ... -a ...]   # add a web article (demo/web/)

Web articles (``pdf2epub web``) have their own section: ``demo/web/<name>.epub``
and ``<name>.quality.json`` (URLs, options, quality baseline). The page
snapshots (MHTML) and reference text stay in the git-ignored cache
``.corpus/web/<name>/``; the gate re-snapshots a page when the cache is empty.
If the page itself has changed since the baseline (its text fingerprint
differs) the entry is reported as ``SOURCE CHANGED`` instead of being compared.

Policy: commit a code change only if this gate passes on every entry. When the
change improves the corpus, run ``--update`` and commit the regenerated EPUBs
and baselines together with the code, so the diff of ``*.quality.json`` shows
the improvement. A change to :mod:`pdf2epub.quality` itself (a new lint rule, a
fixed metric) changes the yardstick, not the conversions: run ``--rescore``,
which re-evaluates the committed EPUBs without converting anything, and commit
that on its own before judging converter changes against the new numbers.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import tempfile
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
PDFS = DEMO / "PDF"  # committed: PDF papers, their accepted EPUBs and baselines
WEB = DEMO / "web"  # committed: web articles' accepted EPUBs and baselines
WEB_CACHE = ROOT / ".corpus" / "web"  # git-ignored: page snapshots and reference text
sys.path.insert(0, str(ROOT / "src"))


def _label(stem: str) -> str:
    """Short name for output: the arXiv id, or the stem."""
    from pdf2epub.naming import arxiv_id

    return arxiv_id(stem) or stem[:40]


def _selected(stem: str, names: list[str]) -> bool:
    from pdf2epub.naming import arxiv_id

    aid = arxiv_id(stem) or ""
    return any(n == stem or n == aid or aid.startswith(n + "v") for n in names)


def entries(names: list[str] | None = None) -> list[dict]:
    out = []
    for pdf in sorted(PDFS.glob("*.pdf")):
        if names and not _selected(pdf.stem, names):
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


def web_entries(names: list[str] | None = None) -> list[dict]:
    out = []
    for base in sorted(WEB.glob("*.quality.json")):
        name = base.name[: -len(".quality.json")]
        if names and not any(n == name or name.startswith(n) for n in names):
            continue
        data = json.loads(base.read_text())
        out.append({"name": name, "kind": "web", "baseline": data, "options": data.get("options", {}), "urls": data.get("urls", [])})
    return out


def _fingerprint(text: str) -> str:
    import hashlib

    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]


def _snapshots(name: str, urls: list[str]) -> list[Path]:
    """Cached MHTML snapshots of an entry's pages; captured from the live URLs when missing."""
    from pdf2epub.web import snapshot_pages

    paths = [WEB_CACHE / name / f"page-{i}.mhtml" for i in range(len(urls))]
    missing = [(u, p) for u, p in zip(urls, paths) if not p.exists()]
    if missing:
        snapshot_pages([u for u, _ in missing], [p for _, p in missing])
    return paths


def run_web(entry: dict, out_dir: str) -> dict:
    """Convert + evaluate one web entry from its snapshots (runs in a worker process)."""
    from pdf2epub import quality
    from pdf2epub.web import WebOptions, convert_web

    opts = entry["options"]
    snaps = entry.get("snapshots") or _snapshots(entry["name"], entry["urls"])
    out = Path(out_dir) / f"{entry['name']}.epub"
    rep = convert_web([p.as_uri() for p in snaps], out, WebOptions(
        title=opts.get("title"), authors=opts.get("authors"), canonical_urls=entry["urls"]))
    q = quality.evaluate_web(rep.reference_text, out)
    data = q.to_json()
    data.update(kind="web", urls=entry["urls"], options=opts, source={"pages": rep.pages, "title": rep.title},
                source_fingerprint=_fingerprint(rep.reference_text))
    issues = [f"[{i.kind}] {i.chapter}: {i.snippet}" for i in q.issues]
    return {"name": entry["name"], "current": data, "epub": str(out), "issues": issues, "reference": rep.reference_text}


def _accept_web(name: str, res: dict) -> None:
    WEB.mkdir(parents=True, exist_ok=True)
    (WEB / f"{name}.quality.json").write_text(json.dumps(res["current"], indent=2) + "\n")
    shutil.copyfile(res["epub"], WEB / f"{name}.epub")
    cache = WEB_CACHE / name
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "reference.txt").write_text(res["reference"], encoding="utf-8")


def write_index() -> None:
    rows = []
    for e in entries():
        b = e["baseline"]
        if not b:
            continue
        m, c = b.get("metrics", {}), b.get("counts", {})
        issues = sum(b.get("issue_counts", {}).values())
        title = b.get("source", {}).get("title", "").replace("|", "\\|")
        label = _label(e["name"])
        paper = f"[{label}](https://arxiv.org/abs/{label})" if label != e["name"][:40] else label
        rows.append(
            f"| {paper} | {title} | [pdf](PDF/{e['name']}.pdf) · [epub](PDF/{e['name']}.epub) | {b.get('source', {}).get('pages', '')} "
            f"| {c.get('figures', 0)}/{c.get('tables', 0)}/{c.get('equations', 0)} | {m.get('word_recall', 0):.3f} "
            f"| {m.get('order_score', 0):.3f} | {issues} |"
        )
    text = (
        "# Conversion corpus\n\n"
        "Every conversion delivered or used for development is kept here, in `PDF/` (papers) and `web/` (web\n"
        "articles): the source, the accepted EPUB, and a `.quality.json` baseline. `tests/test_corpus.py` and\n"
        "`tools/corpus_gate.py` re-convert each source with the\n"
        "current code and fail if any quality metric gets worse. Code changes are committed only when the gate passes,\n"
        "and improvements are accepted with `python tools/corpus_gate.py --update`, which rewrites these files.\n"
        "Files are named `{title_in_snake_case}-{arxiv_id}.{pdf,epub,quality.json}`.\n\n"
        "To read one on Kindle, download the `.epub` and use Send to Kindle (Android: Share → Kindle, or email it to\n"
        "your `@kindle.com` address). The Kindle app does not open `.epub` files tapped in a file manager.\n\n"
        "Columns: fig/tab/eq = figures, tables, display equations; recall = share of the PDF's words present in the\n"
        "EPUB (text and alt text); order = reading-order score; issues = lint findings (lower is better).\n\n"
        "| arXiv | Title | Files | Pages | fig/tab/eq | recall | order | issues |\n"
        "| --- | --- | --- | ---: | :---: | ---: | ---: | ---: |\n"
        + "\n".join(rows) + "\n"
    )
    web_rows = []
    for e in web_entries():
        b = e["baseline"]
        m, c = b.get("metrics", {}), b.get("counts", {})
        title = b.get("source", {}).get("title", "").replace("|", "\\|")
        links = " · ".join(f"[page {i + 1}]({u})" for i, u in enumerate(e["urls"]))
        web_rows.append(f"| [epub](web/{e['name']}.epub) | {title} | {links} | {c.get('figures', 0)}/{c.get('tables', 0)}/{c.get('equations', 0)} "
                        f"| {m.get('word_recall', 0):.3f} | {m.get('order_score', 0):.3f} | {sum(b.get('issue_counts', {}).values())} |")
    if web_rows:
        text += (
            "\n## Web articles (`pdf2epub web`)\n\n"
            "The web converter's corpus is in [`web/`](web/): the accepted EPUB and a baseline with the page URLs. The\n"
            "page snapshots stay in the git-ignored cache `.corpus/web/`; the gate re-snapshots a page when that cache\n"
            "is empty. recall is measured against the page's visible text.\n\n"
            "| File | Title | Source | fig/tab/eq | recall | order | issues |\n"
            "| --- | --- | --- | :---: | ---: | ---: | ---: |\n"
            + "\n".join(web_rows) + "\n"
        )
    (DEMO / "README.md").write_text(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*", help="entry names (file stems in demo/PDF or demo/web, or arXiv ids); default: all")
    ap.add_argument("--jobs", type=int, default=3, help="parallel conversions (default 3)")
    ap.add_argument("--update", action="store_true", help="accept the current output as the new baseline")
    ap.add_argument("--force", action="store_true", help="with --update: accept even if something regressed")
    ap.add_argument("--keep", type=Path, help="copy the new EPUBs here for inspection")
    ap.add_argument("--show-issues", action="store_true", help="print every lint issue of the new output")
    ap.add_argument("--add", type=Path, help="add a PDF to the corpus (copied into demo/PDF/)")
    ap.add_argument("--arxiv", help="with --add: arXiv id (default: found in the PDF's file name)")
    ap.add_argument("--name", help="with --add: override the file stem (default: {title_in_snake_case}-{arxiv_id})")
    ap.add_argument("--title")
    ap.add_argument("-a", "--author", action="append")
    ap.add_argument("--index", action="store_true", help="only regenerate demo/README.md")
    ap.add_argument("--rescore", action="store_true", help="re-evaluate the committed EPUBs with the current metrics (no conversion)")
    ap.add_argument("--add-web", nargs="+", metavar="URL", help="add a web article (several URLs: one book) to demo/web/")
    ap.add_argument("--web-only", action="store_true", help="check only the web articles")
    ap.add_argument("--pdf-only", action="store_true", help="check only the PDF papers")
    args = ap.parse_args(argv)

    from pdf2epub import quality

    if args.index:
        write_index()
        return 0
    if args.rescore:
        for e in entries(args.names or None):
            pdf = Path(e["pdf"])
            data = quality.evaluate(pdf, pdf.with_suffix(".epub")).to_json()
            for key in ("options", "source"):
                if key in e["baseline"]:
                    data[key] = e["baseline"][key]
            old = e["baseline"]
            print(f"{_label(e['name']):<14} issues {sum(old.get('issue_counts', {}).values())} -> {sum(data['issue_counts'].values())}  "
                  + " ".join(f"{k}={old.get('metrics', {}).get(k, 0):.4f}->{v:.4f}" for k, v in data["metrics"].items()
                             if abs(v - old.get("metrics", {}).get(k, 0)) > 1e-4))
            pdf.with_suffix(".quality.json").write_text(json.dumps(data, indent=2) + "\n")
        for e in web_entries(args.names or None):
            cache = WEB_CACHE / e["name"]
            if not (cache / "reference.txt").exists():
                print(f"{e['name'][:40]:<40} skipped: no cached reference text (run the gate once to rebuild the cache)")
                continue
            data = quality.evaluate_web((cache / "reference.txt").read_text(encoding="utf-8"), WEB / f"{e['name']}.epub").to_json()
            for key in ("kind", "urls", "options", "source", "source_fingerprint"):
                if key in e["baseline"]:
                    data[key] = e["baseline"][key]
            (WEB / f"{e['name']}.quality.json").write_text(json.dumps(data, indent=2) + "\n")
            print(f"{e['name'][:40]:<40} rescored")
        write_index()
        return 0
    if args.add_web:
        from pdf2epub.naming import paper_stem
        from pdf2epub.web import snapshot_pages

        opts = {k: v for k, v in (("title", args.title), ("authors", args.author)) if v}
        tmp = Path(tempfile.mkdtemp(prefix="pdf2epub-addweb-"))
        try:
            snaps = [tmp / f"page-{i}.mhtml" for i in range(len(args.add_web))]
            snapshot_pages(args.add_web, snaps)
            res = run_web({"name": "new", "urls": args.add_web, "options": opts, "snapshots": snaps}, str(tmp))
            name = args.name or paper_stem(res["current"]["source"]["title"], None)
            if (WEB / f"{name}.quality.json").exists():
                print(f"demo/web/{name} already exists")
                return 1
            cache = WEB_CACHE / name
            cache.mkdir(parents=True, exist_ok=True)
            for p in snaps:
                shutil.copyfile(p, cache / p.name)
            _accept_web(name, res)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        write_index()
        print(f"added demo/web/{name}.{{epub,quality.json}} (page snapshots cached in .corpus/web/{name}/): "
              + json.dumps(res["current"]["metrics"]) + f" issues={sum(res['current']['issue_counts'].values())}")
        return 0
    if args.add:
        from pdf2epub.naming import arxiv_id, paper_stem

        aid = args.arxiv or arxiv_id(args.add.name)
        if aid:
            base = re.sub(r"v\d+$", "", aid)  # another version of the same paper counts as the same entry
            clash = [e["name"] for e in entries() if re.sub(r"v\d+$", "", _label(e["name"])) == base]
            if clash:
                print(f"{aid} is already in the corpus as {clash[0]}")
                return 1
        elif not args.name:
            print("warning: no arXiv id in the file name; pass --arxiv ID (naming the files by title only)")
        opts = {k: v for k, v in (("title", args.title), ("authors", args.author)) if v}
        tmp = Path(tempfile.mkdtemp(prefix="pdf2epub-add-"))
        try:
            res = run_one({"name": "new", "pdf": str(args.add), "options": opts, "baseline": {}}, str(tmp))
            name = args.name or paper_stem(res["current"]["source"]["title"], aid)
            dest = PDFS / f"{name}.pdf"
            if dest.exists() and dest.resolve() != args.add.resolve():
                print(f"{dest} already exists")
                return 1
            PDFS.mkdir(parents=True, exist_ok=True)
            if args.add.resolve() != dest.resolve():
                shutil.copyfile(args.add, dest)
            shutil.copyfile(res["epub"], dest.with_suffix(".epub"))
            dest.with_suffix(".quality.json").write_text(json.dumps(res["current"], indent=2) + "\n")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        write_index()
        print(f"added demo/PDF/{name}.{{pdf,epub,quality.json}}: " + json.dumps(res["current"]["metrics"])
              + f" issues={sum(res['current']['issue_counts'].values())}")
        return 0

    todo = [] if args.web_only else entries(args.names or None)
    todo += [] if args.pdf_only else web_entries(args.names or None)
    if args.names:
        unknown = [n for n in args.names if not any(_selected(e["name"], [n]) or e["name"].startswith(n) for e in todo)]
        if unknown:
            print("no corpus entry matches: " + ", ".join(unknown) + " (use an arXiv id or a full name from demo/PDF or demo/web)")
            return 2
    tmp = Path(tempfile.mkdtemp(prefix="pdf2epub-gate-"))
    results = []
    try:
        with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futures = {e["name"]: (e, pool.submit(run_web if e.get("kind") == "web" else run_one, e, str(tmp))) for e in todo}
            for name, (e, fut) in futures.items():
                try:
                    results.append((e, fut.result()))
                except Exception as exc:  # a crash is the worst regression
                    results.append((e, {"name": name, "error": repr(exc)}))
        n_reg = 0
        for e, res in results:
            if "error" in res:
                n_reg += 1
                print(f"{_label(e['name'])[:14]:<14} CRASHED    {res['error']}")
                continue
            base, cur = e["baseline"], res["current"]
            if e.get("kind") == "web" and base.get("source_fingerprint") and base["source_fingerprint"] != cur.get("source_fingerprint"):
                # the live page changed since the baseline: numbers are not comparable
                print(f"{e['name'][:14]:<14} SOURCE CHANGED  the page differs from the baseline's; review it, then --update")
                continue
            regs = quality.compare(base, cur) if base.get("metrics") else []
            imps = quality.improvements(base, cur) if base.get("metrics") else ["(no baseline)"]
            n_reg += len(regs)
            status = "REGRESSED" if regs else ("improved" if imps else "same")
            print(f"{(_label(e['name']) if e.get('kind') != 'web' else e['name'][:14]):<14} {status:<10} " + " ".join(f"{k.split('_')[0][:4]}{k.split('_')[1][:3]}={v:.4f}" for k, v in cur["metrics"].items())
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
                if e.get("kind") == "web":
                    _accept_web(e["name"], res)
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
