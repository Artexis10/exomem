"""Persisted previews follow the original from worker claim to inline release."""

from __future__ import annotations

import base64
import hashlib
import io

import pytest
from test_cloud_video_frames import codec_video as codec_video
from test_image_view import _call, _photo, _upload
from test_image_view import served as served
from test_media_store import selected_store as selected_store
from test_pooled_media_processing import _drain

from exomem import extract, image_view, media_jobs, media_processing, media_worker, video_frames

pytestmark = pytest.mark.frame_codecs


@pytest.mark.parametrize("kind", ["image", "video"])
def test_a_worker_persists_one_preview_and_mcp_shows_it_without_decoding(
    vault, served, request, monkeypatch, kind,
):
    from PIL import Image

    mcp, client = served
    filename, original, content_type = (
        ("preview-color-swatch.jpg", _photo(1600, 900, orientation=6, taken="2020:01:01 12:00:00"), "image/jpeg")
        if kind == "image" else ("sample.mp4", request.getfixturevalue("codec_video").read_bytes(), "video/mp4")
    )
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "ocr,speech,previews")
    monkeypatch.setattr(extract, "extract_text", lambda *args, **kwargs: extract.ExtractResult(
        text="Original extraction completed.", media_type=kind, engine="test",
    ))
    path = _upload(client, filename, original, content_type)
    reconciled = media_processing.reconcile_media(vault, path)
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    claimed = media_jobs.MediaJobStore(vault).claim_next()
    assert claimed is not None
    assert worker._process(claimed).state == "handoff"

    from exomem import media_previews

    digest = hashlib.sha256(original).hexdigest()
    jpeg = media_previews.read(vault, path, digest)
    assert jpeg is not None
    worker._drain_parent_results()
    assert "Original extraction completed." in reconciled.sidecar_path.read_text()
    assert media_previews.read(vault, path, "0" * 64) is None
    assert (vault / path).read_bytes() == original
    assert len(jpeg) <= 64 * 1024

    def unexpected_decode(*args, **kwargs):
        pytest.fail("the preview read started a decoder")

    monkeypatch.setattr(image_view, "_render", unexpected_decode)
    monkeypatch.setattr(video_frames, "sample_stream", unexpected_decode)
    result = _call(mcp, {"kind": "preview", "path": reconciled.sidecar_path.relative_to(vault).as_posix()})
    [image] = [item for item in result.content if item.type == "image"]
    assert image.mime_type == "image/jpeg"
    assert base64.b64decode(image.data) == jpeg
    with Image.open(io.BytesIO(jpeg)) as shown:
        assert max(shown.size) <= 512
        assert not shown.getexif()
        if kind == "image":
            assert shown.height > shown.width
    media_processing.reprocess_media(vault, path)
    assert _drain(vault) == ["complete"]
    assert media_previews.read(vault, path, digest) == jpeg


def test_replaced_and_removed_filing_never_serves_stale_pixels(vault, served, monkeypatch):
    from exomem import index_sync, media_previews, state_paths

    mcp, client = served
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "ocr,previews")
    monkeypatch.setattr(extract, "extract_text", lambda *args, **kwargs: extract.ExtractResult(
        text="Original extraction completed.", media_type="image", engine="test",
    ))
    original = _photo(900, 600)
    path = _upload(client, "photo.jpg", original, "image/jpeg")
    media_processing.reconcile_media(vault, path)
    assert _drain(vault) == ["complete"]
    digest = hashlib.sha256(original).hexdigest()
    preview = media_previews.read(vault, path, digest)
    assert preview is not None

    replacement = _photo(600, 900)
    (vault / path).write_bytes(replacement)
    media_processing.reconcile_media(vault, path)
    result = _call(mcp, {"kind": "preview", "path": path})
    assert result.structured_content["status"] == "unavailable"
    assert [block.type for block in result.content] == ["text"]

    assert _drain(vault) == ["complete"]
    replacement_digest = hashlib.sha256(replacement).hexdigest()
    index_sync.drop_media_residue(vault, [path])
    assert media_previews.read(vault, path, replacement_digest) not in (None, preview)
    assert media_previews.read(vault, path, digest) is None
    root = state_paths.vault_state_dir(vault) / media_previews.STORE
    [record] = list(root.glob("*/*.json"))
    assert record.stat().st_size <= 64 * 1024

    # Crash after removing the original, before its index fan-out.
    (vault / path).unlink()
    result = _call(mcp, {"kind": "preview", "path": path})
    assert result.structured_content["error"]["code"] == "ORIGINAL_UNAVAILABLE"
    assert all(block.type != "image" for block in result.content)
    assert record.exists()
    media_processing.reconcile_all_media(vault)
    assert not record.exists()


