"""Stage a folder from outside the vault into `_Imports/<import_id>/<folder name>/`.

This is the layout Exomem Cloud uses for an uploaded folder, so adoption scans it like any
staged upload: the folder's name becomes the collection label and a repeat import dedupes.
The original folder is only read, and no link in it is followed. The manifest is a journal in
the vault's state directory, not in the staged tree, so adoption sees the folder and nothing else.
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
from datetime import UTC, datetime
from pathlib import Path

from platformdirs import user_state_dir

from . import held_fs, import_selection
from .cli_progress import Progress, files_phrase, human_bytes
from .import_selection import Kept

JOURNAL_DIR = "local-imports"
_OPEN_SUFFIX = ".partial.jsonl"
_DONE_SUFFIX = ".jsonl"
_CHUNK = 1 << 20
# A journal line reaches the disk after this many records; a loss in a crash only costs a recopy.
_SYNC_EVERY = 200


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

    def __init__(self, path: Path):
        self.path = path
        self._stream = None
        self._unsynced = 0

    @staticmethod
    def read(path: Path) -> list[dict]:
        records = []
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    break  # a line torn by a crash ends the journal; the copy redoes whatever it described
        return records

    def add(self, record: dict) -> None:
        if self._stream is None:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
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


def _find_open_import(directory: Path, vault: Path, source: str, options: dict) -> tuple[str, list[dict]] | None:
    """The newest unfinished import of this folder into this vault made with the same options, with its records."""
    for path in sorted(directory.glob(f"*{_OPEN_SUFFIX}"), key=lambda item: item.stat().st_mtime, reverse=True):
        records = _Journal.read(path)
        header = records[0] if records else {}
        if (header.get("kind") == "header" and header.get("vault") == str(vault)
                and header.get("source") == source and header.get("options") == options):
            return header["import_id"], records
    return None


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


def _advice(selection: import_selection.Selection, free_after: int, progress: Progress) -> None:
    """Warnings that do not stop the copy: limits the adoption engine applies after the files are staged."""
    from . import adoption_run, client_artifacts

    if len(selection.kept) > adoption_run.MAX_CANDIDATES_DEFAULT:
        progress.say(f"Note: adoption lists up to {adoption_run.MAX_CANDIDATES_DEFAULT:,} files per start; "
                     f"this folder has {len(selection.kept):,}. Start on subfolders of the printed path to cover the rest.")
    ceiling = client_artifacts.fetch_budget().max_file_bytes
    large = sum(1 for item in selection.kept if item.size > ceiling)
    if large:
        progress.say(f"Note: {files_phrase(large)} are over {human_bytes(ceiling)}; "
                     "they stage here, and adoption refuses them with TOO_LARGE.")
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


def _copy_file(fs, directory, root: Path, item: Kept) -> dict:
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
            return {"code": "STAGING_REFUSED", "detail": published.error.code}
        after = os.fstat(descriptor)
        if stream.size != item.size or after.st_size != item.size or after.st_mtime_ns != before.st_mtime_ns:
            # The original changed while it was read, so the staged bytes may mix two versions.
            _remove(fs, directory, leaf)
            return {"code": "SOURCE_CHANGED"}
        return {"kind": "file", "path": item.rel, "size": item.size, "sha256": stream.digest.hexdigest(),
                "mtime_ns": item.mtime_ns}
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
    """A file the journal verified earlier, still in place and still the size and age of the original."""
    if record is None or record["size"] != item.size or record["mtime_ns"] != item.mtime_ns:
        return False
    try:
        info = os.lstat(staged_root / item.rel)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size == item.size


def _sha256_of(path: Path) -> str:
    descriptor = import_selection.open_for_read(path)
    with os.fdopen(descriptor, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stage(folder: Path, vault: Path, *, include_hidden: bool = False, use_gitignore: bool = True,
          progress: Progress | None = None, free_bytes=_free_bytes) -> dict:
    """Copy a folder into the vault's `_Imports/` and verify the copy; the original is never changed."""
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
    resumed = _find_open_import(journals, vault, str(root), options)
    import_id, earlier = resumed if resumed else (str(uuid.uuid4()), [])
    journal = _Journal(journals / f"{import_id}{_OPEN_SUFFIX}")
    verified: dict[str, dict] = {}
    for record in earlier:
        if record.get("kind") == "file":
            verified[record["path"]] = record
        elif record.get("kind") == "invalid":
            verified.pop(record["path"], None)
    staged_root = vault / IMPORT_STAGE_DIR / import_id / root.name
    done = {item.rel for item in selection.kept if _is_staged(staged_root, item, verified.get(item.rel))}
    pending = [item for item in selection.kept if item.rel not in done]
    needed = sum(item.size for item in pending)

    free = free_bytes(vault)
    if needed + DEFAULT_RESERVE_BYTES > free:
        raise LocalImportError(
            "INSUFFICIENT_DISK",
            f"the copy needs {needed} bytes plus a reserve of {DEFAULT_RESERVE_BYTES} bytes and the vault's disk has "
            f"{free} bytes free", import_id)
    if resumed:
        progress.say(f"Resuming import {import_id}: {files_phrase(len(done))} are already staged and are not copied again.")
    else:
        journal.add({"kind": "header", "import_id": import_id, "vault": str(vault), "source": str(root),
                     "name": root.name, "options": options, "started": datetime.now(UTC).isoformat()})
        for skipped in selection.skipped:
            journal.add({"kind": "left_out", "path": skipped.rel, "code": skipped.reason.name,
                         "dir": skipped.is_dir, "files": skipped.files, "bytes": skipped.bytes})
    _advice(selection, free - needed, progress)

    failures: list[tuple[str, str]] = []
    try:
        filesystem = held_fs.acquire(vault).require()
    except held_fs.HeldFsError as error:
        raise LocalImportError("STAGING_UNAVAILABLE", f"the vault refused the staging folder ({error.code})", import_id) from None
    with filesystem:
        progress.say(f"Copying {files_phrase(len(pending))}, {human_bytes(needed)}, into {IMPORT_STAGE_DIR}/{import_id}/{root.name}/.")
        copied_bytes = 0
        directory, open_parent = None, None
        try:
            for ordinal, item in enumerate(pending, 1):
                parent = "/".join([IMPORT_STAGE_DIR, import_id, root.name, *item.rel.split("/")[:-1]])
                if parent != open_parent:
                    if directory is not None:
                        directory.close()
                        directory = None
                    made = filesystem.parent(parent, create=True, access="mutate")
                    if not made.ok:
                        failures.append((item.rel, "STAGING_REFUSED"))
                        journal.add({"kind": "left_out", "path": item.rel, "code": "STAGING_REFUSED", "detail": made.error.code})
                        open_parent = None
                        continue
                    directory, open_parent = made.require(), parent
                if resumed:
                    # An earlier run may have published this file and stopped before journaling it, or the original
                    # changed since; the old staged copy goes first because a publish never replaces a file.
                    _remove(filesystem, directory, item.rel.rsplit("/", 1)[-1])
                    if item.rel in verified:
                        journal.add({"kind": "invalid", "path": item.rel})
                        del verified[item.rel]
                outcome = _copy_file(filesystem, directory, root, item)
                if "code" in outcome:
                    failures.append((item.rel, outcome["code"]))
                    journal.add({"kind": "left_out", "path": item.rel, **outcome})
                    continue
                journal.add(outcome)
                verified[item.rel] = outcome
                done.add(item.rel)
                copied_bytes += item.size
                progress.tick(f"Copying: {ordinal:,} of {len(pending):,} files, "
                              f"{human_bytes(copied_bytes)} of {human_bytes(needed)}")
        finally:
            if directory is not None:
                directory.close()

        progress.say("Verifying the copies.")
        for ordinal, item in enumerate(selection.kept, 1):
            if item.rel not in done:
                continue
            record = verified[item.rel]
            try:
                matches = _sha256_of(staged_root / item.rel) == record["sha256"]
            except OSError:
                matches = False
            if not matches:
                journal.add({"kind": "invalid", "path": item.rel})
                journal.close()
                raise LocalImportError(
                    "STAGED_COPY_MISMATCH", "a staged copy does not match the original; run the command again to redo it", import_id)
            progress.tick(f"Verifying: {ordinal:,} of {len(selection.kept):,} files")

    staged_files = sum(1 for item in selection.kept if item.rel in done)
    staged_bytes = sum(item.size for item in selection.kept if item.rel in done)
    journal.add({"kind": "done", "files": staged_files, "bytes": staged_bytes, "finished": datetime.now(UTC).isoformat()})
    journal.close()
    manifest = journals / f"{import_id}{_DONE_SUFFIX}"
    os.replace(journal.path, manifest)
    for rel, code in failures[:5]:
        progress.say(f"Left out while copying: {rel} ({code}).")
    return {
        "ok": True, "import_id": import_id, "collection": root.name, "start_path": f"{IMPORT_STAGE_DIR}/{import_id}",
        "include_hidden": include_hidden, "staged": {"files": staged_files, "bytes": staged_bytes},
        "failed_while_copying": len(failures), **selection.summary(), "manifest": str(manifest),
        "staged_folder": str(staged_root),
    }


def _closing_lines(result: dict) -> list[str]:
    staged = result["staged"]
    start = f'adoption_studio start with path "{result["start_path"]}"'
    if result["include_hidden"]:
        start += " and include_hidden=true"
    return [
        f"Staged {files_phrase(staged['files'])}, {human_bytes(staged['bytes'])}, in {result['staged_folder']}.",
        "This is a second copy of the folder on this disk. No Exomem command discards it: "
        f"after adoption finishes, delete {Path(result['staged_folder']).parent} to free the space.",
        f"Manifest: {result['manifest']}",
        f"Next: run {start}.",
    ]


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
    return 0
