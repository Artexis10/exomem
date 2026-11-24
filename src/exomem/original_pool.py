"""Canonical immutable originals; logical companions alone confer filing identity and access."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import artifact_lifecycle, held_fs, originals, reserved_paths, state_migration
from .kbdir import managed_join, managed_root

log = logging.getLogger(__name__)

DESCRIPTOR_ID = reserved_paths.ORIGINAL_POOL_DESCRIPTOR_ID
OWNER = reserved_paths.ORIGINAL_POOL_OWNER
# The versioned storage protocol fixes the marker name and its complete contents.
_MARKER_NAME = "format.json"
_MARKER_BYTES = (json.dumps({"schema": 1, "format": state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID},
                          separators=(",", ":")) + "\n").encode()


def pool_root(vault: Path) -> Path:
    return managed_root(Path(vault)) / reserved_paths.ORIGINAL_POOL_DIRNAME


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
        relative = candidate.relative_to(PurePosixPath(managed_join(reserved_paths.ORIGINAL_POOL_DIRNAME)))
    except ValueError:
        return False
    if relative == PurePosixPath(_MARKER_NAME):
        return True
    # The storage protocol fixes SHA-256 names and their shard; these are not semantic categories.
    digest = relative.name.removesuffix(".blob")
    return originals.is_sha256(digest) and relative == _object_relative(digest)


def launcher_supported() -> bool:
    """Whether this process's launcher has proven that it keeps the shared original format."""
    return artifact_lifecycle.supports(state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID)


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


#: What a new archive's creator reports when its process proves no launcher support for shared originals.
NEW_ARCHIVE_ACTION = ("Shared original storage starts at the archive's first service start, with no conversion. "
                      "A standalone server starts it itself. A managed service whose launcher predates shared "
                      "originals needs the offline launcher update in docs/managed-service-upgrades.md first.")


def activate_new(vault: Path, *, authority: object) -> dict[str, str]:
    """Start a genuinely new archive in the shared layout.

    Every new-archive creator calls this once its state is initialized for the final path, under its offline
    authority. The intent is recorded first, so a crash, or a process that proves no launcher support, leaves the
    first capable service start to activate the layout. Until then the archive keeps ordinary storage.
    """
    state_migration.record_shared_originals_intent(vault, authority=authority)
    if _activate_intent(vault):
        return {"layout": "shared"}
    return {"layout": "ordinary", "reason": "no launcher support for shared originals is proven in this process",
            "action": NEW_ARCHIVE_ACTION}


def _activate_intent(vault: Path, *, lock_timeout: float | None = None) -> bool:
    """Activate a recorded intent where this process proves launcher support; otherwise the intent stays."""
    # Checked first because the refusal below logs an offline-migration error, which is no fault of a new archive.
    if not launcher_supported():
        return False
    try:
        enable(vault, lock_timeout=lock_timeout)
    except state_migration.StateMigrationOfflineRequired:
        return False  # Standby withdrew support after the check.
    state_migration.clear_shared_originals_intent(
        vault, authority_check=launcher_supported, lock_timeout=lock_timeout)
    return True


#: What a service start reports when it holds a new archive's shared storage for want of launcher support.
LAUNCHER_UPDATE_ACTION = ("Update the managed launcher offline, as docs/managed-service-upgrades.md describes, then "
                          "restart the service; its next start moves new originals into shared storage.")
# Bounded like startup admission: a service start never waits indefinitely on another migrator.
_START_LOCK_SECONDS = state_migration._BOOTSTRAP_LOCK_TIMEOUT_SECONDS
# This process's held activation, for readiness; None once activation is done or never needed.
_held: dict[str, str] | None = None


class LauncherUpdateRequired(state_migration.StateMigrationOfflineRequired):
    """A shared-storage archive whose state lacks the format, in a process that may not record it."""

    code = "SHARED_ORIGINALS_LAUNCHER_UPDATE_REQUIRED"

    def __init__(self) -> None:
        RuntimeError.__init__(
            self,
            f"{self.code}: this archive keeps its originals in shared storage, but its state does not record "
            "that format and the managed launcher has not proven support for it. Update the managed launcher "
            "offline, as docs/managed-service-upgrades.md describes, then restart the service.",
        )


