"""Catch omitted queries, retained connections and SQL-only keyword timing."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import recall_latency_gate as gate  # noqa: E402
import recall_sql_calibration as calibration  # noqa: E402


@pytest.fixture
def lexical_reference(tmp_path, monkeypatch):
    """Build real managed state; the unavailable encoder cannot hide a load."""
    import synth_vault

    from exomem import due_state, embeddings, readiness

    root = tmp_path / "vault"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    for setting in ("EXOMEM_DISABLE_EMBEDDINGS", "EXOMEM_DISABLE_CLIP",
                    "EXOMEM_DISABLE_RANKING", "EXOMEM_DISABLE_MEDIA_EXTRACTION"):
        monkeypatch.setenv(setting, "1")

    def unavailable_encoder():
        raise AssertionError("SQL calibration must work without an encoder")

    monkeypatch.setattr(embeddings, "get_model", unavailable_encoder)
    profile = json.loads((gate.BENCHMARK / "profile.json").read_text())
    profile.update(pages=40, vocabulary_min=50)
    monkeypatch.setattr(gate, "REFERENCE", {**gate.REFERENCE, "pages_min": 40, "units_min": 50})
    words, _digest = synth_vault.reference_source(profile, gate.BENCHMARK)
    try:
        environment = gate._prepare_worker(root, {"profile": profile, "calibration": "sql"})
        assert environment["encoder_digest"] is environment["encoder_identity"] is None
        assert environment["corpus"]["chunks"] is environment["corpus"]["vector_units"] is None
        assert environment["corpus"]["lexical_rows"] == {"fts": 40, "tri": 40, "unit_fts": 60}
        assert environment["corpus"]["pages"] == 40
        assert due_state.load(root) is not None
        assert readiness.retrieval_admission(root)["admitted"] is True
        # The generator emits these source words together in its first paragraph.
        yield root, " ".join(words[:18])
    finally:
        readiness.unmanage_runtime()


def test_keyword_row_includes_real_page_resolution_and_hit_construction(lexical_reference):
    from exomem import command_surface, commands

    root, query = lexical_reference
    command = command_surface.resolve_command("ask_memory", commands.PRODUCT_COMMANDS)
    ask = command_surface.bind_vault(command.leaf, root, command=command)
    observer = calibration.SqlObserver(gate._document_digest)
    for principal in ("owner", "admitted"):
        for _repeat in range(2):
            sample = gate._worker_sample(ask, {
                "query": query, "principal": principal,
                "arguments": {"mode": "keyword", "result_level": "page"},
                "rows": gate.REFERENCE["stage_rows"],
            }, sql_observer=observer)
            assert sample["state"] == "ok"
            proof = sample["sql"]
            assert proof["connections"] >= 1 and proof["query_connections"] >= 1
            assert proof["fresh"] is proof["closed"] is proof["queries_bound"] is True
            assert proof["keyword_page_resolutions"] > 0 and proof["keyword_hit_builds"] > 0
            assert proof["keyword_work_inside_row"] is True
            assert sample["sql_rows"]["governed-stage-keyword"] is not None
            assert sample["sql_rows"]["governed-stage-bm25"] is None


def test_retained_catalogue_handle_is_neither_fresh_nor_closed(lexical_reference, monkeypatch):
    from exomem import lexstore
    from exomem.governance import principal

    root, _query = lexical_reference
    store = lexstore.get_store(root)
    capture = lexstore.LexicalStore._capture_read_view
    close = lexstore._CatalogReadView.close
    retained = None

    def retained_view(current):
        nonlocal retained
        if retained is None:
            retained = capture(current)
        return retained

    monkeypatch.setattr(lexstore.LexicalStore, "_capture_read_view", retained_view)
    monkeypatch.setattr(lexstore._CatalogReadView, "close", lambda _view: None)
    observer = calibration.SqlObserver(gate._document_digest)
    proofs = []
    try:
        with principal.library_scope(), lexstore.admitted_catalog_scope(root):
            for _request in range(2):
                with observer.observe():
                    view = store._capture_read_view()
                    store._bm25_query(view.connection, ["reference"], 15, "kb")
                    view.close()
                proofs.append(observer.result([], SimpleNamespace(intervals=[]), projection="hybrid"))
        assert proofs[0]["fresh"] is True and proofs[0]["closed"] is False
        assert proofs[1]["fresh"] is False and proofs[1]["closed"] is False
        assert all(proof["queries_bound"] is True for proof in proofs)
    finally:
        if retained is not None:
            close(retained)


def test_calibration_keeps_every_case_query_filter_and_word_band():
    profile = json.loads((gate.BENCHMARK / "profile.json").read_text())
    cases, _digest = gate.reference_cases(profile)
    requests = list(calibration.projected_requests(gate, cases))
    expected = {(case["id"], principal, projection)
                for case in cases for principal in ("owner", "admitted")
                for projection in ("hybrid", "keyword")}
    assert {(r["id"], r["principal"], r["projection"]) for r in requests} == expected
    assert len(requests) == len(expected)
    originals = {case["id"]: case for case in cases}
    for request in requests:
        original = originals[request["id"]]
        assert request["query"] == original["query"]
        assert request["arguments"] == {**original["arguments"], "mode": request["projection"],
                                        "result_level": "page"}
        assert request["band"] == gate.reference_word_band(original["query"])


def test_busy_reference_writes_a_content_free_refusal(tmp_path, monkeypatch):
    from jsonschema import ValidationError

    monkeypatch.setattr(gate, "one_minute_load", lambda: gate.MAX_LOAD_AVERAGE + 1)
    output = tmp_path / "summary.json"
    assert gate.main(["--sql-calibration", "--quiescence-bound", "0", "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["state"] == "refused" and report["reason"] == "load-above-reference"
    assert report["samples"] == [] and report["environment"] is None
    assert report["expected_samples"] == 960 and report["completed_samples"] == 0
    assert report["coverage_complete"] is False
    report["query"] = "private material"
    with pytest.raises(ValidationError):
        gate.validate_document(report, "sql-calibration.schema.json")


def test_small_population_bands_remain_insufficient_and_unknowns_stay_visible():
    samples = [
        {"principal": "owner", "projection": "keyword", "series": f"population_{population}",
         "band": "15-40", "state": "unknown" if case == 9 else "empty" if case == 0 else "ok",
         "rows": {"governed-stage-keyword": float(case + 1)}}
        for population in range(6) for case in range(10)
    ]
    rows = calibration.summarize_rows(gate, samples)
    populations = [row for row in rows if row["series"] is not None]
    aggregate, = [row for row in rows if row["series"] is None]
    assert len(populations) == 6
    assert all(row["state"] == "insufficient" and row["p95_ms"] is None for row in populations)
    assert aggregate["state"] == "measured" and aggregate["samples"] == 54
    assert aggregate["observations"] == 60 and aggregate["unknown"] == aggregate["empty"] == 6
    assert all(row["twice_budget_applied"] is False for row in rows)


def test_external_sql_worker_options_cannot_silently_change_paired_mode(tmp_path):
    with pytest.raises(SystemExit) as raised:
        gate.main(["--paired", "--sql-worker-python", str(tmp_path / "python")])
    assert raised.value.code == 2
