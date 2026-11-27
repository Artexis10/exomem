"""Pair each Takeout sidecar with the one photo it describes, from file names and titles alone.

Google publishes no specification, so the rules follow the community tools that read a Takeout (OpenSpec
`carry-takeout-sidecar-captions`, Decision 2). A sidecar pairs only when its name and its `title` both fit one image
or video in its own folder, no other file fits it, and that file fits no other sidecar. Every vendor fact (the suffix
word, the length limits, the replaced characters) is registry data on the entry (`source_formats`).

Names compare exactly after NFC normalisation, and a copy number `(n)` compares apart from the name. A title's
extension fits a file's when both have the same MIME type, so `.jpeg` fits `.jpg`, and `.heic`, a raw file or a video never
fits a `.jpg` sibling. The pairing is linear in the files: one index over the folder's photos, and a sidecar looks up its
few candidates in it.
"""

from __future__ import annotations

import bisect
import mimetypes
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .capture_metadata import KINDS
from .media_types import media_type_for

if TYPE_CHECKING:
    from .source_formats import SourceFormat

#: Why a sidecar stays unpaired: no file fits it, or more than one file or sidecar does.
NO_PHOTO, AMBIGUOUS = "no-photo", "ambiguous"
_MAX_COPY_DIGITS = 6
#: The standard extension-to-MIME table of the Python standard library and nothing else: no `mime.types` file of the host
#: is read, so the same Takeout pairs the same way on every machine.
_MIME_TYPES = mimetypes.MimeTypes()


@dataclass(frozen=True, slots=True)
class Key:
    """What a sidecar's name and title say about its photo, kept in its row so that pairing needs no second read."""

    base: str  # the photo name that the sidecar's name carries, without the suffix word; a cut name is cut here too
    copy: int | None  # the copy number after the suffix word, when there is one
    at_limit: bool  # the name is as long as Takeout keeps one, so `base` may be a prefix of the photo's name
    title: str | None  # the title as a photo's file name reads it (replaced characters, cut stem); None when it names no photo


@dataclass(frozen=True, slots=True)
class _Reading:
    stem: str  # the name without its extension and, in the copy reading, without its own `(n)`
    copy: int | None
    full: str  # `stem` and the extension


@dataclass(frozen=True, slots=True)
class _Entry:
    path: str
    mime: str
    stem: str


@dataclass(frozen=True, slots=True)
class _Index:
    """The photos of a stage by what a sidecar looks them up with: exact name, and name prefix within a folder and copy number."""

    exact: Mapping[tuple[str, str, int | None, str], list[_Entry]]
    names: Mapping[tuple[str, int | None], list[str]]
    entries: Mapping[tuple[str, int | None], list[_Entry]]


def key_of(file_name: str, title: object, fmt: SourceFormat) -> Key | None:
    """The key of a sidecar file name and its `title`, or None when the name is not a sidecar's (it does not end in `.json`)."""
    name = unicodedata.normalize("NFC", file_name)
    if not name.lower().endswith(".json"):
        return None
    stem, copy = _split_copy(name[: -len(".json")])
    return Key(_cut_suffix(stem, fmt.suffix_word), copy, len(stem) >= fmt.sidecar_name_limit, title_name(title, fmt))


def mime_of(name: str) -> str:
    """The MIME type of a file name's extension, or the lower-cased extension itself when the table has no type for it.

    Two names have the same type when Google may write one for the other (`.jpg` and `.jpeg`); a different format never does.
    """
    extension = PurePosixPath(name).suffix.lower()
    return _MIME_TYPES.guess_type("file" + extension)[0] or extension


def title_name(title: object, fmt: SourceFormat) -> str | None:
    """The file name that a photo with this title has in a Takeout: the replaced characters changed and the stem cut."""
    if not isinstance(title, str):
        return None
    name = unicodedata.normalize("NFC", title)
    if "/" in name or "\\" in name:
        return None
    name = "".join(fmt.replaced.get(char, char) for char in name)
    dot = name.rfind(".")
    return None if dot <= 0 else name[:dot][: fmt.media_name_limit] + name[dot:]


def _split_copy(text: str) -> tuple[str, int | None]:
    """`text` without a trailing copy number `(n)`, and that number."""
    start = text.rfind("(")
    digits = text[start + 1 : -1]
    if text.endswith(")") and start > 0 and digits.isascii() and digits.isdigit() and len(digits) <= _MAX_COPY_DIGITS:
        return text[:start], int(digits)
    return text, None


def _cut_suffix(stem: str, word: str) -> str:
    """`stem` without the suffix word, or without the part of it that Takeout kept when it cut the name: `.suppl`, `.supplemental-me`."""
    for kept in range(len(word), -1, -1):
        tail = "." + word[:kept]
        if stem.endswith(tail):
            return stem[: -len(tail)]
    return stem


def _readings(name: str, copy: int | None = None) -> list[_Reading]:
    """The ways to read a name's stem and copy number: whole, and with its own trailing `(n)` as the copy number.

    A `(n)` is part of the name when the owner named the file so, and a copy number when Takeout added it, and
    the name alone cannot say which; the title decides (see `decide`). A name that already carries a copy number
    after the suffix has no second one in its stem.
    """
    dot = name.rfind(".")
    stem, extension = (name, "") if dot <= 0 else (name[:dot], name[dot:])
    readings = [_Reading(stem, copy, name)]
    if copy is None:
        bare, own = _split_copy(stem)
        if own is not None and bare:
            readings.append(_Reading(bare, own, bare + extension))
    return readings


