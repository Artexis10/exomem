"""Bind canonical copy obligations to the private native allocation protocol."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import generated_fragment_store, held_fs, source_custody, source_native_custody


@dataclass(frozen=True)
class CopyAllocation:
    canonical: generated_fragment_store.Copy
    native: source_native_custody.NativeReservation


@dataclass(frozen=True)
class PreparedCopy:
    canonical: generated_fragment_store.Copy
    image: generated_fragment_store.ImageHeader
    logical_path: Path
    _seal: object = field(repr=False, compare=False)


class NativeCopyCustody:
    """One producer's copy reservations, verified allocations and actual closure facts."""

    def __init__(self, root: Path, *, store: generated_fragment_store.GeneratedFragmentStore,
                 native: source_native_custody.NativeCustody):
        self.root, self.store, self.native = root, store, native
        self.ledger_id, self.physical_scope = native.ledger_id, native.physical_scope
        self._seal = object()

    def _check_allocation(self, copy: CopyAllocation) -> None:
        if (copy.canonical.owner.owner != source_native_custody.OWNER
                or copy.canonical.owner.version != copy.native.binding_id
                or copy.canonical.origin_ledger != self.ledger_id
                or copy.canonical.copy_id != copy.native.generated_copy_id
                or copy.canonical.image_id != copy.native.image_id):
            raise ValueError("canonical copy differs from its native allocation")

    def prepare(self, image: generated_fragment_store.ImageHeader, *, logical_path: Path) -> PreparedCopy:
        """Reserve the expected copy before its private native name is available."""
        binding_id = uuid.uuid4().hex
        owner = source_custody.OwnerLocator(
            owner=source_native_custody.OWNER,
            locator=generated_fragment_store.file_locator(image.catalogue_id, logical_path.relative_to(self.root).as_posix()),
            version=binding_id,
        )
        canonical = self.store.reserve_copy(
            image.image_id, owner=owner, origin_ledger=self.ledger_id,
            expected_header=image, expected_root=self.root,
        )
        return PreparedCopy(canonical, image, logical_path, self._seal)

    def allocate(self, prepared: PreparedCopy, *, relative_path: str,
                 destination: str | None = None, removable: bool = True,
                 publication_id: str | None = None) -> CopyAllocation:
        """Bind the still-open canonical reservation before any native payload bytes."""
        if not isinstance(prepared, PreparedCopy) or prepared._seal is not self._seal:
            raise ValueError("prepared copy belongs to another producer")
        canonical, image = prepared.canonical, prepared.image
        observed = self.store.copy_record(canonical.copy_id)
        current = self.store.image_header(image.image_id)
        locator = generated_fragment_store.file_locator(image.catalogue_id, prepared.logical_path.relative_to(self.root).as_posix())
        if (observed.state != "observed" or observed.value != canonical or canonical.outcome != "reserved"
                or current.state != "observed"
                # Summary folds may raise U without changing the sealed image's content binding.
                or current.value.model_copy(update={"unknown_through_epoch": image.unknown_through_epoch}) != image
                or current.value.unknown_through_epoch < image.unknown_through_epoch
                or canonical.image_id != image.image_id or canonical.origin_ledger != self.ledger_id
                or canonical.owner.owner != source_native_custody.OWNER or canonical.owner.locator != locator
                or destination is not None and destination != prepared.logical_path.relative_to(self.root).as_posix()):
            raise ValueError("prepared copy differs from its canonical reservation")
        # A missing private row cannot close the canonical obligation if this reservation fails.
        native = self.native.reserve(
            relative_path=relative_path, expected_sha256=image.image_sha256, expected_size=image.image_size,
            source_ref=image.whole_source_ref, image_id=image.image_id, generated_copy_id=canonical.copy_id,
            publication_id=publication_id, destination_relative=destination,
            removable=removable, binding_id=canonical.owner.version,
        )
        return CopyAllocation(canonical, native)

    def reserve(self, image: generated_fragment_store.ImageHeader, *, logical_path: Path,
                relative_path: str, destination: str | None = None, removable: bool = True,
                publication_id: str | None = None) -> CopyAllocation:
        return self.allocate(self.prepare(image, logical_path=logical_path), relative_path=relative_path,
                             destination=destination, removable=removable, publication_id=publication_id)

    def verified(self, copy: CopyAllocation, file: held_fs.HeldFile, digest: str) -> None:
        self._check_allocation(copy)
        self.native.verified(copy.native, file, sha256=digest)

    def present(self, copy: CopyAllocation) -> None:
        self._check_allocation(copy)
        self.store.ack_copy(copy.canonical.copy_id, owner=copy.canonical.owner, outcome="present")

    def installed(self, copy: CopyAllocation, witness: held_fs.InstallationWitness) -> None:
        self._check_allocation(copy)
        self.native.record_installation(copy.native, witness)

    def retired(self, copy: CopyAllocation, witness: held_fs.RetirementWitness) -> None:
        self._check_allocation(copy)
        self.native.record_retirement(copy.native, witness)
        self.store.ack_copy(copy.canonical.copy_id, owner=copy.canonical.owner, outcome="retired")

    def closed(self, copy: CopyAllocation, identity: held_fs.StableIdentity | None,
               outcome: Literal["not_written", "removed", "installed"]) -> None:
        self._check_allocation(copy)
        reservation = self.native.get(copy.native.binding_id)
        if reservation.identity is None and outcome != "installed":
            # Binding always precedes bytes; a live unbound reservation cannot have copied its payload.
            identity, outcome = None, "not_written"
        self.native.acknowledge(reservation, identity, outcome)
        self.store.ack_copy(
            copy.canonical.copy_id, owner=copy.canonical.owner,
            outcome="no_effect" if outcome == "not_written" else "retired",
        )
