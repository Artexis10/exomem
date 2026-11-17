"""Canonical Source enrollment and exact producer publication reservations."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from . import memory_refs, reserved_paths, source_capture, source_corrections, vault
from .cli_ops import OpError

DESCRIPTOR_ID = "source-custody"
OWNER = "source_custody"
# These are bounded metadata I/O and work limits, not a content vocabulary.
MAX_MANIFEST_BYTES = 64 * 1024
MAX_OWNER_FAMILIES = 32
MAX_ORIGINAL_BINDINGS = 64
_Hash = Annotated[str, AfterValidator(source_capture.validate_sha256)]
_Opaque = Annotated[str, Field(min_length=1, max_length=1024)]


class OwnerLocator(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    owner: Annotated[str, Field(min_length=1, max_length=128)]
    locator: _Opaque
    version: _Opaque


class RetirementFence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    operation_id: _Opaque
    source_hash: _Hash
    # The content-free completion record lets a later operation answer without repeating cleanup.
    complete: bool = False


class OriginalBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    sha256: _Hash
    size: Annotated[int, Field(ge=0)]


# State literals belong to this custody protocol; producer families and locators remain data.
class DependencyLatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    uncertain: bool = False
    first_publication: OwnerLocator | None = None

    @property
    def state(self) -> Literal["clear", "present", "uncertain"]:
        return "present" if self.first_publication is not None else "uncertain" if self.uncertain else "clear"


class ProducerBirth(BaseModel):
    """A producer's durable checkpoint before a fresh incarnation's generated output."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    catalogue_id: _Opaque
    birth_epoch: Annotated[int, Field(gt=0)]


class Publication(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    source_ref: _Opaque
    publication_id: _Opaque
    producer: OwnerLocator
    primary_path: Annotated[str, Field(min_length=1)]
    primary_ref: _Opaque | None
    primary_hash: _Hash
    state: Literal["pending", "published"] = "pending"


class DependencyEpoch(BaseModel):
    """One bounded recovery interval; the producer retains its exact operations."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    epoch_id: _Opaque
    revision: Annotated[int, Field(gt=0)]
    producer: OwnerLocator
    opening_checkpoint: _Opaque
    state: Literal["open", "closed"] = "open"
    closing_checkpoint: _Opaque | None = None

    @model_validator(mode="after")
    def coherent(self):
        if (self.state == "closed") != (self.closing_checkpoint is not None):
            raise ValueError("dependency closure requires its producer checkpoint")
        return self


class DependencyAdmission(BaseModel):
    """Publication right for one operation, valid only while this epoch stays open."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source_ref: _Opaque
    epoch_id: _Opaque
    operation_id: _Opaque
    producer: OwnerLocator


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[2] = 2
    source_ref: _Opaque
    allocated_hash: _Hash
    originals: Annotated[tuple[OriginalBinding, ...], Field(max_length=MAX_ORIGINAL_BINDINGS)] | None = None
    publication: Literal["allocated", "published"] = "allocated"
    dependency: DependencyLatch = Field(default_factory=DependencyLatch)
    coverage_complete: bool = False
    generated_birth: ProducerBirth | None = None
    owners: Annotated[dict[str, OwnerLocator], Field(max_length=MAX_OWNER_FAMILIES)] = Field(default_factory=dict)
    reservations: Annotated[dict[str, Publication], Field(max_length=MAX_OWNER_FAMILIES)] = Field(default_factory=dict)
    legacy_dependency_uncertain: bool = False
    dependency_epochs: Annotated[dict[str, DependencyEpoch], Field(max_length=MAX_OWNER_FAMILIES)] = Field(default_factory=dict)
    retirement: RetirementFence | None = None

    @field_validator("originals", mode="before")
    @classmethod
    def json_originals(cls, value, info: ValidationInfo):
        # JSON arrays encode tuple metadata; Python callers must still supply a tuple.
        return tuple(value) if info.mode == "json" and isinstance(value, list) else value

    @model_validator(mode="before")
    @classmethod
    def legacy_manifest(cls, value):
        if isinstance(value, dict) and type(value.get("version")) is int and value["version"] == 1:
            # Version one lost displaced publications; upgrading cannot manufacture their closure.
            dependency = value.get("dependency", {})
            value = {**value, "version": 2, "legacy_dependency_uncertain":
                     bool(dependency.get("uncertain", False)) if isinstance(dependency, dict) else False}
        return value

    @model_validator(mode="after")
    def coherent_dependencies(self):
        if any(family != epoch.producer.owner for family, epoch in self.dependency_epochs.items()):
            raise ValueError("dependency epoch belongs to a different producer family")
        uncertain = self.legacy_dependency_uncertain or any(
            epoch.state == "open" for epoch in self.dependency_epochs.values())
        if self.dependency.uncertain != uncertain:
            raise ValueError("dependency summary differs from its retained evidence")
        return self


@dataclass(frozen=True)
class PlannedCustody:
    writes: tuple[vault.PlannedWrite, ...]


def available() -> bool:
    """Registration is an integration dependency, never inferred from a directory."""
    return any(item.id == DESCRIPTOR_ID and item.owner == OWNER
               and item.placement is reserved_paths.StatePlacement.VAULT_CANONICAL
               and item.authority_enabled for item in reserved_paths.internal_state_registry())


def manifest_root(root: Path) -> Path:
    return source_corrections.receipt_root(root).parent / "custody"


def manifest_path(root: Path, reference: str) -> Path:
    identity = memory_refs.parse_memory_ref(reference)
    if identity is None or memory_refs.memory_ref(identity) != reference:
        raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "Source custody requires a canonical reference")
    return manifest_root(root) / f"{identity}.json"


