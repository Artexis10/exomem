"""Video visual search — no-audio handling + CLIP keyframe embedding (engines stubbed)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from kb_mcp import backfill, embeddings, extract, media_worker, preserve


def test_transcribe_silent_video_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # A video with no audio stream → empty transcript, engine "no-audio", never raises,
    # and Whisper is never even loaded.
    monkeypatch.setattr(extract, "_has_audio_stream", lambda p: False)
    monkeypatch.setattr(extract, "_get_whisper", lambda: (_ for _ in ()).throw(AssertionError("loaded whisper")))
    r = extract._transcribe(Path("clip.mp4"), "video")
    assert r.text == "" and r.engine == "no-audio" and r.media_type == "video"


def test_embed_video_mean_pools_keyframes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings, "_sample_video_frames", lambda p, n: ["f1", "f2"])

    class _Model:
        def encode(self, frames, **kw):
            e0 = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
            e0[0] = 1.0
            e1 = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
            e1[1] = 1.0
            return np.stack([e0, e1])

    monkeypatch.setattr(embeddings, "get_clip_model", lambda: _Model())
    v = embeddings.embed_video(Path("clip.mp4"))
    assert v.shape == (embeddings.CLIP_DIM,)
    # mean of two orthogonal unit vectors, renormalized → 1/sqrt(2) on each axis
    assert abs(v[0] - 0.70710) < 1e-3 and abs(v[1] - 0.70710) < 1e-3


def test_embed_video_no_frames_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings, "_sample_video_frames", lambda p, n: [])
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: object())
    with pytest.raises(embeddings.ClipUnavailable):
        embeddings.embed_video(Path("clip.mp4"))


def test_worker_clip_embeds_video_via_keyframes(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_CLIP", raising=False)
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="clips", filename="demo.mp4", data=b"\x00\x00video", text="x",
    )
    called = {}
    monkeypatch.setattr(embeddings, "embed_video", lambda p: called.setdefault("v", np.ones(embeddings.CLIP_DIM, np.float32)))
    monkeypatch.setattr(embeddings, "embed_image", lambda p: (_ for _ in ()).throw(AssertionError("used embed_image for video")))
    w = media_worker.MediaWorker(vault)
    w._process(media_worker._Job(
        binary_path=vault / res.path, sidecar_path=vault / res.sidecar_path,
        media_type="video", do_ocr=False, do_clip=True,
    ))
    assert "v" in called  # embed_video was used, not embed_image
    assert embeddings.ClipIndex(vault).has(res.path)


def test_backfill_clip_indexes_video(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_DISABLE_CLIP", raising=False)
    p = vault / "Knowledge Base/Evidence/Old/clips/legacy.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00video")
    monkeypatch.setattr(embeddings, "embed_video", lambda f: np.ones(embeddings.CLIP_DIM, np.float32))
    stats = backfill.backfill_media(vault, do_ocr=False, log_fn=lambda *a: None)
    assert stats.clip_indexed == 1
    assert embeddings.ClipIndex(vault).has("Knowledge Base/Evidence/Old/clips/legacy.mp4")
