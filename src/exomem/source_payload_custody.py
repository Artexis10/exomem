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
        self.destinations: dict[Path, native_copy_custody.NativePublication] = {}
        self.publications: list[native_copy_custody.NativePublication] = []
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

    def _check(self) -> None:
        source_custody.require_publishable(self.root, self.reference)

    def stage(self, write: vault.PlannedWrite, workspace: vault._BatchWorkspace,
              restoring: bool) -> vault._ArtifactCustody | None:
        payload = self.payloads.get(write.path)
        if payload is None:
            return None
        if restoring:
            raise ValueError("a fresh Source allocation has no before-image to restore")
        return self.copies.stage_artifact(payload.image, logical_path=payload.path,
                                         temporary_parent=workspace.path, check=self._check)

    def publication(self, write: vault.PlannedWrite, artifact: vault._WorkspaceArtifact,
                    restoring: bool) -> vault._ArtifactCustody | None:
        payload = self.payloads.get(write.path)
        if payload is None:
            return None
        if restoring:
            raise ValueError("a fresh Source allocation cannot publish an inverse image")
        self._check()
        publication = self.copies.publication_artifact(
            self.copies.prepare(payload.image, logical_path=payload.path), check=self._check)
        self.destinations[write.path] = publication
        self.publications.append(publication)
        return publication.custody

    def removed(self, write: vault.PlannedWrite, identity: vault.PathIdentity) -> None:
        publication = self.destinations.get(write.path)
        if publication is None or publication.allocation is None:
            return
        copy = publication.allocation
        reservation = self.native.get(copy.native.binding_id)
        native_identity = reservation.identity
        if (native_identity is None or native_identity.device != identity.device
                or native_identity.inode != identity.inode):
            raise ValueError("Source inverse removal has a different native allocation")
        self.copies.closed(copy, native_identity, "removed")

    def finished(self) -> None:
        try:
            for publication in self.publications:
                self.copies.unstarted(publication)
        except (ValueError, sqlite3.Error, OSError, held_fs.HeldFsError, source_native_custody.NativeCustodyUnavailable):
            raise held_fs.CustodyAcknowledgementPending("Source publication acknowledgement is pending") from None


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
        yield vault._BatchPayloadCustody(owner.stage, owner.publication, owner.removed, owner.finished)
    finally:
        owner.close()
