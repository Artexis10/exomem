"""A whole-vault pass that saw only recorded movement publishes behind.

Governed writes land while a whole-vault graph pass runs, and on a large vault
the pass runs for minutes. Before this change any such write invalidated the
pass: it re-targeted, ran out of its budget, failed, and the drain ran the whole
pass again once writes stopped. The registry had recorded every change, so the
pass was never evidence of anything stale.

The contract these tests pin (OpenSpec `converge-graph-incrementally` and
`rebuild-graph-without-blocking-writes`): a pass whose movement the registry
recorded, and which saw no retitled or vanished page, publishes its rows as a
graph that reports itself not current, queues the movement, and the drain
converges it to exactly a fresh rebuild without a second whole-vault pass. A
pass that saw a retitle is never published that way. The graph's public
guarantee is unchanged: it equals a fresh rebuild, or it says it is not current.

Every write goes through `writer_lease.LeaseManager` (the helper in
`test_graph_class_c_evidence`), so the registry and the durable queue learn of
it through the production canonical batch, not a seeded map.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from test_graph_class_c_evidence import EVERY_PASS, _governed_write, _seed_live_freshness

from exomem import deferred_index, epistemic_graph, freshness, graph_drain, graph_sync
from exomem import find as find_module
from exomem.epistemic_graph import EpistemicGraphIndex

NOTES = "Knowledge Base/Notes/Insights"
PAGE_A = f"{NOTES}/behind-a.md"
PAGE_B = f"{NOTES}/behind-b.md"
PAGE_C = f"{NOTES}/behind-c.md"
PAGE_D = f"{NOTES}/behind-d.md"
PAGE_E = f"{NOTES}/behind-e.md"


def _page(title: str, body: str) -> str:
    return f"---\ntype: insight\nstatus: active\n---\n# {title}\n\n## Claim\n\n{body}\n"


@pytest.fixture(autouse=True)
def _stop_the_daemon() -> Iterator[None]:
    yield
    graph_drain.stop()
    graph_drain._DEBT.clear()


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Four pages: A links to B by name and to D, which does not exist yet; C
    links to B by title, so a retitle of B moves C's edge; E links to A."""
    root = tmp_path / "vault"
    (root / NOTES).mkdir(parents=True)
    (root / PAGE_A).write_text(
        _page("A", "A claims against [[behind-b]] and cites [[behind-d]]."), encoding="utf-8"
    )
    (root / PAGE_B).write_text(_page("B", "B is a plain claim."), encoding="utf-8")
    (root / PAGE_C).write_text(_page("C", "C cites [[B]]."), encoding="utf-8")
    _seed_live_freshness(root)
    # One governed write first, so the published graph acknowledges a checkpoint
    # lineage as a served vault's always does.
    _governed_write(root, PAGE_E, _page("E", "E cites [[behind-a]]."))
    EpistemicGraphIndex(root).rebuild_all()
    assert graph_sync.acknowledged_generation(root) == 1
    epistemic_graph.clear_publication_memos()
    yield root
    graph_sync.drain_active_rebuilds(timeout=60.0)
    epistemic_graph.clear_publication_memos()


