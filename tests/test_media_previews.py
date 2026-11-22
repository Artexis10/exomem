"""Persisted previews follow the original from worker claim to inline release."""

from __future__ import annotations

import base64
import hashlib
import io
import os

import pytest
from test_cloud_video_frames import codec_video as codec_video
from test_image_view import _call, _photo, _upload
from test_image_view import served as served
from test_media_store import selected_store as selected_store
from test_pooled_media_processing import _drain

from exomem import (
    artifact_recovery,
    extract,
    media_jobs,
    media_previews,
    media_processing,
    media_worker,
)

pytestmark = [
    pytest.mark.frame_codecs,
    pytest.mark.skipif(os.name != "posix", reason="the codec child requires POSIX resource limits"),
]


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
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "frames,previews")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")

    def unexpected(*args, **kwargs):
        pytest.fail("preview requested extraction, decoding at read time, or alias discovery")

    monkeypatch.setattr(extract, "extract_text", unexpected)
    path = _upload(client, filename, original, content_type)
    store = media_jobs.MediaJobStore(vault)
    assert store.get_by_binary(vault / path) is None
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    claimed = store.claim_next()
    assert claimed is not None and claimed.media_type == media_jobs.PREVIEW_MEDIA_TYPE
    assert worker._process(claimed).state == "complete"
    store.complete(claimed)
    assert store.claim_next() is None

    digest = hashlib.sha256(original).hexdigest()
    jpeg = media_previews.read(vault, path, digest)
    assert jpeg is not None
    assert media_previews.read(vault, path, "0" * 64) is None
    assert (vault / path).read_bytes() == original

    monkeypatch.setattr(media_previews, "_decode", unexpected)
    monkeypatch.setattr(artifact_recovery, "_resolve", unexpected)
    result = _call(mcp, {"kind": "preview", "path": path})
    refusal = _call(mcp, {"kind": "preview", "path": claimed.sidecar_path.relative_to(vault).as_posix()})
    assert refusal.structured_content["error"]["code"] == "ORIGINAL_UNAVAILABLE"
    [image] = [item for item in result.content if item.type == "image"]
    assert image.mime_type == "image/jpeg"
    assert base64.b64decode(image.data) == jpeg
    with Image.open(io.BytesIO(jpeg)) as shown:
        assert max(shown.size) <= 512
        assert not shown.getexif()
        if kind == "image":
            assert shown.height > shown.width
    media_processing.reconcile_media(vault, path)
    assert store.claim_next() is None
    assert media_previews.read(vault, path, digest) == jpeg


def test_replaced_and_removed_filing_never_serves_stale_pixels(vault, served, monkeypatch):
    from exomem import index_sync, state_paths

    mcp, client = served
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "ocr,previews")
    monkeypatch.setattr(extract, "extract_text", lambda *args, **kwargs: extract.ExtractResult(
        text="Original extraction completed.", media_type="image", engine="test",
    ))
    original = _photo(900, 600)
    path = _upload(client, "photo.jpg", original, "image/jpeg")
    media_processing.reconcile_media(vault, path)
    assert _drain(vault) == ["complete", "complete"]
    digest = hashlib.sha256(original).hexdigest()
    preview = media_previews.read(vault, path, digest)
    assert preview is not None

    replacement = _photo(600, 900)
    (vault / path).write_bytes(replacement)
    media_processing.reconcile_media(vault, path)
    result = _call(mcp, {"kind": "preview", "path": path})
    assert result.structured_content["status"] == "unavailable"
    assert [block.type for block in result.content] == ["text"]

    assert _drain(vault) == ["complete", "complete"]
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

    from exomem import commands, preserve
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
    assert _drain(vault) == ["complete", "complete"]
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

    mcp, client = served
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "ocr,previews")
    monkeypatch.setattr(extract, "extract_text", lambda *args, **kwargs: extract.ExtractResult(
        text="Original extraction completed.", media_type="image", engine="test",
    ))
    original = _photo(900, 600)
    path = _upload(client, "sample.jpg", original, "image/jpeg")
    reconciled = media_processing.reconcile_media(vault, path)
    assert _drain(vault) == ["complete", "complete"]
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


