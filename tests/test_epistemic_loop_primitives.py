"""Epistemic loop primitives: the prediction kind, verdict, and check_by.

Verdict is *state*, not supersession: a refuted unit keeps active standing and
full rank. There is no stored confidence — verdict is categorical, never a
number. And `observe_memory` must never drop an authored metadata row it does
not own, which is this change's load-bearing assertion.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    commands,
    note,
    semantic_blocks,
    semantic_language_registry,
    semantic_units,
    set_frontmatter_field,
)
from exomem import (
    find as find_module,
)
from exomem import (
    observe_memory as observe_module,
)
from exomem import (
    vault as vault_module,
)
from exomem.governance import egress as egress_module
from exomem.structured_filters import (
    FilterError,
    compile_filter,
    evaluate_filter,
    unit_view,
)

TODAY = dt.date(2026, 8, 15)
PAGE_ID = "00000000-0000-4000-8000-0000000000f1"
PAGE = "Knowledge Base/Notes/Insights/loop-primitives.md"
_FIXTURE_SOURCE = (
    "Knowledge Base/Sources/Articles/2026-06-02-postgres-autovacuum-tuning"
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _parse(body: str) -> semantic_units.SemanticUnitDocument:
    return semantic_units.parse_semantic_units(body, path="fixture.md")


def _only_unit(body: str) -> semantic_units.SemanticUnit:
    document = _parse(body)
    assert document.errors == (), [error.code for error in document.errors]
    assert len(document.units) == 1
    return document.units[0]


def _codes(document: semantic_units.SemanticUnitDocument) -> list[str]:
    return [error.code for error in document.errors]


def _page_source(body: str) -> str:
    return (
        "---\n"
        "title: Loop primitives\n"
        "type: insight\n"
        "status: active\n"
        f"exomem_id: {PAGE_ID}\n"
        "updated: 2026-08-15\n"
        "---\n\n"
        f"{body.rstrip()}\n"
    )


def _write_page(root: Path, body: str, *, rel: str = PAGE) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_page_source(body), encoding="utf-8")
    return path


def _fake_page(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "rel_path": PAGE,
        "title": "Loop primitives",
        "page_type": "insight",
        "status": "active",
        "updated": "2026-08-15",
        "superseded_by": [],
        "snapshot_hash": "snapshot",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# --------------------------------------------------------------------------
# 1. semantic-unit-language: the prediction kind
# --------------------------------------------------------------------------


def test_prediction_heading_parses_as_a_governed_rich_kind() -> None:
    unit = _only_unit("## Prediction\n\nRetry budgets will stop the incident class.\n")

    assert unit.form == "rich"
    assert unit.kind == "prediction"
    assert "prediction" in semantic_blocks.BLOCK_TYPES


def test_plural_prediction_heading_resolves_to_the_singular_kind() -> None:
    unit = _only_unit("## Predictions\n\nRetry budgets will stop the incident class.\n")

    assert unit.kind == "prediction"


def test_prediction_resolves_from_the_core_registry_without_a_vault_entry() -> None:
    resolution = semantic_language_registry.core_registry().resolve_kind("prediction")

    assert resolution.resolved == "prediction"
    assert resolution.status == "core"
    assert resolution.definition is not None


def test_registry_extension_may_not_shadow_the_prediction_kind() -> None:
    findings = semantic_language_registry.validate_proposal(
        {
            "schema_version": semantic_language_registry.SCHEMA_VERSION,
            "categories": {},
            "kinds": {"prediction": {"description": "A local prediction kind."}},
        }
    )

    collisions = [item for item in findings if item["code"] == "canonical_collision"]
    assert collisions, findings
    assert collisions[0]["path"] == "kinds.prediction"
    assert collisions[0]["severity"] == "error"


# --------------------------------------------------------------------------
# 2. semantic-unit-language: verdict grammar
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "authored,expected",
    [
        ("confirmed", "confirmed"),
        ("Refuted", "refuted"),
        ("  QUALIFIED  ", "qualified"),
        ("inconclusive", "inconclusive"),
        ("Abandoned", "abandoned"),
    ],
)
def test_verdict_values_normalize_onto_the_unit(authored: str, expected: str) -> None:
    unit = _only_unit(
        f"## Prediction\n- verdict: {authored}\n\nRetry budgets hold.\n"
    )

    assert unit.verdict == expected
    assert unit.to_dict()["verdict"] == expected


def test_verdict_vocabulary_is_the_five_audit_approved_values() -> None:
    assert semantic_units.EPISTEMIC_OUTCOMES == (
        "abandoned",
        "confirmed",
        "inconclusive",
        "qualified",
        "refuted",
    )


def test_unknown_verdict_value_is_a_source_addressed_error() -> None:
    document = _parse(
        "## Prediction\n- verdict: probably-wrong\n\nRetry budgets hold.\n"
    )

    assert "invalid_rich_verdict" in _codes(document)
    error = next(
        item for item in document.errors if item.code == "invalid_rich_verdict"
    )
    assert error.line == 2
    assert error.raw == "- verdict: probably-wrong"


def test_numeric_verdict_is_refused_with_the_no_confidence_reason() -> None:
    document = _parse("## Prediction\n- verdict: 0.7\n\nRetry budgets hold.\n")

    error = next(
        item for item in document.errors if item.code == "invalid_rich_verdict"
    )
    assert "confidence" in error.remediation.casefold()
    assert "refuted" in error.remediation


def test_a_refuted_unit_keeps_ordinary_active_standing() -> None:
    unit = _only_unit("## Prediction\n- verdict: refuted\n\nRetry budgets hold.\n")

    hit = find_module._semantic_unit_hit(
        _fake_page(), unit, bm25_rank=None, bm25_score=None
    )

    assert hit.parent_status == "active"
    assert hit.parent_superseded_by == []
    assert hit.as_dict().get("superseded_by") is None


# --------------------------------------------------------------------------
# 3. semantic-unit-language: check_by grammar
# --------------------------------------------------------------------------


def test_check_by_accepts_a_strict_iso_calendar_date() -> None:
    unit = _only_unit(
        "## Prediction\n- check_by: 2026-11-01\n\nRetry budgets hold.\n"
    )

    assert unit.check_by == "2026-11-01"
    assert unit.to_dict()["check_by"] == "2026-11-01"


@pytest.mark.parametrize("authored", ["2026-1-1", "2026-11-01T09:00:00Z", "soon", ""])
def test_check_by_rejects_anything_but_a_calendar_date(authored: str) -> None:
    document = _parse(
        f"## Prediction\n- check_by: {authored}\n\nRetry budgets hold.\n"
    )

    assert "invalid_rich_check_by" in _codes(document)


# --------------------------------------------------------------------------
# 4. semantic-unit-language: reserved, rich-form-only
# --------------------------------------------------------------------------


def test_governed_metadata_alone_is_still_an_empty_rich_unit() -> None:
    document = _parse(
        "## Prediction\n- verdict: refuted\n- check_by: 2026-11-01\n"
    )

    assert "empty_rich_unit" in _codes(document)
    assert {"verdict", "check_by"} <= semantic_blocks._RESERVED_METADATA_KEYS


def test_compact_observations_carry_no_governed_metadata() -> None:
    unit = _only_unit("## Observations\n\n- [risk] Retry storms recur #ops\n")

    assert unit.form == "compact"
    assert unit.verdict is None
    assert unit.check_by is None


def test_the_governed_key_list_agrees_across_the_language_and_the_writer() -> None:
    """One declaration, so a future key cannot be reserved but not preserved."""
    governed = set(semantic_units.GOVERNED_UNIT_METADATA_KEYS)

    assert governed == {"verdict", "check_by"}
    assert governed <= semantic_blocks._RESERVED_METADATA_KEYS
    assert governed <= observe_module._OWNED_METADATA_KEYS


# --------------------------------------------------------------------------
# 5. semantic-write-contract
# --------------------------------------------------------------------------


def _blocking_codes(validation: dict) -> set[str]:
    return {
        finding["code"]
        for finding in validation["contract_result"]["blocking_findings"]
    }


def test_a_prediction_alone_satisfies_the_minimum_unit_rule(vault: Path) -> None:
    validation = commands.op_manage_memory_file(
        vault,
        operation="create",
        path="Knowledge Base/Notes/Insights/prediction-only.md",
        content=(
            "# Prediction only\n\n## Prediction\n\n"
            "Retry budgets will stop the incident class within a quarter.\n"
        ),
        frontmatter={"type": "insight", "status": "active"},
        validate_only=True,
    )

    codes = _blocking_codes(validation)
    assert "missing_semantic_unit" not in codes
    assert "empty_rich_unit" not in codes
    assert validation["contract_result"]["rich_unit_count"] == 1


def test_a_prediction_without_a_verdict_raises_no_finding(vault: Path) -> None:
    validation = commands.op_manage_memory_file(
        vault,
        operation="create",
        path="Knowledge Base/Notes/Insights/prediction-unjudged.md",
        content=(
            "# Prediction unjudged\n\n## Prediction\n- check_by: 2026-11-01\n\n"
            "Retry budgets will stop the incident class within a quarter.\n"
        ),
        frontmatter={"type": "insight", "status": "active"},
        validate_only=True,
    )

    # `RELATION_DISPOSITION_MISSING` is the orthogonal relation-review
    # obligation, not a unit-coverage finding; it is deliberately not asserted
    # away here.
    codes = _blocking_codes(validation)
    assert "missing_semantic_unit" not in codes
    assert "empty_rich_unit" not in codes
    assert "invalid_rich_verdict" not in codes
    assert not any(
        "prediction" in str(finding.get("message", "")).casefold()
        for finding in validation["contract_result"]["blocking_findings"]
    )


def test_adding_a_verdict_does_not_change_the_minimum_unit_outcome(
    vault: Path,
) -> None:
    body = (
        "# Judged\n\n## Prediction\n{rows}\n"
        "Retry budgets will stop the incident class within a quarter.\n"
    )
    without = commands.op_manage_memory_file(
        vault,
        operation="create",
        path="Knowledge Base/Notes/Insights/verdict-absent.md",
        content=body.format(rows=""),
        frontmatter={"type": "insight", "status": "active"},
        validate_only=True,
    )
    with_verdict = commands.op_manage_memory_file(
        vault,
        operation="create",
        path="Knowledge Base/Notes/Insights/verdict-present.md",
        content=body.format(rows="- verdict: refuted\n"),
        frontmatter={"type": "insight", "status": "active"},
        validate_only=True,
    )

    assert _blocking_codes(without) == _blocking_codes(with_verdict)
    assert "missing_semantic_unit" not in _blocking_codes(with_verdict)
    assert (
        without["contract_result"]["rich_unit_count"]
        == with_verdict["contract_result"]["rich_unit_count"]
        == 1
    )


# --------------------------------------------------------------------------
# 6. observe_memory: preserve-by-default (the load-bearing assertion)
# --------------------------------------------------------------------------


def _add_prediction(root: Path, **kwargs: object) -> dict:
    payload: dict[str, object] = {
        "operation": "add",
        "category": "reliability",
        "content": "Retry budgets will stop the incident class.",
        "kind": "prediction",
    }
    payload.update(kwargs)
    return commands.op_observe_memory(root, path=PAGE, **payload)


def test_content_only_update_never_drops_an_authored_verdict(tmp_path: Path) -> None:
    """The change's load-bearing assertion: reconstruction may not lose a row."""
    page = _write_page(tmp_path, "# Loop primitives\n\nExisting prose.\n")
    added = _add_prediction(tmp_path, verdict="refuted", check_by="2026-11-01")
    assert added["unit"]["verdict"] == "refuted"

    updated = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="update",
        category="reliability",
        content="Retry budgets did not stop the incident class.",
        kind="prediction",
        unit_ref=added["unit_ref"],
        expected_fingerprint=added["unit"]["fingerprint"],
        expected_hash=added["after_hash"],
    )

    source = page.read_text(encoding="utf-8")
    assert "- verdict: refuted" in source
    assert "- check_by: 2026-11-01" in source
    assert updated["unit"]["verdict"] == "refuted"
    assert updated["unit"]["check_by"] == "2026-11-01"


