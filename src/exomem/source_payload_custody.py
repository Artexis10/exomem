"""Bind fresh Source payload copies to canonical obligations and exact native allocations."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import (
    generated_fragment_store,
    held_fs,
    memory_refs,
    source_custody,
    source_native_custody,
    vault,
    vocabulary_auxiliaries,
)
from .cli_ops import OpError


@dataclass(frozen=True)
class _Payload:
    image: generated_fragment_store.ImageHeader
    path: Path


@dataclass(frozen=True)
class _Copy:
    canonical: generated_fragment_store.Copy
    native: source_native_custody.NativeReservation


class _CaptureCopies:
    def __init__(self, root: Path, reference: str, primary: vault.PlannedWrite,
                 writes: Sequence[vault.PlannedWrite], auxiliaries):
        self.root, self.reference = root, reference
        source_custody.require_publishable(root, reference)
        manifest, _ = source_custody.read_manifest(root, reference)
        if (manifest is None or not isinstance(primary.content, str)
                or manifest.allocated_hash != vault.content_hash(primary.content)
                or memory_refs.ref_from_markdown(primary.content) != reference):
            raise OpError("SOURCE_CUSTODY_TARGET_CHANGED", "Source payload differs from its allocation")
        self.native = source_native_custody.NativeCustody(root)
        self.ledger_id, self.physical_scope = self.native.ledger_id, self.native.physical_scope
        self.store = generated_fragment_store.GeneratedFragmentStore(root)
        self.payloads: dict[Path, _Payload] = {}
        self.destinations: dict[Path, _Copy] = {}
        try:
            head = self.store.initialize()
            # This locator names the catalogue protocol, rather than each retained physical copy.
            source_custody.register_owner(root, reference, source_custody.OwnerLocator(
                owner=generated_fragment_store.OWNER, locator=head.catalogue_id, version=str(head.version),
            ))
            originals = vocabulary_auxiliaries.declared_originals(root, auxiliaries)
            primary_relative = primary.path.relative_to(root).as_posix()
            for write in writes:
                relative = write.path.relative_to(root).as_posix()
                if write.path == primary.path:
                    digest, size = manifest.allocated_hash, len(primary.content.encode("utf-8"))
                elif relative in originals:
                    digest, owner_path = originals[relative]
                    if (owner_path != primary_relative or not isinstance(write.content, vault.PreparedBinaryContent)
                            or write.content.sha256 != digest):
                        raise ValueError("Source original differs from its sealed declaration")
                    size = write.content.size
                else:
                    continue
                if not write.create_only or write.expected_hash != vault.MISSING_CONTENT_HASH:
                    raise ValueError("fresh Source payload custody requires a missing destination")
                image = self.store.begin_whole_image(
                    whole_source_ref=reference, image_sha256=digest, image_size=size,
                )
                self.payloads[write.path] = _Payload(image, write.path)
        except BaseException:
            self.store.close()
            raise

    def close(self) -> None:
        self.store.close()

    def _reserve(self, payload: _Payload, relative: str, *, destination: str | None = None,
                 removable: bool = True) -> _Copy:
        source_custody.require_publishable(self.root, self.reference)
        binding_id = uuid.uuid4().hex
        owner = source_custody.OwnerLocator(
            owner=source_native_custody.OWNER, locator=binding_id, version=self.physical_scope,
        )
        canonical = self.store.reserve_copy(
            payload.image.image_id, owner=owner, origin_ledger=self.ledger_id,
            expected_header=payload.image, expected_root=self.root,
        )
        # A failed private reservation leaves its canonical Copy unresolved; local absence cannot close it.
        native = self.native.reserve(
            relative_path=relative, expected_sha256=payload.image.image_sha256,
            expected_size=payload.image.image_size, source_ref=self.reference,
            image_id=payload.image.image_id, generated_copy_id=canonical.copy_id,
            destination_relative=destination, removable=removable, binding_id=binding_id,
        )
        return _Copy(canonical, native)

    def _verified(self, copy: _Copy, file: held_fs.HeldFile, digest: str) -> None:
        self.native.verified(copy.native, file, sha256=digest)

    def _present(self, copy: _Copy) -> None:
        self.store.ack_copy(copy.canonical.copy_id, owner=copy.canonical.owner, outcome="present")

    def _closed(self, copy: _Copy, identity: held_fs.StableIdentity | None, outcome: str) -> None:
        reservation = self.native.get(copy.native.binding_id)
        if reservation.identity is None and outcome != "installed":
            # Every binding precedes bytes; an unbound reservation cannot have published its payload.
            identity, outcome = None, "not_written"
        self.native.acknowledge(reservation, identity, outcome)
        self.store.ack_copy(
            copy.canonical.copy_id, owner=copy.canonical.owner,
            outcome="no_effect" if outcome == "not_written" else "retired",
        )

    def stage(self, write: vault.PlannedWrite, workspace: vault._BatchWorkspace,
              restoring: bool) -> vault._ArtifactCustody | None:
        payload = self.payloads.get(write.path)
        if payload is None:
            return None
        if restoring:
            raise ValueError("a fresh Source allocation has no before-image to restore")
        copies: dict[str, _Copy] = {}
        active_name: str | None = None

        def reserve(name):
            nonlocal active_name
            relative = (workspace.path / name).relative_to(self.root).as_posix()
            copies[name] = self._reserve(payload, relative)
            active_name = name

        def bind(name, file):
            self.native.bind(copies[name].native, file)

        def verified(file, digest):
            assert active_name is not None
            copy = copies[active_name]
            self._verified(copy, file, digest)
            self._present(copy)

        def release(name, identity, outcome):
            try:
                self._closed(copies[name], identity, outcome)
            except (sqlite3.Error, OSError, held_fs.HeldFsError, source_native_custody.NativeCustodyUnavailable):
                raise held_fs.CustodyAcknowledgementPending("Source stage acknowledgement is pending") from None

        return vault._ArtifactCustody(held_fs.TemporaryCustody(reserve, bind, release), verified)

    def publication(self, write: vault.PlannedWrite, artifact: vault._WorkspaceArtifact,
                    restoring: bool) -> vault._ArtifactCustody | None:
        payload = self.payloads.get(write.path)
        if payload is None:
            return None
        if restoring:
            raise ValueError("a fresh Source allocation cannot publish an inverse image")
        copies: dict[str, tuple[_Copy, _Copy]] = {}
        active_name: str | None = None

        def reserve(name):
            nonlocal active_name
            relative = (write.path.parent / name).relative_to(self.root).as_posix()
            destination = write.path.relative_to(self.root).as_posix()
            temporary = self._reserve(payload, relative)
            candidate = self._reserve(payload, relative, destination=destination, removable=False)
            copies[name], active_name = (temporary, candidate), name
            self.destinations[write.path] = candidate

        def bind(name, file):
            for copy in copies[name]:
                self.native.bind(copy.native, file)

        def verified(file, digest):
            assert active_name is not None
            for copy in copies[active_name]:
                self._verified(copy, file, digest)
            self._present(copies[active_name][0])

        def before_install():
            source_custody.require_publishable(self.root, self.reference)
            assert active_name is not None
            observed = self.native.observe(copies[active_name][1].native.binding_id)
            if observed.state != "verified" or observed.matching is not True:
                raise ValueError("Source publication no longer matches its verified native allocation")

        def installed(witness):
            assert active_name is not None
            self.native.record_installation(copies[active_name][1].native, witness)

        def release(name, identity, outcome):
            temporary, candidate = copies[name]
            try:
                self._closed(temporary, identity, outcome)
                if outcome == "installed":
                    observed = self.native.observe(candidate.native.binding_id, destination=True)
                    if observed.state != "installed" or observed.matching is not True:
                        raise held_fs.CustodyAcknowledgementPending("Source installation evidence is pending")
                    # Installation keeps destination bytes present; only its temporary-name obligation closed.
                    self._present(candidate)
                else:
                    # Native removal of the exclusive single-link temporary leaves no destination alias.
                    self._closed(candidate, identity, outcome)
            except (sqlite3.Error, OSError, held_fs.HeldFsError, source_native_custody.NativeCustodyUnavailable):
                raise held_fs.CustodyAcknowledgementPending("Source publication acknowledgement is pending") from None

        return vault._ArtifactCustody(held_fs.TemporaryCustody(reserve, bind, release), verified, before_install, installed)

    def removed(self, write: vault.PlannedWrite, identity: vault.PathIdentity) -> None:
        copy = self.destinations.get(write.path)
        if copy is None:
            return
        reservation = self.native.get(copy.native.binding_id)
        native_identity = reservation.identity
        if (native_identity is None or native_identity.device != identity.device
                or native_identity.inode != identity.inode):
            raise ValueError("Source inverse removal has a different native allocation")
        self._closed(copy, native_identity, "removed")


@contextmanager
def capture(root: Path, *, reference: str, primary: vault.PlannedWrite,
            writes: Sequence[vault.PlannedWrite], auxiliaries, enabled: bool) -> Iterator[vault._BatchPayloadCustody | None]:
    """Bind fresh file-mode Source copies; other persistence owners retain their own integration."""
    from .vault_store import availability

    if not enabled or availability.page_writer(root) is not None:
        yield None
        return
    owner = _CaptureCopies(root, reference, primary, writes, auxiliaries)
    try:
        yield vault._BatchPayloadCustody(owner.stage, owner.publication, owner.removed)
    finally:
        owner.close()
