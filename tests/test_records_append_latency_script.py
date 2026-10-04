"""The diagnostic must exercise real store commands without claiming view acceptance."""

import json
import subprocess
import sys
from pathlib import Path


def test_store_diagnostic_uses_scoped_lease_and_full_public_inspect():
    script = Path(__file__).resolve().parents[1] / "scripts/measure-records-append-latency.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--child", "--storage", "store-preview",
         "--size", "3", "--appends", "2"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    line = next(row for row in completed.stdout.splitlines() if row.startswith("RESULT-JSON:"))
    result = json.loads(line.removeprefix("RESULT-JSON:"))
    assert result["storage_mode"] == "store-preview"
    assert result["phase_acceptance"] is False
    assert result["command_path"]["lease"] == "vault-scoped"
    assert result["command_path"]["inspect"] == "full public"
    assert result["command_path"]["projection"] == "synchronous item-view protocol"
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
    inspection = result["final_inspect"]
    assert inspection["contract"]["collection_id"] == "6a7b8c9d-1111-4222-8333-444455556666"
    assert inspection["coverage"]["committed"] == 9
    # Counts alone can survive filtering of an unresolved caller: require the
    # public contract and every source version in this small, authorised fixture.
    versions = inspection["source_versions"]
    assert len(versions) == 10
    assert len({version["path"] for version in versions}) == 10
    assert all(isinstance(version["hash"], str) and version["hash"] for version in versions)
    assert inspection["observed_values"]["status"]["values"] == [
        {"value": "logged", "count": 9, "value_truncated": False},
    ]