def test_an_uninterpreted_authored_metadata_row_survives_an_update(
    tmp_path: Path,
) -> None:
    page = _write_page(
        tmp_path,
        "# Loop primitives\n\n"
        "## Prediction\n"
        "- category: reliability\n"
        "- id: retry-budget\n"
        "- reviewer: someone\n"
        "\n"
        "Retry budgets will stop the incident class.\n",
    )
    document = semantic_units.parse_semantic_units(
        page.read_text(encoding="utf-8"),
        path=PAGE,
        parent_ref=f"exomem://memory/{PAGE_ID}",
    )
    unit = next(item for item in document.units if item.anchor == "retry-budget")

    updated = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="update",
        category="reliability",
        content="Retry budgets did not stop the incident class.",
        kind="prediction",
        unit_ref=unit.unit_ref,
        expected_fingerprint=unit.fingerprint,
        expected_hash=vault_module.content_hash(page.read_text(encoding="utf-8")),
    )

    source = page.read_text(encoding="utf-8")
    assert "- reviewer: someone" in source
    assert updated["unit"]["metadata"]["reviewer"] == "someone"


def test_an_invalid_existing_governed_row_is_never_dropped_silently(
    tmp_path: Path,
) -> None:
    page = _write_page(
        tmp_path,
        "# Loop primitives\n\n"
        "## Prediction\n"
        "- category: reliability\n"
        "- id: retry-budget\n"
        "- verdict: probably-wrong\n"
        "\n"
        "Retry budgets will stop the incident class.\n",
    )
    document = semantic_units.parse_semantic_units(
        page.read_text(encoding="utf-8"),
        path=PAGE,
        parent_ref=f"exomem://memory/{PAGE_ID}",
    )
    unit = next(item for item in document.units if item.anchor == "retry-budget")

    with pytest.raises(ValueError) as caught:
        commands.op_observe_memory(
            tmp_path,
            path=PAGE,
            operation="update",
            category="reliability",
            content="Retry budgets did not stop the incident class.",
            kind="prediction",
            unit_ref=unit.unit_ref,
            expected_fingerprint=unit.fingerprint,
            expected_hash=vault_module.content_hash(
                page.read_text(encoding="utf-8")
            ),
        )

    assert "INVALID_EXISTING_UNIT_METADATA" in str(caught.value)
    assert "- verdict: probably-wrong" in page.read_text(encoding="utf-8")


