"""Stage a folder from outside the vault into `_Imports/<import_id>/<folder name>/`.

This is the layout Exomem Cloud uses for an uploaded folder, so adoption scans it like any
staged upload: the folder's name becomes the collection label and a repeat import dedupes.
The original folder is only read, and no link in it is followed. The manifest is a journal in
the user's state folder, not in the staged tree, so adoption sees the folder and nothing else.

The journal keeps one JSON line per path, and the last line for a path wins. It stays open (named
`*.partial.jsonl`) until every selected file is staged and verified, so a rerun of the same command
resumes the same import id and copies only what is missing. One lock file per import keeps two runs
from writing the same journal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock, Timeout
from platformdirs import user_state_dir

from . import held_fs, import_selection
from .cli_progress import Progress, files_are, files_phrase, human_bytes
from .import_selection import Kept

JOURNAL_DIR = "local-imports"
_OPEN_SUFFIX = ".partial.jsonl"
_DONE_SUFFIX = ".jsonl"
_LOCK_SUFFIX = ".lock"
_CHUNK = 1 << 20
# A journal line reaches the disk after this many records; a loss in a crash only costs a recopy.
_SYNC_EVERY = 200
# held_fs refuses with a closed set of codes. These two follow from the file's own name and path, so a rerun is
# refused the same way; any other code (IO_REFUSED, IDENTITY_CHANGED, CROSS_DEVICE) comes from the disk and can clear.
_REFUSED_ON_EVERY_RUN = frozenset({"DESTINATION_EXISTS", "UNSAFE_PATH"})
_SHOWN_FAILURES = 5


class LocalImportError(Exception):
    """A refusal or failure with a content-free code; the message names measured values, never contents."""

    def __init__(self, code: str, message: str, import_id: str | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.import_id = import_id


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


class _Journal:
    """The manifest: one JSON line per path, appended as the copy goes, so an interrupted copy resumes from it."""

    def __init__(self, path: Path, valid_length: int | None = None):
        self.path = path
        # Set for an existing journal: the bytes after it are a torn line that the first append cuts off.
        self._valid_length = valid_length
        self._stream = None
        self._unsynced = 0

    @staticmethod
    def read(path: Path) -> tuple[list[dict], int]:
        """The records before the first unreadable line, and the byte length of the journal that holds them."""
        records, length = [], 0
        # The last element is the text after the final newline: a line a crash cut short, or nothing.
        for line in path.read_bytes().split(b"\n")[:-1]:
            try:
                records.append(json.loads(line))
            except ValueError:
                break
            length += len(line) + 1
        return records, length

    def add(self, record: dict) -> None:
        if self._stream is None:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
            if self._valid_length is not None:
                # Without this the next line would join the torn one, and neither could be read again.
                os.ftruncate(descriptor, self._valid_length)
            self._stream = os.fdopen(descriptor, "a", encoding="utf-8")
        self._stream.write(json.dumps(record, sort_keys=True) + "\n")
        self._stream.flush()
        self._unsynced += 1
        if self._unsynced >= _SYNC_EVERY:
            os.fsync(self._stream.fileno())
            self._unsynced = 0

    def close(self) -> None:
        if self._stream is not None:
            os.fsync(self._stream.fileno())
            self._stream.close()
            self._stream = None


def _journal_dir() -> Path:
    """Where manifests live: the user's state folder, as the Cloud checkpoints do, and not the vault's own state.

    A vault's state placement is settled when the vault is first initialised, and a state folder created
    earlier by this command would make that later step refuse with a placement conflict.
    """
    directory = Path(user_state_dir("exomem")) / JOURNAL_DIR
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


@dataclass(frozen=True)
class _Unfinished:
    import_id: str
    path: Path
    options: dict
    name: str


def _unfinished_imports(directory: Path, vault: Path, source: str) -> list[_Unfinished]:
    """The unfinished imports of this folder into this vault, newest first, read from each journal's header."""
    found = []
    for path in directory.glob(f"*{_OPEN_SUFFIX}"):
        try:
            modified = path.stat().st_mtime
            with path.open("rb") as stream:
                header = json.loads(stream.readline(1 << 16))
        except (OSError, ValueError):
            continue  # a run that finishes while this one looks removes its journal from the folder
        if (header.get("kind") == "header" and header.get("vault") == str(vault)
                and header.get("source") == source):
            found.append((modified, _Unfinished(header["import_id"], path, header["options"], header["name"])))
    return [item for _, item in sorted(found, key=lambda pair: pair[0], reverse=True)]


