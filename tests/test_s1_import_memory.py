"""Import resource evidence must name the worker rather than the observing harness."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "e2e_s1_summary_journey.py"


@pytest.mark.skipif(not Path("/proc/self/smaps_rollup").exists(), reason="requires Linux process memory accounting")
@pytest.mark.timeout(20)
def test_import_memory_reads_the_owned_worker_and_rejects_another_launch(tmp_path):
    """A child supplies an independent peak witness; the parent must select that child's counters."""
    spec = importlib.util.spec_from_file_location("s1_import_memory_under_test", SCRIPT)
    journey = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(journey)
    observation = tmp_path / "worker.json"
    launch = "owned-import-worker"
    child = """
import importlib.util, json, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("s1_memory_child", sys.argv[1])
journey = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journey)
journey.record_process_observation(Path(sys.argv[2]), sys.argv[3])
# Leave a stable historic peak above current RSS before the independent witness.
allocation = bytearray(32 * 1024 * 1024)
del allocation
# Linux proc fixes the VmHWM field and its KiB unit; use the same counter as the parent.
peak = next(line for line in Path("/proc/self/status").read_text().splitlines() if line.startswith("VmHWM:"))
print(json.dumps({"pid": os.getpid(), "peak_kib": int(peak.split()[1])}), flush=True)
sys.stdin.readline()
"""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("EXOMEM_", "KB_MCP_", "PYTHONPATH"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", child, str(SCRIPT), str(observation), launch],
        cwd=tmp_path, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        witness = json.loads(process.stdout.readline())
        assert witness["pid"] == process.pid
        service = journey.Session(None, 5, process_observation=(observation, launch))
        baseline = service.memory_snapshot(startup=True)
        measured = journey.memory_window(service, baseline, scope="owned worker", progress={})
        assert measured["status"] == "observed"
        assert measured["final"]["pid"] == process.pid
        assert measured["final"]["peak_rss_bytes"] == witness["peak_kib"] * 1024
        assert measured["additional_reported_mib"] > 0
        unrelated = journey.Session(None, 5, process_observation=(observation, "another-launch"))
        assert unrelated.memory_snapshot()["status"] == "unavailable"
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)
