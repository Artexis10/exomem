"""Wiring/integration for event-maintained indexes (OpenSpec: event-maintained-indexes).

Covers the seams between the freshness registry and its producers/consumers
that the pure-unit files (test_freshness_registry, test_embedding_matrix_shared,
test_inbound_index_incremental) don't: the server watcher gate decoupling, the
watcher's publish-to-registries flush, the self-write publish path, and the
load-bearing guarantee that a live registry makes `find`'s freshness check
syscall-free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import embeddings, file_watcher, freshness
from exomem import find as find_module
from exomem.vault import walk_vault_md


@pytest.fixture(autouse=True)
def _reset_registries():
    freshness.clear()
    yield
    freshness.clear()


def _entries(paths):
    for p in paths:
        try:
            yield (str(p), p.stat().st_mtime_ns)
        except OSError:
            continue


def _seed(vault: Path) -> None:
    freshness.seed(vault, "kb", _entries(find_module._walk_md(vault / "Knowledge Base")))
    freshness.seed(vault, "vault", _entries(walk_vault_md(vault)))


def _kb_walk(vault: Path):
    return find_module._walk_freshness_key(find_module._walk_md(vault / "Knowledge Base"))


# ---- The load-bearing perf guarantee: a live registry never stat-walks -------


def test_live_registry_makes_freshness_syscall_free(vault, monkeypatch):
    """When the registry is live, FreshnessSnapshot must read it, NOT walk the
    tree — this is the entire ~494ms/find win. Proven by making the walk raise."""
    _seed(vault)

    def _boom(*_a, **_k):
        raise AssertionError("_walk_md was called while the registry was live")

    monkeypatch.setattr(find_module, "_walk_md", _boom)
    snap = find_module.FreshnessSnapshot(vault)
    # Both scopes resolve from the registry without touching the walk.
    assert snap.kb()[0] > 0
    assert snap.vault()[0] > 0


def test_not_live_falls_back_to_walk(vault):
    """No seed → registry not live → FreshnessSnapshot walks and equals the walk."""
    freshness.clear()
    snap = find_module.FreshnessSnapshot(vault)
    assert snap.kb() == _kb_walk(vault)


# ---- Watcher flush publishes to the registries -------------------------------


def test_watcher_flush_publishes_freshness_and_embeds_only_kb(vault, monkeypatch):
    """_flush maintains freshness for the whole vault but only re-embeds KB
    markdown (sibling-folder edits update freshness, never the KB sidecar)."""
    _seed(vault)
    embed_calls: list[list[Path]] = []
    monkeypatch.setattr(
        "exomem.embeddings.upsert_after_write_status",
        lambda root, paths: embed_calls.append(list(paths))
        or embeddings.EmbeddingSyncStatus(
            "completed", "embedding_upsert_completed", len(paths)
        ),
    )
    w = file_watcher.FileWatcher(vault)

    # A KB note and a sibling-folder note both change.
    kb_note = vault / "Knowledge Base" / "Notes" / "Insights" / "watched.md"
    kb_note.write_text("---\ntype: insight\n---\n# W\nbody", encoding="utf-8")
    sib_dir = vault / "Reference"
    sib_dir.mkdir(exist_ok=True)
    sib_note = sib_dir / "ref.md"
    sib_note.write_text("# Ref\nx", encoding="utf-8")

    w._record(kb_note, deleted=False)
    w._record(sib_note, deleted=False)
    w._flush()

    # Freshness reflects both, each in the right scope.
    assert freshness.triple(vault, "kb") == _kb_walk(vault)
    assert freshness.triple(vault, "vault") == find_module._walk_freshness_key(walk_vault_md(vault))
    # Only the KB note was handed to the embedder.
    embedded = [p for batch in embed_calls for p in batch]
    assert kb_note in embedded
    assert sib_note not in embedded


# ---- Self-write publish (no staleness regression) ----------------------------


def test_self_write_updates_freshness_immediately(vault):
    """register_self_write (called by every server writer) must update freshness
    right away — otherwise a note/edit would be invisible to search until the
    300s reconcile, a regression from the always-walked behavior."""
    _seed(vault)
    before = freshness.triple(vault, "kb")
    note = vault / "Knowledge Base" / "Notes" / "Insights" / "selfwrite.md"
    note.write_text("---\ntype: insight\n---\n# S\nbody", encoding="utf-8")
    file_watcher.register_self_write(vault, [note])
    after = freshness.triple(vault, "kb")
    assert after != before
    assert after == _kb_walk(vault)


def test_self_delete_updates_freshness_immediately(vault):
    _seed(vault)
    # Delete an existing fixture note.
    existing = next((vault / "Knowledge Base").rglob("*.md"))
    rel = existing.relative_to(vault).as_posix()
    existing.unlink()
    file_watcher.register_self_delete(vault, [rel])
    assert freshness.triple(vault, "vault") == find_module._walk_freshness_key(walk_vault_md(vault))


# ---- Server watcher gate decoupled from embeddings ---------------------------


def test_watcher_gate_decoupled_from_embeddings(vault, monkeypatch):
    """The watcher now starts whenever EXOMEM_DISABLE_FILE_WATCHER is unset,
    even with embeddings disabled (it maintains freshness/inbound); and it does
    NOT start when EXOMEM_DISABLE_FILE_WATCHER is set."""
    from exomem import server as server_module

    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")

    started: list[str] = []

    class _FakeWatcher:
        def __init__(self, root):
            self._root = root

        def start(self):
            started.append(str(self._root))
            return True

    monkeypatch.setattr("exomem.file_watcher.FileWatcher", _FakeWatcher)

    # Embeddings disabled but watcher NOT disabled → should start.
    monkeypatch.delenv("EXOMEM_DISABLE_FILE_WATCHER", raising=False)
    server_module.build_server(require_auth=False)
    assert started, "watcher must start with embeddings disabled once decoupled"

    # Watcher explicitly disabled → must not start.
    started.clear()
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    server_module.build_server(require_auth=False)
    assert not started, "EXOMEM_DISABLE_FILE_WATCHER must still suppress the watcher"


# ---- clear_cache clears the new registries -----------------------------------


def test_find_clear_cache_clears_freshness(vault):
    _seed(vault)
    assert freshness.triple(vault, "kb") is not None
    find_module.clear_cache()
    assert freshness.triple(vault, "kb") is None
