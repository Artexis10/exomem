"""The admitted recall path set is selected once per published catalogue snapshot.

A request that recalls under the admitted catalogue used to select every
in-scope path from the sidecar on each call (1 to 6 s on a large vault under
load). The set is now cached per snapshot, so these tests pin what that cache
must never change: the set equals a fresh selection, a policy or row change
reads a new set, and concurrent requests at one snapshot select once.
"""

from __future__ import annotations

import contextlib
import threading
import time
from pathlib import Path

import pytest

from exomem import file_watcher, freshness, lexstore
from exomem import find as find_module

SELECT_SPAN = "lexical.resolver_entries"


def _warm(vault: Path) -> lexstore.LexicalStore:
    """Seed the registry and publish the catalogue through the real path."""
    freshness.clear()
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    store = lexstore.get_store(vault)
    assert store.rebuild_atomic() is True
    return store


def _count_selects(monkeypatch: pytest.MonkeyPatch, *, hold: float = 0.0) -> list[int]:
    """Record each execution of the admitted-path SELECT; optionally hold it open."""
    selects: list[int] = []
    real = lexstore.call_spans.span

    @contextlib.contextmanager
    def counting(name, fields=None):
        with real(name, fields) as measured:
            if name == SELECT_SPAN:
                selects.append(threading.get_ident())
                time.sleep(hold)
            yield measured

    monkeypatch.setattr(lexstore.call_spans, "span", counting)
    return selects


def test_cached_set_equals_a_fresh_selection_and_is_selected_once(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _warm(vault)
    selects = _count_selects(monkeypatch)

    for scope in ("kb", "vault"):
        checkpoint = store.published_recall_checkpoint(scope)
        selected = len(selects)
        first = store.recall_admitted_paths(scope, checkpoint)
        again = store.recall_admitted_paths(scope, checkpoint)
        assert len(selects) - selected == 1
        assert again is first

        fresh = {path for path, _title in store.recall_resolver_entries(scope, checkpoint)}
        assert first == fresh


def test_policy_change_never_serves_the_older_set(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _warm(vault)
    selects = _count_selects(monkeypatch)
    before = store.published_recall_checkpoint("vault")
    cached = store.recall_admitted_paths("vault", before)
    withheld = {path for path in cached if path.startswith("Knowledge Base/Notes/")}
    assert withheld, "the fixture vault must hold pages under Notes"

    (vault / "Knowledge Base" / "_access.yaml").write_text(
        "excluded:\n  - Notes\n", encoding="utf-8"
    )

    assert store.recall_admitted_paths("vault", before) is None, (
        "a checkpoint minted under the old policy must be refused, not answered from the cache"
    )
    _warm(vault)
    after = store.published_recall_checkpoint("vault")
    republished = store.recall_admitted_paths("vault", after)

    assert after != before
    assert republished is not None
    assert withheld.isdisjoint(republished)
    assert len(selects) == 2, "the new policy identity selects its own set"


def test_a_published_row_change_reads_a_new_set(vault: Path) -> None:
    store = _warm(vault)
    before = store.published_recall_checkpoint("vault")
    cached = store.recall_admitted_paths("vault", before)
    rel = min(path for path in cached if path.startswith("Knowledge Base/Notes/"))

    target = vault / rel
    target.unlink()
    freshness.on_files_changed(vault, deleted=[target])
    assert store.delete_rel_paths([rel])
    after = store.published_recall_checkpoint("vault")
    published = store.recall_admitted_paths("vault", after)

    assert after != before
    assert rel not in published
    assert published == {path for path, _title in store.recall_resolver_entries("vault", after)}


def test_concurrent_requests_at_a_new_snapshot_select_once(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _warm(vault)
    checkpoint = store.published_recall_checkpoint("vault")
    selects = _count_selects(monkeypatch, hold=0.3)
    workers = 6
    start = threading.Barrier(workers)
    answers: list[frozenset[str] | None] = []

    def request() -> None:
        start.wait()
        answers.append(store.recall_admitted_paths("vault", checkpoint))

    threads = [threading.Thread(target=request) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(answers) == workers
    assert all(answer == answers[0] and answer for answer in answers)
    assert len(selects) == 1


def test_second_recall_at_one_checkpoint_selects_no_paths(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workflow: two ask-style recalls under the admitted catalogue."""
    from exomem import readiness

    _warm(vault)
    freshness.clear()  # a cold registry makes recall answer from the published catalogue
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    monkeypatch.setattr(
        readiness, "retrieval_admission", lambda _root=None: {"state": "ready", "admitted": True}
    )
    selects = _count_selects(monkeypatch)

    first = find_module.find(vault, query="memory", limit=5, degraded_out=[])
    after_first = len(selects)
    find_module.reset_page_and_result_caches()
    second = find_module.find(vault, query="memory", limit=5, degraded_out=[])

    assert [hit.path for hit in second] == [hit.path for hit in first]
    assert after_first >= 1, "the first recall selects the admitted set"
    assert len(selects) == after_first, "the second recall reuses it"
