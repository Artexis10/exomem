"""An in-flight governed write is not unexplained movement.

The CI gate `graph convergence (concurrent writer)` intermittently ended with
one drift finding. The sidecar was correct; the only false term was
`freshness.external_pending`, which fences every public graph read.

How it got set. `remember` runs under the narrow writer boundary, so its
rename and its registry publication happen under no lock a whole-vault rebuild
waits on. A rebuild's movement proof sampled the disk after the rename and
before the registry publication, found the page differing from a registry
whose history did not name it, classified it as unrecorded (Class C) and,
once the attempt budget ran out under the continuing writes, marked the page
externally pending. Nothing retired that mark until the watcher's five-minute
recovery.

The contract pinned here:

* movement from an in-flight governed write is never evidence that the
  registry is behind the disk. The write registered a publication intent for
  its exact staged bytes before its first rename, so the proof can tell those
  bytes apart from anything else;
* anything else on that path -- a foreign edit landing while the write is in
  flight, even one of the same size -- is still unexplained;
* an external-pending epoch a graph rebuild mints, the rebuild also retires:
  the registry is reconciled against the disk before the refusal propagates;
* `graph_drift` names the refusal it actually hit.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from exomem import epistemic_graph, file_watcher, freshness, graph_sync
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

PAGE_A = "Knowledge Base/Notes/Insights/inflight-a.md"
PAGE_B = "Knowledge Base/Notes/Insights/inflight-b.md"
PAGE_C = "Knowledge Base/Notes/Insights/inflight-c.md"

#: Enough recorded writes to exhaust the re-target budget (#576: eight
#: attempts), so a test that expects no Class C cannot pass by stabilizing.
EVERY_PASS = 12
WRITER_THREAD = "inflight-test-writer"


def _page(title: str, body: str) -> str:
    return f"---\ntype: insight\nstatus: active\n---\n# {title}\n\n## Claim\n\n{body}\n"


def _seed_live_freshness(root: Path) -> None:
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )
    freshness.seed(
        root,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(root / "Knowledge Base")
        ),
    )


def _run_governed_write(root: Path, rel: str, content: str, failure: list[BaseException]) -> None:
    """One committed `remember`-shaped write through the lease manager.

    `remember` is a narrow-boundary command, which is exactly why its rename
    and its registry publication are not serialized against a rebuild.
    """
    from exomem.writer_lease import get_manager, mark_active_mutation_committed

    def leaf(vault_root: Path, **_kwargs: Any) -> dict[str, Any]:
        target = vault_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(target, content)], vault_root=vault_root
        )
        mark_active_mutation_committed()
        return {"status": "committed", "mutated": True}

    saved = epistemic_graph.graph_scheduling_enabled
    epistemic_graph.graph_scheduling_enabled = lambda: False
    try:
        command = SimpleNamespace(name="remember", read_only=False, leaf=leaf)
        get_manager().invoke(command, (root,), {})
    except BaseException as error:  # noqa: BLE001 - re-raised on the caller's thread
        failure.append(error)
    finally:
        epistemic_graph.graph_scheduling_enabled = saved


def _governed_write(root: Path, rel: str, content: str) -> None:
    failure: list[BaseException] = []
    thread = threading.Thread(
        target=_run_governed_write, args=(root, rel, content, failure), name="inflight-recorded"
    )
    thread.start()
    thread.join(timeout=60.0)
    assert not thread.is_alive(), "the governed write did not finish"
    if failure:
        raise failure[0]


class _HeldWrite:
    """A governed write held between its rename and its registry publication.

    The post-commit fan-out's `register_self_write` is the registry
    publication; holding it on the writer's own thread freezes the write at
    exactly the interleaving the convergence gate kept losing: new bytes on
    disk, the registry not yet told.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, root: Path, rel: str, content: str):
        self.root = root
        self.rel = rel
        self.content = content
        self.installed = threading.Event()
        self.release = threading.Event()
        self.failure: list[BaseException] = []
        self.thread: threading.Thread | None = None
        original = file_watcher.register_self_write

        def held(*args: Any, **kwargs: Any) -> Any:
            if threading.current_thread().name == WRITER_THREAD and not self.installed.is_set():
                self.installed.set()
                # Bounded so a broken test can never hang the suite.
                self.release.wait(timeout=30.0)
            return original(*args, **kwargs)

        monkeypatch.setattr(file_watcher, "register_self_write", held)

    def start(self) -> None:
        self.thread = threading.Thread(
            target=_run_governed_write,
            args=(self.root, self.rel, self.content, self.failure),
            name=WRITER_THREAD,
        )
        self.thread.start()
        assert self.installed.wait(timeout=30.0), "the governed write never reached publication"
        assert (self.root / self.rel).read_text(encoding="utf-8") == self.content

    def finish(self) -> None:
        self.release.set()
        if self.thread is not None:
            self.thread.join(timeout=60.0)
            assert not self.thread.is_alive(), "the held governed write did not finish"
            self.thread = None
        if self.failure:
            raise self.failure[0]


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "vault"
    (root / "Knowledge Base/Notes/Insights").mkdir(parents=True)
    (root / PAGE_A).write_text(_page("A", "A claims against [[inflight-b]]."), encoding="utf-8")
    (root / PAGE_B).write_text(_page("B", "B is a plain claim."), encoding="utf-8")
    (root / PAGE_C).write_text(_page("C", "C cites [[inflight-a]]."), encoding="utf-8")
    _seed_live_freshness(root)
    EpistemicGraphIndex(root).rebuild_all()
    epistemic_graph.clear_publication_memos()
    yield root
    graph_sync.drain_active_rebuilds(timeout=60.0)
    epistemic_graph.clear_publication_memos()