def test_a_hanging_codec_times_out_after_extraction_has_published(vault, served, monkeypatch):
    import subprocess
    import sys

    mcp, client = served
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "ocr,previews")
    monkeypatch.setattr(extract, "extract_text", lambda *args, **kwargs: extract.ExtractResult(
        text="Original extraction completed.", media_type="image", engine="test",
    ))
    path = _upload(client, "preview-timeout-fixture.jpg", _photo(900, 600), "image/jpeg")
    reconciled = media_processing.reconcile_media(vault, path)
    run = subprocess.run
    captured_timeouts = []

    def hang(command, **kwargs):
        assert command[2] == "exomem.media_preview_child"
        assert kwargs["stdin"].seekable()
        assert "Original extraction completed." in reconciled.sidecar_path.read_text()
        captured_timeouts.append(kwargs["timeout"])
        return run([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

    assert media_previews.DECODE_SECONDS == 15
    monkeypatch.setattr(media_previews, "DECODE_SECONDS", 0.2)
    monkeypatch.setattr(subprocess, "run", hang)
    assert _drain(vault) == ["complete", "complete"]
    [timeout] = captured_timeouts
    assert 0 < timeout <= 0.2
    result = _call(mcp, {"kind": "preview", "path": path})
    assert result.structured_content["status"] == "unavailable"
    assert [block.type for block in result.content] == ["text"]


def test_enabling_previews_does_not_queue_old_filings_in_a_broad_pass(vault, served, monkeypatch):
    _mcp, client = served
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    path = _upload(client, "preview-sweep-fixture.jpg", _photo(900, 600), "image/jpeg")
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "previews")
    media_processing.reconcile_all_media(vault)
    store = media_jobs.MediaJobStore(vault)
    assert store.get_by_binary(vault / path, media_type=media_jobs.PREVIEW_MEDIA_TYPE) is None


def test_inline_debug_keeps_stored_previews_but_admits_no_preview_work(vault, served, monkeypatch):
    from exomem import media_engines

    mcp, client = served
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "previews")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    older = _upload(client, "preview-inline-stored-fixture.jpg", _photo(900, 600), "image/jpeg")
    assert _drain(vault) == ["complete"]

    monkeypatch.setenv("EXOMEM_MEDIA_WORKER_MODE", "inline")
    assert media_engines.status()[media_engines.PREVIEWS] == media_engines.UNAVAILABLE
    fresh = _upload(client, "preview-inline-fresh-fixture.jpg", _photo(600, 900), "image/jpeg")
    assert media_jobs.MediaJobStore(vault).get_by_binary(
        vault / fresh, media_type=media_jobs.PREVIEW_MEDIA_TYPE,
    ) is None
    assert _call(mcp, {"kind": "preview", "path": older}).structured_content["status"] == "available"


def test_busy_preview_retries_by_itself_after_extraction(vault, served, monkeypatch):
    import time
    from types import SimpleNamespace

    mcp, client = served
    monkeypatch.setenv("EXOMEM_MEDIA_ENGINES", "ocr,previews")
    extractions = []

    def extract_image(*args, **kwargs):
        extractions.append(1)
        return extract.ExtractResult(text="Original extraction completed.", media_type="image", engine="test")

    monkeypatch.setattr(extract, "extract_text", extract_image)
    path = _upload(client, "preview-busy-fixture.jpg", _photo(900, 600), "image/jpeg")
    store = media_jobs.MediaJobStore(vault)
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    ordinary = store.claim_next()
    assert ordinary is not None and ordinary.do_ocr
    assert worker._process(ordinary).state == "handoff"
    worker._drain_parent_results()
    assert "Original extraction completed." in ordinary.sidecar_path.read_text()
    assert store.get_by_binary(vault / path) is None

    now = time.time()
    clock = SimpleNamespace(time=lambda: now)
    monkeypatch.setattr(media_jobs, "time", clock)
    first = store.claim_next()
    assert first is not None and first.media_type == media_jobs.PREVIEW_MEDIA_TYPE
    hold_boundary = worker._holding_boundary
    with monkeypatch.context() as busy:
        busy.setattr(worker, "_holding_boundary", lambda operation, unit: media_worker._BUSY)
        assert worker._process(first).state == "complete"
    store.complete(first)
    pending = store.get(first.id)
    assert pending is not None and pending.attempts == 1 and pending.state == media_jobs.PENDING
    assert store.claim_next() is None
    now += media_jobs.RETRY_FIRST_SECONDS
    second = store.claim_next()
    assert second is not None and second.id == first.id and second.attempts == 2
    assert worker._holding_boundary == hold_boundary
    assert worker._process(second).state == "complete"
    store.complete(second)
    assert store.claim_next() is None and extractions == [1]
    assert _call(mcp, {"kind": "preview", "path": path}).structured_content["status"] == "available"


def test_eviction_preserves_the_preview_for_a_proved_read(vault, served, selected_store, monkeypatch):
    from test_governance_egress import _external, _reset_governance_caches, write_rule, write_scope
    from test_media_store import _offload

    from exomem import commands, index_sync, media_store, originals
    from exomem.governance.principal import request_scope

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

    write_scope(vault, paths=path.removeprefix("Knowledge Base/"))
    write_rule(vault, ceiling=5)
    _reset_governance_caches()
    with request_scope(_external()), pytest.raises(artifact_recovery.RecoveryUnavailable):
        commands.op_read_media(vault, media="preview", path=path)
    assert originals.evicted(vault, path)

    result = _call(mcp, {"kind": "preview", "path": path})
    [image] = [item for item in result.content if item.type == "image"]
    assert base64.b64decode(image.data) == jpeg
