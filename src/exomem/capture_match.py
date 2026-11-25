"""Match an image that states no capture date to the original that does, by luminance structure.

A chat client or a photo export often re-encodes a picture without its EXIF block. The original, with
its date, may sit elsewhere in the vault or in an archive the owner keeps. This module fingerprints
images and accepts a match only when it is unique and confirmed at a finer scale: wrong dates are worse
than missing ones, so a doubtful match is refused with its reason. It reads files and never writes, moves or copies one.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import capture_metadata, image_codecs, media_types
from .perceptual_hash import average_hash, hamming

#: The hash is `HASH_SIDE` squared bits: fine enough to tell two scenes of one place apart, coarse enough
#: to survive a resize and a re-encode.
HASH_SIDE = 16
#: The farthest a copy may be from its original, in bits of 256 (about 5%). Resized, re-encoded copies of
#: test pictures at 320 px and up landed within 5 bits; this is more than twice that. A 200 px thumbnail
#: of a text page reached 16 and goes unmatched, the safe miss. The hash is only the first filter:
#: pictures of one kind (printed pages) often sit within 24 bits of each other, so `confirm` decides.
MAX_DISTANCE = 12
#: How much farther the next original with a different capture time must be. Two shots of one scene a
#: second apart sit within a few bits of each other, and a copy that cannot be told apart from both has
#: no unique date.
MIN_MARGIN = 24
#: A copy keeps its aspect ratio, to within a pixel of rounding on a long edge and a trimmed border.
ASPECT_TOLERANCE = 0.02
#: A hash with fewer set or clear bits than this encodes little structure (a near-flat picture), so many
#: unrelated pictures would match it. Such an image is never matched.
MIN_BITS_EACH = 64
#: A candidate is confirmed at a finer scale before it gives its date: the correlation of both pictures as
#: `DETAIL_SIDE` squared grayscale pixels. Resized, re-encoded copies of test pages scored 0.974 or more;
#: different pages that passed the hash scored 0.82 at most. The bound sits between, nearer the copies.
DETAIL_SIDE = 64
MIN_CORRELATION = 0.95
_DRAFT_SIDE = HASH_SIDE * 8
#: Files that a walk of an archive reads between two stops for the job's own checkpoint.
_WALK_STRIDE = 25

NO_MATCH = "no original within the match distance"
AMBIGUOUS = "another original with a different capture time is nearly as close"
NO_STRUCTURE = "the picture is too uniform to match"
NOT_CONFIRMED = "the closest original differs from the picture at a finer scale"


@dataclass(frozen=True, slots=True)
class Location:
    """Where an image's bytes can be read again: a path, and the format its name states (a stored blob has none)."""

    path: Path
    suffix: str


@dataclass(frozen=True, slots=True)
class Fingerprint:
    bits: int
    #: The upright size of the reduced-scale decode that was hashed; only its ratio is compared.
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Original:
    """A camera-dated image and what it can give to a copy."""

    fingerprint: Fingerprint
    time: str
    date: str
    #: Where it lives: a page id, or a path relative to the archive root.
    origin: str
    #: Where its bytes are, so a candidate can be confirmed against the copy.
    location: Location


def _upright_gray(location: Location):
    """The picture, upright and grayscale, decoded at a reduced scale where the format allows it.

    Raises what the decoder raises for a file it cannot read; the caller counts that as unreadable.
    """
    from PIL import ImageOps

    with image_codecs.open_image(location.path, suffix=location.suffix) as image:
        image.draft("L", (_DRAFT_SIDE, _DRAFT_SIDE))  # a JPEG decodes at reduced scale; other formats ignore it
        return ImageOps.exif_transpose(image).convert("L")


def fingerprint(location: Location) -> Fingerprint | None:
    """The fingerprint of the image at `location`, or None when it has no structure to match."""
    from PIL import Image

    gray = _upright_gray(location)
    bits = average_hash(gray, size=HASH_SIDE, resample=Image.Resampling.BOX)
    ones = bits.bit_count()
    if ones < MIN_BITS_EACH or HASH_SIDE * HASH_SIDE - ones < MIN_BITS_EACH:
        return None
    return Fingerprint(bits, *gray.size)


