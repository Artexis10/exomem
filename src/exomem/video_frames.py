"""On-demand video keyframe retrieval — backend for the `get_video_frames` MCP tool.

Composes the decode primitives that already live in `embeddings` (PyAV seek
decode, perceptual-hash near-dup suppression) into a bounded, vault-confined,
soft-failing sampler that returns JPEG bytes + timestamps. Pure decode and
transcode — no model runs here. The frames come back INLINE in the tool result
so the client never needs an HTTP fetch (openspec capability: video-frames).
"""

from __future__ import annotations

import dataclasses
import io
from pathlib import Path
from typing import BinaryIO

import numpy as np

from . import access, embeddings, mode, reserved_paths
from .extract import media_type_for
from .vault import VaultPathError, resolve_under_vault

DEFAULT_TOOL_FRAMES = 8
# Settings, read like image view's (`EXOMEM_<NAME>`, else the machine's config key, else the
# default): the frames one call may return, and the long side a frame gets when the call names
# none ("original" keeps the video's own size). What a model can take changes with the model.
FRAMES_CAP_SETTING = "video_frames_tool_cap"
DEFAULT_FRAMES_CAP = 16
MAX_SIDE_SETTING = "video_frames_max_side"
DEFAULT_MAX_SIDE = 768
# The Cloud frames worker sizes its allocation ceiling and memory admission from this edge, so
# Cloud frames keep it whatever the call asks and report it as the effective side.
CLOUD_MAX_SIDE = 768
CANDIDATE_MULTIPLIER = 2  # sample 2x the requested frames so dedup has headroom
FRAME_JPEG_QUALITY = 80


class VideoFramesError(Exception):
    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(reason)


@dataclasses.dataclass(frozen=True)
class Frame:
    timestamp_sec: float
    jpeg: bytes


@dataclasses.dataclass(frozen=True)
class FramesResult:
    path: str  # resolved vault-relative posix path
    duration_sec: float | None
    frames: list[Frame]
    candidates: int  # sampled before dedup/cap
    dedup_dropped: int
    max_frames_effective: int
    max_side_effective: int | None  # None: the video's own size
    # Timestamps of frames left out because the call reached its byte limit.
    not_shown: tuple[float, ...] = ()


def _tool_frames_cap() -> int:
    return mode.int_setting(FRAMES_CAP_SETTING, DEFAULT_FRAMES_CAP)


def resolve_side(max_side: int | str | None) -> int | None:
    """The frame long side in pixels, or None for the video's own size."""
    from . import image_view

    try:
        return image_view.resolve_side(max_side, setting=MAX_SIDE_SETTING, default=DEFAULT_MAX_SIDE)
    except image_view.ImageViewError as error:
        raise VideoFramesError(error.code, error.reason) from error


def _probe_duration(source: Path | BinaryIO) -> float | None:
    """Container-metadata duration in seconds, or None when unknown. No decode."""
    try:
        import av
    except ImportError as e:
        raise embeddings.ClipUnavailable(f"PyAV not installed: {e}") from e
    av_source = source if hasattr(source, "read") else str(source)
    with av.open(av_source) as container:
        if not container.streams.video:
            return None
        stream = container.streams.video[0]
        if stream.duration and stream.time_base:
            return float(stream.duration * stream.time_base)
        if container.duration:
            return container.duration / av.time_base
    return None


def _encode_jpeg(img, max_side: int | None) -> bytes:
    """JPEG encode of a PIL image, its longest side at most `max_side` (None keeps its size)."""
    try:
        from PIL import Image  # noqa: F401 — decoded frames are PIL images
    except ImportError as e:
        raise embeddings.ClipUnavailable(f"Pillow not installed: {e}") from e
    out = img.convert("RGB")
    if max_side is not None:
        out.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=FRAME_JPEG_QUALITY)
    return buf.getvalue()


def get_frames(
    vault_root: Path, path: str, *, max_frames: int = DEFAULT_TOOL_FRAMES,
    start_sec: float | None = None, end_sec: float | None = None,
    max_side: int | str | None = None,
) -> FramesResult:
    from . import cloud_cell, originals

    side = resolve_side(max_side)  # refused alike on Cloud, which then keeps its own edge
    if cloud_cell.cloud_mode_enabled():
        from .video_frames_worker import get_frames as cloud_frames

        return _within_call_bytes(cloud_frames(vault_root, path, max_frames=max_frames,
                                               start_sec=start_sec, end_sec=end_sec))
    from .media_store import open_original

    if originals.binding_for(vault_root) is None:
        return _within_call_bytes(_get_frames_local(vault_root, path, max_frames=max_frames,
                                                    start_sec=start_sec, end_sec=end_sec, max_side=side))
    try:
        target, relative = resolve_under_vault(vault_root, path)
        if access.refuse_if_excluded(vault_root, relative):
            raise VideoFramesError("NOT_FOUND", "video does not exist")
        with open_original(vault_root, target):
            return _within_call_bytes(_get_frames_local(vault_root, path, max_frames=max_frames,
                                                        start_sec=start_sec, end_sec=end_sec, max_side=side))
    except VaultPathError as error:
        raise VideoFramesError(error.code, error.reason) from error
    except reserved_paths.ReservedPathLeafError as error:
        raise VideoFramesError("NOT_FOUND", "video does not exist") from error


