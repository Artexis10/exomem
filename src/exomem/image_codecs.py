"""Lazy pixel decoding shared by OCR and image embeddings."""

from __future__ import annotations

import platform
import struct
import sys
from pathlib import Path
from typing import BinaryIO

# TIFF tags the DNG and EXIF specifications fix.
_DNG_VERSION = 50706
_DATE_TIME = 0x0132


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


def decoder_present(suffix: str) -> bool:
    """Whether this install can decode an image with this extension. Imports nothing.

    Mirrors the optional codecs `open_image` imports, so a job parked for a missing codec
    returns only once that codec is installed.
    """
    from importlib.util import find_spec

    from .media_types import RAW_EXTS

    if find_spec("PIL") is None:
        return False
    extension = suffix.lower()
    if extension in RAW_EXTS:
        return find_spec("rawpy") is not None
    if extension == ".heic":
        return find_spec("pillow_heif") is not None
    return True


def _image_formats() -> list[str]:
    from PIL import Image

    from .media_types import IMAGE_EXTS

    registered = Image.registered_extensions()
    return sorted({registered[ext] for ext in IMAGE_EXTS if ext in registered})


def _open_raw(path: Path | BinaryIO, max_pixels: int | None):
    """Demosaic a DNG to an upright image at its full size, or refuse it before LibRaw decodes.

    The full sensor image is decoded, not an embedded preview: phone raws often carry none,
    a 12 MP demosaic takes about a second, and the shown size then stays the original's.
    LibRaw applies the camera's orientation, so the result carries no orientation to apply again.
    """
    from PIL import Image

    try:
        import rawpy
    except ImportError as error:
        if sys.platform == "darwin" and platform.machine() == "x86_64":
            raise ImportError(
                "Camera raw decoding needs rawpy, which publishes no build for Intel macOS; "
                "download the original with transfer_artifact operation=download"
            ) from error
        raise ImportError("Camera raw decoding requires rawpy; install exomem[image-codecs]") from error
    # LibRaw chooses its parser from the bytes, and also decodes headerless sensor dumps by their
    # length. Only a DNG, which a TIFF directory declares, may reach it.
    if not _is_dng(path):
        raise OSError("the file is not a DNG camera raw")
    limit = max_pixels if max_pixels is not None else (
        2 * Image.MAX_IMAGE_PIXELS if Image.MAX_IMAGE_PIXELS else None
    )
    memory = (rawpy.LibRawMemPoolOverflowError, rawpy.LibRawUnsufficientMemoryError)
    try:
        with rawpy.imread(path if hasattr(path, "read") else str(path)) as raw:
            sizes = raw.sizes
            # The output is stretched by the pixel aspect ratio, and the unpacked sensor buffer
            # can exceed the visible area; whichever is larger is what decoding costs.
            aspect = sizes.pixel_aspect if sizes.pixel_aspect > 0 else 1.0
            cost = max(sizes.width * sizes.height * max(aspect, 1 / aspect), sizes.raw_width * sizes.raw_height)
            if limit is not None and cost > limit:
                raise Image.DecompressionBombError(
                    f"camera raw of {int(cost)} decoded pixels exceeds the limit of {limit} pixels"
                )
            rgb = raw.postprocess(use_camera_wb=True)
            try:
                taken = raw.other.timestamp
            except (ValueError, OverflowError, OSError):
                taken = None  # a malformed EXIF date loses only the date, as it does for a JPEG
    except rawpy.LibRawTooBigError as error:
        raise Image.DecompressionBombError("camera raw is too large to decode") from error
    except memory as error:
        # The media worker stops on memory exhaustion instead of failing the artifact.
        raise MemoryError("camera raw decoding ran out of memory") from error
    except rawpy.LibRawError as error:
        # Callers treat OSError as an undecodable image, as Pillow raises it.
        raise OSError(f"camera raw could not be decoded ({type(error).__name__})") from error
    if rgb.ndim == 3 and rgb.shape[2] == 1:
        rgb = rgb[:, :, 0]  # a monochrome raw decodes to one channel
    image = Image.fromarray(rgb)
    # LibRaw reads the capture time; it travels as EXIF DateTime, with no orientation tag.
    # A raw that records none has timestamp 0.
    if taken is not None and taken.timestamp() > 0:
        exif = Image.Exif()
        exif[_DATE_TIME] = taken.strftime("%Y:%m:%d %H:%M:%S")
        image.info["exif"] = exif.tobytes()
    return image


def _is_dng(path: Path | BinaryIO) -> bool:
    """Whether the bytes are a TIFF whose first directory declares a DNG version."""
    from PIL.TiffImagePlugin import ImageFileDirectory_v2

    def declares_dng(stream: BinaryIO) -> bool:
        try:
            directory = ImageFileDirectory_v2(stream.read(8))
            stream.seek(directory.next)
            directory.load(stream)
        except (OSError, SyntaxError, ValueError, struct.error):
            return False
        return _DNG_VERSION in directory

    if hasattr(path, "read"):
        start = path.tell()
        try:
            return declares_dng(path)
        finally:
            path.seek(start)
    with open(path, "rb") as stream:
        return declares_dng(stream)
