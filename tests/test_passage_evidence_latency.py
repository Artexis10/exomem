"""Prevent queued callers from qualifying as concurrent worker execution."""

import asyncio
import hashlib
import importlib.util
import json
import sqlite3
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


def test_paired_slots_alternate_modes_and_keep_every_repeat_client_and_query():
    profile = {"modes": ["disk", "resident"], "repeats": 3, "callers": [1, 4]}
    cases = [{"id": "public-1"}, {"id": "public-2"}]

    slots = driver.paired_slots(profile, cases)

    assert len(slots) == 60
    assert list(dict.fromkeys((slot["repeat"], slot["mode"]) for slot in slots)) == [
        (0, "disk"), (0, "resident"), (1, "resident"), (1, "disk"),
        (2, "disk"), (2, "resident"),
    ]
    assert len({driver.slot_key(slot) for slot in slots}) == len(slots)


def test_a_missing_paired_call_stays_visible_and_prevents_a_comparison():
    profile = {"modes": ["disk", "resident"], "repeats": 1, "callers": [1],
               "pairing": {"confidence": 0.95, "resamples": 100, "seed": 7}}
    slots = driver.paired_slots(profile, [{"id": "public-1"}, {"id": "public-2"}])
    samples = [{**slot, "elapsed_ms": 20.0, "failure": None, "terminal_received": True}
               for slot in slots[:-1]]

    retained = driver.retain_slots(slots, samples)
    comparison = driver.paired_comparison(retained, profile)

    assert len(retained) == len(slots)
    assert retained[-1]["outcome"] == "unattempted"
    assert retained[-1]["elapsed_ms"] is None
    assert comparison[0]["state"] == "unavailable"
    assert comparison[0]["cases"][1]["state"] == "incomplete"


def test_paired_interval_clusters_client_slots_and_repeats_by_query():
    profile = {"modes": ["disk", "resident"], "repeats": 3, "callers": [4],
               "pairing": {"confidence": 0.95, "resamples": 1000, "seed": 7}}
    slots = driver.paired_slots(profile, [{"id": "public-1"}, {"id": "public-2"}])
    samples = [{**slot, "elapsed_ms": 20.0 if slot["mode"] == "disk" else 10.0,
                "failure": None, "terminal_received": True} for slot in slots]

    comparison = driver.paired_comparison(samples, profile)[0]

    assert comparison["cases_in_interval"] == 2
    assert comparison["mean_delta_ms"] == -10.0
    assert comparison["lower_ms"] == comparison["upper_ms"] == -10.0
    assert all(row["attempts_per_mode"] == 12 for row in comparison["cases"])


def test_resource_receipt_separates_cumulative_peak_from_segment_samples(tmp_path):
    snapshots = []
    for current, usage in ((10, 100), (20, 110)):
        snapshots.append({"values": {
            "cgroup/memory.current": {"state": "observed", "raw": str(current)},
            "cgroup/memory.peak": {"state": "observed", "raw": "100"},
            "cgroup/cpu.stat": {"state": "observed", "raw": f"usage_usec {usage}\nnr_throttled 0"},
            "cgroup/memory.events": {"state": "observed", "raw": "oom 0"},
            "host/cpu": {"state": "unavailable", "error": "FileNotFoundError"},
        }})
    path = tmp_path / "resources.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in snapshots))

    result = driver.resource_summary(path)

    assert result["segment_sampled_memory_current_max_bytes"] == 20
    assert result["container_cumulative_memory_peak_bytes"] == 100
    assert result["segment_counter_deltas"]["cgroup/cpu.stat"]["usage_usec"] == 10
    assert result["unavailable_reads"] == 2