def test_governed_arguments_render_replace_and_clear(tmp_path: Path) -> None:
    page = _write_page(tmp_path, "# Loop primitives\n\nExisting prose.\n")
    added = _add_prediction(tmp_path, verdict="inconclusive", check_by="2026-11-01")

    source = page.read_text(encoding="utf-8")
    assert "- verdict: inconclusive" in source
    assert "- check_by: 2026-11-01" in source

    replaced = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="update",
        category="reliability",
        content="Retry budgets will stop the incident class.",
        kind="prediction",
        verdict="confirmed",
        unit_ref=added["unit_ref"],
        expected_fingerprint=added["unit"]["fingerprint"],
        expected_hash=added["after_hash"],
    )
    assert replaced["unit"]["verdict"] == "confirmed"
    assert replaced["unit"]["check_by"] == "2026-11-01"

    cleared = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="update",
        category="reliability",
        content="Retry budgets will stop the incident class.",
        kind="prediction",
        verdict="",
        unit_ref=replaced["unit_ref"],
        expected_fingerprint=replaced["unit"]["fingerprint"],
        expected_hash=replaced["after_hash"],
    )
    assert cleared["unit"]["verdict"] is None
    assert "- verdict:" not in page.read_text(encoding="utf-8")


def test_remove_still_accepts_only_the_reference_and_drift_guards(
    tmp_path: Path,
) -> None:
    _write_page(tmp_path, "# Loop primitives\n\nExisting prose.\n")
    added = _add_prediction(tmp_path, verdict="refuted")

    with pytest.raises(ValueError) as caught:
        commands.op_observe_memory(
            tmp_path,
            path=PAGE,
            operation="remove",
            verdict="confirmed",
            unit_ref=added["unit_ref"],
            expected_fingerprint=added["unit"]["fingerprint"],
            expected_hash=added["after_hash"],
        )

    assert "INVALID_OBSERVE_INPUT" in str(caught.value)


