from __future__ import annotations

import pytest
from test_upkeep_egress_twin import _external, _withhold

from exomem import epistemic_graph, file_watcher, lexstore, semantic_index
from exomem.governance.principal import request_scope
from exomem.review_dimensions import ReviewDimensions, ReviewTarget


@pytest.fixture
def subjects(vault, monkeypatch):
    path = "Knowledge Base/Notes/Insights/review-subject.md"
    empty = "Knowledge Base/Notes/Insights/review-empty.md"
    (vault / path).write_text(
        "---\ntype: insight\nexomem_id: 16ca97b6-174e-40fa-8d69-412a10b5ad33\n---\n"
        "## Claim\n- id: credited\n- asserted_by: The project steward\n\nA measured result.\n\n"
        "## Open question\n- id: unsettled\n\nWhat changes the result?\n",
    )
    (vault / empty).write_text("---\ntype: insight\n---\n# Empty subject\n")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "auto")
    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)
    assert lexstore.ensure_fresh(vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return vault, path, empty


def test_page_filter_does_not_join_dimensions_from_distinct_units(subjects):
    root, path, _empty = subjects
    display = ReviewDimensions(root).classify([ReviewTarget(path)])[0]
    assert display.status == "known"
    assert {"attribution", "uncertainty"} <= set(display.dimensions)

    expression = {"$and": [
        {"unit.dimension": {"$eq": "attribution"}},
        {"unit.dimension": {"$eq": "uncertainty"}},
    ]}
    result = ReviewDimensions(root, expression).classify([ReviewTarget(path)])[0]
    assert result.matches is False


def test_unit_target_cannot_inherit_its_siblings_dimension(subjects):
    root, path, _empty = subjects
    selected = semantic_index.selected_parent_index_state(root, path)
    claim, question = selected.document.units
    adapter = ReviewDimensions(root, {"unit.dimension": {"$eq": "uncertainty"}})
    results = adapter.classify([ReviewTarget(path, claim.unit_ref), ReviewTarget(path, question.unit_ref)])
    assert [result.matches for result in results] == [False, True]
    assert "uncertainty" not in results[0].dimensions
    assert "uncertainty" in results[1].dimensions


def test_unknown_unit_address_and_empty_page_do_not_match_negative_filters(subjects):
    root, path, empty = subjects
    adapter = ReviewDimensions(root, {"$not": {"unit.dimension": {"$eq": "attribution"}}})
    results = adapter.classify([
        ReviewTarget(path, "exomem://memory/16ca97b6-174e-40fa-8d69-412a10b5ad33#missing"),
        ReviewTarget(empty), ReviewTarget(None),
    ])
    assert results[0].status == "unknown" and results[0].matches is None
    assert "dimensions" not in results[0].fields()
    assert results[1].status == "known" and results[1].dimensions == () and results[1].matches is False
    assert results[2].status == "not_applicable" and results[2].matches is False


def test_withheld_subject_does_not_enter_dimension_coverage(subjects):
    root, hidden, visible = subjects
    _withhold(root, hidden)
    with request_scope(_external()):
        adapter = ReviewDimensions(root, {"unit.dimension": {"$eq": "kind"}})
        both = adapter.classify([ReviewTarget(visible), ReviewTarget(hidden)])
        absent = adapter.classify([ReviewTarget(visible)])
    assert both[1] is None
    assert adapter.coverage(both) == adapter.coverage(absent)


def test_review_filter_rejects_other_axes_before_loading_a_subject(tmp_path):
    from exomem.structured_filters import FilterError

    with pytest.raises(FilterError) as error:
        ReviewDimensions(tmp_path, {"$or": [{"page.type": {"$eq": "insight"}}]})
    assert error.value.code == "UNSUPPORTED_FILTER_FIELD"


def test_attention_does_not_turn_a_missing_partitioned_unit_address_into_page_scope(subjects, monkeypatch):
    from exomem import attention, audit, commands

    root, path, _empty = subjects
    finding = audit.AuditFinding(
        "question_aging", "info", path, "Review this unresolved question.",
        meta={"review_partition": "question-signal", "unit_ref": None},
    )
    monkeypatch.setattr(attention.audit_module, "audit", lambda *args, **kwargs: audit.AuditReport(
        findings=[finding], summary={"question_aging": 1},
    ))
    shown = commands.op_review_memory(root, categories=["question_aging"])
    assert shown["items"][0]["dimension_status"] == "unknown"
    assert "dimensions" not in shown["items"][0]
    filtered = commands.op_review_memory(
        root, categories=["question_aging"], filters={"unit.dimension": {"$ne": "attribution"}},
    )
    assert filtered["items"] == []
    assert filtered["dimension_coverage"]["filter_unresolved"] == 1
