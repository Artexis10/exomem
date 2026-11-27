"""Expand the app exports of a staged import into one Markdown transcript per item, inside the stage.

`file` calls `expand` before it scans the stage. Each recognised export (see `source_formats`) is read one item at a
time. An item becomes `<export stem> items/<name>.md` and, for each indexable attachment that it holds,
`<export stem> files/<transcript stem>/<attachment name>`, so filing treats them as any staged file: they are planned,
deduplicated, applied and counted like one. The expansion record keeps what was derived and where each export stands, so
that a call that runs out of budget resumes where it stopped. It is a folder, `<state>/import-expansion/<import_id>/`:

- `rows.jsonl` holds the truth, one row per item, appended and fsynced after the item's files are written:
  `{"export", "item", "status": "derived" | "failed" | "skipped", "code", "reason", "created", "next",
  "files": [{"path", "size", "sha256", "timed"}]}`. `next` is the export's resume position after the item.
- `state.json` caches what the rows say, so that a status read opens one small file: each export's format, Source
  kind, state (`working`, `done` or `partial`), position and counts, the bytes of `rows.jsonl` it has read, and the
  `stop` of an expansion that waits for free space. A read replays the rows that came after those bytes, so a crash
  between a row and the state loses nothing.

An export file stays out of filing while it is `working`, because filing consumes it and a later call still has to read it.
A stage with no recognised export has no record. The export is untrusted content: its text is written as data and
no step follows, runs or interprets it.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import cloud_import, export_parsers, export_transcripts, import_stage, source_formats
from .export_transcripts import Skipped, Transcript, Unreadable

WORKING, DONE, PARTIAL = "working", "done", "partial"  # nosemgrep: ep-word-set -- the closed states of an export in the expansion record.
CODE_PARTIAL = "IMPORT_EXPORT_PARTIAL"
CODE_CAP = "IMPORT_EXPANSION_CAP"
CODE_ITEM_UNREADABLE = export_transcripts.UNREADABLE_ITEM
CODE_NAME_TAKEN = export_transcripts.NAME_TAKEN_ITEM
#: The stop of an expansion whose volume is full; apply's stop on free space has the same code, and one remedy.
CODE_SPACE = "INSUFFICIENT_SPACE"
RECORD_VERSION = 2
#: Derived bytes may pass the export's own bytes by this much: a transcript's headings and a note's references are not in the export's text.
_SLACK_BYTES = 64 * 1024 * 1024
#: The state is rewritten at least this often while an export is read, so a status read replays few rows.
_SAVE_EVERY = 500
#: The names that one transcript tries: its stem, then its stem with its position, then that with a counter up to this many.
_NAME_TRIES = 100
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
_FULL_DISK = (errno.ENOSPC, errno.EDQUOT)  # nosemgrep: ep-word-set -- the closed errno values for a full volume or an exhausted quota.


@dataclass(frozen=True, slots=True)
class Reading:
    """What filing needs to know of the stage's expansion, from one read of its state."""

    exports: tuple[str, ...]  # every recognised export, by path in the stage; each files as an artifact, never as text
    held: tuple[str, ...]  # exports still being read, by path in the stage; filing leaves each out until it is read
    kinds: dict[str, str]  # each derived folder, by path in the stage, with the Source kind its transcripts file as
    files: int  # derived files written: transcripts and attachments
    failed: int  # items that could not be read, or found no free name
    skipped: int  # items that the entry's own rule leaves out; not failed, and not a partial read
    partial: int  # exports that were read in part
    stop: dict | None  # why the expansion waits for the person: its code and the export, when the volume is full


def _entry(format_key: str | None = None, kind: str | None = None, size: int = 0) -> dict:
    return {
        "format": format_key, "kind": kind, "state": WORKING, "code": None, "position": 0, "size": size,
        "items": 0, "files": 0, "bytes": 0, "failed": 0, "skipped": 0}


