"""The dreamer on the shapes real vaults have (review round 2).

The first fixture had no `## Relations` section, no `exomem_id` and no Source
that many notes cite. Real vaults have all three, and each broke a rule:
authored relations built a whole-vault resolver, id-bearing pages wedged the
pass on a cold identity cache, a one-Source cluster filled the family cap for
good, and one Source edit fanned out into unbounded work inside one page.
"""

from __future__ import annotations

import time
from pathlib import Path

import dreamer_fixture as fx
import pytest

from exomem import (
    commands,
    dreamer,
    dreamer_families,
    dreamer_store,
    freshness,
    semantic_contract,
    upkeep,
)
from exomem import vault as vault_module

LATER = time.time() + 3 * 3600


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    semantic_contract.reset_corpus_context_cache()
    upkeep.reset_delivery_state()
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()
    semantic_contract.reset_corpus_context_cache()
    upkeep.reset_delivery_state()


def _quiet(vault: Path, *, now: float | None = None, limit: int = 60) -> list:
    results = fx.run_to_quiet(vault, now=now, limit=limit)
    assert all(result.stop_reason != "error" for result in results), results
    return results


def _rows(vault: Path, family: str | None = None, state: str | None = "open") -> list[dict]:
    view = dreamer_store.read_view(vault)
    return [
        row
        for row in (view.candidates if view else ())
        if (family is None or row["family"] == family) and (state is None or row["state"] == state)
    ]


