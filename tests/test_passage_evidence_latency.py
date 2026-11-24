"""Prevent queued callers from qualifying as concurrent worker execution."""

import asyncio
import hashlib
import importlib.util
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("passage_evidence_latency", SCRIPTS / "passage_evidence_latency.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="the declared worker reference observes Linux descriptor reads")
@pytest.mark.parametrize("request_work", [False, True])
def test_inventory_observer_attributes_public_work_but_excludes_background_proof_reads(vault, tmp_path, monkeypatch, request_work):
    """Global work counters misreport normal proof drain as request Source reads or encoding."""
    from fastmcp import Client

    from exomem import capture_inventory, embeddings, server

    source = vault / "Knowledge Base/Sources/reference.md"
    source.write_text("# Reference\n\nPublic fixture words.\n")
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(embeddings, "embed_texts", lambda *args, **kwargs: [])

    def work():
        with sqlite3.connect(":memory:") as connection:
            connection.execute("SELECT 1").fetchone()
        capture_inventory.read_canonical(vault, source.relative_to(vault).as_posix())
        source.read_text()
        embeddings.embed_texts(["fixture"])

    def inventory(*args, **kwargs):
        background = threading.Thread(target=work)
        background.start()
        background.join()
        if request_work:
            work()
        return {"summary": "Observed fixture.", "items": []}

    monkeypatch.setattr(capture_inventory, "inventory", inventory)
    journal = tmp_path / "request-work.jsonl"
    with driver.observe_inventory_work(journal, vault, [source.relative_to(vault).as_posix()]):
        mcp = server.build_server(require_auth=False)

        async def call():
            async with Client(mcp) as client:
                return await client.call_tool("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "reference"}}, raise_on_error=False)

        result = asyncio.run(call())

    [observation] = [json.loads(line) for line in journal.read_text().splitlines()]
    assert not result.is_error
    assert observation["request_id"]
    assert observation["sql_statements"] == int(request_work)
    assert (observation["canonical_source_read_calls"] > 0) is request_work
    assert (observation["source_file_read_opens"] > 0) is request_work
    assert (observation["source_file_read_ops"] > 0) is request_work
    assert (observation["source_file_read_bytes"] > 0) is request_work
    assert observation["encoder_calls"] == int(request_work)
    assert "SELECT" not in journal.read_text()


def test_inventory_traversal_rejects_a_repeated_page_even_when_all_slots_exist():
    """A page-count receipt alone can certify a repeated page and lose half the Sources."""
    cases = [{"id": "page-1", "arguments": {"arguments": {"limit": 2}}},
             {"id": "page-2", "arguments": {"arguments": {"limit": 2, "cursor": "after-first"}}}]
    rows = [{"case": "page-1", "terminal_received": True, "failure": None,
             "response": {"items": [{"source_ref": "one"}, {"source_ref": "two"}], "next_cursor": "after-first"}},
            {"case": "page-2", "terminal_received": True, "failure": None,
             "response": {"items": [{"source_ref": "one"}, {"source_ref": "two"}], "next_cursor": None}}]
    truth = [{"key": ["reference", str(n)], "item": {"source_ref": ref}} for n, ref in enumerate(("one", "two", "three", "four"))]

    receipt = driver.inventory_traversal(rows, cases, truth, {"page_limit": 2, "sources": 4})

    assert not receipt["complete"]
    assert receipt["unique_sources"] == 2
    assert receipt["failure"] == "traversal-identities-differ"


def test_inventory_traversal_keeps_a_malformed_terminal_visible_as_failed():
    """A null item array must fail traversal without crashing aggregation of the retained calls."""
    cases = [{"id": "page", "arguments": {"arguments": {"limit": 200}}}]
    rows = [{"case": "page", "terminal_received": True, "failure": "unexpected-response", "response": {"items": None}}]

    receipt = driver.inventory_traversal(rows, cases, [], {"page_limit": 200, "sources": 200})

    assert not receipt["complete"]
    assert receipt["failure"] == "traversal-terminal-unavailable"


@pytest.mark.parametrize("observation", ["missing", "truncated", "different-call"])
def test_inventory_observation_failure_preserves_the_received_terminal(tmp_path, observation):
    """Losing count evidence must prevent qualification without erasing a completed public call."""
    work = tmp_path / "work.jsonl"
    if observation == "truncated":
        work.write_text("{partial")
    elif observation == "different-call":
        work.write_text(json.dumps({"arguments_sha256": "wrong", "request_id": "other"}) + "\n")
    log = tmp_path / "worker.log"
    log.write_text("\n".join(json.dumps({"ts": str(n), "message": f"event=tool_{event} tool=inspect_captured_sources request_id=one"})
                             for n, event in enumerate(("start", "success"))))
    row = {"client": 0, "elapsed_ms": 10.0, "terminal_received": True, "failure": None,
           "response": {"items": []}, "arguments_sha256": "expected"}
    with (tmp_path / "terminals.jsonl").open("w") as terminals, (tmp_path / "samples.jsonl").open("w") as samples:
        driver.retain_batch([row], terminals, samples, log, 0, 1,
                            tool_name="inspect_captured_sources", request_observation=(work, 0))

    [retained] = [json.loads(line) for line in (tmp_path / "samples.jsonl").read_text().splitlines()]
    assert retained["terminal_received"] and retained["elapsed_ms"] == 10.0
    assert retained["request_work"]["observation_state"] == "failed"


@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
def test_inventory_worker_start_failure_retains_every_scheduled_attempt(tmp_path, monkeypatch, failure_type):
    """A worker startup failure must leave the full frozen population visible as unattempted."""
    root, baseline = tmp_path / "run", tmp_path / "prepared"
    root.mkdir()
    baseline.mkdir()
    profile = json.loads(driver.INVENTORY_PROFILE.read_text())
    profile.update(sources=4, page_limit=2, limits=[1, 2])
    truth = [{"key": ["reference", str(n), "1", f"ref-{n}", f"source-{n}"],
              "item": {"source_ref": f"ref-{n}", "source_hash": f"hash-{n}"}} for n in range(4)]
    cases = driver.inventory_cases(profile, truth, "space")
    slots = driver.paired_slots(profile, cases)
    for name, value in (("expected-sources", truth), ("frozen-cases", cases), ("attempt-slots", slots)):
        driver.write_json(baseline / f"{name}.json", value)
    preparation = {"encoder": {}, "recall_space_id": "space", "rows": {},
                   "vector_digest": {"rows": 108, "sha256": "vectors"},
                   "truth_sha256": hashlib.sha256((baseline / "expected-sources.json").read_bytes()).hexdigest(),
                   "cases_sha256": hashlib.sha256((baseline / "frozen-cases.json").read_bytes()).hexdigest()}
    driver.write_json(baseline / "inventory-preparation.json", {"profile": profile, "preparation": preparation})

    class FailedArm:
        env = {}

        async def start(self, **kwargs):
            raise failure_type("worker could not start")

        async def stop(self):
            pass

    def copied(_baseline, destination, *_args, **_kwargs):
        destination.mkdir()
        return {"vector_path": str(destination / "vectors"), "vector_digest": preparation["vector_digest"]}

    monkeypatch.setattr(driver, "copied_fixture", copied)
    monkeypatch.setattr(driver, "vector_digest", lambda _path: preparation["vector_digest"])
    monkeypatch.setattr(driver, "verify_rows", lambda *_args: {})
    monkeypatch.setattr(driver, "make_arm", lambda *_args: FailedArm())
    monkeypatch.setattr(driver, "runtime", lambda _profile: {})
    monkeypatch.setattr(driver, "manifest", lambda: {})

    report = asyncio.run(driver.inventory_measure(root, baseline, profile))

    retained = [json.loads(line) for line in (root / "samples.jsonl").read_text().splitlines()]
    assert report["state"] == "failed"
    assert len(retained) == len(slots)
    assert all(row["outcome"] == "unattempted" and row["elapsed_ms"] is None for row in retained)
    assert report["failure"]["message"] == "worker could not start"


@pytest.mark.parametrize("defect", [None, "missing", "sql", "source", "encoder", "descriptor"])
def test_inventory_summary_requires_matching_native_counts_and_zero_request_source_or_encoder_work(defect):
    """Fast valid responses cannot qualify when count evidence is missing or request work grows."""
    cases = [{"id": f"page-{limit}", "arguments": {"arguments": {"limit": limit}}} for limit in (2, 200)]
    profile = {"limits": [2, 200], "repeats": 1}
    rows = [{"case": case["id"], "repeat": 0, "outcome": "ok", "failure": None,
             "elapsed_ms": 1.0, "terminal_received": True, "dispatch": {"overlap_verified": True},
             "request_work": {"observation_state": "observed", "closed": True, "request_id": str(n),
                              "sql_statements": 9, "canonical_source_read_calls": 0,
                              "source_file_read_opens": 0, "source_file_read_ops": 0,
                              "source_file_read_bytes": 0, "descriptor_observation_errors": 0,
                              "encoder_calls": 0}} for n, case in enumerate(cases)]
    if defect == "missing":
        rows[-1].pop("request_work")
    elif defect == "sql":
        rows[-1]["request_work"]["sql_statements"] = 10
    elif defect == "source":
        rows[-1]["request_work"]["canonical_source_read_calls"] = 1
    elif defect == "encoder":
        rows[-1]["request_work"]["encoder_calls"] = 1
    elif defect == "descriptor":
        rows[-1]["request_work"]["descriptor_observation_errors"] = 1

    report = driver.summarize_inventory(rows, cases, profile)

    assert report["request_work_passed"] is (defect is None)
    assert report["sql_constant"] is (defect not in ("missing", "sql"))


def test_inventory_plan_uses_generated_truth_and_the_same_keyset_afterpoints():
    """Lexical record ordering and the final cursor must not drop or repeat Sources."""
    profile = {"sources": 12, "page_limit": 4, "limits": [2, 4], "repeats": 3, "passages_per_source": 27}
    rows = [{"key": ["reference", str(n), "1", f"ref-{n}", f"source-{n}"],
             "item": {"source_ref": f"ref-{n}", "source_hash": f"hash-{n}"}}
            for n in range(12)]
    cases = driver.inventory_cases(profile, rows, "space")
    large = [case for case in cases if case["arguments"]["arguments"]["limit"] == 4]
    small = [case for case in cases if case["arguments"]["arguments"]["limit"] == 2]
    truth = sorted(rows, key=lambda row: row["key"])
    observed = [item["properties"]["source_ref"]["const"] for case in large
                for item in case["expected"]["response_schema"]["properties"]["items"]["prefixItems"]]
    assert observed == [row["item"]["source_ref"] for row in truth]
    assert [case["arguments"]["arguments"].get("cursor") for case in large] == [
        case["arguments"]["arguments"].get("cursor") for case in small]
    assert large[-1]["expected"]["response_schema"]["properties"]["next_cursor"] == {"const": None}
    assert len(driver.paired_slots({"modes": ["inventory"], "repeats": 3, "callers": [1]}, cases)) == 18


def test_inventory_plan_rejects_an_incomplete_generated_manifest():
    """A partial fixture must not silently shrink the frozen attempt population."""
    with pytest.raises(RuntimeError, match="generated inventory truth"):
        driver.inventory_cases({"sources": 2, "page_limit": 200, "limits": [2, 200]}, [], "space")


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


def test_a_ranking_arm_sends_its_request_mode_and_expects_its_ranking():
    profile = driver.load_profile(driver.PROFILE.parent / "ranking-profile.json")
    frozen = driver.frozen_cases({**profile, "queries": 1}, {"model": "test", "fingerprint": "test", "dim": 1})

    dense, hybrid = (driver.arm_cases(profile, arm, frozen)[0] for arm in profile["modes"])

    assert dense["arguments"]["options"] == {"result_level": "passage"}
    assert hybrid["arguments"]["options"] == {"result_level": "passage", "mode": "hybrid"}
    for case, ranking in ((dense, "dense"), (hybrid, "fused")):
        schema = case["expected"]["response_schema"]
        assert schema["properties"]["scoring_mode"] == {"const": "resident"}
        assert schema["properties"]["ranking"] == {"const": ranking}
        assert "ranking" in schema["required"]
    # The base profile declares no arms, so a mode keeps naming its own scoring mode and request.
    undeclared = driver.arm_cases({}, "disk", frozen)[0]
    assert undeclared["arguments"] == frozen[0]["arguments"]
    assert undeclared["expected"]["response_schema"]["properties"]["scoring_mode"] == {"const": "disk"}
    assert "ranking" not in undeclared["expected"]["response_schema"]["properties"]


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
