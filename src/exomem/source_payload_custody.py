"""Bind fresh Source payload copies to canonical obligations and exact native allocations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import (
    generated_fragment_store,
    held_fs,
    memory_refs,
    native_copy_custody,
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
        self.store = generated_fragment_store.GeneratedFragmentStore(root)
        self.copies = native_copy_custody.NativeCopyCustody(root, store=self.store, native=self.native)
        self.payloads: dict[Path, _Payload] = {}
        self.destinations: dict[Path, native_copy_custody.CopyAllocation] = {}
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
                 removable: bool = True) -> native_copy_custody.CopyAllocation:
        source_custody.require_publishable(self.root, self.reference)
        return self.copies.reserve(
            payload.image, logical_path=payload.path, relative_path=relative,
            destination=destination, removable=removable,
        )

    def stage(self, write: vault.PlannedWrite, workspace: vault._BatchWorkspace,
              restoring: bool) -> vault._ArtifactCustody | None:
        payload = self.payloads.get(write.path)
        if payload is None:
            return None
        if restoring:
            raise ValueError("a fresh Source allocation has no before-image to restore")
        copies: dict[str, native_copy_custody.CopyAllocation] = {}
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
            self.copies.verified(copy, file, digest)
            self.copies.present(copy)

        def release(name, identity, outcome):
            try:
                self.copies.closed(copies[name], identity, outcome)
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
        copies: dict[str, tuple[native_copy_custody.CopyAllocation, native_copy_custody.CopyAllocation]] = {}
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
                self.copies.verified(copy, file, digest)
            self.copies.present(copies[active_name][0])

        def before_install():
            source_custody.require_publishable(self.root, self.reference)
            assert active_name is not None
            observed = self.native.observe(copies[active_name][1].native.binding_id)
            if observed.state != "verified" or observed.matching is not True:
                raise ValueError("Source publication no longer matches its verified native allocation")

        def installed(witness):
            assert active_name is not None
            self.copies.installed(copies[active_name][1], witness)

        def release(name, identity, outcome):
            temporary, candidate = copies[name]
            try:
                self.copies.closed(temporary, identity, outcome)
                if outcome == "installed":
                    observed = self.native.observe(candidate.native.binding_id, destination=True)
                    if observed.state != "installed" or observed.matching is not True:
                        raise held_fs.CustodyAcknowledgementPending("Source installation evidence is pending")
                    # Installation keeps destination bytes present; only its temporary-name obligation closed.
                    self.copies.present(candidate)
                else:
                    # Native removal of the exclusive single-link temporary leaves no destination alias.
                    self.copies.closed(candidate, identity, outcome)
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
        self.copies.closed(copy, native_identity, "removed")


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
