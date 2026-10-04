from __future__ import annotations

import contextlib
from types import SimpleNamespace

import numpy as np
import pytest

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


def test_encode_batch_size_follows_the_device_and_bounds_peak_memory(monkeypatch) -> None:
    """Batch size sets peak resident memory, which sets how many cells fit a node.

    Measured with bge-base at ~280-token chunks on CPU: batch 32 peaks at
    1332 MiB and yields 1.8 chunks/s; batch 8 peaks at 918 MiB and yields
    2.3 chunks/s. A large batch on CPU is worse on both axes, so the default
    must not be one global constant shared with accelerators — an oversized CPU
    batch would silently halve hosted tenant density.
    """
    monkeypatch.delenv("EXOMEM_EMBED_BATCH", raising=False)

    assert embeddings.encode_batch_size(SimpleNamespace(device="cpu")) == 8
    # A model object that never exposed `device` must not accidentally take the
    # accelerator path — default to the memory-safe choice.
    assert embeddings.encode_batch_size(object()) == 8
    for accelerated in ("cuda", "cuda:0", "CUDA:1", "mps"):
        assert embeddings.encode_batch_size(SimpleNamespace(device=accelerated)) == 32

    monkeypatch.setenv("EXOMEM_EMBED_BATCH", "4")
    assert embeddings.encode_batch_size(SimpleNamespace(device="cpu")) == 4
    assert embeddings.encode_batch_size(SimpleNamespace(device="cuda")) == 4

    # A malformed or nonsensical override falls back to the device default
    # rather than encoding one chunk at a time or crashing an indexing run.
    for bad in ("", "   ", "0", "-8", "many"):
        monkeypatch.setenv("EXOMEM_EMBED_BATCH", bad)
        assert embeddings.encode_batch_size(SimpleNamespace(device="cpu")) == 8


@pytest.mark.parametrize("service,quantization,expected_turn_sizes", [
    (True, "ort-dynamic-int8", [1] * 6),
    (True, None, [3, 3]),
    (False, "ort-dynamic-int8", [3, 3]),
])
def test_service_int8_releases_execution_between_serial_inferences(
    monkeypatch, service, quantization, expected_turn_sizes,
) -> None:
    """Foreground fairness must apply between the ONNX backend's actual calls."""
    monkeypatch.setenv("EXOMEM_EMBED_BATCH", "3")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1" if service else "0")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1" if service else "legacy")
    turns = []
    active = False

    @contextlib.contextmanager
    def execution():
        nonlocal active
        assert not active
        active = True
        try:
            yield
        finally:
            active = False

    def encode(texts, **_kwargs):
        assert active
        turns.append(len(texts))
        return np.array([[float(text)] for text in texts], dtype=np.float32)

    model = SimpleNamespace(backend="onnx", device="cpu", profile=SimpleNamespace(quantization=quantization), encode=encode)
    result = embeddings._encode_in_turns(model, [str(i) for i in range(6)], admission=contextlib.nullcontext, execution=execution)
    assert turns == expected_turn_sizes
    assert result[:, 0].tolist() == list(range(6))
    assert embeddings.encode_batch_size(model) == 3  # Stored/operator batch policy is unchanged.
