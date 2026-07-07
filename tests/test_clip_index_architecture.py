"""Architecture checks for the CLIP visual sidecar store split."""

from __future__ import annotations

from exomem import clip_index, embeddings


def test_clip_index_remains_reexported_from_embeddings() -> None:
    assert embeddings.ClipIndex is clip_index.ClipIndex
    assert embeddings.CLIP_DIM == clip_index.CLIP_DIM
