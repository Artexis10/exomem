"""Pure scene-detection core — hash/histogram boundary logic, no PyAV/PIL/torch."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

from kb_mcp import embeddings
from kb_mcp.embeddings import detect_scenes

FLAT = np.full(32, 1 / 32, dtype=np.float32)  # uniform histogram — no luminance signal


def _hist(peak: int) -> np.ndarray:
    h = np.zeros(32, dtype=np.float32)
    h[peak] = 1.0
    return h


def _bits(n: int) -> int:
    """Hash with the low n bits set — hamming(0, _bits(n)) == n."""
    return (1 << n) - 1


def _hrange(lo: int, hi: int) -> int:
    v = 0
    for i in range(lo, hi):
        v |= 1 << i
    return v


def test_static_jitter_is_one_scene() -> None:
    # Talking-head: near-dup hashes wobbling a few bits never open a scene.
    series = [(float(t), _bits(3) if (t // 8) % 2 else 0, FLAT) for t in range(0, 160, 8)]
    scenes = detect_scenes(series)
    assert len(scenes) == 1
    mid = (series[0][0] + series[-1][0]) / 2
    assert abs(scenes[0].rep_ts - mid) <= 8  # representative near the temporal midpoint


def test_distinct_blocks_become_scenes() -> None:
    a, b, c = 0, _bits(20), _bits(40)
    series = (
        [(float(t), a, FLAT) for t in range(0, 30, 2)]
        + [(float(t), b, FLAT) for t in range(30, 60, 2)]
        + [(float(t), c, FLAT) for t in range(60, 90, 2)]
    )
    scenes = detect_scenes(series)
    assert len(scenes) == 3
    assert scenes[1].start_ts == 30.0
    assert scenes[2].start_ts == 60.0
    assert 30.0 <= scenes[1].rep_ts < 60.0  # rep drawn from inside the scene


def test_flicker_merges_within_min_scene_secs() -> None:
    # A → B → A within min_scene_secs: one boundary, not three scenes.
    a, b = 0, _bits(20)
    series = (
        [(float(t), a, FLAT) for t in range(0, 10)]
        + [(10.5, b, FLAT)]
        + [(11.0 + float(t), a, FLAT) for t in range(0, 10)]
    )
    scenes = detect_scenes(series, min_scene_secs=4.0)
    assert len(scenes) == 2


def test_cap_merges_weakest_boundary() -> None:
    # Boundary strengths: B=30 bits, C=12 bits (weakest), D=52 bits.
    a, b, c, d = 0, _hrange(0, 30), _hrange(0, 42), _hrange(30, 64)
    series = (
        [(float(t), a, FLAT) for t in range(0, 20, 2)]
        + [(float(t), b, FLAT) for t in range(20, 40, 2)]
        + [(float(t), c, FLAT) for t in range(40, 60, 2)]
        + [(float(t), d, FLAT) for t in range(60, 80, 2)]
    )
    assert len(detect_scenes(series)) == 4  # sanity: uncapped
    scenes = detect_scenes(series, max_scenes=3)
    assert [s.start_ts for s in scenes] == [0.0, 20.0, 60.0]  # weak C boundary merged away


def test_histogram_change_detected_when_hash_blind() -> None:
    # Two flat frames hash identically — the histogram lane must catch the shift.
    series = (
        [(float(t), 0, _hist(2)) for t in range(0, 30, 2)]
        + [(float(t), 0, _hist(20)) for t in range(30, 60, 2)]
    )
    scenes = detect_scenes(series)
    assert len(scenes) == 2
    assert scenes[1].start_ts == 30.0


def test_empty_and_single_candidate() -> None:
    assert detect_scenes([]) == []
    scenes = detect_scenes([(7.0, 0, FLAT)])
    assert len(scenes) == 1
    assert scenes[0].rep_ts == 7.0


def test_detect_scenes_uses_env_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_VIDEO_SCENE_THRESHOLD", "25")
    series = [(0.0, 0, FLAT), (10.0, _bits(20), FLAT)]
    assert len(detect_scenes(series)) == 1  # 20 ≤ 25 → no boundary


def test_env_knobs_override_and_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_VIDEO_SCENE_THRESHOLD", "15")
    assert embeddings._scene_hash_threshold() == 15
    monkeypatch.setenv("KB_MCP_VIDEO_SCENE_THRESHOLD", "chunky")
    assert embeddings._scene_hash_threshold() == embeddings.SCENE_HASH_THRESHOLD
    monkeypatch.setenv("KB_MCP_VIDEO_SCENE_MIN_SECS", "2.5")
    assert embeddings._scene_min_secs() == 2.5
    monkeypatch.setenv("KB_MCP_VIDEO_SCENE_MIN_SECS", "nah")
    assert embeddings._scene_min_secs() == embeddings.SCENE_MIN_SECS


def test_hash_bits_matches_avg_hash_semantics() -> None:
    arr = np.arange(64, dtype=np.float32).reshape(8, 8)
    v = embeddings._hash_bits(arr)
    assert v.bit_length() <= 64
    assert bin(v).count("1") == 32  # exactly the brighter-than-mean half


def test_gray_hist_normalized() -> None:
    arr = np.full((64, 64), 128, dtype=np.uint8)
    h = embeddings._gray_hist(arr)
    assert h.shape == (32,)
    assert float(h.sum()) == pytest.approx(1.0)
    assert h[16] == 1.0  # 128 lands in bin 16 of 32 over [0, 256)


def test_pool_gray_shrinks_to_hash_grid() -> None:
    arr = np.arange(64 * 64, dtype=np.float32).reshape(64, 64)
    pooled = embeddings._pool_gray(arr)
    assert pooled.shape == (8, 8)
    assert pooled[0, 0] == pytest.approx(arr[:8, :8].mean())


# --- Sampling layer (PyAV faked / skipped) -----------------------------------


class _FakeImg:
    """Minimal PIL stand-in: convert/resize chain + array protocol."""

    def __init__(self, arr: np.ndarray) -> None:
        self._arr = arr

    def convert(self, mode: str) -> _FakeImg:
        return self

    def resize(self, size: tuple[int, int]) -> _FakeImg:
        return _FakeImg(np.resize(self._arr, (size[1], size[0])))

    def __array__(self, dtype=None, copy=None) -> np.ndarray:
        return self._arr.astype(dtype) if dtype is not None else self._arr


class _FakeClip:
    def encode(self, images, **kwargs) -> np.ndarray:
        return np.ones((len(images), embeddings.CLIP_DIM), dtype=np.float32)


_GRAY = np.zeros((64, 64), dtype=np.uint8)
_GRAD = (np.arange(64 * 64, dtype=np.uint8) % 251).reshape(64, 64)


def _fake_pil(monkeypatch: pytest.MonkeyPatch) -> None:
    """Satisfy `from PIL import Image` without Pillow installed."""
    monkeypatch.setitem(sys.modules, "PIL", types.SimpleNamespace(Image=object))


def test_gate_off_uses_uniform_sampler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_VIDEO_SCENE_FRAMES", raising=False)
    _fake_pil(monkeypatch)
    frames = [(float(t), _FakeImg(_GRAD)) for t in range(0, 40, 8)]
    called: dict[str, object] = {}
    monkeypatch.setattr(
        embeddings, "_sample_video_keyframes", lambda p: called.setdefault("uniform", frames)
    )
    monkeypatch.setattr(
        embeddings,
        "sample_video_scenes",
        lambda p: (_ for _ in ()).throw(AssertionError("scene path used with gate off")),
    )
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: _FakeClip())
    out = embeddings.embed_video_frames(Path("v.mp4"))
    assert "uniform" in called
    assert len(out) == 1  # identical frames → pHash dedup keeps the first
    assert out[0][0] == 0.0


def test_gate_on_embed_video_frames_returns_scene_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_MCP_VIDEO_SCENE_FRAMES", "1")
    _fake_pil(monkeypatch)
    series = [(float(t), 0 if t < 30 else _bits(20), FLAT) for t in range(0, 60, 2)]
    monkeypatch.setattr(embeddings, "_iter_iframe_metrics", lambda p: series)
    monkeypatch.setattr(
        embeddings, "_decode_frames_at", lambda p, ts: [_FakeImg(_GRAY) for _ in ts]
    )
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: _FakeClip())
    out = embeddings.embed_video_frames(Path("v.mp4"))
    assert [ts for ts, _ in out] == [14.0, 44.0]  # scene midpoints, not uniform ticks
    assert all(v.shape == (embeddings.CLIP_DIM,) for _, v in out)


def test_scene_sampling_falls_back_when_pass1_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        embeddings,
        "_iter_iframe_metrics",
        lambda p: (_ for _ in ()).throw(RuntimeError("codec exploded")),
    )
    frames = [(float(t), _FakeImg(_GRAD)) for t in range(0, 40, 8)]
    monkeypatch.setattr(embeddings, "_sample_video_keyframes", lambda p: frames)
    pairs = embeddings.sample_video_scenes(Path("v.mp4"))
    assert len(pairs) == 1  # identical frames → one scene
    assert pairs[0][0].rep_ts in {ts for ts, _ in frames}


def test_scene_sampling_falls_back_on_too_few_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        embeddings, "_iter_iframe_metrics", lambda p: [(0.0, 0, FLAT)]
    )  # < MIN_VIDEO_KEYFRAMES
    frames = [(float(t), _FakeImg(_GRAD)) for t in range(0, 40, 8)]
    called: dict[str, object] = {}
    monkeypatch.setattr(
        embeddings, "_sample_video_keyframes", lambda p: called.setdefault("uniform", frames)
    )
    pairs = embeddings.sample_video_scenes(Path("v.mp4"))
    assert "uniform" in called
    assert len(pairs) == 1


def test_real_two_scene_video_end_to_end(tmp_path: Path) -> None:
    """Integration (skipped without the media extra): encode a two-scene synthetic
    video and assert one boundary lands near the content switch."""
    av = pytest.importorskip("av")
    pytest.importorskip("PIL")
    path = tmp_path / "two_scene.mp4"
    stripes = ((np.indices((64, 64)).sum(axis=0) % 16) * 4).astype(np.uint8)
    scene_a = np.stack([stripes] * 3, axis=-1)
    scene_b = np.stack([(255 - stripes * 2).astype(np.uint8)] * 3, axis=-1)
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=8)
        stream.width = stream.height = 64
        stream.pix_fmt = "yuv420p"
        stream.codec_context.gop_size = 8  # I-frame every second at 8 fps
        for i in range(96):  # 12s: 6s of scene A, 6s of scene B
            frame = av.VideoFrame.from_ndarray(scene_a if i < 48 else scene_b, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    metrics = embeddings._iter_iframe_metrics(path)
    assert len(metrics) >= embeddings.MIN_VIDEO_KEYFRAMES
    scenes = detect_scenes(metrics)
    assert len(scenes) == 2
    assert 4.0 <= scenes[1].start_ts <= 8.0  # boundary near the 6s switch
    pairs = embeddings.sample_video_scenes(path)
    assert len(pairs) == 2
    assert all(img is not None for _, img in pairs)