def _flags(options: dict) -> str:
    flags = (["--include-hidden"] if options["include_hidden"] else []) + (
        [] if options["use_gitignore"] else ["--no-gitignore"])
    return " ".join(flags) or "no options"


@dataclass(frozen=True)
class _Claim:
    """The import this run works on: its id and, for a resumed one, the journal's records."""

    import_id: str
    records: list[dict]
    valid_length: int | None

    @property
    def resumed(self) -> bool:
        return self.valid_length is not None


def _claim(directory: Path, vault: Path, stage_dir: str, source: str, options: dict,
           progress: Progress) -> tuple[FileLock, _Claim]:
    """Take the lock of the import to resume, or of a new one; two runs never write one journal."""
    unfinished = _unfinished_imports(directory, vault, source)
    for other in unfinished:
        if other.options != options:
            progress.say(
                f"Note: import {other.import_id} of this folder is unfinished and was made with {_flags(other.options)}, "
                f"so this run does not resume it. Its staged folder is {vault / stage_dir / other.import_id / other.name}. "
                "Run again with those options to finish it, or delete that folder and its journal if you do not need it.")
    busy = None
    for candidate in (item for item in unfinished if item.options == options):
        lock = FileLock(directory / f"{candidate.import_id}{_LOCK_SUFFIX}")
        try:
            lock.acquire(timeout=0)
        except Timeout:
            busy = busy or candidate
            continue
        if not candidate.path.exists():
            lock.release()  # the run that held it finished it
            continue
        records, length = _Journal.read(candidate.path)
        return lock, _Claim(candidate.import_id, records, length)
    if busy is not None:
        raise LocalImportError(
            "IMPORT_IN_PROGRESS",
            "another run of this command is copying this folder into the same import; wait for it, then run again",
            busy.import_id)
    import_id = str(uuid.uuid4())
    lock = FileLock(directory / f"{import_id}{_LOCK_SUFFIX}")
    lock.acquire(timeout=0)
    return lock, _Claim(import_id, [], None)


def _checks_before_copy(root: Path, vault: Path) -> None:
    if not vault.is_dir():
        raise LocalImportError("VAULT_NOT_FOUND", "the vault folder does not exist")
    real_root, real_vault = root.resolve(), vault.resolve()
    if real_root == real_vault or real_root.is_relative_to(real_vault):
        raise LocalImportError(
            "FOLDER_INSIDE_VAULT",
            "this folder is already inside the vault; start adoption on its vault-relative path instead")
    if real_vault.is_relative_to(real_root):
        raise LocalImportError(
            "FOLDER_CONTAINS_VAULT", "this folder contains the vault, so staging it would copy the vault into itself")
    # `_collection_of` ignores hidden entries, so a folder with a hidden name would not be found as the collection.
    if not root.name or root.name.startswith("."):
        raise LocalImportError("FOLDER_NAME_UNUSABLE", "the folder needs a name that does not start with a dot")


