"""What a photo or video says about its own capture: when, where, with which camera and at what size.

One reader serves new extractions and the owner backfill. It reads only what the file states: a
value the file does not state is absent, never zero, empty or a default. Every value comes from
untrusted bytes, so each is bounded and a malformed block loses only its own fields.

The sidecar records the result as frontmatter fields (`FIELDS`). The position fields
(`LOCATION_FIELDS`) are classified here once; `without_position` is the only place that drops them.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from . import capture_dates

log = logging.getLogger(__name__)

# The sidecar's protocol field names that this module defines, not a vault vocabulary.
CAPTURE_TIME = "capture_time"
CAPTURE_DATE = "capture_date"
CAPTURE_DATE_BASIS = "capture_date_basis"
CAPTURE_DATE_SOURCE = "capture_date_source"
CAPTURE_DATE_MATCHED_FROM = "capture_date_matched_from"
CAPTURE_LAT = "capture_lat"
CAPTURE_LON = "capture_lon"
CAMERA_MAKE = "camera_make"
CAMERA_MODEL = "camera_model"
PIXEL_WIDTH = "pixel_width"
PIXEL_HEIGHT = "pixel_height"
FIELDS: tuple[str, ...] = (  # nosemgrep: ep-word-set -- sidecar protocol fields, not vault vocabulary
    CAPTURE_TIME, CAPTURE_DATE, CAPTURE_DATE_BASIS, CAPTURE_DATE_SOURCE, CAPTURE_DATE_MATCHED_FROM,
    CAPTURE_LAT, CAPTURE_LON, CAMERA_MAKE, CAMERA_MODEL, PIXEL_WIDTH, PIXEL_HEIGHT,
)
#: The fields that place the original. A caller who may not download the original never receives them.
LOCATION_FIELDS: frozenset[str] = frozenset({CAPTURE_LAT, CAPTURE_LON})  # nosemgrep: ep-word-set -- sidecar protocol fields

#: A video container that records only a UTC creation time gives a UTC date, not a local one.
BASIS_UTC = "utc"

#: The extraction kinds that `media_types` assigns and whose files can state a capture.
KINDS: frozenset[str] = frozenset({"image", "video"})  # nosemgrep: ep-word-set -- closed extraction kinds of the media registry

TEXT_LIMIT = 128
#: A matched original is named by a page id or an archive path, which can be longer than camera text.
MATCHED_FROM_LIMIT = 512
SIDE_LIMIT = 1_000_000
_LAT_LIMIT, _LON_LIMIT = 90.0, 180.0

# The EXIF and GPS tags that the EXIF specification fixes.
_MAKE, _MODEL = 0x010F, 0x0110
_EXIF_IFD, _GPS_IFD = 0x8769, 0x8825
_DATE_TIME_ORIGINAL, _OFFSET_TIME_ORIGINAL = 0x9003, 0x9011
_GPS_LAT_REF, _GPS_LAT, _GPS_LON_REF, _GPS_LON, _GPS_STATUS = 1, 2, 3, 4, 9

_EXIF_TIME = "%Y:%m:%d %H:%M:%S"
# A field's format, not its meaning: an ISO local time with an optional UTC offset.
_TIME_FORMAT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?")
_EXIF_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # the C0 and C1 control characters


@dataclass(frozen=True, slots=True)
class CaptureFacts:
    """The capture metadata a file states. A field the file does not state stays `None`."""

    time: str | None = None
    date: str | None = None
    date_basis: str | None = None
    #: A key of the `capture-date-sources` pack: how the date was found. `None` for a sidecar written before sources were recorded.
    date_source: str | None = None
    #: Where a matched original lives: a page id, or an archive path relative to the owner's originals root.
    date_matched_from: str | None = None
    lat: float | None = None
    lon: float | None = None
    make: str | None = None
    model: str | None = None
    width: int | None = None
    height: int | None = None

    def fields(self) -> dict[str, str | int | float]:
        """The stated values by sidecar field name, in `FIELDS` order."""
        values = {
            CAPTURE_TIME: self.time, CAPTURE_DATE: self.date, CAPTURE_DATE_BASIS: self.date_basis,
            CAPTURE_DATE_SOURCE: self.date_source, CAPTURE_DATE_MATCHED_FROM: self.date_matched_from,
            CAPTURE_LAT: self.lat, CAPTURE_LON: self.lon, CAMERA_MAKE: self.make,
            CAMERA_MODEL: self.model, PIXEL_WIDTH: self.width, PIXEL_HEIGHT: self.height,
        }
        return {name: values[name] for name in FIELDS if values[name] is not None}  # type: ignore[misc]

    def frontmatter(self) -> dict[str, str]:
        """The stated values as frontmatter text that YAML reads back as the same values.

        A string is always quoted, in ASCII only, so no character of it can end the line or break the
        parse. A coordinate is written in fixed-point: Python's `5e-05` reads back as text in YAML.
        """
        return {name: _render(value) for name, value in self.fields().items()}

    def as_payload(self) -> dict[str, str | int | float]:
        return self.fields()

    @classmethod
    def from_payload(cls, value: object) -> CaptureFacts:
        """Rebuild facts from a job result; a value that is not a bounded stated value is a corrupt result."""
        if not isinstance(value, dict) or set(value) - set(FIELDS):
            raise ValueError("capture facts must be a mapping of the known fields")
        facts = _facts(
            time=value.get(CAPTURE_TIME), date=value.get(CAPTURE_DATE), basis=value.get(CAPTURE_DATE_BASIS),
            source=value.get(CAPTURE_DATE_SOURCE), matched_from=value.get(CAPTURE_DATE_MATCHED_FROM),
            lat=value.get(CAPTURE_LAT), lon=value.get(CAPTURE_LON), make=value.get(CAMERA_MAKE),
            model=value.get(CAMERA_MODEL), width=value.get(PIXEL_WIDTH), height=value.get(PIXEL_HEIGHT),
        )
        if facts.fields() != value:
            raise ValueError("capture facts hold a value that is out of bounds")
        return facts

    def __bool__(self) -> bool:
        return bool(self.fields())


def _render(value: str | int | float) -> str:
    if isinstance(value, str):
        # YAML's own emitter, double-quoted and ASCII-only: JSON's `\ud83d\ude00` pairs break YAML for a character beyond the BMP.
        return yaml.safe_dump(value, default_style='"', allow_unicode=False, width=sys.maxsize).rstrip("\n")
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0")  # `_number` rounds a coordinate to six places
        return text + "0" if text.endswith(".") else text
    return str(value)


def recorded(frontmatter: Mapping[str, Any]) -> CaptureFacts:
    """The capture facts a sidecar's frontmatter records, bounded as a fresh read is."""
    return _facts(
        time=frontmatter.get(CAPTURE_TIME), date=frontmatter.get(CAPTURE_DATE),
        basis=frontmatter.get(CAPTURE_DATE_BASIS), source=frontmatter.get(CAPTURE_DATE_SOURCE),
        matched_from=frontmatter.get(CAPTURE_DATE_MATCHED_FROM),
        lat=frontmatter.get(CAPTURE_LAT), lon=frontmatter.get(CAPTURE_LON),
        make=frontmatter.get(CAMERA_MAKE), model=frontmatter.get(CAMERA_MODEL),
        width=frontmatter.get(PIXEL_WIDTH), height=frontmatter.get(PIXEL_HEIGHT),
    )


