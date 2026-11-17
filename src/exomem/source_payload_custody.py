"""Bind fresh Source payload copies to canonical obligations and exact native allocations."""

from __future__ import annotations

import logging
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

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Payload:
    image: generated_fragment_store.ImageHeader
    path: Path


def _bind_declarations(root: Path, reference: str, primary: vault.PlannedWrite,
                       writes: Sequence[vault.PlannedWrite], auxiliaries) -> dict[Path, source_custody.OriginalBinding]:
    if not isinstance(primary.content, str) or memory_refs.ref_from_markdown(primary.content) != reference:
        raise OpError("SOURCE_CUSTODY_TARGET_CHANGED", "Source payload differs from its allocation")
    source_hash = vault.content_hash(primary.content)
    originals = vocabulary_auxiliaries.declared_originals(root, auxiliaries)
    sealed = auxiliaries if isinstance(auxiliaries, tuple) else () if auxiliaries is None else (auxiliaries,)
    primary_relative = primary.path.relative_to(root).as_posix()
    if not sealed or any(item.primary_path != primary_relative or item.primary_after_sha256 != source_hash for item in sealed):
        raise ValueError("Source primary differs from its sealed declaration")
    by_path = {write.path.relative_to(root).as_posix(): write for write in writes}
    if len(by_path) != len(writes) or primary_relative not in by_path or by_path[primary_relative].content != primary.content:
        raise ValueError("Source payload plan differs from its sealed primary")
    payloads = {primary.path: source_custody.OriginalBinding(sha256=source_hash, size=len(primary.content.encode("utf-8")))}
    declarations = []
    for relative, (digest, owner_path) in originals.items():
        write = by_path.get(relative)
        if (owner_path != primary_relative or write is None or not isinstance(write.content, vault.PreparedBinaryContent)
                or write.content.sha256 != digest):
            raise ValueError("Source original differs from its sealed declaration")
        binding = source_custody.OriginalBinding(sha256=digest, size=write.content.size)
        declarations.append(binding)
        payloads[write.path] = binding
    source_custody.bind_originals(root, reference=reference, source_hash=source_hash, originals=tuple(declarations))
    return payloads


class _CaptureCopies:
    def __init__(self, root: Path, reference: str, payloads: dict[Path, source_custody.OriginalBinding],
                 writes: Sequence[vault.PlannedWrite]):
        self.root, self.reference = root, reference
        self.native = source_native_custody.NativeCustody(root)
        self.store = generated_fragment_store.GeneratedFragmentStore(root)
        self.copies = native_copy_custody.NativeCopyCustody(root, store=self.store, native=self.native)
        self.payloads: dict[Path, _Payload] = {}
        self.destinations: dict[Path, native_copy_custody.NativePublication] = {}
        self.publications: list[native_copy_custody.NativePublication] = []
        try:
            head = self.store.initialize()
            from .source_result_custody import active, record_target

            if active(root):
                # Payload, result and native hooks all record this capture, so its census starts complete here.
                source_custody.issue_birth(root, reference, self.store.allocate_birth)
            # This locator names the catalogue protocol, rather than each retained physical copy.
            source_custody.register_owner(root, reference, source_custody.OwnerLocator(
                owner=generated_fragment_store.OWNER, locator=head.catalogue_id, version=str(head.version),
            ))
            for write in writes:
                binding = payloads.get(write.path)
                if binding is None:
                    continue
                record_target(reference, write.path.relative_to(root).as_posix())
                if not write.create_only or write.expected_hash != vault.MISSING_CONTENT_HASH:
                    raise ValueError("fresh Source payload custody requires a missing destination")
                image = self.store.begin_whole_image(
                    whole_source_ref=reference, image_sha256=binding.sha256, image_size=binding.size,
                )
                # The workspace stage, the publication and its temporary each allocate one copy.
                self.copies.reserve_ahead(image, logical_path=write.path, count=3)
                self.payloads[write.path] = _Payload(image, write.path)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            # A batch that never reached settlement still closes the copies it reserved ahead.
            self.copies.release_ahead()
        except (ValueError, sqlite3.Error, OSError, held_fs.HeldFsError, source_native_custody.NativeCustodyUnavailable) as error:
            log.warning("Source payload reservations remain outstanding (%s)", type(error).__name__)
        finally:
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
            self.copies.release_ahead()
            for publication in self.publications:
                self.copies.unstarted(publication)
        except (ValueError, sqlite3.Error, OSError, held_fs.HeldFsError, source_native_custody.NativeCustodyUnavailable):
            raise held_fs.CustodyAcknowledgementPending("Source publication acknowledgement is pending") from None


@contextmanager
def capture(root: Path, *, reference: str, primary: vault.PlannedWrite,
            writes: Sequence[vault.PlannedWrite], auxiliaries, enabled: bool) -> Iterator[vault._BatchPayloadCustody | None]:
    """Bind sealed declarations in every mode, then retain fresh file-mode copies."""
    from .vault_store import availability

    if not enabled:
        yield None
        return
    payloads = _bind_declarations(root, reference, primary, writes, auxiliaries)
    if availability.page_writer(root) is not None:
        yield None
        return
    with generated_fragment_store.unit(root):
        owner = _CaptureCopies(root, reference, payloads, writes)
        try:
            yield vault._BatchPayloadCustody(owner.stage, owner.publication, owner.removed, owner.finished)
        finally:
            owner.close()
