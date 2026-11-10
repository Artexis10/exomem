"""The paired MCP read gate decides per group from case differences.

These tests drive the verdict, the family correction and the two refusals through
`summarize` and the refusal functions with handcrafted fragments. The managed-worker
runs that produce real fragments are exercised by the gate's own CI jobs.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# The verdicts need SciPy's t quantile from the recall-benchmark group, which the gate's own CI job
# installs; the core tier runs without it and skips this module.
pytest.importorskip("scipy")

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import mcp_read_latency_gate as gate  # noqa: E402, I001
from release_arm import MeasurementRefused  # noqa: E402, I001
from synth_vault import gen_collection_fixture, gen_reference_vault  # noqa: E402, I001

CONTRACT_SHA = "c" * 64
PROFILE_SHA = "9" * 64


def reference(*groups: str, allowance_ms: float | None = None, lists: tuple[str, ...] = ()) -> dict:
    return {"repeats": 3, "minimum_cases": 20, "confidence": 0.95, "multiplicity": "bonferroni",
            "gating_groups": {name: {"allowance_ms": allowance_ms} for name in groups},
            "non_gating_groups": {name: {"p95_target_ms": 100} for name in lists}}


def group_cases(group: str, differences: list[float], *, base_ms: float = 100.0):
    """The assigned cases and samples of one group; each case repeats its difference three times."""
    assigned, samples = [], []
    for number, difference in enumerate(differences):
        case = f"{group}-{number:02d}"
        assigned.append({"id": case, "group": group})
        for repeat in range(3):
            for arm, elapsed in (("base", base_ms), ("head", base_ms + difference)):
                samples.append({"case": case, "group": group, "arm": arm, "repeat": repeat, "first": "base",
                                "elapsed_ms": elapsed, "outcome": "ok", "failure": None})
    return assigned, samples


def fragment(groups: list[tuple[list[dict], list[dict]]], *, shard: int = 0, shards: int = 1, **fields) -> dict:
    assigned = [case for cases, _ in groups for case in cases]
    return {"schema_version": 1, "mode": "fragment", "state": "measured", "shard": shard, "shards": shards,
            "contract_revision": "request-latency-v5", "contract_sha256": CONTRACT_SHA,
            "profile_sha256": PROFILE_SHA, "cases_sha256": "a" * 64, "boundary": "mcp-worker-socket-v1",
            "tag": "v1.0.0", "base_commit": "b" * 40, "reduced": False, "case_total": len(assigned),
            "assigned": assigned, "samples": [sample for _, samples in groups for sample in samples],
            "environment": {"cpu_pressure_start": 0.0, "cpu_pressure_end": 0.0}, **fields}


def summarize(fragments: list[dict], ref: dict, mode: str = "aggregate") -> dict:
    return gate.summarize(fragments, mode=mode, reference=ref, head_contract_sha256=CONTRACT_SHA,
                          head_profile_sha256=PROFILE_SHA)


def spread(center: float, width: float = 2.0) -> list[float]:
    """Twenty case differences around `center`."""
    return [center + width * (number % 5 - 2) for number in range(20)]


def row(summary: dict, group: str) -> dict:
    return next(entry for entry in summary["groups"] if entry["group"] == group)


# --- Verdict branches -----------------------------------------------------------


def test_a_group_whose_lower_bound_is_above_the_allowance_fails_the_run() -> None:
    summary = summarize([fragment([group_cases("read", spread(50))])], reference("read", allowance_ms=10))
    assert row(summary, "read")["verdict"] == "failed"
    assert summary["state"] == "failed"
    assert {"code": "slower-than-allowance", "group": "read"} in summary["failures"]


def test_a_group_whose_upper_bound_is_within_the_allowance_is_demonstrated() -> None:
    summary = summarize([fragment([group_cases("read", spread(1))])], reference("read", allowance_ms=10))
    assert row(summary, "read")["verdict"] == "demonstrated"
    assert summary["state"] == "passed"
    assert summary["warnings"] == []


def test_a_group_whose_interval_straddles_the_allowance_passes_with_a_warning() -> None:
    summary = summarize([fragment([group_cases("read", spread(10, width=20))])], reference("read", allowance_ms=10))
    assert row(summary, "read")["verdict"] == "inconclusive"
    assert summary["state"] == "passed"
    assert summary["warnings"] == [{"code": "inconclusive", "group": "read"}]


def test_a_group_without_an_allowance_reports_its_interval_and_passes_with_a_warning() -> None:
    summary = summarize([fragment([group_cases("read", spread(50))])], reference("read"))
    assert row(summary, "read")["verdict"] == "allowance-not-set"
    assert row(summary, "read")["lower_ms"] > 40
    assert summary["state"] == "passed"
    assert summary["warnings"] == [{"code": "allowance-not-set", "group": "read"}]


def test_a_list_group_is_reported_and_never_fails_the_run() -> None:
    ref = reference("read", allowance_ms=10, lists=("listing",))
    summary = summarize([fragment([group_cases("read", spread(1)), group_cases("listing", spread(500))])], ref)
    assert row(summary, "listing")["verdict"] == "reported"
    assert summary["state"] == "passed"


# --- A broken instrument fails --------------------------------------------------


def test_a_failed_case_fails_the_run_and_adds_nothing_to_the_interval() -> None:
    cases, samples = group_cases("read", spread(1))
    samples[0]["failure"] = "unexpected-state"
    summary = summarize([fragment([(cases, samples)])], reference("read", allowance_ms=10))
    assert summary["state"] == "failed"
    assert {"code": "case-failed", "group": "read", "case": "read-00"} in summary["failures"]
    assert row(summary, "read")["cases"] == 19


def test_a_missing_head_sample_is_a_missing_pair() -> None:
    cases, samples = group_cases("read", spread(1))
    samples.remove(next(s for s in samples if s["case"] == "read-03" and s["arm"] == "head" and s["repeat"] == 2))
    summary = summarize([fragment([(cases, samples)])], reference("read", allowance_ms=10))
    assert summary["state"] == "failed"
    assert {"code": "pair-missing", "group": "read", "case": "read-03"} in summary["failures"]


def test_fragments_with_different_contract_digests_fail_the_run() -> None:
    cases, samples = group_cases("read", spread(1))
    first = fragment([(cases[:10], samples[:60])], shard=0, shards=2, case_total=20)
    second = fragment([(cases[10:], samples[60:])], shard=1, shards=2, case_total=20, contract_sha256="d" * 64)
    summary = summarize([first, second], reference("read", allowance_ms=10))
    assert summary["state"] == "failed"
    assert {"code": "fragment-differs", "field": "contract_sha256"} in summary["failures"]


def test_a_fragment_built_on_another_contract_than_head_fails_the_run() -> None:
    summary = gate.summarize([fragment([group_cases("read", spread(1))])], mode="aggregate",
                             reference=reference("read", allowance_ms=10),
                             head_contract_sha256="e" * 64, head_profile_sha256=PROFILE_SHA)
    assert {"code": "fragment-differs", "field": "contract_sha256"} in summary["failures"]


def test_a_missing_shard_fails_the_run() -> None:
    only_shard = fragment([group_cases("read", spread(1))], shard=0, shards=2)
    summary = summarize([only_shard], reference("read", allowance_ms=10))
    assert {"code": "shard-missing"} in summary["failures"]


def test_a_declared_group_with_no_cases_fails_the_run() -> None:
    summary = summarize([fragment([group_cases("read", spread(1))])], reference("read", "search", allowance_ms=10))
    assert {"code": "group-missing", "group": "search"} in summary["failures"]


def test_a_group_below_the_case_floor_fails_unless_the_run_is_reduced() -> None:
    few = fragment([group_cases("read", spread(1)[:5])])
    assert summarize([few], reference("read", allowance_ms=10))["state"] == "failed"
    reduced = summarize([{**few, "reduced": True}], reference("read", allowance_ms=10))
    assert reduced["state"] == "reduced"
    assert {"code": "reduced-run"} in reduced["warnings"]


def test_a_refused_fragment_makes_the_run_refused() -> None:
    refused = {"schema_version": 1, "mode": "fragment", "state": "refused", "shard": 0,
               "reason": "arm-not-quiescent", "phase": "quiescence"}
    summary = summarize([refused], reference("read", allowance_ms=10))
    assert summary["state"] == "refused"
    assert summary["failures"] == [{"code": "arm-not-quiescent", "phase": "quiescence"}]


# --- The family correction ------------------------------------------------------


def test_the_family_size_is_the_contracts_declared_groups_not_the_groups_that_ran() -> None:
    alone = summarize([fragment([group_cases("read", spread(5))])], reference("read", allowance_ms=10))
    declared_three = summarize([fragment([group_cases("read", spread(5))])],
                               reference("read", "search", "recall", allowance_ms=10))
    assert alone["family"]["groups"] == 1
    assert declared_three["family"]["groups"] == 3
    assert declared_three["groups"] == [row(declared_three, "read")]
    narrow = row(alone, "read")["upper_ms"] - row(alone, "read")["lower_ms"]
    wide = row(declared_three, "read")["upper_ms"] - row(declared_three, "read")["lower_ms"]
    assert wide > narrow


def test_the_interval_is_the_student_t_interval_at_the_adjusted_confidence() -> None:
    # t(0.975, 19) = 2.0930 for one group; t(1 - 0.05 / 6, 19) = 2.6251 for a family of three.
    medians = spread(0, width=10)
    one = gate.group_interval(medians, confidence=0.95, family=1)
    three = gate.group_interval(medians, confidence=0.95, family=3)
    assert (one["upper_ms"] - one["mean_ms"]) / (three["upper_ms"] - three["mean_ms"]) == pytest.approx(
        2.0930 / 2.6251, abs=1e-3)


# --- Paired private timings cannot establish absolute acceptance ----------------


def test_private_mode_keeps_absolute_acceptance_unverified_for_paused_workers() -> None:
    ref = {**reference("read", allowance_ms=10), "gating_groups": {"read": {"allowance_ms": 10, "p95_target_ms": 100}}}
    environment = {"cpu_pressure_start": 0.0, "cpu_pressure_end": 0.0, "python": "3.12.3", "cpus": 4,
                   "arms": {"base": {"ready_s": 1.0, "quiescent_s": 1.0},
                            "head": {"ready_s": 1.0, "quiescent_s": 1.0}}}
    slow = summarize([fragment([group_cases("read", spread(1), base_ms=150)], environment=environment)], ref, mode="private")
    fast = summarize([fragment([group_cases("read", spread(1), base_ms=40)], environment=environment)], ref, mode="private")
    for summary in (slow, fast):
        assert row(summary, "read")["target"] == {
            "p95_target_ms": 100, "result": "unverified", "reason": "paused-worker-comparison"}
        gate.validate_document(summary)
    assert "target" not in row(summarize([fragment([group_cases("read", spread(1), base_ms=40)])], ref), "read")


def test_cpu_pressure_reads_the_sixty_second_average_and_names_an_unreadable_file(tmp_path: Path) -> None:
    pressure = tmp_path / "cpu"
    pressure.write_text("some avg10=0.50 avg60=1.25 avg300=0.10 total=1\n")
    assert gate.cpu_pressure(str(pressure)) == 1.25
    assert gate.cpu_pressure(str(tmp_path / "absent")) is None


# --- Unpaused absolute measurements --------------------------------------------


def absolute_cases():
    from exomem.recall_canary import load_cases

    return load_cases(json.dumps([
        {"id": name, "group": "source_read", "tool": "read_memory", "arguments": {"path": name},
         "expected": {"response_schema": {"type": "object", "required": ["content"]}}}
        for name in ("first", "second")
    ]).encode())


def absolute_reference():
    return {**reference(), "gating_groups": {"read": {"p95_target_ms": 100}}}


def test_absolute_lifecycle_warms_every_case_before_timing_and_never_pauses(monkeypatch) -> None:
    events = []
    busy = False

    class PreparedArm:
        client = SimpleNamespace()

        async def start(self, **_):
            events.append("start")
            return {"ready_s": 0.1, "quiescent_s": 0.1}

        def require_timeable(self):
            events.append("timeable")

        def run_python(self, _):
            return json.dumps({"python": "python", "python_version": "3.12", "module_origin": "exomem/__init__.py",
                               "version": "1.0"})

        async def stop(self):
            events.append("stop")

    async def call_tool(tool, arguments, **_):
        nonlocal busy
        events.append(arguments["path"])
        busy = arguments["path"] == "second"
        await asyncio.sleep(0.012)
        busy = False
        return SimpleNamespace(is_error=False, data={"content": "neutral"})

    PreparedArm.client.call_tool = call_tool
    monkeypatch.setattr(gate, "cpu_pressure", lambda: 20.0 if busy else 0.0)
    result = asyncio.run(gate.measure_absolute(PreparedArm(), absolute_cases(), repeats=2,
                                               pressure_wait_seconds=0, cadence_seconds=0.003))
    assert events == ["start", "timeable", "first", "second", "first", "second", "first", "second", "stop"]
    assert [(s["phase"], s["case"], s["repeat"]) for s in result["samples"]] == [
        ("warmup", "first", 0), ("warmup", "second", 0),
        ("timed", "first", 0), ("timed", "second", 0),
        ("timed", "first", 1), ("timed", "second", 1)]
    # Quiet endpoints cannot conceal contention in the middle of the measured window.
    observations = result["environment"]["pressure_observations"]
    assert observations[0]["avg60"] == 0.0
    assert observations[-1]["avg60"] == 0.0
    assert any(o["avg60"] == 20.0 for o in observations[1:-1])
    assert result["environment"]["pressure_max_gap_seconds"] > 0


def absolute_result():
    return {"samples": [
        {"case": case.id, "group": case.group, "phase": phase, "repeat": repeat,
         "elapsed_ms": 40.0, "outcome": "ok", "failure": None, "terminal_received": True}
        for phase, count in (("warmup", 1), ("timed", 3))
        for repeat in range(count) for case in absolute_cases()],
        "environment": {"pressure_observations": [
            {"monotonic_s": 1.0, "avg60": 0.0, "error": None},
            {"monotonic_s": 2.0, "avg60": 0.0, "error": None}],
            "pressure_wait_observations": [], "pressure_cadence_seconds": 1.0,
            "pressure_max_gap_seconds": 1.0, "pressure_threshold_avg60": 10.0,
            "timings": {"ready_s": 0.1, "quiescent_s": 0.1}},
        "runtime": {"python": "python", "python_version": "3.12", "module_origin": "exomem/__init__.py",
                    "version": "1.0"}}


@pytest.mark.parametrize("damage", ["missing", "duplicate", "unexpected", "warmup", "timed", "pressure", "unknown"])
def test_absolute_incomplete_or_contended_series_never_verifies_a_target(damage) -> None:
    result = absolute_result()
    if damage == "missing":
        result["samples"].pop()
    elif damage == "duplicate":
        result["samples"].append(result["samples"][-1].copy())
    elif damage == "unexpected":
        result["samples"].append({**result["samples"][-1], "case": "unassigned"})
    elif damage in ("warmup", "timed"):
        next(s for s in result["samples"] if s["phase"] == damage)["failure"] = "call-failed"
    else:
        result["environment"]["pressure_observations"].insert(1, {
            "monotonic_s": 1.5, "avg60": 20.0 if damage == "pressure" else None,
            "error": None if damage == "pressure" else "pressure-unavailable"})
    summary = gate.summarize_absolute(result, absolute_cases(), {"source_read": "read"},
                                      repeats=3, reference=absolute_reference())
    assert summary["groups"][0]["target"]["result"] == "unverified"
    assert summary["acceptance"] == "unverified"
    assert summary["state"] == ("unverified" if damage in ("pressure", "unknown") else "failed")


def test_absolute_binding_preserves_cases_and_validates_before_worker_start(monkeypatch, tmp_path) -> None:
    cases = absolute_cases()
    original = [case.model_dump() for case in cases]
    bindings = gate.absolute_bindings(cases, {"source_read": "read"}, reference=absolute_reference())
    summary = gate.summarize_absolute(absolute_result(), cases, bindings, repeats=3, reference=absolute_reference())
    assert summary["groups"][0]["target"]["result"] == "below-target"
    assert summary["acceptance"] == "unverified"
    assert [case.model_dump() for case in cases] == original
    with pytest.raises(MeasurementRefused):
        gate.absolute_bindings(cases, {}, reference=absolute_reference())
    with pytest.raises(MeasurementRefused):
        gate.absolute_bindings(cases, {"source_read": "unknown"}, reference=absolute_reference())
    cases[1].group = "another_read"
    with pytest.raises(MeasurementRefused):
        gate.absolute_bindings(cases, {"source_read": "read", "another_read": "read"}, reference=absolute_reference())
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps(original))
    monkeypatch.setattr(gate, "Arm", lambda *a, **k: pytest.fail("invalid bindings started an arm"))
    args = SimpleNamespace(cases=cases_file, group_bindings=None)
    with pytest.raises(MeasurementRefused):
        gate.run_private_absolute(args)


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -1.0, 101.0])
def test_absolute_unknown_pressure_stays_unknown(monkeypatch, value) -> None:
    monkeypatch.setattr(gate, "cpu_pressure", lambda: value)
    observation = gate.pressure_observation()
    assert observation["avg60"] is None
    assert observation["error"] == "pressure-unavailable"


def test_absolute_requests_the_watcher_by_omitting_even_a_zero_disable_value(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "0")
    paired = gate.Arm("paired", "python", tmp_path / "paired", tmp_path, worker_settings={})
    absolute = gate.Arm("absolute", "python", tmp_path / "absolute", tmp_path,
                        worker_settings={"EXOMEM_DISABLE_FILE_WATCHER": "0"}, file_watcher=True)
    assert paired.env["EXOMEM_DISABLE_FILE_WATCHER"] == "1"
    assert "EXOMEM_DISABLE_FILE_WATCHER" not in absolute.env


# --- The two refusals -----------------------------------------------------------


def test_an_arm_whose_worker_created_vault_state_is_refused() -> None:
    gate.require_warm_state(frozenset({"vault-1"}), frozenset({"vault-1"}))
    with pytest.raises(MeasurementRefused) as created:
        gate.require_warm_state(frozenset({"vault-1"}), frozenset({"vault-1", "vault-2"}))
    assert created.value.code == "arm-state-created-during-run"
    with pytest.raises(MeasurementRefused) as cold:
        gate.require_warm_state(frozenset(), frozenset({"vault-1"}))
    assert cold.value.code == "arm-state-created-during-run"


def derived_file(state_root: Path, *states: str) -> None:
    directory = state_root / "vault-1"
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / "collections-derived.sqlite") as connection:
        connection.execute("CREATE TABLE derived_collections(collection_id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        connection.executemany("INSERT INTO derived_collections VALUES (?, ?)",
                               [(f"collection-{number}", state) for number, state in enumerate(states)])


def test_an_arm_with_a_rebuilding_derived_collection_is_refused(tmp_path: Path) -> None:
    derived_file(tmp_path, "ready", "rebuilding")
    with pytest.raises(MeasurementRefused) as pending:
        gate.require_derived_ready(tmp_path)
    assert pending.value.code == "derived-collection-not-ready"


def test_an_arm_whose_derived_collections_are_all_ready_is_accepted(tmp_path: Path) -> None:
    derived_file(tmp_path, "ready", "ready")
    gate.require_derived_ready(tmp_path)


def test_an_arm_without_a_derived_file_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "vault-1").mkdir()
    gate.require_derived_ready(tmp_path)


def searching_arm(answers: list[dict]) -> tuple[gate.Arm, list[str]]:
    """An arm whose searches return `answers` in turn, recording each search and each quiet wait."""
    events: list[str] = []

    class Client:
        async def call_tool(self, name, arguments, **kwargs):
            events.append(name)
            answer = answers.pop(0)
            return SimpleNamespace(data=answer, is_error=False)

    async def quiesce(*, limit, phase):
        events.append(phase)
        return 0.0

    arm = object.__new__(gate.Arm)
    arm.name, arm.client, arm.quiesce = "head", Client(), quiesce
    return arm, events


WARMING = {"success": False, "error": {"code": "RETRIEVAL_INDEX_WARMING", "message": "warming"}}


def test_an_arm_is_timed_only_after_a_search_is_served_instead_of_refused_as_warming() -> None:
    """On 2026-11-09 every search case was refused as warming: the fixture writes left the keyword
    catalogue stale, and the worker went quiet without repairing it."""
    arm, events = searching_arm([WARMING, WARMING, {"hits": []}])
    asyncio.run(arm.serve_search(limit=60))
    assert events == ["search_memory", "search-readiness", "search_memory", "search-readiness", "search_memory"]


def test_an_arm_whose_search_stays_warming_is_refused() -> None:
    arm, _events = searching_arm([WARMING])
    with pytest.raises(MeasurementRefused) as warming:
        asyncio.run(arm.serve_search(limit=0))
    assert warming.value.code == "search-index-not-ready"


# --- Stored documents ----------------------------------------------------------


def test_a_summary_and_its_fragment_validate_against_the_stored_schema() -> None:
    measured = fragment([group_cases("read", spread(1))],
                        environment={"cpu_pressure_start": None, "cpu_pressure_end": 0.5, "python": "3.12.3", "cpus": 4,
                                     "arms": {"base": {"ready_s": 20.0, "quiescent_s": 12.0},
                                              "head": {"ready_s": 21.0, "quiescent_s": 12.0}}})
    summary = summarize([measured], reference("read", allowance_ms=10))
    gate.validate_document(measured)
    gate.validate_document(summary)
    gate.validate_document({"schema_version": 1, "mode": "aggregate", "state": "refused",
                            "failures": [{"code": "arm-not-quiescent", "phase": "quiescence"}]})
    absolute = gate.summarize_absolute(absolute_result(), absolute_cases(), {"source_read": "read"},
                                       repeats=3, reference=absolute_reference())
    gate.validate_document({**absolute, "contract_revision": "request-latency-v5", "contract_sha256": CONTRACT_SHA,
                            "cases_sha256": "a" * 64, "boundary": "mcp-worker-socket-v1",
                            "source_commit": "b" * 40, "group_bindings_sha256": "d" * 64})
    gate.validate_document({"schema_version": 1, "mode": "private-absolute", "state": "refused",
                            "acceptance": "unverified", "failures": [{"code": "case-group-missing", "phase": "cases"}]})


# --- Fixture -------------------------------------------------------------------


def test_an_arm_can_overlay_a_governed_fixture_without_changing_its_notes(tmp_path: Path) -> None:
    from exomem.governance.principal import library_scope

    arm = gate.Arm("overlay", sys.executable, tmp_path / "arm", tmp_path.parent, worker_settings={})
    profile = {**json.loads((ROOT / "benchmarks/recall-latency/profile.json").read_text()),
               "pages": 2, "links_min": 1, "links_max": 1}
    with library_scope():
        rels = gen_reference_vault(arm.vault, profile, ["testdata"])
    notes = {rel: (arm.vault / rel).read_bytes() for rel in rels}

    arm.init_vault(overlay=True)

    assert (arm.vault / "Knowledge Base/index.md").is_file()
    assert {rel: (arm.vault / rel).read_bytes() for rel in rels} == notes


def test_every_planning_item_is_valid_and_sits_under_a_parent_the_registry_allows() -> None:
    """A hierarchy that the worker would refuse is caught here, before a paired run loads it."""
    from exomem import planning
    from exomem.planning_values import PlanningValues

    registry = PlanningValues(None)
    profile = json.loads((ROOT / "benchmarks" / "mcp-read-latency" / "profile.json").read_text())
    for collection in gen_collection_fixture(profile)["planning"]:
        for position, (item, parent) in enumerate(zip(collection["items"], collection["parents"], strict=True)):
            planning.normalize_item(item, vault_root=None)
            allowed = registry.find("kind", item["kind"]).attributes["parents"]
            if allowed:
                assert parent is not None and parent < position
                assert collection["items"][parent]["kind"] in allowed
            else:
                assert parent is None