def without_position(frontmatter: Mapping[str, Any]) -> dict[str, Any]:
    """`frontmatter` with the position fields removed, as a page without a position reads."""
    return {key: value for key, value in frontmatter.items() if key not in LOCATION_FIELDS}


def carries_position(frontmatter: object) -> bool:
    """Whether `frontmatter` holds a position field, valid or not: a withheld caller sees neither."""
    return isinstance(frontmatter, Mapping) and not LOCATION_FIELDS.isdisjoint(frontmatter)


def coordinate(field: str, value: object) -> float | None:
    """`value` as the stored latitude or longitude it names, or None when the page states no such position.

    The filter view and the catalogue columns both read a position through this, so the SQL narrowing
    and the in-memory evaluation settle on the same set. A bool, a non-finite number or a value outside
    the field's range is not a stated position.
    """
    limit = _LAT_LIMIT if field == CAPTURE_LAT else _LON_LIMIT
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
        return None
    return float(value)


def available(media_type: str, original: Path | str) -> bool:
    """Whether this host has the library that decodes `original`, a file of kind `media_type`.

    `read` answers "states nothing" for a decoder that is missing as it does for a file with no
    metadata, which suits an extraction that goes on. A backfill asks first, so that it never reads
    a missing decoder as a library of photos that state nothing. A camera raw needs `rawpy` as well.
    """
    from .media_types import RAW_EXTS

    needed = ["av"] if media_type == "video" else ["PIL"]
    if media_type == "image" and Path(original).suffix.lower() in RAW_EXTS:
        needed.append("rawpy")
    return all(importlib.util.find_spec(library) is not None for library in needed)