def decide(sidecars: Mapping[str, Key], photos: Iterable[str], fmt: SourceFormat) -> dict[str, tuple[str | None, str | None]]:
    """Each sidecar, by its path in the stage, as (its photo's path, None) or (None, why it stays unpaired).

    `photos` are the staged files that may be photos; only the images and videos count. A sidecar pairs only when
    exactly one photo fits it by name and by title, in the sidecar's own folder, and that photo fits no other sidecar.
    """
    index = _index(photos)
    fitting: dict[str, list[str]] = {}
    claims: dict[str, int] = {}
    for path, key in sidecars.items():
        fits = _fits(path.rpartition("/")[0], key, index) if key.title is not None else []
        fitting[path] = fits
        for photo in fits:
            claims[photo] = claims.get(photo, 0) + 1
    decided: dict[str, tuple[str | None, str | None]] = {}
    for path, fits in fitting.items():
        if not fits:
            decided[path] = (None, NO_PHOTO)
        elif len(fits) == 1 and claims[fits[0]] == 1:
            decided[path] = (fits[0], None)
        else:
            decided[path] = (None, AMBIGUOUS)
    return decided


def _index(photos: Iterable[str], *, across_folders: bool = False) -> _Index:
    """The staged files that are images or videos, indexed for a sidecar to look up its few candidates.

    With `across_folders` every file is indexed under the one folder `""`, so that a lookup there finds a photo of any folder.
    """
    exact: dict[tuple[str, str, int | None, str], list[_Entry]] = {}
    by_copy: dict[tuple[str, int | None], list[tuple[str, _Entry]]] = {}
    for path in photos:
        folder, _slash, raw = path.rpartition("/")
        if across_folders:
            folder = ""
        name = unicodedata.normalize("NFC", raw)
        if media_type_for(name) not in KINDS:
            continue
        mime = mime_of(name)
        for reading in _readings(name):
            entry = _Entry(path, mime, reading.stem)
            exact.setdefault((folder, reading.stem, reading.copy, mime), []).append(entry)
            by_copy.setdefault((folder, reading.copy), []).append((reading.full, entry))
    names: dict[tuple[str, int | None], list[str]] = {}
    entries: dict[tuple[str, int | None], list[_Entry]] = {}
    for key, pairs in by_copy.items():
        pairs.sort(key=lambda pair: pair[0])
        names[key], entries[key] = [name for name, _entry in pairs], [entry for _name, entry in pairs]
    return _Index(exact, names, entries)


def _fits(folder: str, key: Key, index: _Index, *, by_title: bool = True) -> list[str]:
    """The photos of `folder` that the sidecar's name fits and, when `by_title`, its title too."""
    title_stem, title_mime = (key.title.rpartition(".")[0], mime_of(key.title)) if by_title and key.title is not None else (None, None)
    base_mime, base_is_media = mime_of(key.base), media_type_for(key.base) in KINDS
    fits: dict[str, None] = {}
    for reading in _readings(key.base, key.copy):
        if base_is_media:
            for entry in index.exact.get((folder, reading.stem, reading.copy, base_mime), ()):
                if title_stem is None or (entry.stem == title_stem and entry.mime == title_mime):
                    fits[entry.path] = None
        if key.at_limit and reading.full:
            held = index.names.get((folder, reading.copy), ())
            at = bisect.bisect_left(held, reading.full)
            while at < len(held) and held[at].startswith(reading.full):
                entry = index.entries[(folder, reading.copy)][at]
                if title_stem is None or (entry.stem == title_stem and entry.mime == title_mime):
                    fits[entry.path] = None
                at += 1
    return list(fits)


def likely_photos(paths: Iterable[str], fmt: SourceFormat) -> dict[str, str]:
    """Each path of `paths` that is named like a sidecar, with the photo of `paths` that its name fits.

    A guess from names alone, for a planner that must keep a sidecar with its photo and opens no file: no title is
    read, so nothing is paired, and a wrong guess only costs the sidecar its caption (`decide` decides). A name that fits
    several photos (a cut name) goes with the first of them in path order. A sidecar that no photo of its own folder fits
    is an album's entry, which Takeout lists beside no photo (GPTH Neo issue 133): it goes with the first photo in path
    order that its name fits in any folder, which is usually the photo that its twin in the year folder goes with.
    """
    paths = list(paths)
    index = _index(paths)
    anywhere: _Index | None = None  # built when a sidecar has no photo in its own folder
    likely: dict[str, str] = {}
    for path in paths:
        folder, _slash, name = path.rpartition("/")
        key = key_of(name, None, fmt)
        if key is None:
            continue
        fits = _fits(folder, key, index, by_title=False)
        if not fits:
            if anywhere is None:
                anywhere = _index(paths, across_folders=True)
            fits = _fits("", key, anywhere, by_title=False)
        if fits:
            likely[path] = min(fits)
    return likely
