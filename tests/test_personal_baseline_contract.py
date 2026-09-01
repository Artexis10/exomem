"""Portable durable-personal-baseline contract and contribution-fixture guard."""

from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from exomem import commands, envelope, prominence
from exomem._hooks import exomem_capture_nudge as capture_hook
from exomem.capabilities import ActiveSurfaceDescriptor, active_surface
from exomem.edit_operations import (
    normalize_edit_surface_arguments,
    public_edit_operation_schema,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "hosted_v5_contributions" / "personal_baselines.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_personal_baseline_contribution_pins_ordinary_prose_and_exact_routing() -> None:
    fixture = _fixture()
    assert fixture["authority_context"] == {
        "proactive_capture": "silent",
        "link_acceptance": "confirm-shortcut",
        "restructure_execution": "confirm",
    }
    assert fixture["authority_context"]["proactive_capture"] in envelope.RANGES[
        "proactive_capture"
    ]
    assert fixture["authority_context"]["link_acceptance"] in envelope.RANGES[
        "link_acceptance"
    ]
    assert (
        fixture["authority_context"]["restructure_execution"]
        == envelope.FIXED["restructure_execution"]
    )
    required_case_fields = {
        "id",
        "class",
        "turn",
        "starting_context",
        "evidence",
        "expected",
    }
    required_context_fields = {
        "entity_resolution",
        "entity_write_shape",
        "compatible_existing_records_collection",
        "exact_relation_confirmation",
    }
    required_evidence_fields = {
        "stability_or_recurrence",
        "reusable_comparison_value",
        "observed_measurement",
        "tentative_or_unresolved",
    }
    required_expected_fields = {
        "route",
        "expected_tools",
        "operation",
        "authority",
        "forbidden_routes",
        "forbidden_tools",
        "forbidden_operations",
        "require_explicit_request",
    }
    for case in fixture["cases"]:
        assert set(case) == required_case_fields, case["id"]
        assert set(case["starting_context"]) == required_context_fields, case["id"]
        assert set(case["evidence"]) == required_evidence_fields, case["id"]
        assert set(case["expected"]) == required_expected_fields, case["id"]
        authority = case["expected"]["authority"]
        if authority["action_class"] is not None:
            assert authority["disposition"] == fixture["authority_context"][
                authority["action_class"]
            ]


def _eligible(case: dict) -> bool:
    """Fixture invariant only; semantic judgment remains with the active agent."""
    evidence = case["evidence"]
    return (
        evidence["stability_or_recurrence"]
        and evidence["reusable_comparison_value"]
        and not evidence["tentative_or_unresolved"]
    )


_ROUTES = {
    "entity_facet",
    "accepted_relation",
    "compiled_observation",
    "records",
    "no_write",
}
_BASELINE_WRITE_TOOLS = {
    "remember",
    "edit_memory",
    "replace_memory",
    "capture_source",
    "preserve_evidence",
    "preserve_artifacts",
    "transfer_artifact",
    "record_memory",
    "plan_memory",
    "observe_memory",
    "connect_memory",
}
_CREATE_ENTITY_OPERATION = {
    "tool": "connect_memory",
    "arguments": {"operation": "create-entity"},
}
_REPLACE_BODY_OPERATION = {
    "tool": "edit_memory",
    "arguments": {"operation": {"kind": "replace_body"}},
}
_ENTITY_FACET_OPERATIONS = {
    "stable-preference-entity-facet": {
        "tool": "edit_memory",
        "arguments": {
            "path": "Knowledge Base/Entities/People/Example Person.md",
            "why": "Add the confirmed durable preference to the resolved Entity.",
            "operation": {
                "kind": "edit_section",
                "heading": "## Profile",
                "new_string": (
                    "- Stable preference: early review before important decisions."
                ),
                "section_position": "append",
            },
        },
    },
    "durable-affiliation-entity-facet": {
        "tool": "edit_memory",
        "arguments": {
            "path": "Knowledge Base/Entities/People/Example Person.md",
            "why": "Add the confirmed durable affiliation to the resolved Entity.",
            "operation": {
                "kind": "edit_section",
                "heading": "## Profile",
                "new_string": (
                    "- Durable affiliation: Example Professional Association."
                ),
                "section_position": "append",
            },
        },
    },
}
_ROUTE_CONTRACT = {
    "entity_facet": {
        "expected_tools": ["edit_memory"],
        "authority": {
            "action_class": "proactive_capture",
            "disposition": "silent",
            "requires_confirmation": False,
        },
    },
    "accepted_relation": {
        "expected_tools": ["connect_memory"],
        "operation": {
            "tool": "connect_memory",
            "arguments": {"operation": "accept-relation"},
        },
        "authority": {
            "action_class": "link_acceptance",
            "disposition": "confirm-shortcut",
            "requires_confirmation": True,
        },
    },
    "compiled_observation": {
        "expected_tools": ["remember"],
        "operation": {"tool": "remember", "arguments": {}},
        "authority": {
            "action_class": "proactive_capture",
            "disposition": "silent",
            "requires_confirmation": False,
        },
    },
    "records": {
        "expected_tools": ["record_memory"],
        "operation": {"tool": "record_memory", "arguments": {"action": "append"}},
        "authority": {
            "action_class": "proactive_capture",
            "disposition": "silent",
            "requires_confirmation": False,
        },
    },
    "no_write": {
        "expected_tools": [],
        "operation": None,
        "authority": {
            "action_class": None,
            "disposition": None,
            "requires_confirmation": False,
        },
    },
}


def _expected_operation(case: dict) -> dict | None:
    route = case["expected"]["route"]
    if route == "entity_facet":
        assert case["id"] in _ENTITY_FACET_OPERATIONS
        return _ENTITY_FACET_OPERATIONS[case["id"]]
    return _ROUTE_CONTRACT[route]["operation"]


def _expected_route(case: dict) -> str:
    evidence = case["evidence"]
    context = case["starting_context"]
    if not _eligible(case):
        return "no_write"
    if evidence["observed_measurement"]:
        assert context["compatible_existing_records_collection"]
        return "records"
    if context["entity_resolution"] == "unique":
        if context["entity_write_shape"] == "accepted_relation":
            assert context["exact_relation_confirmation"] is True
            return "accepted_relation"
        assert context["entity_write_shape"] == "additive_facet"
        return "entity_facet"
    assert context["entity_resolution"] == "none"
    assert context["entity_write_shape"] is None
    assert not context["compatible_existing_records_collection"]
    return "compiled_observation"


def _validate_case(case: dict) -> None:
    expected = case["expected"]
    route = expected["route"]
    assert route == _expected_route(case)
    contract = _ROUTE_CONTRACT[route]
    for key in ("expected_tools", "authority"):
        assert expected[key] == contract[key], (case["id"], key)
    assert expected["operation"] == _expected_operation(case), (
        case["id"],
        "operation",
    )
    assert set(expected["forbidden_routes"]) == _ROUTES - {route}
    assert set(expected["forbidden_tools"]) == (
        _BASELINE_WRITE_TOOLS - set(expected["expected_tools"])
    )
    forbidden_operations = [_CREATE_ENTITY_OPERATION]
    if route == "entity_facet":
        forbidden_operations.append(_REPLACE_BODY_OPERATION)
    assert expected["forbidden_operations"] == forbidden_operations
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
    assert {case["expected"]["route"] for case in cases} == _ROUTES
    for name in {case["class"] for case in cases}:
        twins = [case for case in cases if case["class"] == name]
        assert len(twins) >= 2
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
    eligible = next(case for case in cases if case["expected"]["route"] == "entity_facet")
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
        for key, mutation in (
            ("expected_tools", []),
            ("operation", None),
            (
                "authority",
                {
                    "action_class": "restructure_execution",
                    "disposition": "confirm",
                    "requires_confirmation": True,
                },
            ),
        ):
            if original["expected"][key] == mutation:
                continue
            mutant = copy.deepcopy(original)
            mutant["expected"][key] = mutation
            with pytest.raises(AssertionError):
                _validate_case(mutant)


def test_unique_entity_facet_and_relation_use_distinct_governed_operations() -> None:
    affiliation_cases = {
        case["expected"]["route"]: case
        for case in _fixture()["cases"]
        if case["class"] == "durable_affiliation" and _eligible(case)
    }
    facet = affiliation_cases["entity_facet"]
    relation = affiliation_cases["accepted_relation"]

    assert facet["turn"] == relation["turn"]
    assert facet["starting_context"]["entity_write_shape"] == "additive_facet"
    assert facet["expected"]["operation"] == _ENTITY_FACET_OPERATIONS[
        "durable-affiliation-entity-facet"
    ]
    assert facet["expected"]["authority"]["action_class"] == "proactive_capture"
    assert relation["starting_context"]["entity_write_shape"] == "accepted_relation"
    assert relation["expected"]["operation"] == {
        "tool": "connect_memory",
        "arguments": {"operation": "accept-relation"},
    }
    assert relation["expected"]["authority"]["action_class"] == "link_acceptance"
    assert _CREATE_ENTITY_OPERATION in facet["expected"]["forbidden_operations"]
    assert _CREATE_ENTITY_OPERATION in relation["expected"]["forbidden_operations"]


def test_entity_facet_operation_is_valid_narrow_and_rejects_broad_mutants() -> None:
    facets = [
        case
        for case in _fixture()["cases"]
        if case["expected"]["route"] == "entity_facet"
    ]

    edit_command = next(
        command
        for command in commands.product_commands_for("mcp")
        if command.name == "edit_memory"
    )
    params = {param.name: param for param in edit_command.params}
    required_params = {name for name, param in params.items() if param.required}
    for facet in facets:
        operation = facet["expected"]["operation"]
        assert operation == _ENTITY_FACET_OPERATIONS[facet["id"]]
        assert _REPLACE_BODY_OPERATION in facet["expected"]["forbidden_operations"]
        arguments = operation["arguments"]
        assert {"path", "why", "operation"} <= required_params
        assert set(arguments) <= set(params)
        Draft202012Validator(public_edit_operation_schema()).validate(
            arguments["operation"]
        )
        normalized = normalize_edit_surface_arguments(arguments)
        assert normalized["path"] == arguments["path"]
        assert normalized["why"] == arguments["why"]
        assert normalized["operation"]["kind"] == "edit_section"

    facet = next(case for case in facets if case["class"] == "stable_preference")
    arguments = facet["expected"]["operation"]["arguments"]
    broad = copy.deepcopy(facet)
    broad["expected"]["operation"] = {
        "tool": "edit_memory",
        "arguments": {
            "path": arguments["path"],
            "why": "Replace the whole Entity to add one facet.",
            "operation": {
                "kind": "replace_body",
                "new_body": "# Example Person\n\nOne durable facet.\n",
            },
        },
    }
    assert normalize_edit_surface_arguments(
        broad["expected"]["operation"]["arguments"]
    )["operation"]["kind"] == "replace_body"
    with pytest.raises(AssertionError):
        _validate_case(broad)


def test_stable_low_reuse_trivia_kills_the_reusable_value_mutant() -> None:
    trivia = next(
        case
        for case in _fixture()["cases"]
        if case["id"] == "stable-low-reuse-trivia-quiet"
    )
    assert trivia["evidence"]["stability_or_recurrence"] is True
    assert trivia["evidence"]["reusable_comparison_value"] is False
    assert "for years" in trivia["turn"].lower()
    assert trivia["expected"]["route"] == "no_write"

    mutant = copy.deepcopy(trivia)
    mutant["evidence"]["reusable_comparison_value"] = True
    assert _expected_route(mutant) == "compiled_observation"
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
        ("expected.route", "compiled_observation"),
        ("evidence.tentative_or_unresolved", False),
    ):
        mutant = copy.deepcopy(tentative)
        section, field = mutation[0].split(".")
        mutant[section][field] = mutation[1]
        with pytest.raises(AssertionError):
            _validate_case(mutant)