def read(path: Path, media_type: str) -> CaptureFacts | None:
    """What the file at `path` states about its capture, or None when it states nothing.

    Never raises for a malformed file: a decoder that fails on hostile bytes means the file states
    nothing, and extraction goes on. Only an image or a video is read.
    """
    try:
        if media_type == "image":
            facts = _read_image(Path(path))
        elif media_type == "video":
            facts = _read_video(Path(path))
        else:
            return None
    except MemoryError:
        raise  # the media worker stops on memory exhaustion instead of recording a gap
    except Exception:  # noqa: BLE001 - untrusted bytes: any decoder failure means "states nothing"
        log.debug("capture metadata unreadable for %s", Path(path).name, exc_info=True)
        return None
    if facts.time:
        # What the file itself states is camera evidence; the key is the pack's, not a literal here.
        facts = replace(facts, date_source=capture_dates.source_for("file-metadata"))
    return facts or None


def with_name_date(
    vault_root: Path, facts: CaptureFacts | None, media_type: str, name: object,
) -> CaptureFacts | None:
    """`facts`, plus the capture time that the original's file name states when the file states none.

    The name is the one the client sent, recorded on the sidecar; a time the file itself states always
    wins, and a file time (mtime) is never a capture time. Only an image or a video is read.
    """
    if media_type not in KINDS or (facts is not None and facts.time):
        return facts
    stated = capture_dates.from_file_name(vault_root, name)
    if stated is None:
        return facts
    return replace(
        facts or CaptureFacts(), time=stated.time, date=stated.date,
        date_source=capture_dates.source_for("file-name"),
    )


#: The formats a camera writes an EXIF block into. A screenshot, a diagram or an export in another
#: format never had one, so its missing camera metadata is no loss to warn about. A closed set that
#: the formats' own specifications fix (file extensions), not vocabulary. When it fires wrongly, a photo
#: that a client re-encoded to another format (PNG, WebP) gets no notice and the user is not told to resend it.
_CAMERA_PHOTO_EXTS = frozenset({".jpg", ".jpeg", ".heic"})


def lacks_camera_metadata(vault_root: Path, path: Path, name: object) -> bool:
    """Whether the camera photo at `path` decodes yet states no capture time, position or camera, and `name` states no time.

    False for any other format, and whenever the file cannot be judged (no decoder, a corrupt file):
    an unknown never reads as a gap. The name is read before the file is decoded.
    """
    if not isinstance(name, str) or Path(name).suffix.lower() not in _CAMERA_PHOTO_EXTS:
        return False
    if capture_dates.from_file_name(vault_root, name) is not None or not available("image", path):
        return False
    facts = read(path, "image")
    if facts is None:
        return False
    return not (facts.time or facts.lat is not None or facts.make or facts.model)


# ---------------------------------------------------------------- images


def _read_image(path: Path) -> CaptureFacts:
    from . import image_codecs

    raw = image_codecs.read_raw_facts(path)
    if raw is not None:
        # A raw has no EXIF block that Pillow reads; LibRaw's capture timestamp is its capture time.
        taken, width, height = raw
        return _facts(
            time=taken.strftime("%Y-%m-%dT%H:%M:%S") if taken is not None else None,
            date=taken.strftime("%Y-%m-%d") if taken is not None else None,
            width=width, height=height,
        )
    with image_codecs.open_image(path) as image:
        width, height = image.size
        exif = image.getexif()
        taken = _guarded(_original_time, exif)
        position = _guarded(_position, exif)
        make, model = _guarded(_camera, exif) or (None, None)
    return _facts(
        time=taken[0] if taken else None, date=taken[1] if taken else None,
        lat=position[0] if position else None, lon=position[1] if position else None,
        make=make, model=model, width=width, height=height,
    )


def _guarded(read_block: Callable[[Any], Any], exif: Any) -> Any:
    """One EXIF block's values, or None: a malformed block loses only its own fields."""
    try:
        return read_block(exif)
    except Exception:  # noqa: BLE001 - untrusted EXIF; the other blocks are still read
        return None


def _original_time(exif: Any) -> tuple[str, str] | None:
    """The local capture time and date from DateTimeOriginal, never from the file's modification DateTime."""
    stated = exif.get_ifd(_EXIF_IFD).get(_DATE_TIME_ORIGINAL)
    if not isinstance(stated, str):
        return None
    local = datetime.strptime(stated.strip("\x00 "), _EXIF_TIME)
    text = local.strftime("%Y-%m-%dT%H:%M:%S")
    offset = exif.get_ifd(_EXIF_IFD).get(_OFFSET_TIME_ORIGINAL)
    parsed = _EXIF_OFFSET.fullmatch(offset.strip("\x00 ")) if isinstance(offset, str) else None
    if parsed is not None:
        hours, minutes = int(parsed.group(2)), int(parsed.group(3))
        if hours <= 14 and minutes < 60:
            text += f"{parsed.group(1)}{hours:02d}:{minutes:02d}"
    return text, text[:10]


