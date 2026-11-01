"""Dark, authority-bound publication of collection-store replicas."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from contextlib import ExitStack, closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import held_fs, reserved_paths
from ..cli_ops import OpError
from ..kbdir import kb_dirname
from . import authority, connection, legacy_originals, schema, snapshot, tokens

_OWNER = "collection_store.replica"
_DESCRIPTOR = "collection-replica"
_TOKEN = snapshot._TOKEN
_PENDING = schema.META_PENDING_REPLICA_PUBLICATION
_DIGEST = schema.META_LAST_PUBLISHED_REPLICA_SHA256
_PUBLISHED = schema.META_PUBLISHED_REPLICA_HEAD
_EPOCH = schema.META_PUBLISHED_REPLICA_EPOCH
_INTENT = schema.META_PUBLISHED_INTENT_SHA256


@dataclass(frozen=True, slots=True)
class PublicationResult:
    """The actual installed business head, or an honest unresolved outcome."""

    status: str
    commit_seq: int | None = None
    head_hash: str | None = None
    reason: str | None = None


def replica_path(vault_root: Path) -> Path:
    """The fixed vault-side replica; the live store remains external."""
    return Path(vault_root) / kb_dirname() / "_Collections" / connection.STORE_FILENAME


class _Retry(Exception):
    pass


class _Diverged(Exception):
    pass


@dataclass(slots=True)
class _Verification:
    file: held_fs.HeldFile
    parent: held_fs.HeldDirectory
    leaf: str
    version: held_fs.FileVersion
    digest: str | None = None
    fsynced: bool = False
    intent_loaded: bool = False
    intent_raw: str | None = None


def _metadata(writer: connection.WriterConnection) -> dict[str, str]:
    return legacy_originals.metadata(writer.connection)


def _head(metadata: dict[str, str]) -> dict:
    return {
        "store_id": metadata[schema.META_STORE_ID],
        "instance_id": metadata[schema.META_INSTANCE_ID],
        "commit_seq": int(metadata[schema.META_COMMIT_SEQ]),
        "head_hash": metadata.get(schema.META_STORE_HEAD_HASH),
    }


def _json(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _intent_digest(metadata: Mapping[str, str | None]) -> str | None:
    """The digest of the store's pending intent row, or None when it has none."""
    raw = metadata.get(authority.PENDING_CREATE)
    return None if raw is None else hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _published_metadata_current(metadata, *, copied_raw=None, raw_marker=None):
    """Completed local intent cleanup leaves the authentic replica bookkeeping intact."""
    if metadata.get(_EPOCH) != metadata.get(schema.META_LEASE_EPOCH):
        return False
    if _intent_digest(metadata) == metadata.get(_INTENT):
        return True
    if (metadata.get(authority.PENDING_CREATE) is not None or copied_raw is None
            or raw_marker is None or json.loads(metadata[_PUBLISHED]) != _head(metadata)
            or _intent_digest({authority.PENDING_CREATE: copied_raw}) != metadata.get(_INTENT)):
        return False
    authority.parse_pending_create(copied_raw)
    from .takeover import replica_adoptability

    return replica_adoptability(
        {authority.PENDING_CREATE: copied_raw}, raw_marker, intent_only=True,
    ) is None


def published_evidence(metadata):
    """The bookkeeping proving the installed replica carries this live head, epoch and intent, else None.

    A re-admission that finds the same evidence under the boundary, with the replica bytes
    it names, has nothing to publish. Any pending intent, divergence or foreign adoption
    record is the full publication path's to settle.
    """
    if (metadata.get(_PENDING) is not None or metadata.get(authority.PENDING_CREATE) is not None
            or metadata.get(schema.META_REPLICA_DIVERGENCE) is not None
            or metadata.get(schema.META_ADOPTED_FOREIGN_REPLICA) is not None
            or metadata.get(authority.MARKER_REQUIRED) != "1"
            or metadata.get(_DIGEST) is None
            # Published heads are written only as ``_json``; a malformed one is the full path's to judge.
            or metadata.get(_PUBLISHED) != _json(_head(metadata))
            or not _published_metadata_current(metadata)):
        return None
    return (_json(_head(metadata)), metadata[_DIGEST], metadata.get(_EPOCH), metadata.get(_INTENT))


def _read_copied_intent(filesystem, witness, *, digest, head, epoch, deadline=None, progress=None):
    """Read metadata from a private copy of the authenticated retained descriptor."""
    check = progress if progress is not None else lambda: None
    check()
    with reserved_paths.private_copy_directory() as private:
        with held_fs.acquire(private).require() as private_fs:
            with private_fs.parent(".").require() as parent:
                if witness.file.version() != witness.version:
                    raise _Retry("installed copy changed before private copy")
                filesystem.copy(
                    witness.file, parent, connection.STORE_FILENAME,
                    max_bytes=witness.version.size, deadline=deadline,
                ).require()
                check()
                if witness.file.version() != witness.version:
                    raise _Retry("installed copy changed during private copy")
                with private_fs.file(parent, connection.STORE_FILENAME).require() as copied:
                    version = copied.version()
                    if (version.size != witness.version.size
                            or snapshot._file_sha256(copied.descriptor, check) != digest
                            or copied.version() != version):
                        raise _Retry("private copy differs from the published replica")
                    reader, _ = connection._open_read_only(private / connection.STORE_FILENAME, 50)
                    try:
                        metadata = legacy_originals.metadata(reader)
                    finally:
                        reader.close()
                    with private_fs.file(parent, connection.STORE_FILENAME).require() as current:
                        if current.version() != version or copied.version() != version:
                            raise _Retry("private copy changed while reading metadata")
    check()
    try:
        copied_head = _head(metadata)
    except (KeyError, ValueError, TypeError) as error:
        raise _Retry("private copy metadata is invalid") from error
    if copied_head != head or metadata.get(schema.META_LEASE_EPOCH) != epoch:
        raise _Retry("private copy metadata differs from the published replica")
    return metadata.get(authority.PENDING_CREATE)


