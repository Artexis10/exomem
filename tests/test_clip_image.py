"""Image-tower lane choice and the recorded vector space; real outputs are proved by the image gate."""

import contextlib

import numpy as np
import pytest

from exomem import clip_image, clip_index, embeddings, image_codecs


@pytest.fixture(autouse=True)
def onnx_image_lane(monkeypatch):
    monkeypatch.setenv("EXOMEM_EMBED_BACKEND", "onnx")
    monkeypatch.setattr(embeddings, "_CLIP_MODEL", None)
    yield


class _Decoded:
    def __init__(self):
        self.mode = "P"

    def convert(self, mode):
        converted = _Decoded()
        converted.mode = mode
        return converted


def _photo(tmp_path, monkeypatch):
    path = tmp_path / "photo.png"
    path.write_bytes(b"decoded by the stub below")
    monkeypatch.setattr(image_codecs, "open_image", lambda _path: contextlib.nullcontext(_Decoded()))
    return path


def _unit(index):
    vector = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
    vector[index] = 1
    return vector


def test_a_torch_free_lane_encodes_images_with_the_onnx_tower(monkeypatch, tmp_path):
    seen = []

    class Tower:
        def encode(self, image, **_kwargs):
            seen.append(image.mode)
            return _unit(2)

    monkeypatch.setattr(clip_image, "OnnxClipImageEncoder", Tower)

    np.testing.assert_array_equal(embeddings.embed_image(_photo(tmp_path, monkeypatch)), _unit(2))
    assert seen == ["RGB"]


def test_a_missing_image_artifact_reports_the_image_lane_unavailable(monkeypatch, tmp_path):
    def missing():
        raise embeddings.embedding_backend.ModelFilesUnavailable("pinned artifact unavailable")

    monkeypatch.setattr(clip_image, "OnnxClipImageEncoder", missing)

    with pytest.raises(embeddings.ClipUnavailable, match="image encoder unavailable"):
        embeddings.embed_image(_photo(tmp_path, monkeypatch))
    assert embeddings.CLIP_GUARD.inflight() == 0


def test_image_tags_stay_unavailable_without_the_torch_text_tower(monkeypatch):
    monkeypatch.setattr(clip_image, "OnnxClipImageEncoder", lambda: pytest.fail("tags must not load the image tower"))

    with pytest.raises(embeddings.ClipUnavailable, match="text tower"):
        embeddings.embed_clip_texts(["bicycle"])


def test_the_first_image_write_records_its_space_and_writer(vault):
    index = clip_index.ClipIndex(vault, encoder_space=embeddings.clip_encoder_space)
    index.upsert("Knowledge Base/Evidence/a.png", _unit(0), 1.0)

    recorded = index.space()
    assert {key: recorded[key] for key in clip_index.SPACE_KEYS} == clip_index.legacy_space()
    assert recorded[f"artifact:{embeddings.clip_encoder_space()['artifact']}"] == "written"


def test_a_sidecar_without_a_record_reads_as_the_legacy_space(vault):
    clip_index.ClipIndex(vault).upsert("Knowledge Base/Evidence/a.png", _unit(0), 1.0)

    assert clip_index.ClipIndex(vault).space() == clip_index.legacy_space()


def test_an_encoder_in_another_space_cannot_write_into_the_sidecar(vault):
    clip_index.ClipIndex(vault).upsert("Knowledge Base/Evidence/a.png", _unit(0), 1.0)

    def other_space():
        return {**embeddings.clip_encoder_space(), "space": "another/image-model"}

    other = clip_index.ClipIndex(vault, encoder_space=other_space)
    with pytest.raises(clip_index.ClipSpaceMismatch):
        other.upsert("Knowledge Base/Evidence/b.png", _unit(1), 1.0)
    paths, _, _ = clip_index.ClipIndex(vault).all_vectors()
    assert paths == ["Knowledge Base/Evidence/a.png"]
