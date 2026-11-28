"""The pass over a stage's Takeout sidecars: read each one, pair it with its photo, and keep the photos waiting meanwhile.

`export_expansion.expand` runs this pass first, with the same call budget as an export's read. It derives no files. For
each recognised sidecar (see `source_formats`, the `photo-sidecar` parser) it appends one row to the stage's expansion
rows and moves the pass's cursor past it. A row holds the sidecar's path, its name key (`export_pairing.Key`) and the
caption and Google's timestamp strings that it carries. The Source keeps the timestamps as labelled provenance.
A row never holds a position. When the last sidecar is read, the pass decides every pair in one step and appends one
`pair` row for each sidecar, then an `end` row:

- `{"sidecar": path, "base", "copy", "limit", "title", "caption", "timestamps"}`: one sidecar that was read. Its caption is redacted
  before the row is written, as an upload's description is, so no row keeps a card number or a credential.
- `{"unreadable": path}`: a file that could be a sidecar and could not be read. The pass reads on past it.
- `{"pair": path, "photo": photo path or None, "why": None | "no-photo" | "ambiguous"}`: the decision about it.
- `{"sidecars_end": "done" | "partial", "code", "fault"}`: the pass ended; it is `partial` when a file could not be
  read, and `fault` names the first such file.

The record's state caches the pass as `{"format", "state", "code", "cursor", "fault"}`; a read replays the rows that
came after the state. While the pass is `working`, the stage's images and videos, and the JSON files that it has not yet
examined, stay out of selection (`Hold`), so that no photo files before its sidecar is read. A recognised sidecar never
files as a Source, and neither does a file that could not be read. A stage with JSON files and no sidecar has no row of
the pass and a state that says it ended, so no later call opens its files again; a stage with no JSON file has no state
of the pass. A stage that grows after the pass ended is not read again.
The sidecars are untrusted content: a value is kept as data, and nothing in a sidecar is followed, run or sent to a model.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
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
CODE_PHOTO_ALREADY_IMPORTED = "IMPORT_SIDECAR_PHOTO_ALREADY_IMPORTED"
CODE_PHOTO_NOT_FILED = "IMPORT_SIDECAR_PHOTO_NOT_FILED"
#: A sidecar in a folder that holds no photo of it, whose name and title are those of a sidecar that paired in another folder:
#: an album that lists a photo which Takeout stored once, in its year folder.
CODE_ALBUM_ENTRY = "IMPORT_SIDECAR_ALBUM_ENTRY"
CODE_UNPAIRED = "IMPORT_SIDECAR_UNPAIRED"
#: A file that could be a sidecar and could not be read: it stays staged, and nothing is known of what it holds.
CODE_UNREADABLE = "IMPORT_SIDECAR_UNREADABLE"
#: What became of a paired photo in the import: its Source was stored now, an earlier Source already held its bytes (it
#: is in the vault), or it did not file (it failed, or no outcome says otherwise). `adoption_run` reads the outcomes.
STORED, ALREADY_IMPORTED, NOT_FILED = "stored", "already-imported", "not-filed"  # nosemgrep: ep-word-set -- the closed results of one photo's filing, this module's own protocol with `adoption_run`.
#: Each settled reason as (whether the residual counts the sidecar as in the Knowledge Base although its staged copy stays,
#: because its photo is in the vault and nothing here needs the person; how a residual summary counts such files, with
#: `{n}` as their number). `adoption_consume.STAYS` reads these rows.
STAYS: Mapping[str, tuple[bool, str]] = MappingProxyType({  # nosemgrep: ep-word-set, ep-word-dict -- this module's own closed reason codes, a protocol of the residual report and not vocabulary.
    CODE_APPLIED: (True, "{n} sidecar paired with its filed photo, whose caption it gave"),
    CODE_EMPTY: (True, "{n} sidecar paired with its filed photo, with no caption to give"),
    CODE_PHOTO_ALREADY_IMPORTED: (True, "{n} sidecar whose caption was not applied, because the vault already held its photo"),
    CODE_PHOTO_NOT_FILED: (False, "{n} sidecar whose photo did not file"),
    CODE_ALBUM_ENTRY: (True, "{n} sidecar of an album entry, whose photo is in the vault from another folder"),
    CODE_UNPAIRED: (False, "{n} sidecar that no one photo fits"),
    CODE_UNREADABLE: (False, "{n} possible sidecar that could not be read"),
})
#: The rows are appended, fsynced and counted this many at a time, so a pass costs one fsync for each batch.
_BATCH = 200
_ROW_KEYS = ("sidecar", "unreadable", "pair", "sidecars_end")  # nosemgrep: ep-word-set -- the closed row keys of the pass: this module's own record format.


class Record(Protocol):
    """The part of the expansion record that the pass uses (`export_expansion._Record`)."""

    import_id: str
    sidecars: dict | None

    def append(self, root: Path, rows: list[dict]) -> None: ...

    def save(self, root: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class Rows:
    """The pass's rows: each sidecar that was read, by its path in the stage, each decision about it and each file that could not be read."""

    sidecars: dict[str, dict]
    pairs: dict[str, tuple[str | None, str | None]]  # sidecar path -> (photo path, None) or (None, why it is unpaired)
    unreadable: frozenset[str]  # the paths that could not be read
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
    """Count one row of the pass into its state entry: the cursor moves past a file read or not, and an end row ends the pass."""
    path = row.get("sidecar") or row.get("unreadable")
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
    unreadable: set[str] = set()
    end = None
    for _after, row in import_stage.read_expansion_rows(root, import_id):
        if isinstance(path := row.get("sidecar"), str) and _key_of(row) is not None:
            sidecars[path] = row
        elif isinstance(path := row.get("unreadable"), str):
            unreadable.add(path)
        elif isinstance(path := row.get("pair"), str):
            photo, why = row.get("photo"), row.get("why")
            pairs[path] = (photo if isinstance(photo, str) else None, why if isinstance(why, str) else None)
        elif row.get("sidecars_end") in (DONE, PARTIAL):
            end = row
    return Rows(sidecars, pairs, frozenset(unreadable - sidecars.keys()), end)  # a file that was read since counts as read