class _Record:
    """One stage's expansion as its rows say it, with the state that caches them."""

    def __init__(self, import_id: str, exports: dict[str, dict], stop: dict | None, rows_size: int) -> None:
        self.import_id = import_id
        self.exports = exports
        self.stop = stop
        self.rows_size = rows_size

    def apply(self, row: dict) -> None:
        """Count one row into its export. A row whose position its export has passed is one that a crash repeated, and counts once."""
        export, after = row.get("export"), row.get("next")
        if not isinstance(export, str) or not isinstance(after, int) or isinstance(after, bool):
            return
        entry = self.exports.setdefault(export, _entry())
        if after <= entry["position"]:
            return
        entry["position"] = after
        status = row.get("status")
        if status == "derived":
            files = [file for file in row.get("files") or () if isinstance(file, dict) and isinstance(file.get("size"), int)]
            entry["items"] += 1
            entry["files"] += len(files)
            entry["bytes"] += sum(file["size"] for file in files)
        elif status == "failed":
            entry["failed"] += 1
        elif status == "skipped":
            entry["skipped"] += 1

    def document(self) -> dict:
        return {
            "version": RECORD_VERSION, "import_id": self.import_id, "rows_size": self.rows_size, "exports": self.exports,
            "stop": self.stop}

    def files(self) -> int:
        return sum(entry["files"] for entry in self.exports.values())


def _load(root: Path, import_id: str) -> _Record:
    """The stage's expansion: its cached state, brought up to date from the rows that came after it.

    A state that is missing or cannot be read is rebuilt from all the rows. Such an export has no format any more,
    so it ends as read in part, and the items already derived stay.
    """
    state = import_stage.read_expansion_state(root, import_id)
    rows_size = import_stage.expansion_rows_size(root, import_id)
    valid = (
        isinstance(state, dict) and state.get("version") == RECORD_VERSION and state.get("import_id") == import_id
        and isinstance(state.get("exports"), dict) and isinstance(state.get("rows_size"), int)
        and 0 <= state["rows_size"] <= rows_size
        and all(isinstance(entry, dict) for entry in state["exports"].values())
    )
    record = _Record(
        import_id, state["exports"] if valid else {}, state.get("stop") if valid and isinstance(state.get("stop"), dict) else None,
        state["rows_size"] if valid else 0)
    for after, row in import_stage.read_expansion_rows(root, import_id, offset=record.rows_size):
        record.apply(row)
        record.rows_size = after
    return record


def _save(root: Path, record: _Record) -> None:
    import_stage.write_expansion_state(root, record.import_id, record.document())


def _folder_of(export: str) -> str:
    """The stage folder that holds an export's transcripts: beside the export, named for it."""
    path = PurePosixPath(export)
    return path.with_name(f"{path.stem} items").as_posix()


def _files_folder_of(export: str) -> str:
    """The stage folder that holds the attachments of an export's items, one folder for each item."""
    path = PurePosixPath(export)
    return path.with_name(f"{path.stem} files").as_posix()


def read(root: Path, import_id: str) -> Reading:
    """The stage's expansion as filing reads it; a stage with no record has expanded nothing."""
    record = _load(root, import_id)
    exports = record.exports
    return Reading(
        exports=tuple(exports),
        held=tuple(path for path, state in exports.items() if state.get("state") == WORKING),
        kinds={_folder_of(path): state["kind"] for path, state in exports.items() if isinstance(state.get("kind"), str)},
        files=record.files(), failed=sum(state["failed"] for state in exports.values()),
        skipped=sum(state["skipped"] for state in exports.values()),
        partial=sum(1 for state in exports.values() if state.get("state") == PARTIAL and state.get("code")),
        stop=record.stop)


def unfiled(root: Path, import_id: str) -> list[dict]:
    """The items that expansion left out and the exports it read in part, as residual members: the stage holds no file for them.

    One pass over the rows. Each item is listed by the vendor's id where it has one, else by its position.
    """
    state = _load(root, import_id)
    passed: dict[str, int] = {}
    members = []
    for _after, row in import_stage.read_expansion_rows(root, import_id):
        export, after = row.get("export"), row.get("next")
        if not isinstance(export, str) or not isinstance(after, int) or after <= passed.get(export, 0):
            continue
        passed[export] = after
        if row.get("status") in ("failed", "skipped") and isinstance(row.get("code"), str):
            members.append(import_stage.residual_member(f"{export} item {row.get('item')}", 0, row["code"]))
    members += [
        import_stage.residual_member(f"{path} items", 0, entry["code"])
        for path, entry in state.exports.items() if entry.get("state") == PARTIAL and entry.get("code")]
    return members


