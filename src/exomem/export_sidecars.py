"""The pass over a stage's Takeout sidecars: read each one, pair it with its photo, and keep the photos waiting meanwhile.

`export_expansion.expand` runs this pass first, with the same call budget as an export's read. It derives no files. For
each recognised sidecar (see `source_formats`, the `photo-sidecar` parser) it appends one row to the stage's expansion
rows and moves the pass's cursor past it. A row holds the sidecar's path, its name key (`export_pairing.Key`) and the
time and caption that it carries. It never holds a position. When the last sidecar is read, or a read fault ends the
pass, the pass decides every pair in one step and appends one `pair` row for each sidecar, then an `end` row:

- `{"sidecar": path, "base", "copy", "limit", "title", "time", "skip", "caption"}`: one sidecar that was read.
- `{"pair": path, "photo": photo path or None, "why": None | "no-photo" | "ambiguous"}`: the decision about it.
- `{"sidecars_end": "done" | "partial", "code", "fault"}`: the pass ended; a fault names the file it could not read.

The record's state caches the pass as `{"format", "state", "code", "cursor", "fault"}`; a read replays the rows that
came after the state. While the pass is `working`, the stage's images and videos, and the JSON files that it has not yet
examined, stay out of selection (`Hold`), so that no photo files before its sidecar is read. A recognised sidecar never
files as a Source. A stage with no sidecar has no row of the pass, and a stage with no record at all has no state of
it either. A stage that grows after the pass ended is not read again.
The sidecars are untrusted content: a value is kept as data, and nothing in a sidecar is followed, run or sent to a model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from . import export_pairing, export_parsers, export_photos, import_stage, source_formats
from .capture_metadata import KINDS
from .media_types import media_type_for

WORKING, DONE, PARTIAL = "working", "done", "partial"  # nosemgrep: ep-word-set -- the closed states of an export's read and of the sidecar pass in the expansion record.
CODE_PARTIAL = "IMPORT_EXPORT_PARTIAL"
#: A recognised sidecar never files: it is read for its photo, and counted when filing is done.
CODE_READ = "IMPORT_SIDECAR_READ"
#: A photo, or a JSON file not yet examined, waits until every sidecar of the stage is read.
CODE_WAITING = "IMPORT_SIDECAR_WAITING"
#: How each sidecar counts when filing is done, from its photo's filing result.
CODE_APPLIED = "IMPORT_SIDECAR_APPLIED"
CODE_EMPTY = "IMPORT_SIDECAR_EMPTY"
CODE_PHOTO_NOT_FILED = "IMPORT_SIDECAR_PHOTO_NOT_FILED"
CODE_UNPAIRED = "IMPORT_SIDECAR_UNPAIRED"
#: The rows are appended, fsynced and counted this many at a time, so a pass costs one fsync for each batch.
_BATCH = 200
_ROW_KEYS = ("sidecar", "pair", "sidecars_end")  # nosemgrep: ep-word-set -- the closed row keys of the pass: this module's own record format.


class Record(Protocol):
    """The part of the expansion record that the pass uses (`export_expansion._Record`)."""

    import_id: str
    sidecars: dict | None

    def append(self, root: Path, rows: list[dict]) -> None: ...

    def save(self, root: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class Rows:
    """The pass's rows: each sidecar that was read, by its path in the stage, and each decision about it."""

    sidecars: dict[str, dict]
    pairs: dict[str, tuple[str | None, str | None]]  # sidecar path -> (photo path, None) or (None, why it is unpaired)
    end: dict | None  # the row that ended the pass, if it ended


@dataclass(frozen=True, slots=True)
class Reading:
    """The pass as filing reads it: its state (None when the stage has no pass) and its rows."""

    state: str | None
    cursor: str
    fault: str | None
    rows: Rows

    @property
    def working(self) -> bool:
        return self.state == WORKING


def new_entry(format_key: str | None) -> dict:
    return {"format": format_key, "state": WORKING, "code": None, "cursor": "", "fault": None}


