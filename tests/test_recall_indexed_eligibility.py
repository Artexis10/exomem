"""Structured-filter eligibility must resolve from a maintained index.

Lane 2 of ``accelerate-governed-recall``. Today every page-level filter field
(`projects`, `tags`, `types`, speakers, file types, status, source kind,
domain, updated) is ``unsupported`` to ``plan_index_candidates``, so a filtered
recall routes to ``find._eligible_filter_paths`` — the canonical full-scan
oracle, which enumerates the Markdown scope and parses every page's frontmatter
on the reader thread. That is the 18.1 s ``filter_eligibility`` stage the
proposal measured on the live cell.

The contract governs the MANAGED reader, exactly as Lane 1's sentinel does: an
offline/CLI caller keeps its exact source-walk fallback by design, so every
assertion here is taken against a warm managed cell (registry seeded,
catalogue published, admission ready).

Two things are asserted separately, because they fail independently:

* **structural** — the managed eligibility path never calls the scan oracle and
  never enumerates the scope. The oracle seam is replaced by a raiser rather
  than counted, so the assertion cannot pass by the oracle merely being cheap.
* **identity** — the index-backed answer is the *same set* the oracle returns
  for the same generation, for every field that gains a seed. The oracle is
  the definition of correctness; an index that is fast and wrong is worse than
  the walk.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from exomem import commands, derived_receipts, freshness, lexstore, readiness, structured_filters
from exomem import find as find_module
from exomem.derived_receipts import DerivedBatchPath, DerivedComponent
from exomem.vault import kb_dirname

pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="SQLite build lacks FTS5"
)

_REQUIRED = frozenset({DerivedComponent.LEXSTORE, DerivedComponent.MEMORY_REFS})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _scope_roots(vault: Path) -> tuple[Path, ...]:
    """The directories the read-path contract forbids enumerating (Lane 1)."""
    return (vault, vault / kb_dirname())


def _plan(expr: dict[str, Any] | None, **shortcuts: Any) -> structured_filters.FilterPlan:
    return structured_filters.compile_filter(
        expr,
        shortcuts=structured_filters.FilterShortcuts(**shortcuts),
    )


def _oracle(vault: Path, plan: structured_filters.FilterPlan, *, scope: str = "kb") -> set[str]:
    """The canonical full-scan evaluation — the definition of correctness."""
    return find_module._eligible_filter_paths(vault, scope=scope, plan=plan)


def _candidates(
    vault: Path, plan: structured_filters.FilterPlan, *, scope: str = "kb"
) -> set[str]:
    """The candidate set the catalogue query returns, before evaluation."""
    return find_module._eligibility_candidate_paths(
        vault,
        scope=scope,
        eligibility=structured_filters.plan_index_eligibility(plan),
        freshness=find_module.FreshnessSnapshot(vault).for_scope(scope),
    )


def _indexed(vault: Path, plan: structured_filters.FilterPlan, *, scope: str = "kb") -> set[str]:
    """Eligibility as the managed read path resolves it."""
    return find_module._resolve_eligible_filter_paths(
        vault,
        scope=scope,
        plan=plan,
        snapshot=find_module.FreshnessSnapshot(vault),
    )


def _refuse(*_args: Any, **_kwargs: Any) -> set[str]:
    raise AssertionError(
        "the managed read path called the full-scan oracle "
        "(_eligible_filter_paths) to answer a structured filter"
    )


@contextmanager
def _oracle_withdrawn() -> Iterator[None]:
    """Replace the scan oracle with a raiser for the duration of the block.

    ``_eligible_filter_paths`` IS the function that answers a filter by reading
    pages. Counting its cost would let a cheap walk pass; withdrawing it makes
    "resolved from the catalogue" a structural fact rather than a measurement.
    A context manager rather than a fixture, because the identity test needs
    the real oracle to compute its reference and the raiser to compute the
    answer under test.
    """
    real = find_module._eligible_filter_paths
    find_module._eligible_filter_paths = _refuse  # type: ignore[assignment]
    try:
        yield
    finally:
        find_module._eligible_filter_paths = real  # type: ignore[assignment]


@pytest.fixture
def no_oracle():
    """The same withdrawal for a whole test."""
    with _oracle_withdrawn():
        yield


def _filtered_recall(vault: Path, *, query: str = "metabolism", **kwargs: Any) -> dict:
    """One real timed recall through the public leaf."""
    return commands.op_find(
        vault,
        query=query,
        mode="hybrid",
        scope="kb-only",
        graph=False,
        include_timings=True,
        **kwargs,
    )


def _paths(result: dict) -> set[str]:
    return {hit["path"] for hit in result["hits"]}


def _eligibility_source(result: dict) -> str:
    stage = result["timings"]["stages"]["filter_eligibility"]
    return stage["source"]


# --------------------------------------------------------------------------- #
# Structural: the managed path resolves from the catalogue
# --------------------------------------------------------------------------- #


def test_projects_filter_resolves_from_the_catalogue_without_reading_pages(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """The measured 18.1 s stage: a `projects` filter must come from an index."""
    warm_managed_cell(vault)
    plan = _plan(None, projects=("project-alpha",))
    expected = _oracle(vault, plan)
    assert expected, "the fixture corpus must carry project-alpha pages, or this proves nothing"
    sentinel = walk_sentinel(*_scope_roots(vault))

    sentinel.reset()
    with _oracle_withdrawn():
        result = _filtered_recall(vault, projects=["project-alpha"])
        actual = _indexed(vault, plan)

    assert sentinel.count == 0, sentinel.report()
    assert _eligibility_source(result) == "index"
    assert actual == expected


def test_tags_and_types_filters_resolve_from_the_catalogue(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """`tags` and `types` are page-level list/scalar axes; both must seed."""
    warm_managed_cell(vault)
    cases = (
        ({"tags": ["retrieval"]}, _plan(None, tags=("retrieval",))),
        ({"types": ["insight"]}, _plan(None, types=("insight",))),
    )
    expected = {label["tags"][0] if "tags" in label else label["types"][0]: _oracle(vault, plan)
                for label, plan in cases}
    sentinel = walk_sentinel(*_scope_roots(vault))

    for kwargs, plan in cases:
        key = kwargs.get("tags", kwargs.get("types"))[0]
        assert expected[key], f"{kwargs} must select pages in the fixture corpus"
        sentinel.reset()
        with _oracle_withdrawn():
            result = _filtered_recall(vault, **kwargs)
            actual = _indexed(vault, plan)
        assert sentinel.count == 0, f"{kwargs}: {sentinel.report()}"
        assert _eligibility_source(result) == "index", kwargs
        assert actual == expected[key], kwargs


def test_page_and_unit_clauses_compose_on_the_index(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """An AND of a page clause and a unit clause stays on the index.

    The unit half already seeds from the semantic-unit sidecar; before this
    lane the page half made the whole plan unsupported and dragged the unit
    half onto the walk with it.
    """
    warm_managed_cell(vault)
    expr = {
        "$and": [
            {"page.type": {"$eq": "insight"}},
            {"unit.category": {"$exists": True}},
        ]
    }
    expected = _oracle(vault, _plan(expr))
    sentinel = walk_sentinel(*_scope_roots(vault))

    sentinel.reset()
    with _oracle_withdrawn():
        # `result_level="page"` drives the page eligibility seam this lane
        # owns. `auto` would resolve to "unit" for any plan carrying a unit
        # predicate and route through `_eligible_unit_records`, which repeats
        # the candidate classification independently and is not this lane's.
        result = _filtered_recall(vault, filters=expr, result_level="page")
        actual = _indexed(vault, _plan(expr))

    assert sentinel.count == 0, sentinel.report()
    assert _eligibility_source(result) == "index"
    assert actual == expected


# --------------------------------------------------------------------------- #
# Identity: the index answers exactly what the oracle answers
# --------------------------------------------------------------------------- #

#: Frontmatter shapes the shipped fixture vault does not carry. The identity
#: assertion is only as strong as the corpus it runs on: a value that is
#: already lower-cased cannot catch a writer that forgot to canonicalize, and a
#: scalar-only corpus cannot catch a seed that drops `type: [insight, pattern]`
#: — which is exactly the defect the first round shipped.
_ADVERSARIAL: dict[str, str] = {
    # list-valued declarations on the SCALAR axes: the evaluator matches these
    # member-wise for `$in`, and refuses them for `$eq`.
    "Notes/Adv/type-list.md": "type: [insight, pattern]\nstatus: active\nupdated: 2026-06-01\n",
    "Notes/Adv/status-list.md": "type: insight\nstatus: [active, draft]\nupdated: 2026-06-02\n",
    "Notes/Adv/source-list.md": "type: source\nsource_type: [article, book]\nstatus: active\nupdated: 2026-06-03\n",
    "Notes/Adv/domain-list.md": "type: insight\nstatus: active\ndomain: [engineering, biology]\nupdated: 2026-06-04\n",
    # mixed case and surrounding whitespace on every seeded axis.
    "Notes/Adv/case-scalars.md": "type: Insight\nstatus: Active\nsource_type: Article\ndomain: Engineering\nupdated: 2026-06-05\n",
    "Notes/Adv/spaced-scalars.md": "type: '  insight  '\nstatus: '  active  '\nsource_type: '  article  '\nupdated: 2026-06-06\n",
    "Notes/Adv/case-lists.md": "type: pattern\nstatus: active\ntags: [Retrieval, FUSION]\nspeakers: [Ada Lovelace]\nproject: Alpha-One\nupdated: 2026-06-07\n",
    "Notes/Adv/spaced-lists.md": "type: pattern\nstatus: active\ntags: ['  retrieval  ']\nspeakers: ['  Ada Lovelace  ']\nupdated: 2026-06-08\n",
    # declaration shapes: null, empty, scalar-where-a-list-is-expected, absent.
    "Notes/Adv/type-null.md": "type: null\nstatus: active\nupdated: 2026-06-09\n",
    "Notes/Adv/type-int.md": "type: 7\nstatus: active\nupdated: 2026-06-10\n",
    "Notes/Adv/tags-scalar.md": "type: insight\nstatus: active\ntags: retrieval\nupdated: 2026-06-11\n",
    "Notes/Adv/tags-empty.md": "type: insight\nstatus: active\ntags: []\nupdated: 2026-06-12\n",
    "Notes/Adv/proj-both-keys.md": "type: insight\nstatus: active\nproject: alpha-one\nprojects: [beta-two]\nupdated: 2026-06-13\n",
    "Notes/Adv/proj-absent.md": "type: insight\nstatus: active\nupdated: 2026-06-14\n",
    # temporal shapes: bare day, quoted day, instant, garbage, captured-only.
    "Notes/Adv/upd-instant.md": "type: insight\nstatus: active\nupdated: 2026-06-15T23:30:00Z\n",
    # Earlier in the SAME day than the precise bound below, so a day-granular
    # column admits it while the evaluator does not. That gap is what makes an
    # ordered comparison against a precise bound inexact — and inexactness is
    # the whole reason a complement may not be taken.
    "Notes/Adv/upd-instant-early.md": "type: insight\nstatus: active\nupdated: 2026-06-15T06:00:00Z\n",
    "Notes/Adv/upd-garbage.md": "type: insight\nstatus: active\nupdated: not-a-date\n",
    "Notes/Adv/upd-absent.md": "type: insight\nstatus: active\n",
    "Notes/Adv/upd-captured.md": "type: insight\nstatus: active\ncaptured: 2026-06-16\n",
}

#: A governed unit carrying both axes the first round refused outright, plus a
#: COMPACT unit whose tag is authored in mixed case. `_rich_tags` casefolds at
#: parse, so a rich unit can never show whether the writer canonicalizes; the
#: compact form keeps the authored spelling, and it is the only shape that can.
_GOVERNED_UNIT = (
    "---\ntype: insight\nstatus: active\nupdated: 2026-06-17\n---\n\n"
    "## Prediction\n\n- id: p1\n- verdict: Supported\n- check_by: 2026-08-01\n\n"
    "The catalogue answers the filter without reading the page.\n\n"
    "## Observations\n\n- [constraint] Eligibility resolves from columns #Retrieval\n"
)

#: A scene-frame child and the video parent it collapses into. The parent
#: carries the seeded clause; the child must come back with it through the
#: catalogue's UNION arm, exactly as the walk oracle returns both identities.
_VIDEO_PARENT = "Notes/Adv/Media/talk.md"
_VIDEO_FRAME = "Notes/Adv/Media/talk-frame-0005.md"


def _seed_adversarial(vault: Path) -> None:
    kb = vault / kb_dirname()
    body = "\n# Adversarial\n\nBody about metabolism, retrieval and postgres.\n"
    for rel, front in _ADVERSARIAL.items():
        target = kb / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"---\n{front}---\n{body}", encoding="utf-8")
    governed = kb / "Notes/Adv/governed-unit.md"
    governed.parent.mkdir(parents=True, exist_ok=True)
    governed.write_text(_GOVERNED_UNIT, encoding="utf-8")
    parent = kb / _VIDEO_PARENT
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        "---\ntype: source\nsource_type: video\nstatus: active\nmedia_type: video\n"
        "project: frame-project\nupdated: 2026-06-18\n---\n" + body,
        encoding="utf-8",
    )
    (kb / _VIDEO_FRAME).write_text(
        "---\ntype: source\nparent_media: "
        + f"{kb_dirname()}/{_VIDEO_PARENT.removesuffix('.md')}\n"
        "evidence_file: frame\nframe_ts: 5.0\nstatus: active\nupdated: 2026-06-18\n---\n"
        + body,
        encoding="utf-8",
    )


#: Every field the 0.2 inventory lists as index-answerable, with the operators
#: this lane seeds. Model-free by construction: eligibility is metadata only,
#: so no embedding, ranking or query text enters any of these.
_IDENTITY_CASES: tuple[tuple[str, dict[str, Any] | None, dict[str, Any]], ...] = (
    ("page.project/$in", None, {"projects": ("project-alpha",)}),
    ("page.project/$in case", None, {"projects": ("Alpha-One",)}),
    ("page.project/$in multi", None, {"projects": ("project-alpha", "infrastructure")}),
    ("page.project/$all", {"page.project": {"$all": ["project-alpha", "project-beta"]}}, {}),
    ("page.project/$contains", {"page.project": {"$contains": "alpha-one"}}, {}),
    ("page.project/$exists", {"page.project": {"$exists": True}}, {}),
    ("page.project/$exists false", {"page.project": {"$exists": False}}, {}),
    ("page.tags/$in", None, {"tags": ("retrieval",)}),
    ("page.tags/$in case", None, {"tags": ("RETRIEVAL",)}),
    ("page.tags/$in spaced", None, {"tags": ("  retrieval  ",)}),
    ("page.tags/$all", {"page.tags": {"$all": ["retrieval", "fusion"]}}, {}),
    ("page.tags/$contains", {"page.tags": {"$contains": "Retrieval"}}, {}),
    ("page.tags/$exists", {"page.tags": {"$exists": True}}, {}),
    ("page.tags/$exists false", {"page.tags": {"$exists": False}}, {}),
    ("page.type/$eq", {"page.type": {"$eq": "insight"}}, {}),
    ("page.type/$eq case", {"page.type": {"$eq": "Insight"}}, {}),
    ("page.type/$eq spaced", {"page.type": {"$eq": "  insight  "}}, {}),
    ("page.type/$eq null", {"page.type": {"$eq": None}}, {}),
    ("page.type/$in list-valued", None, {"types": ("insight",)}),
    ("page.type/$in multi", None, {"types": ("insight", "pattern")}),
    ("page.type/$ne", {"page.type": {"$ne": "insight"}}, {}),
    ("page.type/$ne null", {"page.type": {"$ne": None}}, {}),
    ("page.type/$contains", {"page.type": {"$contains": "sight"}}, {}),
    ("page.type/$exists", {"page.type": {"$exists": True}}, {}),
    ("page.type/$exists false", {"page.type": {"$exists": False}}, {}),
    ("page.status/$eq", {"page.status": {"$eq": "active"}}, {}),
    ("page.status/$in list-valued", {"page.status": {"$in": ["active", "draft"]}}, {}),
    ("page.status/$ne", {"page.status": {"$ne": "archived"}}, {}),
    ("page.speakers/$in", None, {"speakers": ("ada lovelace",)}),
    ("page.speakers/$in case", None, {"speakers": ("Ada Lovelace",)}),
    ("page.speakers/$exists", {"page.speakers": {"$exists": True}}, {}),
    ("page.file_type/$eq", {"page.file_type": {"$eq": "note"}}, {}),
    ("page.file_type/$in", None, {"file_types": ("note",)}),
    ("page.file_type/$not $in", None, {"exclude_file_types": ("note",)}),
    ("page.source_kind/$eq", {"page.source_kind": {"$eq": "article"}}, {}),
    ("page.source_kind/$eq case", {"page.source_kind": {"$eq": "Article"}}, {}),
    ("page.source_kind/$in list-valued", None, {"source_kinds": ("article", "book")}),
    ("page.domain/$eq", {"page.domain": {"$eq": "engineering"}}, {}),
    ("page.domain/$eq case", {"page.domain": {"$eq": "Engineering"}}, {}),
    ("page.domain/$in list-valued", None, {"domains": ("engineering",)}),
    ("page.updated/$gt", {"page.updated": {"$gt": "2026-06-10"}}, {}),
    ("page.updated/$gte", {"page.updated": {"$gte": "2026-06-01"}}, {}),
    ("page.updated/$lt", {"page.updated": {"$lt": "2026-06-10"}}, {}),
    ("page.updated/$lte", {"page.updated": {"$lte": "2026-06-10"}}, {}),
    ("page.updated/$gte instant", {"page.updated": {"$gte": "2026-06-15T12:00:00Z"}}, {}),
    ("page.updated/$between", {"page.updated": {"$between": ["2026-05-01", "2026-06-30"]}}, {}),
    ("page.updated/$eq", {"page.updated": {"$eq": "2026-06-14"}}, {}),
    ("page.updated/$in", {"page.updated": {"$in": ["2026-06-14", "2026-06-16"]}}, {}),
    ("page.updated/$ne", {"page.updated": {"$ne": "2026-06-14"}}, {}),
    ("page.updated/$exists", {"page.updated": {"$exists": True}}, {}),
    ("page.updated/$exists false", {"page.updated": {"$exists": False}}, {}),
    ("page.updated/updated_after", None, {"updated_after": "2026-06-01"}),
    ("page.updated/updated_before", None, {"updated_before": "2026-06-10"}),
    ("page.updated/recency_days", None, {"recency_days": 3650}),
    ("unit.category/$eq", {"unit.category": {"$eq": "constraint"}}, {}),
    ("unit.category_key/$eq", {"unit.category_key": {"$eq": "constraint"}}, {}),
    ("unit.kind/$exists", {"unit.kind": {"$exists": True}}, {}),
    ("unit.kind/$exists false", {"unit.kind": {"$exists": False}}, {}),
    ("unit.form/$eq", {"unit.form": {"$eq": "compact"}}, {}),
    ("unit.form/$ne", {"unit.form": {"$ne": "compact"}}, {}),
    ("unit.tags/$in", {"unit.tags": {"$in": ["retrieval"]}}, {}),
    ("unit.tags/$in case", {"unit.tags": {"$in": ["Retrieval"]}}, {}),
    ("unit.context/$exists", {"unit.context": {"$exists": True}}, {}),
    ("unit.verdict/$eq", {"unit.verdict": {"$eq": "supported"}}, {}),
    ("unit.verdict/$eq case", {"unit.verdict": {"$eq": "Supported"}}, {}),
    ("unit.verdict/$in", {"unit.verdict": {"$in": ["supported", "refuted"]}}, {}),
    ("unit.verdict/$exists", {"unit.verdict": {"$exists": True}}, {}),
    ("unit.verdict/$exists false", {"unit.verdict": {"$exists": False}}, {}),
    ("unit.check_by/$lt", {"unit.check_by": {"$lt": "2026-12-01"}}, {}),
    ("unit.check_by/$gte", {"unit.check_by": {"$gte": "2026-01-01"}}, {}),
    ("unit.check_by/$between", {"unit.check_by": {"$between": ["2026-01-01", "2026-12-31"]}}, {}),
    ("unit.check_by/$exists", {"unit.check_by": {"$exists": True}}, {}),
    (
        "AND page+unit",
        {"$and": [{"page.type": {"$eq": "insight"}}, {"unit.category": {"$exists": True}}]},
        {},
    ),
    (
        "AND page+seeded unit",
        {"$and": [{"page.status": {"$eq": "active"}}, {"unit.category": {"$eq": "constraint"}}]},
        {},
    ),
    (
        "AND two unit axes on one row",
        {"$and": [{"unit.category": {"$eq": "constraint"}}, {"unit.form": {"$eq": "compact"}}]},
        {},
    ),
    (
        "OR page+page",
        {"$or": [{"page.type": {"$eq": "insight"}}, {"page.tags": {"$in": ["postgres"]}}]},
        {},
    ),
    (
        "OR page+unit",
        {"$or": [{"page.status": {"$eq": "active"}}, {"unit.category": {"$eq": "constraint"}}]},
        {},
    ),
    ("NOT page", {"$not": {"page.type": {"$eq": "insight"}}}, {}),
    ("NOT page $in", {"$not": {"page.tags": {"$in": ["retrieval"]}}}, {}),
    ("NOT unit", {"$not": {"unit.category": {"$eq": "constraint"}}}, {}),
    (
        "NOT of OR",
        {"$not": {"$or": [{"page.type": {"$eq": "insight"}}, {"page.type": {"$eq": "pattern"}}]}},
        {},
    ),
    (
        "AND with NOT",
        {"$and": [{"page.status": {"$eq": "active"}}, {"$not": {"page.tags": {"$in": ["retrieval"]}}}]},
        {},
    ),
    (
        "contradiction",
        {"$and": [{"page.type": {"$eq": "insight"}}, {"page.type": {"$eq": "pattern"}}]},
        {},
    ),
    ("scene-frame parent clause", None, {"projects": ("frame-project",)}),
)


def test_index_result_equals_the_oracle_for_every_seeded_field(
    vault: Path, warm_managed_cell
) -> None:
    """The index answer IS the scan answer, field by field, on one generation.

    Model-free: no query text, no embeddings, no ranking — eligibility is a set
    of identities decided by metadata alone, so this compares sets and nothing
    else. A field whose index answer diverges from the oracle names itself.
    """
    _seed_adversarial(vault)
    warm_managed_cell(vault)

    plans = [(label, _plan(expr, **shortcuts)) for label, expr, shortcuts in _IDENTITY_CASES]
    # The reference is taken FIRST, with the real oracle. Everything after this
    # runs with the oracle withdrawn, so a comparison can never be satisfied by
    # both sides being the same walk.
    expected = {label: _oracle(vault, plan) for label, plan in plans}

    divergences: list[str] = []
    covered: set[str] = set()
    with _oracle_withdrawn():
        for label, plan in plans:
            actual = _indexed(vault, plan)
            covered.add(label.split("/", 1)[0])
            if actual != expected[label]:
                divergences.append(
                    f"{label}: index-only={sorted(actual - expected[label])} "
                    f"oracle-only={sorted(expected[label] - actual)}"
                )
        # Non-vacuity: at least one case must actually select a proper subset,
        # or every comparison above could be "everything equals everything".
        narrowing = _indexed(vault, _plan(None, projects=("project-alpha",)))
        everything = _indexed(vault, _plan({"page.file_type": {"$exists": True}}))

    assert not divergences, "\n".join(divergences)
    assert narrowing and narrowing < everything
    # The scene-frame child comes back with its parent, through the UNION arm.
    frames = _indexed(vault, _plan(None, projects=("frame-project",)))
    assert f"{kb_dirname()}/{_VIDEO_FRAME}" in frames, sorted(frames)
    # Every index-answerable field in the 0.2 inventory is exercised.
    assert covered >= {
        "page.project",
        "page.tags",
        "page.type",
        "page.status",
        "page.speakers",
        "page.file_type",
        "page.source_kind",
        "page.domain",
        "page.updated",
        "unit.category",
        "unit.category_key",
        "unit.kind",
        "unit.form",
        "unit.tags",
        "unit.context",
        "unit.verdict",
        "unit.check_by",
    }, sorted(covered)


# --------------------------------------------------------------------------- #
# Generation binding, custody, and the closed field vocabulary
# --------------------------------------------------------------------------- #


def test_candidate_hydration_is_bounded_by_the_answer(
    vault: Path, warm_managed_cell
) -> None:
    """The managed arm reads the pages it returns, never the corpus.

    The walk sentinel counts directory enumerations; this counts page
    hydrations, which is what the spec sentence "SHALL NOT evaluate the plan by
    reading page frontmatter on the reader thread" actually forbids. A seed
    that narrows nothing satisfies the sentinel and still parses every page in
    the scope — the same cost reached by a different road.

    Every shape below is one the columns must decide in SQL: a negation, a
    complement, a presence test, and a governed unit axis. The bound asserted
    is `reads <= |answer|`, not a constant, so it stays meaningful as the
    corpus grows.
    """
    for index in range(60):
        target = vault / kb_dirname() / f"Notes/Bulk/bulk-{index:03d}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"---\ntype: insight\nstatus: active\nproject: bulk-{index % 5}\n"
            f"tags: [retrieval]\nupdated: 2026-07-{(index % 28) + 1:02d}\n---\n\n"
            "# Bulk\n\nBody about metabolism.\n",
            encoding="utf-8",
        )
    warm_managed_cell(vault)
    corpus = find_module.FreshnessSnapshot(vault).for_scope("kb")[0]
    assert corpus > 60, corpus

    from exomem import semantic_index

    shapes = (
        ("narrowing $in", _plan(None, projects=("bulk-1",))),
        ("$ne, no positive seed", _plan({"page.status": {"$ne": "archived"}})),
        ("top-level $not", _plan({"$not": {"page.type": {"$eq": "insight"}}})),
        ("$exists only", _plan({"page.tags": {"$exists": True}})),
        ("unit.tags $in", _plan({"unit.tags": {"$in": ["retrieval"]}})),
    )
    report: list[str] = []
    for label, plan in shapes:
        reads = {"pages": 0, "units": 0}
        real_resolve = find_module._resolve_page
        real_state = semantic_index.current_parent_index_state

        def _count_page(*args: Any, _real=real_resolve, _at=reads, **kwargs: Any):
            _at["pages"] += 1
            return _real(*args, **kwargs)

        def _count_unit(*args: Any, _real=real_state, _at=reads, **kwargs: Any):
            _at["units"] += 1
            return _real(*args, **kwargs)

        find_module.reset_page_and_result_caches()
        find_module._resolve_page = _count_page  # type: ignore[assignment]
        semantic_index.current_parent_index_state = _count_unit  # type: ignore[assignment]
        try:
            with _oracle_withdrawn():
                answer = _indexed(vault, plan)
        finally:
            find_module._resolve_page = real_resolve  # type: ignore[assignment]
            semantic_index.current_parent_index_state = real_state  # type: ignore[assignment]
        report.append(f"{label}: reads={reads['pages']} units={reads['units']} n={len(answer)}")
        assert reads["pages"] <= len(answer), report[-1]
        assert reads["units"] <= len(answer), report[-1]
    # Non-vacuity: one shape must really select a small slice of a big corpus.
    assert report[0].endswith("n=12") or "reads=12" in report[0], report


def test_the_eligibility_stage_reports_its_candidate_count(
    vault: Path, warm_managed_cell
) -> None:
    """`index` says where the answer came from; the count says what it cost.

    A stage reporting `index` over a candidate set the size of the corpus has
    answered from an index and still paid for the corpus. Only the count makes
    that visible in the diagnostics, and the durable query log is where a
    returning whole-scope hydration would otherwise be invisible.
    """
    warm_managed_cell(vault)
    with _oracle_withdrawn():
        result = _filtered_recall(vault, projects=["project-alpha"])
    profile = result["timings"]["profile"]["filter_eligibility"]
    assert profile["candidates"] >= 1
    assert profile["candidates"] < 46, profile
    assert _eligibility_source(result) == "index"


def test_stale_catalogue_generation_declines_with_warming(
    vault: Path, warm_managed_cell, walk_sentinel, no_oracle
) -> None:
    """A catalogue behind the live projection declines; it never walks instead.

    An index-backed answer is only correct for the generation it was built
    from. Serving a filter from a stale catalogue is a silently wrong result
    set, and regressing to the scan oracle is the cost this lane exists to
    remove — so the honest third option is the typed retryable outcome.
    """
    warm_managed_cell(vault)
    plan = _plan(None, projects=("project-alpha",))
    assert _indexed(vault, plan), "the live catalogue must answer before it is aged"

    # Move the corpus without republishing the catalogue: canonical Markdown is
    # now ahead of the maintained rows, which is exactly the acknowledgement-time
    # state a governed write leaves behind.
    (vault / kb_dirname() / "Notes" / f"drift-{uuid.uuid4().hex[:8]}.md").write_text(
        "---\ntype: insight\nstatus: active\nupdated: 2026-07-01\n"
        "project: project-alpha\n---\n\n# Drift\n\nDrifted body.\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    freshness.rebaseline(vault)

    sentinel = walk_sentinel(*_scope_roots(vault))
    sentinel.reset()
    with pytest.raises(find_module.RetrievalIndexWarming) as caught:
        _indexed(vault, plan)

    assert caught.value.status in {"warming", "temporarily_unavailable"}
    assert caught.value.site in find_module.RETRIEVAL_WARMING_SITES
    assert sentinel.count == 0, sentinel.report()


def test_a_complement_over_an_inexact_bound_declines_rather_than_under_selecting(
    vault: Path, warm_managed_cell
) -> None:
    """`$not` may only complement a set the columns describe EXACTLY.

    A seed is allowed to be wider than the question, because the evaluation
    settles the remainder. A complement inverts that licence: `NOT superset` is
    a SUBSET of `NOT exact`, so it drops pages the evaluator keeps, and no
    later evaluation can put them back — the candidate never arrives.

    `page.updated` is where that bites. `_temporal_match` lets the BOUND decide
    granularity, so a day-scoped bound is answered exactly by a day column, but
    a PRECISE bound compares instants and a day column can only bracket it. The
    page below is inside the day and before the instant: the seed admits it,
    the evaluator refuses it, and the complement must therefore not be taken.
    """
    _seed_adversarial(vault)
    warm_managed_cell(vault)
    early = f"{kb_dirname()}/Notes/Adv/upd-instant-early.md"
    bound = {"page.updated": {"$gte": "2026-06-15T12:00:00Z"}}

    # The CANDIDATE set for the child is a superset: the SQL admits the early
    # instant that the evaluator then excludes. (The resolved set is exact
    # either way — post-evaluation corrects a superset. It cannot correct a
    # complement, which is why the gap has to be measured here.)
    assert early in _candidates(vault, _plan(bound))
    assert early not in _oracle(vault, _plan(bound))
    # So the complement's true answer contains it, and a complemented seed
    # would not — which is why the classifier refuses to complement at all.
    assert early in _oracle(vault, _plan({"$not": bound}))

    negated = structured_filters.plan_index_eligibility(_plan({"$not": bound}))
    assert negated.inexpressible, negated
    assert not negated.narrows, negated.expr

    # The day-scoped counter-case, so this is a rule about exactness and not a
    # blanket refusal of `$not`: a whole-day bound IS exact, and complements.
    day_bound = {"page.updated": {"$gte": "2026-06-15"}}
    assert structured_filters.plan_index_eligibility(_plan({"$not": day_bound})).narrows
    expected = _oracle(vault, _plan({"$not": day_bound}))
    with _oracle_withdrawn():
        assert _indexed(vault, _plan({"$not": day_bound})) == expected


def test_a_complement_the_columns_cannot_express_is_refused_not_deferred(
    vault: Path, warm_managed_cell, walk_sentinel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inexpressible shape is refused; only a stale index is deferred.

    The retryable warming outcome is a promise: come back and this will work.
    It is the right answer for a catalogue behind the live projection, and the
    wrong one for a shape no catalogue generation can express — a caller that
    retries a sub-day `$not` bound retries forever, and nothing in the envelope
    says so.

    So the two outcomes are separated. The typed refusal names the shape and
    the day-scoped bounds that do work, because the fix is on the caller's
    side; the warming outcome keeps its meaning for the case that really does
    resolve itself.
    """
    _seed_adversarial(vault)
    warm_managed_cell(vault)
    sentinel = walk_sentinel(*_scope_roots(vault))
    precise = {"$not": {"page.updated": {"$gte": "2026-06-15T12:00:00Z"}}}

    sentinel.reset()
    with pytest.raises(structured_filters.FilterError) as caught:
        _filtered_recall(vault, filters=precise)
    error = caught.value
    assert error.code == "UNSUPPORTED_FILTER_FIELD"
    assert "page.updated" in error.message
    # The remediation has to be actionable, so it names the shortcuts that are
    # day-scoped and therefore exact.
    assert "updated_after" in error.remediation
    assert "recency_days" in error.remediation
    assert sentinel.count == 0, sentinel.report()

    # The day-scoped bound is exact, complements, and is not refused.
    day = {"$not": {"page.updated": {"$gte": "2026-06-15"}}}
    expected = _oracle(vault, _plan(day))
    with _oracle_withdrawn():
        assert _indexed(vault, _plan(day)) == expected

    # An offline caller keeps the exact source walk for the refused shape.
    monkeypatch.setattr(readiness, "runtime_managed", lambda: False)
    find_module.reset_page_and_result_caches()
    assert _indexed(vault, _plan(precise)) == _oracle(vault, _plan(precise))