@pytest.fixture
def held_write(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_HeldWrite]:
    held = _HeldWrite(
        monkeypatch, vault, PAGE_B, _page("B", "B is a plain claim, rewritten in flight.")
    )
    try:
        yield held
    finally:
        held.release.set()
        if held.thread is not None:
            held.thread.join(timeout=60.0)


class _MidPassAction:
    """Run `action` after each whole-vault pass on the rebuilding thread."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        action: Callable[[int], None],
        *,
        times: int,
    ) -> None:
        self.acted = 0
        self._busy = False
        owner = threading.get_ident()
        original = EpistemicGraphIndex._rebuild_all_pass

        def hooked(index: EpistemicGraphIndex, *args: Any, **kwargs: Any) -> Any:
            report = original(index, *args, **kwargs)
            if threading.get_ident() == owner and not self._busy and self.acted < times:
                self._busy = True
                try:
                    action(self.acted)
                    self.acted += 1
                finally:
                    self._busy = False
            return report

        monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_pass", hooked)


def _spy_marks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    marks: list[tuple[str, ...]] = []
    original = freshness.mark_external_pending

    def spy(vault_root: Path, *, paths: Any = ()) -> int:
        paths = tuple(paths)
        marks.append(tuple(sorted(str(Path(path).resolve()) for path in paths)))
        return original(vault_root, paths=paths)

    monkeypatch.setattr(freshness, "mark_external_pending", spy)
    return marks


# --- (a) the in-flight write is recorded movement ----------------------------


def test_a_write_between_its_rename_and_its_registry_publication_is_recorded(
    vault: Path, held_write: _HeldWrite
) -> None:
    """The exact interleaving: renamed bytes on disk, registry not yet told."""
    held_write.start()

    sample = EpistemicGraphIndex(vault)._unexplained_differences()

    assert sample is not None
    _lineage, _disk, differing, unexplained = sample
    assert PAGE_B in differing, "the held write did not put the registry behind the disk"
    assert PAGE_B not in unexplained, (
        "a governed write's own staged bytes, renamed before its registry "
        "publication, were classified as unrecorded movement"
    )
    held_write.finish()
    assert freshness.external_pending(vault) is False


def test_a_rebuild_racing_an_in_flight_write_leaves_no_fence_behind(
    vault: Path, held_write: _HeldWrite, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's failure end to end: the first pass lands mid-commit, the rest
    under ordinary recorded writes until the attempt budget runs out."""
    marks = _spy_marks(monkeypatch)

    def action(number: int) -> None:
        if number == 0:
            held_write.start()
            return
        held_write.finish()
        _governed_write(vault, PAGE_C, _page("C", f"C cites [[inflight-a]], revision {number}."))

    _MidPassAction(monkeypatch, action, times=EVERY_PASS)

    with pytest.raises(Exception) as raised:  # noqa: PT011 - the class is the assertion
        EpistemicGraphIndex(vault).rebuild_all()
    held_write.finish()

    assert not isinstance(raised.value, epistemic_graph.GraphProjectionMoved), (
        f"an in-flight governed write was classified Class C: {raised.value}"
    )
    assert epistemic_graph.is_publication_failure(raised.value) is True, repr(raised.value)
    assert marks == [], f"the rebuild minted external-pending marks: {marks}"
    assert freshness.external_pending(vault) is False


# --- the bound: a foreign edit on the same path is still evidence ------------


