"""Task 1.1 — RED: candidate-first category/kind algebra and bounded opens.

These tests pin OpenSpec change ``restore-indexed-category-recall`` decision 2
(spec: *Candidate-First Category And Kind Algebra*, *Candidate Cost Tracks
Candidate Count*).  They are RED until:

* ``structured_filters.plan_index_candidates(plan)`` exists and classifies a
  compiled plan as ``complete`` (a possibly-empty candidate set) or
  ``unsupported`` — AND narrows by positive category/kind seeds while page /
  NOT / unsupported predicates post-evaluate; every OR branch needs a seed; a
  top-level NOT or page-only expression is ``unsupported``; contradictory
  positive equals prove a complete-empty candidate set.
* ``find`` derives eligible parents for a safe indexed plan from the maintained
  catalog instead of walking + parsing every Markdown parent, so the number of
  parents opened tracks candidate count, not corpus size.

The bounded-opens tests deliberately use a small physical corpus plus an
operation counter and a parametrized filler count as the corpus-size proxy —
per the design, materializing 8,000 real files is unnecessary when the counter
proves the bound.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from exomem import find as find_module
from exomem import freshness, lexstore, semantic_index, structured_filters

needs_fts5 = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="this SQLite build lacks FTS5"
)


@pytest.fixture(autouse=True)
def _fresh_state() -> Any:
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    freshness.clear()
    yield
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    freshness.clear()


def _plan(expression: dict[str, Any]) -> structured_filters.FilterPlan:
    return structured_filters.compile_filter(expression)


# --------------------------------------------------------------------------- #
# Pure algebra: complete(paths) vs unsupported, AND narrowing, OR/NOT rules.
# --------------------------------------------------------------------------- #


def test_exact_category_equality_is_a_complete_positive_seed() -> None:
    algebra = structured_filters.plan_index_candidates(_plan({"unit.category": {"$eq": "config"}}))
    assert algebra.status == "complete"
    assert algebra.definitely_empty is False
    assert algebra.category_seeds == frozenset({"config"})
    assert algebra.kind_seeds is None


def test_membership_predicate_is_a_complete_positive_seed() -> None:
    algebra = structured_filters.plan_index_candidates(
        _plan({"unit.kind": {"$in": ["decision", "requirement"]}})
    )
    assert algebra.status == "complete"
    assert algebra.kind_seeds == frozenset({"decision", "requirement"})


def test_and_narrows_by_seed_and_post_evaluates_page_predicate() -> None:
    # A category seed narrows candidates; page.status is post-evaluated after
    # bounded hydration and must NOT make the plan unsupported.
    algebra = structured_filters.plan_index_candidates(
        _plan(
            {
                "$and": [
                    {"unit.category": {"$eq": "config"}},
                    {"page.status": {"$eq": "active"}},
                ]
            }
        )
    )
    assert algebra.status == "complete"
    assert algebra.category_seeds == frozenset({"config"})
    assert algebra.definitely_empty is False


def test_and_of_two_category_equalities_narrows_to_intersection() -> None:
    algebra = structured_filters.plan_index_candidates(
        _plan(
            {
                "$and": [
                    {"unit.category": {"$in": ["config", "rule"]}},
                    {"unit.category": {"$eq": "config"}},
                ]
            }
        )
    )
    assert algebra.status == "complete"
    assert algebra.category_seeds == frozenset({"config"})


def test_contradictory_category_equalities_prove_complete_empty() -> None:
    # Two exact positive seeds intersect to nothing: the plan stays *complete*
    # with zero candidates so the request returns no hits WITHOUT a scope walk.
    algebra = structured_filters.plan_index_candidates(
        _plan(
            {
                "$and": [
                    {"unit.category": {"$eq": "config"}},
                    {"unit.category": {"$eq": "rule"}},
                ]
            }
        )
    )
    assert algebra.status == "complete"
    assert algebra.definitely_empty is True
    assert algebra.clauses == ()


def test_or_of_seeded_branches_is_complete() -> None:
    algebra = structured_filters.plan_index_candidates(
        _plan(
            {
                "$or": [
                    {"unit.category": {"$eq": "config"}},
                    {"unit.category": {"$eq": "rule"}},
                ]
            }
        )
    )
    assert algebra.status == "complete"
    assert algebra.definitely_empty is False


def test_or_with_page_only_branch_is_unsupported() -> None:
    # One branch without a complete seed makes the whole OR unsupported — the
    # planner must not emit an incomplete category set.
    algebra = structured_filters.plan_index_candidates(
        _plan(
            {
                "$or": [
                    {"unit.category": {"$eq": "config"}},
                    {"page.status": {"$eq": "active"}},
                ]
            }
        )
    )
    assert algebra.status == "unsupported"


def test_top_level_not_is_unsupported() -> None:
    algebra = structured_filters.plan_index_candidates(
        _plan({"$not": {"unit.category": {"$eq": "config"}}})
    )
    assert algebra.status == "unsupported"


def test_page_only_expression_is_unsupported() -> None:
    algebra = structured_filters.plan_index_candidates(_plan({"page.status": {"$eq": "active"}}))
    assert algebra.status == "unsupported"


def test_empty_plan_is_unsupported() -> None:
    # No predicate at all provides no positive seed, so eligibility is not an
    # indexed candidate plan (recall stays on the ordinary keyword path).
    algebra = structured_filters.plan_index_candidates(structured_filters.compile_filter(None))
    assert algebra.status == "unsupported"


# --------------------------------------------------------------------------- #
# Bounded opens: parents opened track candidate count, not corpus size.
# --------------------------------------------------------------------------- #


def _write_page(root: Path, rel_path: str, body: str, *, status: str = "active") -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    page_id = uuid.uuid5(uuid.NAMESPACE_URL, f"category-algebra:{rel_path}")
    path.write_text(
        "---\n"
        "type: insight\n"
        f"title: {path.stem}\n"
        f"exomem_id: {page_id}\n"
        f"status: {status}\n"
        "updated: 2026-07-22\n"
        "---\n\n"
        f"# {path.stem}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _seed_live_freshness(root: Path, paths: list[Path]) -> None:
    vault_entries = [(str(path), freshness.stat_signature(path)) for path in paths]
    kb_entries = [
        entry for entry in vault_entries if Path(entry[0]).is_relative_to(root / "Knowledge Base")
    ]
    freshness.seed(root, "kb", kb_entries)
    freshness.seed(root, "vault", vault_entries)


@needs_fts5
@pytest.mark.parametrize("filler_count", [0, 8, 32])
def test_page_filter_eligibility_opens_only_indexed_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filler_count: int
) -> None:
    """Two identical candidates in growing corpora cost a constant bounded open.

    ``filler_count`` stands in for the 2,000-page and 8,000-page fixtures: the
    number of parents opened must stay equal to the candidate count, and no
    Markdown scope walk may occur for a safe indexed category plan.
    """
    targets = [
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/target-one.md",
            "- [config] first indexed candidate ^t1",
        ),
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/target-two.md",
            "- [config] second indexed candidate ^t2",
        ),
    ]
    pages = list(targets)
    for index in range(filler_count):
        pages.append(
            _write_page(
                tmp_path,
                f"Knowledge Base/Notes/filler-{index:04d}.md",
                f"- [observation] unrelated filler {index} ^f{index}",
            )
        )
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)

    def forbidden_walk(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("safe indexed category eligibility must not walk the corpus")

    opened: list[str] = []
    original_state = semantic_index.current_parent_index_state

    def observed_state(root: Path, path: Path | str, **kwargs: Any) -> Any:
        opened.append(Path(path).as_posix())
        return original_state(root, path, **kwargs)

    monkeypatch.setattr(find_module, "_walk_md", forbidden_walk)
    monkeypatch.setattr(semantic_index, "current_parent_index_state", observed_state)

    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        graph=False,
        result_level="page",
        categories=["config"],
        limit=20,
    )

    assert {hit.path for hit in hits} == {
        "Knowledge Base/Notes/target-one.md",
        "Knowledge Base/Notes/target-two.md",
    }
    # Exactly the two candidate parents are hydrated, independent of corpus size.
    assert sorted(set(opened)) == [
        "Knowledge Base/Notes/target-one.md",
        "Knowledge Base/Notes/target-two.md",
    ]


@needs_fts5
def test_default_scope_auto_widen_keeps_safe_category_plan_indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default KB scope must not reintroduce a vault scan while widening."""
    pages = [
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/in-kb.md",
            "- [config] indexed KB candidate with shared needle ^kb",
        ),
        _write_page(
            tmp_path,
            "Projects/outside.md",
            "- [config] indexed outside candidate with shared needle ^outside",
        ),
        _write_page(
            tmp_path,
            "Projects/unrelated.md",
            "- [observation] unrelated outside filler ^filler",
        ),
    ]
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)

    def forbidden_scan(*_args: Any, **_kwargs: Any) -> set[str]:
        raise AssertionError("safe auto-widen category eligibility must stay indexed")

    monkeypatch.setattr(find_module, "_eligible_filter_paths", forbidden_scan)

    hits = find_module.find(
        tmp_path,
        query="shared needle",
        scope="kb",
        mode="keyword",
        graph=False,
        result_level="page",
        categories=["config"],
        limit=5,
    )

    assert {hit.path for hit in hits} == {
        "Knowledge Base/Notes/in-kb.md",
        "Projects/outside.md",
    }
    assert next(hit for hit in hits if hit.path == "Projects/outside.md").outside_kb


