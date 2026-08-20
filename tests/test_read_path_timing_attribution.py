"""The read path must account for its own wall time, and not re-resolve invariants.

#283 reported a ~7 s warm hybrid search and named vector/BM25 as the cost. By the
time it was profiled neither was dominant, and the reason the report could be
stale for a month is that `FindTimings` had no way to say "28 % of this call
happened somewhere I do not measure". Two things are pinned here:

* `unattributed_ms` — wall time inside the call that no span claimed. An
  uninstrumented region now shows up in the timings payload itself.
* the memoized vault-root resolution — the profile showed
  `_canonical_parts_after_safe_validation` resolving the SAME vault root once
  per candidate (2,438 `_getfinalpathname` calls for one 588-candidate query).

The root memo is a security-adjacent cache, so its failure direction is pinned
too: a stale entry must refuse a page, never admit one.
"""

from __future__ import annotations

import time
from pathlib import Path

from exomem import commands, recall_policy
from exomem.find_types import FindTimings


def test_unattributed_time_counts_work_no_span_claimed() -> None:
    timings = FindTimings()
    with timings.span("measured"):
        time.sleep(0.05)
    time.sleep(0.05)  # nothing wraps this

    out = timings.as_dict()

    # Both sleeps are in `total`; only the first is inside a span. Bounds are
    # deliberately loose in the slow direction — a contended runner can stretch
    # a sleep but cannot make unattributed work vanish.
    assert out["stages"]["measured"]["ms"] >= 40.0
    assert out["unattributed_ms"] >= 40.0
    assert out["unattributed_ms"] <= out["total_ms"]


def test_a_fully_wrapped_call_reports_almost_no_unattributed_time() -> None:
    timings = FindTimings()
    with timings.span("everything"):
        time.sleep(0.05)

    out = timings.as_dict()

    # The only unclaimed time is the collector's own bookkeeping.
    assert out["unattributed_ms"] < 20.0


def test_a_nested_span_is_not_charged_twice() -> None:
    """Nesting must not make covered time exceed the wall clock.

    `graph` already wraps `graph.seeds` / `graph.expand` / `graph.resolver`, so
    summing stage times over-counts. `unattributed_ms` merges intervals instead,
    and would go negative-then-clamped-to-zero if it did not.
    """
    timings = FindTimings()
    with timings.span("outer"):
        time.sleep(0.03)
        with timings.span("inner"):
            time.sleep(0.05)
    time.sleep(0.06)  # genuinely unattributed, alongside the nesting

    out = timings.as_dict()

    assert out["stages"]["outer"]["ms"] >= out["stages"]["inner"]["ms"]
    # Summing the table claims ~130 ms of an ~140 ms call, so it would report
    # ~10 ms unattributed and swallow the 60 ms that really is unmeasured.
    # Merging claims ~80 ms and reports ~60 ms. Note the assertion has to be
    # made against a call that HAS unattributed time: with none, an over-count
    # clamps to zero and reads as correct.
    summed = sum(stage["ms"] for stage in out["stages"].values())
    assert summed > out["stages"]["outer"]["ms"]
    assert out["unattributed_ms"] >= 40.0


def test_a_stage_that_runs_twice_reports_both_runs() -> None:
    """`filter_eligibility` runs once per lane in "mixed" — assigning drops one."""
    timings = FindTimings()
    for _ in range(2):
        with timings.span("filter_eligibility"):
            time.sleep(0.03)

    stage = timings.as_dict()["stages"]["filter_eligibility"]

    assert stage["calls"] == 2
    assert stage["ms"] >= 55.0  # both runs, not the last one


def test_a_stage_that_runs_once_carries_no_call_count() -> None:
    """The single-run shape is pinned by exact-dict assertions elsewhere."""
    timings = FindTimings()
    with timings.span("keyword"):
        pass

    assert set(timings.as_dict()["stages"]["keyword"]) == {"ms"}


