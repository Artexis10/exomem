"""In-place conversion of a populated store collection from items mode to summary mode (design §17).

The cutover converts one collection at a time, on the service that serves the vault:

1. One store transaction commits the summary manifest, retires the item projection state,
   keeps each row's former view path as its retired alias, queues the summary pages and
   records the conversion with the item files it found (``CollectionWriter.convert_to_summary``).
2. ``_archive`` copies those files under the vault's state root and verifies every copy.
3. ``_remove`` unlinks each vault file whose bytes equal its verified copy.

The store record names the phase reached, so a re-run after a crash resumes there and repeats
nothing that is done. A file leaves the vault only after its archived copy verifies.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .. import held_fs, record_formats, state_paths, vault
from .. import structured_collections as collections
from . import summary
from .connection import CollectionStoreError

# This namespace, its record version and its phases are a closed storage protocol, not vocabulary.
PREFIX = "collection-conversion:"
_END = PREFIX[:-1] + chr(ord(PREFIX[-1]) + 1)
COMMITTED, ARCHIVED = "committed", "archived"
#: The reserved tree under the vault's state root (reserved_paths "collection-archive").
ARCHIVE_TREE = ".collection-archive"
_MANIFEST = "manifest.json"


def unsupported(strategy, view_mode):
    return collections.CollectionError(
        "COLLECTION_CONVERSION_UNSUPPORTED",
        f"only an items-mode markdown-items collection converts to summary mode; this one is "
        f"{strategy} in {view_mode} mode")


def outstanding(reason):
    return collections.CollectionError(
        "COLLECTION_CONVERSION_OUTSTANDING",
        f"{reason}; retry the conversion once edit-back and publication have settled")


def disclosure_change(names):
    return collections.CollectionError(
        "COLLECTION_CONVERSION_DISCLOSURE_CHANGE",
        "summary rows take the collection path for policy, which would change who can see or change a row "
        f"under {', '.join(names)}; widen that scope or rule to the collection path, then retry",
        {"changes": list(names)})


def summary_manifest(text):
    """The stored manifest with ``storage.strategy: sqlite`` and ``view_mode: summary``; every other byte kept."""
    frontmatter, _, _ = vault.parse_frontmatter(text, strict=True)
    storage = {**frontmatter["storage"], "strategy": collections.SQLITE_STORAGE}
    return record_formats.splice_markdown_frontmatter(text, {"storage": storage, "view_mode": summary.SUMMARY})


def record(conn, cid):
    """The pending conversion of ``cid``, or None when none is pending."""
    row = conn.execute("SELECT value FROM store_meta WHERE key=?", (PREFIX + cid,)).fetchone()
    if row is None:
        return None
    value = json.loads(row[0])
    if value.get("version") != 1 or value.get("phase") not in (COMMITTED, ARCHIVED):
        raise CollectionStoreError("COLLECTION_CONVERSION_RECORD_INVALID", "the conversion record cannot be read")
    return value


def pending(conn):
    """The collections whose item files a conversion has not yet removed."""
    return frozenset(key[len(PREFIX):] for (key,) in conn.execute(
        "SELECT key FROM store_meta WHERE key>=? AND key<?", (PREFIX, _END)))


def put(execute, cid, value):
    if value is None:
        execute("DELETE FROM store_meta WHERE key=?", (PREFIX + cid,))
    else:
        execute("INSERT INTO store_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (PREFIX + cid, json.dumps({"version": 1, **value}, sort_keys=True, separators=(",", ":"))))


def read_files(root, paths):
    """Each vault file's bytes through the held vault root, or None when it is missing."""
    found = {}
    with held_fs.acquire(Path(root)).require() as fs:
        for path in paths:
            parent = fs.parent(str(Path(path).parent))
            if parent.error is not None and parent.error.code == "MISSING":
                found[path] = None
                continue
            with parent.require() as directory:
                opened = fs.file(directory, Path(path).name)
                if opened.error is not None and opened.error.code == "MISSING":
                    found[path] = None
                    continue
                with opened.require() as file:
                    found[path] = fs.read(file).require()
    return found


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def archive_folder(root, cid, txn_id):
    return state_paths.vault_state_dir(Path(root)) / ARCHIVE_TREE / cid / str(txn_id)


