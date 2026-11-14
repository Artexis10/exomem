"""Canonical Source enrollment and exact producer publication reservations."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from . import memory_refs, reserved_paths, source_capture, source_corrections, vault
from .cli_ops import OpError

DESCRIPTOR_ID = "source-custody"
OWNER = "source_custody"
# These are bounded metadata I/O and work limits, not a content vocabulary.
MAX_MANIFEST_BYTES = 64 * 1024
MAX_OWNER_FAMILIES = 32
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


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    source_ref: _Opaque
    allocated_hash: _Hash
    publication: Literal["allocated", "published"] = "allocated"
    dependency: DependencyLatch = Field(default_factory=DependencyLatch)
    coverage_complete: bool = False
    generated_birth: ProducerBirth | None = None
    owners: Annotated[dict[str, OwnerLocator], Field(max_length=MAX_OWNER_FAMILIES)] = Field(default_factory=dict)
    reservations: Annotated[dict[str, Publication], Field(max_length=MAX_OWNER_FAMILIES)] = Field(default_factory=dict)
    retirement: RetirementFence | None = None


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
                                       "reservations": reservations, "dependency": dependency})
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