def activate_at_start(vault: Path) -> None:
    """Make the vault's original storage servable, and activate a new archive's shared storage, before writes.

    The service calls this once it owns the vault, after the launcher or standalone claim: at its start, or at a
    standby's promotion, since a standby proves no support.

    A shared-storage archive that arrived without its format recorded, as a bare copy or a Cloud import does, has
    the format recorded first. Without launcher support that raises `LauncherUpdateRequired` and the service
    refuses to start, as cancelling a held upload refuses: an older release would read its originals as missing.

    A new archive's recorded intent then activates. Without launcher support, or when activation fails, the
    archive keeps ordinary storage and its intent for the next start, and the startup log and readiness name the
    action; startup never fails over it. Originals filed before activation keep their ordinary storage.
    """
    global _held
    _held = None
    _record_arrived_format(vault)
    try:
        intended = state_migration.shared_originals_intent(vault)
    except (ValueError, OSError, state_migration.StateMigrationManifestError):
        # State that cannot be read shows no intent: ordinary storage serves, and the next start reads it again.
        log.warning("could not read whether this archive starts in shared original storage", exc_info=True)
        return
    if not intended:
        return
    try:
        if _activate_intent(vault, lock_timeout=_START_LOCK_SECONDS):
            log.info("a new archive's originals now use shared storage")
            return
        _held = {"state": "held", "reason": "launcher_support_unproven", "action": LAUNCHER_UPDATE_ACTION}
    except Exception:  # noqa: BLE001 - ordinary storage serves every request; the recorded intent retries next start
        log.warning("shared original storage activation failed", exc_info=True)
        _held = {"state": "held", "reason": "activation_failed",
                 "action": "Restart the service to retry; the archive keeps ordinary storage until then."}
    log.warning("a new archive keeps ordinary original storage (%s). %s", _held["reason"], _held["action"])
    from . import runtime_readiness

    # A ready answer proved before the hold, as during a promotion, must not be served without it.
    runtime_readiness.invalidate_cached_readiness()


def _record_arrived_format(vault: Path) -> None:
    # A plain stat first: an archive with no marker, every ordinary one, reads no state and never waits on the
    # identity gate here, which a promotion must survive while it is busy.
    if not (pool_root(vault) / _MARKER_NAME).exists():
        return
    required = state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID
    if required in (state_migration.recorded_descriptor_ids(vault) or ()) or not enabled(vault):
        return
    if not launcher_supported():
        log.error("refusing to serve a shared-storage archive whose state does not record that format")
        raise LauncherUpdateRequired()
    state_migration.record_shared_originals_compatibility(
        vault, authority_check=launcher_supported, lock_timeout=_START_LOCK_SECONDS)
    log.warning("recorded the shared original storage format of an archive that arrived without it")


def held_activation() -> dict[str, str] | None:
    """The readiness block for a new archive whose shared storage this process holds, or None."""
    return None if _held is None else dict(_held)


def enable(vault: Path, *, lock_timeout: float | None = None) -> None:
    """Enrol compatibility before publishing the marker; callers own explicit layout activation.

    A serving caller passes `lock_timeout` and gets `TimeoutError` while another migrator holds the state lock.
    """
    if not launcher_supported():
        raise state_migration.StateMigrationOfflineRequired(
            "update the managed launcher offline before enabling shared originals")
    state_migration.record_shared_originals_compatibility(
        vault, authority_check=launcher_supported, lock_timeout=lock_timeout)
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


class ObjectMismatch(ValueError):
    """A shared object whose bytes differ from the binding that names it: damage, never another original."""


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
            raise ObjectMismatch("the shared original differs from its canonical binding")
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
            replacing: bool = False, meter_allocation: bool = False) -> Iterator[PreparedOriginal]:
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
                temporary_name=temporary_name, on_created=on_created,
                meter_allocation=meter_allocation) as staged:
            # An OS handle alone does not exclude eviction. Keep the ledger pin
            # outside winner handles so confirmation retries cannot release it.
            with _pin(vault, target, object_key=target.relative_to(vault).as_posix()), ExitStack() as handles:
                prepared = PreparedOriginal(vault, digest, size, staged, handles)
                if not replacing:  # A damaged object is no winner to confirm; the repair replaces it.
                    prepared.confirm_existing()
                yield prepared


@contextmanager
def open_object(vault: Path, digest: str, size: int | None) -> Iterator[reserved_paths.GenericBinarySource]:
    """Open bytes after logical authorization; the digest itself grants no public access.

    A `size` of None opens an object of any size, for a reader that hashes every byte to judge it.
    """
    with ExitStack() as held:
        # The authority covers the open alone. The source keeps its own for later checks, and a request may enter
        # this in a worker thread and leave it on the event loop, where a scope held across the yield cannot reset.
        with reserved_paths._subsystem_authority_scope(OWNER):
            source = held.enter_context(
                reserved_paths._open_owner_binary(vault, object_path(vault, digest), DESCRIPTOR_ID))
        if size is not None and source.version.size != size:
            raise ObjectMismatch("the shared original differs from its canonical binding")
        yield source


def locally_present(vault: Path, digest: str, size: int) -> bool:
    """A bounded metadata probe, distinct from full byte verification; an object of another size raises ObjectMismatch."""
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


def evict_object(
    vault: Path, digest: str, *, expected_version: held_fs.FileVersion,
    before_unlink: Callable[[], None] | None = None,
) -> None:
    """Remove one verified physical version after the ledger proves every binding recoverable."""
    with reserved_paths._subsystem_authority_scope(OWNER):
        reserved_paths._remove_owner_file(vault, object_path(vault, digest), DESCRIPTOR_ID,
                                         expected_version=expected_version, before_unlink=before_unlink)