def _boundary(root: Path) -> None:
    from .writer_lease import active_manager

    boundary = active_manager()._mutation_coordinator_for(root)
    if not boundary.current_thread_holds_boundary() or boundary.current_thread_borrows_boundary():
        raise OpError("SOURCE_CUSTODY_BOUNDARY_REQUIRED", "Source custody requires the actual writer boundary")


def _read(root: Path, path: Path, model, limit: int):
    if not available():
        raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "canonical Source custody is not registered")
    relative = path.relative_to(root).as_posix()
    with reserved_paths._subsystem_authority_scope(OWNER):
        try:
            absent = vault.PathGuard.capture(root, relative, leaf_policy="absent")
        except vault.PathGuardError:
            raw, guard = vault.read_bounded_guarded_bytes(root, relative, limit=limit)
        else:
            return None, absent
    try:
        return model.model_validate_json(raw), guard
    except ValidationError:
        raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "canonical Source custody is unreadable") from None


def read_manifest(root: Path, reference: str) -> tuple[Manifest | None, vault.PathGuard]:
    value, guard = _read(root, manifest_path(root, reference), Manifest, MAX_MANIFEST_BYTES)
    if value is not None and value.source_ref != reference:
        raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "canonical Source custody has a different reference")
    return value, guard


def _write(root: Path, path: Path, value: BaseModel, guard: vault.PathGuard, limit: int) -> vault.PlannedWrite:
    content = json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    if len(content.encode("utf-8")) > limit:
        raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "canonical Source custody exceeds its metadata bound")
    return vault.PlannedWrite(path, content, create_only=guard.expects_absence, guard=guard)