def _advice(selection: import_selection.Selection, name: str, free_after: int, progress: Progress) -> None:
    """Warnings that do not stop the copy: limits the adoption engine applies after the files are staged."""
    from . import adoption_run, client_artifacts

    if len(selection.kept) > adoption_run.MAX_CANDIDATES_DEFAULT:
        progress.say(f"Note: adoption lists up to {adoption_run.MAX_CANDIDATES_DEFAULT:,} files per start; "
                     f"this folder has {len(selection.kept):,}. Start on subfolders of the printed path to cover the rest, "
                     f'and pass collection="{name}" to each start so that they stay one collection.')
    ceiling = client_artifacts.fetch_budget().max_file_bytes
    large = sum(1 for item in selection.kept if item.size > ceiling)
    if large:
        progress.say(f"Note: {files_are(large)} over {human_bytes(ceiling)}; "
                     f"{'it stages' if large == 1 else 'they stage'} here, "
                     f"and adoption refuses {'it' if large == 1 else 'them'} with TOO_LARGE.")
    progress.say(f"Note: adoption copies each file again into Sources, which needs about "
                 f"{human_bytes(selection.kept_bytes)} more; {human_bytes(free_after)} will be free after staging.")


class _Stream:
    """The bytes of one source file for `held_fs`, hashed as they pass; a retried pass starts over."""

    def __init__(self, descriptor: int):
        self._descriptor = descriptor
        self.digest = hashlib.sha256()
        self.size = 0

    def parts(self):
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        self.digest, self.size = hashlib.sha256(), 0
        while block := os.read(self._descriptor, _CHUNK):
            self.digest.update(block)
            self.size += len(block)
            yield block


def _refusal(error: held_fs.HeldFsError) -> dict:
    """The outcome of a vault refusal; it is final when a rerun would be refused the same way."""
    return {"code": "STAGING_REFUSED", "detail": error.code, "final": error.code in _REFUSED_ON_EVERY_RUN}


def _copy_file(fs, directory, root: Path, staged_root: Path, item: Kept) -> dict:
    """Publish one file from the original into the staged folder; return its record or its failure code."""
    try:
        descriptor = import_selection.open_for_read(root / item.rel)
    except OSError:
        return {"code": "UNREADABLE"}
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != item.identity
                or before.st_size != item.size):
            return {"code": "SOURCE_CHANGED"}
        stream = _Stream(descriptor)
        leaf = item.rel.rsplit("/", 1)[-1]
        published = held_fs.publish_bytes(fs, directory, leaf, held_fs.Chunks(open=stream.parts))
        if not published.ok:
            return _refusal(published.error)
        after = os.fstat(descriptor)
        if stream.size != item.size or after.st_size != item.size or after.st_mtime_ns != before.st_mtime_ns:
            # The original changed while it was read, so the staged bytes may mix two versions.
            _remove(fs, directory, leaf)
            return {"code": "SOURCE_CHANGED"}
        try:
            staged = os.lstat(staged_root / item.rel)
        except OSError:
            return {"code": "STAGED_FILE_MISSING"}
        # The staged file's own identity and change time let a resume tell this copy from a replaced one.
        return {"kind": "file", "path": item.rel, "size": item.size, "sha256": stream.digest.hexdigest(),
                "mtime_ns": item.mtime_ns, "staged_ino": staged.st_ino, "staged_ctime_ns": staged.st_ctime_ns}
    except OSError:
        return {"code": "UNREADABLE"}
    finally:
        os.close(descriptor)


def _remove(fs, directory, leaf: str) -> None:
    found = fs.file(directory, leaf, access="mutate")
    if found.ok:
        with found.require() as staged:
            fs.unlink(staged)


def _is_staged(staged_root: Path, item: Kept, record: dict | None) -> bool:
    """A file the journal verified earlier, still the same staged file, and still the size and age of the original."""
    if record is None or record["size"] != item.size or record["mtime_ns"] != item.mtime_ns:
        return False
    try:
        info = os.lstat(staged_root / item.rel)
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_size == item.size
            and (info.st_ino, info.st_ctime_ns) == (record.get("staged_ino"), record.get("staged_ctime_ns")))


