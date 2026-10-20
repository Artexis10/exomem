"""Lazy pixel decoding shared by OCR and image embeddings."""

from __future__ import annotations

from pathlib import Path


def open_image(path: Path):
    """Open an image with bounded HEIC decoding when that codec is needed."""
    try:
        from PIL import Image
    except ImportError as error:
        raise ImportError("Image decoding requires Pillow; install exomem[image-codecs]") from error
    # HEIC is the image registry's supported HEIF extension; other formats need only Pillow.
    if path.suffix.lower() == ".heic":
        try:
            from pillow_heif import register_heif_opener
        except ImportError as error:
            raise ImportError("HEIC decoding requires pillow-heif; install exomem[image-codecs]") from error
        register_heif_opener(decode_threads=1)
    return Image.open(path)
