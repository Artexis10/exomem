"""Acceptance invariants for the durable-closure benchmark harness."""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import json
import os
import subprocess
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


def test_recall_gate_does_not_accept_a_diagnostic_path_without_a_real_hit() -> None:
    recall = {
        "hits": [],
        "diagnostics": {"pending_overlay": {"path": "Knowledge Base/Notes/Insights/new-note.md"}},
    }

    assert benchmark.recall_has_exact_hit(
        recall, "Knowledge Base/Notes/Insights/new-note.md", "unique marker"
    ) is False


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
        {"tool": "remember", "outcome": "ok", "error_code": None, "client_elapsed_ms": 8.0},
        {"tool": "ask_memory", "outcome": "ok", "error_code": None, "client_elapsed_ms": 3.0},
    ]
    joined = benchmark.attach_ledger_measurements(
        calls,
        [
            {"tool": "remember", "outcome": "ok", "error_code": None, "duration_ms": 5.0, "total_ms": 7.0, "ts_utc": "2026-09-05T12:00:01.000+00:00"},
            {"tool": "ask_memory", "outcome": "ok", "error_code": None, "duration_ms": 2.0, "total_ms": 2.5, "ts_utc": "2026-09-05T12:00:02.000+00:00"},
        ],
    )

    assert joined[0]["server_duration_ms"] == 5.0
    assert joined[1]["server_total_ms"] == 2.5
    assert joined[0]["server_interval"] == {"started_ms": 1788609600993.0, "ended_ms": 1788609601000.0}


def test_ledger_refusal_cannot_be_joined_to_an_ok_client_call() -> None:
    calls = [{"tool": "ask_memory", "outcome": "ok", "client_elapsed_ms": 1.0, "ended_ms": 1.0}]
    rows = [{"tool": "ask_memory", "outcome": "refused", "error_code": "RETRIEVAL_INDEX_WARMING", "duration_ms": 1, "total_ms": 1, "ts_utc": "2026-09-05T12:00:01.000+00:00"}]

    try:
        benchmark.attach_ledger_measurements(calls, rows)
    except ValueError as error:
        assert "outcome" in str(error)
    else:
        raise AssertionError("ledger refusal must invalidate an incompatible client join")


def test_null_ledger_outcome_cannot_hide_a_client_refusal() -> None:
    try:
        benchmark.attach_ledger_measurements(
            [{"tool": "ask_memory", "outcome": "refused", "error_code": "RETRIEVAL_INDEX_WARMING", "client_elapsed_ms": 1}],
            [{"tool": "ask_memory", "outcome": None, "error_code": None, "duration_ms": 1, "total_ms": 1, "ts_utc": "2026-09-05T12:00:01+00:00"}],
        )
    except ValueError as error:
        assert "outcome" in str(error)
    else:
        raise AssertionError("null ledger terminal fields must not hide a refusal")


def test_plaintext_mcp_tool_error_is_not_fabricated_as_a_refusal() -> None:
    class Text:
        text = "ValueError: STALE_SEMANTIC_WRITE"

    class Result:
        isError = True
        content = (Text(),)

    class Client:
        async def call_tool_mcp(self, tool: str, arguments: dict[str, object]) -> Result:
            return Result()

    calls = [{"_origin": benchmark.time.perf_counter()}]
    payload = asyncio.run(benchmark._call(Client(), calls, "remember", {}))

    assert payload == {"_wire_status": "tool_error"}
    assert calls[1]["outcome"] == "tool_error"
    assert calls[1]["error_code"] is None
    assert "STALE_SEMANTIC_WRITE" not in str(calls[1])


def test_tool_error_joins_the_ledger_error_without_inventing_a_public_code() -> None:
    calls = [{"tool": "remember", "outcome": "tool_error", "error_code": None, "client_elapsed_ms": 1.0}]
    rows = [{"tool": "remember", "outcome": "error", "error_code": "ToolError", "duration_ms": 1, "total_ms": 1, "ts_utc": "2026-09-05T12:00:01+00:00"}]

    joined = benchmark.attach_ledger_measurements(calls, rows)

    assert joined[0]["outcome"] == "tool_error"
    assert joined[0]["error_code"] is None