def _get_frames_local(
    vault_root: Path,
    path: str,
    *,
    max_frames: int = DEFAULT_TOOL_FRAMES,
    start_sec: float | None = None,
    end_sec: float | None = None,
    max_side: int | None = DEFAULT_MAX_SIDE,
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
    # `excluded` paths (_access.yaml) refuse identically to a missing path —
    # checked before any other differentiation (e.g. NOT_A_VIDEO) so the
    # refusal never leaks that the excluded path exists.
    if access.refuse_if_excluded(vault_root, rel_path):
        raise VideoFramesError("NOT_FOUND", f"path does not exist: {rel_path}")
    try:
        snapshot = reserved_paths.read_generic_bytes(vault_root, rel_path)
    except reserved_paths.ReservedPathLeafError:
        raise VideoFramesError(
            "NOT_FOUND", f"path does not exist: {rel_path}"
        ) from None
    if media_type_for(abs_path) != "video":
        raise VideoFramesError("NOT_A_VIDEO", f"not a video file: {rel_path}")

    return sample_stream(io.BytesIO(snapshot.data), rel_path, max_frames=max_frames,
                         start_sec=start_sec, end_sec=end_sec, max_side=max_side)


def sample_stream(
    source: BinaryIO, rel_path: str, *, max_frames: int = DEFAULT_TOOL_FRAMES,
    start_sec: float | None = None, end_sec: float | None = None,
    resize_candidates: bool = False, max_side: int | None = DEFAULT_MAX_SIDE,
) -> FramesResult:
    """Sample one retained seekable stream; the caller owns custody and disclosure.

    `resize_candidates` shrinks each candidate to `max_side` as it decodes, so it needs a size.
    """
    if start_sec is not None and start_sec < 0:
        raise VideoFramesError("BAD_RANGE", "start_sec must be >= 0")
    if end_sec is not None and end_sec <= (start_sec or 0.0):
        raise VideoFramesError("BAD_RANGE", "end_sec must be greater than start_sec")
    effective = max(1, min(max_frames, _tool_frames_cap()))
    windowed = start_sec is not None or end_sec is not None
    hashes = [] if resize_candidates else None
    options = {"max_edge": max_side, "hashes": hashes} if resize_candidates else {}
    try:
        duration = _probe_duration(source)
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
            source.seek(0)
            images = embeddings._decode_frames_at(source, ts_list, **options)
            candidates = [
                (t, img) for t, img in zip(ts_list, images, strict=True) if img is not None
            ]
            if hashes is not None:
                hashes = [value for image, value in zip(images, hashes, strict=True) if image is not None]
        elif windowed:
            raise VideoFramesError(
                "NO_DECODABLE_FRAMES",
                f"duration of {rel_path} is unknown; windowed sampling is "
                "unavailable — retry without start_sec/end_sec",
            )
        else:
            source.seek(0)
            candidates = embeddings._sample_video_keyframes(source, **options)
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

    kept = (embeddings._dedup_keyframes(candidates, hashes=hashes) if hashes is not None
            else embeddings._dedup_keyframes(candidates))
    dropped = len(candidates) - len(kept)
    if len(kept) > effective:  # uniform subsample preserving time order
        idx = sorted(set(np.linspace(0, len(kept) - 1, effective).round().astype(int).tolist()))
        kept = [kept[i] for i in idx]
    try:
        frames = [Frame(timestamp_sec=round(float(ts), 3), jpeg=_encode_jpeg(img, max_side)) for ts, img in kept]
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
        max_side_effective=max_side,
    )


def _within_call_bytes(result: FramesResult) -> FramesResult:
    """`result` cut to the frames that fit image view's inline and per-call byte settings.

    Applied once, in the process that answers the call, after the local or Cloud read. Frames keep
    time order; once one does not fit, it and every later frame are listed in `not_shown`, so the
    shown frames are the start of the sample and a later window can ask for the rest. When not
    even the first frame fits, the call is refused with the size to lower, never answered empty.
    """
    from . import image_view

    inline = mode.int_setting(image_view.INLINE_BYTES_SETTING, image_view.DEFAULT_INLINE_BYTES)
    budget = mode.int_setting(image_view.CALL_BYTES_SETTING, image_view.DEFAULT_CALL_BYTES)
    used = 0
    for index, frame in enumerate(result.frames):
        size = image_view.wire_bytes(frame.jpeg)
        if size > inline or used + size > budget:
            if index == 0:
                raise VideoFramesError(
                    "FRAME_TOO_LARGE",
                    "one frame at this size is larger than a call can carry; ask for a smaller max_side, "
                    "or raise the image_view_max_call_bytes setting",
                )
            return dataclasses.replace(result, frames=list(result.frames[:index]),
                                       not_shown=tuple(left.timestamp_sec for left in result.frames[index:]))
        used += size
    return result