def _verified(folder):
    """The archive's files by path and digest when its manifest and every copy verify, else None."""
    try:
        listed = json.loads((folder / _MANIFEST).read_bytes())
        files = {entry["path"]: entry["sha256"] for entry in listed["files"]}
        if all(_sha((folder / "files" / path).read_bytes()) == sha for path, sha in files.items()):
            return files
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _durable(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.tmp")
    with open(staged, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staged, path)
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _archive(root, cid, value):
    """Copy the recorded item files into the archive and verify every copy; return what it holds.

    A file whose bytes are no longer the bytes the store published stays out of the archive and
    in the vault, where the census holds it as an unbound view. The manifest is written last, so
    an archive without a verified manifest is incomplete and is built again.
    """
    folder = archive_folder(root, cid, value["txn_id"])
    done = _verified(folder)
    if done is not None:
        return done
    held = {}
    for path, raw in read_files(root, sorted(value["files"])).items():
        if raw is not None and _sha(raw) == value["files"][path]:
            _durable(folder / "files" / path, raw)
            held[path] = value["files"][path]
    _durable(folder / _MANIFEST, json.dumps({
        "version": 1, "collection_id": cid, "txn_id": value["txn_id"],
        "files": [{"path": path, "sha256": sha} for path, sha in sorted(held.items())],
    }, indent=1, sort_keys=True).encode())
    if _verified(folder) != held:
        raise CollectionStoreError("COLLECTION_CONVERSION_ARCHIVE_UNVERIFIED",
                                   f"the archive at {folder} does not hold the item files it copied")
    return held


def _remove(root, folder, archived):
    """Unlink each archived vault file while its bytes equal its verified copy; return the paths kept.

    The store retired these paths as views in the conversion commit, so edit-back reads the
    removal as no view change. A file edited since its copy stays in the vault for the census.
    """
    kept = []
    with held_fs.acquire(Path(root)).require() as fs:
        for path, sha in sorted(archived.items()):
            if _sha((folder / "files" / path).read_bytes()) != sha:
                raise CollectionStoreError("COLLECTION_CONVERSION_ARCHIVE_UNVERIFIED",
                                           f"the archived copy of {path} no longer verifies")
            parent = fs.parent(str(Path(path).parent), access="mutate")
            if parent.error is not None and parent.error.code == "MISSING":
                continue
            with parent.require() as directory:
                opened = fs.file(directory, Path(path).name, access="mutate")
                if opened.error is not None and opened.error.code == "MISSING":
                    continue
                with opened.require() as file:
                    if _sha(fs.read(file).require()) != sha:
                        kept.append(path)
                        continue
                    fs.unlink(file).require()
                fs.flush_directory(directory).require()
    return kept


def convert(vault_root, collection, *, why):
    """Convert one items-mode collection to summary mode, or finish a conversion a crash cut short.

    Runs on the store thread of the service that serves the vault. The receipt names the
    outcome, the phase reached and the archive that holds the retired item files.
    """
    from .preview import bound_writer
    from .runtime import _server

    root = Path(vault_root).resolve()
    if bound_writer(root) is not None:
        return _convert(root, collection, why)
    server = _server(root)
    if server is None:
        raise CollectionStoreError("COLLECTION_STORE_UNAVAILABLE",
                                   "only the service serving this vault converts a collection")
    return server.call(lambda: _convert(root, collection, why))


def _convert(root, collection, why):
    from .. import state_migration
    from .preview import _mutate, bound_writer

    writer = bound_writer(root)
    writer._facade_profile = None  # A store-level operation answers to no tool profile.
    cid, view_mode, value = writer.conversion_state(collection)
    receipt = None
    if value is None:
        if view_mode == summary.SUMMARY:
            return {"outcome": "already_converted", "collection_id": cid}

        def floor():
            # Before the commit, so no release without the floor serves the sqlite manifest.
            state_migration.record_collections_sqlite_only(
                root, authority_check=lambda: writer.handle.require_write_authority() is None)

        receipt = _mutate(root, writer.convert_to_summary, collection, why=why, before_commit=floor)
        cid, view_mode, value = writer.conversion_state(cid)
    folder = archive_folder(root, cid, value["txn_id"])
    if value["phase"] == COMMITTED:
        value = {**value, "phase": ARCHIVED, "archived": _archive(root, cid, value)}
        _mutate(root, writer.advance_conversion, cid, value)
    kept = _remove(root, folder, value["archived"])
    _mutate(root, writer.advance_conversion, cid, None)
    return {"outcome": "converted", "phase": "complete", "collection_id": cid, "archive": str(folder),
            "archived": len(value["archived"]), "kept_in_vault": kept, "receipt": receipt}