def test_typed_warming_refusal_still_requires_the_same_ledger_error_code() -> None:
    class Text:
        text = '{"success": false, "error": {"code": "RETRIEVAL_INDEX_WARMING"}}'

    class Result:
        isError = True
        content = (Text(),)

    payload = benchmark._decode_call(Result())
    outcome, code = benchmark._result_outcome(payload)

    assert (outcome, code) == ("refused", "RETRIEVAL_INDEX_WARMING")
    try:
        benchmark.attach_ledger_measurements(
            [{"tool": "ask_memory", "outcome": outcome, "error_code": code, "client_elapsed_ms": 1.0}],
            [{"tool": "ask_memory", "outcome": "refused", "error_code": "ToolError", "duration_ms": 1, "total_ms": 1, "ts_utc": "2026-09-05T12:00:01+00:00"}],
        )
    except ValueError as error:
        assert "error code" in str(error)
    else:
        raise AssertionError("a typed warming refusal must not be laundered through a generic ledger error")


def test_mcp_error_flag_overrides_parseable_nonterminal_payloads() -> None:
    class Text:
        text = '{"message": "STALE_SEMANTIC_WRITE"}'

    class StructuredResult:
        is_error = True
        structured_content = {"message": "STALE_SEMANTIC_WRITE"}

    class JsonResult:
        isError = True
        content = (Text(),)

    for result in (StructuredResult(), JsonResult()):
        assert benchmark._result_outcome(benchmark._decode_call(result)) == ("tool_error", None)


def test_invalid_measurement_report_retains_public_and_ledger_counts() -> None:
    report = benchmark.invalid_measurement_report(
        calls=[{"tool": "remember", "outcome": "tool_error"}],
        ledger_row_count=1,
        reason="ledger outcome does not match client outcome",
    )

    assert report["status"] == "invalid"
    assert report["public_call_count"] == 1
    assert report["ledger"]["row_count"] == 1
    assert report["ledger"]["reason"] == "ledger outcome does not match client outcome"


def test_missing_ledger_duration_and_outcome_are_not_measured_as_zero_or_ok() -> None:
    try:
        benchmark.summarize_ledger_calls([{"tool": "remember", "total_ms": 2, "started_ms": 0, "ended_ms": 2}])
    except ValueError as error:
        assert "duration" in str(error)
    else:
        raise AssertionError("missing duration must invalidate ledger summary")

    try:
        benchmark.attach_ledger_measurements(
            [{"tool": "remember", "outcome": "ok", "client_elapsed_ms": 1}],
            [{"tool": "remember", "duration_ms": 1, "total_ms": 1, "ts_utc": "2026-09-05T12:00:01+00:00"}],
        )
    except ValueError as error:
        assert "outcome" in str(error)
    else:
        raise AssertionError("missing ledger outcome must invalidate a client join")


def test_ledger_utc_completion_and_total_duration_form_a_measured_interval() -> None:
    rows = benchmark.ledger_intervals(
        [{"ts_utc": "2026-09-05T12:00:01.000+00:00", "total_ms": 250.0}]
    )

    assert rows == [{"started_ms": 1788609600750.0, "ended_ms": 1788609601000.0}]


def test_utc_ledger_step_invalidates_cross_call_occupancy() -> None:
    calls = [{"ended_ms": 100.0}, {"ended_ms": 200.0}]
    rows = [
        {"ts_utc": "2026-09-05T12:00:01.000+00:00", "total_ms": 5.0},
        {"ts_utc": "2026-09-05T12:00:02.500+00:00", "total_ms": 5.0},
    ]

    assert benchmark.ledger_clock_continuous(calls, rows) is False


