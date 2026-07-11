"""Typed-graph sidecar token in the find hot-cache freshness key.

The graph lane's content generation must join `_freshness_key` so a cached
ranking is never served against a stale graph — and must NOT ride the sidecar
file mtime (a WAL checkpoint moves mtime without a content change).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem import epistemic_graph
from exomem import find as find_module


@pytest.fixture(autouse=True)
def _clear_find_caches():
    """Flush the process-global find caches these tests populate on teardown."""
    yield
    find_module.clear_cache()


def _count_semantic(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls = {"n": 0}
    orig = find_module._find_semantic

    def counting(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(find_module, "_find_semantic", counting)
    return calls


def test_relation_adding_write_invalidates(vault: Path, monkeypatch) -> None:
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    calls = _count_semantic(monkeypatch)
    find_module.find(vault, query="metabolism")
    find_module.find(vault, query="metabolism")
    assert calls["n"] == 1  # served from cache

    note = vault / "Knowledge Base" / "Notes" / "graph-freshness-probe.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "# Probe\n\nmetabolism probe.\n\n## Relations\n\n"
        "- supports [[Knowledge Base/Notes/Insights/current-view]]\n",
        encoding="utf-8",
    )
    epistemic_graph.upsert_after_write(vault, [note])

    find_module.find(vault, query="metabolism")
    assert calls["n"] == 2  # graph content changed -> re-ranked


def test_wal_mtime_change_does_not_evict(vault: Path, monkeypatch) -> None:
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    calls = _count_semantic(monkeypatch)
    find_module.find(vault, query="metabolism")
    find_module.find(vault, query="metabolism")
    assert calls["n"] == 1

    token_before = epistemic_graph.cache_token(vault)
    sidecar = epistemic_graph.sidecar_path(vault)
    bumped = sidecar.stat().st_mtime_ns + 1_000_000_000
    os.utime(sidecar, ns=(bumped, bumped))
    token_after = epistemic_graph.cache_token(vault)
    assert token_after == token_before  # mtime is not the token

    find_module.find(vault, query="metabolism")
    assert calls["n"] == 1  # still a cache hit despite the moved mtime


def test_availability_flip_separates_cache_entries(vault: Path, monkeypatch) -> None:
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    calls = _count_semantic(monkeypatch)
    find_module.find(vault, query="metabolism")
    assert calls["n"] == 1

    # Sidecar becomes unavailable between two identical calls: the typed-mode
    # cache entry must not be reused for the fallback-mode answer.
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    find_module.find(vault, query="metabolism")
    assert calls["n"] == 2