def file_writer_marker(root, acquired=None):
    """Prove current local publication and complete routing without admitting a store producer."""
    from ..writer_lease import CollectionStoreHead

    raw = authority.read_marker(root)
    if raw is None:
        return None
    marker = authority.parse_marker(root, raw)
    if marker["version"] != 2:
        return None
    with closing(connection.open_ownership_reader(connection.store_path(root))) as reader:
        reader.execute("BEGIN")
        metadata = legacy_originals.metadata(reader)
        head = CollectionStoreHead.from_json(_head(metadata))
        tail = reader.execute(
            "SELECT commit_seq,store_head_hash FROM txns ORDER BY commit_seq DESC LIMIT 1"
        ).fetchone()
        # Only current local published evidence relaxes the file fence; business heads omit intent changes.
        if ((tail or (0, None)) != (head.commit_seq, head.head_hash)
                or marker["store_id"] != head.store_id
                or authority.PENDING_CREATE in metadata or _PENDING in metadata
                or schema.META_REPLICA_DIVERGENCE in metadata):
            return None
        if {entry["collection_id"] for entry in marker["collections"]} != {
                row[0] for row in reader.execute("SELECT collection_id FROM collections")}:
            return None
        for entry in marker["collections"]:
            authority.require_selected(reader, marker, entry, root=root)
        digest = metadata.get(_DIGEST)
        tokens._hex64(digest, "published replica digest")
        if CollectionStoreHead.from_json(json.loads(metadata[_PUBLISHED])) != head:
            return None
        if acquired is not None and (
                not acquired.granted or acquired.collection_store_head != head
                or acquired.required_collection_store_capability != marker["collection_store_fence"]["capability"]
                or acquired.collection_store_fence_generation != marker["collection_store_fence"]["generation"]):
            return None
        relative = replica_path(root).relative_to(root)
        with held_fs.acquire(root).require() as filesystem:
            with filesystem.parent(relative.parent.as_posix()).require() as parent:
                with filesystem.file(parent, relative.name).require() as file:
                    version = file.version()
                    if snapshot._file_sha256(file.descriptor) != digest or file.version() != version:
                        return None
                    # A publisher older than the epoch and intent bookkeeping recorded neither key, so
                    # nothing can bind them; the digest-checked copy at the current head is its proof.
                    # The store's own producer republishes it and stamps the epoch on its first publication.
                    legacy = _EPOCH not in metadata and _INTENT not in metadata
                    copied_raw = None
                    if not legacy and not _published_metadata_current(metadata):
                        copied_raw = _read_copied_intent(
                            filesystem, _Verification(file, parent, relative.name, version),
                            digest=digest, head=_head(metadata), epoch=metadata.get(_EPOCH),
                        )
                    if not legacy and not _published_metadata_current(
                            metadata, copied_raw=copied_raw, raw_marker=raw):
                        return None
                    filesystem.validate_directory(parent).require()
                    with filesystem.file(parent, relative.name).require() as current:
                        if (not held_fs._same_file_identity(file.identity, current.identity)
                                or current.version() != version):
                            return None
        return raw if authority.read_marker(root) == raw else None