@needs_fts5
def test_complete_empty_intersection_returns_zero_hits_without_a_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = [
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/config-note.md",
            "- [config] present but never both ^c",
        ),
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/rule-note.md",
            "- [rule] present but never both ^r",
        ),
    ]
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)

    def forbidden_walk(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a complete-empty candidate set must not fall back to a scan")

    monkeypatch.setattr(find_module, "_walk_md", forbidden_walk)

    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="page",
        filters={
            "$and": [
                {"unit.category": {"$eq": "config"}},
                {"unit.category": {"$eq": "rule"}},
            ]
        },
        limit=20,
    )

    assert hits == []


@needs_fts5
def test_mixed_axis_or_hydrates_only_branch_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``category=config OR kind=decision`` must remain a union of two
    branch seeds, not flatten to an unconstrained scan of every catalog unit."""
    pages = [
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/config.md",
            "- [config] compact branch candidate ^config",
        ),
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/decision.md",
            "## Decision\n- id: choose-a\n\nChoose branch A.",
        ),
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/unrelated.md",
            "- [observation] unrelated catalog unit ^other",
        ),
    ]
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)

    opened: list[str] = []
    original_state = semantic_index.current_parent_index_state

    def observed_state(root: Path, path: Path | str, **kwargs: Any) -> Any:
        opened.append(Path(path).as_posix())
        return original_state(root, path, **kwargs)

    monkeypatch.setattr(semantic_index, "current_parent_index_state", observed_state)
    store = lexstore.get_store(tmp_path)
    monkeypatch.setattr(
        store,
        "_semantic_unit_hit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("parent candidate lookup must not hydrate semantic-unit rows")
        ),
    )
    monkeypatch.setattr(
        find_module,
        "_walk_md",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("mixed-axis OR must not walk the corpus")
        ),
    )

    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        graph=False,
        result_level="page",
        filters={
            "$or": [
                {"unit.category": {"$eq": "config"}},
                {"unit.kind": {"$eq": "decision"}},
            ]
        },
        limit=20,
    )

    assert {hit.path for hit in hits} == {
        "Knowledge Base/Notes/config.md",
        "Knowledge Base/Notes/decision.md",
    }
    assert "Knowledge Base/Notes/unrelated.md" not in set(opened)


@needs_fts5
def test_correlated_or_does_not_hydrate_cross_product_superset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve correlation between category and kind inside each OR branch."""
    pages = [
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/config-decision.md",
            "## Decision\n- id: config-decision\n- category: config\n\nChoose config A.",
        ),
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/rule-requirement.md",
            "## Requirement\n- id: rule-requirement\n- category: rule\n\nRequire rule B.",
        ),
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/rule-decision-cross-product.md",
            "## Decision\n- id: rule-decision\n- category: rule\n\nThis is only a cross-product match.",
        ),
    ]
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)

    opened: list[str] = []
    original_state = semantic_index.current_parent_index_state

    def observed_state(root: Path, path: Path | str, **kwargs: Any) -> Any:
        opened.append(Path(path).as_posix())
        return original_state(root, path, **kwargs)

    monkeypatch.setattr(semantic_index, "current_parent_index_state", observed_state)

    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        graph=False,
        result_level="page",
        filters={
            "$or": [
                {
                    "$and": [
                        {"unit.category": {"$eq": "config"}},
                        {"unit.kind": {"$eq": "decision"}},
                    ]
                },
                {
                    "$and": [
                        {"unit.category": {"$eq": "rule"}},
                        {"unit.kind": {"$eq": "requirement"}},
                    ]
                },
            ]
        },
        limit=20,
    )

    assert {hit.path for hit in hits} == {
        "Knowledge Base/Notes/config-decision.md",
        "Knowledge Base/Notes/rule-requirement.md",
    }
    assert "Knowledge Base/Notes/rule-decision-cross-product.md" not in set(opened)


