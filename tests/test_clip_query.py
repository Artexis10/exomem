"""Query routing and lifecycle; the release workflow proves real model outputs."""

import numpy as np
import pytest

from exomem import accel, clip_query, embeddings, model_reaper, readiness


@pytest.fixture(autouse=True)
def isolated_clip_models(monkeypatch):
    monkeypatch.setattr(embeddings, "_CLIP_MODEL", None)
    monkeypatch.setattr(embeddings, "_CLIP_QUERY_MODEL", None)
    readiness.reset()
    yield
    readiness.reset()


def test_multilingual_query_and_calibrated_tags_keep_separate_towers(monkeypatch):
    queries = []
    tags = []
    query_vector = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
    query_vector[0] = 1
    tag_vector = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
    tag_vector[1] = 1

    class QueryEncoder:
        def encode(self, texts):
            queries.extend(texts)
            return np.asarray([query_vector])

    class TagEncoder:
        def encode(self, texts, **_kwargs):
            tags.extend(texts)
            return np.asarray([tag_vector])

    monkeypatch.setattr(clip_query, "OnnxClipQueryEncoder", QueryEncoder)
    monkeypatch.setattr(embeddings, "get_clip_model", TagEncoder)

    np.testing.assert_array_equal(embeddings.embed_clip_text("a red bicycle"), query_vector)
    np.testing.assert_array_equal(embeddings.embed_clip_texts(["bicycle"]), [tag_vector])
    assert queries == ["a red bicycle"]
    assert tags == ["bicycle"]


def test_missing_query_runtime_preserves_the_unavailable_image_lane(monkeypatch):
    def missing_runtime():
        raise ModuleNotFoundError("query runtime is not installed", name="onnxruntime")

    monkeypatch.setattr(clip_query, "OnnxClipQueryEncoder", missing_runtime)

    with pytest.raises(embeddings.ClipUnavailable, match="query encoder unavailable"):
        embeddings.embed_clip_text("a red bicycle")
    assert embeddings.CLIP_GUARD.inflight() == 0


def test_missing_query_artifact_preserves_the_unavailable_image_lane(monkeypatch):
    def missing_artifact():
        raise embeddings.embedding_backend.ModelFilesUnavailable("pinned artifact unavailable")

    monkeypatch.setattr(clip_query, "OnnxClipQueryEncoder", missing_artifact)

    with pytest.raises(embeddings.ClipUnavailable, match="pinned artifact unavailable"):
        embeddings.embed_clip_text("a red bicycle")
    assert embeddings.CLIP_GUARD.inflight() == 0


def test_query_only_reaping_waits_for_encodes_and_never_loads_torch(monkeypatch):
    constructions = []

    class QueryEncoder:
        def __init__(self):
            constructions.append(self)

        def encode(self, texts):
            return np.ones((len(texts), embeddings.CLIP_DIM), dtype=np.float32)

    monkeypatch.setattr(clip_query, "OnnxClipQueryEncoder", QueryEncoder)
    monkeypatch.setattr(accel, "empty_cache", lambda: pytest.fail("query-only unload touched Torch"))
    embeddings.embed_clip_text("a red bicycle")
    slot = next(slot for slot in model_reaper.default_slots() if slot.name == "clip")
    assert slot.is_loaded()

    with embeddings.CLIP_GUARD.active():
        assert not slot.unload()
    assert slot.is_loaded()
    assert slot.unload()
    assert not slot.is_loaded()

    embeddings.embed_clip_text("a blue bicycle")
    assert len(constructions) == 2
