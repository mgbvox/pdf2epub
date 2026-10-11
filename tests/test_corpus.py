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

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"

COMMITTED = sorted(p for p in DEMO.glob("*.pdf") if p.with_suffix(".quality.json").exists())


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


@pytest.mark.corpus
@pytest.mark.parametrize("pdf", COMMITTED, ids=[p.stem for p in COMMITTED])
def test_committed_conversion_does_not_regress(pdf: Path, tmp_path: Path):
    _check(pdf, json.loads(pdf.with_suffix(".quality.json").read_text()), tmp_path)
