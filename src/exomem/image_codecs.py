"""Lazy pixel decoding shared by OCR and image embeddings."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO


def open_image(path: Path | BinaryIO, *, suffix: str | None = None, max_pixels: int | None = None):
    """Open an image with bounded HEIC decoding when that codec is needed.

    `suffix` names the format of bytes read without a filename, such as an open original.
    `max_pixels` refuses a camera raw larger than that before it is demosaiced; without it,
    Pillow's own decompression-bomb limit applies, as it does to every other format.
    """
    try:
        from PIL import Image
    except ImportError as error:
        raise ImportError("Image decoding requires Pillow; install exomem[image-codecs]") from error
    from .media_types import RAW_EXTS

    extension = (suffix if suffix is not None else Path(path).suffix).lower()
    if extension in RAW_EXTS:
        return _open_raw(path, max_pixels)
    # HEIC is the image registry's supported HEIF extension; other formats need only Pillow.
    if extension == ".heic":
        try:
            from pillow_heif import register_heif_opener
        except ImportError as error:
            raise ImportError("HEIC decoding requires pillow-heif; install exomem[image-codecs]") from error
        register_heif_opener(decode_threads=1)
    # Pillow chooses a decoder by sniffing content, and some (EPS) run external programs. Only
    # the decoders of the registry's image extensions may run, whatever the file claims to be.
    return Image.open(path, formats=_image_formats())


def _image_formats() -> list[str]:
    from PIL import Image

    from .media_types import IMAGE_EXTS

    registered = Image.registered_extensions()
    return sorted({registered[ext] for ext in IMAGE_EXTS if ext in registered})


def _open_raw(path: Path | BinaryIO, max_pixels: int | None):
    """Demosaic a camera raw to an upright RGB image at its full size.

    The full sensor image is decoded, not an embedded preview: phone raws often carry none,
    a 12 MP demosaic takes under a second, and the shown size then stays the original's.
    LibRaw applies the camera's orientation, so the result carries no EXIF to turn again.
    """
    from PIL import Image

    try:
        import rawpy
    except ImportError as error:
        raise ImportError("Camera raw decoding requires rawpy; install exomem[image-codecs]") from error
    limit = max_pixels if max_pixels is not None else (
        2 * Image.MAX_IMAGE_PIXELS if Image.MAX_IMAGE_PIXELS else None
    )
    try:
        with rawpy.imread(path if hasattr(path, "read") else str(path)) as raw:
            pixels = raw.sizes.width * raw.sizes.height
            if limit is not None and pixels > limit:
                raise Image.DecompressionBombError(
                    f"camera raw of {pixels} pixels exceeds the limit of {limit} pixels"
                )
            rgb = raw.postprocess(use_camera_wb=True)
    except rawpy.LibRawError as error:
        # Callers treat OSError as an undecodable image, as Pillow raises it.
        raise OSError(f"camera raw could not be decoded: {error}") from error
    return Image.fromarray(rgb)
