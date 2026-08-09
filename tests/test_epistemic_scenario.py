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


def test_catastrophic_list_must_be_a_subset_of_the_registry() -> None:
    with pytest.raises(ScenarioLoadError) as excinfo:
        load_scenario(FIXTURES / "red-catastrophic-not-registered.yaml")
    assert "contradiction_is_pretty" in str(excinfo.value)


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