def test_cumulative_utc_offset_drift_invalidates_cross_call_occupancy() -> None:
    calls = [{"ended_ms": float(index * 1000)} for index in range(20)]
    base = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
    rows = [
        {"ts_utc": (base + dt.timedelta(milliseconds=index * 1100)).isoformat(), "total_ms": 5.0}
        for index in range(20)
    ]

    assert benchmark.ledger_clock_continuous(calls, rows) is False


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


def test_instrumentation_error_invalidates_benchmark_measurement(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "instrumentation.json").write_text(
        '{"graph_drain_attempts":0,"graph_drain_completed":0,"graph_rebuild_attempts":0,'
        '"graph_rebuild_completed":0,"graph_incremental_execution_elapsed_ms":0,'
        '"graph_rebuild_execution_elapsed_ms":0,"source_scan_pages":0,"source_scan_bytes":0,'
        '"graph_topology_paths_enumerated":0,"graph_topology_stat_estimated_bytes":0,'
        '"graph_topology_actual_body_read_bytes":0,'
        '"wrapper_status":"installed","instrumentation_error":"ImportError"}',
        encoding="utf-8",
    )

    try:
        benchmark.read_instrumentation(state)
    except RuntimeError as error:
        assert "error" in str(error)
    else:
        raise AssertionError("instrumentation errors must not be reported as measured zeroes")


def test_scan_walker_does_not_publish_or_poll_control_per_page(tmp_path: Path) -> None:
    hook = benchmark.install_subprocess_instrumentation(tmp_path / "state")
    source = (hook / "sitecustomize.py").read_text(encoding="utf-8")
    walker = source.split("    def walk(root):", 1)[1].split("    find._walk_md = walk", 1)[0]

    assert "command()" not in walker
    assert "publish()" not in walker


def test_child_hook_measures_appeared_target_topology_reads_and_graph_execution_time(tmp_path: Path) -> None:
    state = tmp_path / "state"
    hook = benchmark.install_subprocess_instrumentation(state)
    package = tmp_path / "exomem"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "index_sync.py").write_text("", encoding="utf-8")
    (package / "find.py").write_text("def _walk_md(root): return ()\n", encoding="utf-8")
    (package / "vault.py").write_text(
        "from pathlib import Path\n"
        "def walk_vault_md(root): yield Path(root) / 'appeared.md'\n"
        "def read_bytes_without_pinning(path): return path.read_bytes()\n",
        encoding="utf-8",
    )
    (package / "epistemic_graph.py").write_text(
        "import time\nfrom . import vault\n"
        "class EpistemicGraphIndex:\n"
        " def drain_paths(self, paths): time.sleep(0.01); return {}\n"
        " def _rebuild_all_off_boundary(self): time.sleep(0.01)\n"
        " def _sources_linking_to(self, targets, *, resolver=None):\n"
        "  return {str(path) for path in vault.walk_vault_md('.') if vault.read_bytes_without_pinning(path)}\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(hook), str(tmp_path))),
        "DURABLE_CLOSURE_INSTRUMENTATION": str(state / "instrumentation.json"),
    }
    code = (
        "from pathlib import Path; Path('appeared.md').write_bytes(b'appeared-body'); "
        "from exomem.epistemic_graph import EpistemicGraphIndex as I; "
        "index = I(); index._sources_linking_to({'appeared.md'}); index.drain_paths([]); index._rebuild_all_off_boundary()"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=environment, check=True, capture_output=True, text=True
    )

    assert completed.stderr == ""
    measured = json.loads((state / "instrumentation.json").read_text(encoding="utf-8"))
    assert measured["graph_topology_paths_enumerated"] == 1
    assert measured["graph_topology_actual_body_read_bytes"] == len(b"appeared-body")
    assert measured["graph_incremental_execution_elapsed_ms"] > 0
    assert measured["graph_rebuild_execution_elapsed_ms"] > 0