def publish(root: Path, plan: PlannedCustody) -> None:
    """Publish only this registered owner's metadata; caller bodies cannot join its batch."""
    _boundary(root)
    if not available():
        raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "canonical Source custody is not registered")
    if not plan.writes:
        return
    from . import curation_witness
    from .governance.principal import owner_local_producer

    checked = []
    with reserved_paths._subsystem_authority_scope(OWNER):
        for write in plan.writes:
            try:
                content, path, guard = write.content, Path(write.path), write.guard
                if not isinstance(content, str):
                    raise ValueError("metadata is not textual")
                manifest = Manifest.model_validate_json(content)
                valid = (reserved_paths.state_target_descriptor_id(root, path) == DESCRIPTOR_ID
                         and path == manifest_path(root, manifest.source_ref)
                         and isinstance(guard, vault.PathGuard) and guard.target == path.relative_to(root).as_posix()
                         and not write.ensure_directories and len(content.encode("utf-8")) <= MAX_MANIFEST_BYTES)
            except (ValidationError, TypeError, ValueError):
                valid = False
            if not valid:
                raise OpError("SOURCE_CUSTODY_TARGET_INVALID", "Source custody accepts only its exact guarded manifest paths")
            checked.append(vault.PlannedWrite(path, content, create_only=write.create_only, guard=guard, expected_hash=write.expected_hash))
        # Shared retention metadata uses service authority only after every immutable target and payload is bound.
        with curation_witness.suspended(), owner_local_producer(root, OWNER):
            # A reservation survives failure without claiming the parent's Source or semantic body committed.
            vault.batch_atomic_write(checked, vault_root=root,
                                     commit_point=False, post_commit_fanout=False)


def prepare_enrollment(root: Path, *, reference: str, source_hash: str) -> PlannedCustody:
    current, guard = read_manifest(root, reference)
    if current is not None:
        if current.allocated_hash != source_hash:
            raise OpError("SOURCE_CUSTODY_TARGET_CHANGED", "Source allocation differs from canonical custody")
        require_publishable(root, reference)
        return PlannedCustody(())
    value = Manifest(source_ref=reference, allocated_hash=source_hash)
    return PlannedCustody((_write(root, manifest_path(root, reference), value, guard, MAX_MANIFEST_BYTES),))


def require_publishable(root: Path, reference: str) -> None:
    if not available():
        return
    manifest, _ = read_manifest(root, reference)
    if manifest is not None and manifest.retirement is not None:
        raise OpError("SOURCE_RETIRED", "the Source incarnation is retired")


def bind_originals(root: Path, *, reference: str, source_hash: str,
                   originals: tuple[OriginalBinding, ...]) -> None:
    """Retain the trusted capture's original declarations against its exact allocation."""
    _boundary(root)
    if type(originals) is not tuple or any(type(item) is not OriginalBinding for item in originals):
        raise ValueError("Source original declarations require typed bindings")
    checked = (OriginalBinding(sha256=item.sha256, size=item.size) for item in originals)
    declarations = tuple(sorted(set(checked), key=lambda item: (item.sha256, item.size)))
    manifest, guard = read_manifest(root, reference)
    if manifest is None:
        raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "Source allocation custody is unavailable")
    if manifest.allocated_hash != source_hash:
        raise OpError("SOURCE_CUSTODY_TARGET_CHANGED", "Source allocation differs from canonical custody")
    require_publishable(root, reference)
    if manifest.originals is not None:
        if manifest.originals == declarations:
            return
        # An issued Source cannot silently switch originals; the caller must retry the correct allocation.
        raise OpError("SOURCE_CUSTODY_TARGET_CHANGED", "Source originals differ from their issued binding")
    updated = Manifest.model_validate({**manifest.model_dump(), "originals": declarations})
    publish(root, PlannedCustody((_write(root, manifest_path(root, reference), updated, guard, MAX_MANIFEST_BYTES),)))


def register_owner(root: Path, reference: str, owner: OwnerLocator) -> None:
    """Retain one canonical owner locator per family before its payload can survive."""
    _boundary(root)
    manifest, guard = read_manifest(root, reference)
    if manifest is None:
        raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "Source allocation custody is unavailable")
    require_publishable(root, reference)
    previous = manifest.owners.get(owner.owner)
    if previous is not None:
        if previous != owner:
            raise OpError("SOURCE_CUSTODY_TARGET_CHANGED", "Source custody belongs to another owner instance")
        return
    value = Manifest.model_validate({**manifest.model_dump(), "owners": {**manifest.owners, owner.owner: owner}})
    publish(root, PlannedCustody((_write(root, manifest_path(root, reference), value, guard, MAX_MANIFEST_BYTES),)))