def confirm(copy: Location, original: Original) -> bool:
    """Whether the copy and the original agree at a finer scale than the hash.

    The hash keeps the pictures of one kind (printed pages, screenshots of one app) close together, so a
    near hash alone would hand one picture's date to another. A picture that cannot be read again is
    not confirmed.
    """
    from PIL import Image

    try:
        vectors = []
        for location in (copy, original.location):
            small = _upright_gray(location).resize((DETAIL_SIDE, DETAIL_SIDE), Image.Resampling.BOX)
            pixels = np.asarray(small, dtype=np.float32).ravel()
            vectors.append(pixels - pixels.mean())
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - untrusted bytes: an image that no decoder reads again is not confirmed
        return False
    norm = float(np.linalg.norm(vectors[0]) * np.linalg.norm(vectors[1]))
    return norm > 0 and float(vectors[0] @ vectors[1]) / norm >= MIN_CORRELATION


def _same_shape(first: Fingerprint, second: Fingerprint) -> bool:
    ratio = first.width / first.height
    return abs(ratio - second.width / second.height) <= ASPECT_TOLERANCE * ratio


class Pool:
    """The camera-dated originals a copy may be matched to."""

    def __init__(self) -> None:
        self._originals: list[Original] = []

    def __len__(self) -> int:
        return len(self._originals)

    def add(self, original: Original) -> None:
        self._originals.append(original)

    def find(self, copy: Fingerprint) -> Original | str:
        """The one original that `copy` came from, or the reason that none is accepted.

        The question is which date, not which file: two copies of one original state one time, so
        they never make a match ambiguous.
        """
        near = [
            (hamming(copy.bits, original.fingerprint.bits), original)
            for original in self._originals if _same_shape(copy, original.fingerprint)
        ]
        if not near:
            return NO_MATCH
        distance, best = min(near, key=lambda item: (item[0], item[1].origin))
        if distance > MAX_DISTANCE:
            return NO_MATCH
        rivals = (other for other_distance, other in near if other_distance < distance + MIN_MARGIN)
        return AMBIGUOUS if any(other.time != best.time for other in rivals) else best


def archive_files(root: Path) -> Iterator[Path]:
    """The image files under `root`, in a stable order. Links are not followed and nothing is opened.

    A camera raw is left out: demosaicing one costs seconds and memory, and its date belongs to a
    camera original that the vault pool or a JPEG rendering already states.
    """
    for folder, subfolders, names in os.walk(root, followlinks=False):
        subfolders.sort()
        for name in sorted(names):
            path = Path(folder) / name
            if (
                media_types.media_type_for(name) == "image"
                and path.suffix.lower() not in media_types.RAW_EXTS
                and path.is_file()
                and not path.is_symlink()
            ):
                yield path


def add_archive(pool: Pool, root: Path, *, tick: Callable[[int], None] | None = None) -> int:
    """Add every camera-dated image under `root` to `pool` and return how many files were read.

    A file that no decoder reads, or that states no camera time, adds nothing: the pool holds only
    originals whose date a file itself states. `tick` is told how many files were read, every few files.
    """
    read = 0
    for path in archive_files(root):
        read += 1
        if tick is not None and read % _WALK_STRIDE == 0:
            tick(read)
        facts = capture_metadata.read(path, "image")
        if facts is None or not facts.time or facts.date is None:
            continue
        location = Location(path, path.suffix)
        try:
            fingerprinted = fingerprint(location)
        except Exception:  # noqa: BLE001 - untrusted bytes: a decoder failure means this file is not an original to match
            continue
        if fingerprinted is not None:
            pool.add(Original(fingerprinted, facts.time, facts.date, path.relative_to(root).as_posix(), location))
    return read