def test_missing_required_instrumentation_hook_is_not_silently_counted_as_zero(tmp_path: Path) -> None:
    hook = benchmark.install_subprocess_instrumentation(tmp_path / "state")
    source = (hook / "sitecustomize.py").read_text(encoding="utf-8")

    assert 'raise AttributeError(f"missing required instrumentation hook: {name}")' in source


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


def test_workflow_plan_delays_only_the_evidence_backed_note_until_media_settles() -> None:
    names = [step["name"] for step in benchmark.workflow_plan("optimized")]

    enqueue_end = names.index("process-media")
    independent_end = names.index("repair-stale-relation")
    media_proof = names.index("media-completion-proof")
    source_note = names.index("remember-evidence-backed-note")
    final_verification = names.index("ordinary-recall")

    assert enqueue_end < names.index("observe-tracker") <= independent_end
    assert enqueue_end < names.index("observe-critique") <= independent_end
    assert enqueue_end < names.index("read-archived") <= independent_end
    assert independent_end < media_proof < source_note < final_verification


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


def test_process_media_uses_one_selected_batch_only_when_registered_schema_exposes_paths() -> None:
    paths = ["Knowledge Base/Evidence/a.pdf", "Knowledge Base/Evidence/a.png", "Knowledge Base/Evidence/b.png"]
    batch_tool = {"inputSchema": {"properties": {"path": {}, "paths": {"type": "array"}}}}
    legacy_tool = {"inputSchema": {"properties": {"path": {}}}}

    assert benchmark.process_media_requests(batch_tool, paths) == [
        {"operation": "process", "paths": paths}
    ]
    assert benchmark.process_media_requests(legacy_tool, paths) == [
        {"operation": "process", "path": path} for path in paths
    ]


def test_projected_media_results_and_legacy_single_result_feed_extraction_reads() -> None:
    batch = {
        "media_results": [
            {"path": "a.pdf", "outcome": "processed", "state": "pending", "sidecar_path": "a.pdf.md"},
            {"path": "a.png", "outcome": "processed", "state": "pending", "sidecar_path": "a.png.md"},
        ]
    }
    legacy = {"path": "b.png", "state": "pending", "sidecar_path": "b.png.md"}

    assert benchmark.media_result_rows(batch) == batch["media_results"]
    assert benchmark.media_result_rows(legacy) == [{**legacy, "outcome": "processed"}]


def test_media_projection_must_match_requested_paths_once_and_in_order() -> None:
    paths = ["a.pdf", "a.png", "b.png"]
    duplicate_rows = [{"path": "a.pdf", "outcome": "processed", "sidecar_path": "a.md", "job_id": 1}] * 3

    assert benchmark.media_rows_match_request(paths, duplicate_rows) is False


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


def test_failed_dependency_gate_does_not_run_the_dependent_write() -> None:
    calls = 0

    async def dependent_write() -> None:
        nonlocal calls
        calls += 1

    gate = asyncio.run(
        benchmark.run_after_dependencies(
            {"real-extraction-proof": False},
            dependent_write,
        )
    )

    assert calls == 0
    assert gate == {"ready": False, "blocked_dependencies": ["real-extraction-proof"]}


def test_missing_manifest_dependency_skips_all_source_closure_calls() -> None:
    source_calls = 0

    async def source_closure() -> None:
        nonlocal source_calls
        source_calls += 1

    gate = asyncio.run(
        benchmark.run_after_dependencies(
            benchmark.source_closure_dependencies(artifacts_present=False),
            source_closure,
        )
    )

    assert source_calls == 0
    assert gate == {"ready": False, "blocked_dependencies": ["artifact-manifest"]}


def test_unproven_source_closure_is_blocked_only_when_independent_work_and_recalls_hold() -> None:
    common = {
        "core_passed": False,
        "media_status": "blocked",
        "blocked_dependencies": ["real-extraction-proof"],
    }

    assert benchmark.workflow_status(
        **common, independent_work_succeeded=True, ordinary_retrieval_refused=False
    ) == "blocked"
    assert benchmark.workflow_status(
        **common, independent_work_succeeded=False, ordinary_retrieval_refused=False
    ) == "fail"
    assert benchmark.workflow_status(
        **common, independent_work_succeeded=True, ordinary_retrieval_refused=True
    ) == "fail"