class _Passes:
    """Count whole-vault passes and run `before`/`after` hooks around the first ones.

    `after` lands once the pass has read the vault and before it proves
    anything: the window a concurrent writer hits. `before` lands after the pass
    built its resolver and before it reads any page. Only passes on the thread
    that installed the hooks act, so a write's own work never triggers them.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        before: Callable[[int], None] | None = None,
        after: Callable[[int], None] | None = None,
        times: int = 1,
    ) -> None:
        self.count = 0
        owner = threading.get_ident()
        original = EpistemicGraphIndex._rebuild_all_pass

        def hooked(index: EpistemicGraphIndex, *args: Any, **kwargs: Any) -> Any:
            acting = threading.get_ident() == owner and self.count < times
            number = self.count
            self.count += 1
            if acting and before is not None:
                before(number)
            report = original(index, *args, **kwargs)
            if acting and after is not None:
                after(number)
            return report

        monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_pass", hooked)


def _replacements(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[bool]:
    """Record, for each live replacement, whether it left the graph fenced.

    A behind publication leaves the persisted read barrier set; a current one
    clears it. That is what a reader sees, so it is what the tests assert on.
    """
    fenced: list[bool] = []
    real = graph_sync.replace_sidecar

    def spy(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)
        fenced.append(EpistemicGraphIndex(root).reads_suspended())

    monkeypatch.setattr(graph_sync, "replace_sidecar", spy)
    return fenced


def _owe_a_whole_vault_rebuild(root: Path) -> None:
    """Raise whole-vault debt the way the drain does for an unreadable graph."""
    generation = int(graph_sync.status(root).get("generation") or 0)
    deferred_index.mark_graph_full_rebuild(root, generation=generation)


def _drain_to_empty(root: Path) -> None:
    for _ in range(12):
        if not deferred_index.list_graph_paths(root) and (
            deferred_index.graph_full_rebuild_pending(root) is None
        ):
            return
        graph_drain._work_once(root)


def _graph_rows(root: Path) -> tuple[set[tuple[object, ...]], list[tuple[object, ...]]]:
    connection = sqlite3.connect(epistemic_graph.sidecar_path(root))
    try:
        edges = connection.execute(
            "SELECT src_key, dst_key, relation_type, origin, source_path, "
            "COALESCE(source_anchor, ''), metadata FROM graph_edges"
        ).fetchall()
        files = connection.execute(
            "SELECT path, source_hash FROM graph_nodes WHERE kind = 'file' ORDER BY 1"
        ).fetchall()
    finally:
        connection.close()
    return set(edges), files


def _assert_matches_a_fresh_rebuild(root: Path) -> None:
    """Every page's rows, not only the moved ones, equal a whole-vault rebuild."""
    drained_edges, drained_files = _graph_rows(root)
    EpistemicGraphIndex(root).rebuild_all()
    rebuilt_edges, rebuilt_files = _graph_rows(root)
    assert drained_files == rebuilt_files
    missing = sorted(map(str, rebuilt_edges - drained_edges))
    extra = sorted(map(str, drained_edges - rebuilt_edges))
    assert not missing and not extra, f"missing={missing[:5]} extra={extra[:5]}"


def _assert_published_behind_then_converges(
    root: Path, passes: _Passes, fenced: list[bool], moved: set[str]
) -> None:
    assert fenced == [True], (
        "a pass whose movement the registry recorded must publish once, fenced, "
        f"instead of re-running the whole vault: replacements={fenced} passes={passes.count}"
    )
    assert passes.count == 1
    graph = EpistemicGraphIndex(root)
    assert graph.available() is False, "a behind graph was served as current"
    assert moved <= set(deferred_index.list_graph_paths(root)), (
        "the movement the pass missed must be queued before the replacement"
    )
    assert deferred_index.graph_full_rebuild_pending(root) is None, (
        "the publication paid the whole-vault debt it observed"
    )

    _drain_to_empty(root)

    assert passes.count == 1, "the drain paid a second whole-vault pass"
    assert deferred_index.list_graph_paths(root) == []
    assert EpistemicGraphIndex(root).available() is True
    assert epistemic_graph.graph_drift(root) == []
    _assert_matches_a_fresh_rebuild(root)


