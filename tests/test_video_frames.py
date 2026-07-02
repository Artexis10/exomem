"""Backend tests for the on-demand video frames tool.

No PyAV/Pillow required: the decode layer is monkeypatched at the module
seams (`video_frames._probe_duration`, `embeddings._decode_frames_at`,
`video_frames._encode_jpeg`). Real JPEG-encode tests run behind
`pytest.importorskip("PIL")`, matching test_clip.py's convention.
"""

from __future__ import annotations

import asyncio
import base64
import json

import numpy as np
import pytest

from exomem import commands as commands_module
from exomem import embeddings, video_frames
from exomem import server as server_module

VIDEO = "Knowledge Base/Sources/clip.mp4"

# Checkerboard + inverse: maximally distant average-hashes, so alternating
# variants never dedup while identical variants always do.
_BASE = (np.indices((8, 8)).sum(axis=0) % 2).astype(np.float32) * 255.0


class _FakeImage:
    """Duck-typed stand-in for a PIL image: enough for `_avg_hash`."""

    def __init__(self, arr: np.ndarray) -> None:
        self.arr = arr

    def convert(self, mode: str) -> _FakeImage:
        return self

    def resize(self, size: tuple[int, int]) -> _FakeImage:
        return self

    def __array__(self, dtype=None, copy=None) -> np.ndarray:
        return self.arr.astype(dtype or np.float32)


def _img(variant: int) -> _FakeImage:
    return _FakeImage(_BASE if variant % 2 == 0 else 255.0 - _BASE)


@pytest.fixture
def video_vault(vault):
    (vault / "Knowledge Base" / "Sources").mkdir(parents=True, exist_ok=True)
    (vault / "Knowledge Base" / "Sources" / "clip.mp4").write_bytes(b"\x00fake-mp4")
    return vault


def _patch_decode(monkeypatch, *, duration=100.0, images=None):
    """Stub the decode layer: known duration, distinct frames, fake JPEG bytes."""
    monkeypatch.setattr(video_frames, "_probe_duration", lambda p: duration)
    if images is None:
        def images(ts_list):
            return [_img(i) for i in range(len(ts_list))]
    monkeypatch.setattr(embeddings, "_decode_frames_at", lambda p, ts_list: images(ts_list))
    monkeypatch.setattr(video_frames, "_encode_jpeg", lambda img: b"\xff\xd8fake")


# ---------------- error paths ----------------


def test_path_escape_guarded(vault) -> None:
    with pytest.raises(video_frames.VideoFramesError) as exc:
        video_frames.get_frames(vault, "../escape.mp4")
    assert exc.value.code == "INVALID_PATH"


def test_missing_file(vault) -> None:
    with pytest.raises(video_frames.VideoFramesError) as exc:
        video_frames.get_frames(vault, "Knowledge Base/Sources/nope.mp4")
    assert exc.value.code == "NOT_FOUND"


def test_non_video_refused(vault) -> None:
    with pytest.raises(video_frames.VideoFramesError) as exc:
        video_frames.get_frames(vault, "Knowledge Base/index.md")
    assert exc.value.code == "NOT_A_VIDEO"


def test_missing_deps_soft_fail(video_vault, monkeypatch) -> None:
    def boom(path):
        raise embeddings.ClipUnavailable("PyAV not installed: No module named 'av'")

    monkeypatch.setattr(video_frames, "_probe_duration", boom)
    with pytest.raises(video_frames.VideoFramesError) as exc:
        video_frames.get_frames(video_vault, VIDEO)
    assert exc.value.code == "VIDEO_DEPS_MISSING"
    assert "media" in exc.value.reason  # names the extra to install


def test_all_seeks_fail(video_vault, monkeypatch) -> None:
    _patch_decode(monkeypatch, images=lambda ts_list: [None] * len(ts_list))
    with pytest.raises(video_frames.VideoFramesError) as exc:
        video_frames.get_frames(video_vault, VIDEO)
    assert exc.value.code == "NO_DECODABLE_FRAMES"


def test_corrupt_container(video_vault, monkeypatch) -> None:
    def boom(path):
        raise OSError("moov atom not found")

    monkeypatch.setattr(video_frames, "_probe_duration", boom)
    with pytest.raises(video_frames.VideoFramesError) as exc:
        video_frames.get_frames(video_vault, VIDEO)
    assert exc.value.code == "NO_DECODABLE_FRAMES"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_sec": -1.0},
        {"start_sec": 5.0, "end_sec": 5.0},
        {"end_sec": 0.0},
        {"start_sec": 100.0},  # exactly at probed duration
        {"start_sec": 150.0},  # past probed duration
    ],
)
def test_bad_range(video_vault, monkeypatch, kwargs) -> None:
    _patch_decode(monkeypatch, duration=100.0)
    with pytest.raises(video_frames.VideoFramesError) as exc:
        video_frames.get_frames(video_vault, VIDEO, **kwargs)
    assert exc.value.code == "BAD_RANGE"


