"""Whole-call latency accounting and the real disclosure boundary."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from exomem.recall_timing import CURRENT, Interval, Trace


@pytest.mark.parametrize("reuse_first", [False, True])
def test_an_actual_encode_interval_is_never_reported_as_skipped(reuse_first: bool) -> None:
    from exomem.find_types import FindTimings

    timings = FindTimings()
    if reuse_first:
        timings.skipped("vector.embed")
    with timings.span("vector.embed", source="index"):
        pass
    timings.skipped("vector.embed")
    stage = timings.as_dict()["stages"]["vector.embed"]
    assert "ms" in stage
    assert not stage.get("skipped", False)
    assert stage["source"] == "index"


def _trace(intervals: list[Interval], edges: list[tuple[int, int | None]]) -> Trace:
    trace = Trace(known_serial=True)
    trace.started, trace.ended = edges[0][0], edges[-1][0]
    trace.edges, trace.intervals = edges, intervals
    trace.cpu_supported = edges[0][1] is not None
    trace.queue_started = trace.queue_ended = None
    return trace


def test_separately_owned_child_counts_once_and_parent_keeps_other_children() -> None:
    trace = _trace([
        Interval("semantic_units", 1, None, 0, 100_000_000),
        Interval("vector.embed", 1, 0, 10_000_000, 50_000_000),
        Interval("unit.lookup", 1, 0, 60_000_000, 80_000_000),
    ], [(0, 0), (10_000_000, 10_000_000), (50_000_000, 50_000_000),
        (60_000_000, 60_000_000), (80_000_000, 80_000_000), (100_000_000, 100_000_000)])
    measured = trace.result({"units": ["semantic_units"], "encode": ["vector.embed"]})
    assert measured["stages"]["units"]["ms"] == 60.0
    assert measured["stages"]["encode"]["ms"] == 40.0
    assert measured["unattributed_ms"] == 0.0
    assert measured["unbudgeted"] == []


def test_cross_thread_overlap_and_root_uncovered_cpu_count_once() -> None:
    trace = _trace([
        Interval("vector.embed", 1, None, 10_000_000, 70_000_000),
        Interval("bm25", 2, None, 40_000_000, 90_000_000),
    ], [(0, 0), (10_000_000, 5_000_000), (40_000_000, 35_000_000),
        (70_000_000, 95_000_000), (90_000_000, 115_000_000), (100_000_000, 120_000_000)])
    measured = trace.result({"encode": ["vector.embed"], "lexical": ["bm25"]})
    assert measured["stages"] == {"encode": {"ms": 60.0, "cpu_ms": 30.0},
                                   "lexical": {"ms": 50.0, "cpu_ms": 20.0}}
    assert measured["overlap_cpu_ms"] == 60.0
    assert measured["unattributed_cpu_ms"] == 10.0
    assert measured["critical_path"] is None
    assert sum(row["cpu_ms"] for row in measured["stages"].values()) + measured["overlap_cpu_ms"] + measured["unattributed_cpu_ms"] == measured["cpu_ms"]


def test_wait_does_not_inflate_unit_wall_and_unknown_cpu_stays_unknown() -> None:
    trace = _trace([
        Interval("semantic_units", 1, None, 0, 60_000_000),
        Interval("vector.wait", 1, 0, 0, 40_000_000),
        Interval("vector.embed", 2, None, 0, 40_000_000),
    ], [(0, None), (40_000_000, None), (60_000_000, None)])
    measured = trace.result({"units": ["semantic_units"], "encode": ["vector.embed"]})
    assert measured["stages"]["units"]["ms"] == 20.0
    assert measured["unbudgeted"] == []
    assert measured["cpu_ms"] is measured["overlap_cpu_ms"] is measured["unattributed_cpu_ms"] is None
    assert all(row["cpu_ms"] is None for row in measured["stages"].values())


def test_cpu_during_an_excluded_wait_remains_visible_without_uncovered_wall() -> None:
    trace = _trace([
        Interval("semantic_units", 1, None, 0, 100_000_000),
        Interval("vector.wait", 1, 0, 0, 40_000_000),
    ], [(0, 0), (40_000_000, 20_000_000), (100_000_000, 80_000_000)])
    measured = trace.result({"units": ["semantic_units"]})
    assert measured["stages"]["units"]["cpu_ms"] == 60.0
    assert measured["unattributed_cpu_ms"] == 20.0
    assert measured["overlap_cpu_ms"] == measured["unattributed_ms"] == 0.0
    assert measured["cpu_ms"] == 80.0


def test_a_separately_owned_worker_child_is_excluded_from_its_raw_container() -> None:
    trace = _trace([
        Interval("semantic_units", 1, None, 0, 100_000_000),
        Interval("", 1, 0, 0, 60_000_000, "parallel"),
        Interval("", 2, 1, 0, 60_000_000, "branch"),
        Interval("vector.embed", 2, 2, 0, 40_000_000),
    ], [(0, 0), (40_000_000, 40_000_000), (60_000_000, 60_000_000), (100_000_000, 100_000_000)])
    measured = trace.result({"units": ["semantic_units"], "encode": ["vector.embed"]})
    assert measured["stages"]["units"] == {"ms": 60.0, "cpu_ms": 60.0}
    assert measured["stages"]["encode"] == {"ms": 40.0, "cpu_ms": 0.0}
    assert measured["overlap_cpu_ms"] == 40.0
    assert measured["unattributed_cpu_ms"] == 0.0


def test_critical_path_keeps_earlier_stages_of_the_branch_that_finishes_last() -> None:
    trace = _trace([
        Interval("", 1, None, 0, 50_000_000, "parallel"),
        Interval("", 1, 0, 0, 50_000_000, "branch"),
        Interval("", 2, 0, 0, 45_000_000, "branch"),
        Interval("vector.embed", 1, 1, 0, 40_000_000),
        Interval("vector.search", 1, 1, 46_000_000, 50_000_000),
        Interval("bm25", 2, 2, 0, 30_000_000),
        Interval("keyword", 2, 2, 30_000_000, 45_000_000),
    ], [(0, 0), (30_000_000, 30_000_000), (40_000_000, 40_000_000),
        (45_000_000, 45_000_000), (46_000_000, 46_000_000), (50_000_000, 50_000_000)])
    measured = trace.result({"encode": ["vector.embed"], "dense": ["vector.search"],
                             "bm25": ["bm25"], "keyword": ["keyword"]})
    assert measured["critical_path"] == ["dense", "encode"]


@pytest.mark.parametrize("branch_end, expected", [(55, ["encode", "setup", "tail"]),
                                                (50, ["encode", "lexical", "setup", "tail"])])
def test_branch_lifetime_keeps_waiting_winner_and_shared_setup_and_tail(branch_end, expected) -> None:
    trace = _trace([
        Interval("setup", 1, None, 0, 5_000_000),
        Interval("", 1, None, 5_000_000, 55_000_000, "parallel"),
        Interval("", 2, 1, 5_000_000, branch_end * 1_000_000, "branch"),
        Interval("", 3, 1, 5_000_000, 50_000_000, "branch"),
        Interval("encode", 2, 2, 5_000_000, 35_000_000),
        Interval("lexical", 3, 3, 5_000_000, 50_000_000),
        Interval("tail", 1, None, 55_000_000, 60_000_000),
    ], [(n * 1_000_000, n * 1_000_000) for n in sorted({0, 5, 35, 50, branch_end, 55, 60})])
    measured = trace.result({name: [name] for name in ("setup", "tail", "encode", "lexical")})
    assert measured["critical_path"] == expected
    assert measured["unbudgeted"] == []
    assert measured["unattributed_ms"] == 5.0


def test_anonymous_work_on_another_thread_has_no_inferred_critical_path() -> None:
    trace = _trace([Interval("first", 1, None, 0, 10_000_000),
                    Interval("second", 2, None, 10_000_000, 20_000_000)],
                   [(0, 0), (10_000_000, 10_000_000), (20_000_000, 20_000_000)])
    assert trace.result({})["critical_path"] is None


def test_parallel_contexts_link_complete_branch_lifetimes_across_threads() -> None:
    import threading

    trace = Trace()
    observed = []
    with trace.span("setup"):
        pass
    with trace.parallel() as group:
        def worker():
            with group.branch(), trace.span("encode"):
                observed.append(CURRENT.get())
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
    with trace.span("tail"):
        pass
    trace.finish()
    measured = trace.result({name: [name] for name in ("setup", "tail", "encode")})
    assert measured["critical_path"] == ["encode", "setup", "tail"]
    assert observed == [trace]
    assert measured["unbudgeted"] == []


def test_unbudgeted_stage_is_named_instead_of_disappearing() -> None:
    trace = _trace([Interval("selection", 1, None, 0, 5_000_000)],
                   [(0, 0), (5_000_000, 3_000_000)])
    measured = trace.result({})
    assert measured["unbudgeted"] == ["selection"]
    assert measured["stages"]["unbudgeted:selection"]["ms"] == 5.0


def test_open_intervals_are_a_broken_instrument() -> None:
    trace = _trace([Interval("selection", 1, None, 0)], [(0, 0), (1, 1)])
    with pytest.raises(ValueError, match="not closed"):
        trace.result({})


def test_private_adapter_observes_real_owner_and_admitted_calls_without_disclosing(monkeypatch, tmp_path: Path) -> None:
    scripts = str(Path(__file__).resolve().parents[1] / "scripts")
    monkeypatch.syspath_prepend(scripts)
    from recall_latency_adapter import instrument
    from synth_vault import gen_dense_vault
    from test_governance_egress import _external, write_rule, write_scope

    from exomem import commands, find_types, lexstore
    from exomem.governance import principal

    gen_dense_vault(tmp_path, 12)
    write_scope(tmp_path, paths="Notes/**")
    write_rule(tmp_path, ceiling=3)
    lexstore.ensure_fresh(tmp_path)
    timing_type = find_types.FindTimings
    for caller in (principal.owner_principal(surface="library"), _external()):
        with principal.request_scope(caller), principal.invocation_scope():
            with instrument() as (trace, collectors):
                result = commands.op_ask_memory(tmp_path, query="topic", mode="keyword", include_timings=True)
            assert find_types.FindTimings is timing_type
            assert collectors and all(type(item) is timing_type for item in collectors)
            observed = trace.result({"keyword": ["keyword"], "due_state": ["due_state"]})
            assert observed["total_ms"] > 0
            if caller.audience_id != principal.OWNER_AUDIENCE:
                assert result["timings_suppressed"] == {"status": "release_restricted"}
                assert "timings" not in result
            else:
                assert any(item.name == "due_state" for item in trace.intervals)
                assert result["timings"]["runqueue_ms"] is not None or sys.platform != "linux"


def test_whole_call_timing_includes_tail_after_find(monkeypatch, tmp_path: Path) -> None:
    from exomem import call_spans, commands
    from exomem.governance import principal

    # The command boundary, not a reconstructed find snapshot, closes the clock.
    def find(*args, **kwargs):
        return {"hits": [], "timings": {"total_ms": 0.0, "stages": {}}}

    def tail(root, result, **kwargs):
        with call_spans.span("recall.due_state"):
            result["tail_complete"] = True
        return result

    monkeypatch.setattr(commands, "op_find", find)
    monkeypatch.setattr(commands, "_with_due_state", tail)
    with principal.library_scope():
        result = commands.op_ask_memory(tmp_path, query="topic", include_timings=True)
    assert result["tail_complete"] is True
    assert result["timings"]["total_ms"] > 0.0
    assert result["timings"]["stage_rows"]["governed-stage-due_state"]["ms"] > 0.0


@pytest.mark.parametrize("status", ["release_restricted", "governed_projection"])
def test_whole_call_finalization_keeps_public_suppression_exact(status: str) -> None:
    from exomem import call_spans, recall_timing

    result = {"hits": [], "timings_suppressed": {"status": status}}
    with recall_timing.measure_recall(True) as trace:
        with call_spans.span("recall.due_state"):
            pass
    assert recall_timing.completed(result, trace) == result


def test_result_only_reset_preserves_warm_pages_and_retrieval_results(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from synth_vault import gen_dense_vault

    from exomem import commands, find, lexstore
    from exomem.governance import principal

    gen_dense_vault(tmp_path, 12)
    lexstore.ensure_fresh(tmp_path)
    with principal.library_scope():
        first = commands.op_ask_memory(tmp_path, query="topic", mode="keyword", include_timings=True)
        warm = commands.op_ask_memory(tmp_path, query="topic", mode="keyword", include_timings=True)
        pages = dict(find._CACHE.entries)
        find.reset_result_cache()
        assert find._CACHE.entries == pages
        fresh = commands.op_ask_memory(tmp_path, query="topic", mode="keyword", include_timings=True)
    assert warm["timings"]["cache"]["hit"] is True
    assert fresh["timings"]["cache"]["hit"] is False
    assert first["hits"] == fresh["hits"]