def test_a_body_edit_mid_pass_publishes_behind_and_drains_to_a_fresh_rebuild(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def edit_b(_number: int) -> None:
        _governed_write(vault, PAGE_B, _page("B", "B now cites [[behind-c]]."))

    passes = _Passes(monkeypatch, after=edit_b)
    fenced = _replacements(monkeypatch, vault)
    _owe_a_whole_vault_rebuild(vault)

    graph_drain._work_once(vault)

    _assert_published_behind_then_converges(vault, passes, fenced, {PAGE_B})


def test_a_page_created_mid_pass_publishes_behind_and_drains_to_a_fresh_rebuild(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass reads a page its resolver never held, so that page's rows go.

    The create lands while the walk is still running, and the walk yields it.
    Its rows were derived under a resolver that did not know it, so they are
    dropped before publication; A's link to it was a placeholder under that
    resolver and only the drain may resolve it.
    """
    owner = threading.get_ident()
    in_pass = threading.Event()
    created: list[bool] = []
    real_walk = find_module._walk_md

    def walk(kb: Path, *args: Any, **kwargs: Any) -> Iterator[Path]:
        yield from real_walk(kb, *args, **kwargs)
        if threading.get_ident() == owner and in_pass.is_set() and not created:
            created.append(True)
            _governed_write(vault, PAGE_D, _page("D", "D cites [[behind-e]]."))
            yield vault / PAGE_D

    monkeypatch.setattr(find_module, "_walk_md", walk)
    passes = _Passes(monkeypatch, before=lambda _n: in_pass.set(), after=lambda _n: in_pass.clear())
    fenced = _replacements(monkeypatch, vault)
    _owe_a_whole_vault_rebuild(vault)

    graph_drain._work_once(vault)

    assert created == [True]
    _assert_published_behind_then_converges(vault, passes, fenced, {PAGE_D})


def test_a_retitle_the_pass_saw_is_never_published_behind(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B is retitled after the resolver was built and before the pass read it.

    The pass derived C's `[[B]]` edge under the old title while B's own row
    carries the new one. A drain widens a queued page by the title its row
    stores, which is already the new one, so it would never re-target C: the
    stale edge would be served as current. The pass re-targets instead.
    """

    def retitle_b(_number: int) -> None:
        _governed_write(vault, PAGE_B, _page("Bee", "B is a plain claim."))

    passes = _Passes(monkeypatch, before=retitle_b)
    fenced = _replacements(monkeypatch, vault)
    _owe_a_whole_vault_rebuild(vault)

    graph_drain._work_once(vault)
    _drain_to_empty(vault)

    assert True not in fenced, f"a pass that saw a retitle published behind: {fenced}"
    assert passes.count >= 2, "the pass that saw the retitle must re-target"
    assert EpistemicGraphIndex(vault).available() is True
    _assert_matches_a_fresh_rebuild(vault)


def test_a_retitle_after_the_pass_read_the_page_publishes_behind_and_converges(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B's row still carries the title the resolver held, so the drain widens to C.

    The boundary of the rule above: the pass never saw this retitle, its rows
    agree with its resolver, and the queued retitle repairs C's `[[B]]` edge.
    """

    def retitle_b(_number: int) -> None:
        _governed_write(vault, PAGE_B, _page("Bee", "B is a plain claim."))

    passes = _Passes(monkeypatch, after=retitle_b)
    fenced = _replacements(monkeypatch, vault)
    _owe_a_whole_vault_rebuild(vault)

    graph_drain._work_once(vault)

    _assert_published_behind_then_converges(vault, passes, fenced, {PAGE_B})


def test_a_page_removed_before_the_pass_read_it_is_never_published_behind(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolver held B; the pass found no file and wrote no row for it.

    A drain widens a removed page by the row it stores, and there is none, so
    C's `[[B]]` edge -- derived under a resolver that still held B -- would
    never be re-targeted. The pass re-targets instead.
    """

    def remove_b(_number: int) -> None:
        (vault / PAGE_B).unlink()
        # Recorded, as the watcher records an outside removal.
        EpistemicGraphIndex(vault)._reconcile_recall_publication()

    passes = _Passes(monkeypatch, before=remove_b)
    fenced = _replacements(monkeypatch, vault)
    _owe_a_whole_vault_rebuild(vault)

    graph_drain._work_once(vault)
    _drain_to_empty(vault)

    assert True not in fenced, f"a pass that saw a removed page published behind: {fenced}"
    assert passes.count >= 2, "the pass that saw the removal must re-target"
    assert EpistemicGraphIndex(vault).available() is True
    _assert_matches_a_fresh_rebuild(vault)


def _marks(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[tuple[str, ...]]:
    marks: list[tuple[str, ...]] = []
    original_mark = freshness.mark_external_pending

    def spy(vault_root: Path, *, paths: Any = ()) -> int:
        paths = tuple(paths)
        marks.append(tuple(str(Path(path).resolve()) for path in paths))
        return original_mark(vault_root, paths=paths)

    monkeypatch.setattr(freshness, "mark_external_pending", spy)
    return marks


def _after_each_resolver(monkeypatch: pytest.MonkeyPatch, action: Callable[[int], None]) -> None:
    """Run `action` after the pass builds its resolver, before it proves the bytes.

    The resolver-source-versions proof then reads a vault the resolver did not
    see, which is the Class C trigger the pass checks before reading any page.
    """
    owner = threading.get_ident()
    real = find_module.recall_resolver_snapshot
    calls = [0]

    def hooked(*args: Any, **kwargs: Any) -> Any:
        resolver = real(*args, **kwargs)
        if threading.get_ident() == owner and calls[0] < EVERY_PASS:
            action(calls[0])
            calls[0] += 1
        return resolver

    monkeypatch.setattr(find_module, "recall_resolver_snapshot", hooked)


def test_a_recorded_create_during_the_resolver_proof_is_class_b_with_no_mark(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marks = _marks(monkeypatch, vault)

    def create(number: int) -> None:
        _governed_write(vault, f"{NOTES}/created-{number}.md", _page(f"N{number}", "New."))

    _after_each_resolver(monkeypatch, create)

    with pytest.raises(Exception) as raised:  # noqa: PT011 - the class is the assertion
        EpistemicGraphIndex(vault).rebuild_all()

    assert not isinstance(raised.value, epistemic_graph.GraphProjectionMoved), (
        f"recorded movement was classified Class C: {raised.value}"
    )
    assert epistemic_graph.is_publication_failure(raised.value) is True, repr(raised.value)
    assert marks == [], f"recorded movement marked the registry: {marks}"
    assert freshness.external_pending_epoch(vault) is None


def test_an_unrecorded_edit_during_the_resolver_proof_is_class_c_scoped_to_its_path(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marks = _marks(monkeypatch, vault)

    def retitle_out_of_band(number: int) -> None:
        (vault / PAGE_B).write_text(_page(f"B{number}", "Edited outside every recorder."), encoding="utf-8")

    _after_each_resolver(monkeypatch, retitle_out_of_band)

    with pytest.raises(epistemic_graph.GraphProjectionMoved, match="Class C"):
        EpistemicGraphIndex(vault).rebuild_all()

    assert marks == [(str((vault / PAGE_B).resolve()),)], (
        "an unexplained difference the proof could name must be marked scoped "
        f"to it, never unscoped: {marks}"
    )


@pytest.mark.parametrize("change", ["access_policy", "external_epoch"])
def test_the_behind_ticket_refuses_a_changed_policy_or_a_newer_external_epoch(
    vault: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """The change lands under the publication hold, after the first ticket check.

    The ticket's final check runs after the live readers drain, so it must see
    the change and refuse; the next attempt publishes a current graph.
    """

    def edit_b(_number: int) -> None:
        _governed_write(vault, PAGE_B, _page("B", "B now cites [[behind-c]]."))

    passes = _Passes(monkeypatch, after=edit_b)
    fenced = _replacements(monkeypatch, vault)
    real_hold = EpistemicGraphIndex._before_publish_replacement
    changed: list[str] = []

    def change_under_the_hold(index: EpistemicGraphIndex, *args: Any, **kwargs: Any) -> Any:
        if not changed:
            changed.append(change)
            if change == "access_policy":
                (vault / "Knowledge Base" / "_access.yaml").write_text(
                    "readonly: []\n", encoding="utf-8"
                )
            else:
                freshness.mark_external_pending(vault, paths=[vault / PAGE_C])
        return real_hold(index, *args, **kwargs)

    monkeypatch.setattr(EpistemicGraphIndex, "_before_publish_replacement", change_under_the_hold)
    _owe_a_whole_vault_rebuild(vault)

    graph_drain._work_once(vault)
    _drain_to_empty(vault)

    assert changed == [change]
    assert True not in fenced, f"the ticket published behind across a moved {change}"
    assert passes.count >= 2
    assert EpistemicGraphIndex(vault).available() is True
    _assert_matches_a_fresh_rebuild(vault)


def test_a_registered_rebuild_that_published_behind_owes_its_waiters_no_newer_pass(
    tmp_path: Path,
) -> None:
    """Writes that register while the pass runs are already in its queued movement.

    Building the newer registration would pay the second whole-vault pass this
    change removes, so every waiter, older and newer, is told the repair is
    queued instead.
    """
    root = tmp_path / "vault"
    root.mkdir()

    def checkpoint(generation: int) -> graph_sync.GraphSyncCheckpoint:
        return graph_sync.GraphSyncCheckpoint.create(
            generation=generation,
            mutation_id=f"{generation:024d}",
            paths=((PAGE_B, "d" * 64),),
            created_paths=(),
        )

    coordinator = graph_sync.GraphRebuildCoordinator(root)
    started = threading.Event()
    release = threading.Event()
    built: list[int] = []

    def publish_behind(required: graph_sync.GraphSyncCheckpoint) -> graph_sync.GraphBuildOutcome:
        built.append(required.generation)
        started.set()
        release.wait(10.0)
        raise graph_sync.GraphRepairQueued()

    first = coordinator.start_or_join(checkpoint(1), publish_behind)
    assert started.wait(10.0)
    newer = coordinator.start_or_join(checkpoint(2), publish_behind)
    release.set()

    for waiter in (first, newer):
        with pytest.raises(graph_sync.GraphRepairQueued) as raised:
            waiter.wait(10.0)
        assert raised.value.code == "GRAPH_SYNC_REPAIR_QUEUED"
    assert built == [1], f"a newer whole-vault pass ran after a behind publication: {built}"
