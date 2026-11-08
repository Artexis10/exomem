"""A watcher batch that goes stale during dispatch repairs its own paths.

The watcher's dispatch waits on the graph. A governed write that lands in that
wait changes a page the batch had captured, so the batch's final revalidation
fails. The answer was a whole-catalogue lexical rebuild with no recorded cause,
which leaves retrieval unavailable for the length of a full build. The cause is
one page's disagreement with disk, so the repair replays that page. A replay
that fails still escalates to the rebuild; `test_lexical_deferred_upsert.py`
covers that worker path.

A batch that published and then went stale owes only the identities that
changed, not every page it carried: the governed log and index change on every
write, so one concurrent write used to requeue the whole knowledge base.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from exomem import (
    deferred_index,
    embeddings,
    epistemic_graph,
    freshness,
    index_sync,
    lexstore,
    recall_policy,
)

PAGE = "Knowledge Base/Notes/item.md"
UNTOUCHED = "Knowledge Base/Notes/untouched.md"


@pytest.fixture
def stale_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A live catalogue, and a watcher batch whose page is rewritten mid-dispatch."""
    freshness.clear()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path))
    page = tmp_path / PAGE
    untouched = tmp_path / UNTOUCHED
    page.parent.mkdir(parents=True)
    page.write_text("# Item\n\nbeforechange\n", encoding="utf-8")
    untouched.write_text("# Untouched\n\nsteadytext\n", encoding="utf-8")
    entries = [(str(path), freshness.stat_signature(path)) for path in (page, untouched)]
    for scope in freshness.SCOPES:
        freshness.seed(tmp_path, scope, entries)
    store = lexstore.get_store(tmp_path)
    assert store.rebuild_atomic()

    def graph_join_with_a_governed_write(*_args, **_kwargs):
        page.write_text("# Item\n\nafterchange, rewritten during the join\n", encoding="utf-8")
        freshness.on_files_changed(tmp_path, changed=[page])
        return epistemic_graph.GraphDispatchResult("completed", "incremental_completed")

    monkeypatch.setattr(epistemic_graph, "upsert_after_write", graph_join_with_a_governed_write)
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda _root, paths: embeddings.EmbeddingSyncStatus(
            "completed", "embedding_upsert_completed", len(paths)
        ),
    )
    page.write_text("# Item\n\nbatchtext\n", encoding="utf-8")
    freshness.on_files_changed(tmp_path, changed=[page])
    yield store, page
    freshness.clear()
    lexstore.clear_stores()


def _run_stale_batch(root: Path, page: Path):
    report = index_sync.upsert_after_write(
        root, [page], publish_corpus_change=False, watcher_deleted_rel_paths=[]
    )
    assert report.reconcile_required, "the rewrite during dispatch did not make the batch stale"
    _wait_for_lexical_repair(root)


def _wait_for_lexical_repair(root: Path) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with lexstore._REPAIRS_LOCK:
            if root.resolve() not in lexstore._REPAIRS_IN_FLIGHT:
                return
        time.sleep(0.01)
    raise AssertionError("the lexical repair never finished")


def _paths_matching(store, term: str) -> list[str]:
    connection = store._connect()
    try:
        return [
            row[0]
            for row in connection.execute(
                "SELECT pages.path FROM tri JOIN pages ON pages.rowid = tri.rowid "
                "WHERE tri MATCH ?",
                (f'"{term}"',),
            )
        ]
    finally:
        connection.close()