def test_unknown_duration_window_refused(video_vault, monkeypatch) -> None:
    _patch_decode(monkeypatch, duration=None)
    with pytest.raises(video_frames.VideoFramesError) as exc:
        video_frames.get_frames(video_vault, VIDEO, start_sec=10.0)
    assert exc.value.code == "NO_DECODABLE_FRAMES"
    assert "without start_sec/end_sec" in exc.value.reason


# ---------------- sampling and bounding ----------------


def test_default_call_shape(video_vault, monkeypatch) -> None:
    _patch_decode(monkeypatch, duration=100.0)
    result = video_frames.get_frames(video_vault, VIDEO)
    assert result.path == VIDEO
    assert result.duration_sec == 100.0
    assert result.max_frames_effective == 8
    assert result.candidates == 16  # 2x headroom for dedup
    assert len(result.frames) == 8
    ts = [f.timestamp_sec for f in result.frames]
    assert ts == sorted(ts)
    assert all(0.0 < t < 100.0 for t in ts)
    assert all(f.jpeg == b"\xff\xd8fake" for f in result.frames)


def test_max_frames_clamps_to_cap(video_vault, monkeypatch) -> None:
    _patch_decode(monkeypatch, duration=100.0)
    result = video_frames.get_frames(video_vault, VIDEO, max_frames=999)
    assert result.max_frames_effective == 16
    assert len(result.frames) == 16
    assert result.candidates == 32


def test_max_frames_floor_is_one(video_vault, monkeypatch) -> None:
    _patch_decode(monkeypatch, duration=100.0)
    result = video_frames.get_frames(video_vault, VIDEO, max_frames=0)
    assert result.max_frames_effective == 1
    assert len(result.frames) == 1


def test_cap_env_override(video_vault, monkeypatch) -> None:
    _patch_decode(monkeypatch, duration=100.0)
    monkeypatch.setenv("EXOMEM_VIDEO_FRAMES_TOOL_CAP", "4")
    result = video_frames.get_frames(video_vault, VIDEO, max_frames=999)
    assert result.max_frames_effective == 4
    assert len(result.frames) == 4


def test_cap_env_unparseable_falls_back(video_vault, monkeypatch) -> None:
    _patch_decode(monkeypatch, duration=100.0)
    monkeypatch.setenv("EXOMEM_VIDEO_FRAMES_TOOL_CAP", "many")
    result = video_frames.get_frames(video_vault, VIDEO, max_frames=999)
    assert result.max_frames_effective == 16


def test_window_confines_timestamps(video_vault, monkeypatch) -> None:
    _patch_decode(monkeypatch, duration=100.0)
    result = video_frames.get_frames(video_vault, VIDEO, start_sec=10.0, end_sec=20.0)
    assert all(10.0 < f.timestamp_sec < 20.0 for f in result.frames)


def test_end_sec_clamped_to_duration(video_vault, monkeypatch) -> None:
    _patch_decode(monkeypatch, duration=100.0)
    result = video_frames.get_frames(video_vault, VIDEO, start_sec=90.0, end_sec=500.0)
    assert all(90.0 < f.timestamp_sec < 100.0 for f in result.frames)


def test_dedup_collapses_static_video(video_vault, monkeypatch) -> None:
    _patch_decode(monkeypatch, images=lambda ts_list: [_img(0) for _ in ts_list])
    result = video_frames.get_frames(video_vault, VIDEO)
    assert len(result.frames) == 1
    assert result.dedup_dropped == result.candidates - 1


def test_unknown_duration_falls_back_sequential(video_vault, monkeypatch) -> None:
    monkeypatch.setattr(video_frames, "_probe_duration", lambda p: None)
    monkeypatch.setattr(
        embeddings,
        "_sample_video_keyframes",
        lambda p: [(0.0, _img(0)), (1.0, _img(1))],
    )
    monkeypatch.setattr(video_frames, "_encode_jpeg", lambda img: b"\xff\xd8fake")
    result = video_frames.get_frames(video_vault, VIDEO)
    assert result.duration_sec is None
    assert [f.timestamp_sec for f in result.frames] == [0.0, 1.0]


# ---------------- JPEG encoding (needs Pillow) ----------------


def test_encode_jpeg_bounds_long_edge() -> None:
    PIL_Image = pytest.importorskip("PIL.Image")
    src = PIL_Image.new("RGB", (2000, 1000), (200, 30, 30))
    data = video_frames._encode_jpeg(src)
    assert data.startswith(b"\xff\xd8")  # JPEG magic
    import io

    out = PIL_Image.open(io.BytesIO(data))
    assert max(out.size) <= video_frames.FRAME_JPEG_MAX_EDGE


