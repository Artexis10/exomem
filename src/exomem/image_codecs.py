"""Lazy pixel decoding shared by OCR and image embeddings."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO


def open_image(path: Path | BinaryIO, *, suffix: str | None = None):
    """Open an image with bounded HEIC decoding when that codec is needed.

    `suffix` names the format of bytes read without a filename, such as an open original.
    """
    try:
        from PIL import Image
    except ImportError as error:
        raise ImportError("Image decoding requires Pillow; install exomem[image-codecs]") from error
    # HEIC is the image registry's supported HEIF extension; other formats need only Pillow.
    if (suffix if suffix is not None else Path(path).suffix).lower() == ".heic":
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
