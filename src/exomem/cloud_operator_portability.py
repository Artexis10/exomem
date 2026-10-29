"""Prepare a vault-only operator export from exclusively restored, stopped backup roots."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from . import cell_init, held_fs, reserved_paths, state_migration, state_paths, vault_portability
from .collection_store import (
    authority,
    chain,
    connection,
    legacy_originals,
    ownership,
    replica,
    schema,
    snapshot,
)


def archive_members(vault: Path) -> list[Path]:
    from .kbdir import kb_dirname
    from .media_store import local_originals

    with local_originals(vault, (kb_dirname(),)):
        return _archive_members_local(vault)


def _archive_members_local(vault: Path) -> list[Path]:
    """Select canonical tar members and their parents through the shared classifier.

    Call tar.add with recursive=False: a discarded directory must not add its children.
    Reserved parents can contain canonical exceptions such as the replica and schema.
    """
    vault = Path(vault)
    members = {Path(".")}
    for relative in vault_portability._walk_regular_files(vault):
        path = Path(relative)
        if vault_portability.classify_artifact(relative).artifact_class is vault_portability.ArtifactClass.CANONICAL:
            members.update((path, *path.parents))
    for path in vault.rglob("*"):
        relative = path.relative_to(vault)
        if (path.is_dir() and vault_portability.classify_artifact(relative.as_posix()).artifact_class
                is vault_portability.ArtifactClass.CANONICAL):
            members.update((relative, *relative.parents))
    return sorted(members)


def prepare_export(
    vault: Path, host_root: Path, original_vault: Path, *,
    source_state_directory: Path | None = None, timeout_seconds: float,
) -> dict:
    """Replace only scratch's replica, retaining the stopped canonical source bytes.

    The operator supplies exclusive scratch and its reviewed original runtime placement.
    Neither a source path nor this preparation grants a lease or destination custody.
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout must be positive and finite")
    deadline = time.monotonic() + timeout_seconds

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError("COLLECTION_SNAPSHOT_DEADLINE")

    vault = Path(vault).absolute()
    host_root = Path(host_root).absolute()
    original_vault = Path(original_vault)
    if (not original_vault.is_absolute() or vault.is_relative_to(host_root) or host_root.is_relative_to(vault)
            or not vault.is_dir() or not host_root.is_dir()
            or vault.resolve() != vault or host_root.resolve() != host_root):
        raise ValueError("backup root binding is invalid")
    raw_marker = authority.read_marker(vault)
    if raw_marker is not None:
        authority.parse_marker(vault, raw_marker)
    try:
        relative = state_paths.vault_state_dir(original_vault).relative_to(cell_init.account_home())
    except ValueError:
        relative = None
    if source_state_directory is None:
        if relative is None:
            raise ValueError("reviewed source state directory is required")
        source_state_directory = host_root / relative
    source_state_directory = Path(source_state_directory).absolute()
    source_relative = source_state_directory.relative_to(host_root)
    if (source_state_directory.resolve() != source_state_directory
            or relative is not None and source_relative != relative):
        raise ValueError("source state binding is invalid")
    target = replica.replica_path(vault)
    with ExitStack() as stack:
        source_fs = stack.enter_context(held_fs.acquire(host_root).require())
        source_parent = stack.enter_context(source_fs.parent(source_relative.as_posix()).require())
        manifest_file = stack.enter_context(source_fs.file(source_parent, state_migration.MANIFEST_NAME).require())
        manifest_raw = source_fs.read(manifest_file).require()
        manifest = json.loads(manifest_raw)
        state_migration._validate_manifest(Path(state_migration.MANIFEST_NAME), manifest, vault_root=original_vault)
        if manifest["state"] != "complete":
            raise ValueError("source state migration is incomplete")
        source = source_fs.file(source_parent, connection.STORE_FILENAME)
        if source.ok:
            source.require().close()
        elif source.error.code == "MISSING":
            # A missing hint proves no absence; the reviewed family and enrollment must agree.
            if (raw_marker is not None or os.path.lexists(target)
                    or set(manifest["descriptors"]) & state_migration._store_owned_ids()):
                raise ValueError("canonical source is unavailable")
            for suffix in snapshot._COMPANIONS:
                companion = source_fs.file(source_parent, connection.STORE_FILENAME + suffix)
                if companion.ok:
                    companion.require().close()
                    raise ValueError("stopped source family is incomplete")
                if companion.error.code != "MISSING":
                    raise companion.error
            return {"ok": True, "store": "absent"}
        else:
            raise source.error
        vault_fs = stack.enter_context(held_fs.acquire(vault).require())
        target_parent = stack.enter_context(vault_fs.parent(
            target.parent.relative_to(vault).as_posix(), access="mutate").require())
        current = vault_fs.file(target_parent, target.name)
        if current.error is not None and current.error.code != "MISSING":
            raise current.error
        previous = stack.enter_context(current.require()) if current.ok else None
        previous_identity = None if previous is None else previous.identity
        previous_digest = None if previous is None else snapshot._file_sha256(previous.descriptor, check)
        work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".exomem-operator-store-", dir=vault.parent)))
        journal = source_fs.file(source_parent, connection.STORE_FILENAME + "-journal")
        if journal.ok:
            journal.require().close()
            raise ValueError("stopped source requires recovery")
        if journal.error.code != "MISSING":
            raise journal.error
        # SQLite's WAL protocol fixes these family suffixes; never open the stopped source.
        members = []
        for suffix in ("", "-wal"):
            found = source_fs.file(source_parent, connection.STORE_FILENAME + suffix)
            if suffix and found.error is not None and found.error.code == "MISSING":
                members.append((suffix, None, None, None))
                continue
            member = stack.enter_context(found.require())
            version = member.version()
            digest = snapshot._file_sha256(member.descriptor, check)
            members.append((suffix, member, version, digest))
            os.lseek(member.descriptor, 0, os.SEEK_SET)
            with (work / (connection.STORE_FILENAME + suffix)).open("xb") as output:
                while chunk := os.read(member.descriptor, 1024 * 1024):
                    check()
                    output.write(chunk)

        def unchanged():
            check()
            if authority.read_marker(vault) != raw_marker:
                raise ValueError("authority marker changed")
            source_fs.validate_directory(source_parent).require()
            with source_fs.file(source_parent, state_migration.MANIFEST_NAME).require() as named:
                if named.identity != manifest_file.identity or source_fs.read(named).require() != manifest_raw:
                    raise ValueError("source binding changed")
            for suffix, member, version, digest in members:
                found = source_fs.file(source_parent, connection.STORE_FILENAME + suffix)
                if member is None:
                    if found.ok:
                        found.require().close()
                        raise ValueError("source family changed")
                    if found.error.code != "MISSING":
                        raise found.error
                    continue
                with found.require() as named:
                    if (named.identity != member.identity or member.version() != version
                            or snapshot._file_sha256(member.descriptor, check) != digest):
                        raise ValueError("source family changed")

        unchanged()
        with snapshot.staged_store_snapshot(
            work / connection.STORE_FILENAME, directory=work, deadline=deadline,
            preserve_schema=True,
        ) as artifact:
            with vault_portability.checked_collection_replica(vault, source_path=artifact.path, check=check) as verified:
                canonical = verified.connection
                canonical_meta = legacy_originals.metadata(canonical)
                if previous is not None:
                    ownership_marker = (ownership.declarations(vault, canonical)[0] if raw_marker is None
                                        else authority.parse_marker(vault, raw_marker))
                    if ownership_marker["version"] == 1:
                        ownership_marker = authority.upgraded_marker(vault, canonical, ownership_marker)
                    with vault_portability.checked_collection_replica(
                        vault, ancestor_witness=True, check=check,
                    ) as old_verified:
                        old = old_verified.connection
                        authority.require_projection_ownership(old, ownership_marker, root=vault)
                        old_meta = legacy_originals.metadata(old)
                        relation = chain.head_relation(canonical, int(old_meta[schema.META_COMMIT_SEQ]),
                                                       old_meta.get(schema.META_STORE_HEAD_HASH))
                        old_lineage = json.loads(old_meta[schema.META_LINEAGE])
                        lineage = json.loads(canonical_meta[schema.META_LINEAGE])
                        # head_relation fixes these ancestry tokens; sequence alone proves nothing.
                        if (old_meta[schema.META_STORE_ID] != canonical_meta[schema.META_STORE_ID]
                                or relation not in {"same", "ancestor"} or old_lineage != lineage[:len(old_lineage)]):
                            raise ValueError("replica ancestry differs")
            unchanged()

            def chunks():
                digest = hashlib.sha256()
                with artifact.path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        check()
                        digest.update(chunk)
                        yield chunk
                if digest.hexdigest() != artifact.file_sha256:
                    raise ValueError("prepared snapshot changed")

            def validate_existing(file):
                unchanged()
                if snapshot._file_sha256(file.descriptor, check) != previous_digest:
                    raise held_fs.HeldFsError("IDENTITY_CHANGED", "held filesystem content changed")
                unchanged()

            with reserved_paths._subsystem_authority_scope("collection_store.replica"):
                held_fs.publish_bytes(
                    vault_fs, target_parent, target.name, held_fs.Chunks(chunks),
                    expected_identity=previous_identity, validate_existing=validate_existing,
                    prepare=lambda _file: unchanged(),
                ).require()
                vault_fs.flush_directory(target_parent).require()
            return {"ok": True, "store": "prepared", "commit_seq": artifact.commit_seq,
                    "head_hash": artifact.head_hash, "bytes": artifact.size_bytes}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default diagnostics echo argument values, which can contain private paths.
        raise ValueError("invalid operator export arguments")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="python -m exomem.cloud_operator_portability")
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--host-root", type=Path, required=True)
    parser.add_argument("--original-vault", type=Path, required=True)
    parser.add_argument("--source-state-directory", type=Path)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    try:
        args = parser.parse_args(argv)
        result = prepare_export(args.vault, args.host_root, args.original_vault,
                                source_state_directory=args.source_state_directory,
                                timeout_seconds=args.timeout_seconds)
    except (OSError, ValueError, sqlite3.Error, connection.CollectionStoreError, cell_init.CellInitError,
            chain.StoreChainError, held_fs.HeldFsError, state_migration.StateMigrationManifestError,
            vault_portability.PortabilityError):
        # Paths and database diagnostics can contain private backup content.
        result = {"ok": False, "error": "OPERATOR_EXPORT_REFUSED"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
