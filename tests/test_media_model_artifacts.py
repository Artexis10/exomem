"""New media artifacts fetch complete pins and never build on a serving host."""

import json
from dataclasses import replace

import pytest

from exomem import embedding_backend as backend


def test_cold_media_artifact_fetches_the_declared_graph_weights_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv(backend.ARTIFACT_DIR_ENV, str(tmp_path / "build"))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    served = backend.ServedArtifact(
        revision="pinned-revision", source=(), quantization="fp32", file_format=backend.ONNX_EXTERNAL_DATA,
        files=("onnx/encoder.onnx", "onnx/encoder.data", "vocab.json"),
    )
    source = backend.artifact_dir("example/speech", served)
    (source / "onnx").mkdir(parents=True)
    (source / "onnx/encoder.onnx").write_bytes(b"graph")
    (source / "onnx/encoder.data").write_bytes(b"weights")
    (source / "vocab.json").write_text('{"token":1}')
    digest = backend.artifact_sha256(source, served.files)
    served = replace(served, digest=digest)
    publication = tmp_path / "publication"
    asset = backend.write_artifact_asset("example/speech", served, publication)
    import hashlib

    served = replace(served, asset_digest=hashlib.sha256(asset.read_bytes()).hexdigest())
    monkeypatch.setenv(backend.ARTIFACT_DIR_ENV, str(tmp_path / "host"))
    monkeypatch.setenv(backend.ARTIFACT_URL_ENV, publication.as_uri())
    monkeypatch.setattr(backend, "_build_artifact", lambda *_: pytest.fail("media built on the serving host"))

    graph, installed_digest = backend.ensure_artifact("example/speech", served)

    installed = backend.artifact_dir("example/speech", served)
    assert graph == str(installed / "onnx/encoder.onnx")
    assert installed_digest == digest[:16]
    assert (installed / "onnx/encoder.data").read_bytes() == b"weights"
    assert json.loads((installed / "vocab.json").read_text()) == {"token": 1}


def test_rewritten_cache_manifest_cannot_admit_other_bytes_for_a_media_pin(tmp_path, monkeypatch):
    monkeypatch.setenv(backend.ARTIFACT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(backend, "_ACQUIRE_FAILED", {})
    served = backend.ServedArtifact(
        revision="pinned-revision", source=(), quantization="fp32", file_format=backend.ONNX_EXTERNAL_DATA,
        digest="a" * 64,
    )
    root = backend.artifact_dir("example/query", served)
    root.mkdir(parents=True)
    (root / "model.onnx").write_bytes(b"other graph")
    (root / "model.onnx.data").write_bytes(b"other weights")
    digest = backend.artifact_sha256(root)
    (root / "artifact.json").write_text(json.dumps({
        "model": "example/query", "revision": served.revision, "quantization": served.quantization,
        "file_format": served.file_format, "digest": digest[:16],
    }))
    monkeypatch.setattr(backend, "_build_artifact", lambda *_: pytest.fail("missing pin triggered a host build"))

    with pytest.raises(backend.ModelFilesUnavailable, match="pinned published artifact"):
        backend.ensure_artifact("example/query", served)