@needs_fts5
def test_unit_level_mixed_axis_or_hydrates_only_branch_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit lane must consume the same branch-preserving clauses as pages."""
    pages = [
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/config-unit.md",
            "- [config] compact branch candidate ^config-unit",
        ),
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/decision-unit.md",
            "## Decision\n- id: choose-unit\n\nChoose the unit branch.",
        ),
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/unrelated-unit.md",
            "- [observation] unrelated unit candidate ^unrelated-unit",
        ),
    ]
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)

    opened: list[str] = []
    original_state = semantic_index.current_parent_index_state

    def observed_state(root: Path, path: Path | str, **kwargs: Any) -> Any:
        opened.append(Path(path).as_posix())
        return original_state(root, path, **kwargs)

    monkeypatch.setattr(semantic_index, "current_parent_index_state", observed_state)

    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="unit",
        filters={
            "$or": [
                {"unit.category": {"$eq": "config"}},
                {"unit.kind": {"$eq": "decision"}},
            ]
        },
        limit=20,
    )

    assert {hit.parent_path for hit in hits} == {
        "Knowledge Base/Notes/config-unit.md",
        "Knowledge Base/Notes/decision-unit.md",
    }
    assert "Knowledge Base/Notes/unrelated-unit.md" not in set(opened)


@needs_fts5
@pytest.mark.parametrize("mode", ["keyword", "vector"])
def test_unit_exact_candidates_are_post_filtered_before_limit(
    tmp_path: Path, mode: str
) -> None:
    """A page post-filter must see exact candidates beyond the ranking window."""
    pages = [
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/a-active.md",
            "- [constraint] the older eligible unit ^active",
            status="active",
        )
    ]
    for index in range(21):
        pages.append(
            _write_page(
                tmp_path,
                f"Knowledge Base/Notes/z-inactive-{index:02d}.md",
                f"- [constraint] newer ineligible unit {index} ^inactive-{index}",
                status="draft",
            )
        )
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)

    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode=mode,
        result_level="unit",
        filters={
            "$and": [
                {"unit.category": {"$eq": "constraint"}},
                {"page.status": {"$eq": "active"}},
            ]
        },
        limit=1,
    )

    assert [hit.parent_path for hit in hits] == ["Knowledge Base/Notes/a-active.md"]


@needs_fts5
def test_vector_unit_exact_candidates_are_post_filtered_before_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vector top-k window cannot precede canonical exact post-filters."""
    pages = [
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/a-active-vector.md",
            "- [constraint] eligible vector needle ^active-vector",
            status="active",
        )
    ]
    for index in range(21):
        pages.append(
            _write_page(
                tmp_path,
                f"Knowledge Base/Notes/z-draft-vector-{index:02d}.md",
                f"- [constraint] ineligible vector needle {index} ^draft-vector-{index}",
                status="draft",
            )
        )
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)

    plan = _plan(
        {
            "$and": [
                {"unit.category": {"$eq": "constraint"}},
                {"page.status": {"$eq": "active"}},
            ]
        }
    )
    algebra = structured_filters.plan_index_candidates(plan)
    snapshot = find_module.FreshnessSnapshot(tmp_path)
    catalog = lexstore.search_semantic_units_result(
        tmp_path,
        "vector needle",
        k=2_147_483_647,
        clauses=algebra.clauses,
        scope="kb",
        freshness=snapshot.kb(),
    )
    assert catalog.readiness.complete
    ranked = [
        SimpleNamespace(
            unit_ref=row.unit_ref,
            parent_path=row.parent_path,
            parent_generation=row.parent_generation,
            source_order=row.source_order,
            cosine=1.0 - index / 1000,
        )
        for index, row in enumerate(catalog.value or [])
    ]
    assert len(ranked) == 22

    def fake_vector_candidates(
        _root: Path,
        *,
        candidate_limit: int,
        allowed_unit_refs: set[str] | None,
        **_kwargs: Any,
    ) -> tuple[list[Any], dict[str, str], str]:
        assert allowed_unit_refs == {hit.unit_ref for hit in ranked}
        return ranked[:candidate_limit], {"status": "participated"}, "kb"

    monkeypatch.setattr(find_module, "_vector_unit_candidates", fake_vector_candidates)

    hits = find_module.find(
        tmp_path,
        query="vector needle",
        scope="kb-only",
        mode="vector",
        result_level="unit",
        filters={
            "$and": [
                {"unit.category": {"$eq": "constraint"}},
                {"page.status": {"$eq": "active"}},
            ]
        },
        limit=1,
    )

    assert [hit.parent_path for hit in hits] == [
        "Knowledge Base/Notes/a-active-vector.md"
    ]