class _Publisher:
    def __init__(self, root, writer, authority_check, deadline, cancelled):
        self.root = Path(root).resolve()
        self.writer = writer
        self.authority_check = authority_check
        self.deadline = deadline
        self.cancelled = cancelled
        self.metadata = _metadata(writer)
        self.identity = _head(self.metadata)
        self.epoch = self.metadata.get(schema.META_LEASE_EPOCH)
        self.pending_raw = self.metadata.get(_PENDING)
        self.filesystem = None
        self.kb_parent = None
        self.parent = None
        self.workspace = None
        self.workspace_relative = None
        self.relative = replica_path(self.root).parent.relative_to(self.root)
        self.steps = None
        self.step = None
        self.patience = 0.0
        self.verifications = {}
        self.locations = {}

    def enter_step(self):
        writer, self.authority_check = self.steps.enter_context(
            self.step(self.patience, self.cancelled)
        )
        self.writer = writer
        self.steps.enter_context(reserved_paths._identity_coordination_scope(
            self.root, descriptor_ids=(_DESCRIPTOR,)
        ))

    def forget(self, leaf, parent=None):
        witness = self.verifications.pop((id(parent or self.parent), leaf), None)
        if witness is not None:
            witness.file.close()

    def close_verifications(self):
        for witness in self.verifications.values():
            witness.file.close()
        self.verifications.clear()

    @contextmanager
    def outside(self, *, allow_diverged=False):
        """Release exclusion for verification, then compare its complete observation."""
        self.check(allow_diverged=allow_diverged)
        metadata = _metadata(self.writer)
        # Business commits and pending creates may advance while the copy is verified.
        fixed = {key: metadata.get(key) for key in (
            _PENDING, _DIGEST, _PUBLISHED, _EPOCH, _INTENT, schema.META_REPLICA_DIVERGENCE,
            schema.META_ADOPTED_FOREIGN_REPLICA,
        )}
        topology = [(parent, set(self.filesystem.iter_names(parent)))
                    for parent in (self.parent, self.workspace) if parent is not None]
        if self.steps is not None:
            self.steps.close()
            self.writer = self.authority_check = None
        yield
        if self.steps is not None:
            self.enter_step()
        self.check(allow_diverged=allow_diverged)
        current = _metadata(self.writer)
        if any(current.get(key) != value for key, value in fixed.items()):
            raise _Retry("publication metadata changed during verification")
        if any(set(self.filesystem.iter_names(parent)) != names for parent, names in topology):
            raise _Retry("publication names changed during verification")
        for witness in self.verifications.values():
            with self.filesystem.file(witness.parent, witness.leaf).require() as current:
                if current.version() != witness.version or witness.file.version() != witness.version:
                    raise _Retry("publication file changed during verification")

    def check(self, *, allow_diverged=False):
        if allow_diverged:
            self.writer.require_write_authority(allow_diverged=True)
        else:
            self.writer.require_write_authority()
        if not self.authority_check():
            raise _Retry("publication authority was lost")
        if self.writer.path != connection.store_path(self.root).resolve():
            raise _Retry("live store binding changed")
        if self.cancelled is not None and self.cancelled():
            raise _Retry("publication was cancelled")
        if time.monotonic() >= self.deadline:
            raise _Retry("publication deadline elapsed")
        current = _metadata(self.writer)
        if any(current[key] != self.identity[key] for key in ("store_id", "instance_id")):
            raise _Retry("store identity changed")
        if current.get(schema.META_LEASE_EPOCH) != self.epoch:
            raise _Retry("lease epoch changed")
        if current.get(_PENDING) != self.pending_raw:
            raise _Retry("publication intent changed")
        for parent in (self.kb_parent, self.parent, self.workspace):
            if parent is not None:
                self.filesystem.validate_directory(parent).require()

    def validate_head(self, value):
        if not isinstance(value, dict):
            raise ValueError("invalid published head")
        for key in ("store_id", "instance_id"):
            snapshot._uuid(value[key])
            if value[key] != self.identity[key]:
                raise ValueError("publication identity is foreign")
        sequence, head = value["commit_seq"], value["head_hash"]
        snapshot._head(sequence, head)
        current = _head(_metadata(self.writer))
        if sequence > current["commit_seq"]:
            raise ValueError("publication is ahead of live store")
        if sequence:
            found = self.writer.connection.execute(
                "SELECT store_head_hash FROM txns WHERE commit_seq=?", (sequence,)
            ).fetchone()
            if found != (head,):
                raise ValueError("publication head is not an ancestor")
        return {key: value[key] for key in self.identity}

    def pending(self):
        if self.pending_raw is None:
            return None
        try:
            value = json.loads(self.pending_raw)
            fields = {
                "version",
                "phase",
                "token",
                "workspace_leaf",
                "workspace_identity",
                "stage_leaf",
                "store_id",
                "instance_id",
                "lease_epoch",
            }
            if value.get("phase") == "ready":
                fields |= {"sha256", "commit_seq", "head_hash"}
            elif value.get("phase") != "staging":
                raise ValueError("invalid publication phase")
            if set(value) != fields:
                raise ValueError("invalid publication intent")
            if type(value["version"]) is not int or value["version"] != 2:
                raise ValueError("unsupported publication intent")
            if not isinstance(value["token"], str) or not _TOKEN.fullmatch(value["token"]):
                raise ValueError("invalid publication token")
            if value["workspace_leaf"] != f".exomem-collection-replica-work-{value['token']}":
                raise ValueError("invalid owned workspace")
            if value["stage_leaf"] != f"{snapshot._PREFIX}{value['token']}.sqlite":
                raise ValueError("invalid owned stage")
            for key in ("store_id", "instance_id"):
                if value[key] != self.identity[key]:
                    raise ValueError("publication identity is foreign")
            identity = value["workspace_identity"]
            if identity is not None:
                if not isinstance(identity, dict) or set(identity) != {
                    "device",
                    "inode",
                    "kind",
                    "link_count",
                }:
                    raise ValueError("invalid workspace identity")
                if identity["kind"] != "directory" or any(
                    type(identity[key]) is not int or identity[key] < 0
                    for key in ("device", "inode", "link_count")
                ):
                    raise ValueError("invalid workspace identity")
            if value["phase"] == "ready":
                if identity is None:
                    raise ValueError("ready workspace identity is missing")
                tokens._hex64(value["sha256"], "replica digest")
                self.validate_head(value)
            if value["lease_epoch"] != self.epoch:
                raise _Retry("pending publication belongs to another lease epoch")
            return value
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            self.diverge("invalid pending publication")
            raise _Diverged from error

    def published(self):
        metadata = _metadata(self.writer)
        digest, raw = metadata.get(_DIGEST), metadata.get(_PUBLISHED)
        if digest is None and raw is None:
            return None, None
        try:
            tokens._hex64(digest, "published replica digest")
            if raw is None:
                # Only a tenure adopted at this head knows the replica's exact bytes but
                # not its own published head; it must republish before reporting one.
                entry = json.loads(metadata[schema.META_LINEAGE])[-1]
                if (entry["instance_id"] != metadata[schema.META_INSTANCE_ID]
                        or entry["adopted_from"] is None
                        or entry["adopted_at_commit_seq"] != int(metadata[schema.META_COMMIT_SEQ])):
                    raise ValueError("a published digest needs its published head")
                return digest, None
            return digest, self.validate_head(json.loads(raw))
        except (ValueError, TypeError, LookupError, AttributeError) as error:
            self.diverge("invalid published replica head")
            raise _Diverged from error

    def bookkeeping(self, values):
        self.check()
        with self.writer.transaction() as conn:
            self.check()
            for key, value in values.items():
                before = conn.total_changes
                if value is None:
                    conn.execute("DELETE FROM store_meta WHERE key=?", (key,))
                else:
                    conn.execute(
                        "INSERT INTO store_meta(key,value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value),
                    )
                # Only this known metadata statement is managed; unrelated SQL must still invalidate reuse.
                self.writer.release_cache.account(conn.total_changes - before)
        if _PENDING in values:
            self.pending_raw = values[_PENDING]
        self.metadata = _metadata(self.writer)

    def diverge(self, reason, *, foreign=None, leaf=None, parent=None):
        marker = {"reason": reason}
        if foreign is not None:
            marker.update(self.aside(foreign, leaf, parent))
        self.bookkeeping({schema.META_REPLICA_DIVERGENCE: _json(marker)})
        if foreign is not None:
            # Durable divergence fences business writes before preservation.
            self.preserve(foreign, leaf, parent, marker)
        raise _Diverged(reason)

    def aside(self, foreign, leaf, parent):
        return {
            "source_leaf": self.leaf_relative(leaf, parent).as_posix(),
            "foreign_leaf": f".foreign-{self.identity['instance_id']}-{secrets.token_hex(16)}",
            "sha256": self.digest(foreign),
        }

    def preserve(self, foreign, leaf, parent, marker):
        """Keep foreign bytes aside, no-clobber; this seam retains every other authority check."""
        self.check(allow_diverged=True)
        self.filesystem.rename(foreign, self.parent, marker["foreign_leaf"], replace=False).require()
        self.forget(leaf, parent)
        self.check(allow_diverged=True)
        with self.file(marker["foreign_leaf"], required=True) as preserved:
            if (
                not held_fs._same_file_identity(foreign.identity, preserved.identity)
                or self.digest(preserved, allow_diverged=True) != marker["sha256"]
            ):
                raise _Diverged("foreign preservation identity changed; all bytes retained")
            self.record(marker["foreign_leaf"], preserved.identity)
        self.record(leaf, None, parent=parent)
        for directory in (self.parent, self.workspace):
            if directory is not None:
                self.check(allow_diverged=True)
                self.filesystem.flush_directory(directory).require()

    def file(self, leaf, *, required=False, parent=None, access="mutate"):
        result = self.filesystem.file(parent or self.parent, leaf, access=access)
        if result.error is not None and result.error.code == "MISSING":
            if required:
                raise _Retry("publication file disappeared before verification")
            return None
        file = result.require()
        if file.identity.link_count != 1:
            file.close()
            self.diverge("replica file has ambiguous identity")
        self.locations[id(file)] = (parent or self.parent, leaf)
        return file

    def digest(self, file, *, allow_diverged=False):
        """Keep the verified inode until a coordinated replacement handle compares equal."""
        parent, leaf = self.locations[id(file)]
        key = (id(parent), leaf)
        witness = self.verifications.get(key)
        version = file.version()
        if witness is not None:
            if witness.version != version:
                raise _Retry("publication file changed after verification")
            if witness.digest is not None:
                return witness.digest
        else:
            retained = self.filesystem.file(parent, leaf).require()
            if retained.version() != version:
                retained.close()
                raise _Retry("publication name changed before verification")
            witness = _Verification(retained, parent, leaf, version)
            self.verifications[key] = witness
        with self.outside(allow_diverged=allow_diverged):
            found = snapshot._file_sha256(witness.file.descriptor, self._progress)
            if witness.file.version() != version:
                raise _Retry("publication file changed while hashing")
            witness.digest = found
        return found

    def _progress(self):
        if self.cancelled is not None and self.cancelled():
            raise _Retry("publication was cancelled")
        if time.monotonic() >= self.deadline:
            raise _Retry("publication deadline elapsed")

    def leaf_relative(self, leaf, parent=None):
        return (
            self.workspace_relative / leaf
            if parent is self.workspace and parent is not None
            else self.relative / leaf
        )

    def record(self, leaf, identity, *, parent=None):
        reserved_paths._replace_published_owner_path(
            self.root, _DESCRIPTOR, self.leaf_relative(leaf, parent), identity
        )

    def remove(self, leaf, expected):
        if self.workspace is None:
            return
        file = self.file(leaf, parent=self.workspace)
        if file is None:
            return
        with file:
            if self.digest(file) != expected:
                self.diverge(
                    "unexpected publication cleanup content",
                    foreign=file,
                    leaf=leaf,
                    parent=self.workspace,
                )
            self.check()
            self.filesystem.unlink(file).require()
            self.forget(leaf, self.workspace)
        self.record(leaf, None, parent=self.workspace)

    def flush(self):
        self.check()
        self.filesystem.flush_directory(self.parent).require()
        if self.workspace is not None:
            self.check()
            self.filesystem.flush_directory(self.workspace).require()

    def rename(self, file, leaf, *, parent=None, capture=False):
        parent = parent or self.parent
        self.check()
        result = self.filesystem.rename(file, parent, leaf, replace=False)
        if result.error is not None and result.error.code == "DESTINATION_EXISTS":
            unexpected = self.file(leaf, parent=parent)
            if unexpected is not None:
                with unexpected:
                    self.diverge(
                        "publication destination arrived or collided",
                        foreign=unexpected,
                        leaf=leaf,
                        parent=parent,
                    )
            self.diverge("publication destination arrived or collided")
        result.require()
        source_parent, source_leaf = self.locations[id(file)]
        self.forget(source_leaf, source_parent)
        with self.file(leaf, required=True, parent=parent) as installed:
            if not capture and not held_fs._same_file_identity(file.identity, installed.identity):
                self.diverge(
                    "publication identity changed during rename",
                    foreign=installed,
                    leaf=leaf,
                    parent=parent,
                )
            self.record(leaf, installed.identity, parent=parent)

    def open_workspace(self, pending, *, create=False):
        self.check()
        self.workspace_relative = self.relative / pending["workspace_leaf"]
        result = self.filesystem.parent(
            self.workspace_relative.as_posix(), create=create, exclusive=create, access="mutate"
        )
        if result.error is not None and result.error.code == "MISSING" and not create:
            return
        self.workspace = result.require()
        identity = self.workspace.identity
        recorded = pending["workspace_identity"]
        if identity.device != self.parent.identity.device or (
            recorded is not None
            and not held_fs._same_file_identity(identity, held_fs.StableIdentity(**recorded))
        ):
            raise _Retry("publication workspace identity changed")
        if recorded is None:
            if set(self.filesystem.iter_names(self.workspace)):
                raise _Retry("unrecorded publication workspace is not empty")
            self.flush()
            pending["workspace_identity"] = asdict(identity)
            self.bookkeeping({_PENDING: _json(pending)})
        self.record(pending["workspace_leaf"], identity)

    def clean_workspace(self, pending, *, staging=False):
        if self.workspace is None:
            self.flush()
            return
        names = set(self.filesystem.iter_names(self.workspace))
        if staging:
            allowed = {pending["stage_leaf"] + suffix for suffix in ("", *snapshot._COMPANIONS)}
            if names - allowed:
                raise _Retry("publication workspace contains unknown entries")
            for leaf in names:
                self.check()
                with self.file(leaf, required=True, parent=self.workspace) as file:
                    self.filesystem.unlink(file).require()
                self.record(leaf, None, parent=self.workspace)
        elif names:
            raise _Retry("publication workspace cleanup is incomplete")
        self.flush()
        self.check()
        self.filesystem.unlink_directory(self.workspace).require()
        self.workspace.close()
        self.workspace = None
        self.record(pending["workspace_leaf"], None)
        self.flush()

    def flush_installed(self, expected):
        leaf = connection.STORE_FILENAME
        with self.file(leaf, required=True) as installed:
            if self.digest(installed) != expected:
                with self.file(leaf, required=True) as foreign:
                    self.diverge("installed replica changed", foreign=foreign, leaf=leaf)
            witness = self.verifications[(id(self.parent), leaf)]
            if not witness.fsynced:
                with self.file(leaf, required=True, access="write") as flushable:
                    if flushable.version() != witness.version:
                        raise _Retry("installed replica changed before flush")
                    with self.outside():
                        os.fsync(flushable.descriptor)
                        if flushable.version() != witness.version:
                            raise _Retry("installed replica changed during flush")
                        witness.fsynced = True
            self.check()
            self.filesystem.flush_directory(self.parent).require()
            with self.file(leaf, required=True) as current:
                if (
                    not held_fs._same_file_identity(installed.identity, current.identity)
                    or current.version() != witness.version
                ):
                    raise _Retry("installed replica changed during flush")
                self.record(leaf, current.identity)

    def resolve(self, pending, *, finish_staging=None, artifact=None):
        if self.workspace is None:
            self.open_workspace(pending)
        if pending["phase"] == "staging":
            self.clean_workspace(pending, staging=True)
            self.bookkeeping({_PENDING: None})
            return None
        predecessor, _ = self.published()
        aside_leaf = f".exomem-collection-replica-aside-{pending['token']}"
        target_leaf = connection.STORE_FILENAME
        aside = stage = None
        with ExitStack() as readers:
            target = self.file(target_leaf)
            if target is not None:
                readers.enter_context(target)
            if self.workspace is not None:
                if set(self.filesystem.iter_names(self.workspace)) - {
                    aside_leaf,
                    pending["stage_leaf"],
                }:
                    raise _Retry("publication workspace contains unknown entries")
                aside = self.file(aside_leaf, parent=self.workspace)
                if aside is not None:
                    readers.enter_context(aside)
                stage = self.file(pending["stage_leaf"], parent=self.workspace)
                if stage is not None:
                    readers.enter_context(stage)
            target_digest = self.digest(target) if target is not None else None
            if aside is not None and (predecessor is None or self.digest(aside) != predecessor):
                self.diverge(
                    "unexpected predecessor aside",
                    foreign=aside,
                    leaf=aside_leaf,
                    parent=self.workspace,
                )
            if stage is not None and self.digest(stage) != pending["sha256"]:
                self.diverge(
                    "unexpected publication stage",
                    foreign=stage,
                    leaf=pending["stage_leaf"],
                    parent=self.workspace,
                )
            if target_digest == pending["sha256"]:
                self.flush()
            else:
                if target is not None and target_digest != predecessor:
                    self.diverge("unexpected replica target", foreign=target, leaf=target_leaf)
                if target is not None and aside is not None:
                    self.diverge("publication aside collision")
                if target is None and predecessor is not None and aside is None:
                    self.diverge("published predecessor disappeared")
                if stage is None:
                    if target is None and aside is not None:
                        self.rename(aside, target_leaf)
                        self.record(aside_leaf, None, parent=self.workspace)
                        self.flush_installed(predecessor)
                    readers.close()
                    if finish_staging is not None:
                        finish_staging()
                    self.clean_workspace(pending)
                    self.bookkeeping({_PENDING: None})
                    return None
                if target is not None:
                    self.rename(target, aside_leaf, parent=self.workspace, capture=True)
                    self.record(target_leaf, None)
                    # Hash the actual displaced name: a pre-rename reader can
                    # still refer to the old inode after a sync replacement.
                    with self.file(aside_leaf, required=True, parent=self.workspace) as displaced:
                        if self.digest(displaced) != predecessor:
                            self.diverge(
                                "displaced replica was replaced",
                                foreign=displaced,
                                leaf=aside_leaf,
                                parent=self.workspace,
                            )
                    self.flush()
                self.rename(stage, target_leaf)
                self.record(pending["stage_leaf"], None, parent=self.workspace)
                self.flush()
        # Windows deletion completes only after every reader and snapshot owner closes.
        if finish_staging is not None:
            finish_staging()
        self.remove(pending["stage_leaf"], pending["sha256"])
        if predecessor is not None:
            self.remove(aside_leaf, predecessor)
        self.clean_workspace(pending)
        # A matching replacement after cleanup still needs its own file flush.
        self.flush_installed(pending["sha256"])
        head = {key: pending[key] for key in self.identity}
        # A business commit can change the live intent while verification runs.
        # Only the installed copy proves which intent a successor will read.
        self.bookkeeping({_DIGEST: pending["sha256"], _PUBLISHED: _json(head),
                          _EPOCH: pending["lease_epoch"], _PENDING: None,
                          _INTENT: _intent_digest({authority.PENDING_CREATE: self.copied_intent(pending, artifact)})})
        return head

    @contextmanager
    def bound(self):
        """Hold the replica directory under this subsystem's reserved-path authority."""
        with (
            reserved_paths._subsystem_authority_scope(_OWNER),
            ExitStack() as coordination,
            held_fs.acquire(self.root).require() as filesystem,
            filesystem.parent(kb_dirname(), access="flush").require() as kb_parent,
        ):
            if self.steps is None:
                coordination.enter_context(reserved_paths._identity_coordination_scope(
                    self.root, descriptor_ids=(_DESCRIPTOR,)
                ))
            self.filesystem = filesystem
            self.kb_parent = kb_parent
            self.check()
            with filesystem.parent(
                self.relative.as_posix(), create=True, access="mutate"
            ).require() as parent:
                self.parent = parent
                try:
                    self.check()
                    filesystem.flush_directory(kb_parent).require()
                    reserved_paths._replace_published_owner_path(
                        self.root, _DESCRIPTOR, self.relative, parent.identity
                    )
                    yield
                finally:
                    self.close_verifications()
                    if self.workspace is not None:
                        self.workspace.close()
                        self.workspace = None
                    self.parent = self.kb_parent = self.filesystem = None

    def copied_intent(self, pending, artifact):
        """Bind pending-create metadata to the artifact or an isolated held-descriptor copy."""
        leaf = connection.STORE_FILENAME
        with self.file(leaf, required=True) as installed:
            if self.digest(installed) != pending["sha256"]:
                raise _Retry("installed copy changed before reading its intent")
            witness = self.verifications[(id(self.parent), leaf)]
            if artifact is not None:
                if (artifact.file_sha256 != pending["sha256"]
                        or artifact.lease_epoch != pending["lease_epoch"]
                        or any(getattr(artifact, key) != pending[key] for key in self.identity)):
                    raise _Retry("snapshot metadata differs from the ready intent")
                witness.intent_raw = artifact.pending_create_raw
                witness.intent_loaded = True
            if not witness.intent_loaded:
                with self.outside():
                    witness.intent_raw = _read_copied_intent(
                        self.filesystem, witness, digest=pending["sha256"],
                        head={key: pending[key] for key in self.identity}, epoch=pending["lease_epoch"],
                        deadline=self.deadline, progress=self._progress,
                    )
                    witness.intent_loaded = True
            return witness.intent_raw

    def abandon(self, intent, predecessor):
        """Remove an adopted-over tenure's publication workspace: bounded recovery (A4).

        Only that intent's own stage family and an aside holding ``predecessor`` (the old
        tenure's own replica) are removed. Anything else keeps the workspace in place and
        returns why; None means it is gone or never existed.
        """
        if not isinstance(intent.get("token"), str) or not _TOKEN.fullmatch(intent["token"]) or (
                intent.get("workspace_leaf") != f".exomem-collection-replica-work-{intent['token']}"):
            return "the abandoned intent names no owned workspace; nothing was removed"
        self.workspace_relative = self.relative / intent["workspace_leaf"]
        result = self.filesystem.parent(self.workspace_relative.as_posix(), access="mutate")
        if result.error is not None and result.error.code == "MISSING":
            return None
        self.workspace = result.require()
        aside = f".exomem-collection-replica-aside-{intent['token']}"
        names = set(self.filesystem.iter_names(self.workspace))
        stage = {f"{snapshot._PREFIX}{intent['token']}.sqlite" + suffix for suffix in ("", *snapshot._COMPANIONS)}
        if names - stage - {aside}:
            return "the abandoned workspace holds unknown entries; it was kept"
        if aside in names:
            with self.file(aside, required=True, parent=self.workspace) as file:
                if predecessor is None or self.digest(file) != predecessor:
                    return "the abandoned workspace holds a replica this store never published; it was kept"
        for leaf in sorted(names):
            self.check()
            try:
                with self.file(leaf, required=True, parent=self.workspace) as file:
                    self.filesystem.unlink(file).require()
            finally:
                self.forget(leaf, self.workspace)
            self.record(leaf, None, parent=self.workspace)
        self.flush()
        self.check()
        self.filesystem.unlink_directory(self.workspace).require()
        self.workspace.close()
        self.workspace = None
        self.record(intent["workspace_leaf"], None)
        self.flush()
        return None

    def carries_metadata(self):
        """Whether the installed replica holds the current pending intent and lease epoch.

        An intent (an export's above all) can change without a commit, so a published head
        alone does not prove that a host adopting the replica reads it.
        """
        metadata = _metadata(self.writer)
        if _published_metadata_current(metadata):
            return True
        if (metadata.get(authority.PENDING_CREATE) is not None
                or metadata.get(_EPOCH) != metadata.get(schema.META_LEASE_EPOCH)
                or metadata.get(_PUBLISHED) is None
                or json.loads(metadata[_PUBLISHED]) != _head(metadata)):
            return False
        marker = authority.read_marker(self.root)
        if marker is None:
            return False
        pending = {**_head(metadata), "sha256": metadata[_DIGEST], "lease_epoch": metadata.get(_EPOCH)}
        copied_raw = self.copied_intent(pending, None)
        # Exclusion was released for verification; a new intent or commit must not borrow the old proof.
        current = _metadata(self.writer)
        if (current.get(authority.PENDING_CREATE) != metadata.get(authority.PENDING_CREATE)
                or _head(current) != _head(metadata) or authority.read_marker(self.root) != marker):
            raise _Retry("live intent, head or marker changed during verification")
        return _published_metadata_current(current, copied_raw=copied_raw, raw_marker=marker)

    def run(self, stage_new, *, force=False):
        """Orderly publication: settle, then copy and swap the head known at entry, all under the boundary.

        It copies at a published head too when the intent or lease epoch changed since that copy.
        """
        self.check()
        requested = _head(_metadata(self.writer))
        with self.bound():
            digest, head = self.settle()
            if (not force and head == requested and self.carries_metadata()) or not stage_new:
                return self.current(digest, head)
            while force or head is None or head["commit_seq"] < requested["commit_seq"] or not self.carries_metadata():
                pending = self.begin()
                with ExitStack() as staging:
                    artifact = staging.enter_context(snapshot.staged_snapshot(
                        self.root,
                        directory=self.root / self.workspace_relative,
                        deadline=self.deadline,
                        cancelled=self._staging_cancelled,
                        scratch_token=pending["token"],
                        cleanup=False,
                    ))
                    head = self.install(pending, artifact, staging.close)
                    force = head is None
            return PublicationResult("published", head["commit_seq"], head["head_hash"])

    def prepare(self):
        """Concurrent publication, first boundary step: settle and record the staging intent.

        Returns the recorded intent, or the outcome when nothing needs copying.
        """
        self.check()
        requested = _head(_metadata(self.writer))
        with self.bound():
            digest, head = self.settle()
            if head == requested and self.carries_metadata():
                return self.current(digest, head)
            return self.begin()

    def swap(self, pending, artifact, finish_staging):
        """Concurrent publication, second boundary step: verify the copy and swap it in.

        The intent must be the one ``prepare`` recorded and its workspace unchanged; the
        copied head must still be in this store's chain, and the installed replica must
        still be the one this store last published (check-then-swap, A4).
        """
        self.check()
        if self.pending_raw is None or json.loads(self.pending_raw) != pending:
            raise _Retry("publication intent changed")
        with self.bound():
            self.open_workspace(pending)
            if self.workspace is None:
                raise _Retry("publication workspace disappeared")
            head = self.install(pending, artifact, finish_staging)
            if head is None:
                return PublicationResult("retry_pending", reason="publication stage disappeared")
            return PublicationResult("published", head["commit_seq"], head["head_hash"])

    def settle(self):
        """Resolve leftover intent and check the installed replica; returns the published digest and head."""
        pending = self.pending()
        if pending is not None:
            self.resolve(pending)
        digest, head = self.published()
        current = self.file(connection.STORE_FILENAME)
        if current is not None:
            with current:
                found = self.digest(current)
                adopted = self.metadata.get(schema.META_ADOPTED_FOREIGN_REPLICA)
                if found != digest and found == adopted:
                    # Owner adopt-local previewed exactly these bytes: keep them as
                    # evidence and publish over the freed name.
                    self.preserve(current, connection.STORE_FILENAME, None,
                                  self.aside(current, connection.STORE_FILENAME, None))
                    self.bookkeeping({schema.META_ADOPTED_FOREIGN_REPLICA: None})
                elif found != digest:
                    self.diverge(
                        "unexpected replica target",
                        foreign=current,
                        leaf=connection.STORE_FILENAME,
                    )
        elif digest is not None:
            self.diverge("published replica disappeared")
        return digest, head

    def current(self, digest, head):
        if head is not None:
            self.flush_installed(digest)
        return PublicationResult(
            "published" if head is not None else "retry_pending",
            head["commit_seq"] if head else None,
            head["head_hash"] if head else None,
        )

    def begin(self):
        """Record a staging intent and create its empty private workspace."""
        self.check()
        token = secrets.token_hex(16)
        pending = {
            "version": 2,
            "phase": "staging",
            "token": token,
            "workspace_leaf": f".exomem-collection-replica-work-{token}",
            "workspace_identity": None,
            "stage_leaf": f"{snapshot._PREFIX}{token}.sqlite",
            "store_id": self.identity["store_id"],
            "instance_id": self.identity["instance_id"],
            "lease_epoch": self.epoch,
        }
        self.bookkeeping({_PENDING: _json(pending)})
        self.open_workspace(pending, create=True)
        return pending

    def install(self, pending, artifact, finish_staging):
        """Verify a staged copy against this store and the intent, mark it ready and swap it in."""
        self.check()
        copied = {key: getattr(artifact, key) for key in self.identity}
        if any(copied[key] != self.identity[key] for key in ("store_id", "instance_id")):
            raise _Retry("store identity changed")
        self.validate_head(copied)
        if artifact.lease_epoch != self.epoch:
            raise _Retry("snapshot lease epoch changed")
        with self.file(artifact.path.name, required=True, parent=self.workspace) as staged:
            if self.digest(staged) != artifact.file_sha256:
                self.diverge(
                    "verified snapshot changed",
                    foreign=staged,
                    leaf=artifact.path.name,
                    parent=self.workspace,
                )
            self.record(artifact.path.name, staged.identity, parent=self.workspace)
        self.flush()
        pending.update(phase="ready", sha256=artifact.file_sha256, **copied)
        self.bookkeeping({_PENDING: _json(pending)})
        return self.resolve(pending, finish_staging=finish_staging, artifact=artifact)

    def _staging_cancelled(self):
        self.check()
        return False


