"""Timing attribution must be complete, and every stage must say where it came from.

#983 converted three stages that wrote a duration into the table without
registering an interval, so `unattributed_ms` double-counted them. Nothing
stopped the fourth. This file makes the property structural instead of
reviewed:

* the stages table is write-through from `FindTimings.span`, so a manual write
  is rejected by construction rather than caught by inspection;
* every stage entry carries a `source` in {index, cache, declined, computed},
  so a corpus walk is visible in the diagnostics without a benchmark;
* a real `op_find` -- the public leaf, not a hand-built `FindTimings` --
  satisfies both attribution bounds.

The last one is measured on a warm managed cell carrying a supported page
filter, which is the shape the live-cell numbers in the proposal were taken
under.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, find_types, readiness, structured_filters
from exomem import find as find_module
from exomem.find_types import FindTimings

SOURCE_VOCABULARY = frozenset({"index", "cache", "declined", "computed"})


def _timed_filtered_recall(vault: Path) -> dict:
    """One real timed hybrid recall carrying a `projects` filter."""
    result = commands.op_find(
        vault,
        query="metabolism",
        projects=["project-alpha"],
        mode="hybrid",
        scope="kb-only",
        graph=True,
        include_timings=True,
    )
    return result["timings"]


def _root_stages(timings: dict) -> dict[str, dict]:
    """The stages that partition the call: the ones no other stage contained.

    A root is an entry with no `parent` key. This replaces a dot-in-the-name
    heuristic that was wrong in both directions: `semantic.search` is a ROOT
    whose name contains a dot (so a 28 ms parent was dropped from the sum), and
    `recall_projection` was one name opened at three depths (so a nested scalar
    was counted as a root). The bound held by accident, not by partition.
    """
    return {
        name: entry
        for name, entry in timings["stages"].items()
        if "parent" not in entry and "ms" in entry
    }


def _rounding_budget(terms: int) -> float:
    """Slack owed purely to 3-dp rounding, and nothing else.

    Every `ms` in the payload is rounded to three decimals, so a sum of `terms`
    of them can sit up to 0.0005 ms above the unrounded truth for each term
    while `total_ms` sits up to 0.0005 ms below it. That is microseconds; no
    real stage hides inside it, and the budget shrinks to nothing if the
    payload ever reports full precision.
    """
    return 0.0005 * terms


def test_manual_stage_write_is_rejected() -> None:
    """A duration may only enter the table through a registered interval.

    This is the structural half of the completeness property: `unattributed_ms`
    is computed from merged intervals, so a stage written straight into the
    dict claims time that no interval ever covered and is double-counted
    against the remainder.
    """
    timings = FindTimings()

    with pytest.raises(TypeError):
        timings.stages["filter_eligibility"] = {"ms": 0.0, "cache_hit": True}

    assert "filter_eligibility" not in timings.as_dict()["stages"]


def test_every_stage_carries_a_source(vault: Path, warm_managed_cell) -> None:
    """Each stage says whether it was answered from an index, a cache, or declined."""
    warm_managed_cell(vault)

    timings = _timed_filtered_recall(vault)

    assert timings["stages"], "a real recall reported no stages at all"
    missing = sorted(name for name, stage in timings["stages"].items() if "source" not in stage)
    assert not missing, f"stages with no source: {missing}"
    bad = sorted(
        f"{name}={stage['source']!r}"
        for name, stage in timings["stages"].items()
        if stage["source"] not in SOURCE_VOCABULARY
    )
    assert not bad, f"stages outside the source vocabulary: {bad}"


def test_real_op_find_satisfies_the_attribution_bounds(
    vault: Path, warm_managed_cell
) -> None:
    """The public leaf, not a hand-built timing object, must account for its own time.

    The sum is over ROOT stages, which is the only set that partitions the call:
    a nested stage's time is already inside its parent's interval, so adding it
    counts the same wall clock twice.
    """
    warm_managed_cell(vault)

    timings = _timed_filtered_recall(vault)

    total = timings["total_ms"]
    unattributed = timings["unattributed_ms"]
    roots = _root_stages(timings)
    assert roots, "a real recall reported no root stages"
    root_ms = sum(entry["ms"] for entry in roots.values())
    budget = _rounding_budget(len(roots) + 2)

    assert root_ms + unattributed <= total + budget, (
        f"root stages {round(root_ms, 3)} + unattributed {unattributed} "
        f"exceed total {total}; roots="
        f"{ {name: entry['ms'] for name, entry in sorted(roots.items())} }"
    )
    assert unattributed <= 0.15 * total, (
        f"unattributed_ms={unattributed} of total_ms={total}: "
        "material time is reported only as the remainder"
    )


def test_every_nested_stage_fits_inside_its_parent(
    vault: Path, warm_managed_cell
) -> None:
    """The other half of the partition: containment must actually contain.

    A root sum only means anything if the entries excluded from it really are
    inside the ones that remain. A nested stage reporting more time than its
    parent would prove the recorded forest is a fiction and the bound above is
    measuring the wrong set.
    """
    warm_managed_cell(vault)

    stages = _timed_filtered_recall(vault)["stages"]

    nested = {
        name: entry
        for name, entry in stages.items()
        if "parent" in entry and "ms" in entry
    }
    assert nested, "no stage reported containment at all"
    for name, entry in sorted(nested.items()):
        parent = stages[entry["parent"]]
        assert "ms" in parent, f"{name} names a parent that recorded no duration"
        assert entry["ms"] <= parent["ms"] + _rounding_budget(2), (
            f"{name}={entry['ms']}ms does not fit inside "
            f"{entry['parent']}={parent['ms']}ms"
        )


def _plan(**shortcuts) -> structured_filters.FilterPlan:
    return structured_filters.compile_filter(
        None, shortcuts=structured_filters.FilterShortcuts(**shortcuts)
    )


def _eligibility_source(vault: Path, plan: structured_filters.FilterPlan) -> str:
    """Drive the eligibility seam under an open span and read back its source."""
    timings = FindTimings()
    snapshot = find_module.FreshnessSnapshot(vault)
    with timings.span("filter_eligibility"):
        try:
            find_module._resolve_eligible_filter_paths(
                vault,
                scope="kb",
                plan=plan,
                snapshot=snapshot,
                timings=timings,
            )
        except find_module.RetrievalIndexWarming:
            pass
    return timings.as_dict()["stages"]["filter_eligibility"]["source"]


def test_a_stage_that_walked_reports_that_it_computed(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source vocabulary is only worth anything if it cannot flatter a walk.

    `filter_eligibility` is the one stage whose source is decided at runtime.
    Lane 2 moved the MANAGED reader's page clauses (`projects`, `tags`) onto
    the page catalogue, so they report `index` there — but the walking branch
    did not disappear, it moved: an offline/CLI caller keeps the canonical
    full-scan oracle by design, and that branch must still say `computed`.
    A walking branch that reported `index` is precisely the mislabel that would
    let the cost this change exists to expose hide behind a reassuring
    diagnostic, so both directions stay pinned rather than only the
    reassuring one.
    """
    warm_managed_cell(vault)

    assert _eligibility_source(vault, _plan(projects=("project-alpha",))) == "index"
    assert _eligibility_source(vault, _plan(tags=("metabolism",))) == "index"
    # A `unit.category` clause seeds from the maintained sidecar and says so.
    assert _eligibility_source(vault, _plan(categories=("rule",))) == "index"

    # The counter-case, so this is a mapping and not a constant: the same plan
    # on the same corpus, resolved by the branch that really does walk.
    monkeypatch.setattr(readiness, "runtime_managed", lambda: False)
    assert _eligibility_source(vault, _plan(projects=("project-alpha",))) == "computed"
    assert _eligibility_source(vault, _plan(tags=("metabolism",))) == "computed"