def test_governed_metadata_without_a_rich_kind_is_refused(tmp_path: Path) -> None:
    _write_page(tmp_path, "# Loop primitives\n\nExisting prose.\n")

    with pytest.raises(ValueError) as caught:
        commands.op_observe_memory(
            tmp_path,
            path=PAGE,
            operation="add",
            category="reliability",
            content="Retry budgets will stop the incident class.",
            verdict="refuted",
        )

    assert "COMPACT_METADATA_REQUIRES_RICH_KIND" in str(caught.value)


def test_an_invalid_verdict_argument_is_refused(tmp_path: Path) -> None:
    _write_page(tmp_path, "# Loop primitives\n\nExisting prose.\n")

    with pytest.raises(ValueError) as caught:
        _add_prediction(tmp_path, verdict="0.7")

    assert "INVALID_SEMANTIC_VERDICT" in str(caught.value)


def test_an_invalid_check_by_argument_is_refused(tmp_path: Path) -> None:
    _write_page(tmp_path, "# Loop primitives\n\nExisting prose.\n")

    with pytest.raises(ValueError) as caught:
        _add_prediction(tmp_path, check_by="2026-1-1")

    assert "INVALID_SEMANTIC_CHECK_BY" in str(caught.value)


def test_an_explicit_anchor_is_honoured_end_to_end(tmp_path: Path) -> None:
    page = _write_page(tmp_path, "# Loop primitives\n\nExisting prose.\n")

    added = _add_prediction(tmp_path, id="retry-budget-prediction")

    assert "- id: retry-budget-prediction" in page.read_text(encoding="utf-8")
    assert added["unit_ref"].endswith("#retry-budget-prediction")
    assert added["unit"]["anchor"] == "retry-budget-prediction"


