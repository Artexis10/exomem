"""Dark, authority-bound publication of collection-store replicas."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import held_fs, reserved_paths
from ..kbdir import kb_dirname
from . import connection, schema, snapshot, tokens

_OWNER = "collection_store.replica"
_DESCRIPTOR = "collection-replica"
_TOKEN = snapshot._TOKEN
_PENDING = schema.META_PENDING_REPLICA_PUBLICATION
_DIGEST = schema.META_LAST_PUBLISHED_REPLICA_SHA256
_PUBLISHED = schema.META_PUBLISHED_REPLICA_HEAD


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


def _metadata(writer: connection.WriterConnection) -> dict[str, str]:
    return dict(writer.connection.execute("SELECT key,value FROM store_meta"))


def _head(metadata: dict[str, str]) -> dict:
    return {
        "store_id": metadata[schema.META_STORE_ID],
        "instance_id": metadata[schema.META_INSTANCE_ID],
        "commit_seq": int(metadata[schema.META_COMMIT_SEQ]),
        "head_hash": metadata.get(schema.META_STORE_HEAD_HASH),
    }


def _json(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


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
                if value is None:
                    conn.execute("DELETE FROM store_meta WHERE key=?", (key,))
                else:
                    conn.execute(
                        "INSERT INTO store_meta(key,value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value),
                    )
        if _PENDING in values:
            self.pending_raw = values[_PENDING]

    def diverge(self, reason, *, foreign=None, leaf=None, parent=None):
        marker = {"reason": reason}
        digest = None
        if foreign is not None:
            digest = self.digest(foreign)
            marker.update(
                {
                    "source_leaf": self.leaf_relative(leaf, parent).as_posix(),
                    "foreign_leaf": f".foreign-{self.identity['instance_id']}-{secrets.token_hex(16)}",
                    "sha256": digest,
                }
            )
        self.bookkeeping({schema.META_REPLICA_DIVERGENCE: _json(marker)})
        if foreign is not None:
            # Durable divergence fences business writes before preservation.
            # This trusted seam retains every other intrinsic authority check.
            self.check(allow_diverged=True)
            self.filesystem.rename(
                foreign, self.parent, marker["foreign_leaf"], replace=False
            ).require()
            self.check(allow_diverged=True)
            with self.file(marker["foreign_leaf"], required=True) as preserved:
                if (
                    not held_fs._same_file_identity(foreign.identity, preserved.identity)
                    or snapshot._file_sha256(
                        preserved.descriptor, lambda: self.check(allow_diverged=True)
                    )
                    != digest
                ):
                    raise _Diverged("foreign preservation identity changed; all bytes retained")
                self.record(marker["foreign_leaf"], preserved.identity)
            self.record(leaf, None, parent=parent)
            for directory in (self.parent, self.workspace):
                if directory is not None:
                    self.check(allow_diverged=True)
                    self.filesystem.flush_directory(directory).require()
        raise _Diverged(reason)

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
        return file

    def digest(self, file):
        self.check()
        return snapshot._file_sha256(file.descriptor, self.check)

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
        with self.file(leaf, required=True, access="write") as installed:
            if self.digest(installed) != expected:
                with self.file(leaf, required=True) as foreign:
                    self.diverge("installed replica changed", foreign=foreign, leaf=leaf)
            self.check()
            os.fsync(installed.descriptor)
            self.check()
            self.filesystem.flush_directory(self.parent).require()
            with self.file(leaf, required=True) as current:
                if (
                    not held_fs._same_file_identity(installed.identity, current.identity)
                    or self.digest(current) != expected
                ):
                    raise _Retry("installed replica changed during flush")
                self.record(leaf, current.identity)

    def resolve(self, pending):
        if self.workspace is None:
            self.open_workspace(pending)
        if pending["phase"] == "staging":
            self.clean_workspace(pending, staging=True)
            self.bookkeeping({_PENDING: None})
            return None
        predecessor, _ = self.published()
        aside_leaf = f".exomem-collection-replica-aside-{pending['token']}"
        target_leaf = connection.STORE_FILENAME
        target = aside = stage = None
        try:
            target = self.file(target_leaf)
            if self.workspace is not None:
                if set(self.filesystem.iter_names(self.workspace)) - {
                    aside_leaf,
                    pending["stage_leaf"],
                }:
                    raise _Retry("publication workspace contains unknown entries")
                aside = self.file(aside_leaf, parent=self.workspace)
                stage = self.file(pending["stage_leaf"], parent=self.workspace)
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
            self.remove(pending["stage_leaf"], pending["sha256"])
            if predecessor is not None:
                self.remove(aside_leaf, predecessor)
            self.clean_workspace(pending)
            # A matching replacement after cleanup still needs its own file flush.
            self.flush_installed(pending["sha256"])
            head = {key: pending[key] for key in self.identity}
            self.bookkeeping({_DIGEST: pending["sha256"], _PUBLISHED: _json(head), _PENDING: None})
            return head
        finally:
            for file in (target, aside, stage):
                if file is not None:
                    file.close()

    def run(self, stage_new):
        self.check()
        requested = _head(_metadata(self.writer))
        with (
            reserved_paths._subsystem_authority_scope(_OWNER),
            reserved_paths._identity_coordination_scope(self.root, descriptor_ids=(_DESCRIPTOR,)),
            held_fs.acquire(self.root).require() as filesystem,
            filesystem.parent(kb_dirname(), access="flush").require() as kb_parent,
        ):
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
                    pending = self.pending()
                    if pending is not None:
                        self.resolve(pending)
                    digest, head = self.published()
                    current = self.file(connection.STORE_FILENAME)
                    if current is not None:
                        with current:
                            if self.digest(current) != digest:
                                self.diverge(
                                    "unexpected replica target",
                                    foreign=current,
                                    leaf=connection.STORE_FILENAME,
                                )
                    elif digest is not None:
                        self.diverge("published replica disappeared")
                    if head == requested or not stage_new:
                        if head is not None:
                            self.flush_installed(digest)
                        return PublicationResult(
                            "published" if head is not None else "retry_pending",
                            head["commit_seq"] if head else None,
                            head["head_hash"] if head else None,
                        )
                    while head is None or head["commit_seq"] < requested["commit_seq"]:
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
                        with snapshot.staged_snapshot(
                            self.root,
                            directory=self.root / self.workspace_relative,
                            deadline=self.deadline,
                            cancelled=self._staging_cancelled,
                            scratch_token=token,
                        ) as artifact:
                            self.check()
                            self.validate_head(
                                {key: getattr(artifact, key) for key in self.identity}
                            )
                            if artifact.lease_epoch != self.epoch:
                                raise _Retry("snapshot lease epoch changed")
                            with self.file(
                                artifact.path.name, required=True, parent=self.workspace
                            ) as staged:
                                if self.digest(staged) != artifact.file_sha256:
                                    self.diverge(
                                        "verified snapshot changed",
                                        foreign=staged,
                                        leaf=artifact.path.name,
                                        parent=self.workspace,
                                    )
                                self.record(
                                    artifact.path.name, staged.identity, parent=self.workspace
                                )
                            self.flush()
                            pending.update(
                                phase="ready",
                                sha256=artifact.file_sha256,
                                **{key: getattr(artifact, key) for key in self.identity},
                            )
                            self.bookkeeping({_PENDING: _json(pending)})
                            head = self.resolve(pending)
                    return PublicationResult("published", head["commit_seq"], head["head_hash"])
                finally:
                    if self.workspace is not None:
                        self.workspace.close()
                        self.workspace = None

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
    return _run(vault_root, writer, authority_check, deadline, cancelled, stage_new=True)


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


def _run(root, writer, authority_check, deadline, cancelled, *, stage_new):
    writer.require_owner_thread()
    if writer.path != connection.store_path(root).resolve():
        raise connection.CollectionStoreError(
            "COLLECTION_STORE_VAULT_MISMATCH", "replica publisher requires this vault's live store"
        )
    if writer._closed:
        raise connection.CollectionStoreError("COLLECTION_STORE_WRITER_CLOSED", "writer is closed")
    if writer.connection.in_transaction:
        raise ValueError("replica publication must run after business commit")
    publisher = _Publisher(root, writer, authority_check, deadline, cancelled)
    try:
        return publisher.run(stage_new)
    except _Diverged as error:
        return PublicationResult("diverged", reason=str(error))
    except connection.CollectionStoreError as error:
        if error.code == "COLLECTION_STORE_DIVERGED":
            return PublicationResult("diverged", reason=error.code)
        if error.code not in {"COLLECTION_STORE_LEASE_REQUIRED", "COLLECTION_SNAPSHOT_CANCELLED"}:
            raise
        if schema.META_REPLICA_DIVERGENCE in _metadata(writer):
            return PublicationResult(
                "diverged", reason=f"foreign preservation pending: {error.code}"
            )
        return PublicationResult("retry_pending", reason=error.code)
    except (_Retry, TimeoutError, held_fs.HeldFsError, OSError, sqlite3.OperationalError) as error:
        if schema.META_REPLICA_DIVERGENCE in _metadata(writer):
            return PublicationResult("diverged", reason=f"foreign preservation pending: {error}")
        return PublicationResult("retry_pending", reason=str(error))
