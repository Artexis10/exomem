"""The paired MCP read gate decides per group from case differences.

These tests drive the verdict, the family correction and the two refusals through
`summarize` and the refusal functions with handcrafted fragments. The managed-worker
runs that produce real fragments are exercised by the gate's own CI jobs.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import mcp_read_latency_gate as gate  # noqa: E402, I001
from release_arm import MeasurementRefused  # noqa: E402, I001

CONTRACT_SHA = "c" * 64
PROFILE_SHA = "p" * 64


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
                samples.append({"case": case, "group": group, "arm": arm, "repeat": repeat,
                                "elapsed_ms": elapsed, "failure": None})
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


# --- Private mode reports the head p95 against the target ------------------------


def test_private_mode_reports_a_store_group_against_its_p95_target() -> None:
    ref = {**reference("read", allowance_ms=10), "gating_groups": {"read": {"allowance_ms": 10, "p95_target_ms": 100}}}
    slow = summarize([fragment([group_cases("read", spread(1), base_ms=150)])], ref, mode="private")
    fast = summarize([fragment([group_cases("read", spread(1), base_ms=40)])], ref, mode="private")
    assert row(slow, "read")["target"] == {"p95_target_ms": 100, "result": "misses target"}
    assert row(fast, "read")["target"] == {"p95_target_ms": 100, "result": "meets target"}
    assert "target" not in row(summarize([fragment([group_cases("read", spread(1), base_ms=40)])], ref), "read")


def test_cpu_pressure_reads_the_sixty_second_average_and_names_an_unreadable_file(tmp_path: Path) -> None:
    pressure = tmp_path / "cpu"
    pressure.write_text("some avg10=0.50 avg60=1.25 avg300=0.10 total=1\n")
    assert gate.cpu_pressure(str(pressure)) == 1.25
    assert gate.cpu_pressure(str(tmp_path / "absent")) is None


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