def is_row(row: dict) -> bool:
    """Whether a row of the expansion rows belongs to the pass and not to an export's items."""
    return any(key in row for key in _ROW_KEYS)


def apply(entry: dict, row: dict) -> None:
    """Count one row of the pass into its state entry: the cursor moves past a sidecar, and an end row ends the pass."""
    path = row.get("sidecar")
    if isinstance(path, str) and path > entry["cursor"]:
        entry["cursor"] = path
    end = row.get("sidecars_end")
    if end in (DONE, PARTIAL):
        entry["state"] = end
        entry["code"] = row.get("code") if isinstance(row.get("code"), str) else None
        entry["fault"] = row.get("fault") if isinstance(row.get("fault"), str) else None


def read_rows(root: Path, import_id: str) -> Rows:
    """One pass over the expansion rows for the pass's own rows; when a sidecar or a pair has two rows, the last one counts."""
    sidecars: dict[str, dict] = {}
    pairs: dict[str, tuple[str | None, str | None]] = {}
    end = None
    for _after, row in import_stage.read_expansion_rows(root, import_id):
        if isinstance(path := row.get("sidecar"), str) and _key_of(row) is not None:
            sidecars[path] = row
        elif isinstance(path := row.get("pair"), str):
            photo, why = row.get("photo"), row.get("why")
            pairs[path] = (photo if isinstance(photo, str) else None, why if isinstance(why, str) else None)
        elif row.get("sidecars_end") in (DONE, PARTIAL):
            end = row
    return Rows(sidecars, pairs, end)


def read(root: Path, import_id: str) -> Reading:
    """The stage's pass: its state and cursor from the record, and its rows; a stage with no pass reads as `state=None`."""
    state = import_stage.read_expansion_state(root, import_id)
    held = state.get("sidecars") if isinstance(state, dict) else None
    rows = read_rows(root, import_id)
    entry = held if isinstance(held, dict) else new_entry(None)
    if rows.sidecars or rows.end is not None:
        entry = {**entry, "cursor": max([entry["cursor"] if isinstance(entry.get("cursor"), str) else "", *rows.sidecars])}
        if rows.end is not None:
            apply(entry, rows.end)  # the rows outrank a state that a crash left behind them
    elif not isinstance(held, dict):
        return Reading(None, "", None, rows)
    fault = entry.get("fault") if isinstance(entry.get("fault"), str) else None
    state_now = entry.get("state") if entry.get("state") in (WORKING, DONE, PARTIAL) else WORKING
    return Reading(state_now, entry["cursor"] if isinstance(entry.get("cursor"), str) else "", fault, rows)


def _key_of(row: dict) -> export_pairing.Key | None:
    """The name key that a sidecar row holds; None for a row that is not well-formed, which then reads as no sidecar."""
    base, copy, limit, title = row.get("base"), row.get("copy"), row.get("limit"), row.get("title")
    ok = (
        isinstance(base, str) and isinstance(limit, bool) and (title is None or isinstance(title, str))
        and (copy is None or (isinstance(copy, int) and not isinstance(copy, bool)))
    )
    return export_pairing.Key(base, copy, limit, title) if ok else None


class Hold:
    """Which staged files stay out of selection while the stage's sidecars are read, and which never file.

    A recognised sidecar never files. While the pass works, an image or a video waits, and so does a JSON file that
    the pass has not examined yet: it may be a sidecar. Both ends hold by media type and name, never as a list of paths.
    """

    def __init__(self, reading: Reading, known: tuple[source_formats.SourceFormat, ...], prefix: str) -> None:
        self.prefix = prefix  # what a path of the vault carries before its path in the stage
        self._sidecars = reading.rows.sidecars
        self._working = reading.working
        self._cursor = reading.cursor
        self._known = known

    def code(self, path: str, size: int) -> str | None:
        """Why the staged file `path` (relative to the stage) stays out of selection, or None."""
        if path in self._sidecars:
            return CODE_READ
        if not self._working:
            return None
        if media_type_for(path) in KINDS:
            return CODE_WAITING
        name = PurePosixPath(path).name
        if size <= export_photos.MAX_BYTES and path > self._cursor and any(fmt.names(name) for fmt in self._known):
            return CODE_WAITING
        return None