def _position(exif: Any) -> tuple[float, float] | None:
    """The GPS position, or None for a void fix or a block that does not state both coordinates."""
    gps = exif.get_ifd(_GPS_IFD)
    status = gps.get(_GPS_STATUS)
    if isinstance(status, str) and status.strip("\x00 ").upper().startswith("V"):
        return None  # the receiver had no fix; the coordinates are not a position
    lat = _coordinate(gps.get(_GPS_LAT), gps.get(_GPS_LAT_REF), ("N", "S"), 90.0)
    lon = _coordinate(gps.get(_GPS_LON), gps.get(_GPS_LON_REF), ("E", "W"), 180.0)
    return None if lat is None or lon is None else (lat, lon)


def _coordinate(value: Any, ref: Any, signs: tuple[str, str], limit: float) -> float | None:
    if not isinstance(ref, str) or ref.strip("\x00 ").upper() not in signs:
        return None
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        return None
    degrees, minutes, seconds = (float(part) for part in value)
    if not all(math.isfinite(part) and part >= 0 for part in (degrees, minutes, seconds)):
        return None
    if minutes >= 60 or seconds >= 60:
        return None
    magnitude = degrees + minutes / 60 + seconds / 3600
    if magnitude > limit:
        return None
    return -magnitude if ref.strip("\x00 ").upper() == signs[1] else magnitude


def _camera(exif: Any) -> tuple[str | None, str | None]:
    return _text(_utf8_first(exif.get(_MAKE))), _text(_utf8_first(exif.get(_MODEL)))


def _utf8_first(value: object) -> object:
    """EXIF text as the camera wrote it. Pillow decodes it as latin-1, but cameras write UTF-8: undo that, and keep the latin-1 text when the bytes are not UTF-8."""
    if not isinstance(value, str):
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value


# ---------------------------------------------------------------- video


def _read_video(path: Path) -> CaptureFacts:
    import av

    with av.open(str(path)) as container:
        metadata = dict(container.metadata)
        stream = next((s for s in container.streams if s.type == "video"), None)
        width = height = None
        if stream is not None:
            width, height = stream.codec_context.width, stream.codec_context.height
    time, date, basis = None, None, None
    local = _quicktime_local(metadata.get("com.apple.quicktime.creationdate"))
    if local is not None:
        time, date = local, local[:10]
    else:
        utc = _utc_creation(metadata.get("creation_time"))
        if utc is not None:
            time, date, basis = utc, utc[:10], BASIS_UTC
    return _facts(time=time, date=date, basis=basis, width=width or None, height=height or None)


def _quicktime_local(value: object) -> str | None:
    """The local creation date that QuickTime records with its offset, as ISO text."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.utcoffset() is None:
        return moment.strftime("%Y-%m-%dT%H:%M:%S")
    offset = moment.utcoffset()
    assert offset is not None
    if abs(offset) > timedelta(hours=14):
        return None
    return moment.astimezone(timezone(offset)).isoformat(timespec="seconds")


def _utc_creation(value: object) -> str | None:
    """The container's UTC creation time as ISO text with `Z`."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None  # the container gives no zone, so it is not a UTC instant
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- bounds


def _facts(
    *, time: object = None, date: object = None, basis: object = None, source: object = None,
    matched_from: object = None, lat: object = None, lon: object = None, make: object = None,
    model: object = None, width: object = None, height: object = None,
) -> CaptureFacts:
    """The facts whose values stay in bounds; a value out of bounds is dropped, not repaired."""
    stated_time = time if _is_time(time) else None
    latitude, longitude = _number(lat, 90.0), _number(lon, 180.0)
    # A source qualifies a stated date. Only an evidence key that the shipped pack names is stored: a
    # fallback date is derived at read time and never written.
    stated_source = source if stated_time and capture_dates.is_evidence(source) else None
    if latitude is None or longitude is None:
        latitude = longitude = None  # half a position places nothing
    return CaptureFacts(
        time=stated_time,
        date=date if stated_time and date == stated_time[:10] else None,
        date_basis=basis if stated_time and basis == BASIS_UTC else None,
        date_source=stated_source,
        date_matched_from=_text(matched_from, MATCHED_FROM_LIMIT) if stated_source else None,
        lat=latitude, lon=longitude,
        make=_text(make), model=_text(model),
        width=_side(width), height=_side(height),
    )


def _is_time(value: object) -> bool:
    if not isinstance(value, str) or _TIME_FORMAT.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _number(value: object, limit: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or abs(number) > limit:
        return None
    return round(number, 6)


def _side(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= SIDE_LIMIT:
        return None
    return value


def _text(value: object, limit: int = TEXT_LIMIT) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL.sub("", value).strip()[:limit].strip()
    return cleaned or None