def test_an_invalid_anchor_argument_is_refused(tmp_path: Path) -> None:
    _write_page(tmp_path, "# Loop primitives\n\nExisting prose.\n")

    with pytest.raises(ValueError) as caught:
        _add_prediction(tmp_path, id="not a valid anchor")

    assert "INVALID_SEMANTIC_ANCHOR" in str(caught.value)


def test_a_colliding_anchor_argument_is_refused(tmp_path: Path) -> None:
    _write_page(tmp_path, "# Loop primitives\n\nExisting prose.\n")
    _add_prediction(tmp_path, id="retry-budget-prediction")

    with pytest.raises(ValueError) as caught:
        _add_prediction(
            tmp_path,
            id="retry-budget-prediction",
            content="A different prediction entirely.",
        )

    assert "DUPLICATE_SEMANTIC_ANCHOR" in str(caught.value)


def test_round_trip_assertion_catches_a_dropped_preserved_row() -> None:
    """The guard itself, not only its effect: a lost row must raise."""
    rendered = observe_module._render_unit(
        kind="prediction",
        category="reliability",
        content="Retry budgets hold.",
        tags=(),
        context=None,
        relations=(),
        anchor="retry-budget",
        verdict="refuted",
        preserved=(("reviewer", "someone"),),
    )
    unit = _only_unit(f"{rendered}\n")

    observe_module._assert_round_trip(
        unit,
        kind="prediction",
        category="reliability",
        content="Retry budgets hold.",
        tags=(),
        context=None,
        relations=(),
        anchor="retry-budget",
        verdict="refuted",
        preserved=(("reviewer", "someone"),),
    )

    with pytest.raises(observe_module.ObserveMemoryError) as caught:
        observe_module._assert_round_trip(
            unit,
            kind="prediction",
            category="reliability",
            content="Retry budgets hold.",
            tags=(),
            context=None,
            relations=(),
            anchor="retry-budget",
            verdict="refuted",
            preserved=(),
        )

    assert caught.value.code == "AMBIGUOUS_SEMANTIC_UNIT_CONTENT"


def test_governed_arguments_are_registered_on_every_generated_surface() -> None:
    command = next(
        item for item in commands.PRODUCT_COMMANDS if item.name == "observe_memory"
    )
    names = [param.name for param in command.params]

    assert {"verdict", "check_by", "id"} <= set(names)
    assert {"mcp", "rest", "cli"} <= set(command.surfaces)

    schemas = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "fixtures"
            / "mcp_tool_schemas.json"
        ).read_text(encoding="utf-8")
    )
    properties = schemas["observe_memory"]["inputSchema"]["properties"]
    assert {"verdict", "check_by", "id"} <= set(properties)
    assert "preserve-by-default" in schemas["observe_memory"]["description"]


# --------------------------------------------------------------------------
# 7. structured-retrieval-filters
# --------------------------------------------------------------------------


def _unit_dict(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "category": "reliability",
        "category_key": "reliability",
        "kind": "prediction",
        "tags": [],
        "context": None,
        "form": "rich",
    }
    values.update(overrides)
    return values


def _page_dict() -> dict[str, object]:
    return {
        "status": "active",
        "type": "insight",
        "project": [],
        "tags": [],
        "speakers": [],
        "file_type": "note",
        "updated": dt.date(2026, 8, 15),
        "frontmatter": {},
    }


def test_verdict_predicate_compiles_and_matches_casefolded() -> None:
    plan = compile_filter({"unit.verdict": {"$eq": "Refuted"}})

    assert plan.has_unit_predicate is True
    assert evaluate_filter(
        plan, page=_page_dict(), unit=_unit_dict(verdict="refuted")
    )
    assert not evaluate_filter(
        plan, page=_page_dict(), unit=_unit_dict(verdict="confirmed")
    )


def test_absent_verdict_is_distinguishable_by_exists() -> None:
    plan = compile_filter({"unit.verdict": {"$exists": False}})

    assert evaluate_filter(plan, page=_page_dict(), unit=_unit_dict())
    assert not evaluate_filter(
        plan, page=_page_dict(), unit=_unit_dict(verdict="refuted")
    )


