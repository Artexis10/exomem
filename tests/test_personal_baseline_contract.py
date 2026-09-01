"""Portable durable-personal-baseline contract and contribution-fixture guard."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from exomem import commands, prominence

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "hosted_v5_contributions" / "personal_baselines.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _eligible(case: dict) -> bool:
    """Fixture invariant only; semantic judgment remains with the active agent."""
    return case["stability_or_recurrence"] and case["reusable_comparison_value"]


def _validate_case(case: dict) -> None:
    if _eligible(case):
        assert case["route"] in {"entity", "compiled_observation", "records"}
    else:
        assert case["route"] == "no_write"


def test_personal_baseline_contribution_has_generic_positive_negative_twins() -> None:
    fixture = _fixture()
    assert fixture["schema_version"] == 1
    cases = fixture["cases"]
    assert {case["class"] for case in cases} == {
        "stable_preference",
        "recurring_routine",
        "historical_baseline",
        "durable_affiliation",
    }
    assert {case["route"] for case in cases} == {
        "entity",
        "compiled_observation",
        "records",
        "no_write",
    }
    for name in {case["class"] for case in cases}:
        twins = [case for case in cases if case["class"] == name]
        assert len(twins) == 2
        assert any(_eligible(case) for case in twins)
        assert any(case["route"] == "no_write" for case in twins)
    for case in cases:
        _validate_case(case)
    assert fixture["separate_executed_method_cases"] == [
        "executed-method-positive",
        "executed-method-failure",
        "executed-method-parameter-boundary",
        "executed-method-unreusable-one-off",
    ]


def test_personal_baseline_fixture_kills_eligibility_and_route_mutants() -> None:
    eligible = next(case for case in _fixture()["cases"] if case["route"] == "entity")
    for key in ("stability_or_recurrence", "reusable_comparison_value"):
        mutant = copy.deepcopy(eligible)
        mutant[key] = False
        assert not _eligible(mutant), key
    no_write = next(case for case in _fixture()["cases"] if case["route"] == "no_write")
    assert not _eligible(no_write)
    assert eligible["route"] == "entity"
    for original, mutated_route in ((eligible, "no_write"), (no_write, "compiled_observation")):
        mutant = copy.deepcopy(original)
        mutant["route"] = mutated_route
        with pytest.raises(AssertionError):
            _validate_case(mutant)


_CARRIERS = {
    "bootstrap": ROOT / "src" / "exomem" / "prominence.py",
    "bootstrap_projection": ROOT / "src" / "exomem" / "commands.py",
    "scaffold": ROOT / "src" / "exomem" / "_scaffold" / "_Schema" / "SKILL.md",
    "capture_workflow": ROOT
    / "src"
    / "exomem"
    / "_scaffold"
    / "_Schema"
    / "workflow-skills"
    / "exomem-capture"
    / "SKILL.md",
    "operations": ROOT / "src" / "exomem" / "_scaffold" / "_Schema" / "references" / "operations.md",
    "reminder": ROOT / "src" / "exomem" / "_hooks" / "exomem_capture_nudge.py",
    "quickstart": ROOT / "QUICKSTART.md",
    "assistant_guide": ROOT / "docs" / "ai-assistant-guide.md",
    "prominence_docs": ROOT / "docs" / "prominence.md",
    "claude_core": ROOT / "plugins" / "claude-code" / "skills" / "exomem" / "SKILL.md",
    "claude_capture": ROOT / "plugins" / "claude-code" / "skills" / "exomem-capture" / "SKILL.md",
    "claude_operations": ROOT
    / "plugins"
    / "claude-code"
    / "skills"
    / "exomem"
    / "references"
    / "operations.md",
    "claude_reminder": ROOT / "plugins" / "claude-code" / "hooks" / "exomem_capture_nudge.py",
}


@pytest.mark.parametrize("carrier,path", _CARRIERS.items())
def test_non_hosted_capture_carrier_inventory_has_baseline_doctrine(
    carrier: str, path: Path
) -> None:
    text = path.read_text(encoding="utf-8").lower()
    assert "stable preference" in text, carrier
    assert "recurr" in text, carrier
    assert "reusable" in text and "comparison" in text, carrier
    assert "compiled observation" in text, carrier
    assert "records" in text, carrier
    assert "one-off" in text and "incidental" in text, carrier


@pytest.mark.parametrize("profile", ("compact", "full", "diagnostics"))
@pytest.mark.parametrize("level", ("balanced", "maximal"))
def test_prominent_bootstrap_profiles_expose_command_independent_baselines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, level: str
) -> None:
    monkeypatch.setenv("EXOMEM_PROMINENCE", level)
    (tmp_path / "Knowledge Base").mkdir()
    payload = commands.op_bootstrap(tmp_path, profile=profile)
    capture = payload["engagement"]["contract"]["capture"].lower()
    assert "stable preference" in capture
    assert "recurr" in capture and "reusable comparison" in capture
    assert "compiled observation" in capture and "records" in capture
    assert "one-off" in capture and "incidental" in capture
    assert "_memory" not in capture


@pytest.mark.parametrize("level", ("off", "light"))
def test_quiet_profiles_remain_explicit_request_only(level: str) -> None:
    capture = prominence.contract(level).capture.lower()
    assert "only when the user asks" in capture or "explicitly asks" in capture
    assert "stable preference" not in capture


def test_capture_reminder_is_structural_bounded_and_model_free() -> None:
    source = (ROOT / "src" / "exomem" / "_hooks" / "exomem_capture_nudge.py").read_text(
        encoding="utf-8"
    ).lower()
    assert "language-agnostic" in source
    assert "structural" in source
    tree = ast.parse(source)
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not imports & {"openai", "anthropic", "transformers", "requests", "httpx"}
    assert "stable preference" in source
