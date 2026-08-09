"""Exomem adapter end-to-end on T00: leaf CI gate + wire smoke.

Drives the real product through its public boundaries against an isolated
temp vault (EXOMEM_VAULT_PATH is mandatory with no fallback, so a real vault
cannot be touched). Asserts sentinel survivability through capture→retrieve
and full env restoration after cleanup.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from membench.adapters.exomem_local import ExomemLocalAdapter, lexical_profile
from membench.generate import generate_corpus
from membench.native import exomem_kb, load_corpus_view
from membench.runner import RunSpec, execute_run

T00 = "t00_mini_smoke"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("corpus") / "s1"
    generate_corpus(1, root, template_ids=[T00])
    return root


def test_leaf_adapter_direct_lifecycle(corpus: Path, tmp_path: Path) -> None:
    view = load_corpus_view(corpus)
    native_dir = tmp_path / "native"
    exomem_kb.render(view, native_dir)

    adapter = ExomemLocalAdapter(mode="leaf")
    before_vault = os.environ.get("EXOMEM_VAULT_PATH")
    adapter.setup(tmp_path / "provider", lexical_profile())
    try:
        results = adapter.ingest(corpus, native_dir)
        assert results and all(r.ok for r in results), [r.detail for r in results if not r.ok]

        # Harness property: sentinel survivability through capture->retrieve.
        # Probe with a source TITLE (statement-form; every stem present in the
        # stored page) rather than a natural-language question: in the
        # lexical-degraded profile exomem's hybrid retention gate requires
        # all query stems / a literal excerpt / non-lexical corroboration, so
        # NL questions honestly return zero hits — that is a scored product
        # finding (see docs/memory-proof-benchmark.md), not a harness fault.
        probe_source = view.sources[0]
        hits = adapter.search(probe_source.title, 10)
        assert hits, f"title probe {probe_source.title!r} returned no hits"
        all_sentinels = {s for h in hits for s in h.sentinels}
        assert probe_source.source_id in all_sentinels, "sentinel lost through capture"

        export = adapter.export_state()
        assert len(export.pages) >= len(view.sources)
    finally:
        adapter.cleanup()
    assert os.environ.get("EXOMEM_VAULT_PATH") == before_vault


def test_leaf_run_end_to_end_produces_valid_run_dir(corpus: Path, tmp_path: Path) -> None:
    result = execute_run(
        RunSpec(
            corpus_dir=corpus,
            adapter=ExomemLocalAdapter(mode="leaf"),
            profile=lexical_profile(),
            runs_root=tmp_path / "runs",
            top_k=10,
        )
    )
    assert not result.invalid, result.invalid_reason
    scores = json.loads((result.run_dir / "deterministic-scores.json").read_text())
    ok_rows = [r for r in scores["per_query"] if r["status"] == "ok"]
    assert ok_rows, "no queries scored"
    # Harness assertions only: every scored query carries gate items and a
    # retrieval record where applicable. Whether exomem FINDS anything for
    # NL questions in the lexical profile is a scored result, not a harness
    # precondition (currently it does not — the conjunctive retention gate
    # finding; the failing gates below are the honest measurement).
    assert all(row["gates"] for row in ok_rows)
    with_retrieval = [r for r in ok_rows if r.get("retrieval") is not None]
    assert with_retrieval, "no retrieval-applicable queries were measured"
    assert (result.run_dir / "parity.json").exists()
    assert result.dimensions["_run"]["failures"] == 0
    assert "factual_qa" in result.dimensions and "temporal" in result.dimensions


def test_wire_mode_smoke_search(corpus: Path, tmp_path: Path) -> None:
    view = load_corpus_view(corpus)
    native_dir = tmp_path / "native"
    exomem_kb.render(view, native_dir)
    adapter = ExomemLocalAdapter(mode="wire")
    adapter.setup(tmp_path / "provider", lexical_profile())
    try:
        results = adapter.ingest(corpus, native_dir)
        assert all(r.ok for r in results), [r.detail for r in results if not r.ok]
        hits = adapter.search("delivery deadline", 5)
        assert isinstance(hits, list)
        assert hits, "wire-mode search returned nothing"
    finally:
        adapter.cleanup()