def test_no_write_cases_forbid_every_baseline_write_surface() -> None:
    no_write_cases = [
        case for case in _fixture()["cases"] if case["expected"]["route"] == "no_write"
    ]
    assert no_write_cases
    for case in no_write_cases:
        assert case["expected"]["expected_tools"] == []
        assert {"capture_source", "connect_memory", "observe_memory"} <= set(
            case["expected"]["forbidden_tools"]
        )
        mutant = copy.deepcopy(case)
        mutant["expected"]["forbidden_tools"].remove("capture_source")
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


def _fenced_block_after(path: Path, anchor: str) -> str:
    """Return the copyable instruction block after one exact section anchor."""
    text = path.read_text(encoding="utf-8")
    tail = text.split(anchor, 1)[1]
    opening = re.search(r"```[^\n]*\n", tail)
    assert opening is not None, path
    return tail[opening.end() :].split("```", 1)[0]


def test_every_actual_copyable_instruction_fits_the_web_byte_budget() -> None:
    prominence_doc = ROOT / "docs" / "prominence.md"
    quickstart = _fenced_block_after(
        ROOT / "QUICKSTART.md",
        "### Make the KB proactive in the Claude app (custom instructions)",
    )
    # QUICKSTART explicitly calls its first paragraph optional tone guidance;
    # the Exomem paragraph is the standing instruction users paste into the
    # separately capped custom-instructions field.
    quickstart_exomem = "I keep" + quickstart.split("I keep", 1)[1]
    blocks = {
        "prominence maximal": _fenced_block_after(
            prominence_doc, "### Maximal — recommended for web and hosted"
        ),
        "prominence balanced": _fenced_block_after(
            prominence_doc, "### Balanced — the default where hooks exist"
        ),
        "prominence light": _fenced_block_after(
            prominence_doc, "### Light — when it is getting in the way"
        ),
        "prominence off": _fenced_block_after(
            prominence_doc, "### Off — explicit invocation only"
        ),
        "assistant guide": _fenced_block_after(
            ROOT / "docs" / "ai-assistant-guide.md", "## Copyable instruction block"
        ),
        "quickstart": quickstart_exomem,
    }
    for name, block in blocks.items():
        size = len(block.rstrip().encode("utf-8"))
        assert size <= 1_500, f"{name} is {size} bytes, over the 1,500-byte web cap"