class _Builds:
    """Counts whole-vault resolver builds and vault walks."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.builds = 0
        self.walks = 0
        real_build = vault_module.WikilinkResolver._build
        real_walk = vault_module.walk_vault_md

        def build(resolver):
            self.builds += 1
            return real_build(resolver)

        def walk(*args, **kwargs):
            self.walks += 1
            return real_walk(*args, **kwargs)

        monkeypatch.setattr(vault_module.WikilinkResolver, "_build", build)
        monkeypatch.setattr(vault_module, "walk_vault_md", walk)


# ----------------------------------------------------------------------
# F6: authored relations never build a whole-vault resolver
# ----------------------------------------------------------------------


def test_a_tick_over_relations_builds_no_resolver_and_walks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = fx.build_realistic(tmp_path)
    fx.warm_identity(vault, monkeypatch)
    spy = _Builds(monkeypatch)
    _quiet(vault)
    assert spy.builds == 0 and spy.walks == 0, (spy.builds, spy.walks)
    # The pass still proposes: the unauthored pair, and hydration.
    families = {row["family"] for row in _rows(vault)}
    assert families == {dreamer_families.LINK_FAMILY, dreamer_families.HYDRATION_FAMILY}


def test_authored_relations_still_suppress_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = fx.build_realistic(tmp_path, with_graph=False)
    # The inlet note now authors a relation to the cavitation note.
    text = (vault / fx.INLET).read_text(encoding="utf-8")
    fx.write(vault, fx.INLET, text + "- relates_to [[Notes/Insights/pump-cavitation]]\n")
    freshness.clear()
    fx.seed(vault)
    fx.publish_graph(vault)
    fx.warm_identity(vault, monkeypatch)
    spy = _Builds(monkeypatch)
    _quiet(vault)
    assert spy.builds == 0
    subjects = {row["subject_path"] for row in _rows(vault, dreamer_families.LINK_FAMILY)}
    assert subjects == set()


def test_item_and_context_over_relations_build_no_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = fx.build_realistic(tmp_path)
    fx.warm_identity(vault, monkeypatch)
    _quiet(vault)
    link = _rows(vault, dreamer_families.LINK_FAMILY)[0]
    ref = upkeep.upkeep_ref(link["id"])
    spy = _Builds(monkeypatch)
    commands.op_review_memory(vault, mode="item", ref=ref)
    commands.op_review_item_context(vault, ref=ref)
    assert spy.builds == 0 and spy.walks == 0, (spy.builds, spy.walks)


# ----------------------------------------------------------------------
# F4: the family cap counts only rows still eligible, and never starves
# ----------------------------------------------------------------------


def _cluster_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int = 14) -> Path:
    vault = tmp_path / "vault"
    fx.write(vault, fx.SOURCE_ONE, fx.source("Field report one"))
    fx.write(vault, fx.SOURCE_TWO, fx.source("Field report two"))
    fx.cluster(vault, count, "field-report-one")
    fx.seed(vault)
    fx.publish_graph(vault)
    fx.warm_identity(vault, monkeypatch)
    return vault


def test_decided_rows_leave_room_for_a_fresh_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _cluster_vault(tmp_path, monkeypatch)
    _quiet(vault)
    first = _rows(vault, dreamer_families.LINK_FAMILY)
    assert len(first) == dreamer_store.MAX_OPEN_PER_FAMILY
    for row in first:
        commands.op_triage_memory(
            vault,
            ref=row["ref"],
            action="dismiss",
            why="false_positive: unrelated notes",
            source_path=row["subject_path"],
        )
    # A fresh, undecided pair on another Source.
    fresh = []
    for index in (900, 901):
        rel = f"{fx.KB}/Notes/Insights/fresh-{index}.md"
        text = fx.insight(f"Fresh {index}", sources=["field-report-two"], updated="2026-05-02")
        fx.write(vault, rel, fx.with_id(text, rel))
        fresh.append(rel)
    freshness.on_files_changed(vault, changed=[vault / rel for rel in fresh])
    fx.publish_graph(vault)
    fx.warm_identity(vault, monkeypatch)
    _quiet(vault)
    subjects = {row["subject_path"] for row in _rows(vault, dreamer_families.LINK_FAMILY)}
    assert subjects & set(fresh), "the fresh pair was starved by decided rows"
    _quiet(vault, now=LATER)
    listed = upkeep.review(vault, state="open", limit=50)
    assert any(item["dispose"]["args"].get("source_path") in fresh for item in listed["items"])


# ----------------------------------------------------------------------
# F5: one page's work is bounded; cited subjects are requeued, not redone inline
# ----------------------------------------------------------------------


def _pending(vault: Path) -> list[str]:
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    try:
        return [str(row[0]) for row in conn.execute("SELECT path FROM pending ORDER BY path")]
    finally:
        conn.close()


def test_a_widely_cited_source_edit_requeues_its_subjects_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph, relation_queue

    vault = _cluster_vault(tmp_path, monkeypatch)
    _quiet(vault)
    citing = {
        row["subject_path"]
        for row in _rows(vault)
        if any(item.get("path") == fx.SOURCE_ONE for item in row.get("evidence") or ())
    }
    assert len(citing) > 1
    calls = {"page_candidates": 0, "snapshots": 0}
    real_candidates = relation_queue._page_candidates
    real_open = epistemic_graph.EpistemicGraphIndex._open_read_snapshot

    def counted_candidates(*args, **kwargs):
        calls["page_candidates"] += 1
        return real_candidates(*args, **kwargs)

    def counted_open(index):
        calls["snapshots"] += 1
        return real_open(index)

    monkeypatch.setattr(relation_queue, "_page_candidates", counted_candidates)
    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "_open_read_snapshot", counted_open)
    fx.edit(vault, fx.SOURCE_ONE, fx.source("Field report one") + "\nMore raw notes.\n")
    fx.warm_identity(vault, monkeypatch)
    tick = dreamer.run_once(vault, budget=dreamer.Budget(pages=1))
    assert tick.processed == (fx.SOURCE_ONE,), tick
    # The Source's own page does no link work; its citing subjects wait their turn.
    assert calls["page_candidates"] == 0
    assert citing <= set(_pending(vault))
    print(f"\nFANOUT source page cpu={tick.cpu:.3f}s wall={tick.wall:.3f}s")
    assert tick.cpu < 1.0

    # Each subject is then one page of its own: one neighbourhood, one snapshot.
    calls.update(page_candidates=0, snapshots=0)
    results = _quiet(vault)
    processed = [rel for result in results for rel in result.processed]
    assert len(processed) == len(set(processed))
    assert calls["page_candidates"] == len([rel for rel in processed if rel in citing])
    assert calls["snapshots"] <= len(processed)


def test_a_requeued_subject_does_not_requeue_its_neighbours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _cluster_vault(tmp_path, monkeypatch, count=4)
    _quiet(vault)
    fx.edit(vault, fx.SOURCE_ONE, fx.source("Field report one") + "\nMore raw notes.\n")
    fx.warm_identity(vault, monkeypatch)
    results = _quiet(vault)
    processed = [rel for result in results for rel in result.processed]
    # The Source, then each citing note once: an unchanged page requeues nothing.
    assert processed[0] == fx.SOURCE_ONE
    assert len(processed) == len(set(processed)) <= 5
    assert _pending(vault) == []


# ----------------------------------------------------------------------
# F2: a page that cannot run yet is held, visibly, without spinning
# ----------------------------------------------------------------------


def _seen(vault: Path) -> set[str]:
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    try:
        return {str(row[0]) for row in conn.execute("SELECT path FROM seen")}
    finally:
        conn.close()


def test_a_cold_identity_cache_holds_its_pages_and_lets_the_rest_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Id-bearing pages and a cold identity cache, as after a restart with no
    # write since: the suites disable the corpus-context cache.
    monkeypatch.setenv("EXOMEM_DREAMER", "on")
    vault = fx.build_realistic(tmp_path)
    results = [dreamer.run_once(vault, budget=dreamer.Budget(pages=1)) for _ in range(10)]
    assert all(result.stop_reason != "error" for result in results), results
    held = set(_pending(vault))
    assert held and held <= {fx.CAVITATION, fx.SEAL_WEAR, fx.INLET, fx.ENTITY}, held
    # The pages queued behind the held ones were not starved.
    assert {fx.SOURCE_ONE, fx.SOURCE_TWO, fx.SOURCE_THREE} <= _seen(vault)
    last = results[-1]
    assert last.processed == () and last.stop_reason == "deferred", last
    status = dreamer.status(vault)
    assert status["state"] == "waiting", status
    assert status["waiting_reason"] == "identity_cache_cold", status
    assert status["sidecar"]["waiting_reason"] == "identity_cache_cold", status

    # Once the cache is warm the held pages go through and the reason clears.
    fx.warm_identity(vault, monkeypatch)
    _quiet(vault)
    assert _pending(vault) == []
    assert held <= _seen(vault)
    assert dreamer.status()["waiting_reason"] is None


def test_a_held_pass_ticks_at_most_once_per_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import dreamer_policy

    poll = 1.0
    monkeypatch.setattr(dreamer_policy, "IDLE_SECONDS", 0.5)
    monkeypatch.setattr(dreamer_policy, "SETTLE_FLOOR_SECONDS", 0.5)
    monkeypatch.setattr(dreamer_policy, "settle_seconds", lambda _last: 0.5)
    monkeypatch.setattr(dreamer_policy, "POLL_SECONDS", poll)
    monkeypatch.setattr(dreamer_policy, "MIN_SLEEP_SECONDS", 0.1)
    monkeypatch.setenv("EXOMEM_DREAMER", "on")
    vault = fx.build_realistic(tmp_path)
    ticks: list[tuple[float, object]] = []
    real = dreamer.run_once

    def counted(*args, **kwargs):
        result = real(*args, **kwargs)
        ticks.append((time.monotonic(), result))
        return result

    monkeypatch.setattr(dreamer, "run_once", counted)
    dreamer.start(vault)
    try:
        time.sleep(6.0)
    finally:
        dreamer.stop(timeout=5)
    stalled = [result for _at, result in ticks if not result.processed]
    assert stalled, ticks
    gaps = [
        later_at - at
        for (at, result), (later_at, _later) in zip(ticks, ticks[1:])
        if not result.processed
    ]
    print(f"\nHELD ticks={len(ticks)} stalled={len(stalled)} gaps={[round(g, 2) for g in gaps]}")
    assert all(gap >= poll * 0.9 for gap in gaps), gaps
    assert dreamer.status()["waiting_reason"] == "identity_cache_cold"