@needs_fts5
def test_unit_level_correlated_or_never_hydrates_cross_product_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = [
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/config-decision-unit.md",
            "## Decision\n- id: config-decision-unit\n- category: config\n\nChoose config A.",
        ),
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/rule-requirement-unit.md",
            "## Requirement\n- id: rule-requirement-unit\n- category: rule\n\nRequire rule B.",
        ),
        _write_page(
            tmp_path,
            "Knowledge Base/Notes/rule-decision-unit.md",
            "## Decision\n- id: rule-decision-unit\n- category: rule\n\nCross-product only.",
        ),
    ]
    _seed_live_freshness(tmp_path, pages)
    lexstore.ensure_fresh(tmp_path)

    opened: list[str] = []
    original_state = semantic_index.current_parent_index_state

    def observed_state(root: Path, path: Path | str, **kwargs: Any) -> Any:
        opened.append(Path(path).as_posix())
        return original_state(root, path, **kwargs)

    monkeypatch.setattr(semantic_index, "current_parent_index_state", observed_state)

    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="unit",
        filters={
            "$or": [
                {
                    "$and": [
                        {"unit.category": {"$eq": "config"}},
                        {"unit.kind": {"$eq": "decision"}},
                    ]
                },
                {
                    "$and": [
                        {"unit.category": {"$eq": "rule"}},
                        {"unit.kind": {"$eq": "requirement"}},
                    ]
                },
            ]
        },
        limit=20,
    )

    assert {hit.parent_path for hit in hits} == {
        "Knowledge Base/Notes/config-decision-unit.md",
        "Knowledge Base/Notes/rule-requirement-unit.md",
    }
    assert "Knowledge Base/Notes/rule-decision-unit.md" not in set(opened)
