"""Quality gate: every committed conversion must not get worse.

Each ``demo/<name>.pdf`` is re-converted with the current code and compared
with the accepted baseline ``demo/<name>.quality.json`` (fidelity metrics and
lint counts from :mod:`pdf2epub.quality`). A failure means the change made at
least one historical conversion worse; fix the change, or if the change is a
deliberate trade-off, discuss it before accepting new baselines with
``python tools/corpus_gate.py --update``.

Run only this gate with ``pytest -m corpus``; skip it while iterating with
``pytest -m "not corpus"``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pdf2epub import ConvertOptions, convert, quality
from pdf2epub.naming import arxiv_id, paper_stem

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"

COMMITTED = sorted(p for p in DEMO.glob("*.pdf") if p.with_suffix(".quality.json").exists())
WEB_ENTRIES = sorted((DEMO / "web").glob("*.quality.json"))


def _check(pdf: Path, baseline: dict, tmp_path: Path) -> None:
    opts = baseline.get("options", {})
    out = tmp_path / f"{pdf.stem}.epub"
    convert(pdf, out, ConvertOptions(title=opts.get("title"), authors=opts.get("authors"), language=opts.get("language", "en")))
    current = quality.evaluate(pdf, out).to_json()
    regressions = quality.compare(baseline, current)
    assert not regressions, f"{pdf.name} regressed: {regressions}"


def test_every_committed_pdf_has_a_conversion_and_baseline():
    for pdf in DEMO.glob("*.pdf"):
        assert pdf.with_suffix(".epub").exists(), f"{pdf.name} has no committed EPUB next to it"
        assert pdf.with_suffix(".quality.json").exists(), f"{pdf.name} has no quality baseline (add it with tools/corpus_gate.py --add)"


def test_corpus_files_are_named_title_dash_arxiv_id():
    """demo/ files are named {title_in_snake_case}-{arxiv_id}.{ext}, the title being the EPUB's."""
    for base in DEMO.glob("*.quality.json"):
        stem = base.name[: -len(".quality.json")]
        title = json.loads(base.read_text()).get("source", {}).get("title", "")
        assert stem == paper_stem(title, arxiv_id(stem)), f"{stem} should be named {paper_stem(title, arxiv_id(stem))}"


@pytest.mark.corpus
@pytest.mark.parametrize("pdf", COMMITTED, ids=[p.stem for p in COMMITTED])
def test_committed_conversion_does_not_regress(pdf: Path, tmp_path: Path):
    _check(pdf, json.loads(pdf.with_suffix(".quality.json").read_text()), tmp_path)


def test_web_corpus_manifests_are_complete_and_named_by_title():
    """demo/web/<name>.quality.json: URLs + baseline only (page snapshots stay in .corpus/web/)."""
    for base in WEB_ENTRIES:
        data = json.loads(base.read_text())
        name = base.name[: -len(".quality.json")]
        assert data.get("kind") == "web" and data.get("urls"), f"{name}: needs kind=web and its URLs"
        assert {"metrics", "issue_counts", "source", "source_fingerprint"} <= data.keys(), f"{name}: incomplete baseline"
        assert name == paper_stem(data["source"]["title"], arxiv_id(data["urls"][0])), f"{name} is not named after its title"
    assert not list((DEMO / "web").glob("*.epub")) and not list((DEMO / "web").glob("*.mhtml")), \
        "web article content belongs in the git-ignored .corpus/web/ cache, not in demo/web/"


@pytest.mark.corpus
@pytest.mark.parametrize("base", WEB_ENTRIES, ids=[b.name[: -len(".quality.json")] for b in WEB_ENTRIES])
def test_web_conversion_does_not_regress(base: Path, tmp_path: Path):
    pytest.importorskip("playwright.sync_api")
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    import corpus_gate

    data = json.loads(base.read_text())
    name = base.name[: -len(".quality.json")]
    res = corpus_gate.run_web({"name": name, "urls": data["urls"], "options": data.get("options", {})}, str(tmp_path))
    if res["current"]["source_fingerprint"] != data["source_fingerprint"]:
        pytest.skip(f"{name}: the live page changed since its baseline; review and accept with corpus_gate.py --update")
    regressions = quality.compare(data, res["current"])
    assert not regressions, f"{name} regressed: {regressions}"