def test_a_grant_to_download_raw_bytes_does_not_release_derived_pixels(vault, served, monkeypatch):
    from test_shared_original_custody_workflow import _grant, _stream

    from exomem import commands, media_previews, preserve
    from exomem.artifact_recovery import RecoveryUnavailable
    from exomem.governance.principal import RequestPrincipal, library_scope, request_scope

    mcp, _client = served
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "ocr,previews")
    monkeypatch.setattr(extract, "extract_text", lambda *args, **kwargs: extract.ExtractResult(
        text="Original extraction completed.", media_type="image", engine="test",
    ))
    original = _photo(900, 600)
    with library_scope():
        captured = preserve.preserve_stream(
            vault, scope="Archive", category="Photos", filename="sample.jpg",
            stream=io.BytesIO(original), raw_protection=True, media_pending=True,
        )
    media_processing.reconcile_media(vault, captured.path)
    assert _drain(vault) == ["complete"]
    result = _call(mcp, {"kind": "preview", "path": captured.path})
    assert result.structured_content["status"] == "available"

    reader = RequestPrincipal("external", surface="mcp", issuer_family="test-issuer")
    _grant(vault, "01ARZ3NDEKTSV4RRFFQ69G5FD0", captured.path, reader)

    def unexpected_lookup(*args):
        pytest.fail("a caller denied derived pixels reached preview storage")

    monkeypatch.setattr(media_previews, "read", unexpected_lookup)
    with request_scope(reader):
        assert _stream(vault, captured.path, reader) == original
        with pytest.raises(RecoveryUnavailable) as refused:
            commands.op_read_media(vault, media="preview", path=captured.path)
        with pytest.raises(RecoveryUnavailable) as missing:
            commands.op_read_media(vault, media="preview", path="Knowledge Base/Sources/missing.jpg")
    assert str(refused.value) == str(missing.value)


@pytest.mark.parametrize("drift", ["access", "binding"])
def test_a_live_change_during_lookup_prevents_release(vault, served, monkeypatch, drift):
    from test_access_read_paths import _write_cfg

    from exomem import media_previews

    mcp, client = served
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "ocr,previews")
    monkeypatch.setattr(extract, "extract_text", lambda *args, **kwargs: extract.ExtractResult(
        text="Original extraction completed.", media_type="image", engine="test",
    ))
    original = _photo(900, 600)
    path = _upload(client, "sample.jpg", original, "image/jpeg")
    reconciled = media_processing.reconcile_media(vault, path)
    assert _drain(vault) == ["complete"]
    read = media_previews.read

    def drift_during_lookup(*args):
        jpeg = read(*args)
        assert jpeg is not None
        if drift == "access":
            _write_cfg(vault, 'excluded:\n  - "Evidence"\n')
        else:
            sidecar = reconciled.sidecar_path
            content = sidecar.read_text(encoding="utf-8")
            digest = hashlib.sha256(original).hexdigest()
            assert digest in content
            sidecar.write_text(content.replace(digest, "0" * 64), encoding="utf-8")
        return jpeg

    monkeypatch.setattr(media_previews, "read", drift_during_lookup)
    result = _call(mcp, {"kind": "preview", "path": path})
    assert result.structured_content["error"]["code"] == "ORIGINAL_UNAVAILABLE"
    assert all(block.type != "image" for block in result.content)


@pytest.mark.parametrize("enabled", [False, True], ids=["disabled", "undecodable"])
def test_extraction_finishes_when_the_preview_cannot_be_made(vault, served, monkeypatch, enabled):
    mcp, client = served
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "ocr,previews" if enabled else "ocr")
    monkeypatch.setattr(extract, "extract_text", lambda *args, **kwargs: extract.ExtractResult(
        text="Original extraction completed.", media_type="image", engine="test",
    ))
    original = b"damaged image" if enabled else _photo(900, 600)
    path = _upload(client, "sample.jpg", original, "image/jpeg")
    reconciled = media_processing.reconcile_media(vault, path)
    assert _drain(vault) == ["complete"]
    assert "Original extraction completed." in reconciled.sidecar_path.read_text()
    result = _call(mcp, {"kind": "preview", "path": path})
    assert result.structured_content["status"] == "unavailable"
    assert [block.type for block in result.content] == ["text"]


def test_eviction_preserves_the_preview_for_a_proved_read(vault, served, selected_store, monkeypatch):
    from test_media_store import _offload

    from exomem import index_sync, media_previews, media_store, originals

    mcp, client = served
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "ocr,previews")
    monkeypatch.setattr(extract, "extract_text", lambda *args, **kwargs: extract.ExtractResult(
        text="Original extraction completed.", media_type="image", engine="test",
    ))
    original = _photo(900, 600)
    path = _upload(client, "sample.jpg", original, "image/jpeg")
    media_processing.reconcile_media(vault, path)
    assert _drain(vault)
    digest = hashlib.sha256(original).hexdigest()
    jpeg = media_previews.read(vault, path, digest)
    assert jpeg is not None
    _offload(vault, vault / path)
    assert media_store.evict(vault, vault / path)
    assert originals.evicted(vault, path)
    index_sync.drop_media_residue(vault, [path])
    media_processing.reconcile_all_media(vault)
    assert media_previews.read(vault, path, digest) == jpeg

    result = _call(mcp, {"kind": "preview", "path": path})
    [image] = [item for item in result.content if item.type == "image"]
    assert base64.b64decode(image.data) == jpeg
