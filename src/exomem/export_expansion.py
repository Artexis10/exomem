"""Expand the app exports of a staged import into one Markdown transcript per item, inside the stage.

`file` calls `expand` before it scans the stage. Each recognised export (see `source_formats`) is read one item at a
time and each item is written as `<export stem> items/<item id>.md`, so filing then treats it as any staged file: it
is planned, deduplicated, applied and counted like one. The expansion record, `<state>/import-expansion/<import_id>.json`,
keeps what was derived and where each export stands, so that a call that runs out of budget resumes where it stopped:

    {"version": 1, "import_id": ..., "bytes": <derived bytes>,
     "exports": {"<path in the stage>": {"format": key, "kind": source kind, "state": "working" | "done" | "partial",
                                         "code": null | IMPORT_EXPORT_PARTIAL | IMPORT_EXPANSION_CAP, "read": items read}},
     "rows": [{"path", "item", "export", "created", "timed", "size", "sha256"}],
     "failed": [{"export", "item", "reason"}]}

An export file stays out of filing while it is `working`, because filing consumes it and a later call still has to read it.
A stage with no recognised export has no record. The export is untrusted content: its text is written as data and
no step follows, runs or interprets it.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import archive_members, export_transcripts, import_stage, source_formats

WORKING, DONE, PARTIAL = "working", "done", "partial"  # nosemgrep: ep-word-set -- the closed states of an export in the expansion record.
CODE_PARTIAL = "IMPORT_EXPORT_PARTIAL"
CODE_CAP = "IMPORT_EXPANSION_CAP"
CODE_ITEM_UNREADABLE = export_transcripts.UNREADABLE_ITEM
RECORD_VERSION = 1
#: A batch of items is recorded together, so a crash costs at most one batch of rewrites and the record is not rewritten per item.
_BATCH_ITEMS = 100
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True, slots=True)
class Reading:
    """What filing needs to know of the stage's expansion, from one read of its record."""

    exports: tuple[str, ...]  # every recognised export, by path in the stage; each files as an artifact, never as text
    held: tuple[str, ...]  # exports still being read, by path in the stage; filing leaves each out until it is read
    kinds: dict[str, str]  # each derived folder, by path in the stage, with the Source kind its transcripts file as
    files: int  # transcripts written
    unfiled: tuple[dict, ...]  # unreadable items and partly read exports, as residual members

    @property
    def failed(self) -> int:
        """The items that could not be read."""
        return sum(1 for member in self.unfiled if member["code"] == CODE_ITEM_UNREADABLE)

    @property
    def partial(self) -> int:
        """The exports that were read in part."""
        return len(self.unfiled) - self.failed


def _fresh(import_id: str) -> dict:
    return {"version": RECORD_VERSION, "import_id": import_id, "bytes": 0, "exports": {}, "rows": [], "failed": []}


def _record(root: Path, import_id: str) -> dict:
    record = import_stage.read_expansion(root, import_id)
    ok = (
        isinstance(record, dict) and record.get("version") == RECORD_VERSION and record.get("import_id") == import_id
        and isinstance(record.get("exports"), dict) and isinstance(record.get("rows"), list)
        and isinstance(record.get("failed"), list)
    )
    return record if ok else _fresh(import_id)


def _folder_of(export: str) -> str:
    """The stage folder that holds an export's transcripts: beside the export, named for it."""
    path = PurePosixPath(export)
    return path.with_name(f"{path.stem} items").as_posix()


def read(root: Path, import_id: str) -> Reading:
    """The stage's expansion as filing reads it; a stage with no record has expanded nothing."""
    record = _record(root, import_id)
    exports = {path: state for path, state in record["exports"].items() if isinstance(state, dict)}
    unfiled = [
        import_stage.residual_member(f"{failure.get('export')} item {failure.get('item')}", 0, CODE_ITEM_UNREADABLE)
        for failure in record["failed"] if isinstance(failure, dict)
    ]
    unfiled += [
        import_stage.residual_member(f"{path} items", 0, state["code"])
        for path, state in exports.items() if state.get("state") == PARTIAL and state.get("code")
    ]
    return Reading(
        exports=tuple(exports),
        held=tuple(path for path, state in exports.items() if state.get("state") == WORKING),
        kinds={_folder_of(path): state["kind"] for path, state in exports.items() if isinstance(state.get("kind"), str)},
        files=len(record["rows"]), unfiled=tuple(unfiled))


def expand(root: Path, import_id: str, *, keep_going: Callable[[], bool], rewalk: bool = False) -> None:
    """Derive transcripts from the stage's recognised exports until each is read or `keep_going` says the call is out of budget.

    A call walks the stage to find its exports when it has none yet, and `rewalk` walks it again for an export that
    came after, such as one that a rerun of a local import copied. A stage with no recognised export is left with no
    record. Each call resumes the export that is `working` at its recorded position, and always handles one item,
    so a call that starts with no budget left still moves on. An export that cannot be read to its end is `partial`,
    and the transcripts read before the fault stay.
    """
    record = _record(root, import_id)
    if (rewalk or not record["exports"]) and _walk(root, import_id, record):
        import_stage.write_expansion(root, import_id, record)
    for export, state in record["exports"].items():
        if state.get("state") != WORKING:
            continue
        if not _slice(root, import_id, record, export, state, keep_going):
            return


