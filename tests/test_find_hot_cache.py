"""Hot find cache: repeat-request reuse, parameter separation, freshness
invalidation, caller-mutation safety, and the clear_cache() test hook."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from exomem import commands, embeddings
from exomem import find as find_module


def _count_semantic(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls = {"n": 0}
    orig = find_module._find_semantic

    def counting(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(find_module, "_find_semantic", counting)
    return calls


def test_repeat_request_served_from_cache(vault: Path, monkeypatch) -> None:
    calls = _count_semantic(monkeypatch)
    first = find_module.find(vault, query="metabolism")
    second = find_module.find(vault, query="metabolism")
    assert calls["n"] == 1
    assert [h.as_dict() for h in first] == [h.as_dict() for h in second]


def test_cache_hit_visible_in_timings(vault: Path) -> None:
    commands.op_find(vault, query="metabolism")
    out = commands.op_find(vault, query="metabolism", include_timings=True)
    assert out["timings"]["cache"]["hit"] is True


def test_different_params_do_not_collide(vault: Path, monkeypatch) -> None:
    calls = _count_semantic(monkeypatch)
    find_module.find(vault, query="metabolism", limit=5)
    find_module.find(vault, query="metabolism", limit=6)
    find_module.find(vault, query="metabolism", limit=5, prefer_compiled=False)
    assert calls["n"] == 3


def test_detail_is_serialization_not_a_cache_key(vault: Path, monkeypatch) -> None:
    calls = _count_semantic(monkeypatch)
    commands.op_find(vault, query="metabolism", detail="full")
    commands.op_find(vault, query="metabolism", detail="compact")
    assert calls["n"] == 1


def test_caller_mutation_cannot_poison_cache(vault: Path) -> None:
    hits = find_module.find(vault, query="metabolism")
    assert hits
    original_title = hits[0].title
    hits[0].title = "MUTATED"
    hits[0].superseded_by.append("junk")
    again = find_module.find(vault, query="metabolism")
    assert again[0].title == original_title
    assert "junk" not in again[0].superseded_by


def test_markdown_write_invalidates(vault: Path, monkeypatch) -> None:
    calls = _count_semantic(monkeypatch)
    find_module.find(vault, query="metabolism")
    new = vault / "Knowledge Base" / "Notes" / "hot-cache-freshness-probe.md"
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text("# Probe\n\nmetabolism probe body\n", encoding="utf-8")
    hits = find_module.find(vault, query="metabolism")
    assert calls["n"] == 2
    assert any(h.path.endswith("hot-cache-freshness-probe.md") for h in hits)


def test_preserved_mtime_replacement_invalidates_warmed_find(vault: Path) -> None:
    """External sync tools may replace bytes while preserving the source mtime."""

    note = vault / "Knowledge Base" / "Notes" / "freshness-replacement.md"
    note.write_text("# Freshness\n\nalphauniquesentinel\n", encoding="utf-8")
    before = note.stat()
    assert any(
        hit.path.endswith(note.name)
        for hit in find_module.find(vault, query="alphauniquesentinel", mode="keyword")
    )

    replacement = note.with_suffix(".replacement")
    replacement.write_text(
        "# Freshness\n\nbetauniquereplacementsentinel with different bytes\n",
        encoding="utf-8",
    )
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(replacement, note)
    assert note.stat().st_mtime_ns == before.st_mtime_ns

    new_hits = find_module.find(vault, query="betauniquereplacementsentinel", mode="keyword")
    old_hits = find_module.find(vault, query="alphauniquesentinel", mode="keyword")
    assert any(hit.path.endswith(note.name) for hit in new_hits)
    assert not any(hit.path.endswith(note.name) for hit in old_hits)


@pytest.mark.parametrize("sidecar_name", [".embeddings.sqlite", ".clip.sqlite"])
def test_sidecar_generation_invalidates(vault: Path, monkeypatch, sidecar_name: str) -> None:
    """A gen-bumping write to a semantic sidecar invalidates the hot cache — keyed
    on the in-band (epoch, generation) token, NOT the sidecar file mtime (a WAL
    checkpoint moves the mtime with no content change; an uncheckpointed commit
    leaves it unmoved). Replaces the old mtime/utime-driven invalidation."""
    calls = _count_semantic(monkeypatch)
    find_module.find(vault, query="metabolism")
    find_module.find(vault, query="metabolism")
    assert calls["n"] == 1  # second serve came from the hot cache

    if sidecar_name == ".embeddings.sqlite":
        vec = np.zeros((1, embeddings.VECTOR_DIM), dtype=np.float32)
        vec[0, 0] = 1.0
        embeddings.get_embedding_index(vault).upsert_file(
            "Knowledge Base/Notes/gen-probe.md", ["x"], vec, 1.0
        )
    else:
        vec = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
        vec[0] = 1.0
        embeddings.get_clip_index(vault).upsert(
            "Knowledge Base/Attachments/gen-probe.png", vec, 1.0
        )

    find_module.find(vault, query="metabolism")
    assert calls["n"] == 2  # the generation bump invalidated the cache


def test_cache_disabled_by_env(vault: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    calls = _count_semantic(monkeypatch)
    find_module.find(vault, query="metabolism")
    find_module.find(vault, query="metabolism")
    assert calls["n"] == 2


def test_clear_cache_clears_hot_cache(vault: Path, monkeypatch) -> None:
    calls = _count_semantic(monkeypatch)
    find_module.find(vault, query="metabolism")
    find_module.clear_cache()
    find_module.find(vault, query="metabolism")
    assert calls["n"] == 2


def test_unload_ram_caches_preserves_freshness(vault: Path) -> None:
    """Freshness survives an unload -- and so, by default, does the page cache.

    The parsed-page cache used to go with every meaning of "unload". Exact
    receipt custody (`accelerate-governed-recall`, design Decision 2) says a
    whole-scope event may not discard a substrate cache whose paths are covered
    by receipts, and a correctness eviction's subject is the resolver: a page
    row is keyed to its file's content signature and evicted by its own
    receipt, so it cannot be stale in the sense the eviction exists to fix.

    So the default preserves it and `pages=True` -- what a caller releasing
    memory asks for -- still clears it. Both halves are pinned here, because
    losing either one is silent: the first costs a re-parse of the vault after
    every graph rebuild, the second leaks the largest cache the reaper reclaims.
    """
    from exomem import freshness

    kb = vault / "Knowledge Base"
    freshness.seed(
        vault,
        "kb",
        ((str(p), freshness.stat_signature(p)) for p in find_module._walk_md(kb)),
    )
    before_freshness = freshness.triple(vault, "kb")

    assert find_module.find(vault, query="metabolism")
    status = find_module.cache_status()
    resident_pages = status["pages"]["entries"]
    assert resident_pages > 0
    assert status["hot_find"]["entries"] > 0
    assert find_module._CACHE._signatures

    unloaded = find_module.unload_ram_caches()
    assert unloaded["pages"] == 0
    assert unloaded["hot_find"] > 0
    assert find_module.cache_status()["pages"]["entries"] == resident_pages
    assert find_module.cache_status()["hot_find"]["entries"] == 0
    assert find_module._CACHE._signatures
    assert freshness.triple(vault, "kb") == before_freshness

    released = find_module.unload_ram_caches(pages=True)
    assert released["pages"] == resident_pages
    assert find_module.cache_status()["pages"]["entries"] == 0
    assert not find_module._CACHE._signatures
    assert freshness.triple(vault, "kb") == before_freshness

    assert find_module.find(vault, query="metabolism")


def test_the_graph_rebuild_unload_seam_leaves_receipt_covered_pages(
    vault: Path,
) -> None:
    """`epistemic_graph` evicts to re-derive the resolver, not to drop pages.

    Its two call sites (`epistemic_graph.py`, inside `_rebuild_all_locked` and
    the incremental topology check) take the default, and the default now
    spares the parsed-page cache. That is the whole point of the split: a graph
    rebuild is the frequent whole-scope event, and on a busy cell it was
    charging every following reader a re-parse of the vault for a projection
    the page rows have no part in.

    Pinned in both directions -- the seam the graph actually calls, and what
    that seam does -- because either half alone can drift without the other
    noticing.
    """
    import inspect

    from exomem import epistemic_graph

    graph_source = inspect.getsource(epistemic_graph)
    assert "unload_ram_caches()" in graph_source, (
        "the graph rebuild no longer takes the page-preserving default"
    )
    assert "unload_ram_caches(pages=True)" not in graph_source, (
        "a graph rebuild must not discard receipt-covered page rows"
    )

    assert find_module.find(vault, query="metabolism")
    resident = find_module.cache_status()["pages"]["entries"]
    assert resident > 0, "the query hydrated no pages, so this pins nothing"

    find_module.unload_ram_caches()

    assert find_module.cache_status()["pages"]["entries"] == resident


def test_keyword_mode_also_cached(vault: Path, monkeypatch) -> None:
    calls = {"n": 0}
    orig = find_module._find_keyword

    def counting(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(find_module, "_find_keyword", counting)
    find_module.find(vault, query="metabolism", mode="keyword")
    find_module.find(vault, query="metabolism", mode="keyword")
    assert calls["n"] == 1


def test_explicit_config_objects_keyed_separately(vault: Path, monkeypatch) -> None:
    calls = _count_semantic(monkeypatch)
    tuned = find_module.RankingConfig(compiled_boost=1.4)
    find_module.find(vault, query="metabolism")
    find_module.find(vault, query="metabolism", config=tuned)
    assert calls["n"] == 2
