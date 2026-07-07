"""Architecture checks for the text embedding sidecar store split."""

from __future__ import annotations

from exomem import embedding_index, embeddings


def test_embedding_index_remains_reexported_from_embeddings() -> None:
    assert embeddings.EmbeddingIndex is embedding_index.EmbeddingIndex
    assert embeddings.VECTOR_DIM == embedding_index.VECTOR_DIM