def _assert_baseline_authority_mapping(name: str, block: str) -> None:
    normalized = " ".join(block.lower().split())
    assert re.search(
        r"(?:concise (?:compiled )?observation|narrow (?:additive )?entity facet)"
        r".{0,140}proactive_capture",
        normalized,
    ), (name, "proactive_capture")
    assert re.search(r"(?:affiliation|accepted) relation.{0,100}link_acceptance", normalized), (
        name,
        "link_acceptance",
    )
    assert re.search(
        r"entity creation.{0,140}(?:substantial curation|structural change).{0,100}"
        r"restructure_execution",
        normalized,
    ), (name, "restructure_execution")


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
        "quickstart copyable instructions": _fenced_block_after(
            ROOT / "QUICKSTART.md",
            "### Make the KB proactive in the Claude app (custom instructions)",
        ),
        "assistant-guide copyable instructions": _fenced_block_after(
            ROOT / "docs" / "ai-assistant-guide.md",
            "## Copyable instruction block",
        ),
        "structural reminder": capture_hook.REMINDER.lower(),
        "prominence balanced": prominence.CONTRACTS["balanced"].capture,
        "prominence maximal": prominence.CONTRACTS["maximal"].capture,
        "maximal docs": _markdown_block(ROOT / "docs" / "prominence.md", "### Maximal", "### Balanced"),
        "balanced docs": _markdown_block(ROOT / "docs" / "prominence.md", "### Balanced", "### Light"),
    }
    for name, block in blocks.items():
        block = " ".join(block.lower().split())
        for marker in _BASELINE_MARKERS:
            assert marker in block, (name, marker)
        _assert_baseline_authority_mapping(name, block)
        for marker in _DELEGATION_MARKERS:
            mutant = block.replace(marker, "", 1)
            with pytest.raises(AssertionError):
                _assert_baseline_authority_mapping(name, mutant)
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
    _assert_baseline_authority_mapping(f"{level}/{profile} bootstrap", capture)
    for marker in _DELEGATION_MARKERS:
        mutant = capture.replace(marker, "", 1)
        with pytest.raises(AssertionError):
            _assert_baseline_authority_mapping(f"{level}/{profile} bootstrap", mutant)
    assert "_memory" not in capture


