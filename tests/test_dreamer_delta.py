"""D1-T4: which pages the dreamer processes next, without ever walking the vault.

Changed pages come from the freshness registry: its consumer delta when that is
complete, and otherwise an exact diff of the live signature map against the
dreamer's persisted `seen` map. A restart therefore costs a dict diff, not a
rescan, and a write in the middle of a pass just requeues that one page.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem import dreamer_delta, dreamer_store, freshness
from exomem import vault as vault_module


@pytest.fixture(autouse=True)
def _fresh_registry():
    freshness.clear()
    dreamer_store.clear_reader_memo()
    yield
    freshness.clear()
    dreamer_store.clear_reader_memo()


def _write(vault: Path, rel: str, body: str = "body") -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: insight\nstatus: active\n---\n# Page\n\n{body}\n", encoding="utf-8"
    )
    return path


def _seed(vault: Path) -> None:
    freshness.seed(
        vault,
        "vault",
        [(str(p), freshness.stat_signature(p)) for p in vault_module.walk_vault_md(vault)],
    )


def _vault(tmp_path: Path, count: int = 5) -> Path:
    vault = tmp_path / "vault"
    for index in range(count):
        _write(vault, f"Knowledge Base/Notes/Insights/page-{index:02d}.md")
    _seed(vault)
    return vault


def _drain(store, conn, vault: Path, limit: int = 100) -> list[str]:
    """Process every queued page the way the worker does: one commit per page."""
    processed: list[str] = []
    while True:
        with store.write(conn):
            work = dreamer_delta.next_paths(store, conn, vault, limit=limit)
        if not work.paths:
            return processed
        for rel in work.paths:
            with store.write(conn):
                dreamer_delta.mark_processed(store, conn, vault, rel)
            processed.append(rel)


def test_complete_delta_uses_delta_since(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _vault(tmp_path)
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    assert len(_drain(store, conn, vault)) == 5
    assert dreamer_delta.checkpoint(store, conn) is not None

    changed = _write(vault, "Knowledge Base/Notes/Insights/page-01.md", "edited body")
    freshness.on_files_changed(vault, changed=[changed])
    calls: list[str] = []
    real_delta = freshness.delta_since
    monkeypatch.setattr(
        freshness,
        "delta_since",
        lambda *a, **k: (calls.append("delta"), real_delta(*a, **k))[1],
    )
    monkeypatch.setattr(
        freshness,
        "live_entries",
        lambda *_a, **_k: pytest.fail("a complete delta must not diff the live map"),
    )
    with store.write(conn):
        work = dreamer_delta.next_paths(store, conn, vault, limit=10)
    assert calls == ["delta"]
    assert work.paths == ["Knowledge Base/Notes/Insights/page-01.md"]
    assert work.reseeding is False
    conn.close()


def test_restart_diff_is_exact_without_a_filesystem_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    _drain(store, conn, vault)

    # The service goes down; while it is down one page is edited, one deleted
    # and one created. The restarted watcher seeds a new registry instance.
    freshness.clear()
    _write(vault, "Knowledge Base/Notes/Insights/page-02.md", "edited while down")
    (vault / "Knowledge Base/Notes/Insights/page-03.md").unlink()
    _write(vault, "Knowledge Base/Notes/Insights/page-09.md")
    _seed(vault)

    def no_walk(*_args, **_kwargs):
        raise AssertionError("the dreamer must never walk the filesystem")

    monkeypatch.setattr(os, "scandir", no_walk)
    monkeypatch.setattr(os, "walk", no_walk)
    monkeypatch.setattr(os, "listdir", no_walk)
    monkeypatch.setattr(Path, "iterdir", no_walk)
    monkeypatch.setattr(Path, "glob", no_walk)
    monkeypatch.setattr(Path, "rglob", no_walk)
    with store.write(conn):
        work = dreamer_delta.next_paths(store, conn, vault, limit=10)
    assert work.paths == [
        "Knowledge Base/Notes/Insights/page-02.md",
        "Knowledge Base/Notes/Insights/page-03.md",
        "Knowledge Base/Notes/Insights/page-09.md",
    ]
    assert work.reseeding is False
    conn.close()


def test_first_enable_reseeds_in_sorted_resumable_order(tmp_path: Path) -> None:
    vault = _vault(tmp_path, count=7)
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    with store.write(conn):
        first = dreamer_delta.next_paths(store, conn, vault, limit=3)
    assert first.reseeding is True
    assert first.paths == [f"Knowledge Base/Notes/Insights/page-{i:02d}.md" for i in range(3)]
    assert first.remaining == 7
    for rel in first.paths[:2]:
        with store.write(conn):
            dreamer_delta.mark_processed(store, conn, vault, rel)
    conn.close()

    # A restart mid-reseed resumes exactly at the remainder.
    conn = dreamer_store.DreamerStore(vault).connect()
    with store.write(conn):
        resumed = dreamer_delta.next_paths(store, conn, vault, limit=3)
    assert resumed.paths == [f"Knowledge Base/Notes/Insights/page-{i:02d}.md" for i in range(2, 5)]
    assert resumed.remaining == 5
    conn.close()


def test_checkpoint_advances_only_when_pending_is_empty(tmp_path: Path) -> None:
    vault = _vault(tmp_path, count=4)
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    with store.write(conn):
        work = dreamer_delta.next_paths(store, conn, vault, limit=2)
    for rel in work.paths:
        with store.write(conn):
            dreamer_delta.mark_processed(store, conn, vault, rel)
    with store.write(conn):
        dreamer_delta.advance_if_drained(store, conn)
    assert dreamer_delta.checkpoint(store, conn) is None
    with store.write(conn):
        rest = dreamer_delta.next_paths(store, conn, vault, limit=10)
    assert len(rest.paths) == 2
    for rel in rest.paths:
        with store.write(conn):
            dreamer_delta.mark_processed(store, conn, vault, rel)
    with store.write(conn):
        dreamer_delta.advance_if_drained(store, conn)
    advanced = dreamer_delta.checkpoint(store, conn)
    assert advanced is not None
    assert advanced.instance_id == freshness.consumer_checkpoint(vault, "vault").instance_id
    conn.close()


def test_a_mid_tick_write_requeues_one_page_only(tmp_path: Path) -> None:
    vault = _vault(tmp_path, count=6)
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    with store.write(conn):
        work = dreamer_delta.next_paths(store, conn, vault, limit=10)
    # A write lands after page-01 was processed and before the tick ends.
    for rel in work.paths:
        with store.write(conn):
            dreamer_delta.mark_processed(store, conn, vault, rel)
        if rel.endswith("page-01.md"):
            edited = _write(vault, rel, "written mid-tick")
            freshness.on_files_changed(vault, changed=[edited])
    assert _drain(store, conn, vault) == ["Knowledge Base/Notes/Insights/page-01.md"]
    assert _drain(store, conn, vault) == []
    conn.close()


def test_watcher_off_waits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "vault"
    _write(vault, "Knowledge Base/Notes/Insights/page-00.md")
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    monkeypatch.setattr(Path, "iterdir", lambda *_a: pytest.fail("no fallback walk"))
    with store.write(conn):
        work = dreamer_delta.next_paths(store, conn, vault, limit=10)
    assert work.waiting == "freshness_unavailable"
    assert work.paths == []
    conn.close()


def test_governance_trash_and_non_markdown_are_skipped(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    kept = "Knowledge Base/Notes/Insights/kept.md"
    for rel in (
        kept,
        "Knowledge Base/_Governance/rules/rule.md",
        "Knowledge Base/_trash/old.md",
        "Knowledge Base/.trash/older.md",
        "Knowledge Base/_Schema/SKILL.md",
        "Knowledge Base/index.md",
        "Knowledge Base/Notes/Insights/log.md",
        "Outside/notes.md",
    ):
        _write(vault, rel)
    (vault / "Knowledge Base/Notes/Insights/image.png").write_bytes(b"png")
    _seed(vault)
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    with store.write(conn):
        work = dreamer_delta.next_paths(store, conn, vault, limit=50)
    assert work.paths == [kept]
    conn.close()