def test_a_hot_cache_hit_reports_itself_as_a_cache(
    vault: Path, warm_managed_cell
) -> None:
    """The exact-catalogue hot lane must register an interval like any stage.

    It used to write itself straight into the table, with a hand-chosen 0.0 ms
    "without timing clock noise" — the exact shape `unattributed_ms`
    double-counts and the one this change makes impossible. The lane really is
    free, so its own interval says 0.0 anyway; what it now also says is that
    the answer came from a cache.
    """
    warm_managed_cell(vault)
    request = {
        "query": "",
        "categories": ["rule"],
        "scope": "kb-only",
        "mode": "keyword",
        "graph": False,
        "rerank": False,
        "limit": 10,
        "include_timings": True,
    }

    commands.op_find(vault, **request)
    hot = commands.op_find(vault, **request)["timings"]

    assert hot["cache"]["hit"] is True
    assert hot["stages"]["filter_eligibility"]["source"] == "cache"
    assert hot["stages"]["filter_eligibility"]["cache_hit"] is True


def test_the_query_log_summary_carries_a_source_for_every_stage_it_lists(
    vault: Path, warm_managed_cell
) -> None:
    """The durable log is where a returning walk becomes visible over many requests.

    `_timing_log_summary` is a closed projection: a field it does not name never
    reaches the query log. It already carries `unattributed_ms` for exactly this
    reason — #283 was a month of not being able to see a rising uninstrumented
    term. A stage that silently stops answering from an index and starts walking
    is the same shape of defect one level up, so the source rides alongside the
    duration, drawn from the same filtered set: the log cannot report a stage's
    time without saying where that time came from.
    """
    warm_managed_cell(vault)
    timings = _timed_filtered_recall(vault)

    summary = commands._timing_log_summary(timings)

    assert summary is not None
    assert summary["stage_ms"], "the summary listed no stages"
    assert set(summary["stage_source"]) == set(summary["stage_ms"])
    assert set(summary["stage_source"].values()) <= SOURCE_VOCABULARY
    # The stage this change exists to expose, carried all the way to the log.
    # `index` since Lane 2 moved managed page filters onto the catalogue; the
    # value that matters is that the log carries one at all, and that a walk
    # would have to publish `computed` here to be served.
    assert summary["stage_source"]["filter_eligibility"] == "index"


