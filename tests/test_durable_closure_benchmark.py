"""Acceptance invariants for the durable-closure benchmark harness."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "durable_closure_benchmark.py"
spec = importlib.util.spec_from_file_location("durable_closure_benchmark", MODULE_PATH)
benchmark = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = benchmark
spec.loader.exec_module(benchmark)


def test_ordinary_recall_refusal_fails_useful_closure_and_keeps_its_window() -> None:
    """A write ACK cannot mask an intervening public recall refusal."""
    report = benchmark.evaluate_useful_closure(
        calls=[
            {"tool": "remember", "outcome": "ok", "started_ms": 0, "ended_ms": 5},
            {
                "tool": "ask_memory",
                "outcome": "refused",
                "error_code": "RETRIEVAL_INDEX_WARMING",
                "started_ms": 6,
                "ended_ms": 8,
            },
            {"tool": "read_memory", "outcome": "ok", "started_ms": 9, "ended_ms": 12},
        ],
        final_read_your_write=True,
        graph_warming_components=(),
    )

    assert report["passed"] is False
    assert report["refusal_observations"] == [
        {
            "tool": "ask_memory",
            "code": "RETRIEVAL_INDEX_WARMING",
            "window_ms": [6.0, 8.0],
        }
    ]


def test_successful_recall_with_graph_warming_passes_and_keeps_lag_separate() -> None:
    report = benchmark.evaluate_useful_closure(
        calls=[
            {"tool": "remember", "outcome": "ok", "started_ms": 0, "ended_ms": 5},
            {"tool": "ask_memory", "outcome": "ok", "started_ms": 6, "ended_ms": 8},
        ],
        final_read_your_write=True,
        graph_warming_components=("graph",),
    )

    assert report == {
        "passed": True,
        "final_read_your_write": True,
        "refusal_observations": [],
        "warming_components": ["graph"],
    }


def test_overlapping_ledger_calls_keep_sum_and_union_distinct() -> None:
    timings = benchmark.summarize_ledger_calls(
        [
            {"tool": "remember", "duration_ms": 30, "total_ms": 40, "started_ms": 0, "ended_ms": 40},
            {"tool": "ask_memory", "duration_ms": 20, "total_ms": 30, "started_ms": 10, "ended_ms": 40},
        ]
    )

    assert timings["server_duration_sum_ms"] == 50.0
    assert timings["server_total_sum_ms"] == 70.0
    assert timings["ledger_observation_span_ms"] == 40.0
    assert timings["server_occupied_union_ms"] == 40.0
    assert timings["server_idle_within_observed_span_ms"] == 0.0


def test_ledger_durations_join_to_each_client_call_without_inventing_intervals() -> None:
    calls = [
        {"tool": "remember", "client_elapsed_ms": 8.0},
        {"tool": "ask_memory", "client_elapsed_ms": 3.0},
    ]
    joined = benchmark.attach_ledger_measurements(
        calls,
        [
            {"tool": "remember", "duration_ms": 5.0, "total_ms": 7.0, "ts_utc": "2026-09-05T12:00:01.000+00:00"},
            {"tool": "ask_memory", "duration_ms": 2.0, "total_ms": 2.5, "ts_utc": "2026-09-05T12:00:02.000+00:00"},
        ],
    )

    assert joined[0]["server_duration_ms"] == 5.0
    assert joined[1]["server_total_ms"] == 2.5
    assert joined[0]["server_interval"] == {"started_ms": 1788609600993.0, "ended_ms": 1788609601000.0}


def test_ledger_utc_completion_and_total_duration_form_a_measured_interval() -> None:
    rows = benchmark.ledger_intervals(
        [{"ts_utc": "2026-09-05T12:00:01.000+00:00", "total_ms": 250.0}]
    )

    assert rows == [{"started_ms": 1788609600750.0, "ended_ms": 1788609601000.0}]


def test_malformed_ledger_row_invalidates_the_measurement(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "ledger.jsonl").write_text("not json\n", encoding="utf-8")

    try:
        benchmark._read_ledger(ledger, workflow_started=0.0)
    except ValueError as error:
        assert "malformed" in str(error)
    else:
        raise AssertionError("malformed ledger rows must not be silently skipped")


def test_workflow_wall_excludes_transport_shutdown_and_postprocessing() -> None:
    timing = benchmark.lifecycle_timings(workflow_started=10.0, closure_finished=30.0, shutdown_finished=35.0)

    assert timing == {"workflow_wall_ms": 20_000.0, "server_shutdown_ms": 5_000.0}


def test_ack_accounting_excludes_validation_and_status_previews() -> None:
    assert benchmark.is_mutation_ack("remember", {"validate_only": True}) is False
    assert benchmark.is_mutation_ack("observe_memory", {"operation": "validate"}) is False
    assert benchmark.is_mutation_ack("process_media", {"operation": "status"}) is False
    assert benchmark.is_mutation_ack("edit_memory", {"operation": {"kind": "replace_string"}}) is True


def test_missing_required_public_tool_is_a_refusal_not_a_reduced_workflow() -> None:
    missing = benchmark.required_tools_missing({"remember", "ask_memory"})

    assert missing == ["preserve_artifacts", "process_media", "read_memory"]


def test_stress_variant_places_a_public_probe_after_each_mutating_step() -> None:
    optimized = benchmark.workflow_plan("optimized")
    stress = benchmark.workflow_plan("stress")

    assert optimized != stress
    mutation_steps = [step for step in stress if step["mutates"]]
    for mutation in mutation_steps:
        position = stress.index(mutation)
        assert stress[position + 1] == {
            "name": f"probe-after-{mutation['name']}",
            "tool": "ask_memory",
            "mutates": False,
            "probe": True,
        }


def test_artifact_manifest_requires_three_https_handles_and_expected_hashes(tmp_path: Path) -> None:
    manifest = tmp_path / "artifacts.json"
    manifest.write_text(
        '{"artifacts":[{"file_id":"pdf","download_url":"https://example.test/a.pdf",'
        '"mime_type":"application/pdf","file_name":"a.pdf","sha256":"'
        + "a" * 64
        + '"},{"file_id":"image-a","download_url":"https://example.test/a.png",'
        '"mime_type":"image/png","file_name":"a.png","sha256":"'
        + "b" * 64
        + '"},{"file_id":"image-b","download_url":"https://example.test/b.png",'
        '"mime_type":"image/png","file_name":"b.png","sha256":"'
        + "c" * 64
        + '"}]}'
    )

    artifacts = benchmark.load_artifact_manifest(manifest)

    assert [item["file_id"] for item in artifacts] == ["pdf", "image-a", "image-b"]


def test_artifact_receipt_requires_each_expected_hash_before_evidence_can_be_cited() -> None:
    artifacts = [
        {"file_id": "pdf", "sha256": "a" * 64},
        {"file_id": "image-a", "sha256": "b" * 64},
        {"file_id": "image-b", "sha256": "c" * 64},
    ]
    files = [
        {"file_id": "pdf", "outcome": "stored", "hash": "a" * 64, "stored_path": "Knowledge Base/Evidence/a.pdf"},
        {"file_id": "image-a", "outcome": "stored", "hash": "b" * 64, "stored_path": "Knowledge Base/Evidence/a.png"},
        {"file_id": "image-b", "outcome": "stored", "hash": "wrong", "stored_path": "Knowledge Base/Evidence/b.png"},
    ]

    assert benchmark.validated_evidence_paths(artifacts, files) is None


def test_real_extraction_requires_unique_expected_text_and_named_engines() -> None:
    artifacts = [
        {"expected_text": "pdf unique"},
        {"expected_text": "image one"},
        {"expected_text": "image two"},
    ]
    reads = [
        {"frontmatter": {"extracted_by": "pymupdf 1.25"}, "body": "pdf unique"},
        {"frontmatter": {"extracted_by": "tesseract 5.5"}, "body": "image one"},
        {"frontmatter": {"extracted_by": "tesseract 5.5"}, "body": "different OCR text"},
    ]

    assert benchmark.extraction_proof(artifacts, reads) is None


def test_missing_artifact_manifest_blocks_the_full_workflow() -> None:
    assert benchmark.workflow_status(core_passed=True, media_status="blocked") == "blocked"
    assert benchmark.workflow_status(core_passed=False, media_status="ready") == "fail"


def test_corpus_provenance_has_varied_content_links_and_byte_digests(tmp_path: Path) -> None:
    corpus = benchmark.materialize_corpus(tmp_path / "vault", pages=7)

    inventory = corpus["inventory"]
    assert len(inventory) == 7
    assert {entry["bytes"] for entry in inventory}.__len__() > 3
    assert all(len(entry["sha256"]) == 64 for entry in inventory)
    assert len(corpus["corpus_sha256"]) == 64
    assert any("[[Knowledge Base/Notes/Reference/" in (tmp_path / "vault" / entry["path"]).read_text(encoding="utf-8") for entry in inventory)


def test_small_model_free_smoke_uses_one_registered_stdio_product_session(
    tmp_path: Path,
) -> None:
    """The smoke is an integration boundary: registered tools, not leaf fakes."""
    report = benchmark.asyncio.run(
        benchmark.run_public_workflow(
            python=Path(sys.executable),
            state=tmp_path / "state",
            vault=tmp_path / "vault",
            pages=4,
            profile=benchmark.MODEL_FREE_PROFILE,
            timeout=30.0,
        )
    )

    assert report["status"] in {"blocked", "fail"}, report
    assert report["public_call_count"] >= 5
    assert {call["tool"] for call in report["calls"]} >= {
        "bootstrap",
        "remember",
        "ask_memory",
        "read_memory",
    }
    assert report["media"]["status"] == "blocked"
    if report["status"] == "blocked":
        assert report["verification"] == {
            "direct_marker": True,
            "tracker_marker": True,
            "recall_path": True,
            "stale_relation_absent": True,
            "mutations_succeeded": True,
        }
    else:
        assert report["useful_closure"]["refusal_observations"]
    ordinary_recall = next(call for call in report["calls"] if call["tool"] == "ask_memory")
    assert ordinary_recall["request_shape"] == {"mode": "hybrid", "graph": True, "rerank": False}


def test_stress_smoke_places_a_timed_public_recall_after_every_real_mutation(tmp_path: Path) -> None:
    report = benchmark.asyncio.run(
        benchmark.run_public_workflow(
            python=Path(sys.executable),
            state=tmp_path / "state",
            vault=tmp_path / "vault",
            pages=4,
            profile=benchmark.MODEL_FREE_PROFILE,
            variant="stress",
            timeout=30.0,
        )
    )

    for index, call in enumerate(report["calls"]):
        if call["mutation_ack"]:
            assert report["calls"][index + 1]["tool"] == "ask_memory"
