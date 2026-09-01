"""Portable durable-personal-baseline contract and contribution-fixture guard."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from exomem import commands, prominence
from exomem._hooks import exomem_capture_nudge as capture_hook

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "hosted_v5_contributions" / "personal_baselines.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_personal_baseline_contribution_pins_ordinary_prose_and_exact_routing() -> None:
    fixture = _fixture()
    required_case_fields = {"id", "class", "turn", "evidence", "expected"}
    required_evidence_fields = {
        "stability_or_recurrence",
        "reusable_comparison_value",
        "entity_resolution",
        "observed_measurement",
        "compatible_existing_records_collection",
        "tentative_or_unresolved",
    }
    required_expected_fields = {
        "route",
        "forbidden_routes",
        "forbidden_tools",
        "require_explicit_request",
    }
    for case in fixture["cases"]:
        assert set(case) == required_case_fields, case["id"]
        assert set(case["evidence"]) == required_evidence_fields, case["id"]
        assert set(case["expected"]) == required_expected_fields, case["id"]


def _eligible(case: dict) -> bool:
    """Fixture invariant only; semantic judgment remains with the active agent."""
    evidence = case["evidence"]
    return (
        evidence["stability_or_recurrence"]
        and evidence["reusable_comparison_value"]
        and not evidence["tentative_or_unresolved"]
    )


_ROUTES = {"entity", "compiled_observation", "records", "no_write"}
_FORBIDDEN_TOOLS = {
    "entity": {"remember", "record_memory"},
    "compiled_observation": {"edit_memory", "record_memory"},
    "records": {"edit_memory", "remember"},
    "no_write": {"edit_memory", "remember", "record_memory"},
}


def _expected_route(case: dict) -> str:
    evidence = case["evidence"]
    if not _eligible(case):
        return "no_write"
    if evidence["observed_measurement"]:
        assert evidence["compatible_existing_records_collection"]
        return "records"
    if evidence["entity_resolution"] == "unique":
        return "entity"
    assert evidence["entity_resolution"] == "none"
    assert not evidence["compatible_existing_records_collection"]
    return "compiled_observation"


def _validate_case(case: dict) -> None:
    expected = case["expected"]
    route = expected["route"]
    assert route == _expected_route(case)
    assert set(expected["forbidden_routes"]) == _ROUTES - {route}
    assert set(expected["forbidden_tools"]) == _FORBIDDEN_TOOLS[route]
    assert expected["require_explicit_request"] is (route == "no_write")


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
    assert {case["expected"]["route"] for case in cases} == {
        "entity",
        "compiled_observation",
        "records",
        "no_write",
    }
    for name in {case["class"] for case in cases}:
        twins = [case for case in cases if case["class"] == name]
        assert len(twins) == 2
        assert any(_eligible(case) for case in twins)
        assert any(case["expected"]["route"] == "no_write" for case in twins)
    for case in cases:
        _validate_case(case)
    methods = fixture["separate_executed_method_cases"]
    assert [case["id"] for case in methods] == [
        "executed-method-positive",
        "executed-method-failure",
        "executed-method-parameter-boundary",
        "executed-method-unreusable-one-off",
    ]
    assert [case["expected"] for case in methods] == [
        "separate-executed-method-route",
        "separate-executed-method-route",
        "separate-executed-method-route",
        "no_write",
    ]


def test_personal_baseline_fixture_kills_eligibility_and_route_mutants() -> None:
    cases = _fixture()["cases"]
    eligible = next(case for case in cases if case["expected"]["route"] == "entity")
    for key in ("stability_or_recurrence", "reusable_comparison_value"):
        mutant = copy.deepcopy(eligible)
        mutant["evidence"][key] = False
        assert not _eligible(mutant), key
        with pytest.raises(AssertionError):
            _validate_case(mutant)
    no_write = next(case for case in cases if case["expected"]["route"] == "no_write")
    assert not _eligible(no_write)
    for original in cases:
        route = original["expected"]["route"]
        for mutated_route in _ROUTES - {route}:
            mutant = copy.deepcopy(original)
            mutant["expected"]["route"] = mutated_route
            with pytest.raises(AssertionError):
                _validate_case(mutant)


def test_tentative_or_unresolved_affiliation_stays_quiet_despite_durable_signals() -> None:
    tentative = next(
        case
        for case in _fixture()["cases"]
        if case["id"] == "tentative-unresolved-affiliation-quiet"
    )
    assert _eligible(tentative) is False
    assert tentative["expected"]["route"] == "no_write"
    for mutation in (
        ("expected.route", "entity"),
        ("evidence.tentative_or_unresolved", False),
    ):
        mutant = copy.deepcopy(tentative)
        section, field = mutation[0].split(".")
        mutant[section][field] = mutation[1]
        with pytest.raises(AssertionError):
            _validate_case(mutant)


_BASELINE_MARKERS = (
    "stable preference",
    "recurring routine",
    "historical baseline",
    "durable affiliation",
    "stability or recurrence",
    "reusable comparison",
    "uniquely resolved entity",
    "compiled observation",
    "records",
    "one-off",
    "incidental",
    "tentative",
)
_DELEGATION_MARKERS = ("proactive_capture", "link_acceptance", "restructure_execution")


def _markdown_block(path: Path, start: str, end: str) -> str:
    text = path.read_text(encoding="utf-8").lower()
    return text.split(start.lower(), 1)[1].split(end.lower(), 1)[0].replace("\n> ", "\n")


def test_active_baseline_blocks_preserve_doctrine_and_delegation() -> None:
    scaffold = ROOT / "src" / "exomem" / "_scaffold" / "_Schema"
    blocks = {
        "scaffold": _markdown_block(scaffold / "SKILL.md", "- A **durable personal baseline**", "- Capture whether"),
        "capture workflow": _markdown_block(
            scaffold / "workflow-skills" / "exomem-capture" / "SKILL.md",
            "## Durable personal baselines",
            "## Workflow",
        ),
        "operations": _markdown_block(
            scaffold / "references" / "operations.md",
            "## Durable personal baseline routing",
            "## Planning",
        ),
        "quickstart": _markdown_block(
            ROOT / "QUICKSTART.md",
            '> **What "auto-capture" does and doesn\'t do.',
            "> **Other MCP clients.",
        ),
        "assistant guide": _markdown_block(
            ROOT / "docs" / "ai-assistant-guide.md",
            "Save durable conclusions when the conversation lands on one",
            "## Simple actions",
        ),
        "structural reminder": capture_hook.REMINDER.lower(),
        "maximal docs": _markdown_block(ROOT / "docs" / "prominence.md", "### Maximal", "### Balanced"),
        "balanced docs": _markdown_block(ROOT / "docs" / "prominence.md", "### Balanced", "### Light"),
    }
    for name, block in blocks.items():
        block = " ".join(block.split())
        for marker in _BASELINE_MARKERS:
            assert marker in block, (name, marker)
        for marker in _DELEGATION_MARKERS:
            assert marker in block, (name, marker)
    assert "when torn between saving and letting it pass, save" in blocks["maximal docs"]
    assert "not mid-thought exploration" in blocks["balanced docs"]


def test_executed_method_cases_remain_a_separate_predicate() -> None:
    skill = (ROOT / "src" / "exomem" / "_scaffold" / "_Schema" / "SKILL.md").read_text(
        encoding="utf-8"
    ).lower()
    method = " ".join(
        skill.split("- a carried-out method", 1)[1].split("- a **stated intent", 1)[0].split()
    )
    for marker in ("actually executed", "reports the result", "informative", "reusable", "one-off"):
        assert marker in method
    assert "stable preference" not in method


def test_generated_active_copies_match_each_named_source_block() -> None:
    schema = ROOT / "src" / "exomem" / "_scaffold" / "_Schema"
    plugin = ROOT / "plugins" / "claude-code"
    pairs = (
        (schema / "SKILL.md", plugin / "skills" / "exomem" / "SKILL.md"),
        (
            schema / "workflow-skills" / "exomem-capture" / "SKILL.md",
            plugin / "skills" / "exomem-capture" / "SKILL.md",
        ),
        (schema / "references" / "operations.md", plugin / "skills" / "exomem" / "references" / "operations.md"),
        (ROOT / "src" / "exomem" / "_hooks" / "exomem_capture_nudge.py", plugin / "hooks" / "exomem_capture_nudge.py"),
    )
    for source, generated in pairs:
        assert generated.read_bytes() == source.read_bytes(), generated


@pytest.mark.parametrize("profile", ("compact", "full", "diagnostics"))
@pytest.mark.parametrize("level", ("off", "light", "balanced", "maximal"))
def test_every_served_bootstrap_projection_respects_its_prominence_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, level: str
) -> None:
    monkeypatch.setenv("EXOMEM_PROMINENCE", level)
    (tmp_path / "Knowledge Base").mkdir()
    payload = commands.op_bootstrap(tmp_path, profile=profile)
    capture = payload["engagement"]["contract"]["capture"].lower()
    if level in {"off", "light"}:
        assert "explicitly asks" in capture or "only when the user asks" in capture
        assert "stable preference" not in capture
        return
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