def test_read_tool_error_is_an_ordinary_retrieval_failure_not_a_blocked_mask() -> None:
    calls = [{"tool": "read_memory", "outcome": "tool_error"}]

    assert benchmark.has_ordinary_retrieval_failure(calls) is True
    assert benchmark.workflow_status(
        core_passed=False,
        media_status="blocked",
        blocked_dependencies=["real-extraction-proof"],
        independent_work_succeeded=True,
        ordinary_retrieval_refused=benchmark.has_ordinary_retrieval_failure(calls),
    ) == "fail"


def test_blocked_cli_result_is_a_nonzero_incomplete_exit(tmp_path: Path, monkeypatch: object) -> None:
    def fake_run(coroutine: object) -> dict[str, str]:
        coroutine.close()  # type: ignore[attr-defined]
        return {"status": "blocked"}

    monkeypatch.setattr(benchmark.asyncio, "run", fake_run)  # type: ignore[attr-defined]

    assert benchmark.main(["--state", str(tmp_path / "state"), "--vault", str(tmp_path / "vault")]) == 2


def test_corpus_provenance_has_varied_content_links_and_byte_digests(tmp_path: Path) -> None:
    corpus = benchmark.materialize_corpus(tmp_path / "vault", pages=7)

    inventory = corpus["inventory"]
    assert len(inventory) == 7
    assert {entry["bytes"] for entry in inventory}.__len__() > 3
    assert all(len(entry["sha256"]) == 64 for entry in inventory)
    assert len(corpus["corpus_sha256"]) == 64
    assert any("[[Knowledge Base/Notes/Reference/" in (tmp_path / "vault" / entry["path"]).read_text(encoding="utf-8") for entry in inventory)


def test_server_root_selects_actual_subprocess_package_and_changes_source_identity(tmp_path: Path) -> None:
    roots: list[Path] = []
    for name, marker in (("baseline", "baseline-source"), ("candidate", "candidate-source")):
        root = tmp_path / name
        package = root / "src" / "exomem"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
        roots.append(root)

    baseline = benchmark.runtime_provenance(roots[0], Path(sys.executable))
    candidate = benchmark.runtime_provenance(roots[1], Path(sys.executable))

    assert baseline["source"]["root"] == str((roots[0] / "src").resolve())
    assert candidate["source"]["root"] == str((roots[1] / "src").resolve())
    assert baseline["source"]["package_origin"] == str((roots[0] / "src" / "exomem" / "__init__.py").resolve())
    assert candidate["source"]["package_origin"] == str((roots[1] / "src" / "exomem" / "__init__.py").resolve())
    assert baseline["source"]["sha256"] != candidate["source"]["sha256"]
    assert baseline["python"]["executable"] == str(Path(sys.executable).resolve())
    assert isinstance(baseline["python"]["packages"], list)


def test_server_root_default_remains_current_tree_and_invalid_root_fails(tmp_path: Path) -> None:
    environment = benchmark.benchmark_environment(tmp_path / "state", tmp_path / "vault")

    assert benchmark.validate_server_root(benchmark.ROOT) == benchmark.ROOT.resolve()
    assert environment["PYTHONPATH"] == str(benchmark.ROOT / "src")
    try:
        benchmark.validate_server_root(tmp_path / "not-a-server")
    except ValueError as error:
        assert "src/exomem" in str(error)
    else:
        raise AssertionError("a runner root without src/exomem must fail before benchmark setup")


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
        "ask_memory",
        "read_memory",
    }
    assert all(call["phase"] != "source-closure" for call in report["calls"])
    assert report["media"]["status"] == "blocked"
    if report["status"] == "blocked":
        assert report["blocked_dependencies"] == ["artifact-manifest"]
        assert report["useful_closure"]["final_read_your_write"] is False
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