def find_exports(root: Path, import_id: str, *, rewalk: bool = False) -> bool:
    """Record the stage's recognised exports as `working`, so a scan leaves each out until it is read; True when it found one.

    It walks the stage when the record holds no export yet or one has lost its format, and `rewalk` walks it again
    for an export that came after, such as one that a rerun of a local import copied. It reads no export item.
    A stage with no recognised export is left with no record.
    """
    return _register(root, _load(root, import_id), rewalk)


def _register(root: Path, record: _Record, rewalk: bool) -> bool:
    lost = any(entry["format"] is None for entry in record.exports.values())
    if (rewalk or lost or not record.exports) and _walk(root, record.import_id, record):
        _save(root, record)  # before the first row, so that a crash never leaves rows whose export has no format
        return True
    return False


def expand(root: Path, import_id: str, *, keep_going: Callable[[], bool], rewalk: bool = False) -> bool:
    """Derive transcripts from the stage's recognised exports until each is read or `keep_going` says the call is out of budget.

    It finds the exports as `find_exports` does. Each call resumes the export that is `working` at its recorded
    position, and always handles one item, so a call that starts with no budget left still moves on. An export that
    cannot be read to its end is `partial`, and the transcripts read before the fault stay. A full volume stops the
    call with the record's `stop`, and the next call tries again from the same item. It returns whether the call did
    work: it found an export or read from one. A call that returns False changed nothing.
    """
    record = _load(root, import_id)
    record.stop = None
    worked = _register(root, record, rewalk)
    for export, state in record.exports.items():
        if state.get("state") != WORKING:
            continue
        worked = True
        if not _slice(root, record, export, state, keep_going):
            break
    return worked


def _walk(root: Path, import_id: str, record: _Record) -> bool:
    """Add the stage's recognised exports that the record does not hold yet to it as `working`; True when it added one.

    A file that an expansion wrote is never an export: not a path of the rows, and nothing under a recorded export's
    `items` and `files` folders. A stage with no manifest therefore still expands.
    """
    known = source_formats.formats(root)
    base = root / import_stage.STAGE_DIR / import_id
    derived = import_stage.derived_files(root, import_id)
    folders = tuple(
        f"{folder}/" for export in record.exports for folder in (_folder_of(export), _files_folder_of(export)))
    found = False
    for path, size in sorted(import_stage.staged_files(root, import_id)):
        held = record.exports.get(path)
        if (
            (path in record.exports and (held or {}).get("format") is not None) or path in derived or path.startswith(folders)
            or not any(candidate.names(PurePosixPath(path).name) for candidate in known)
            or any(part.startswith(".") for part in PurePosixPath(path).parts)
        ):
            continue
        fmt = source_formats.recognise(root, base / path)
        if fmt is not None:
            # An export whose format is lost keeps the position that its rows reached; a new one starts at its first item.
            record.exports[path] = {**(held or _entry()), "format": fmt.key, "kind": fmt.item_kind, "size": size}
            found = True
    return found


