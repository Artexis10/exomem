from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from exomem import embeddings, readiness
from exomem import find as find_module


def test_live_embedding_slices_oversized_file_in_order(vault, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_LIVE_EMBED_MAX_CHUNKS", "3")
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    readiness.reset()

    path = vault / "Knowledge Base" / "Notes" / "large.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Large\n", encoding="utf-8")
    page = SimpleNamespace(rel_path="Knowledge Base/Notes/large.md")
    monkeypatch.setattr(find_module._CACHE, "get", lambda *_args: page)
    chunks = [f"chunk-{i}" for i in range(7)]
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: chunks)

    calls: list[list[str]] = []

    def _embed(texts, *, is_query):
        assert is_query is False
        calls.append(list(texts))
        return np.asarray([[float(text.split("-")[1])] * embeddings.VECTOR_DIM for text in texts])

    monkeypatch.setattr(embeddings, "embed_texts", _embed)
    written: dict = {}

    class _Index:
        def upsert_file(self, rel_path, actual_chunks, vectors, mtime):
            written.update(rel_path=rel_path, chunks=actual_chunks, vectors=vectors, mtime=mtime)

        def delete_file(self, _rel_path):
            raise AssertionError("existing file must not be deleted")

        def delete_semantic_units(self, _rel_path):
            return None

    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: _Index())
    embeddings.upsert_after_write(vault, [path])

    assert [len(batch) for batch in calls] == [3, 3, 1]
    assert written["chunks"] == chunks
    assert written["vectors"][:, 0].tolist() == list(map(float, range(7)))
