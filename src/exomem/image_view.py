"""Show governed still images to the model as image content.

An agent that can only find an image's metadata cannot inspect the photo. This
module reads each requested original through the same custody and full-disclosure
release checks as an original download (`artifact_recovery.stage`), then returns
a downscaled JPEG the model can look at. The stored original is never changed.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from pathlib import Path

from . import artifact_recovery, image_codecs, media_types, reserved_paths

# One call shows a small comparison set; each image costs the model image tokens.
MAX_IMAGES = 6
# Long side in pixels: 1568 is the size vision models read without further downscaling.
DEFAULT_MAX_SIDE = 1568
MIN_MAX_SIDE = 256
MAX_MAX_SIDE = 2048
_JPEG_QUALITY = 85
# A larger original is read into memory to decode; past this, the person downloads it instead.
MAX_ORIGINAL_BYTES = 128 * 1024 * 1024
# Decoded pixels, after JPEG's reduced-scale decode. 50 MP covers 48 MP phone HEIF photos
# (about 150 MB as RGB in the serving process); a 50 MP camera JPEG decodes at a quarter
# scale, and a 13,000 px PNG is refused.
MAX_DECODE_PIXELS = 50_000_000
# One decode at a time bounds the memory that concurrent image reads can take.
_DECODE = threading.Lock()
# EXIF tags fixed by the EXIF specification, not by any vault vocabulary.
_EXIF_IFD = 0x8769
_DATE_TIME_ORIGINAL = 0x9003
_DATE_TIME = 0x0132
_ORIENTATION = 0x0112
# Orientation values 5 to 8 rotate the image a quarter turn, so width and height swap.
_QUARTER_TURNS = frozenset({5, 6, 7, 8})


class ImageViewError(ValueError):
    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ShownImage:
    path: str
    file_name: str
    original_pixels: tuple[int, int]
    shown_pixels: tuple[int, int]
    taken_at: str | None
    jpeg: bytes


def clamp_side(max_side: int | None) -> int:
    if max_side is None:
        return DEFAULT_MAX_SIDE
    return max(MIN_MAX_SIDE, min(MAX_MAX_SIDE, int(max_side)))


def _taken_at(image) -> str | None:
    """The camera's capture time as EXIF records it (local time, no zone), or None."""
    from datetime import datetime

    try:
        exif = image.getexif()
        value = exif.get_ifd(_EXIF_IFD).get(_DATE_TIME_ORIGINAL) or exif.get(_DATE_TIME)
        # EXIF writes "YYYY:MM:DD HH:MM:SS"; its "unknown" value of blanks and colons fails to parse.
        return datetime.strptime(str(value).strip("\x00 "), "%Y:%m:%d %H:%M:%S").isoformat()
    except Exception:  # noqa: BLE001 -- a missing or malformed EXIF date only loses the optional date.
        return None


def _rgb(image):
    """Convert any decoded mode to RGB for JPEG, flattening transparency onto white."""
    from PIL import Image

    if image.mode == "P" and "transparency" in image.info:
        image = image.convert("RGBA")
    if "A" in image.getbands():
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, (0, 0), rgba.getchannel("A"))
        return background
    if image.mode.startswith("I"):
        # 16-bit and 32-bit integer greyscale: scale to 8 bits, or every pixel clips to white.
        return image.convert("I").point(lambda value: value / 256).convert("L").convert("RGB")
    return image if image.mode == "RGB" else image.convert("RGB")


def _render(data: bytes, suffix: str, max_side: int) -> tuple[tuple[int, int], tuple[int, int], str | None, bytes]:
    from PIL import Image, ImageOps

    with image_codecs.open_image(io.BytesIO(data), suffix=suffix) as opened:
        # The stored size, before a reduced-scale decode changes what `size` reports.
        stored = opened.size
        # JPEG decodes directly at a reduced scale; other formats ignore the draft request.
        opened.draft("RGB", (max_side, max_side))
        # The header names the decode size. Check it before anything that loads pixels:
        # Pillow decodes a whole PNG to read EXIF that follows the image data.
        if opened.size[0] * opened.size[1] > MAX_DECODE_PIXELS:
            raise _TooLarge
        exif = opened.getexif()
        original = stored if exif.get(_ORIENTATION) not in _QUARTER_TURNS else stored[::-1]
        taken_at = _taken_at(opened)
        image = ImageOps.exif_transpose(opened)
        # Pillow shrinks bilevel and palette images by nearest neighbour, which drops thin
        # strokes in scans; give them a mode that LANCZOS resamples.
        if image.mode == "1":
            image = image.convert("L")
        elif image.mode == "P":
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        # Shrink before the remaining conversions, so they copy the small image.
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        image = _rgb(image)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        return original, image.size, taken_at, out.getvalue()


class _TooLarge(Exception):
    pass


def _download_instead(path: str) -> str:
    return f"{path} is too large to show; download the original with transfer_artifact operation=download"


def show(vault_root: Path, selected: list[str], *, max_side: int | None = None) -> list[ShownImage]:
    """Return each selected image, in order, or raise for the first one that cannot be shown."""
    if not selected:
        raise ImageViewError("MEDIA_PATH_REQUIRED", "give the path of the image to show")
    if len(selected) > MAX_IMAGES:
        raise ImageViewError("TOO_MANY_IMAGES", f"show at most {MAX_IMAGES} images per call; split the paths")
    side = clamp_side(max_side)
    who = artifact_recovery.delegated_principal()
    shown = []
    for path in selected:
        # The recovery slot covers the verified read only; downloads share the two slots.
        with artifact_recovery.stage(vault_root, path, who) as (copied, descriptor):
            original_path = descriptor["path"]
            suffix = Path(original_path).suffix.lower()
            kind = media_types.media_type_for(original_path)
            if kind == "video":
                raise ImageViewError("NOT_AN_IMAGE", f"{path} is a video; read it with read_media kind=video")
            if kind != "image":
                raise ImageViewError(
                    "IMAGE_FORMAT_UNSUPPORTED",
                    f"{path} ({suffix or 'no extension'}) cannot be shown as an image; "
                    "download the original with transfer_artifact operation=download",
                )
            if copied.size > MAX_ORIGINAL_BYTES:
                raise ImageViewError("IMAGE_TOO_LARGE", _download_instead(path))
            # `stage` hands over a held handle; read_held refuses bytes read across a change.
            data = reserved_paths.read_held(copied, 0, copied.size)
        from PIL import Image

        try:
            with _DECODE:
                original, size, taken_at, jpeg = _render(data, suffix, side)
        except ImportError as error:
            raise ImageViewError("IMAGE_DEPS_MISSING", str(error)) from error
        except (_TooLarge, Image.DecompressionBombError) as error:
            raise ImageViewError("IMAGE_TOO_LARGE", _download_instead(path)) from error
        except (OSError, ValueError) as error:
            raise ImageViewError("IMAGE_UNDECODABLE", f"{path} could not be decoded as an image") from error
        finally:
            del data
        shown.append(ShownImage(original_path, descriptor["file_name"], original, size, taken_at, jpeg))
    return shown