def publish_replica(
    vault_root: Path,
    writer: connection.WriterConnection,
    *,
    authority_check: Callable[[], bool],
    deadline: float,
    cancelled: Callable[[], bool] | None = None,
    force: bool = False,
) -> PublicationResult:
    """Resolve pending work, then flush the committed head known at entry.

    This is deliberately not wired to a public or service adapter. The caller
    supplies mode/adoption/fence authority, holds the mutation boundary and
    guarantees exclusive custody of the operation workspace for the entire call.
    Every sync client must exclude that subtree from incoming and outgoing sync;
    a random name, permissions or the lease alone cannot establish custody.
    Disposable isolated callers supply their own custody. No replica is ever
    opened by SQLite for writing; this foundation proves no live sync configuration.
    The vault's ordinary Knowledge Base directory must already exist.
    """
    return _run(vault_root, writer, authority_check, deadline, cancelled, stage_new=True, force=force)


def recover_replica(
    vault_root: Path,
    writer: connection.WriterConnection,
    *,
    authority_check: Callable[[], bool],
    deadline: float,
    cancelled: Callable[[], bool] | None = None,
) -> PublicationResult:
    """Resolve only existing intent, returning its actual published head."""
    return _run(vault_root, writer, authority_check, deadline, cancelled, stage_new=False)


