"""Where a capture date came from, and the file-name shapes that state one.

A sidecar's `capture_date_source` names a registry key. The keys, the class of each (evidence or
fallback) and their precedence are shipped pack data; code knows only which closed `producer` writes
which key and branches only on `class`. The file-name shapes are a second registry that the vault
overlay can extend, because tools and phones add naming shapes over time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache, lru_cache
from pathlib import Path, PurePosixPath

from .governance import raw_protection
from .vault import kb_root
from .vocabulary import registry

SOURCES_PACK = "capture-date-sources.yaml"
FORMATS_PACK = "capture-date-name-formats.yaml"

#: The code paths that can produce a date. A source entry names one, so the key stays data.
# nosemgrep: ep-word-set -- closed set of code paths implemented in this module's callers, not vocabulary.
PRODUCERS = frozenset({"file-metadata", "perceptual-match", "file-name", "captured-field"})
#: `evidence` is a time that a file states. A `fallback` is derived from another fact and never stored.
# nosemgrep: ep-word-set -- the closed trust classes this module and its callers implement.
CLASSES = frozenset({"evidence", "fallback"})

#: The groups a file-name pattern must capture: a name that states less than a time to the second
#: does not state a capture time that the sidecar's `capture_time` field can carry.
# nosemgrep: ep-word-set -- the six fields of a calendar time, fixed by the datetime constructor, not vocabulary.
_TIME_GROUPS = ("year", "month", "day", "hour", "minute", "second")
_PATTERN_LIMIT = 400
#: A file name is at most this long on the filesystems Exomem stores, so a pattern only ever reads this much.
_NAME_LIMIT = 255
_FIRST_YEAR = 1990
#: A leading set number such as `01-`: it orders a set of files and never states a date, so it is
#: stripped by its shape before the name is matched against the registry's formats. Only at the
#: start: a number inside a name (`IMG12-2023...`) is part of the maker's own shape.
_ORDINAL_PREFIX = re.compile(r"^\d{1,3}-")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# ---------------------------------------------------------------- sources


@dataclass(frozen=True, slots=True)
class Source:
    key: str
    evidence: bool
    producer: str
    #: Position among the evidence entries in pack order; `None` for a fallback.
    rank: int | None


@cache
def sources() -> Mapping[str, Source]:
    """The shipped sources by key. A malformed pack is a build defect, so this raises."""
    found: dict[str, Source] = {}
    rank = 0
    for entry in registry.pack_entries(SOURCES_PACK):
        kind, producer = entry.attributes.get("class"), entry.attributes.get("producer")
        if kind not in CLASSES or producer not in PRODUCERS:
            raise RuntimeError(f"shipped pack {SOURCES_PACK}: {entry.key!r} needs a known class and producer")
        evidence = kind == "evidence"
        found[entry.key] = Source(entry.key, evidence, producer, rank if evidence else None)
        rank += 1 if evidence else 0
    if sorted(item.producer for item in found.values()) != sorted(PRODUCERS):
        raise RuntimeError(f"shipped pack {SOURCES_PACK}: each producer needs exactly one source")
    return found


def source_for(producer: str) -> str:
    """The key that `producer` writes."""
    for source in sources().values():
        if source.producer == producer:
            return source.key
    raise KeyError(producer)


def is_known(key: object) -> bool:
    return isinstance(key, str) and key in sources()


def is_evidence(key: object) -> bool:
    return isinstance(key, str) and key in sources() and sources()[key].evidence


def outranks(new: str, old: str) -> bool:
    """Whether evidence source `new` takes precedence over `old`; never for a fallback or an unknown key."""
    first, second = sources().get(new), sources().get(old)
    return (
        first is not None and second is not None
        and first.rank is not None and second.rank is not None
        and first.rank < second.rank
    )


def recorded_source(value: object) -> object:
    """The source that a sidecar's `capture_date_source` field names.

    A date recorded without a source was stated by the file's own metadata: that reader was the only
    one that recorded a date before sources were named.
    """
    return source_for("file-metadata") if value is None else value


def survives_extraction(recorded: object, extracted: object) -> bool:
    """Whether a recorded date stays when a read of the file now states `extracted` (or no date, `None`).

    Reading the file again reproduces its camera date and its file-name date, so those follow the
    file. A matched original is a fact about another file that no re-read can reproduce, so it
    stays unless the read now states a source of higher precedence.
    """
    if not isinstance(recorded, str) or recorded != source_for("perceptual-match"):
        return False
    return not (isinstance(extracted, str) and outranks(extracted, recorded))


# ---------------------------------------------------------------- file names


@dataclass(frozen=True, slots=True)
class NameDate:
    """A capture time that a file name states, with the key of the format that read it."""

    time: str
    date: str
    format: str


def registry_path(root: Path) -> Path:
    return kb_root(root) / "_Schema" / "capture-date-name-formats.yaml"


def _check(entry: registry.Entry, entries: Mapping[str, registry.Entry]) -> None:
    attributes = entry.attributes
    pattern, maker = attributes.get("pattern"), attributes.get("maker")
    if not isinstance(pattern, str) or not pattern or len(pattern) > _PATTERN_LIMIT:
        raise registry.RegistryError(
            f"INVALID_NAME_FORMAT: pattern must be a regular expression of at most {_PATTERN_LIMIT} characters")
    try:
        groups = re.compile(pattern, re.IGNORECASE).groupindex
    except re.error as error:
        raise registry.RegistryError(f"INVALID_NAME_FORMAT: pattern does not compile: {error}") from error
    missing = [name for name in _TIME_GROUPS if name not in groups]
    if missing:
        raise registry.RegistryError(
            "INVALID_NAME_FORMAT: pattern must name the groups " + ", ".join(_TIME_GROUPS)
            + "; missing " + ", ".join(missing))
    if not isinstance(maker, str) or not maker.strip() or len(maker) > 80 or _CONTROL.search(maker):
        raise registry.RegistryError("INVALID_NAME_FORMAT: maker names what writes the shape, in one short line")
    if not entry.description.strip() or "\n" in entry.description.strip():
        raise registry.RegistryError("INVALID_NAME_FORMAT: description gives the one-line reason the shape states a time")


SPEC = registry.RegistrySpec(
    name="capture-date-name-formats",
    stem="capture-date-name-formats",
    overlay=registry_path,
    adapter=registry.FixedPackAdapter(FORMATS_PACK, lambda: SPEC, _check),
    fields=frozenset({"label", "description", "status", "attributes"}),
    attributes=frozenset({"pattern", "maker"}),
    immutable=frozenset({"attributes.pattern", "attributes.maker"}),
    # Agents meet these shapes only through a recovery preview, not the bootstrap.
    summarize_keys=False,
)


@lru_cache(maxsize=64)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def formats(vault_root: Path) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Each active format's key and compiled pattern: the shipped ones, then the vault's.

    An overlay with findings leaves the shipped formats in force (the adapter keeps them), so a
    malformed overlay never blocks an ingest; `schema_memory validate` reports it.
    """
    snapshot = registry.load(SPEC, vault_root)
    return tuple((entry.key, _compiled(entry.attributes["pattern"])) for entry in snapshot.active)


def from_file_name(vault_root: Path, name: object, *, now: datetime | None = None) -> NameDate | None:
    """The capture time that `name` states in a registered format, or None.

    Never reads a file time: a name that matches no format, or one whose digits are not a real
    moment between 1990 and next year, states nothing.
    """
    if not isinstance(name, str):
        return None
    # A raw-protected capture is stored under its name with this marker in front; the client's name follows it.
    base = PurePosixPath(name.replace("\\", "/")).name.removeprefix(raw_protection.PREFIX)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stripped = _ORDINAL_PREFIX.sub("", stem, count=1)
    last_year = (now or datetime.now(UTC)).year + 1
    for candidate in dict.fromkeys((stem, stripped)):
        if not candidate or len(candidate) > _NAME_LIMIT:
            continue
        for key, pattern in formats(vault_root):
            match = pattern.fullmatch(candidate)
            if match is None:
                continue
            try:
                moment = datetime(*(int(match.group(group)) for group in _TIME_GROUPS))
            except ValueError:
                continue
            if _FIRST_YEAR <= moment.year <= last_year:
                text = moment.strftime("%Y-%m-%dT%H:%M:%S")
                return NameDate(time=text, date=text[:10], format=key)
    return None