def test_unknown_unit_field_stays_rejected() -> None:
    with pytest.raises(FilterError) as caught:
        compile_filter({"unit.confidence": {"$eq": "high"}})

    assert caught.value.code == "INVALID_FILTER_FIELD"


def test_non_string_verdict_operand_is_refused() -> None:
    with pytest.raises(FilterError) as caught:
        compile_filter({"unit.verdict": {"$eq": 1}})

    assert caught.value.code == "INVALID_FILTER_VALUE"


def test_check_by_supports_ordered_due_by_comparison() -> None:
    plan = compile_filter({"unit.check_by": {"$lte": "2026-11-01"}})

    assert evaluate_filter(
        plan, page=_page_dict(), unit=_unit_dict(check_by=dt.date(2026, 10, 1))
    )
    assert not evaluate_filter(
        plan, page=_page_dict(), unit=_unit_dict(check_by=dt.date(2026, 12, 1))
    )


def test_check_by_refuses_a_non_date_operand() -> None:
    with pytest.raises(FilterError) as caught:
        compile_filter({"unit.check_by": {"$eq": "soon"}})

    assert caught.value.code == "INVALID_FILTER_VALUE"


def test_check_by_refuses_substring_comparison() -> None:
    with pytest.raises(FilterError) as caught:
        compile_filter({"unit.check_by": {"$contains": "2026"}})

    assert caught.value.code == "INVALID_FILTER_OPERATOR"


def test_unit_view_normalizes_check_by_and_omits_absent_governed_keys() -> None:
    judged = _only_unit(
        "## Prediction\n- verdict: refuted\n- check_by: 2026-11-01\n\nBody.\n"
    )
    unjudged = _only_unit("## Prediction\n\nBody.\n")

    judged_view = unit_view(judged)
    assert judged_view["verdict"] == "refuted"
    assert judged_view["check_by"] == dt.date(2026, 11, 1)

    unjudged_view = unit_view(unjudged)
    assert "verdict" not in unjudged_view
    assert "check_by" not in unjudged_view


def test_a_due_by_filter_is_decidable_against_a_parsed_unit() -> None:
    unit = _only_unit("## Prediction\n- check_by: 2026-10-01\n\nBody.\n")
    plan = compile_filter({"unit.check_by": {"$lte": "2026-11-01"}})

    assert evaluate_filter(plan, page=_page_dict(), unit=unit_view(unit))


def _recall_page(root: Path, name: str, body: str) -> Path:
    path = root / "Knowledge Base" / "Notes" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        f"title: {name}\n"
        f"exomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, f'exomem-loop:{name}')}\n"
        "status: active\n"
        "updated: 2026-08-15\n"
        "---\n\n"
        f"# {name}\n\n{body.rstrip()}\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    return path


def test_recall_filters_and_returns_governed_metadata_end_to_end(
    tmp_path: Path,
) -> None:
    """The whole point, exercised through `find`: judged units are addressable."""
    _recall_page(
        tmp_path,
        "refuted-prediction",
        "## Prediction\n"
        "- id: refuted-one\n"
        "- check_by: 2026-10-01\n"
        "- verdict: refuted\n"
        "\n"
        "Retry budgets will stop the incident class.\n",
    )
    _recall_page(
        tmp_path,
        "unjudged-prediction",
        "## Prediction\n"
        "- id: unjudged-one\n"
        "\n"
        "Connection pooling will stop the incident class.\n",
    )

    refuted = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="unit",
        filters={"unit.verdict": {"$eq": "refuted"}},
        limit=10,
    )

    assert [hit.source_anchor for hit in refuted] == ["refuted-one"]
    payload = refuted[0].as_dict()
    assert payload["verdict"] == "refuted"
    assert payload["check_by"] == "2026-10-01"
    assert payload["parent_status"] == "active"

    due = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="unit",
        filters={"unit.check_by": {"$lte": "2026-11-01"}},
        limit=10,
    )
    assert [hit.source_anchor for hit in due] == ["refuted-one"]

    unjudged = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="unit",
        filters={"unit.verdict": {"$exists": False}},
        limit=10,
    )
    assert [hit.source_anchor for hit in unjudged] == ["unjudged-one"]
    assert "verdict" not in unjudged[0].as_dict()


# --------------------------------------------------------------------------
# 8. semantic-unit-retrieval: hit payload
# --------------------------------------------------------------------------