def hold(root: Path, import_id: str, *, prefix: str = "") -> Hold | None:
    """The stage's hold, or None when the stage has no pass."""
    reading = read(root, import_id)
    if reading.state is None:
        return None
    return Hold(reading, photo_formats(root), prefix)


def waiting(root: Path, import_id: str) -> int:
    """How many staged files wait for the pass now: a photo or an unexamined JSON file; none once the pass ended."""
    held = hold(root, import_id)
    if held is None:
        return 0
    return sum(1 for path, size in import_stage.staged_files(root, import_id) if held.code(path, size) == CODE_WAITING)


def photo_formats(root: Path) -> tuple[source_formats.SourceFormat, ...]:
    """The active entries whose parser derives no items: the sidecar formats."""
    return tuple(fmt for fmt in source_formats.formats(root) if not export_parsers.derives(fmt))


def photo_facts(reading: Reading) -> dict[str, dict]:
    """Each paired photo, by its path in the stage, with what its sidecar carries: `sidecar`, `time`, `caption` and `skipped`."""
    facts = {}
    for sidecar, (photo, _why) in reading.rows.pairs.items():
        row = reading.rows.sidecars.get(sidecar)
        if photo is not None and row is not None:
            facts[photo] = {"sidecar": sidecar, "time": row.get("time"), "caption": row.get("caption"), "skipped": row.get("skip") is True}
    return facts


def settle(reading: Reading, filed: Callable[[str], bool]) -> tuple[dict[str, str], dict[str, int] | None]:
    """Each sidecar, by its path in the stage, with the one reason that it stays, and the counts of the stage's sidecars.

    The reason follows the photo's filing result (`filed` says whether the import filed the photo at a path in the stage):
    an unpaired sidecar is `UNPAIRED`; one whose photo is not filed is `PHOTO_NOT_FILED`; any other is `APPLIED` when it
    carried a time or a caption, else `EMPTY`. The counts are the filed photos that received a date and a caption, and the
    times that were skipped as upload times. A stage with no sidecar has no counts, never zero ones.
    """
    if not reading.rows.sidecars:
        return {}, None
    codes: dict[str, str] = {}
    counts = {"dated": 0, "captioned": 0, "time_skipped": 0}
    for path, row in reading.rows.sidecars.items():
        photo = reading.rows.pairs.get(path, (None, None))[0]
        if photo is None:
            codes[path] = CODE_UNPAIRED
        elif not filed(photo):
            codes[path] = CODE_PHOTO_NOT_FILED
        else:
            dated, captioned = row.get("time") is not None, row.get("caption") is not None
            codes[path] = CODE_APPLIED if dated or captioned else CODE_EMPTY
            counts["dated"] += dated
            counts["captioned"] += captioned
            counts["time_skipped"] += row.get("skip") is True
    return codes, counts


