"""Idle reclamation keeps what is expensive to rebuild (#676).

A recall call on the production vault measured 34,052 ms, 30,787 ms of which was
building the recall wikilink resolver. #677 took most of that off the reader --
the cold build is single-flighted, and correctness evictions schedule a
background rebuild. One path out of that net remained: `unload_ram_caches`
deliberately bypasses the eviction seam so that no rebuild is scheduled, which is
right for a reaper handing memory back and wrong for what it was handing back.

Measured on a 2,400-page vault: the resolver retains 3.05 MiB, and rebuilding it
reads every admitted page -- 39 s of page reads alone, charged to whichever
reader asks first.

So the two senses of "evict" are now separate calls. Correctness eviction still
clears it, because there a stale resolver is a wrong answer rather than a slow
one.
"""

from __future__ import annotations

from pathlib import Path

from exomem import find as find_module


def _vault(root: Path) -> Path:
    (root / "Knowledge Base" / "Notes").mkdir(parents=True)
    (root / ".exomem" / "schema").mkdir(parents=True)
    (root / ".exomem" / "schema" / "SKILL.md").write_text("contract", encoding="utf-8")
    for index in range(3):
        (root / "Knowledge Base" / "Notes" / f"note-{index}.md").write_text(
            f"---\ntype: note\ntitle: Note {index}\n---\n\n# Note {index}\n\nBody.\n",
            encoding="utf-8",
        )
    return root


def _resident(root: Path) -> bool:
    with find_module._RESOLVER_LOCK:
        return root in find_module._RECALL_RESOLVER_CACHE


def test_idle_reclamation_keeps_the_recall_resolver(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_RESOLVER_WARM", "1")
    root = _vault(tmp_path)
    find_module.recall_resolver_snapshot(root)
    assert _resident(root)

    released = find_module.release_idle_ram_caches()

    assert _resident(root), "the 3 MiB the reaper wanted back costs a reader 39 s of page reads"
    assert released["pages"] >= 0
    assert len(find_module._CACHE.entries) == 0, "the page cache is the large one and still goes"


def test_correctness_eviction_still_clears_the_recall_resolver(
    tmp_path: Path, monkeypatch
) -> None:
    """`epistemic_graph` uses this to force a re-derivation, not to free memory."""
    monkeypatch.setenv("EXOMEM_DISABLE_RESOLVER_WARM", "1")
    root = _vault(tmp_path)
    find_module.recall_resolver_snapshot(root)
    assert _resident(root)

    find_module.unload_ram_caches()

    assert not _resident(root)


def test_a_reader_after_idle_reclamation_does_not_reread_the_vault(
    tmp_path: Path, monkeypatch
) -> None:
    """The whole point: the next reader is served from memory, not from disk.

    Counted at the page read rather than at the walk. The walk is 484 ms of the
    cold build and freshness performs one of its own before the resolver is even
    consulted; the 39 seconds is the read-and-parse of every admitted page, and
    that happens only when the resolver is actually rebuilt.
    """
    monkeypatch.setenv("EXOMEM_DISABLE_RESOLVER_WARM", "1")
    root = _vault(tmp_path)
    find_module.recall_resolver_snapshot(root)
    find_module.release_idle_ram_caches()

    reads = 0
    real_get = find_module._CACHE.get

    def counting_get(path, vault_root):
        nonlocal reads
        reads += 1
        return real_get(path, vault_root)

    monkeypatch.setattr(find_module._CACHE, "get", counting_get)
    resolver = find_module.recall_resolver_snapshot(root)

    assert resolver is not None
    assert reads == 0, "reclaiming 3 MiB must not cost the next reader a re-read of the vault"


def test_the_idle_reaper_and_quiet_mode_both_use_the_narrow_release() -> None:
    """The two memory-motivated callers, pinned so neither drifts back.

    `epistemic_graph`'s call sites deliberately stay on `unload_ram_caches`:
    they evict to force a re-derivation, where keeping a stale resolver would be
    a wrong answer rather than a slow one.
    """
    import inspect

    from exomem import epistemic_graph, mode, model_reaper

    assert "release_idle_ram_caches" in inspect.getsource(model_reaper)
    assert "release_idle_ram_caches" in inspect.getsource(mode)
    graph_source = inspect.getsource(epistemic_graph)
    assert "release_idle_ram_caches" not in graph_source
    assert "unload_ram_caches" in graph_source
