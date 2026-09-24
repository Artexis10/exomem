"""A whole-vault graph pass cools the event registry only on evidence it is behind.

A whole-vault pass proves a vault-global identity before and after itself and
throws the pass away if anything moved. Any governed write moves it, so under
writes the pass re-targeted until its budget ran out and raised Class C
(`GraphProjectionMoved`), which marked the event registry externally pending,
unscoped. That mark cooled recall for every reader, fenced the graph for every
public read and left every later lineage gap uncoverable, so the next writes
registered more whole-vault passes. Probe L on main: three writers, one write a
second each, 3,000 pages -- Class C after 8 attempts in 110.8 s, unscoped.

The contract these tests pin (OpenSpec `live-index-freshness`, "A graph proof
cools the event registry only on evidence it is behind the disk"):

* movement the registry recorded -- a governed write this service committed --
  is not evidence. A pass that cannot stabilize under it is a publication
  failure (Class B) and never marks the registry;
* Class C needs positive evidence: a registry-vs-disk difference the registry's
  own history does not explain, and the mark names exactly those paths;
* a comparison that cannot complete proves nothing and is Class B.

Every write here goes through `writer_lease.LeaseManager` from a hook on
`_rebuild_all_pass`, so the registry learns of it through the production
post-commit fan-out rather than a hand-seeded freshness map.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from exomem import epistemic_graph, freshness, graph_sync
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

PAGE_A = "Knowledge Base/Notes/Insights/class-c-a.md"
PAGE_B = "Knowledge Base/Notes/Insights/class-c-b.md"
PAGE_C = "Knowledge Base/Notes/Insights/class-c-c.md"

#: Enough writes to exhaust the re-target budget (#576: eight attempts), so a
#: test that expects no Class C cannot pass by luck.
EVERY_PASS = 12


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


def _governed_write(root: Path, rel: str, content: str) -> None:
    """One committed write through the lease manager, as a request makes it.

    Run on its own thread so the mutation request's context variables never
    leak into the rebuild that the hook interrupts. Its own graph dispatch is
    switched off: these tests ask what the pass concludes about movement the
    registry recorded, and the registry still records the write.
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

    failure: list[BaseException] = []

    def run() -> None:
        saved = epistemic_graph.graph_scheduling_enabled
        epistemic_graph.graph_scheduling_enabled = lambda: False
        try:
            command = SimpleNamespace(name="remember", read_only=False, leaf=leaf)
            get_manager().invoke(command, (root,), {})
        except BaseException as error:  # noqa: BLE001 - re-raised on the caller's thread
            failure.append(error)
        finally:
            epistemic_graph.graph_scheduling_enabled = saved

    thread = threading.Thread(target=run, name="class-c-test-writer")
    thread.start()
    thread.join(timeout=60.0)
    assert not thread.is_alive(), "the governed write did not finish"
    if failure:
        raise failure[0]


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "vault"
    (root / "Knowledge Base/Notes/Insights").mkdir(parents=True)
    (root / PAGE_A).write_text(_page("A", "A claims against [[class-c-b]]."), encoding="utf-8")
    (root / PAGE_B).write_text(_page("B", "B is a plain claim."), encoding="utf-8")
    (root / PAGE_C).write_text(_page("C", "C cites [[class-c-a]]."), encoding="utf-8")
    _seed_live_freshness(root)
    EpistemicGraphIndex(root).rebuild_all()
    epistemic_graph.clear_publication_memos()
    yield root
    graph_sync.drain_active_rebuilds(timeout=60.0)
    epistemic_graph.clear_publication_memos()


class _MidPassAction:
    """Run `action` after each whole-vault pass on the rebuilding thread.

    `_rebuild_all_pass` is the pass itself, so an action here lands after the
    pass read the vault and before it proves anything -- inside the window a
    concurrent writer actually hits. Nested passes (a write's own dispatch)
    never trigger it.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        action: Callable[[int], None],
        *,
        times: int = 1,
    ) -> None:
        self.passes = 0
        self.acted = 0
        self._busy = False
        owner = threading.get_ident()
        original = EpistemicGraphIndex._rebuild_all_pass

        def hooked(index: EpistemicGraphIndex, *args: Any, **kwargs: Any) -> Any:
            report = original(index, *args, **kwargs)
            self.passes += 1
            if threading.get_ident() == owner and not self._busy and self.acted < times:
                self._busy = True
                try:
                    action(self.acted)
                    self.acted += 1
                finally:
                    self._busy = False
            return report

        monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_pass", hooked)


def _write_b(root: Path) -> Callable[[int], None]:
    def action(number: int) -> None:
        _governed_write(root, PAGE_B, _page("B", f"B is a plain claim, revision {number}."))

    return action


def _edit_b_out_of_band(root: Path) -> Callable[[int], None]:
    """A direct byte edit no watcher and no governed writer ever records."""

    def action(number: int) -> None:
        (root / PAGE_B).write_text(
            _page("B", f"B edited outside every recorder, revision {number}."), encoding="utf-8"
        )

    return action


def test_governed_writes_during_every_pass_are_class_b_not_c(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live loop: every Class C window held committed governed writes."""
    _MidPassAction(monkeypatch, _write_b(vault), times=EVERY_PASS)

    with pytest.raises(Exception) as raised:  # noqa: PT011 - the class is the assertion
        EpistemicGraphIndex(vault).rebuild_all()

    assert not isinstance(raised.value, epistemic_graph.GraphProjectionMoved), (
        f"recorded movement was classified Class C: {raised.value}"
    )
    assert epistemic_graph.is_publication_failure(raised.value) is True, repr(raised.value)
    assert freshness.external_pending(vault) is False
    assert freshness.external_pending_epoch(vault) is None


