"""media_worker — the async extraction pipeline (extract engines stubbed; no GPU)."""

from __future__ import annotations

import numpy as np
import pytest

from kb_mcp import embeddings, extract, media_worker, preserve
from kb_mcp import find as find_module


def _preserve_media_stub(vault, filename="rec.mp3"):
    """Preserve a media binary with no text → a `pending` stub sidecar."""
    return preserve.preserve_bytes(
        vault, scope="Yolo", category="audio", filename=filename, data=b"FAKEBYTES"
    )


def test_preserve_media_writes_pending_stub(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault)
    assert result.sidecar_path is not None
    body = (vault / result.sidecar_path).read_text(encoding="utf-8")
    assert "media_type: audio" in body
    assert "evidence_file: " in body
    assert "extracted_by: pending" in body


def test_preserve_media_no_stub_when_extraction_disabled(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", "1")
    result = _preserve_media_stub(vault, filename="rec2.mp3")
    assert result.sidecar_path is None  # nothing would fill it → don't write a stub


def test_worker_fills_pending_sidecar(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="call.mp3")
    sidecar = vault / result.sidecar_path
    monkeypatch.setattr(
        extract, "extract_text",
        lambda p, media_type=None: extract.ExtractResult(
            text="discussion of the broken sink and water damage", media_type="audio", engine="faster-whisper:test"
        ),
    )
    w = media_worker.MediaWorker(vault)
    w._process(media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio"))

    body = sidecar.read_text(encoding="utf-8")
    assert "water damage" in body
    assert "extracted_by: faster-whisper:test" in body
    assert "extracted_by: pending" not in body


def test_worker_marks_failed_on_extraction_error(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="bad.mp3")
    sidecar = vault / result.sidecar_path

    def boom(p, media_type=None):
        raise RuntimeError("corrupt container")

    monkeypatch.setattr(extract, "extract_text", boom)
    w = media_worker.MediaWorker(vault)
    w._process(media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio"))

    body = sidecar.read_text(encoding="utf-8")
    assert "extracted_by: failed:" in body
    assert "extracted_by: pending" not in body  # won't re-loop on restart scan


def test_worker_unavailable_leaves_pending(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="later.mp3")
    sidecar = vault / result.sidecar_path

    def unavailable(p, media_type=None):
        raise extract.ExtractionUnavailable("engine not installed")

    monkeypatch.setattr(extract, "extract_text", unavailable)
    w = media_worker.MediaWorker(vault)
    w._process(media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio"))

    # Engine absent now → stays pending so a provisioned box retries on its restart scan.
    assert "extracted_by: pending" in sidecar.read_text(encoding="utf-8")


def test_scan_pending_reenqueues(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    _preserve_media_stub(vault, filename="one.mp3")
    _preserve_media_stub(vault, filename="two.wav")
    w = media_worker.MediaWorker(vault)
    assert w.scan_pending() == 2


def test_worker_clip_embeds_image(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_CLIP", raising=False)
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="photos", filename="p.jpg", data=b"\xff\xd8\xff", text="beach",
    )
    monkeypatch.setattr(embeddings, "embed_image", lambda p: np.ones(embeddings.CLIP_DIM, dtype=np.float32))
    w = media_worker.MediaWorker(vault)
    w._process(media_worker._Job(
        binary_path=vault / res.path, sidecar_path=vault / res.sidecar_path,
        media_type="image", do_ocr=False, do_clip=True,
    ))
    assert embeddings.ClipIndex(vault).has(res.path)


def test_scan_unindexed_images_enqueues(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_CLIP", raising=False)
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    preserve.preserve_bytes(vault, scope="Yolo", category="photos", filename="x.jpg", data=b"\xff\xd8\xff", text="t")
    preserve.preserve_bytes(vault, scope="Yolo", category="photos", filename="y.png", data=b"\x89PNG", text="t")
    w = media_worker.MediaWorker(vault)
    assert w._scan_unindexed_images() == 2  # both images queued for CLIP


def test_find_surfaces_media_fields(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    # Provide text so the sidecar is populated + keyword-findable; media frontmatter is set either way.
    preserve.preserve_bytes(
        vault, scope="Yolo", category="audio", filename="meeting.mp3", data=b"X",
        text="quarterly review of the water damage claim",
    )
    find_module.clear_cache()
    hits = find_module.find(vault, query="water damage claim", mode="keyword")
    media = [h for h in hits if "meeting.mp3.md" in h.path]
    assert media, [h.path for h in hits]
    d = media[0].as_dict()
    assert d["media_type"] == "audio"
    assert d["media_file"].endswith("meeting.mp3")
