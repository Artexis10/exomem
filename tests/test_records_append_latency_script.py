"""The diagnostic must exercise real store commands without claiming view acceptance."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_store_diagnostic_uses_scoped_lease_and_full_public_inspect(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/measure-records-append-latency.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--child", "--work-dir", str(tmp_path), "--storage", "store",
         "--size", "3", "--appends", "2"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    line = next(row for row in completed.stdout.splitlines() if row.startswith("RESULT-JSON:"))
    result = json.loads(line.removeprefix("RESULT-JSON:"))
    assert result["storage_mode"] == "store"
    assert result["phase_acceptance"] is False
    assert result["command_path"]["lease"] == "vault-scoped"
    assert result["command_path"]["inspect"] == "full public"
    assert result["command_path"]["projection"] == "deferred item-view publication"
    assert result["command_path"]["acknowledgement"] == "durable SQLite commit"
    # Elapsed and CPU values must cover the same actual public calls, rather
    # than subtracting unrelated percentile distributions to invent a wait budget.
    assert result["timing_clocks"]["elapsed"] == "perf_counter"
    assert result["timing_clocks"]["cpu"] == "thread_time"
    assert result["timing_clocks"]["cpu_scope"] == "invoking thread"
    assert result["timing_clocks"]["profile"] == "thread_time"
    pairs = result["guarded_operation_samples_ms"]
    assert len(pairs) == 2
    for pair in pairs:
        for operation in ("inspect", "append"):
            sample = pair[operation]
            assert sample["elapsed"] > 0 and sample["thread_cpu"] > 0
            assert abs(sample["elapsed"] - sample["thread_cpu"] - sample["non_cpu_elapsed"]) < 0.003
    assert "Ordered by: internal time" in result["profile_refresh_tottime"]
    # The pending view hash rides the main transaction: a second store commit would add an fsync.
    trace = result["run_metadata"]["commit_fsync_count"]
    assert trace["store_commit"] == {"invoking_thread": 1, "other_threads": 0}
    inspection = result["final_inspect"]
    assert inspection["contract"]["collection_id"] == "6a7b8c9d-1111-4222-8333-444455556666"
    assert inspection["coverage"]["committed"] == 10
    assert inspection["projection"]["pending_views"] > 0
    # Counts alone can survive filtering of an unresolved caller: require the
    # public contract and every source version in this small, authorised fixture.
    versions = inspection["source_versions"]
    assert len(versions) == 11
    assert len({version["path"] for version in versions}) == 11
    assert all(isinstance(version["hash"], str) and version["hash"] for version in versions)
    assert inspection["observed_values"]["status"]["values"] == [
        {"value": "logged", "count": 10, "value_truncated": False},
    ]


@pytest.mark.parametrize("env_prefix", ["EXOMEM_", "KB_MCP_"])
def test_store_diagnostic_cannot_use_an_ambient_coordinator(monkeypatch, env_prefix, tmp_path):
    """A developer's lease configuration must not send the invented vault to a live coordinator."""
    monkeypatch.setenv(env_prefix + "WRITER_LEASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv(env_prefix + "WRITER_LEASE_TOPOLOGY", "single-host")
    monkeypatch.setenv(env_prefix + "WRITER_LEASE_VAULT_ID", "ambient-vault")
    monkeypatch.setenv(env_prefix + "WRITER_LEASE_REPLICA_ID", "ambient-replica")
    monkeypatch.setenv(env_prefix + "WRITER_LEASE_TOKEN", "invented-token")
    script = Path(__file__).resolve().parents[1] / "scripts/measure-records-append-latency.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--child", "--work-dir", str(tmp_path), "--storage", "store",
         "--size", "1", "--appends", "1"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    line = next(row for row in completed.stdout.splitlines() if row.startswith("RESULT-JSON:"))
    result = json.loads(line.removeprefix("RESULT-JSON:"))
    assert result["final_inspect"]["coverage"]["committed"] == 6
    assert result["guarded_append_wall_ms"]["p95"] > 0


def test_file_diagnostic_accounts_work_and_removes_temporary_state(tmp_path):
    """File commands must report reached stages, and the parent deletes the child's state after it exits."""
    script = Path(__file__).resolve().parents[1] / "scripts/measure-records-append-latency.py"
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    report = tmp_path / "report.json"
    environment = {**os.environ, "TMPDIR": str(temporary)}
    completed = subprocess.run(
        [sys.executable, str(script), "--sizes", "1", "--storage", "files",
         "--appends", "1", "--out", str(report)],
        env=environment, capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    results = json.loads(report.read_text(encoding="utf-8"))
    assert len(results) == 1
    result = results[0]
    assert result["final_inspect"]["coverage"]["committed"] == 5
    assert result["phase_acceptance"] is False
    assert result["stage_calls_last_append"]["collection.resolve"] > 0
    assert result["stage_calls_last_append"]["authz.visibility"] > 0
    assert result["stage_calls_last_append"]["authz.precommit"] > 0
    for name in ("resolution", "governance"):
        assert result["guarded_exclusive_budgets"][name]["status"] == "reached"
        assert result["guarded_exclusive_budgets"][name]["p95_ms"] > 0
    assert list(temporary.iterdir()) == []


def test_phase_probes_prove_release_refusal_and_bulk_storage(tmp_path):
    """A filtered public read must not turn a partial audience's write into a commit."""
    script = Path(__file__).resolve().parents[1] / "scripts/measure-records-append-latency.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--child", "--work-dir", str(tmp_path), "--storage", "store",
         "--size", "10", "--appends", "2", "--phase-probes"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    line = next(row for row in completed.stdout.splitlines() if row.startswith("RESULT-JSON:"))
    result = json.loads(line.removeprefix("RESULT-JSON:"))
    probes = result["phase_probes"]
    assert probes["fixture"] == {
        "seeded_rows": 10, "selected_refs": 1,
        "complete_inspect_rows": 10, "partial_inspect_rows": 9,
    }
    assert len(probes["complete_append_samples"]) == 2
    assert len(probes["partial_refusal_samples"]) == 2
    assert probes["complete_append_wall_ms"]["p95"] > 0
    assert probes["partial_refusal_wall_ms"]["p95"] > 0
    assert probes["refusal_state_unchanged"] is True
    assert probes["bulk_500"]["stored_count_delta"] == 500
    assert probes["bulk_500"]["elapsed_ms"] > 0
    assert result["final_inspect"]["projection"]["pending_views"] > 0
    assert result["phase_acceptance"] is False
    assert result["run_metadata"]["sqlite_version"]
    assert result["run_metadata"]["script_sha256"]
    samples = result["guarded_budget_samples"] + probes["complete_append_samples"]
    samples += probes["partial_refusal_samples"]
    for sample in samples:
        assert sample["elapsed_ms"] > 0 and sample["thread_cpu_ms"] > 0
        assert abs(sum(sample["exclusive_ms"].values()) - sample["elapsed_ms"]) < 0.00001
        assert abs(sample["reconciliation_error_ms"]) < 0.00001
        assert sample["missing_instrumentation"] == []
        assert sample["stages"]["receipt"]["status"] in {"reached", "not_reached"}
    assert all(sample["error_code"] == "COLLECTION_NOT_FOUND"
               for sample in probes["partial_refusal_samples"])