def test_recorded_movement_keeps_recall_live_and_the_corpus_cache_warm(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared contract's assertions 1 and 2 (#508 section 4) survive the pass.

    No epoch is allocated at any point, and find and the corpus cache stay
    warm. An unscoped mark is what used to cool both for every reader.
    """
    from exomem import semantic_contract

    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.reset_corpus_context_cache()
    semantic_contract.build_corpus_context(vault)
    cache_key = semantic_contract._corpus_cache_key(vault)
    observed: list[int | None] = []
    write = _write_b(vault)

    def write_then_observe(number: int) -> None:
        write(number)
        observed.append(freshness.external_pending_epoch(vault))

    _MidPassAction(monkeypatch, write_then_observe, times=EVERY_PASS)

    with pytest.raises(Exception) as raised:  # noqa: PT011 - the class is the assertion
        EpistemicGraphIndex(vault).rebuild_all()

    assert not isinstance(raised.value, epistemic_graph.GraphProjectionMoved)
    observed.append(freshness.external_pending_epoch(vault))
    assert observed and all(epoch is None for epoch in observed), observed
    assert freshness.is_live(vault, "vault") is True
    assert freshness.triple(vault, "vault") is not None
    assert freshness.external_pending(vault) is False
    assert semantic_contract._CORPUS_CONTEXT_EVENT_TOKENS.get(cache_key) == freshness.triple(
        vault, "vault"
    )
    assert semantic_contract.cached_corpus_census(vault) is not None


def test_an_unrecorded_edit_is_class_c_scoped_to_its_path(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive evidence marks exactly what it can name, not the whole vault."""
    _MidPassAction(monkeypatch, _edit_b_out_of_band(vault), times=EVERY_PASS)

    with pytest.raises(epistemic_graph.GraphProjectionMoved, match="Class C"):
        EpistemicGraphIndex(vault).rebuild_all()

    assert freshness.external_pending(vault) is True
    assert freshness.external_pending_unscoped(vault) is False, (
        "an unexplained difference the proof could name was marked unscoped, "
        "which makes every later lineage gap uncoverable"
    )
    assert freshness.external_pending_paths(vault) == frozenset(
        {str((vault / PAGE_B).resolve())}
    )


def test_an_incomplete_registry_history_is_class_b_not_c(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry that cannot answer proves nothing, so it may not mark."""
    edit = _edit_b_out_of_band(vault)

    def edit_and_lose_the_registry(number: int) -> None:
        edit(number)
        # The registry is no longer live, so the pass-end comparison has no
        # history to explain the movement against.
        freshness.invalidate(vault)

    _MidPassAction(monkeypatch, edit_and_lose_the_registry, times=EVERY_PASS)

    with pytest.raises(Exception) as raised:  # noqa: PT011 - the class is the assertion
        EpistemicGraphIndex(vault).rebuild_all()

    assert not isinstance(raised.value, epistemic_graph.GraphProjectionMoved)
    assert epistemic_graph.is_publication_failure(raised.value) is True
    assert freshness.external_pending(vault) is False
    assert freshness.external_pending_epoch(vault) is None


def test_a_page_with_a_decomposed_name_does_not_block_a_whole_vault_rebuild(
    tmp_path: Path,
) -> None:
    """A page synced from macOS keeps its NFD name on a byte-exact file system.

    The freshness identity and the graph's membership name it by the spelling
    the walk found. The recall resolver read it through the governed reader,
    which opened the NFKC spelling of that path -- a different, absent name on
    ext4 or NTFS -- so the resolver never held the page, and every whole-vault
    pass raised Class C "the supplied freshness identity did not name the
    resolver bytes" until its budget ran out. One such page kept the graph
    from ever publishing.
    """
    import unicodedata

    from exomem import find_corpus

    root = tmp_path / "vault"
    (root / "Knowledge Base/Notes/Insights").mkdir(parents=True)
    decomposed = unicodedata.normalize("NFD", "Knowledge Base/Notes/Insights/cafe-é.md")
    (root / PAGE_A).write_text(_page("A", "A cites [[class-c-b]]."), encoding="utf-8")
    (root / PAGE_B).write_text(_page("B", "B is a plain claim."), encoding="utf-8")
    (root / decomposed).write_text(_page("Cafe", "Cafe cites [[class-c-a]]."), encoding="utf-8")
    _seed_live_freshness(root)

    assert find_corpus._read_page_bytes(root / decomposed, root) is not None, (
        "the resolver's reader could not open the page the walk found"
    )
    EpistemicGraphIndex(root).rebuild_all()

    assert EpistemicGraphIndex(root).available()
    assert epistemic_graph.graph_drift(root) == []
    assert not freshness.external_pending(root)