def test_a_foreign_edit_during_an_in_flight_write_is_still_unexplained(
    vault: Path, held_write: _HeldWrite
) -> None:
    """The intent explains the writer's exact bytes and nothing else.

    The foreign bytes are the same length as the writer's, so a stat or size
    signature could not tell them apart; only the staged content hash can.
    """
    held_write.start()
    staged = (vault / PAGE_B).read_bytes()
    foreign = bytes(reversed(staged[-8:]))
    foreign = staged[:-8] + (foreign if foreign != staged[-8:] else b"XXXXXXXX")
    assert len(foreign) == len(staged) and foreign != staged
    (vault / PAGE_B).write_bytes(foreign)

    sample = EpistemicGraphIndex(vault)._unexplained_differences()

    assert sample is not None
    _lineage, _disk, _differing, unexplained = sample
    assert PAGE_B in unexplained, (
        "a foreign edit landing during an in-flight governed write was hidden "
        "behind the write's publication intent"
    )


def test_an_aborted_write_explains_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an outstanding intent explains movement; a terminal one does not."""
    target = vault / PAGE_B
    content = _page("B", "B, staged by a write that will abort.").encode("utf-8")
    import hashlib
    import os

    staged = vault.parent / "staged-b.tmp"
    staged.write_bytes(content)
    descriptor = os.open(staged, os.O_RDONLY)
    try:
        intents = file_watcher.register_publication_intents(
            vault, [(target, descriptor, hashlib.sha256(content).hexdigest())]
        )
    finally:
        os.close(descriptor)
    assert intents
    file_watcher.begin_publication_installation(intents)
    target.write_bytes(content)
    file_watcher.mark_publication_installed(intents)
    index = EpistemicGraphIndex(vault)
    try:
        explained = index._unexplained_differences()
        assert explained is not None
        assert PAGE_B not in explained[3]

        file_watcher.finalize_publication_intents(intents)  # no success: aborted

        sample = index._unexplained_differences()
        assert sample is not None
        assert PAGE_B in sample[3]
    finally:
        file_watcher.clear_self_write_registry()


# --- (b) the graph retires the fence it mints ---------------------------------


def test_a_class_c_rebuild_retires_the_mark_it_minted(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant: any external-pending epoch the graph mints, the graph retires."""
    marks = _spy_marks(monkeypatch)
    monkeypatch.setattr(
        EpistemicGraphIndex, "_source_versions_current", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        EpistemicGraphIndex,
        "_classify_movement",
        lambda *_args, **_kwargs: ("unrecorded", frozenset({PAGE_B})),
    )

    with pytest.raises(epistemic_graph.GraphProjectionMoved, match="Class C"):
        EpistemicGraphIndex(vault).rebuild_all()

    assert marks == [(str((vault / PAGE_B).resolve()),)], marks
    assert freshness.external_pending(vault) is False, (
        "the Class C rebuild left its own external-pending mark for the watcher's "
        "five-minute recovery to retire"
    )
    assert freshness.external_pending_epoch(vault) is None


def test_retiring_the_mark_never_retires_a_newer_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconcile clears only through the epoch it sampled."""
    monkeypatch.setattr(
        EpistemicGraphIndex, "_source_versions_current", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        EpistemicGraphIndex,
        "_classify_movement",
        lambda *_args, **_kwargs: ("unrecorded", frozenset({PAGE_B})),
    )
    original = EpistemicGraphIndex._reconcile_recall_publication

    def reconcile_then_a_newer_event(index: EpistemicGraphIndex) -> None:
        original(index)
        freshness.mark_external_pending(vault, paths=[vault / PAGE_C])

    monkeypatch.setattr(
        EpistemicGraphIndex, "_reconcile_recall_publication", reconcile_then_a_newer_event
    )

    with pytest.raises(epistemic_graph.GraphProjectionMoved):
        EpistemicGraphIndex(vault).rebuild_all()

    assert freshness.external_pending_paths(vault) == frozenset(
        {str((vault / PAGE_C).resolve())}
    )


# --- (c) graph_drift names the refusal it hit ---------------------------------


def test_graph_drift_names_an_external_pending_refusal(vault: Path) -> None:
    assert epistemic_graph.graph_drift(vault) == []
    freshness.mark_external_pending(vault, paths=[vault / PAGE_B])

    drift = epistemic_graph.graph_drift(vault)

    assert len(drift) == 1, drift
    assert drift[0]["refusal"] == "external_pending"
    assert "external_pending" in drift[0]["reason"]
    assert "schema-mismatched" not in drift[0]["reason"]


def test_graph_drift_names_a_missing_sidecar(vault: Path) -> None:
    EpistemicGraphIndex(vault).path.unlink()

    drift = epistemic_graph.graph_drift(vault)

    assert len(drift) == 1, drift
    assert drift[0]["refusal"] == "sidecar_missing"
