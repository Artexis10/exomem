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

from exomem import commands, structured_filters
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


def _top_level_stage_ms(timings: dict) -> float:
    """Sum only un-nested stages.

    `graph` wraps `graph.seeds`/`graph.expand`/`graph.resolver`, so a sub-stage's
    time is already inside its parent's interval; summing every entry would
    over-count the same wall time and could not satisfy any bound.
    """
    return sum(
        stage["ms"]
        for name, stage in timings["stages"].items()
        if "." not in name and "ms" in stage
    )


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
    """The public leaf, not a hand-built timing object, must account for its own time."""
    warm_managed_cell(vault)

    timings = _timed_filtered_recall(vault)

    total = timings["total_ms"]
    unattributed = timings["unattributed_ms"]
    assert _top_level_stage_ms(timings) + unattributed <= total, timings["stages"]
    assert unattributed <= 0.15 * total, (
        f"unattributed_ms={unattributed} of total_ms={total}: "
        "material time is reported only as the remainder"
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
    vault: Path, warm_managed_cell
) -> None:
    """The source vocabulary is only worth anything if it cannot flatter a walk.

    `filter_eligibility` is the one stage whose source is decided at runtime
    today: page clauses (`projects`, `tags`) are unsupported by
    `plan_index_candidates` and fall to the canonical full-scan oracle, while a
    `unit.category` clause seeds from the maintained sidecar. A walking branch
    that reported `index` is precisely the mislabel that would let the cost
    this change exists to expose hide behind a reassuring diagnostic, so both
    directions are pinned rather than only the reassuring one.
    """
    warm_managed_cell(vault)

    assert _eligibility_source(vault, _plan(projects=("project-alpha",))) == "computed"
    assert _eligibility_source(vault, _plan(tags=("metabolism",))) == "computed"
    # The counter-case, so this is a mapping and not a constant: a `unit.category`
    # clause IS seeded from the maintained sidecar and says so.
    assert _eligibility_source(vault, _plan(categories=("rule",))) == "index"


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
    assert summary["stage_source"]["filter_eligibility"] == "computed"