def _slice(root: Path, record: _Record, export: str, state: dict, keep_going: Callable[[], bool]) -> bool:
    """Read one export from its recorded position; True when it is no longer `working`, so the next export may start.

    A fault in the export's own stream ends it as `partial`. A full volume stops the whole call: False, with the
    record's `stop` set. Any other fault in writing, such as a missing permission, is not the export's: it propagates,
    and the call that meets it again resumes from the recorded position.
    """
    import_id = record.import_id
    fmt = next((candidate for candidate in source_formats.formats(root) if candidate.key == state.get("format")), None)
    base = root / import_stage.STAGE_DIR / import_id
    incoming = f"{_files_folder_of(export)}/.incoming"
    _refuse_links(base, incoming)  # like every derived folder: nothing is written or removed through a link
    workdir = base / incoming
    shutil.rmtree(workdir, ignore_errors=True)  # what an earlier call left half-written
    taken = {
        path for _after, row in import_stage.read_expansion_rows(root, import_id) if row.get("export") == export
        for path in import_stage.derived_of(row)}
    since_save = 0
    try:
        if fmt is None:
            raise export_transcripts.ExportFault("the format is no longer in the registry")
        with source_formats.open_export(base / export) as stream:
            for found in export_parsers.read(fmt, stream, start=state["position"], workdir=workdir):
                outcome = _take(root, record, export, state, found, taken, workdir)
                if outcome is not None:
                    return _halt(root, record, export, outcome)
                since_save += 1
                out_of_budget = not keep_going()
                if since_save >= _SAVE_EVERY or out_of_budget:
                    _save(root, record)
                    since_save = 0
                if out_of_budget:
                    return False
    except (export_transcripts.ExportFault, FileNotFoundError, NotADirectoryError, IsADirectoryError):
        # The export ends early, is not what it declared, or has left the stage: the items before it stay, and the export is partial.
        return _halt(root, record, export, CODE_PARTIAL)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    state["state"] = DONE
    _save(root, record)
    return True


def _halt(root: Path, record: _Record, export: str, code: str) -> bool:
    """End the call's read of an export with `code`: a full volume waits for room, and any other code ends the export as partial."""
    state = record.exports[export]
    if code == CODE_SPACE:
        record.stop = {"code": code, "export": export}
        try:
            record.stop["free_bytes"] = shutil.disk_usage(root).free
        except OSError:
            record.stop["free_bytes"] = None
        _save(root, record)
        return False
    state["state"], state["code"] = PARTIAL, code
    _save(root, record)
    return True


def _take(
    root: Path, record: _Record, export: str, state: dict, found: export_transcripts.ReadItem, taken: set[str], workdir: Path,
) -> str | None:
    """Record one item that was read, as files in the stage, a failure or a skip; None when it did, else the code that ends the read.

    The item's files are written first and its one row is appended after, so a crash between the two leaves files
    that the same item writes again with the same bytes. A cap or a full volume leaves the item unwritten, so that a
    later call reads it again.
    """
    after, item = found
    if isinstance(item, Unreadable):
        return _append(root, record, export, {"item": item.item_id, "status": "failed", "code": CODE_ITEM_UNREADABLE, "reason": item.reason, "next": after})
    if isinstance(item, Skipped):
        return _append(root, record, export, {"item": item.item_id, "status": "skipped", "code": export_transcripts.SKIPPED_LABEL_ITEM, "reason": item.reason, "next": after})
    # A lone surrogate that a reader hands back cannot be encoded; it is written as a replacement character.
    data = item.text.encode("utf-8", errors="replace")
    if state["bytes"] + len(data) + sum(part.size for part in item.parts) > state["size"] + _SLACK_BYTES or (
        record.files() + 1 + len(item.parts) > cloud_import.max_members_from_env()
    ):
        _discard(item)
        return CODE_CAP
    base = root / import_stage.STAGE_DIR / record.import_id
    stem = _free_stem(base, export, item, after, data, taken)
    if stem is None:
        _discard(item)
        return _append(root, record, export, {
            "item": item.item_id, "status": "failed", "code": CODE_NAME_TAKEN, "reason": "no file name is free for it in the stage", "next": after})
    try:
        files = _write(base, export, stem, item, data)
    except OSError as error:
        _discard(item)
        if error.errno in _FULL_DISK:
            return CODE_SPACE
        raise
    taken.update(file["path"] for file in files)
    return _append(root, record, export, {
        "item": item.item_id, "status": "derived", "created": item.created, "next": after, "files": files})


def _append(root: Path, record: _Record, export: str, row: dict) -> str | None:
    """Append one item's row, durably, and count it; the code that ends the read when the volume has no room for it."""
    row = {"export": export, **row}
    try:
        record.rows_size = import_stage.append_expansion_rows(root, record.import_id, [row])
    except OSError as error:
        if error.errno in _FULL_DISK:
            return CODE_SPACE
        raise
    record.apply(row)
    return None