def test_a_stale_watcher_batch_replays_its_paths_and_names_its_cause(
    tmp_path: Path, stale_batch, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, page = stale_batch
    rebuilds: list[bool] = []
    monkeypatch.setattr(store, "rebuild_atomic", lambda: rebuilds.append(True) or True)
    caplog.set_level(logging.INFO, logger="exomem.lexstore")

    _run_stale_batch(tmp_path, page)

    assert rebuilds == [], "a stale batch rebuilt the whole catalogue"
    assert _paths_matching(store, "afterchange") == [PAGE]
    assert _paths_matching(store, "steadytext") == [UNTOUCHED]
    started = [
        record.fields
        for record in caplog.records
        if getattr(record, "event", None) == "lexical_repair_started"
    ]
    assert any(
        fields["kind"] == "targeted" and "watcher_batch_stale" in fields["causes"].split(",")
        for fields in started
    ), started



LOG = "Knowledge Base/log.md"
INDEX = "Knowledge Base/index.md"
NOTES = [f"Knowledge Base/Notes/{name}.md" for name in ("alpha", "beta", "gamma")]


@pytest.fixture
def whole_kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A catalogue and a watcher batch over the whole knowledge base.

    Three notes plus the governed log and index, all changed since the catalogue
    was built, as after a bulk change. The graph step stands in for the long
    dispatch: each test says which concurrent write lands in it.
    """
    freshness.clear()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path))
    files = [tmp_path / rel for rel in (*NOTES, LOG, INDEX)]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {path.stem}\n\nbefore\n", encoding="utf-8")
    entries = [(str(path), freshness.stat_signature(path)) for path in files]
    for scope in freshness.SCOPES:
        freshness.seed(tmp_path, scope, entries)
    assert lexstore.get_store(tmp_path).rebuild_atomic()
    for path in files:
        path.write_text(f"# {path.stem}\n\nbatch text\n", encoding="utf-8")
    freshness.on_files_changed(tmp_path, changed=files)
    yield files
    freshness.clear()
    lexstore.clear_stores()


def _during_dispatch(monkeypatch: pytest.MonkeyPatch, concurrent_write) -> None:
    def graph_join(*_args, **_kwargs):
        concurrent_write()
        return epistemic_graph.GraphDispatchResult("completed", "incremental_completed")

    monkeypatch.setattr(epistemic_graph, "upsert_after_write", graph_join)


def _dispatch_whole_kb(
    monkeypatch: pytest.MonkeyPatch, root: Path, files: list[Path]
) -> set[str]:
    """Run the watcher batch as quiet mode does; return the pages its lexical replay named."""
    replayed: set[str] = set()
    request_repair = lexstore.request_repair

    def spy(vault_root, *, deferred_paths=None, **kwargs):
        replayed.update(path.relative_to(root).as_posix() for path in deferred_paths or [])
        return request_repair(vault_root, deferred_paths=deferred_paths, **kwargs)

    monkeypatch.setattr(lexstore, "request_repair", spy)
    report = index_sync.upsert_after_write(
        root, files, defer_semantic=True, publish_corpus_change=False, watcher_deleted_rel_paths=[]
    )
    assert report.reconcile_required, "the concurrent write did not make the batch stale"
    _wait_for_lexical_repair(root)
    return replayed


def test_a_governed_write_mid_batch_requeues_only_what_it_changed(
    tmp_path: Path, whole_kb, monkeypatch: pytest.MonkeyPatch
) -> None:
    beta = tmp_path / NOTES[1]
    log, index = tmp_path / LOG, tmp_path / INDEX

    def governed_write() -> None:
        # What every governed write does: a page the batch never saw, a page it
        # did, and the log and index entries about both.
        (tmp_path / "Knowledge Base/Notes/delta.md").write_text("# delta\n", encoding="utf-8")
        beta.write_text("# beta\n\nedited during the batch\n", encoding="utf-8")
        log.write_text("# log\n\nbatch text\nwrote beta and delta\n", encoding="utf-8")
        index.write_text("# index\n\nbatch text\n[[delta]]\n", encoding="utf-8")
        freshness.on_files_changed(tmp_path, changed=[beta, log, index])

    _during_dispatch(monkeypatch, governed_write)

    replayed = _dispatch_whole_kb(monkeypatch, tmp_path, whole_kb)

    changed = {NOTES[1], LOG, INDEX}
    assert set(deferred_index.list_full_paths(tmp_path)) == changed
    assert replayed == changed


def test_a_policy_change_mid_batch_requeues_the_whole_batch(
    tmp_path: Path, whole_kb, monkeypatch: pytest.MonkeyPatch
) -> None:
    def policy_change() -> None:
        # No batch file changes, but admission of any of them may have flipped.
        (tmp_path / "Knowledge Base/_access.yaml").write_text(
            "excluded:\n  - Archive\n", encoding="utf-8"
        )

    _during_dispatch(monkeypatch, policy_change)

    replayed = _dispatch_whole_kb(monkeypatch, tmp_path, whole_kb)

    everything = {*NOTES, LOG, INDEX}
    assert set(deferred_index.list_full_paths(tmp_path)) == everything
    assert replayed == everything


def test_a_batch_that_went_stale_before_publishing_still_owes_every_page(
    tmp_path: Path, whole_kb, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing was published yet, so the unchanged pages owe their work as much
    # as the changed one. Narrowing this case would drop their derived-index work.
    log = tmp_path / LOG
    partition = recall_policy.partition_markdown_paths

    def partition_then_write(root, paths):
        batch = partition(root, paths)
        log.write_text("# log\n\nbatch text\nwritten during the read\n", encoding="utf-8")
        return batch

    monkeypatch.setattr(recall_policy, "partition_markdown_paths", partition_then_write)
    _during_dispatch(monkeypatch, lambda: None)

    _dispatch_whole_kb(monkeypatch, tmp_path, whole_kb)

    assert set(deferred_index.list_full_paths(tmp_path)) == {*NOTES, LOG, INDEX}