def _walk(root: Path, import_id: str, record: dict) -> bool:
    """Add the stage's recognised exports that the record does not hold yet to it as `working`; True when it added one."""
    known = source_formats.formats(root)
    base = root / import_stage.STAGE_DIR / import_id
    found = False
    for path, _size in sorted(import_stage.staged_files(root, import_id)):
        if (
            path in record["exports"] or not any(candidate.names(PurePosixPath(path).name) for candidate in known)
            or any(part.startswith(".") for part in PurePosixPath(path).parts)
        ):
            continue
        fmt = source_formats.recognise(root, base / path)
        if fmt is not None:
            record["exports"][path] = {"format": fmt.key, "kind": fmt.item_kind, "state": WORKING, "code": None, "read": 0}
            found = True
    return found


def _slice(root: Path, import_id: str, record: dict, export: str, state: dict, keep_going: Callable[[], bool]) -> bool:
    """Read one export from its recorded position; True when it is no longer `working`, so the next export may start.

    A fault in the export's own stream ends it as `partial`. A fault in writing a transcript, such as a full disk,
    is not the export's: it propagates, and the call that meets it again resumes from the recorded position.
    """
    fmt = next((candidate for candidate in source_formats.formats(root) if candidate.key == state.get("format")), None)
    base = root / import_stage.STAGE_DIR / import_id
    taken = {row["path"] for row in record["rows"] if row.get("export") == export}
    since_record = 0
    try:
        if fmt is None:
            raise export_transcripts.ExportFault("the format is no longer in the registry")
        with source_formats.open_export(base / export) as stream:
            for index, result in export_transcripts.read(fmt, stream, start=state["read"]):
                if len(record["rows"]) + len(record["failed"]) >= archive_members.MAX_ENTRIES:
                    return _stop(root, import_id, record, state, CODE_CAP)
                if not _take(root, import_id, record, export, index, result, taken):
                    return _stop(root, import_id, record, state, CODE_CAP)
                state["read"] = index + 1
                since_record += 1
                out_of_budget = not keep_going()
                if since_record >= _BATCH_ITEMS or out_of_budget:
                    import_stage.write_expansion(root, import_id, record)
                    since_record = 0
                if out_of_budget:
                    return False
    except (export_transcripts.ExportFault, FileNotFoundError, NotADirectoryError, IsADirectoryError):
        # The export ends early, is not JSON, or has left the stage: the items before it stay, and the export is partial.
        return _stop(root, import_id, record, state, CODE_PARTIAL)
    state["state"] = DONE
    import_stage.write_expansion(root, import_id, record)
    return True


def _take(
    root: Path, import_id: str, record: dict, export: str, index: int,
    result: export_transcripts.Transcript | export_transcripts.Unreadable, taken: set[str],
) -> bool:
    """Record one item that was read, as a transcript in the stage or as a failure; False when it would pass the byte cap."""
    if isinstance(result, export_transcripts.Unreadable):
        record["failed"].append({"export": export, "item": result.item_id, "reason": result.reason})
        return True
    # A lone surrogate that a JSON reader hands back cannot be encoded; it is written as a replacement character.
    data = result.text.encode("utf-8", errors="replace")
    if _over_cap(record, data):
        return False
    base = root / import_stage.STAGE_DIR / import_id
    path = _free_path(base, _folder_of(export), result.item_id, index, data, taken)
    if path is None:
        record["failed"].append({"export": export, "item": result.item_id, "reason": "its file name is taken in the stage"})
        return True
    timed = _publish(base / path, data, result.updated)
    taken.add(path)
    record["bytes"] += len(data)
    record["rows"].append({
        "path": path, "item": result.item_id, "export": export, "created": result.created, "timed": timed,
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return True


def _stop(root: Path, import_id: str, record: dict, state: dict, code: str) -> bool:
    state["state"], state["code"] = PARTIAL, code
    import_stage.write_expansion(root, import_id, record)
    return True


def _over_cap(record: dict, data: bytes) -> bool:
    """Whether `data` would take the derived bytes past what one import may stage."""
    from . import import_upload  # import_upload imports adoption_run, which imports this module

    return record["bytes"] + len(data) > import_upload.max_expanded_bytes()


def _free_path(base: Path, folder: str, item_id: str, index: int, data: bytes, taken: set[str]) -> str | None:
    """The stage path for an item's transcript: its id, else its id with its position; None when a staged file holds both.

    A name is free when no transcript of this expansion has it and no file is there that is not these bytes. A file with
    these bytes is this item's own, left by a call that stopped before it recorded the row. The sender's file at a
    name is never replaced: the item is counted instead.
    """
    digest = hashlib.sha256(data).hexdigest()
    for name in (item_id, f"{item_id}-{index + 1}"):
        path = f"{folder}/{name}.md"
        if path not in taken and not _held_by_other(base / path, digest):
            return path
    return None


def _held_by_other(target: Path, digest: str) -> bool:
    """Whether the name is taken by a file that is not these bytes: a staged member, or another item's transcript.

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
        return hashlib.sha256(target.read_bytes()).hexdigest() != digest
    except OSError:
        return True


def _publish(target: Path, data: bytes, mtime: float | None) -> bool:
    """Write `data` to `target` through a hidden temporary name, so a scan never sees half a file; True when it set the modified time."""
    folder = target.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if folder.is_symlink():
        raise OSError(f"{folder.name} is a link, so nothing is derived into it")
    temporary = folder / f".{target.name}.{time.monotonic_ns()}.tmp"
    try:
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