def test_encode_jpeg_handles_alpha() -> None:
    PIL_Image = pytest.importorskip("PIL.Image")
    src = PIL_Image.new("RGBA", (100, 50), (0, 100, 0, 128))
    data = video_frames._encode_jpeg(src)
    assert data.startswith(b"\xff\xd8")


def test_real_video_end_to_end(video_vault) -> None:
    """Integration (skipped without the media extra): encode a two-scene synthetic
    video and pull real frames through the full backend path — no monkeypatching."""
    av = pytest.importorskip("av")
    pytest.importorskip("PIL")
    path = video_vault / "Knowledge Base" / "Sources" / "clip.mp4"
    stripes = ((np.indices((64, 64)).sum(axis=0) % 16) * 4).astype(np.uint8)
    scene_a = np.stack([stripes] * 3, axis=-1)
    scene_b = np.stack([(255 - stripes * 2).astype(np.uint8)] * 3, axis=-1)
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=8)
        stream.width = stream.height = 64
        stream.pix_fmt = "yuv420p"
        for i in range(96):  # 12s: 6s of scene A, 6s of scene B
            frame = av.VideoFrame.from_ndarray(
                scene_a if i < 48 else scene_b, format="rgb24"
            )
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    result = video_frames.get_frames(video_vault, VIDEO, max_frames=4)
    assert result.duration_sec and 10.0 <= result.duration_sec <= 14.0
    assert 1 <= len(result.frames) <= 4
    assert all(f.jpeg.startswith(b"\xff\xd8") for f in result.frames)
    assert all(0.0 <= f.timestamp_sec <= result.duration_sec for f in result.frames)
    ts = [f.timestamp_sec for f in result.frames]
    assert ts == sorted(ts)
    windowed = video_frames.get_frames(
        video_vault, VIDEO, max_frames=2, start_sec=6.0, end_sec=12.0
    )
    assert windowed.frames
    assert all(6.0 <= f.timestamp_sec < 12.0 for f in windowed.frames)


# ---------------- MCP surface ----------------


def _build_server(monkeypatch):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    return server_module.build_server(require_auth=False)


def test_mcp_tool_returns_metadata_then_images(video_vault, monkeypatch) -> None:
    canned = video_frames.FramesResult(
        path=VIDEO,
        duration_sec=42.0,
        frames=[
            video_frames.Frame(timestamp_sec=1.5, jpeg=b"\xff\xd8AA"),
            video_frames.Frame(timestamp_sec=20.25, jpeg=b"\xff\xd8BB"),
        ],
        candidates=4,
        dedup_dropped=2,
        max_frames_effective=2,
    )
    monkeypatch.setattr(
        commands_module.video_frames_module, "get_frames", lambda *a, **k: canned
    )
    mcp = _build_server(monkeypatch)
    result = asyncio.run(
        mcp.call_tool("get_video_frames", {"path": VIDEO}, run_middleware=False)
    )
    meta = result.structured_content
    assert meta["path"] == VIDEO
    assert meta["duration_sec"] == 42.0
    assert meta["frame_count"] == 2
    assert meta["frames"] == [
        {"index": 0, "timestamp_sec": 1.5},
        {"index": 1, "timestamp_sec": 20.25},
    ]
    blocks = result.content
    assert json.loads(blocks[0].text) == meta
    images = blocks[1:]
    assert [b.mimeType for b in images] == ["image/jpeg", "image/jpeg"]
    assert base64.b64decode(images[0].data) == b"\xff\xd8AA"
    assert base64.b64decode(images[1].data) == b"\xff\xd8BB"


def test_mcp_tool_error_carries_code(video_vault, monkeypatch) -> None:
    mcp = _build_server(monkeypatch)
    with pytest.raises(Exception) as exc:
        asyncio.run(
            mcp.call_tool(
                "get_video_frames", {"path": "../escape.mp4"}, run_middleware=False
            )
        )
    assert "INVALID_PATH" in str(exc.value)


def test_mcp_only_surface() -> None:
    assert "get_video_frames" in {c.name for c in commands_module.commands_for("mcp")}
    assert "get_video_frames" not in {
        c.name for c in commands_module.commands_for("rest")
    }
    assert "get_video_frames" not in {
        c.name for c in commands_module.commands_for("cli")
    }


def test_mcp_tool_is_read_only_tier2() -> None:
    cmd = next(
        c for c in commands_module.commands_for("mcp") if c.name == "get_video_frames"
    )
    assert cmd.read_only
    assert cmd.tier == 2