_DEPTH_SHAPES = (
    ("hybrid with a page filter", {"query": "metabolism", "projects": ["project-alpha"]}),
    ("mixed with a page filter", {
        "query": "metabolism", "projects": ["project-alpha"], "result_level": "mixed",
    }),
    ("mixed with a unit filter", {
        "query": "metabolism", "categories": ["rule"], "result_level": "mixed",
    }),
    ("keyword", {"query": "metabolism", "mode": "keyword", "graph": False}),
    ("empty query with a tag filter", {"query": "", "tags": ["metabolism"]}),
)


def test_no_stage_name_spans_two_depths(vault: Path, warm_managed_cell) -> None:
    """One name at two depths breaks the partition and the source routing both.

    `recall_projection` was opened at the top level, inside `freshness`, and
    inside `graph.resolver`. Its scalar was therefore simultaneously a root
    stage and time already inside two other roots, which is how the old bound
    passed while `sum(stage.ms) + unattributed` exceeded `total_ms` by 34 ms.
    The same collision aliases `_pending_sources`, which is keyed by stage name:
    an inner span's `mark_source` could be consumed by an outer one.

    Driven through `find` with an explicit collector because the conflict
    record is a defect report, not a diagnostic, and so is deliberately absent
    from the response payload.
    """
    warm_managed_cell(vault)

    for label, request in _DEPTH_SHAPES:
        timings = FindTimings()
        find_module.find(vault, scope="kb-only", timings=timings, **request)
        assert timings.parent_conflicts() == {}, f"{label}: {timings.parent_conflicts()}"


def test_span_rejects_a_reserved_field() -> None:
    """`**fields` may not write the accounting the collector computes.

    `span("semantic.search", ms=0.0)` hid thirty milliseconds of a fifty
    millisecond request with every gate green: the field update ran after the
    computed keys and simply overwrote the measured duration. The same door let
    a caller relabel `source`, which is the one field the walk sentinel's
    diagnostics rely on.
    """
    timings = FindTimings()

    for field in ("ms", "calls", "skipped", "error", "parent"):
        with pytest.raises(TypeError):
            with timings.span("semantic.search", **{field: 0.0}):
                pass

    # `source` cannot reach `**fields` at all — it is a named parameter — and
    # the other half of that door is closed too: it only accepts the four
    # values the vocabulary defines.
    assert "source" in find_types._RESERVED_STAGE_FIELDS
    with pytest.raises(ValueError):
        with timings.span("semantic.search", source="fast"):
            pass

    assert "semantic.search" not in timings.as_dict()["stages"]


def test_span_rejects_a_non_scalar_field_value() -> None:
    """Timing diagnostics never carry bulk content, and the type says so."""
    timings = FindTimings()

    with pytest.raises(TypeError):
        with timings.span("keyword", excerpt=["a note body", "another"]):
            pass

    # A scalar fact about the stage is exactly what the field is for.
    with timings.span("keyword", cache_hit=True):
        pass
    assert timings.as_dict()["stages"]["keyword"]["cache_hit"] is True


def test_a_stage_entry_cannot_be_mutated_through_the_table() -> None:
    """Refusing `stages[name] = ...` is worthless if `stages[name]` is writable.

    The table handed out its live dict, so `stages["keyword"]["ms"] = 999.0`
    took effect and no gate could see it — the same defect as a manual write,
    one subscript further in.
    """
    timings = FindTimings()
    with timings.span("keyword"):
        pass
    measured = timings.stages["keyword"]["ms"]

    with pytest.raises(TypeError):
        timings.stages["keyword"]["ms"] = 999.0
    with pytest.raises(TypeError):
        timings.stages["keyword"]["source"] = "index"

    assert timings.as_dict()["stages"]["keyword"]["ms"] == measured