def issue_birth(root: Path, reference: str, allocate: Callable[[], ProducerBirth]) -> None:
    """Checkpoint a fresh allocation before its first generated copy, so its copy census is definitive.

    The caller proves that payload, result and native hooks all record this capture. An allocation that
    already registered a producer, or already published, may hold copies from before its birth.
    """
    _boundary(root)
    manifest, guard = read_manifest(root, reference)
    if manifest is None:
        raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "Source allocation custody is unavailable")
    require_publishable(root, reference)
    if manifest.generated_birth is not None or manifest.owners or manifest.publication != "allocated":
        return
    updated = Manifest.model_validate({**manifest.model_dump(), "generated_birth": allocate(),
                                       "coverage_complete": True})
    publish(root, PlannedCustody((_write(root, manifest_path(root, reference), updated, guard, MAX_MANIFEST_BYTES),)))


#: Lifecycle operations refuse an enrolled Source with this code; the reason names sources.erase only where it can run.
ERASE_REQUIRED = "SOURCE_ERASE_REQUIRED"


#: Vault state, not process memory: a standby that builds a catalogue in another process must see a cutoff.
_GENERATION_NAME = ".source-retirement-generation"
_GENERATIONS: dict[str, tuple[tuple[int, int, int], int]] = {}
_GENERATIONS_LOCK = threading.Lock()


def _generation_path(root: Path) -> Path:
    from .state_paths import vault_state_dir

    return vault_state_dir(Path(root)) / _GENERATION_NAME