def read(root: Path, import_id: str) -> Reading:
    """The stage's pass: its state and cursor from the record, and its rows; a stage with no pass reads as `state=None`."""
    state = import_stage.read_expansion_state(root, import_id)
    held = state.get("sidecars") if isinstance(state, dict) else None
    rows = read_rows(root, import_id)
    entry = held if isinstance(held, dict) else new_entry(None)
    if rows.sidecars or rows.unreadable or rows.end is not None:
        cursor = entry["cursor"] if isinstance(entry.get("cursor"), str) else ""
        entry = {**entry, "cursor": max([cursor, *rows.sidecars, *rows.unreadable])}
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

    A recognised sidecar never files, and neither does a file that the pass could not read: it may be a sidecar. While
    the pass works, an image or a video waits, and so does a JSON file that the pass has not examined yet. The waiting
    is by media type and name, never a list of paths.
    """

    def __init__(self, reading: Reading, known: tuple[source_formats.SourceFormat, ...], prefix: str) -> None:
        self.prefix = prefix  # what a path of the vault carries before its path in the stage
        self._sidecars = reading.rows.sidecars
        self._unreadable = reading.rows.unreadable
        self._working = reading.working
        self._cursor = reading.cursor
        self._known = known

    def code(self, path: str, size: int) -> str | None:
        """Why the staged file `path` (relative to the stage) stays out of selection, or None."""
        if path in self._sidecars:
            return CODE_READ
        if path in self._unreadable:
            return CODE_UNREADABLE
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


def photo_formats(root: Path | None) -> tuple[source_formats.SourceFormat, ...]:
    """The active entries whose parser derives no items: the sidecar formats (the shipped ones when `root` is None)."""
    return tuple(fmt for fmt in source_formats.formats(root) if not export_parsers.derives(fmt))


def photo_facts(reading: Reading) -> dict[str, dict]:
    """The caption and timestamps of each paired photo, by its path in the stage."""
    facts = {}
    for sidecar, (photo, _why) in reading.rows.pairs.items():
        row = reading.rows.sidecars.get(sidecar)
        if photo is not None and row is not None:
            facts[photo] = {"caption": row.get("caption"), "timestamps": row.get("timestamps") or {}}
    return facts


def _album_key(row: dict) -> tuple[export_pairing.Key, tuple[str, str]] | None:
    """An album entry must agree with a paired sidecar on the name, title and both Google timestamps."""
    key = _key_of(row)
    timestamps = export_photos.timestamp_pair(row.get("timestamps") or {})
    return (key, timestamps) if key is not None and timestamps is not None else None


def settle(reading: Reading, photo_filing: Callable[[str], str]) -> tuple[dict[str, str], dict | None]:
    """Each sidecar, by its path in the stage, with the one reason that it stays, and the counts of the stage's sidecars.

    The reason follows the photo's filing result (`photo_filing` says, for the photo at a path in the stage, whether it
    was `STORED`, was `ALREADY_IMPORTED` or did not file): a file that could not be read is `UNREADABLE`; an unpaired
    sidecar is `UNPAIRED`; one whose photo did not file is `PHOTO_NOT_FILED`; one whose photo an earlier Source already holds is `PHOTO_ALREADY_IMPORTED`; any other is `APPLIED`
    when it carried a caption, else `EMPTY`. `APPLIED` says that the sidecar was paired with a photo that filed now and gave it
    its caption.

    A sidecar with no matching photo that agrees with a paired sidecar on its album key is an album entry. Takeout stores a photo
    once, in its year folder, and lists it in each album with a sidecar alone (GPTH Neo issue 133). It follows the photo of
    the paired sidecar: `ALBUM_ENTRY` when that photo is in the vault, `PHOTO_NOT_FILED` when it is not. It gives no caption
    and counts in no `captioned`. One index of the paired keys serves every sidecar, so the work stays linear.

    The count is over the sidecars whose photo filed now: those that carry a caption (`captioned`). A stage with no sidecar has no counts, never zero ones.
    """
    codes = dict.fromkeys(reading.rows.unreadable, CODE_UNREADABLE)
    if not reading.rows.sidecars:
        return codes, None
    captioned = 0
    photos: dict[tuple[export_pairing.Key, tuple[str, str]], str | None] = {}
    for path, row in reading.rows.sidecars.items():
        photo = reading.rows.pairs.get(path, (None, None))[0]
        if photo is not None and (key := _album_key(row)) is not None:
            photos[key] = None if key in photos else photo
    for path, row in reading.rows.sidecars.items():
        photo, why = reading.rows.pairs.get(path, (None, None))
        if photo is None:
            key = _album_key(row) if why == export_pairing.NO_PHOTO else None
            elsewhere = photos.get(key) if key is not None else None
            if elsewhere is None:
                codes[path] = CODE_UNPAIRED
            else:
                codes[path] = CODE_ALBUM_ENTRY if photo_filing(elsewhere) in (STORED, ALREADY_IMPORTED) else CODE_PHOTO_NOT_FILED
            continue
        filing = photo_filing(photo)
        if filing == ALREADY_IMPORTED:
            codes[path] = CODE_PHOTO_ALREADY_IMPORTED
        elif filing != STORED:
            codes[path] = CODE_PHOTO_NOT_FILED
        else:
            has_caption = row.get("caption") is not None
            codes[path] = CODE_APPLIED if has_caption else CODE_EMPTY
            captioned += has_caption
    by_reason: dict[str, int] = {}
    for code in codes.values():
        if STAYS[code][0]:
            by_reason[code] = by_reason.get(code, 0) + 1
    return codes, {"captioned": captioned, "by_reason": by_reason}


def open_pass(root: Path, record: Record, *, listing: list[tuple[str, int]], skip: Callable[[str], bool]) -> bool:
    """Open the stage's pass, reading no file, when it holds a JSON file that the pass may read; True when it opened one.

    A scan that runs before the pass's first read then already holds the photos and every unexamined JSON file, so no
    sidecar files as a Source. A stage with no such file keeps no pass, as `advance` leaves it.
    """
    if record.sidecars is not None or not _candidates(listing, "", skip, photo_formats(root)):
        return False
    record.sidecars = new_entry(None)
    record.save(root)
    return True


def _candidates(
    listing: list[tuple[str, int]], cursor: str, skip: Callable[[str], bool], known: tuple[source_formats.SourceFormat, ...],
) -> list[str]:
    """The staged paths after `cursor` that the pass reads: a name that a sidecar format may have, within the size bound."""
    return [
        path for path, size in listing
        if path > cursor and size <= export_photos.MAX_BYTES and not skip(path)
        and any(fmt.names(PurePosixPath(path).name) for fmt in known)
    ]


def advance(
    root: Path, record: Record, *, listing: list[tuple[str, int]], skip: Callable[[str], bool], keep_going: Callable[[], bool],
) -> bool:
    """Read the stage's sidecars from the pass's cursor until all are read or `keep_going` says the call is out of budget.

    `listing` is the stage's files by path in the stage, sorted. True when the pass no longer works. A call always
    reads one file, so a call that starts with no budget left still moves on. A file that cannot be read gets its own
    `unreadable` row and the pass reads on, so no JSON file after it files unexamined; the pass then ends as `partial`.
    """
    known = photo_formats(root)
    entry = record.sidecars
    if entry is not None and entry["state"] != WORKING:
        return True
    cursor = entry["cursor"] if entry is not None else ""
    candidates = _candidates(listing, cursor, skip, known)
    if entry is None and not candidates:
        return True  # a stage with no JSON file to read has no pass, and so no record
    base = root / import_stage.STAGE_DIR / record.import_id
    buffered: list[dict] = []
    examined, out_of_budget = cursor, False
    for handled, path in enumerate(candidates):
        if handled and not keep_going():
            out_of_budget = True
            break
        try:
            hit = _examine(base / path, known)
        except OSError:  # a file that is gone or cannot be opened: listed alone, and never filed unexamined
            if record.sidecars is None:
                record.sidecars = new_entry(None)
                record.save(root)  # before the first row, as for the first sidecar
            buffered.append({"unreadable": path})
            examined = path
            continue
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
        buffered.append(_row(path, key, document))
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
    _end(root, record, [path for path, _size in listing if not skip(path)], known)
    return True


def _examine(path: Path, known: tuple[source_formats.SourceFormat, ...]) -> tuple[source_formats.SourceFormat, dict] | None:
    """The entry and the document of the staged file when it is a recognised sidecar, else None; an `OSError` propagates."""
    with source_formats.open_export(path) as stream:
        document = export_photos.read_object(stream)
    if document is None:
        return None
    name = path.name
    return next(((fmt, document) for fmt in known if fmt.names(name) and all(key in document for key in fmt.item_keys)), None)


def _row(path: str, key: export_pairing.Key, document: dict) -> dict:
    """One sidecar's row. The caption is redacted as an upload's description is (`preserve`), before it reaches disk."""
    from . import preserve

    _text, redacted, _extracted, _warnings = preserve.redact_capture_text(description=export_photos.caption_of(document))
    return {
        "sidecar": path, "base": key.base, "copy": key.copy, "limit": key.at_limit, "title": key.title,
        "caption": redacted, "timestamps": export_photos.timestamps_of(document),
    }


def _end(root: Path, record: Record, photos: list[str], known: tuple[source_formats.SourceFormat, ...]) -> None:
    """End the pass: decide every pair from the rows, then record the end, as `done`, or as `partial` when a file could not be read."""
    entry: dict[str, Any] = record.sidecars  # type: ignore[assignment]
    rows = read_rows(root, record.import_id)
    fault = min(rows.unreadable, default=None)  # the first file that could not be read, over every call of the pass
    if not rows.sidecars and fault is None:
        # No sidecar was found: the pass has no row to write. The state remembers it, so that no later call opens these files again.
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
