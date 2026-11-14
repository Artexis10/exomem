"""Prevent queued callers from qualifying as concurrent worker execution."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("passage_evidence_latency", SCRIPTS / "passage_evidence_latency.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


@pytest.mark.parametrize("overlapping", [True, False])
def test_four_terminals_require_four_worker_starts_before_the_first_end(overlapping):
    events = []
    if overlapping:
        events = [("start", number) for number in range(4)] + [("success", number) for number in range(4)]
    else:
        events = [(event, number) for number in range(4) for event in ("start", "success")]
    lines = "\n".join(json.dumps({"ts": str(index), "message":
                                f"event=hosted_call kind=tool_{event} tool=ask_memory request_id={number}"})
                      for index, (event, number) in enumerate(events))

    result = driver.dispatch_overlap(lines, 4)

    assert result["starts"] == result["ends"] == 4
    assert result["overlap_verified"] is overlapping
    assert result["peak_dispatched"] == (4 if overlapping else 1)