def discard_abandoned_publication(
    vault_root: Path,
    writer: connection.WriterConnection,
    intent: str,
    *,
    predecessor: str | None,
    authority_check: Callable[[], bool],
    deadline: float,
) -> str | None:
    """Remove the workspace of a publication intent that adopt-local dropped.

    Same authority and custody contract as ``publish_replica``. Returns None when the
    workspace is gone, or why it was kept: unknown entries, foreign bytes or an
    unresolved filesystem state each keep it rather than guess.
    """
    publisher = _publisher(vault_root, writer, authority_check, deadline, None)
    try:
        with _alone(publisher.root, deadline), publisher.bound():
            return publisher.abandon(json.loads(intent), predecessor)
    except (_Diverged, _Retry, TimeoutError, held_fs.HeldFsError, OSError, ValueError, TypeError,
            AttributeError) as error:
        return f"the abandoned workspace was kept: {error}"


def _publisher(root, writer, authority_check, deadline, cancelled):
    writer.require_owner_thread()
    if writer.path != connection.store_path(root).resolve():
        raise connection.CollectionStoreError(
            "COLLECTION_STORE_VAULT_MISMATCH", "replica publisher requires this vault's live store"
        )
    if writer._closed:
        raise connection.CollectionStoreError("COLLECTION_STORE_WRITER_CLOSED", "writer is closed")
    if writer.connection.in_transaction:
        raise ValueError("replica publication must run after business commit")
    return _Publisher(root, writer, authority_check, deadline, cancelled)


