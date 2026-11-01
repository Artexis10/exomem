"""A watcher batch that goes stale during dispatch repairs its own paths.

The watcher's dispatch waits on the graph. A governed write that lands in that
wait changes a page the batch had captured, so the batch's final revalidation
fails. The answer was a whole-catalogue lexical rebuild with no recorded cause,
which leaves retrieval unavailable for the length of a full build. The cause is
one page's disagreement with disk, so the repair replays that page. A replay
that fails still escalates to the rebuild; `test_lexical_deferred_upsert.py`
covers that worker path.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from exomem import embeddings, epistemic_graph, freshness, index_sync, lexstore

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