def retirement_generation(root: Path) -> int | None:
    """A per-vault counter that reusable caches capture before reading and check before inserting.

    None when it cannot be read: no cutoff can then be ruled out, so a cache neither stores nor serves.
    """
    path = _generation_path(root)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    # A replacement is a new file, so its identity and time change on every advance.
    signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    with _GENERATIONS_LOCK:
        cached = _GENERATIONS.get(str(path))
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        value = int(path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None
    with _GENERATIONS_LOCK:
        _GENERATIONS[str(path)] = (signature, value)
    return value


def retired_since(root: Path, generation: int | None) -> bool:
    """Whether a cutoff may have landed since ``generation`` was read: True when either read failed."""
    current = retirement_generation(root)
    return current is None or generation is None or current != generation


def advance_retirement_generation(root: Path) -> bool:
    """Void every reusable cache entry read before now, in any process; False when vault state refused it."""
    import os

    from .state_paths import ensure_vault_state_dir

    _boundary(root)
    current = retirement_generation(root)
    if current is None:
        return False
    try:
        directory = ensure_vault_state_dir(Path(root))
    except OSError:
        return False
    temporary = directory / f"{_GENERATION_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "x", encoding="ascii") as handle:
            handle.write(f"{current + 1}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / _GENERATION_NAME)
    except OSError:
        temporary.unlink(missing_ok=True)
        return False
    return True


def retire(root: Path, reference: str, fence: RetirementFence) -> None:
    """Persist the cutoff under the actual writer boundary, then invalidate reusable cache publication."""
    _boundary(root)
    manifest, guard = read_manifest(root, reference)
    if manifest is None:
        raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "Source allocation custody is unavailable")
    if manifest.retirement is None or manifest.retirement.complete != fence.complete:
        updated = Manifest.model_validate({**manifest.model_dump(), "retirement": fence.model_dump()})
        publish(root, PlannedCustody((_write(root, manifest_path(root, reference), updated, guard, MAX_MANIFEST_BYTES),)))
    # The eraser's caches component advances again once derived rows are gone and reports a refusal there.
    advance_retirement_generation(root)


def erase_refusal(root: Path, markdown: str) -> tuple[str, str] | None:
    """The refusal for moving or removing this page when it is an enrolled Source incarnation, else None.

    A trashed or moved copy would sit outside the copy census that lets an erase report completion.
    """
    if not available():
        return None
    reference = memory_refs.ref_from_markdown(markdown)
    if reference is None or read_manifest(root, reference)[0] is None:
        return None
    from . import source_erasure

    try:
        fields, _body, _raw = vault.parse_frontmatter(markdown)
        system = source_capture.validate_identity(fields.get("source_identity"))["system"]
        policy = source_erasure.SourceErasurePolicy.from_env()
        removable = source_erasure.admission_refusal(root, policy, system) is None
    except (ValueError, source_capture.SourceCaptureError, OpError):
        removable = False
    if removable:
        return ERASE_REQUIRED, "this captured Source is held for erasure; remove it with sources.erase"
    return ERASE_REQUIRED, "this captured Source is held under capture custody, and this cell cannot remove it"


def erase_refusal_at(root: Path, relative: str) -> tuple[str, str] | None:
    """Read one file-mode page, or the page of the original that it names as ``<filename>.md``."""
    if not available():
        return None
    page = relative if relative.lower().endswith(".md") else f"{relative}.md"
    try:
        markdown = reserved_paths.read_generic_bytes(root, page).data.decode("utf-8")
    except (reserved_paths.ReservedPathLeafError, UnicodeDecodeError):
        return None  # The caller's own read reports a missing, held or unreadable page.
    return erase_refusal(root, markdown)


def _exact_primary(root: Path, *, path: str, reference: str | None, source_hash: str) -> bool:
    if reference is None:
        return False
    try:
        source, guard = vault.read_guarded_text(root, root / path)
        exact = memory_refs.ref_from_markdown(source) == reference and vault.content_hash(source) == source_hash
        guard.recheck(root)
    except (OSError, vault.PathGuardError):
        return False
    return exact


def observe_capture(root: Path, reference: str, *, path: str) -> bool:
    """An exact Source after-image proves that Source, independently of its caller's terminal."""
    _boundary(root)
    manifest, guard = read_manifest(root, reference)
    if manifest is None or not _exact_primary(root, path=path, reference=reference, source_hash=manifest.allocated_hash):
        return False
    if manifest.publication != "published":
        updated = manifest.model_copy(update={"publication": "published"})
        publish(root, PlannedCustody((_write(root, manifest_path(root, reference), updated, guard, MAX_MANIFEST_BYTES),)))
    return True


def begin_capture(root: Path, *, reference: str, path: str, source_hash: str,
                  identity: source_capture.SourceIdentity | None) -> bool:
    from . import capture_attempts, source_erasure

    if not available() or identity is None:
        return False
    # Explicit attempts retain custody independently of erasure admission or later policy errors.
    attempt_owned = capture_attempts.record_allocation(root, reference=reference, source_hash=source_hash, path=path)
    if not attempt_owned:
        try:
            policy = source_erasure.SourceErasurePolicy.from_env()
        except OpError:
            return False  # Erasure configuration cannot gate an ordinary unreserved capture.
        if identity["system"] not in policy.enabled_systems:
            return False
    _boundary(root)
    publish(root, prepare_enrollment(root, reference=reference, source_hash=source_hash))
    return True


def finish_capture(root: Path, reference: str, *, path: str) -> None:
    from . import capture_attempts

    if available() and observe_capture(root, reference, path=path):
        capture_attempts.record_published(root, reference)


def reserve_publication(root: Path, *, reference: str, producer: OwnerLocator, primary: vault.PlannedWrite) -> Publication | None:
    """Reserve before parent staging; missing legacy coverage remains visibly unproved."""
    _boundary(root)
    manifest, manifest_guard = read_manifest(root, reference)
    if manifest is None:
        return None
    require_publishable(root, reference)
    primary_ref = memory_refs.ref_from_markdown(primary.content)
    publication = Publication(source_ref=reference, publication_id=str(uuid.uuid4()), producer=producer,
                              primary_path=primary.path.relative_to(root).as_posix(), primary_ref=primary_ref,
                              primary_hash=vault.content_hash(primary.content))
    owners = dict(manifest.owners)
    owners.setdefault(producer.owner, producer)
    reservations = {**manifest.reservations, producer.owner: publication}
    # Replacing an unresolved owner entry cannot erase uncertainty about an older publication.
    dependency = manifest.dependency.model_copy(update={"uncertain": True})
    updated = Manifest.model_validate({**manifest.model_dump(), "owners": owners,
                                       "reservations": reservations, "dependency": dependency,
                                       "legacy_dependency_uncertain": True})
    publish(root, PlannedCustody((_write(root, manifest_path(root, reference), updated, manifest_guard, MAX_MANIFEST_BYTES),)))
    return publication


def observe_publication(root: Path, publication: Publication) -> bool:
    """Absence cannot prove rollback; interrupted or unexplained publication stays uncertain."""
    _boundary(root)
    if not _exact_primary(root, path=publication.primary_path, reference=publication.primary_ref, source_hash=publication.primary_hash):
        return False
    manifest, manifest_guard = read_manifest(root, publication.source_ref)
    if manifest is None:
        raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "publication reservation is unavailable")
    dependency = manifest.dependency.model_copy(update={
        "first_publication": manifest.dependency.first_publication or publication.producer,
    })
    reservations = dict(manifest.reservations)
    current = reservations.get(publication.producer.owner)
    if current is not None and current.publication_id == publication.publication_id:
        if current != publication and current != publication.model_copy(update={"state": "published"}):
            raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "publication witness differs from its reservation")
        reservations[publication.producer.owner] = current.model_copy(update={"state": "published"})
    updated = manifest.model_copy(update={"dependency": dependency, "reservations": reservations})
    publish(root, PlannedCustody((_write(root, manifest_path(root, publication.source_ref), updated, manifest_guard, MAX_MANIFEST_BYTES),)))
    return True


