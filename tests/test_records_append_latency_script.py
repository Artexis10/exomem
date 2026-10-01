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
    assert result["command_path"]["projection"] == "pending-only"
    assert result["final_inspect"]["coverage"]["committed"] == 9
    assert result["final_inspect"]["observed_values"]["status"]["values"] == [
        {"value": "logged", "count": 9, "value_truncated": False},
    ]