def test_the_query_log_projection_carries_the_unattributed_term() -> None:
    """A closed projection drops any field it does not name.

    The query log is where a slowly-growing uninstrumented term would become
    visible across many requests, so the field has to survive the trip. This is
    the same shape as a response field that never reaches its caller because a
    compact projection was not widened alongside it.
    """
    timings = FindTimings()
    with timings.span("bm25"):
        pass
    payload = timings.as_dict()

    summary = commands._timing_log_summary(payload)

    assert summary is not None
    assert summary["unattributed_ms"] == payload["unattributed_ms"]
    assert "bm25" in summary["stage_ms"]


def test_the_query_log_projection_tolerates_no_timings() -> None:
    assert commands._timing_log_summary(None) is None


def _kb_page(vault: Path, name: str) -> Path:
    page = vault / "Knowledge Base" / "Notes" / name
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: note\n---\n\nbody\n", encoding="utf-8")
    return page


def test_the_vault_root_is_resolved_once_not_once_per_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    """The fixed side of the alias comparison must not be a per-candidate syscall."""
    # The alias check is Windows-only in production; the seam exists so the
    # behaviour is testable on any platform (see its docstring).
    monkeypatch.setattr(recall_policy, "_needs_canonical_alias_check", lambda parts: True)
    recall_policy.clear_resolved_roots()

    pages = [_kb_page(tmp_path, f"page-{i}.md") for i in range(12)]

    resolved: list[str] = []
    real_resolve = Path.resolve

    def counting_resolve(self, *args, **kwargs):
        resolved.append(str(self))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counting_resolve)

    for page in pages:
        assert recall_policy.is_recall_candidate(tmp_path, page) is True

    root_resolves = [entry for entry in resolved if entry == str(tmp_path)]
    # Every candidate is still resolved on its own; only the root is remembered.
    assert len(root_resolves) == 1, f"root resolved {len(root_resolves)}x for 12 candidates"
    assert len(resolved) - len(root_resolves) == len(pages)


def test_a_stale_remembered_root_refuses_a_page_rather_than_admitting_it(
    tmp_path: Path, monkeypatch
) -> None:
    """The memo's only failure direction must be refusal.

    A cache that can admit a path outside the vault would turn a latency fix
    into a boundary hole, so prime it with the wrong root and prove the answer
    is False.
    """
    monkeypatch.setattr(recall_policy, "_needs_canonical_alias_check", lambda parts: True)
    page = _kb_page(tmp_path, "page.md")
    assert recall_policy.is_recall_candidate(tmp_path, page) is True

    elsewhere = tmp_path.parent / "somewhere-else"
    elsewhere.mkdir(exist_ok=True)
    recall_policy.clear_resolved_roots()
    monkeypatch.setattr(recall_policy, "_resolved_root", lambda root: elsewhere)

    assert recall_policy.is_recall_candidate(tmp_path, page) is False


def test_dropping_rebuildable_caches_drops_the_root_memo_too(tmp_path: Path, monkeypatch) -> None:
    """`unload_ram_caches` means "everything rebuildable" — no quiet exceptions."""
    from exomem import find as find_module

    monkeypatch.setattr(recall_policy, "_needs_canonical_alias_check", lambda parts: True)
    page = _kb_page(tmp_path, "page.md")
    recall_policy.is_recall_candidate(tmp_path, page)
    assert recall_policy._RESOLVED_ROOTS

    find_module.unload_ram_caches()

    assert not recall_policy._RESOLVED_ROOTS


def test_clearing_the_memo_makes_the_next_lookup_cold(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(recall_policy, "_needs_canonical_alias_check", lambda parts: True)
    page = _kb_page(tmp_path, "page.md")
    recall_policy.clear_resolved_roots()
    assert recall_policy.is_recall_candidate(tmp_path, page) is True

    resolved: list[str] = []
    real_resolve = Path.resolve

    def counting_resolve(self, *args, **kwargs):
        resolved.append(str(self))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counting_resolve)

    recall_policy.is_recall_candidate(tmp_path, page)
    assert str(tmp_path) not in resolved  # warm

    recall_policy.clear_resolved_roots()
    recall_policy.is_recall_candidate(tmp_path, page)
    assert str(tmp_path) in resolved  # cold again