def advance(
    root: Path, record: Record, *, listing: list[tuple[str, int]], skip: Callable[[str], bool], keep_going: Callable[[], bool],
) -> bool:
    """Read the stage's sidecars from the pass's cursor until all are read or `keep_going` says the call is out of budget.

    `listing` is the stage's files by path in the stage, sorted. True when the pass no longer works. A call always
    reads one file, so a call that starts with no budget left still moves on. A file that cannot be read ends the
    pass as `partial`: the sidecars read before it stay, and the photos are released.
    """
    known = photo_formats(root)
    entry = record.sidecars
    if entry is not None and entry["state"] != WORKING:
        return True
    cursor = entry["cursor"] if entry is not None else ""
    candidates = [
        path for path, size in listing
        if path > cursor and size <= export_photos.MAX_BYTES and not skip(path)
        and any(fmt.names(PurePosixPath(path).name) for fmt in known)
    ]
    if entry is None and not candidates:
        return True  # a stage with no JSON file to read has no pass, and so no record
    base = root / import_stage.STAGE_DIR / record.import_id
    buffered: list[dict] = []
    examined, fault, out_of_budget = cursor, None, False
    for handled, path in enumerate(candidates):
        if handled and not keep_going():
            out_of_budget = True
            break
        try:
            hit = _examine(base / path, known)
        except OSError:
            fault = path
            break
        examined = path
        if hit is None:
            continue
        fmt, document = hit
        if record.sidecars is None:
            record.sidecars = new_entry(fmt.key)
            record.save(root)  # before the first row, so that a crash never leaves rows whose pass has no format
        elif record.sidecars["format"] is None:
            record.sidecars["format"] = fmt.key
        key = export_pairing.key_of(PurePosixPath(path).name, document.get("title"), fmt)
        if key is None:  # the entry's glob matched a name that is no `.json` file: not a sidecar
            continue
        buffered.append(_row(path, key, export_photos.facts_of(document)))
        if len(buffered) >= _BATCH:
            record.append(root, buffered)
            buffered = []
    if buffered:
        record.append(root, buffered)
    if record.sidecars is None:
        record.sidecars = new_entry(None)  # files were examined and none was a sidecar: remembered, so no call reads them again
    record.sidecars["cursor"] = max(record.sidecars["cursor"], examined)
    if out_of_budget:
        record.save(root)
        return False
    _end(root, record, [path for path, _size in listing if not skip(path)], known, fault)
    return True


def _examine(path: Path, known: tuple[source_formats.SourceFormat, ...]) -> tuple[source_formats.SourceFormat, dict] | None:
    """The entry and the document of the staged file when it is a recognised sidecar, else None; an `OSError` propagates."""
    with source_formats.open_export(path) as stream:
        document = export_photos.read_object(stream)
    if document is None:
        return None
    name = path.name
    return next(((fmt, document) for fmt in known if fmt.names(name) and all(key in document for key in fmt.item_keys)), None)


def _row(path: str, key: export_pairing.Key, facts: export_photos.Facts) -> dict:
    return {
        "sidecar": path, "base": key.base, "copy": key.copy, "limit": key.at_limit, "title": key.title,
        "time": facts.time, "skip": facts.time_skipped, "caption": facts.caption}


def _end(root: Path, record: Record, photos: list[str], known: tuple[source_formats.SourceFormat, ...], fault: str | None) -> None:
    """End the pass: decide every pair from the rows, then record the end, as `done`, or as `partial` when a file could not be read."""
    entry: dict[str, Any] = record.sidecars  # type: ignore[assignment]
    rows = read_rows(root, record.import_id)
    if not rows.sidecars and fault is None:
        # No sidecar was found: the pass has no row to write. The state remembers it, so no call opens these files again,
        # but a stage that has no record (no export either) is left with none.
        if import_stage.read_expansion_state(root, record.import_id) is None:
            record.sidecars = None
        else:
            entry["state"] = DONE
            record.save(root)
        return
    code = CODE_PARTIAL if fault is not None else None
    fmt = next((candidate for candidate in known if candidate.key == entry["format"]), None)
    if rows.sidecars and fmt is None:
        code = CODE_PARTIAL  # the entry left the registry: nothing says how its sidecars pair, so each one stays unpaired
    elif rows.sidecars:
        keys = {path: key for path, row in rows.sidecars.items() if (key := _key_of(row)) is not None}
        decided = export_pairing.decide(keys, photos, fmt)
        pending = [
            {"pair": path, "photo": photo, "why": why} for path, (photo, why) in decided.items() if path not in rows.pairs]
        for start in range(0, len(pending), _BATCH):
            record.append(root, pending[start : start + _BATCH])
    state = PARTIAL if code is not None else DONE
    record.append(root, [{"sidecars_end": state, "code": code, "fault": fault}])
    record.save(root)
