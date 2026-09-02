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

#: Every field the 0.2 inventory lists as index-answerable, with the operators
#: this lane seeds. Model-free by construction: eligibility is metadata only,
#: so no embedding, ranking or query text enters any of these.
_IDENTITY_CASES: tuple[tuple[str, dict[str, Any] | None, dict[str, Any]], ...] = (
    ("page.project/$in", None, {"projects": ("project-alpha",)}),
    ("page.project/$in multi", None, {"projects": ("project-alpha", "infrastructure")}),
    ("page.project/$all", {"page.project": {"$all": ["project-alpha", "project-beta"]}}, {}),
    ("page.project/$contains", {"page.project": {"$contains": "project-alpha"}}, {}),
    ("page.project/$exists", {"page.project": {"$exists": True}}, {}),
    ("page.project/$exists false", {"page.project": {"$exists": False}}, {}),
    ("page.tags/$in", None, {"tags": ("retrieval",)}),
    ("page.tags/$all", {"page.tags": {"$all": ["retrieval", "fusion"]}}, {}),
    ("page.tags/$contains", {"page.tags": {"$contains": "postgres"}}, {}),
    ("page.tags/$exists", {"page.tags": {"$exists": True}}, {}),
    ("page.type/$eq", {"page.type": {"$eq": "insight"}}, {}),
    ("page.type/$in", None, {"types": ("insight", "pattern")}),
    ("page.type/$ne", {"page.type": {"$ne": "insight"}}, {}),
    ("page.type/$contains", {"page.type": {"$contains": "sight"}}, {}),
    ("page.type/$exists", {"page.type": {"$exists": True}}, {}),
    ("page.status/$eq", {"page.status": {"$eq": "active"}}, {}),
    ("page.status/$in", {"page.status": {"$in": ["active", "draft"]}}, {}),
    ("page.speakers/$in", None, {"speakers": ("ada lovelace",)}),
    ("page.speakers/$exists", {"page.speakers": {"$exists": True}}, {}),
    ("page.file_type/$eq", {"page.file_type": {"$eq": "note"}}, {}),
    ("page.file_type/$in", None, {"file_types": ("note",)}),
    ("page.file_type/$not $in", None, {"exclude_file_types": ("note",)}),
    ("page.source_kind/$eq", {"page.source_kind": {"$eq": "article"}}, {}),
    ("page.source_kind/$in", None, {"source_kinds": ("article", "book")}),
    ("page.domain/$eq", {"page.domain": {"$eq": "engineering"}}, {}),
    ("page.domain/$in", None, {"domains": ("engineering",)}),
    ("page.updated/$gte", {"page.updated": {"$gte": "2026-06-01"}}, {}),
    ("page.updated/$lt", {"page.updated": {"$lt": "2026-06-01"}}, {}),
    (
        "page.updated/$between",
        {"page.updated": {"$between": ["2026-05-01", "2026-06-30"]}},
        {},
    ),
    ("page.updated/$exists", {"page.updated": {"$exists": True}}, {}),
    ("page.updated/updated_after", None, {"updated_after": "2026-06-01"}),
    ("unit.category/$eq", {"unit.category": {"$eq": "constraint"}}, {}),
    ("unit.category_key/$eq", {"unit.category_key": {"$eq": "constraint"}}, {}),
    ("unit.kind/$exists", {"unit.kind": {"$exists": True}}, {}),
    ("unit.form/$eq", {"unit.form": {"$eq": "statement"}}, {}),
    ("unit.tags/$in", {"unit.tags": {"$in": ["retrieval"]}}, {}),
    ("unit.context/$exists", {"unit.context": {"$exists": True}}, {}),
    (
        "AND page+unit",
        {
            "$and": [
                {"page.type": {"$eq": "insight"}},
                {"unit.category": {"$exists": True}},
            ]
        },
        {},
    ),
    (
        "AND page+seeded unit",
        {
            "$and": [
                {"page.status": {"$eq": "active"}},
                {"unit.category": {"$eq": "constraint"}},
            ]
        },
        {},
    ),
    (
        "AND two unit axes on one row",
        {
            "$and": [
                {"unit.category": {"$eq": "constraint"}},
                {"unit.form": {"$eq": "compact"}},
            ]
        },
        {},
    ),
    (
        "OR page+page",
        {
            "$or": [
                {"page.type": {"$eq": "insight"}},
                {"page.tags": {"$in": ["postgres"]}},
            ]
        },
        {},
    ),
    (
        "OR page+unit",
        {
            "$or": [
                {"page.status": {"$eq": "active"}},
                {"unit.category": {"$eq": "constraint"}},
            ]
        },
        {},
    ),
    ("NOT page", {"$not": {"page.type": {"$eq": "insight"}}}, {}),
    (
        "AND with NOT",
        {
            "$and": [
                {"page.status": {"$eq": "active"}},
                {"$not": {"page.tags": {"$in": ["retrieval"]}}},
            ]
        },
        {},
    ),
    (
        "contradiction",
        {"$and": [{"page.type": {"$eq": "insight"}}, {"page.type": {"$eq": "pattern"}}]},
        {},
    ),
)


def test_index_result_equals_the_oracle_for_every_seeded_field(
    vault: Path, warm_managed_cell
) -> None:
    """The index answer IS the scan answer, field by field, on one generation.

    Model-free: no query text, no embeddings, no ranking — eligibility is a set
    of identities decided by metadata alone, so this compares sets and nothing
    else. A field whose index answer diverges from the oracle names itself.
    """
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
    }, sorted(covered)


# --------------------------------------------------------------------------- #
# Generation binding, custody, and the closed field vocabulary
# --------------------------------------------------------------------------- #


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


def test_unsupported_field_is_rejected_at_compile_time_not_scanned(
    vault: Path, warm_managed_cell, walk_sentinel, no_oracle
) -> None:
    """A field no index answers fails compilation; it never becomes a walk.

    `page.frontmatter:/<pointer>` is open-ended by design and `unit.verdict` /
    `unit.check_by` are read off the parsed unit with no column anywhere. For a
    managed reader there is no honest way to answer them, and a scan fallback
    would silently reintroduce the whole cost this lane removes.
    """
    warm_managed_cell(vault)
    sentinel = walk_sentinel(*_scope_roots(vault))

    for expr in (
        {"page.frontmatter:/custom_axis": {"$eq": "x"}},
        {"unit.verdict": {"$eq": "supported"}},
        {"unit.check_by": {"$lt": "2026-01-01"}},
    ):
        sentinel.reset()
        with pytest.raises(structured_filters.FilterError) as caught:
            _filtered_recall(vault, filters=expr)
        assert caught.value.code == "UNSUPPORTED_FILTER_FIELD", expr
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
