"""Average hashes of images: a small luminance fingerprint that survives a resize or a re-encode.

Two users share it: the video keyframe dedup (`embeddings`) and the capture-date recovery that
matches an EXIF-stripped copy to its original (`capture_match`).
"""

from __future__ import annotations

import numpy as np


def hash_bits(arr: np.ndarray) -> int:
    """Pack a small grayscale array into an average-hash int: bit per pixel vs mean.

    Shared core between the PIL path (`average_hash`) and the ndarray path used by the
    scene-detection metrics pass, so both produce comparable hashes.
    """
    flat = np.asarray(arr, dtype=np.float32).ravel()
    bits = flat >= flat.mean()
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return val


def average_hash(img, *, size: int = 8, resample=None) -> int:
    """`size`*`size`-bit perceptual average-hash: downscale, grayscale, bit per pixel vs mean.

    Keys on luminance *structure*, so it distinguishes textured frames (faces, slides,
    whiteboards, what real recordings contain) but is blind to two flat frames of
    differing uniform colour (both hash to all-ones). Dedup is best-effort; a
    pathologically flat video simply keeps fewer keyframes. `resample` is the Pillow filter
    for the downscale; the default is Pillow's own.
    """
    gray = img.convert("L")
    small = gray.resize((size, size)) if resample is None else gray.resize((size, size), resample)
    return hash_bits(np.asarray(small, dtype=np.float32))


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()