def test_a_plan_the_columns_cannot_narrow_declines_instead_of_hydrating(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """A tautology over the columns is refused, not answered by hydration.

    The walk sentinel cannot see this failure: a seed that narrows nothing
    enumerates no directory and still parses every page in the scope. That is
    the same whole-corpus cost the stage exists to remove, reached by a
    different road and invisible in the diagnostics, because the answer is
    still correct.

    Every compiled predicate this lane accepts does narrow, so the shape is
    driven at the seam rather than through `find()` — which never passes an
    empty plan here. The guard is the floor under the classifier, not a case
    the request surface can reach today: if a later lane adds an operator with
    no column, this is what stops it becoming a silent scan.
    """
    warm_managed_cell(vault)
    empty = structured_filters.compile_filter(None)
    assert not structured_filters.plan_index_eligibility(empty).narrows

    sentinel = walk_sentinel(*_scope_roots(vault))
    sentinel.reset()
    with _oracle_withdrawn(), pytest.raises(find_module.RetrievalIndexWarming) as caught:
        _indexed(vault, empty)
    assert caught.value.site == "filter_eligibility_unnarrowed"
    assert sentinel.count == 0, sentinel.report()


def test_pending_write_is_filtered_against_its_committed_page(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """A page under pending custody is filtered on its committed frontmatter.

    Between a governed write's durable commit and the catalogue republication,
    the catalogue row is the PREVIOUS generation. A filter answered from that
    row returns the page under its old project and hides it under its new one —
    a read-your-write violation with no error to see it by, because both
    answers are well-formed.

    Driven through the public leaf so the overlay, the freshness snapshot and
    the eligibility seam are wired the way a served request wires them, with an
    empty query so the assertion is a filter result and not a ranking.
    """
    rel = f"{kb_dirname()}/Notes/Research/Project Alpha/engine-architecture.md"
    before = (vault / rel).read_text(encoding="utf-8")
    assert "project: project-alpha" in before
    after = before.replace("project: project-alpha", "project: project-omega")

    # `warm_managed_cell` leaves the freshness registry and the catalogue both
    # published at this, the pre-write projection. The governed write below
    # then moves canonical Markdown alone, which is the acknowledgement-time
    # state: the catalogue row is stale for exactly one path.
    warm_managed_cell(vault)
    assert rel in _paths(_filtered_recall(vault, query="", projects=["project-alpha"]))

    generation = f"gen-{uuid.uuid4().hex[:8]}"
    batch_id = f"batch-{uuid.uuid4().hex[:8]}"
    receipt = derived_receipts.prepare_batch(
        vault,
        batch_id=batch_id,
        mutation_attempt_digest=hashlib.sha256(batch_id.encode()).hexdigest(),
        canonical_generation=generation,
        checkpoint_id=f"checkpoint-{generation}",
        paths=(
            DerivedBatchPath(
                rel_path=rel,
                before_hash=hashlib.sha256(before.encode("utf-8")).hexdigest(),
                after_hash=hashlib.sha256(after.encode("utf-8")).hexdigest(),
                stable_memory_ref=None,
            ),
        ),
        required_components=_REQUIRED,
        now=10.0,
    )
    (vault / rel).write_text(after, encoding="utf-8")
    proof = derived_receipts.prove_committed(vault, receipt, current_generation=generation)
    assert proof.outcome == "ready", proof.outcome
    from exomem import pending_recall

    pending_recall.reset()
    assert derived_receipts.publish_pending_visibility(
        vault, receipt, publisher=pending_recall.publish
    )
    # `reset_page_and_result_caches`, not `clear_cache`: the latter also drops
    # the freshness registry, which would turn this into a cold-projection test
    # instead of a pending-custody one.
    find_module.reset_page_and_result_caches()
    assert pending_recall.overlay(vault).covers(rel), (
        "the test needs live pending custody to mean anything"
    )

    sentinel = walk_sentinel(*_scope_roots(vault))
    sentinel.reset()
    with _oracle_withdrawn():
        # Withdrawn oracle: the overlay must be consulted on the INDEXED arm.
        # Re-reading the page through the walk would satisfy read-your-write
        # while leaving the cost this lane removes exactly where it was.
        committed = _paths(_filtered_recall(vault, query="", projects=["project-omega"]))
        stale = _paths(_filtered_recall(vault, query="", projects=["project-alpha"]))

    assert rel in committed, "the committed project must be visible"
    assert rel not in stale, "the stale catalogue project must not be"
    assert sentinel.count == 0, sentinel.report()


def test_pending_custody_outside_the_kb_stays_out_of_a_kb_scoped_recall(
    vault: Path, warm_managed_cell
) -> None:
    """Re-offering a committed pending page must respect the requested scope.

    `_merge_pending_walk` drops an out-of-KB pending identity for every scope
    but `vault`, because a `scope="kb"` request asked not to see it. The
    indexed arm unions `current_pages()` into the candidate set, and without
    the same gate a governed write to `Reference/` becomes eligible for a
    KB-scoped recall — and reaches the caller through the empty-query
    filter-only lane, where eligibility IS the result.
    """
    outside = vault / "Reference" / "outside-pending.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(
        "---\ntype: insight\nstatus: active\nproject: outside-omega\n"
        "updated: 2026-07-09\n---\n\n# Outside\n\nA page about metabolism.\n",
        encoding="utf-8",
    )
    warm_managed_cell(vault)

    from exomem import find_corpus, pending_recall

    rel = "Reference/outside-pending.md"
    page = find_corpus.parse_page(outside, outside.stat().st_mtime, vault)
    assert page is not None
    overlay = pending_recall.PendingOverlay(
        outcome="ready",
        failure_code=None,
        snapshot_generation=1,
        rows={
            rel: pending_recall.PendingRow(
                rel_path=rel,
                canonical_generation="gen-outside",
                batch_id="batch-outside",
                after_hash=None,
                page=page,
                stable_memory_ref=None,
            )
        },
    )
    plan = _plan(None, projects=("outside-omega",))

    def _eligible(scope: str) -> set[str]:
        with _oracle_withdrawn():
            return find_module._resolve_eligible_filter_paths(
                vault,
                scope=scope,
                plan=plan,
                snapshot=find_module.FreshnessSnapshot(vault),
                pending=overlay,
            )

    assert _eligible("kb") == find_module._eligible_filter_paths(
        vault, scope="kb", plan=plan, pending=overlay
    )
    assert rel not in _eligible("kb"), "a KB-scoped recall must not see it"
    assert rel in _eligible("vault"), "a vault-scoped recall must"

    # And end to end, where an empty query makes eligibility the whole answer.
    hits = _paths(_filtered_recall(vault, query="", projects=["outside-omega"]))
    assert rel not in hits, sorted(hits)


def test_unsupported_field_is_rejected_at_compile_time_not_scanned(
    vault: Path, warm_managed_cell, walk_sentinel, no_oracle
) -> None:
    """A field no index answers fails compilation; it never becomes a walk.

    `page.frontmatter:/<pointer>` is the whole of that set. It is open-ended by
    construction — any key, any depth — so there is no column to hold it and no
    honest managed answer, and a scan fallback for it would silently
    reintroduce the whole cost this lane removes.

    The governed unit axes are NOT in that set, and the counter-case below is
    the point: `unit.verdict` and `unit.check_by` are documented filter fields
    that "what is due" queries depend on, so they are carried in
    `semantic_units` and answered rather than refused.
    """
    _seed_adversarial(vault)
    warm_managed_cell(vault)
    sentinel = walk_sentinel(*_scope_roots(vault))

    sentinel.reset()
    with pytest.raises(structured_filters.FilterError) as caught:
        _filtered_recall(vault, filters={"page.frontmatter:/custom_axis": {"$eq": "x"}})
    assert caught.value.code == "UNSUPPORTED_FILTER_FIELD"
    assert sentinel.count == 0, sentinel.report()

    for expr in (
        {"unit.verdict": {"$eq": "supported"}},
        {"unit.check_by": {"$lt": "2026-12-01"}},
    ):
        sentinel.reset()
        with _oracle_withdrawn():
            result = _filtered_recall(vault, filters=expr, result_level="page")
        assert _eligibility_source(result) == "index", expr
        assert sentinel.count == 0, f"{expr}: {sentinel.report()}"


def test_offline_readers_keep_the_scan_oracle(vault: Path, monkeypatch) -> None:
    """The oracle is not deleted: an unmanaged caller still gets the exact walk.

    The no-walk contract binds the managed reader. A CLI user with a cold
    catalogue must keep answering filters, including the ones no index can
    answer, rather than being told to retry forever.
    """
    monkeypatch.setattr(readiness, "runtime_managed", lambda: False)
    plan = _plan({"page.frontmatter:/type": {"$eq": "insight"}})
    assert _indexed(vault, plan) == _oracle(vault, plan)
