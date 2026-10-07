"""Durable records shared by hosted and standalone stopped restore owners."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import state_migration, state_paths

DIRECTORY = "restore-journal"
BASE_MAX_BYTES = 65536
# These discriminator values are the closed proposal-evidence protocol, not content vocabulary.
CONTINUITY_EVIDENCE = "restore-continuity/v1"
MEMBERSHIP_EVIDENCE = "affected-membership/v1"
_ACTIVE: ContextVar[ProtectionRecovery | None] = ContextVar("restore_protection", default=None)


class RestoreJournalError(ValueError):
    """A stopped restore cannot prove its exact destination-owned continuation."""


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode()


def _ownership(uid: int | None, gid: int | None) -> tuple[int | None, int | None]:
    return (uid, gid) if os.name == "nt" else (
        os.geteuid() if uid is None else uid, os.getegid() if gid is None else gid,
    )


def read(path: Path, identity: Mapping[str, Any], *, uid: int | None = None,
         gid: int | None = None, max_bytes: int = BASE_MAX_BYTES) -> dict[str, Any] | None:
    if not os.path.lexists(path):
        return None
    uid, gid = _ownership(uid, gid)
    try:
        value = path.lstat()
        if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
                or value.st_size > max_bytes
                or os.name != "nt" and (value.st_uid != uid or value.st_gid != gid
                                         or stat.S_IMODE(value.st_mode) != 0o600)):
            raise ValueError("unsafe journal")

        def no_duplicates(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate journal key")
                result[key] = item
            return result

        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (value.st_dev, value.st_ino):
                raise ValueError("journal identity changed")
            record = json.loads(stream.read(max_bytes + 1), object_pairs_hook=no_duplicates)
        if not isinstance(record, dict) or any(record.get(key) != item for key, item in identity.items()):
            raise ValueError("journal identity differs")
        return record
    except (OSError, ValueError) as error:
        raise RestoreJournalError("restore journal cannot prove the destination") from error


def write(path: Path, record: Mapping[str, Any], *, uid: int | None = None,
          gid: int | None = None) -> None:
    uid, gid = _ownership(uid, gid)
    if os.name == "nt":
        from .mutation_lock import prepare_windows_private_state_root

        prepare_windows_private_state_root(path.parent)
    else:
        existed = path.parent.exists()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        value = path.parent.lstat()
        if not stat.S_ISDIR(value.st_mode):
            raise RestoreJournalError("restore journal directory is unsafe")
        if os.geteuid() == 0:
            os.chown(path.parent, uid, gid, follow_symlinks=False)
        path.parent.chmod(0o700, follow_symlinks=False)
        value = path.parent.lstat()
        if value.st_uid != uid or value.st_gid != gid:
            raise RestoreJournalError("restore journal directory has another owner")
        if not existed:
            state_migration._fsync_directory(path.parent.parent)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical_bytes(record))
            output.flush()
            if os.name != "nt":
                os.fchmod(output.fileno(), 0o600)
                if os.geteuid() == 0:
                    os.fchown(output.fileno(), uid, gid)
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    state_migration._fsync_directory(path.parent)


def protection_limit(documents: Mapping[str, str]) -> int:
    # The verified archive bounds these documents; the journal retains their missing subset once.
    return BASE_MAX_BYTES + 2 * len(canonical_bytes(documents))


def protection_record(root: Path, *, archive_sha256: str, manifest_sha256: str,
                      operation_id: str, documents: Mapping[str, str]) -> dict[str, Any]:
    return {"version": 1, "destination": str(root.resolve()),
            "state_dir": str(state_paths.vault_state_dir(root).resolve()),
            "archive_sha256": archive_sha256, "manifest_sha256": manifest_sha256,
            "operation_id": operation_id, "documents": dict(documents),
            "proposal_id": uuid.uuid4().hex, "missing_documents": None}


@dataclass
class ProtectionRecovery:
    """An adapter-validated durable link, never a request-supplied authority."""

    root: Path
    record: dict[str, Any]
    persist: Callable[[], None]
    verify_continuity: Callable[[ProtectionRecovery], Mapping[str, Any]] | None = None

    def __post_init__(self) -> None:
        try:
            if (set(self.record) != {"version", "destination", "state_dir", "archive_sha256",
                                    "manifest_sha256", "operation_id", "documents", "proposal_id", "missing_documents"}
                    or type(self.record["version"]) is not int or self.record["version"] != 1
                    or self.record["destination"] != str(self.root.resolve())
                    or self.record["state_dir"] != str(state_paths.vault_state_dir(self.root).resolve())
                    or uuid.UUID(hex=self.record["proposal_id"]).hex != self.record["proposal_id"]):
                raise ValueError
            for key in ("archive_sha256", "manifest_sha256"):
                value = self.record[key]
                if len(value) != 64 or bytes.fromhex(value).hex() != value:
                    raise ValueError
            documents = self.record["documents"]
            missing = self.record["missing_documents"]
            if (not isinstance(documents, dict) or not documents
                    or not all(isinstance(key, str) and isinstance(value, str) for key, value in documents.items())
                    or missing is not None and (not isinstance(missing, dict)
                        or any(documents.get(key) != value for key, value in missing.items()))):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise RestoreJournalError("protection recovery identity differs") from error

    @contextmanager
    def scope(self):
        self.__post_init__()
        token = _ACTIVE.set(self)
        try:
            yield
        finally:
            _ACTIVE.reset(token)

    def continuity(self, documents: Mapping[str, str]) -> dict[str, str]:
        self.__post_init__()
        if documents != self.record["missing_documents"] or self.verify_continuity is None:
            raise RestoreJournalError("restore continuity lacks its exact protective documents")
        manifest = self.verify_continuity(self)
        if manifest["overall_digest"]["value"] != self.record["manifest_sha256"]:
            raise RestoreJournalError("restore continuity manifest differs")
        return {"schema": CONTINUITY_EVIDENCE, "restore_binding": self.digest,
                "manifest_sha256": self.record["manifest_sha256"],
                "inventory_sha256": hashlib.sha256(canonical_bytes({"files": manifest["files"]})).hexdigest()}

    @property
    def proposal_id(self) -> str:
        return self.record["proposal_id"]

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_bytes({key: value for key, value in self.record.items()
                                              if key != "missing_documents"})).hexdigest()

    def bind_missing(self, documents: Mapping[str, str]) -> None:
        if self.record["missing_documents"] is None:
            self.record["missing_documents"] = dict(documents)
            self.persist()
        elif self.record["missing_documents"] != documents:
            raise RestoreJournalError("restore proposal documents changed")

    def validate_proposal(self, payload: Mapping[str, Any]) -> None:
        if (payload.get("restore_binding") != self.digest
                or payload.get("documents") != self.record["missing_documents"]):
            raise RestoreJournalError("restore proposal is not the linked publication")


def active_protection(root: Path) -> ProtectionRecovery | None:
    recovery = _ACTIVE.get()
    return recovery if recovery is not None and recovery.root == root else None