def publish_replica_concurrently(vault_root, *, step, deadline, cancelled=None, patience=5.0) -> PublicationResult:
    """Coalesced publication (A9) with witnessed, optimistic boundary steps.

    ``step(patience, cancelled)`` is the caller's context manager for one brief
    boundary step: at an idle instant it holds the boundary, rechecks lease and
    custody, and yields this vault's writer with its authority check. Copying,
    byte verification and file fsync run outside canonical and identity exclusion.
    Each resumed step compares metadata, name topology and exact held versions
    before effects. A changed observation leaves the v2 intent for recovery.

    The result can carry a head older than the live one: the next window or orderly
    boundary publishes the newer head. Orderly publication in this process stops the
    copy and runs alone.
    """
    root = Path(vault_root).resolve()
    serial = _serial(root)
    if not serial.lock.acquire(blocking=False):
        return PublicationResult("retry_pending", reason="another replica publication is running")
    publisher = None
    try:
        def stop():
            return serial.waiters > 0 or (cancelled is not None and cancelled())

        with reserved_paths._subsystem_authority_scope(_OWNER):
            with ExitStack() as steps:
                writer, authority_check = steps.enter_context(step(0.0, stop))
                publisher = _publisher(root, writer, authority_check, deadline, stop)
                del writer, authority_check
                publisher.steps, publisher.step, publisher.patience = steps, step, patience
                steps.enter_context(reserved_paths._identity_coordination_scope(
                    root, descriptor_ids=(_DESCRIPTOR,)
                ))
                pending = _outcome(None, publisher.prepare, metadata=lambda: publisher.metadata)
                publisher.writer = publisher.authority_check = None
                if isinstance(pending, PublicationResult):
                    return pending
            with ExitStack() as staging:
                artifact = staging.enter_context(snapshot.staged_snapshot(
                    root,
                    directory=replica_path(root).parent / pending["workspace_leaf"],
                    deadline=deadline,
                    cancelled=stop,
                    scratch_token=pending["token"],
                    cleanup=False,
                ))
                with ExitStack() as steps:
                    publisher.steps = steps
                    publisher.enter_step()
                    return _outcome(None, lambda: publisher.swap(pending, artifact, staging.close),
                                    metadata=lambda: publisher.metadata)
    except connection.CollectionStoreError as error:
        # Before publisher construction, no publication effect occurred; preserve the caller's busy retry.
        if error.code == "COLLECTION_STORE_BUSY" and publisher is None:
            raise
        if error.code not in {"COLLECTION_STORE_BUSY", "COLLECTION_STORE_LEASE_REQUIRED",
                              "COLLECTION_STORE_CUSTODY_UNVERIFIED", "COLLECTION_SNAPSHOT_CANCELLED"}:
            raise
        return PublicationResult("retry_pending", reason=error.code)
    except (_Retry, TimeoutError, held_fs.HeldFsError, OSError, sqlite3.OperationalError) as error:
        return PublicationResult("retry_pending", reason=str(error))
    except OpError as error:
        # Either step's authority check: the lease manager fenced this token or could not
        # reach the coordinator. The copy is abandoned like a lost lease; the next window retries.
        return PublicationResult("retry_pending", reason=str(error))
    finally:
        if publisher is not None:
            publisher.writer = publisher.authority_check = None
        serial.lock.release()


