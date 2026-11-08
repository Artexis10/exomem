"""Show governed still images to the model as image content.

An agent that can only find an image's metadata cannot inspect the photo. This
module reads each requested original through the same custody and full-disclosure
release checks as an original download (`artifact_recovery.stage`), then returns
a JPEG the model can look at, at the size the caller asks for, up to the original
resolution, optionally cropped to a region. The stored original is never changed.

What a model can take changes with the model, so nothing here fixes how many images
a call shows or how large they are: the caller chooses, and the defaults are
settings. The bounds in code protect the serving process only.
"""

from __future__ import annotations

import io
import math
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import artifact_recovery, image_codecs, media_types, mode, reserved_paths

#: `max_side` value that keeps the original resolution.
ORIGINAL = "original"
# Settings: each is the `EXOMEM_<NAME>` environment value, else the per-machine config key, else
# this default. The long side a call gets when it names none ("original" keeps the original size).
MAX_SIDE_SETTING = "image_view_max_side"
DEFAULT_MAX_SIDE = 1568
# The largest image one inline result may carry. The default is the 5 MB per-image limit of the
# Claude API; a larger view is shrunk until it fits and the reply links the original.
INLINE_BYTES_SETTING = "image_view_inline_max_bytes"
DEFAULT_INLINE_BYTES = 5 * 1000 * 1000
# The encoded images one call holds in memory and returns; images past it are listed, not shown.
CALL_BYTES_SETTING = "image_view_max_call_bytes"
DEFAULT_CALL_BYTES = 48 * 1024 * 1024
_JPEG_QUALITY = 85
# Re-encodes at a smaller size before an oversized view is given up as too large to inline.
_SHRINK_ATTEMPTS = 4
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
class Region:
    """A crop of the upright image, as fractions of its width and height."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class ShownImage:
    path: str
    file_name: str
    original_pixels: tuple[int, int]
    shown_pixels: tuple[int, int]
    taken_at: str | None
    jpeg: bytes
    #: True when the view was shrunk below the requested size to fit the inline limit.
    shrunk_to_fit: bool = False


@dataclass(frozen=True, slots=True)
class Shown:
    images: list[ShownImage]
    #: Requested paths left out because the call's byte limit was reached, in request order.
    not_shown: list[str]


def _setting(name: str, default: int, *, original: bool = False) -> int | None:
    """A positive integer setting, or None for "original" where `original` allows it."""
    raw = os.environ.get(f"EXOMEM_{name.upper()}") or mode.read_config().get(name)
    if original and str(raw).strip().lower() == ORIGINAL:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def resolve_side(max_side: int | str | None) -> int | None:
    """The long side in pixels, or None to keep the original resolution."""
    if max_side is None:
        return _setting(MAX_SIDE_SETTING, DEFAULT_MAX_SIDE, original=True)
    if isinstance(max_side, str) and max_side.strip().lower() == ORIGINAL:
        return None
    if isinstance(max_side, bool) or not isinstance(max_side, int) or max_side < 1:
        raise ImageViewError(
            "IMAGE_MAX_SIDE_INVALID", 'give max_side as a positive number of pixels, or "original"'
        )
    return max_side


def resolve_region(region: Sequence[float] | None) -> Region | None:
    """`[x, y, width, height]` as fractions of the upright image, or None for the whole image."""
    if region is None:
        return None
    try:
        x, y, width, height = (float(value) for value in region)
    except (TypeError, ValueError):
        x = y = width = height = math.nan
    if not (
        0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 - x + 1e-9 and 0 < height <= 1 - y + 1e-9
    ):
        raise ImageViewError(
            "IMAGE_REGION_INVALID",
            "give region as [x, y, width, height], fractions of the upright image from its top-left "
            "corner, with x + width and y + height at most 1",
        )
    return Region(x, y, min(width, 1 - x), min(height, 1 - y))


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


def _render(
    data: bytes, suffix: str, max_side: int | None, region: Region | None, inline_bytes: int
) -> tuple[tuple[int, int], tuple[int, int], str | None, bytes, bool]:
    from PIL import Image, ImageOps

    with image_codecs.open_image(io.BytesIO(data), suffix=suffix) as opened:
        # The stored size, before a reduced-scale decode changes what `size` reports.
        stored = opened.size
        if max_side is not None:
            # JPEG decodes directly at a reduced scale; other formats ignore the draft request.
            # A region keeps its own detail: the decode must hold max_side across the crop.
            need = max_side if region is None else math.ceil(max_side / min(region.width, region.height))
            opened.draft("RGB", (need, need))
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
        if region is not None:
            width, height = image.size
            left, top = round(region.x * width), round(region.y * height)
            image = image.crop((
                min(left, width - 1), min(top, height - 1),
                max(left + 1, round((region.x + region.width) * width)),
                max(top + 1, round((region.y + region.height) * height)),
            ))
        # Shrink before the remaining conversions, so they copy the small image.
        if max_side is not None:
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        image = _rgb(image)
        jpeg = _jpeg(image)
        shrunk = False
        for _ in range(_SHRINK_ATTEMPTS):
            if len(jpeg) <= inline_bytes:
                break
            # JPEG size grows about with the pixel count; aim a little under the limit.
            scale = math.sqrt(inline_bytes / len(jpeg)) * 0.9
            image.thumbnail(
                (max(1, int(image.size[0] * scale)), max(1, int(image.size[1] * scale))),
                Image.Resampling.LANCZOS,
            )
            jpeg, shrunk = _jpeg(image), True
        if len(jpeg) > inline_bytes:
            raise _TooLarge
        return original, image.size, taken_at, jpeg, shrunk


def _jpeg(image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return out.getvalue()


class _TooLarge(Exception):
    pass


def _download_instead(path: str) -> str:
    return f"{path} is too large to show; download the original with transfer_artifact operation=download"


def show(
    vault_root: Path,
    selected: list[str],
    *,
    max_side: int | str | None = None,
    region: Sequence[float] | None = None,
) -> Shown:
    """Return each selected image, in order, or raise for the first one that cannot be shown.

    Images past the call's byte limit are returned in `not_shown`, never dropped silently.
    """
    if not selected:
        raise ImageViewError("MEDIA_PATH_REQUIRED", "give the path of the image to show")
    side = resolve_side(max_side)
    crop = resolve_region(region)
    inline_bytes = _setting(INLINE_BYTES_SETTING, DEFAULT_INLINE_BYTES)
    call_bytes = _setting(CALL_BYTES_SETTING, DEFAULT_CALL_BYTES)
    who = artifact_recovery.delegated_principal()
    shown: list[ShownImage] = []
    held = 0
    for index, path in enumerate(selected):
        if shown and held >= call_bytes:
            return Shown(shown, list(selected[index:]))
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
                original, size, taken_at, jpeg, shrunk = _render(data, suffix, side, crop, inline_bytes)
        except ImportError as error:
            raise ImageViewError("IMAGE_DEPS_MISSING", str(error)) from error
        except (_TooLarge, Image.DecompressionBombError) as error:
            raise ImageViewError("IMAGE_TOO_LARGE", _download_instead(path)) from error
        except (OSError, ValueError) as error:
            raise ImageViewError("IMAGE_UNDECODABLE", f"{path} could not be decoded as an image") from error
        finally:
            del data
        if shown and held + len(jpeg) > call_bytes:
            return Shown(shown, list(selected[index:]))
        held += len(jpeg)
        shown.append(ShownImage(original_path, descriptor["file_name"], original, size, taken_at, jpeg, shrunk))
    return Shown(shown, [])
