"""Bind canonical copy obligations to the private native allocation protocol."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import generated_fragment_store, held_fs, source_custody, source_native_custody


@dataclass(frozen=True)
class CopyAllocation:
    canonical: generated_fragment_store.Copy
    native: source_native_custody.NativeReservation


class NativeCopyCustody:
    """One producer's copy reservations, verified allocations and actual closure facts."""

    def __init__(self, root: Path, *, store: generated_fragment_store.GeneratedFragmentStore,
                 native: source_native_custody.NativeCustody):
        self.root, self.store, self.native = root, store, native
        self.ledger_id, self.physical_scope = native.ledger_id, native.physical_scope

    def reserve(self, image: generated_fragment_store.ImageHeader, *, logical_path: Path,
                relative_path: str, destination: str | None = None, removable: bool = True,
                publication_id: str | None = None) -> CopyAllocation:
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
        # A missing private row cannot close the canonical obligation if this reservation fails.
        native = self.native.reserve(
            relative_path=relative_path, expected_sha256=image.image_sha256, expected_size=image.image_size,
            source_ref=image.whole_source_ref, image_id=image.image_id, generated_copy_id=canonical.copy_id,
            publication_id=publication_id, destination_relative=destination,
            removable=removable, binding_id=binding_id,
        )
        return CopyAllocation(canonical, native)

    def verified(self, copy: CopyAllocation, file: held_fs.HeldFile, digest: str) -> None:
        self.native.verified(copy.native, file, sha256=digest)

    def present(self, copy: CopyAllocation) -> None:
        self.store.ack_copy(copy.canonical.copy_id, owner=copy.canonical.owner, outcome="present")

    def installed(self, copy: CopyAllocation, witness: held_fs.InstallationWitness) -> None:
        self.native.record_installation(copy.native, witness)

    def closed(self, copy: CopyAllocation, identity: held_fs.StableIdentity | None,
               outcome: Literal["not_written", "removed", "installed"]) -> None:
        reservation = self.native.get(copy.native.binding_id)
        if reservation.identity is None and outcome != "installed":
            # Binding always precedes bytes; a live unbound reservation cannot have copied its payload.
            identity, outcome = None, "not_written"
        self.native.acknowledge(reservation, identity, outcome)
        self.store.ack_copy(
            copy.canonical.copy_id, owner=copy.canonical.owner,
            outcome="no_effect" if outcome == "not_written" else "retired",
        )