def _free_stem(base: Path, export: str, item: Transcript, after: int, data: bytes, taken: set[str]) -> str | None:
    """The file stem for an item's transcript; None when every name it tries is held.

    The item's own stem comes first, then the stem with the position that follows the item in its export, which
    is the item's alone, then that with a counter. A name is free when no file of this expansion has it and no file is
    there that is not these bytes. A file with these bytes is this item's own, left by a call that stopped before it
    recorded the row. A staged file of the sender is never replaced. The item's attachments must find their names free too.
    """
    digest = hashlib.sha256(data).hexdigest()
    items, attachments = _folder_of(export), _files_folder_of(export)
    names = [item.item_id, f"{item.item_id}-{after}", *(f"{item.item_id}-{after}-{count}" for count in range(2, _NAME_TRIES + 1))]
    for stem in names:
        path = f"{items}/{stem}.md"
        if path in taken or _held_by_other(base / path, digest):
            continue
        if all(
            (own := f"{attachments}/{stem}/{part.name}") not in taken and not _held_by_other(base / own, part.sha256)
            for part in item.parts
        ):
            return stem
    return None


def _held_by_other(target: Path, digest: str) -> bool:
    """Whether the name is taken by a file that is not these bytes: a staged member, or another item's file.

    A file with these very bytes is this item's own, left by a call that stopped before it recorded the row.
    """
    try:
        info = target.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not stat.S_ISREG(info.st_mode):
        return True
    try:
        return _digest_of(target) != digest
    except OSError:
        return True


def _digest_of(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as source:
        while block := source.read(1024 * 1024):
            sha.update(block)
    return sha.hexdigest()


def _write(base: Path, export: str, stem: str, item: Transcript, data: bytes) -> list[dict]:
    """Write an item's attachments, then its transcript last, and describe each file; the item's own modified time goes on all of them."""
    items, attachments = _folder_of(export), f"{_files_folder_of(export)}/{stem}"
    files = []
    for part in item.parts:
        path = f"{attachments}/{part.name}"
        timed = _publish(base, path, part.data, item.updated)
        files.append({"path": path, "size": part.size, "sha256": part.sha256, "timed": timed})
    path = f"{items}/{stem}.md"
    timed = _publish(base, path, data, item.updated)
    return [{"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "timed": timed}, *files]


def _discard(item: Transcript) -> None:
    """Delete the hidden files that an item's attachments still hold, when the item is not written."""
    for part in item.parts:
        if isinstance(part.data, Path):
            part.data.unlink(missing_ok=True)


def _refuse_links(base: Path, relative: str) -> None:
    """Refuse a link at any existing step of the stage path `relative`; the folders themselves are made later, when needed."""
    folder = base
    for name in PurePosixPath(relative).parts:
        folder = folder / name
        if folder.is_symlink():
            raise OSError(f"{name} is a link, so nothing is derived into it")


def _folder(base: Path, relative: str) -> Path:
    """The stage folder `relative`, made owner-only; a link in its path is refused, so nothing is derived through it."""
    folder = base
    for name in PurePosixPath(relative).parts:
        folder = folder / name
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.is_symlink():
            raise OSError(f"{name} is a link, so nothing is derived into it")
    return folder


def _publish(base: Path, relative: str, data: bytes | Path, mtime: float | None) -> bool:
    """Put `data` at `relative` in the stage through a hidden temporary name, so a scan never sees half a file; True when it set the modified time.

    A `Path` is a hidden file that the reader already wrote, and moves into place.
    """
    target = base / relative
    _folder(base, PurePosixPath(relative).parent.as_posix())
    temporary = data if isinstance(data, Path) else target.parent / f".{target.name}.{time.monotonic_ns()}.tmp"
    try:
        if not isinstance(data, Path):
            with os.fdopen(os.open(temporary, _FILE_FLAGS, 0o600), "wb") as sink:
                sink.write(data)
        timed = False
        if mtime is not None:
            try:
                os.utime(temporary, (mtime, mtime))
                timed = True
            except (OSError, OverflowError, ValueError):
                pass  # a timestamp is metadata; the row says that the file does not carry the item's
        os.replace(temporary, target)
        return timed
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