def reserve_dependencies(root: Path, references: Sequence[str], *, producer: OwnerLocator,
                         operation_id: str, checkpoint: str) -> tuple[DependencyAdmission, ...]:
    """Join one recovery epoch per Source, before this complete operation can retain bytes."""
    if not available() or not references:
        return ()
    _boundary(root)
    admissions = []
    writes = []
    for reference in dict.fromkeys(references):
        manifest, guard = read_manifest(root, reference)
        if manifest is None:
            continue
        if manifest.retirement is not None:
            raise OpError("SOURCE_RETIRED", "the Source incarnation is retired")
        previous = manifest.dependency_epochs.get(producer.owner)
        if previous is not None and previous.producer != producer:
            raise OpError("SOURCE_CUSTODY_TARGET_CHANGED", "dependency recovery belongs to another producer scope")
        if previous is not None and previous.state == "open":
            epoch = previous.model_copy(update={"revision": previous.revision + 1})
        else:
            # Closed identifiers never reopen: that would restore a delayed publisher's revoked right.
            epoch = DependencyEpoch(epoch_id=str(uuid.uuid4()), revision=1, producer=producer,
                                    opening_checkpoint=checkpoint)
        admission = DependencyAdmission(source_ref=reference, epoch_id=epoch.epoch_id,
                                         operation_id=operation_id, producer=producer)
        epochs = {**manifest.dependency_epochs, producer.owner: epoch}
        updated = Manifest.model_validate({**manifest.model_dump(), "dependency_epochs": epochs,
                                           "dependency": manifest.dependency.model_copy(update={"uncertain": True})})
        writes.append(_write(root, manifest_path(root, reference), updated, guard, MAX_MANIFEST_BYTES))
        admissions.append(admission)
    publish(root, PlannedCustody(tuple(writes)))
    return tuple(admissions)


def require_dependencies(root: Path, admissions: Sequence[DependencyAdmission]) -> None:
    """Recheck immediately before each effect, under the same actual boundary hold."""
    if not admissions:
        return
    _boundary(root)
    for admission in admissions:
        manifest, _ = read_manifest(root, admission.source_ref)
        if manifest is None:
            raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "issued dependency custody is unavailable")
        if manifest.retirement is not None:
            raise OpError("SOURCE_RETIRED", "the Source incarnation is retired")
        epoch = manifest.dependency_epochs.get(admission.producer.owner)
        if (epoch is None or epoch.state != "open" or epoch.epoch_id != admission.epoch_id
                or epoch.producer != admission.producer):
            raise OpError("SOURCE_DEPENDENCY_STALE", "dependency recovery closed this publication right")


