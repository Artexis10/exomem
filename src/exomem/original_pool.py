"""Canonical immutable originals; logical companions alone confer filing identity and access."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import artifact_lifecycle, held_fs, originals, reserved_paths, state_migration
from .kbdir import kb_dirname

DESCRIPTOR_ID = reserved_paths.ORIGINAL_POOL_DESCRIPTOR_ID
OWNER = reserved_paths.ORIGINAL_POOL_OWNER
# The versioned storage protocol fixes the marker name and its complete contents.
_MARKER_NAME = "format.json"
_MARKER_BYTES = (json.dumps({"schema": 1, "format": state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID},
                          separators=(",", ":")) + "\n").encode()


def pool_root(vault: Path) -> Path:
    return Path(vault) / kb_dirname() / reserved_paths.ORIGINAL_POOL_DIRNAME


def _object_relative(digest: str) -> PurePosixPath:
    if not originals.is_sha256(digest):
        raise ValueError("an original requires a lowercase SHA-256 digest")
    return PurePosixPath("sha256") / digest[:2] / f"{digest}.blob"


def object_path(vault: Path, digest: str) -> Path:
    return pool_root(vault) / _object_relative(digest)


def is_canonical_member(path: str) -> bool:
    """Only the format marker and schema-shaped objects enter backups; stages remain disposable."""
    candidate = PurePosixPath(path)
    if candidate.as_posix() != path:
        return False
    try:
        relative = candidate.relative_to(PurePosixPath(kb_dirname()) / reserved_paths.ORIGINAL_POOL_DIRNAME)
    except ValueError:
        return False
    if relative == PurePosixPath(_MARKER_NAME):
        return True
    # The storage protocol fixes SHA-256 names and their shard; these are not semantic categories.
    digest = relative.name.removesuffix(".blob")
    return originals.is_sha256(digest) and relative == _object_relative(digest)


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

    def replace(self, damaged: held_fs.FileVersion) -> None:
        """Remove one exact damaged object version, then publish the staged bytes in its place.

        Runs under the filing's canonical guard, after the caller moved the damaged bytes to Trash.
        """
        reserved_paths._remove_owner_file(self.vault, object_path(self.vault, self.digest), DESCRIPTOR_ID,
                                         expected_version=damaged)
        self.publish()

    def require_current(self) -> None:
        if not self._published or self._source is None:
            raise RuntimeError("the original must be published before its filing commits")
        self._source.require_current()

    @property
    def source(self) -> reserved_paths.GenericBinarySource:
        """Expose the verified published version without reopening a replaceable pathname."""
        self.require_current()
        assert self._source is not None
        return self._source


@contextmanager
def prepare(vault: Path, digest: str, size: int, data: held_fs.Chunks, *,
            temporary_name: str | None = None,
            on_created: Callable[[held_fs.StableIdentity], None] | None = None,
            replacing: bool = False) -> Iterator[PreparedOriginal]:
    """Stage every supplied byte before examining physical reuse; retain custody through commit.

    `replacing` stages a repair of a damaged object, which `PreparedOriginal.replace` swaps out.
    """
    if not enabled(vault):
        raise ValueError("shared originals require explicit format activation")
    from .media_store import _pin

    target = object_path(vault, digest)
    with reserved_paths._subsystem_authority_scope(OWNER):
        with reserved_paths._stage_owner_chunks(vault, target, DESCRIPTOR_ID,
                data, expected_sha256=digest, expected_size=size,
                temporary_name=temporary_name, on_created=on_created) as staged:
            # An OS handle alone does not exclude eviction. Keep the ledger pin
            # outside winner handles so confirmation retries cannot release it.
            with _pin(vault, target, object_key=target.relative_to(vault).as_posix()), ExitStack() as handles:
                prepared = PreparedOriginal(vault, digest, size, staged, handles)
                if not replacing:  # A damaged object is no winner to confirm; the repair replaces it.
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


def remove_stage(vault: Path, object_key: str, name: str, identity: held_fs.StableIdentity | None) -> None:
    """Clean a reserved hydration stage without granting custody over an unverified replacement."""
    if not is_canonical_member(object_key) or not object_key.endswith(".blob"):
        raise ValueError("hydration cleanup requires a canonical object destination")
    with reserved_paths._subsystem_authority_scope(OWNER):
        reserved_paths._remove_owner_stage(vault, (vault / object_key).with_name(name), DESCRIPTOR_ID, identity)


def evict_object(vault: Path, digest: str, *, expected_version: held_fs.FileVersion) -> None:
    """Remove one verified physical version after the ledger proves every binding recoverable."""
    with reserved_paths._subsystem_authority_scope(OWNER):
        reserved_paths._remove_owner_file(vault, object_path(vault, digest), DESCRIPTOR_ID,
                                         expected_version=expected_version)
