"""One disposable codec invocation. Its only input is a held original on stdin."""

from __future__ import annotations

import os
import signal
import sys
import time
from typing import BinaryIO

MAX_SIDE = 512
# DCI 4K fits. Limit codec pixels before any decoded frame reaches Pillow.
_MAX_VIDEO_PIXELS = 16_000_000


def _video_poster(source: BinaryIO, inline_bytes: int) -> bytes:
    import av

    from .video_frames import _encode_jpeg

    def refuse_secondary_open(*_args):
        raise OSError("preview decoding permits only the original")

    codec_options = {"max_pixels": str(_MAX_VIDEO_PIXELS), "threads": "1"}
    with av.open(
        source,
        options={
            **codec_options,
            "protocol_whitelist": "none",
            "probesize": str(1024 * 1024),
            "analyzeduration": "1000000",
            "max_probe_packets": "64",
        },
        io_open=refuse_secondary_open,
    ) as container:
        if not container.streams.video:
            raise ValueError("no video stream")
        video = container.streams.video[0]
        codec = video.codec_context
        if codec.width * codec.height > _MAX_VIDEO_PIXELS:
            raise ValueError("video exceeds preview pixel limit")
        codec.thread_count = 1
        codec.options = {**codec.options, **codec_options}
        frame = next(container.decode(video))
        if frame.width * frame.height > _MAX_VIDEO_PIXELS:
            raise ValueError("video exceeds preview pixel limit")
        scale = min(1, MAX_SIDE / max(frame.width, frame.height))
        small = frame.reformat(
            width=max(1, int(frame.width * scale)),
            height=max(1, int(frame.height * scale)),
            format="rgb24",
        )
        return _encode_jpeg(small.to_image(), MAX_SIDE, inline_bytes=inline_bytes)


def _limit(kind: int, maximum: int) -> None:
    import resource

    soft, hard = resource.getrlimit(kind)
    limit = min(value for value in (soft, hard, maximum) if value != resource.RLIM_INFINITY)
    resource.setrlimit(kind, (limit, limit))


def main() -> int:
    # This platform must provide both a bounded allocation and a real-time kill.
    if os.name != "posix":
        return 1
    import resource

    kind, suffix, wire_limit, memory_limit, deadline = sys.argv[1:]
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        return 1
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    inline_bytes = int(wire_limit)
    max_bytes = (inline_bytes // 4) * 3
    _limit(resource.RLIMIT_DATA, int(memory_limit))
    _limit(resource.RLIMIT_AS, int(memory_limit))
    _limit(resource.RLIMIT_FSIZE, max_bytes)
    source = sys.stdin.buffer
    if not source.seekable():
        return 1
    source.seek(0)
    if kind == "image":
        from .image_view import render_preview

        jpeg = render_preview(source, suffix, max_side=MAX_SIDE, inline_bytes=inline_bytes)
    else:
        jpeg = _video_poster(source, inline_bytes)
    if not jpeg or len(jpeg) > max_bytes:
        return 1
    sys.stdout.buffer.write(jpeg)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
