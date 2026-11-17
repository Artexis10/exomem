"""Bind canonical copy obligations to the private native allocation protocol."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import generated_fragment_store, held_fs, source_custody, source_native_custody, vault


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


@dataclass
class NativePublication:
    """One native attempt retains its actual allocation and witnesses before acknowledgements."""

    prepared: PreparedCopy
    custody: vault._ArtifactCustody | None = None
    allocation: CopyAllocation | None = None
    temporary: CopyAllocation | None = None
    installation: held_fs.InstallationWitness | None = None
    retirement: held_fs.RetirementWitness | None = None
    allocation_started: bool = False


class NativeCopyCustody:
    """One producer's copy reservations, verified allocations and actual closure facts."""

    def __init__(self, root: Path, *, store: generated_fragment_store.GeneratedFragmentStore,
                 native: source_native_custody.NativeCustody):
        self.root, self.store, self.native = root, store, native
        self.ledger_id, self.physical_scope = native.ledger_id, native.physical_scope
        self._seal = object()
        self._ahead: dict[tuple[str, Path], list[PreparedCopy]] = {}

    def _check_allocation(self, copy: CopyAllocation) -> None:
        if (copy.canonical.owner.owner != source_native_custody.OWNER
                or copy.canonical.owner.version != copy.native.binding_id
                or copy.canonical.origin_ledger != self.ledger_id
                or copy.canonical.copy_id != copy.native.generated_copy_id
                or copy.canonical.image_id != copy.native.image_id):
            raise ValueError("canonical copy differs from its native allocation")

    def prepare(self, image: generated_fragment_store.ImageHeader, *, logical_path: Path) -> PreparedCopy:
        """Reserve the expected copy before its private native name is available."""
        ahead = self._ahead.get((image.image_id, logical_path))
        if ahead:
            return ahead.pop()
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

    def reserve_ahead(self, image: generated_fragment_store.ImageHeader, *, logical_path: Path, count: int) -> None:
        """Reserve the copies a publication will allocate, so one catalogue phase holds them all."""
        prepared = [self.prepare(image, logical_path=logical_path) for _ in range(count)]
        self._ahead.setdefault((image.image_id, logical_path), []).extend(prepared)

    def release_ahead(self) -> None:
        """Close every reservation made ahead that no native allocation used."""
        while self._ahead:
            _, prepared = self._ahead.popitem()
            while prepared:
                copy = prepared.pop().canonical
                self.store.ack_copy(copy.copy_id, owner=copy.owner, outcome="no_effect")

    def allocate(self, prepared: PreparedCopy, *, relative_path: str,
                 destination: str | None = None, removable: bool = True,
                 publication_id: str | None = None) -> CopyAllocation:
        """Bind the still-open canonical reservation before any native payload bytes."""
        if not isinstance(prepared, PreparedCopy) or prepared._seal is not self._seal:
            raise ValueError("prepared copy belongs to another producer")
        canonical, image = prepared.canonical, prepared.image
        # Native bytes follow this allocation, so the staged reservation must be durable first.
        self.store.barrier()
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

    def erase(self, copy: generated_fragment_store.Copy) -> Literal["retired", "no_effect", "unknown"]:
        """Close one copy of a retired Source through its exact native allocation, never by path or bytes."""
        if copy.owner.owner != source_native_custody.OWNER or copy.origin_ledger != self.ledger_id:
            return "unknown"
        bound = self.native.bound(copy.owner.version)
        if bound is False and copy.outcome == "reserved":
            # A binding precedes any byte, so this ledger never wrote the reserved copy.
            self.store.ack_copy(copy.copy_id, owner=copy.owner, outcome="no_effect")
            return "no_effect"
        if bound is not True:
            return "unknown"
        try:
            allocation = CopyAllocation(copy, self.native.get(copy.owner.version))
            self._check_allocation(allocation)
        except (ValueError, source_native_custody.NativeCustodyUnavailable):
            return "unknown"
        if allocation.native.destination_relative is not None and self.native.retire_installed(allocation.native):
            self.store.ack_copy(copy.copy_id, owner=copy.owner, outcome="retired")
            return "retired"
        observed = self.native.cleanup(copy.owner.version)
        if observed.state != "closed" or observed.outcome not in ("not_written", "removed"):
            return "unknown"
        outcome = "no_effect" if observed.outcome == "not_written" else "retired"
        self.store.ack_copy(copy.copy_id, owner=copy.owner, outcome=outcome)
        return outcome

    def stage_artifact(self, image: generated_fragment_store.ImageHeader, *, logical_path: Path,
                       temporary_parent: Path, check: Callable[[], None] | None = None) -> vault._ArtifactCustody:
        copies: dict[str, CopyAllocation] = {}
        active: CopyAllocation | None = None

        def reserve(name):
            nonlocal active
            if check is not None:
                check()
            active = self.reserve(image, logical_path=logical_path,
                                  relative_path=(temporary_parent / name).relative_to(self.root).as_posix())
            copies[name] = active

        def verified(file, digest):
            assert active is not None
            self.verified(active, file, digest)
            self.present(active)

        def release(name, identity, outcome):
            try:
                self.closed(copies[name], identity, outcome)
            except (sqlite3.Error, OSError, held_fs.HeldFsError, source_native_custody.NativeCustodyUnavailable):
                raise held_fs.CustodyAcknowledgementPending("native stage acknowledgement is pending") from None

        return vault._ArtifactCustody(held_fs.TemporaryCustody(
            reserve, lambda name, file: self.native.bind(copies[name].native, file), release), verified)

    def publication_artifact(self, prepared: PreparedCopy, *, publication_id: str | None = None,
                             retiring: CopyAllocation | None = None,
                             check: Callable[[], None] | None = None) -> NativePublication:
        attempt = NativePublication(prepared)
        if retiring is not None:
            self._check_allocation(retiring)
        temporary: CopyAllocation | None = None
        path = prepared.logical_path

        def reserve(name):
            nonlocal temporary
            if check is not None:
                check()
            relative = (path.parent / name).relative_to(self.root).as_posix()
            attempt.allocation_started = True
            attempt.allocation = self.allocate(prepared, relative_path=relative,
                destination=path.relative_to(self.root).as_posix(), removable=False, publication_id=publication_id)
            current = self.store.image_header(prepared.image.image_id)
            if current.state != "observed" or not isinstance(current.value, generated_fragment_store.ImageHeader):
                raise ValueError("native publication image is unavailable")
            temporary = self.reserve(current.value, logical_path=path, relative_path=relative)
            attempt.temporary = temporary

        def bind(name, file):
            assert temporary is not None and attempt.allocation is not None
            self.native.bind(temporary.native, file)
            self.native.bind(attempt.allocation.native, file)

        def verified(file, digest):
            assert temporary is not None and attempt.allocation is not None
            for copy in (temporary, attempt.allocation):
                self.verified(copy, file, digest)
            self.present(temporary)

        def before_install():
            if check is not None:
                check()
            assert attempt.allocation is not None
            observed = self.native.observe(attempt.allocation.native.binding_id)
            if observed.state != "verified" or observed.matching is not True:
                raise ValueError("native publication differs from its verified allocation")

        def installed(witness):
            attempt.installation = witness
            assert attempt.allocation is not None
            self.installed(attempt.allocation, witness)

        def retired(witness):
            attempt.retirement = witness
            assert retiring is not None
            try:
                self.retired(retiring, witness)
            except ValueError:
                # A changed old version withholds retirement; it cannot refuse the new publication.
                raise held_fs.CustodyAcknowledgementPending("native retirement no longer has exact version evidence") from None
            except (sqlite3.Error, OSError, held_fs.HeldFsError, source_native_custody.NativeCustodyUnavailable):
                raise held_fs.CustodyAcknowledgementPending("native retirement acknowledgement is pending") from None

        def release(name, identity, outcome):
            assert temporary is not None and attempt.allocation is not None
            try:
                self.closed(temporary, identity, outcome)
                if attempt.installation is not None:
                    observed = self.native.observe(attempt.allocation.native.binding_id, destination=True)
                    if observed.state != "installed" or observed.matching is not True:
                        raise held_fs.CustodyAcknowledgementPending("native installation acknowledgement is pending")
                    self.present(attempt.allocation)
                else:
                    self.closed(attempt.allocation, identity, outcome)
            except (sqlite3.Error, OSError, held_fs.HeldFsError, source_native_custody.NativeCustodyUnavailable):
                raise held_fs.CustodyAcknowledgementPending("native publication acknowledgement is pending") from None

        attempt.custody = vault._ArtifactCustody(held_fs.TemporaryCustody(reserve, bind, release), verified,
            before_install, installed, retired if retiring is not None else None)
        return attempt

    def unstarted(self, attempt: NativePublication) -> None:
        """Close a live attempt only when its native reservation was never invoked."""
        if attempt.prepared._seal is not self._seal:
            raise ValueError("publication belongs to another producer")
        if not attempt.allocation_started:
            copy = attempt.prepared.canonical
            self.store.ack_copy(copy.copy_id, owner=copy.owner, outcome="no_effect")