def _sha256_of(path: Path) -> str:
    descriptor = import_selection.open_for_read(path)
    with os.fdopen(descriptor, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _replay(records: list[dict]) -> dict[str, dict]:
    """The files an earlier run verified and nothing has since invalidated or removed."""
    verified: dict[str, dict] = {}
    for record in records:
        if record.get("kind") == "file":
            verified[record["path"]] = record
        elif record.get("kind") == "invalid" or record.get("kind") == "removed":
            verified.pop(record["path"], None)
    return verified


class _Run:
    """One run of the command on a claimed import: prune, copy, verify, then finish or stay open."""

    def __init__(self, claim: _Claim, root: Path, vault: Path, stage_dir: str, selection: import_selection.Selection,
                 journal: _Journal, progress: Progress):
        self.claim, self.root, self.selection, self.journal, self.progress = claim, root, selection, journal, progress
        self.start_path = f"{stage_dir}/{claim.import_id}"
        self.staged_rel = f"{self.start_path}/{root.name}"
        self.staged_root = vault / self.staged_rel
        self.verified = _replay(claim.records)
        self.done = {item.rel for item in selection.kept
                     if _is_staged(self.staged_root, item, self.verified.get(item.rel))}
        self.pending = [item for item in selection.kept if item.rel not in self.done]
        self.needed = sum(item.size for item in self.pending)
        # A rerun can copy a file in `failures` (it changed, could not be read, or the disk refused it).
        # A file in `refused` is final.
        self.failures: list[tuple[str, str]] = []
        self.refused: list[tuple[str, str]] = []

    def _fail(self, rel: str, outcome: dict) -> None:
        (self.refused if outcome.get("final") else self.failures).append((rel, outcome["code"]))
        self.journal.add({"kind": "left_out", "path": rel, **outcome})

    def prune(self, filesystem) -> None:
        """Remove staged files that the selection no longer holds, so adoption never scans a stale one."""
        keep = {item.rel for item in self.selection.kept}
        directory, open_parent = None, None
        for folder, _, names in os.walk(self.staged_root):
            inside = Path(folder).relative_to(self.staged_root).as_posix()
            prefix = "" if inside == "." else inside + "/"
            for leaf in sorted(names):
                rel = prefix + leaf
                if rel in keep:
                    continue
                parent = self.staged_rel if inside == "." else f"{self.staged_rel}/{inside}"
                if parent != open_parent:
                    if directory is not None:
                        directory.close()
                    opened = filesystem.parent(parent, access="mutate")
                    directory, open_parent = (opened.require(), parent) if opened.ok else (None, None)
                if directory is not None:
                    _remove(filesystem, directory, leaf)
                self.verified.pop(rel, None)
                if os.path.lexists(self.staged_root / rel):
                    self._fail(rel, {"code": "STALE_NOT_REMOVED"})
                else:
                    self.journal.add({"kind": "removed", "path": rel, "code": "NOT_SELECTED"})
        if directory is not None:
            directory.close()

    def copy(self, filesystem) -> None:
        self.progress.say(f"Copying {files_phrase(len(self.pending))}, {human_bytes(self.needed)}, into {self.staged_rel}/.")
        copied_bytes = 0
        directory, open_parent = None, None
        try:
            for ordinal, item in enumerate(self.pending, 1):
                parent = "/".join([self.staged_rel, *item.rel.split("/")[:-1]])
                if parent != open_parent:
                    if directory is not None:
                        directory.close()
                        directory = None
                    made = filesystem.parent(parent, create=True, access="mutate")
                    if not made.ok:
                        self._fail(item.rel, _refusal(made.error))
                        open_parent = None
                        continue
                    directory, open_parent = made.require(), parent
                if self.claim.resumed:
                    # An earlier run may have published this file and stopped before journaling it, or the original
                    # changed since; the old staged copy goes first because a publish never replaces a file.
                    _remove(filesystem, directory, item.rel.rsplit("/", 1)[-1])
                    if item.rel in self.verified:
                        self.journal.add({"kind": "invalid", "path": item.rel})
                        del self.verified[item.rel]
                outcome = _copy_file(filesystem, directory, self.root, self.staged_root, item)
                if "code" in outcome:
                    self._fail(item.rel, outcome)
                    continue
                self.journal.add(outcome)
                self.verified[item.rel] = outcome
                self.done.add(item.rel)
                copied_bytes += item.size
                self.progress.tick(f"Copying: {ordinal:,} of {len(self.pending):,} files, "
                                   f"{human_bytes(copied_bytes)} of {human_bytes(self.needed)}")
        finally:
            if directory is not None:
                directory.close()

    def verify(self) -> None:
        self.progress.say("Verifying the copies.")
        for ordinal, item in enumerate(self.selection.kept, 1):
            if item.rel not in self.done:
                continue
            try:
                matches = _sha256_of(self.staged_root / item.rel) == self.verified[item.rel]["sha256"]
            except OSError:
                matches = False
            if not matches:
                self.journal.add({"kind": "invalid", "path": item.rel})
                raise LocalImportError(
                    "STAGED_COPY_MISMATCH",
                    "a staged copy does not match the original; run the command again to redo it", self.claim.import_id)
            self.progress.tick(f"Verifying: {ordinal:,} of {len(self.selection.kept):,} files")

    def finish(self, journals: Path, include_hidden: bool) -> dict:
        """Close the manifest: final when every file is staged or refused by the vault, open for a rerun otherwise."""
        kept = [item for item in self.selection.kept if item.rel in self.done]
        staged = {"files": len(kept), "bytes": sum(item.size for item in kept)}
        manifest = self.journal.path
        if not self.failures:
            self.journal.add({"kind": "done", **staged, "finished": datetime.now(UTC).isoformat()})
            self.journal.close()
            manifest = journals / f"{self.claim.import_id}{_DONE_SUFFIX}"
            os.replace(self.journal.path, manifest)
        for rel, code in [*self.failures, *self.refused][:_SHOWN_FAILURES]:
            self.progress.say(f"Not copied: {rel} ({code}).")
        result = {
            "ok": not self.failures, "import_id": self.claim.import_id, "collection": self.root.name,
            "start_path": self.start_path, "include_hidden": include_hidden, "staged": staged,
            "failed_while_copying": len(self.failures), "refused_by_vault": len(self.refused),
            **self.selection.summary(), "manifest": str(manifest),
            "staged_folder": str(self.staged_root),
        }
        if self.failures:
            result["code"] = "COPY_INCOMPLETE"
        return result


def stage(folder: Path, vault: Path, *, include_hidden: bool = False, use_gitignore: bool = True,
          progress: Progress | None = None, free_bytes=_free_bytes) -> dict:
    """Copy a folder into the vault's `_Imports/` and verify the copy; the original is never changed.

    The result has `ok` false and the code `COPY_INCOMPLETE` when some files changed or could not be read while
    copying. The import then stays open, and the same command run again retries only those files under the same
    import id. A file that the vault refuses for its name or path is final: it is left out with its code, counted in
    `refused_by_vault`, and the import finishes without it. A refusal that comes from the disk keeps the import open.
    """
    from .adoption_run import IMPORT_STAGE_DIR
    from .cloud_import import DEFAULT_RESERVE_BYTES

    progress = progress or Progress()
    vault = Path(vault)
    # abspath, not resolve: the name the user typed is the collection's name even when the folder is reached by a link.
    root = Path(os.path.abspath(folder))
    if not root.is_dir():
        raise LocalImportError("FOLDER_NOT_FOUND", "the source is not a folder")
    _checks_before_copy(root, vault)

    progress.say(f"Scanning {root} without following links.")
    selection = import_selection.select(root, include_hidden=include_hidden, use_gitignore=use_gitignore)
    for line in selection.report_lines():
        progress.say(line)
    if not selection.kept:
        raise LocalImportError("NOTHING_TO_IMPORT",
                               "no file is left to import; pass --include-hidden or --no-gitignore to take more")

    options = {"include_hidden": include_hidden, "use_gitignore": use_gitignore}
    journals = _journal_dir()
    lock, claim = _claim(journals, vault, IMPORT_STAGE_DIR, str(root), options, progress)
    try:
        journal = _Journal(journals / f"{claim.import_id}{_OPEN_SUFFIX}", claim.valid_length)
        run = _Run(claim, root, vault, IMPORT_STAGE_DIR, selection, journal, progress)
        try:
            free = free_bytes(vault)
            if run.needed + DEFAULT_RESERVE_BYTES > free:
                raise LocalImportError(
                    "INSUFFICIENT_DISK",
                    f"the copy needs {run.needed} bytes plus a reserve of {DEFAULT_RESERVE_BYTES} bytes and the vault's "
                    f"disk has {free} bytes free", claim.import_id)
            if claim.resumed:
                progress.say(f"Resuming import {claim.import_id}: {files_are(len(run.done))} already staged "
                             "and not copied again.")
            else:
                journal.add({"kind": "header", "import_id": claim.import_id, "vault": str(vault), "source": str(root),
                             "name": root.name, "options": options, "started": datetime.now(UTC).isoformat()})
                for skipped in selection.skipped:
                    journal.add({"kind": "left_out", "path": skipped.rel, "code": skipped.reason.name,
                                 "dir": skipped.is_dir, "files": skipped.files, "bytes": skipped.bytes})
            _advice(selection, root.name, free - run.needed, progress)
            try:
                filesystem = held_fs.acquire(vault).require()
            except held_fs.HeldFsError as error:
                raise LocalImportError(
                    "STAGING_UNAVAILABLE", f"the vault refused the staging folder ({error.code})", claim.import_id) from None
            with filesystem:
                if claim.resumed:
                    run.prune(filesystem)
                run.copy(filesystem)
            run.verify()
            return run.finish(journals, include_hidden)
        finally:
            journal.close()
    finally:
        lock.release()


def _closing_lines(result: dict) -> list[str]:
    staged = result["staged"]
    start = f'adoption_studio start with path "{result["start_path"]}"'
    if result["include_hidden"]:
        start += " and include_hidden=true"
    lines = [f"Staged {files_phrase(staged['files'])}, {human_bytes(staged['bytes'])}, in {result['staged_folder']}.",
             "This is a second copy of the folder on this disk. No Exomem command discards it: "
             f"after adoption finishes, delete {Path(result['staged_folder']).parent} to free the space."]
    if result["ok"]:
        refused = result["refused_by_vault"]
        note = [f"{files_phrase(refused)} left out because the vault refused {'it' if refused == 1 else 'them'}: "
                "the manifest gives each a code."] if refused else []
        return [*lines, *note, f"Manifest: {result['manifest']}", f"Next: run {start}."]
    return [*lines, f"Manifest: {result['manifest']}",
            f"The import is not finished: {files_phrase(result['failed_while_copying'])} could not be copied. "
            f"Run the same command again to retry only those under import {result['import_id']}.",
            f"To adopt what is staged so far first, run {start}."]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem import",
        description="Copy a folder from outside the vault into the vault's _Imports/ folder, verified, "
        "so adoption can scan it. The original folder is never changed.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--vault", type=Path, help="vault root (default: EXOMEM_VAULT_PATH)")
    import_selection.add_selection_arguments(parser)
    args = parser.parse_args(argv)
    progress = Progress()
    try:
        if args.vault is not None:
            vault = args.vault.expanduser()
        else:
            from .vault import resolve_vault

            try:
                vault = resolve_vault()
            except RuntimeError:
                raise LocalImportError("VAULT_NOT_FOUND", "set EXOMEM_VAULT_PATH or pass --vault") from None
        result = stage(args.folder, vault, include_hidden=args.include_hidden,
                       use_gitignore=not args.no_gitignore, progress=progress)
    except LocalImportError as error:
        progress.say(f"The import stopped: {error.code}. {error.message}.")
        print(json.dumps({"ok": False, "code": error.code, "message": error.message, "import_id": error.import_id}))
        return 1
    print(json.dumps(result), flush=True)
    for line in _closing_lines(result):
        print(line, file=sys.stderr, flush=True)
    return 0 if result["ok"] else 1