class _Serial:
    """One vault's replica work in this process: a concurrent copy yields to orderly publication."""

    def __init__(self):
        self.lock = threading.Lock()
        self.waiters = 0


_SERIAL_GUARD = threading.Lock()
_SERIALS = weakref.WeakValueDictionary()


def _serial(root):
    with _SERIAL_GUARD:
        serial = _SERIALS.get(root)
        if serial is None:
            serial = _SERIALS[root] = _Serial()
        return serial


@contextmanager
def _alone(root, deadline):
    """Run orderly replica work alone: this process's concurrent copy stops first."""
    serial = _serial(Path(root).resolve())
    with _SERIAL_GUARD:
        serial.waiters += 1
    try:
        acquired = serial.lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
    finally:
        with _SERIAL_GUARD:
            serial.waiters -= 1
    if not acquired:
        raise _Retry("a concurrent replica copy did not stop")
    try:
        yield
    finally:
        serial.lock.release()


def _run(root, writer, authority_check, deadline, cancelled, *, stage_new, force=False):
    publisher = _publisher(root, writer, authority_check, deadline, cancelled)

    def attempt():
        with _alone(publisher.root, deadline):
            return publisher.run(stage_new, force=force)

    return _outcome(writer, attempt)


def _outcome(writer, attempt, *, metadata=None):
    """Run one publication attempt and map its failure to an honest outcome."""
    try:
        return attempt()
    except _Diverged as error:
        return PublicationResult("diverged", reason=str(error))
    except connection.CollectionStoreError as error:
        if error.code == "COLLECTION_STORE_DIVERGED":
            return PublicationResult("diverged", reason=error.code)
        if error.code not in {"COLLECTION_STORE_LEASE_REQUIRED", "COLLECTION_SNAPSHOT_CANCELLED"}:
            raise
        if schema.META_REPLICA_DIVERGENCE in (metadata() if metadata else _metadata(writer)):
            return PublicationResult(
                "diverged", reason=f"foreign preservation pending: {error.code}"
            )
        return PublicationResult("retry_pending", reason=error.code)
    except (_Retry, TimeoutError, held_fs.HeldFsError, OSError, sqlite3.OperationalError) as error:
        if schema.META_REPLICA_DIVERGENCE in (metadata() if metadata else _metadata(writer)):
            return PublicationResult("diverged", reason=f"foreign preservation pending: {error}")
        return PublicationResult("retry_pending", reason=str(error))