@pytest.mark.parametrize("level", ("off", "light"))
def test_quiet_profiles_remain_explicit_request_only(level: str) -> None:
    capture = prominence.contract(level).capture.lower()
    assert "only when the user asks" in capture or "explicitly asks" in capture
    assert "stable preference" not in capture


@pytest.mark.parametrize("profile", ("compact", "full", "diagnostics"))
@pytest.mark.parametrize("level", ("balanced", "maximal"))
def test_reduced_surface_keeps_command_free_baseline_authority_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    level: str,
) -> None:
    monkeypatch.setenv("EXOMEM_PROMINENCE", level)
    (tmp_path / "Knowledge Base").mkdir()
    descriptor = ActiveSurfaceDescriptor(
        surface="test",
        profile="baseline-reduced",
        tier2_enabled=False,
        product_commands=("bootstrap", "ask_memory"),
    )
    with active_surface(descriptor):
        capture = commands.op_bootstrap(tmp_path, profile=profile)["engagement"][
            "contract"
        ]["capture"].lower()

    assert "stable preference" in capture
    _assert_baseline_authority_mapping(f"{level}/{profile} reduced bootstrap", capture)
    assert not set(commands.PRODUCT_PUBLIC_NAMES) & set(re.findall(r"\b[a-z_]+\b", capture))


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