def test_unit_hit_carries_governed_metadata_when_present() -> None:
    unit = _only_unit(
        "## Prediction\n- verdict: refuted\n- check_by: 2026-11-01\n\nBody.\n"
    )

    hit = find_module._semantic_unit_hit(
        _fake_page(), unit, bm25_rank=None, bm25_score=None
    )

    assert hit.as_dict()["verdict"] == "refuted"
    assert hit.as_dict()["check_by"] == "2026-11-01"
    assert hit.as_compact_dict()["verdict"] == "refuted"


def test_unit_hit_omits_governed_metadata_when_absent() -> None:
    unit = _only_unit("## Prediction\n\nBody.\n")

    hit = find_module._semantic_unit_hit(
        _fake_page(), unit, bm25_rank=None, bm25_score=None
    )

    assert "verdict" not in hit.as_dict()
    assert "check_by" not in hit.as_dict()
    assert "verdict" not in hit.as_compact_dict()


def test_governed_egress_registers_both_metadata_fields() -> None:
    assert {"verdict", "check_by"} <= egress_module._UNIT_FIELDS


def test_a_verdict_changes_no_ranking_signal() -> None:
    judged = _only_unit("## Prediction\n- verdict: refuted\n\nBody.\n")
    unjudged = _only_unit("## Prediction\n\nBody.\n")

    judged_hit = find_module._semantic_unit_hit(
        _fake_page(), judged, bm25_rank=3, bm25_score=1.5
    )
    unjudged_hit = find_module._semantic_unit_hit(
        _fake_page(), unjudged, bm25_rank=3, bm25_score=1.5
    )

    assert judged_hit.as_dict()["signals"] == unjudged_hit.as_dict()["signals"]


def test_page_status_still_governs_a_judged_unit() -> None:
    unit = _only_unit("## Prediction\n- verdict: confirmed\n\nBody.\n")

    hit = find_module._semantic_unit_hit(
        _fake_page(status="superseded", superseded_by=["Knowledge Base/Notes/x.md"]),
        unit,
        bm25_rank=None,
        bm25_score=None,
    )

    assert hit.parent_status == "superseded"
    assert hit.as_dict()["parent_superseded_by"] == ["Knowledge Base/Notes/x.md"]


# --------------------------------------------------------------------------
# 9. note-type-contract
# --------------------------------------------------------------------------


def test_concluded_is_an_accepted_experiment_status() -> None:
    assert "concluded" in note.STATUS_EXPERIMENT
    assert "archived" in note.STATUS_EXPERIMENT
    assert note.STATUS_EXPERIMENT.index("concluded") != note.STATUS_EXPERIMENT.index(
        "archived"
    )


def test_experiment_outcome_shares_the_unit_verdict_vocabulary() -> None:
    assert note.EXPERIMENT_OUTCOME_VALUES == semantic_units.EPISTEMIC_OUTCOMES


def _experiment_note(vault: Path, *, status: str, title: str) -> object:
    return note.note(
        vault,
        content=(
            f"# {title}\n\n## Observations\n\n"
            "- [finding] Batching cut context switches #workflow\n"
        ),
        note_type="experiment",
        title=title,
        status=status,
        domain="workflow",
        started="2026-07-01",
        duration="30 days",
        sources=[_FIXTURE_SOURCE],
        today=TODAY,
        validate_only=True,
    )


def test_a_concluded_experiment_is_accepted(vault: Path) -> None:
    validation = _experiment_note(vault, status="concluded", title="Batching review")

    assert validation.mutated is False
    assert "status: concluded" in validation.source


def test_an_unknown_experiment_status_is_still_refused(vault: Path) -> None:
    with pytest.raises(note.NoteError) as caught:
        _experiment_note(vault, status="finished", title="Batching review two")

    assert "status" in caught.value.missing
    assert "concluded" in caught.value.reason


_EXPERIMENT_ID = "00000000-0000-4000-8000-0000000000f2"
_EXPERIMENT_PAGE = "Knowledge Base/Notes/Experiments/Workflow/2026-07-outcome.md"


def _experiment_page(vault: Path) -> str:
    path = vault / _EXPERIMENT_PAGE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "title: Outcome fixture\n"
        "type: experiment\n"
        f"exomem_id: {_EXPERIMENT_ID}\n"
        "domain: workflow\n"
        "status: active\n"
        "created: 2026-07-01\n"
        "updated: 2026-07-01\n"
        "started: 2026-07-01\n"
        'duration: "30 days"\n'
        f'sources:\n  - "[[{_FIXTURE_SOURCE}]]"\n'
        "tags: [workflow]\n"
        "---\n\n"
        "# Outcome fixture\n\n"
        "## Observations\n\n"
        "- [finding] Batching cut context switches #workflow\n",
        encoding="utf-8",
    )
    return _EXPERIMENT_PAGE


