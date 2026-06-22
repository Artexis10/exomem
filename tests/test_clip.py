"""CLIP image search — ClipIndex store + find() visual-match fusion (model stubbed)."""

from __future__ import annotations

import numpy as np
import pytest

from kb_mcp import embeddings
from kb_mcp import find as find_module
from kb_mcp import preserve


def _unit(i: int) -> np.ndarray:
    v = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
    v[i] = 1.0
    return v


def test_clip_index_upsert_search_has_delete(vault) -> None:
    idx = embeddings.ClipIndex(vault)
    a_path = "Knowledge Base/Evidence/Yolo/photos/a.jpg"
    b_path = "Knowledge Base/Evidence/Yolo/photos/b.jpg"
    idx.upsert(a_path, _unit(0), 1.0)
    idx.upsert(b_path, _unit(1), 2.0)

    assert idx.has(a_path)
    res = idx.search(_unit(0), k=2)
    assert res[0][0] == a_path  # closest to the query vector
    assert round(res[0][1], 3) == 1.0

    idx.delete(a_path)
    assert not idx.has(a_path)
    assert idx.search(_unit(0), k=2)[0][0] == b_path


def test_clip_index_empty_search(vault) -> None:
    assert embeddings.ClipIndex(vault).search(_unit(0), k=5) == []


def test_clip_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_DISABLE_CLIP", "1")
    assert embeddings.clip_enabled() is False
    monkeypatch.delenv("KB_MCP_DISABLE_CLIP", raising=False)
    assert embeddings.clip_enabled() is True


def test_find_clip_surfaces_textless_image(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLIP visual match surfaces the image sidecar even with ZERO lexical overlap."""
    monkeypatch.delenv("KB_MCP_DISABLE_CLIP", raising=False)
    # Image + sidecar; the text deliberately does NOT contain the query terms.
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="photos", filename="scene.jpg", data=b"\xff\xd8\xff",
        text="a calm beach at sunset",
    )
    img_vec = _unit(3)
    embeddings.ClipIndex(vault).upsert(res.path, img_vec, 1.0)

    # Stub the heavy models: bge returns empty (no text-vector load), CLIP query → the image vec.
    monkeypatch.setattr(
        embeddings, "embed_texts",
        lambda texts, **kw: np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32),
    )
    monkeypatch.setattr(embeddings, "embed_clip_text", lambda q: img_vec)

    find_module.clear_cache()
    hits = find_module.find(vault, query="purple dinosaur costume", mode="hybrid")
    match = [h for h in hits if h.path == res.sidecar_path]
    assert match, [h.path for h in hits]
    d = match[0].as_dict()
    assert d["signals"]["clip_rank"] == 1
    assert d["media_type"] == "image"
    assert d["media_file"].endswith("scene.jpg")


def test_find_clip_skipped_when_disabled(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_DISABLE_CLIP", "1")
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="photos", filename="scene2.jpg", data=b"\xff\xd8\xff",
        text="a calm beach at sunset",
    )
    embeddings.ClipIndex(vault).upsert(res.path, _unit(3), 1.0)
    monkeypatch.setattr(
        embeddings, "embed_texts",
        lambda texts, **kw: np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32),
    )
    # If CLIP ran it would raise (no real model) — proving it's gated off, find must not call it.
    monkeypatch.setattr(embeddings, "embed_clip_text", lambda q: (_ for _ in ()).throw(AssertionError("CLIP ran")))
    find_module.clear_cache()
    hits = find_module.find(vault, query="purple dinosaur costume", mode="hybrid")
    assert not [h for h in hits if h.path == res.sidecar_path]