def begin_dependency_effect(root: Path, admissions: Sequence[DependencyAdmission]) -> None:
    """Invalidate older censuses before an effect; keep this boundary held through that effect.

    Admission alone does not cover a later commit: a census may already have passed this
    producer when it publishes. Advancing before every staging or canonical commit prevents
    that commit from disappearing behind a crashed positive acknowledgment.
    """
    if not admissions:
        return
    require_dependencies(root, admissions)
    writes = []
    families: dict[str, set[str]] = {}
    for admission in admissions:
        families.setdefault(admission.source_ref, set()).add(admission.producer.owner)
    for reference, owners in families.items():
        manifest, guard = read_manifest(root, reference)
        epochs = {family: epoch.model_copy(update={"revision": epoch.revision + 1}) if family in owners else epoch
                  for family, epoch in manifest.dependency_epochs.items()}
        updated = manifest.model_copy(update={"dependency_epochs": epochs})
        writes.append(_write(root, manifest_path(root, reference), updated, guard, MAX_MANIFEST_BYTES))
    publish(root, PlannedCustody(tuple(writes)))


def observe_dependency(root: Path, admission: DependencyAdmission, *, witness: OwnerLocator) -> None:
    """Retain the trusted producer's exact immutable effect witness, including delayed positives."""
    _boundary(root)
    if witness.owner != admission.producer.owner:
        raise OpError("SOURCE_CUSTODY_TARGET_CHANGED", "dependency witness belongs to another producer family")
    manifest, guard = read_manifest(root, admission.source_ref)
    if manifest is None:
        raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "issued dependency custody is unavailable")
    if manifest.dependency.first_publication is not None:
        return
    dependency = manifest.dependency.model_copy(update={"first_publication": witness})
    updated = manifest.model_copy(update={"dependency": dependency})
    publish(root, PlannedCustody((_write(root, manifest_path(root, admission.source_ref), updated, guard, MAX_MANIFEST_BYTES),)))


def reconcile_dependencies(root: Path, reference: str, *, producer: OwnerLocator, expected: DependencyEpoch,
                           checkpoint: str, complete: bool) -> bool:
    """Close only the producer's complete, revision-qualified census; individual rollback is insufficient.

    The producer verifies exact effects and cleanup before calling this function. It closes SQL
    snapshots and releases its request checkout first. The boundary keeps this CAS and the final
    producer checkpoint check in one hold, so a new admission invalidates an earlier census.
    """
    _boundary(root)
    if not complete or expected.producer != producer or expected.state != "open":
        return False
    manifest, guard = read_manifest(root, reference)
    if manifest is None:
        raise OpError("SOURCE_CUSTODY_UNAVAILABLE", "dependency recovery custody is unavailable")
    closed = expected.model_copy(update={"state": "closed", "closing_checkpoint": checkpoint})
    current = manifest.dependency_epochs.get(producer.owner)
    if current == closed:
        return True
    if current != expected:
        return False
    epochs = {**manifest.dependency_epochs, producer.owner: closed}
    dependency = manifest.dependency.model_copy(update={"uncertain": manifest.legacy_dependency_uncertain
        or any(epoch.state == "open" for epoch in epochs.values())})
    updated = manifest.model_copy(update={"dependency_epochs": epochs, "dependency": dependency})
    publish(root, PlannedCustody((_write(root, manifest_path(root, reference), updated, guard, MAX_MANIFEST_BYTES),)))
    return True


@contextmanager
def producing(root: Path, references: Sequence[str], *, owner: str, locator: str, version: str, primary: vault.PlannedWrite) -> Iterator[None]:
    """Keep producer reservations canonical without widening the parent's write authority."""
    if not available() or not references:
        yield
        return
    producer = OwnerLocator(owner=owner, locator=locator, version=version)
    reservations = [reserve_publication(root, reference=reference, producer=producer, primary=primary)
                    for reference in dict.fromkeys(references)]
    try:
        yield
    finally:
        # An exact primary can publish even when a later auxiliary or terminal fails.
        for reservation in reservations:
            if reservation is not None:
                observe_publication(root, reservation)
