"""Hot find cache: repeat-request reuse, parameter separation, freshness
invalidation, caller-mutation safety, and the clear_cache() test hook."""

from __future__ import annotations

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


@pytest.mark.parametrize("sidecar_name", [".embeddings.sqlite", ".clip.sqlite"])
def test_sidecar_generation_invalidates(
    vault: Path, monkeypatch, sidecar_name: str
) -> None:
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