def test_a_valid_experiment_outcome_is_accepted(vault: Path) -> None:
    path = _experiment_page(vault)

    result = set_frontmatter_field.set_frontmatter_field(
        vault,
        path=path,
        field="outcome",
        value="Refuted",
        why="Recording the experiment result.",
        today=TODAY,
    )

    # An accepted spelling is normalized on the way in, so the file never holds
    # two spellings of one state.
    assert result.new_value == "refuted"
    assert "outcome: refuted" in (vault / path).read_text(encoding="utf-8")


def test_an_invalid_experiment_outcome_is_refused(vault: Path) -> None:
    path = _experiment_page(vault)

    with pytest.raises(set_frontmatter_field.SetFrontmatterError) as caught:
        set_frontmatter_field.set_frontmatter_field(
            vault,
            path=path,
            field="outcome",
            value="mostly-right",
            why="Recording the experiment result.",
            today=TODAY,
        )

    assert caught.value.code == "INVALID_OUTCOME"
    assert "refuted" in caught.value.reason


def test_outcome_belongs_only_to_experiments(vault: Path) -> None:
    with pytest.raises(set_frontmatter_field.SetFrontmatterError) as caught:
        set_frontmatter_field.set_frontmatter_field(
            vault,
            path="Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization.md",
            field="outcome",
            value="confirmed",
            why="Recording an outcome.",
            today=TODAY,
        )

    assert caught.value.code == "INVALID_OUTCOME"
    assert "experiment" in caught.value.reason


def test_confidence_remains_a_refused_frontmatter_field(vault: Path) -> None:
    path = _experiment_page(vault)

    with pytest.raises(set_frontmatter_field.SetFrontmatterError) as caught:
        set_frontmatter_field.set_frontmatter_field(
            vault,
            path=path,
            field="confidence",
            value=0.7,
            why="Trying to store a credence.",
            today=TODAY,
        )

    assert caught.value.code == "EXCLUDED_FIELD"


# --------------------------------------------------------------------------
# 10. shipped teaching
# --------------------------------------------------------------------------


_SCAFFOLD = Path(__file__).resolve().parents[1] / "src" / "exomem" / "_scaffold"
_PLUGIN = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "claude-code"
    / "skills"
    / "exomem"
)


@pytest.mark.parametrize("name", ["frontmatter.md", "page-types.md"])
def test_scaffold_and_plugin_references_stay_byte_identical(name: str) -> None:
    scaffold = (_SCAFFOLD / "_Schema" / "references" / name).read_bytes()
    plugin = (_PLUGIN / "references" / name).read_bytes()

    assert scaffold == plugin


def test_shipped_frontmatter_reference_teaches_the_loop_contract() -> None:
    text = (_SCAFFOLD / "_Schema" / "references" / "frontmatter.md").read_text(
        encoding="utf-8"
    )

    assert "concluded" in text
    assert "`outcome`" in text
    assert "confirmed" in text and "refuted" in text
    assert "`confidence`" in text


def test_shipped_page_type_reference_teaches_the_loop_primitives() -> None:
    text = (_SCAFFOLD / "_Schema" / "references" / "page-types.md").read_text(
        encoding="utf-8"
    )

    assert "## Prediction" in text
    assert "verdict" in text
    assert "check_by" in text


def test_the_shipped_prediction_example_actually_parses_clean() -> None:
    """Teaching that does not parse is worse than no teaching.

    This caught a first draft that used an ungoverned `refutes:` relation, which
    would have handed every reader an `unsupported_relation` error.
    """
    text = (_SCAFFOLD / "_Schema" / "references" / "page-types.md").read_text(
        encoding="utf-8"
    )
    example = next(
        block
        for block in text.split("```markdown\n")[1:]
        if block.startswith("## Prediction")
    ).split("```", 1)[0]

    document = semantic_units.parse_semantic_units(example, path="page-types.md")

    assert document.errors == (), [error.code for error in document.errors]
    kinds = {unit.kind for unit in document.units}
    assert "prediction" in kinds
    prediction = next(unit for unit in document.units if unit.kind == "prediction")
    assert prediction.verdict == "refuted"
    assert prediction.check_by == "2026-08-01"
