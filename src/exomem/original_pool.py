"""Canonical immutable originals; logical companions alone confer filing identity and access."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import artifact_lifecycle, held_fs, originals, reserved_paths, state_migration
from .kbdir import kb_dirname

DESCRIPTOR_ID = "original-pool"
OWNER = "original_pool"
# The versioned storage protocol fixes the marker name and its complete contents.
_MARKER_NAME = "format.json"
_MARKER_BYTES = (json.dumps({"schema": 1, "format": state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID},
                          separators=(",", ":")) + "\n").encode()


def pool_root(vault: Path) -> Path:
    return Path(vault) / kb_dirname() / reserved_paths.ORIGINAL_POOL_DIRNAME


def object_path(vault: Path, digest: str) -> Path:
    if not originals.is_sha256(digest):
        raise ValueError("an original requires a lowercase SHA-256 digest")
    return pool_root(vault) / "sha256" / digest[:2] / f"{digest}.blob"


def enabled(vault: Path) -> bool:
    """Read canonical format state; a missing derived index cannot disable this layout."""
    with reserved_paths._subsystem_authority_scope(OWNER):
        try:
            with reserved_paths._open_owner_binary(vault, pool_root(vault) / _MARKER_NAME, DESCRIPTOR_ID) as source:
                if source.version.size != len(_MARKER_BYTES) or source.stream.read(len(_MARKER_BYTES) + 1) != _MARKER_BYTES:
                    raise ValueError("the original pool has an unsupported format marker")
                source.require_current()
        except held_fs.HeldFsError as error:
            if error.code == "MISSING":
                return False
            raise
    return True


def enable(vault: Path) -> None:
    """Enrol compatibility before publishing the marker; callers own explicit layout activation."""
    required = state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID
    if not artifact_lifecycle.supports(required):
        raise state_migration.StateMigrationOfflineRequired(
            "update the managed launcher offline before enabling shared originals")
    state_migration.record_shared_originals_compatibility(
        vault, authority_check=lambda: artifact_lifecycle.supports(required))
    with reserved_paths._subsystem_authority_scope(OWNER):
        with reserved_paths._stage_owner_chunks(vault, pool_root(vault) / _MARKER_NAME, DESCRIPTOR_ID,
                held_fs.Chunks(lambda: [_MARKER_BYTES]), expected_sha256=hashlib.sha256(_MARKER_BYTES).hexdigest(),
                expected_size=len(_MARKER_BYTES)) as staged:
            for _attempt in range(3):
                with ExitStack() as handles:
                    existing = None
                    try:
                        existing = handles.enter_context(reserved_paths._open_owner_binary(
                            vault, pool_root(vault) / _MARKER_NAME, DESCRIPTOR_ID))
                    except held_fs.HeldFsError as error:
                        if error.code != "MISSING":
                            raise
                    if existing is not None:
                        if existing.stream.read(len(_MARKER_BYTES) + 1) != _MARKER_BYTES:
                            raise ValueError("the original pool has an unsupported format marker")
                        existing.require_current()
                    try:
                        reserved_paths._install_staged_owner(staged, verified_existing=existing)
                        return
                    except held_fs.HeldFsError as error:
                        if error.code != "DESTINATION_EXISTS":
                            raise
            raise PublicationRace("original format activation is busy; retry activation")


class PublicationRace(RuntimeError):
    """Leave the canonical guard, confirm the winner's bytes, then retry publication."""


@dataclass(slots=True)
class PreparedOriginal:
    """One verified upload stage and an optional verified winner, retained through its filing commit."""

    vault: Path
    digest: str
    size: int
    staged: reserved_paths.StagedOwnerFile
    _handles: ExitStack
    _source: reserved_paths.GenericBinarySource | None = None
    _published: bool = False

    def confirm_existing(self) -> bool:
        """Hash a concurrent winner outside the canonical guard and identity coordination."""
        self._handles.close()
        self._source = None
        try:
            source = self._handles.enter_context(reserved_paths._open_owner_binary(
                self.vault, object_path(self.vault, self.digest), DESCRIPTOR_ID))
        except held_fs.HeldFsError as error:
            if error.code == "MISSING":
                return False
            raise
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: source.stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
        source.require_current()
        if digest.hexdigest() != self.digest or size != self.size:
            raise ValueError("the shared original differs from its canonical binding")
        source.stream.seek(0)
        self._source = source
        return True

    def publish(self) -> None:
        """Publish under the filing's canonical guard; no content copy or hash runs here."""
        try:
            identity = reserved_paths._install_staged_owner(self.staged, verified_existing=self._source)
        except held_fs.HeldFsError as error:
            if error.code == "DESTINATION_EXISTS":
                raise PublicationRace("confirm the concurrently published original before retrying") from error
            raise
        if self._source is None:
            self._source = self._handles.enter_context(reserved_paths._open_owner_binary(
                self.vault, object_path(self.vault, self.digest), DESCRIPTOR_ID))
            if self._source.version.identity != identity or self._source.version.size != self.size:
                raise held_fs.HeldFsError("IDENTITY_CHANGED", "published original changed")
        self._published = True
        self.require_current()

    def require_current(self) -> None:
        if not self._published or self._source is None:
            raise RuntimeError("the original must be published before its filing commits")
        self._source.require_current()


@contextmanager
def prepare(vault: Path, digest: str, size: int, data: held_fs.Chunks) -> Iterator[PreparedOriginal]:
    """Stage every supplied byte before examining physical reuse; retain custody through commit."""
    if not enabled(vault):
        raise ValueError("shared originals require explicit format activation")
    with reserved_paths._subsystem_authority_scope(OWNER):
        with reserved_paths._stage_owner_chunks(vault, object_path(vault, digest), DESCRIPTOR_ID,
                data, expected_sha256=digest, expected_size=size) as staged:
            with ExitStack() as handles:
                prepared = PreparedOriginal(vault, digest, size, staged, handles)
                prepared.confirm_existing()
                yield prepared


@contextmanager
def open_object(vault: Path, digest: str, size: int) -> Iterator[reserved_paths.GenericBinarySource]:
    """Open bytes after logical authorization; the digest itself grants no public access."""
    with reserved_paths._subsystem_authority_scope(OWNER):
        with reserved_paths._open_owner_binary(vault, object_path(vault, digest), DESCRIPTOR_ID) as source:
            if source.version.size != size:
                raise ValueError("the shared original differs from its canonical binding")
            yield source


def locally_present(vault: Path, digest: str, size: int) -> bool:
    """A bounded metadata probe, distinct from full byte verification."""
    try:
        with open_object(vault, digest, size):
            return True
    except held_fs.HeldFsError as error:
        if error.code == "MISSING":
            return False
        raise
