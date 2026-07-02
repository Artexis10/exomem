"""On-demand video keyframe retrieval — backend for the `get_video_frames` MCP tool.

Composes the decode primitives that already live in `embeddings` (PyAV seek
decode, perceptual-hash near-dup suppression) into a bounded, vault-confined,
soft-failing sampler that returns JPEG bytes + timestamps. Pure decode and
transcode — no model runs here. The frames come back INLINE in the tool result
so the client never needs an HTTP fetch (openspec capability: video-frames).
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import embeddings
from .extract import media_type_for
from .vault import VaultPathError, resolve_under_vault

DEFAULT_TOOL_FRAMES = 8
MAX_TOOL_FRAMES = 16  # hard cap per call (KB_MCP_VIDEO_FRAMES_TOOL_CAP overrides)
CANDIDATE_MULTIPLIER = 2  # sample 2x the requested frames so dedup has headroom
FRAME_JPEG_MAX_EDGE = 768  # longest side of returned JPEGs — server policy, not a param
FRAME_JPEG_QUALITY = 80


class VideoFramesError(Exception):
    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Frame:
    timestamp_sec: float
    jpeg: bytes


@dataclass(frozen=True)
class FramesResult:
    path: str  # resolved vault-relative posix path
    duration_sec: float | None
    frames: list[Frame]
    candidates: int  # sampled before dedup/cap
    dedup_dropped: int
    max_frames_effective: int


def _tool_frames_cap() -> int:
    raw = os.environ.get("KB_MCP_VIDEO_FRAMES_TOOL_CAP")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return MAX_TOOL_FRAMES


def _probe_duration(path: Path) -> float | None:
    """Container-metadata duration in seconds, or None when unknown. No decode."""
    try:
        import av
    except ImportError as e:
        raise embeddings.ClipUnavailable(f"PyAV not installed: {e}") from e
    with av.open(str(path)) as container:
        if not container.streams.video:
            return None
        stream = container.streams.video[0]
        if stream.duration and stream.time_base:
            return float(stream.duration * stream.time_base)
        if container.duration:
            return container.duration / av.time_base
    return None


def _encode_jpeg(img) -> bytes:
    """Bounded JPEG encode of a PIL image: longest side ≤ FRAME_JPEG_MAX_EDGE."""
    try:
        from PIL import Image  # noqa: F401 — decoded frames are PIL images
    except ImportError as e:
        raise embeddings.ClipUnavailable(f"Pillow not installed: {e}") from e
    out = img.convert("RGB")
    out.thumbnail((FRAME_JPEG_MAX_EDGE, FRAME_JPEG_MAX_EDGE))
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=FRAME_JPEG_QUALITY)
    return buf.getvalue()


def get_frames(
    vault_root: Path,
    path: str,
    *,
    max_frames: int = DEFAULT_TOOL_FRAMES,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> FramesResult:
    """Sample, dedup, bound, and JPEG-encode keyframes for a vault video.

    Known duration → evenly-spaced midpoints over the (clamped) window,
    decoded via the O(1)-seek `embeddings._decode_frames_at`; unknown
    duration → `embeddings._sample_video_keyframes`' sequential fallback
    (unwindowed only). Near-dups collapse via `_dedup_keyframes`; the kept
    frames uniform-subsample down to the effective cap.

    Raises VideoFramesError with code in {INVALID_PATH, NOT_FOUND,
    NOT_A_FILE, NOT_A_VIDEO, VIDEO_DEPS_MISSING, NO_DECODABLE_FRAMES,
    BAD_RANGE}.
    """
    if start_sec is not None and start_sec < 0:
        raise VideoFramesError("BAD_RANGE", "start_sec must be >= 0")
    if end_sec is not None and end_sec <= (start_sec or 0.0):
        raise VideoFramesError("BAD_RANGE", "end_sec must be greater than start_sec")
    try:
        abs_path, rel_path = resolve_under_vault(
            vault_root, path, must_exist=True, must_be_file=True
        )
    except VaultPathError as e:
        raise VideoFramesError(e.code, e.reason) from e
    if media_type_for(abs_path) != "video":
        raise VideoFramesError("NOT_A_VIDEO", f"not a video file: {rel_path}")

    effective = max(1, min(max_frames, _tool_frames_cap()))
    windowed = start_sec is not None or end_sec is not None
    try:
        duration = _probe_duration(abs_path)
        if duration is not None and duration > 0:
            lo = start_sec if start_sec is not None else 0.0
            if lo >= duration:
                raise VideoFramesError(
                    "BAD_RANGE",
                    f"start_sec {lo:g} is past the end of the video ({duration:.1f}s)",
                )
            hi = min(end_sec, duration) if end_sec is not None else duration
            n = CANDIDATE_MULTIPLIER * effective
            ts_list = [lo + (hi - lo) * (k + 0.5) / n for k in range(n)]
            images = embeddings._decode_frames_at(abs_path, ts_list)
            candidates = [
                (t, img) for t, img in zip(ts_list, images, strict=True) if img is not None
            ]
        elif windowed:
            raise VideoFramesError(
                "NO_DECODABLE_FRAMES",
                f"duration of {rel_path} is unknown; windowed sampling is "
                "unavailable — retry without start_sec/end_sec",
            )
        else:
            candidates = embeddings._sample_video_keyframes(abs_path)
    except VideoFramesError:
        raise
    except embeddings.ClipUnavailable as e:
        raise VideoFramesError(
            "VIDEO_DEPS_MISSING", f"{e} — install the media extra to decode video"
        ) from e
    except Exception as e:  # noqa: BLE001 — corrupt/unreadable container
        raise VideoFramesError(
            "NO_DECODABLE_FRAMES", f"decode failed for {rel_path}: {e}"
        ) from e
    if not candidates:
        raise VideoFramesError(
            "NO_DECODABLE_FRAMES", f"no decodable video frames in {rel_path}"
        )

    kept = embeddings._dedup_keyframes(candidates)
    dropped = len(candidates) - len(kept)
    if len(kept) > effective:  # uniform subsample preserving time order
        idx = sorted(set(np.linspace(0, len(kept) - 1, effective).round().astype(int).tolist()))
        kept = [kept[i] for i in idx]
    try:
        frames = [
            Frame(timestamp_sec=round(float(ts), 3), jpeg=_encode_jpeg(img))
            for ts, img in kept
        ]
    except embeddings.ClipUnavailable as e:
        raise VideoFramesError(
            "VIDEO_DEPS_MISSING", f"{e} — install the media extra to encode frames"
        ) from e
    return FramesResult(
        path=rel_path,
        duration_sec=round(duration, 3) if duration is not None else None,
        frames=frames,
        candidates=len(candidates),
        dedup_dropped=dropped,
        max_frames_effective=effective,
    )
