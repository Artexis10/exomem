"""Scenario loader: assertion names resolve at LOAD time, or loading fails."""

from __future__ import annotations

from pathlib import Path

import pytest

from epistemic.registry import ASSERTION_REGISTRY
from epistemic.schema import ScenarioLoadError, load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "benchmarks" / "epistemic" / "fixtures"


def test_minimal_scenario_loads_and_binds_every_assertion() -> None:
    scenario = load_scenario(FIXTURES / "scenario-minimal.yaml")
    assert scenario.scenario_id == "minimal-explicit-correction"
    assert scenario.family_id == "f01"
    assert scenario.kind == "corpus"
    assert [phase.phase_id for phase in scenario.phases] == ["p1-ingest", "p2-correct"]
    names = [
        expectation.assertion for phase in scenario.phases for expectation in phase.expect
    ]
    assert names
    assert set(names) <= set(ASSERTION_REGISTRY)
    assert {op.op for phase in scenario.phases for op in phase.ops} == {
        "ingest_source",
        "agent_turn",
        "snapshot",
    }


def test_loaded_scenario_exposes_bound_callables() -> None:
    scenario = load_scenario(FIXTURES / "scenario-minimal.yaml")
    bound = scenario.bound_assertions()
    assert bound
    for name, fn in bound:
        assert name in ASSERTION_REGISTRY
        assert callable(fn)


def test_unknown_assertion_is_a_hard_load_error() -> None:
    with pytest.raises(ScenarioLoadError) as excinfo:
        load_scenario(FIXTURES / "red-unknown-assertion.yaml")
    message = str(excinfo.value)
    assert "memory_feels_coherent" in message
    assert "red-unknown-assertion" in message


def test_unknown_operation_is_a_hard_load_error() -> None:
    with pytest.raises(ScenarioLoadError) as excinfo:
        load_scenario(FIXTURES / "red-unknown-op.yaml")
    assert "rewrite_history" in str(excinfo.value)


def test_missing_fairness_packet_blocks_the_scenario() -> None:
    with pytest.raises(ScenarioLoadError) as excinfo:
        load_scenario(FIXTURES / "red-missing-fairness-packet.yaml")
    message = str(excinfo.value)
    assert "fairness" in message
    assert "red-missing-fairness-packet" in message


def test_catastrophic_list_must_be_a_subset_of_the_frozen_set() -> None:
    """A registered-but-non-§3 name is still an illegal escalation."""

    with pytest.raises(ScenarioLoadError) as excinfo:
        load_scenario(FIXTURES / "red-catastrophic-escalation.yaml")
    message = str(excinfo.value)
    assert "contradiction_visible" in message
    assert "§3" in message


def test_scenario_model_is_strict_about_unknown_keys(tmp_path: Path) -> None:
    text = (FIXTURES / "scenario-minimal.yaml").read_text(encoding="utf-8")
    polluted = tmp_path / "polluted.yaml"
    polluted.write_text(text + "\nunexpected_key: 1\n", encoding="utf-8")
    with pytest.raises(ScenarioLoadError):
        load_scenario(polluted)


def test_malformed_yaml_is_a_load_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("scenario_id: [unclosed\n", encoding="utf-8")
    with pytest.raises(ScenarioLoadError):
        load_scenario(broken)


def test_every_shipped_fixture_named_red_fails_to_load() -> None:
    red = sorted(FIXTURES.glob("red-*.yaml"))
    assert len(red) >= 3
    for path in red:
        with pytest.raises(ScenarioLoadError):
            load_scenario(path)


def test_every_shipped_fixture_not_named_red_loads() -> None:
    green = [path for path in sorted(FIXTURES.glob("*.yaml")) if not path.name.startswith("red-")]
    assert green
    for path in green:
        assert load_scenario(path).scenario_id


# --------------------------------------------------------------------------
# Correction round.
# --------------------------------------------------------------------------


def test_m8_pair_assertion_without_a_counterpart_fails_to_load() -> None:
    """M8: an expectation whose semantics need two named items must declare both."""

    with pytest.raises(ScenarioLoadError) as excinfo:
        load_scenario(FIXTURES / "red-pair-assertion-missing-counterpart.yaml")
    message = str(excinfo.value)
    assert "contradiction_not_flattened" in message
    assert "counterpart" in message


def test_m8_snapshot_pair_assertion_needs_two_snapshot_ops() -> None:
    """M8: a transition invariant cannot be scored from a single snapshot."""

    with pytest.raises(ScenarioLoadError) as excinfo:
        load_scenario(FIXTURES / "red-transition-without-snapshot-pair.yaml")
    message = str(excinfo.value)
    assert "review_reopens_on_material_change" in message
    assert "snapshot" in message


def test_catastrophic_escalation_outside_the_frozen_set_fails_to_load() -> None:
    """The §3 set is frozen; a scenario may not escalate anything else."""

    with pytest.raises(ScenarioLoadError) as excinfo:
        load_scenario(FIXTURES / "red-catastrophic-escalation.yaml")
    message = str(excinfo.value)
    assert "contradiction_visible" in message


def test_unregistered_family_id_fails_to_load() -> None:
    with pytest.raises(ScenarioLoadError) as excinfo:
        load_scenario(FIXTURES / "red-unregistered-family.yaml")
    assert "f99" in str(excinfo.value)


def test_dead_declared_catastrophic_helper_is_gone() -> None:
    from epistemic.schema import Scenario

    assert not hasattr(Scenario, "declared_catastrophic")


def test_pair_requirement_sets_live_next_to_the_registry() -> None:
    from epistemic.registry import REQUIRES_ITEM_PAIR, REQUIRES_SNAPSHOT_PAIR

    assert "contradiction_not_flattened" in REQUIRES_ITEM_PAIR
    assert "decision_distinguishable_from_hypothesis" in REQUIRES_ITEM_PAIR
    assert {
        "review_reopens_on_material_change",
        "review_stays_closed_on_irrelevant_change",
        "external_edit_authoritative_within",
        "export_reconstructs_state",
        "dependent_conclusions_surfaced_for_review",
    } <= REQUIRES_SNAPSHOT_PAIR
    assert not REQUIRES_ITEM_PAIR & REQUIRES_SNAPSHOT_PAIR