@pytest.mark.parametrize("log_failure", ["read", "parse"])
def test_completed_terminals_survive_a_failed_native_log_observation(tmp_path, monkeypatch, log_failure):
    rows = [{"client": number, "elapsed_ms": 25.0 + number, "terminal_received": True,
             "outcome": "ok", "failure": None, "response": {"hits": [{"path": "Sources/reference.md"}]}}
            for number in range(4)]
    terminal_path = tmp_path / "terminals.jsonl"
    samples_path = tmp_path / "samples.jsonl"
    log_path = tmp_path / "worker.log"
    if log_failure == "parse":
        log_path.write_text("{broken json\n")
    original_open = Path.open
    rows_present_at_log_read = []

    def observe_open(path, *args, **kwargs):
        if path == log_path:
            with original_open(terminal_path) as retained:
                rows_present_at_log_read.append(len(retained.readlines()))
            if log_failure == "read":
                raise FileNotFoundError("worker log disappeared after the calls completed")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe_open)
    with terminal_path.open("w") as terminals, samples_path.open("w") as output:
        driver.retain_batch(rows, terminals, output, log_path, 0, 4)

    retained = [json.loads(line) for line in samples_path.read_text().splitlines()]
    assert rows_present_at_log_read == [4]
    assert len(retained) == 4
    assert [row["elapsed_ms"] for row in retained] == [25.0, 26.0, 27.0, 28.0]
    assert all(row["terminal_received"] and row["failure"] is None and row["response"]["hits"] for row in retained)
    assert all(row["dispatch"]["observation_state"] == "failed" for row in retained)
    assert all(row["dispatch"]["peak_dispatched"] is None for row in retained)


@pytest.mark.parametrize("enrichment", ["missing", "truncated", "duplicate", "unexpected"])
def test_paired_aggregation_keeps_journaled_calls_after_sample_output_failure(tmp_path, monkeypatch, enrichment):
    root, baseline = tmp_path / "run", tmp_path / "baseline"
    root.mkdir()
    vectors = baseline / "arm/state/index/.embeddings.sqlite"
    vectors.parent.mkdir(parents=True)
    with sqlite3.connect(vectors) as conn:
        conn.execute("CREATE TABLE chunks(file_path TEXT, chunk_idx INTEGER, chunk_text TEXT, vector BLOB, file_mtime REAL)")
    profile = json.loads(driver.PROFILE.read_text())
    profile.update(queries=1, repeats=1, callers=[4])
    profile["decision"]["bars"] = ["passage-four"]
    raw_cases = json.dumps(driver.frozen_cases(profile, {"model": "test", "fingerprint": "test", "dim": 1})).encode()
    (baseline / "frozen-cases.json").write_bytes(raw_cases)
    driver.write_json(baseline / "measure-report.json", {"frozen_cases_sha256": hashlib.sha256(raw_cases).hexdigest()})
    driver.write_json(baseline / "preparation.json", {"preparation": {}, "command": ["prepare", "--root", str(root)]})

    def copy_fixture(_baseline, destination, *_args, **_kwargs):
        destination.mkdir()
        return {"vector_digest": driver.vector_digest(vectors), "vector_path": str(vectors)}

    async def output_failure(_arm, segment, _profile, _preparation, **kwargs):
        rows = [{"mode": kwargs["scoring_mode"], "repeat": 0, "callers": 4,
                 "case": json.loads(raw_cases)[0]["id"], "client": number,
                 "elapsed_ms": 25.0 + number, "terminal_received": True, "outcome": "ok",
                 "failure": None, "response": {"hits": [{"path": "Sources/reference.md"}]}}
                for number in range(4)]
        (segment / "terminals.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        enriched = [rows[0], rows[0]] if enrichment == "duplicate" else [{**rows[0], "client": 99}] if enrichment == "unexpected" else []
        (segment / "samples.jsonl").write_text("{partial" if enrichment == "truncated" else "\n".join(json.dumps(row) for row in enriched))
        raise OSError("sample output failed after the terminal journal flushed")

    monkeypatch.setattr(driver, "copied_fixture", copy_fixture)
    monkeypatch.setattr(driver, "make_arm", lambda *_args: None)
    monkeypatch.setattr(driver, "runtime", lambda _profile: {})
    monkeypatch.setattr(driver, "measure", output_failure)

    if enrichment in ("duplicate", "unexpected"):
        with pytest.raises(RuntimeError, match="duplicate or undeclared attempt slots"):
            asyncio.run(driver.paired(root, baseline, profile))
        return
    report = asyncio.run(driver.paired(root, baseline, profile))

    retained = [json.loads(line) for line in (root / "samples.jsonl").read_text().splitlines()]
    completed = [row for row in retained if row["mode"] == "disk"]
    assert [row["elapsed_ms"] for row in completed] == [25.0, 26.0, 27.0, 28.0]
    assert all(row["terminal_received"] and row["outcome"] == "ok" and row["response"]["hits"] for row in completed)
    assert all(row["dispatch"]["observation_state"] == ("failed" if enrichment == "truncated" else "missing") for row in completed)
    assert report["conditions"]["disk"][0]["terminals"] == 4
    assert not report["qualifications"]["disk"]
    assert report["state"] == "failed"
