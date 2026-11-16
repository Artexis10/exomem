"""Publish sealed renderer images through the common native batch and canonical Slots."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from . import generated_fragment_store as generated
from . import (
    generated_output_plan,
    held_fs,
    native_copy_custody,
    source_custody,
    source_native_custody,
    vault,
)


@dataclass
class _Attempt:
    right: generated.SlotPublication
    publisher: native_copy_custody.NativePublication


@dataclass
class _Output:
    write: vault.PlannedWrite
    after: generated.ImageHeader
    before: generated.ImageHeader | None
    attempts: list[_Attempt] = field(default_factory=list)
    ready: _Attempt | None = None


class _GeneratedCopies:
    def __init__(self, root: Path, writes: Sequence[vault.PlannedWrite]):
        self.root = Path(root).absolute()
        self.store = generated.GeneratedFragmentStore(self.root)
        self.native = source_native_custody.NativeCustody(self.root)
        self.copies = native_copy_custody.NativeCopyCustody(self.root, store=self.store, native=self.native)
        self.outputs: dict[Path, _Output] = {}
        self.settlement_attempted = False
        self.publishable: set[str] = set()
        try:
            self.store.recover()
            head = self.store.head()
            for write in writes:
                plan = write.generated_image
                if not isinstance(plan, generated_output_plan.GeneratedImagePlan):
                    raise ValueError("generated output requires a sealed producer plan")
                plan.validate(self.root, write)
                for item in plan.inputs:
                    item.guard.recheck(self.root)
                    if item.catalogue_id != head.catalogue_id or item.physical_scope != self.copies.physical_scope:
                        raise ValueError("generated input belongs to another catalogue or native scope")
                    observed = self._binding(item.path)
                    if observed.state not in ("missing", "observed") or observed.slot != item.binding.slot:
                        raise ValueError("generated input Slot changed after rendering")
                    if item.current is not None:
                        native = self.native.observe(item.current.native.binding_id, destination=True)
                        if native.matching is not True or native.version != item.native_version:
                            raise ValueError("generated input allocation changed after rendering")
            for write in writes:
                plan = write.generated_image
                binding = self._resolve(write.path)
                horizon = max(plan.after.unknown_through_epoch, binding.unknown_through_epoch or 0)
                build = self.store.begin_image(write.content.encode("utf-8"), plan.after,
                                               unknown_through_epoch=horizon)
                for page in build.pages:
                    self.store.append_image(page)
                after = self.store.seal_image(build.header.image_id)
                before = None
                if not plan.before.guard.expects_absence:
                    if plan.before.current is not None:
                        before = self._image(plan.before.current.canonical.image_id)
                    else:
                        image_id = uuid.uuid4().hex
                        pages = tuple(generated.pack_pages(image_id, plan.before.mapping.after.fragments))
                        before = self.store.begin_verified_image(image_id=image_id,
                            image_sha256=plan.before.mapping.after.image_sha256, image_size=plan.before.image_size,
                            page_count=len(pages), expected_last_page_hash=pages[-1].digest() if pages else None,
                            unknown_through_epoch=max(plan.before.mapping.unknown_through_epoch, horizon))
                        for page in pages:
                            self.store.append_image(page)
                        before = self.store.seal_image(before.image_id)
                output = _Output(write, after, before)
                self.outputs[write.path] = output
                # All first publication rights precede the batch's first surviving byte stage.
                output.ready = self._prepare(output, restoring=False)
                # The workspace stage and the publication temporary each allocate one more copy.
                self.copies.reserve_ahead(self._image(after.image_id), logical_path=write.path, count=2)
        except BaseException:
            try:
                try:
                    self.finished()
                except held_fs.CustodyAcknowledgementPending:
                    pass
            finally:
                self.store.close()
            raise

    def _image(self, image_id: str) -> generated.ImageHeader:
        result = self.store.image_header(image_id)
        if result.state != "observed" or not isinstance(result.value, generated.ImageHeader):
            raise ValueError("generated image is unavailable")
        return result.value

    def _binding(self, path: Path) -> generated.SlotBinding:
        return self.store.read_slot(owner=source_native_custody.OWNER,
            locator=generated.file_locator(self.store.head().catalogue_id, path.relative_to(self.root).as_posix()),
            physical_scope=self.copies.physical_scope)

    def _allocation(self, copy: generated.Copy | None, path: Path) -> native_copy_custody.CopyAllocation | None:
        if copy is None or copy.closed or copy.origin_ledger != self.copies.ledger_id:
            return None
        try:
            observed = self.native.observe(copy.owner.version, destination=True)
        except ValueError:
            return None
        reservation = observed.reservation
        if (observed.state == "installed" and observed.matching is True and reservation is not None
                and reservation.generated_copy_id == copy.copy_id and reservation.image_id == copy.image_id
                and reservation.destination_relative == path.relative_to(self.root).as_posix()):
            return native_copy_custody.CopyAllocation(copy, reservation)
        return None

    def _witness(self, path: Path, binding: generated.SlotBinding) -> generated.NativeSlotWitness:
        matches = [selection for selection, copy in (("before", binding.current), ("candidate", binding.candidate))
                   if self._allocation(copy, path) is not None]
        selection = matches[0] if len(matches) == 1 else "mismatch"
        if not matches:
            try:
                vault.PathGuard.capture(self.root, path.relative_to(self.root).as_posix(), leaf_policy="absent").recheck(self.root)
            except (OSError, vault.PathGuardError):
                pass
            else:
                selection = "absence"
        return generated.NativeSlotWitness(binding.slot.slot_id, binding.slot.revision, self.copies.physical_scope,
            binding.slot.pending.publication_id if binding.slot.pending is not None else None, selection)

    def _resolve(self, path: Path) -> generated.SlotBinding:
        binding = self._binding(path)
        if binding.state not in ("missing", "observed"):
            raise ValueError("generated Slot custody is unavailable")
        if binding.publication is not None:
            self.store.settle_slot(binding.publication, witness=lambda current: self._witness(path, current))
            binding = self._binding(path)
        return binding

    def _check_sources(self, output: _Output) -> None:
        # The sealed map supplies identities; deduplication avoids a manifest read per fragment.
        # Retirement needs the writer boundary this batch holds, so a Source checked once stays publishable here.
        for reference in dict.fromkeys(fragment.source_ref for fragment in output.write.generated_image.after.after.fragments):
            if reference not in self.publishable:
                source_custody.require_publishable(self.root, reference)
                self.publishable.add(reference)

    def _prepare(self, output: _Output, *, restoring: bool) -> _Attempt:
        binding = self._resolve(output.write.path)
        image = output.before if restoring else output.after
        if image is None:
            raise ValueError("generated inverse has no before-image")
        if not restoring:
            self._check_sources(output)
        prepared = self.copies.prepare(self._image(image.image_id), logical_path=output.write.path)
        publication_id = uuid.uuid4().hex
        if binding.publication is not None:
            right = self.store.takeover_slot(binding.publication, candidate=prepared.canonical, publication_id=publication_id)
        else:
            right = self.store.prepare_slot(owner=prepared.canonical.owner.owner, locator=prepared.canonical.owner.locator,
                physical_scope=self.copies.physical_scope, expected_revision=binding.slot.revision if binding.slot else None,
                expected_current_copy_id=binding.current.copy_id if binding.current else None,
                candidate=prepared.canonical, publication_id=publication_id)

        def check():
            self.store.check_slot(right)
            if not restoring:
                self._check_sources(output)

        publisher = self.copies.publication_artifact(prepared, publication_id=publication_id,
            retiring=self._allocation(binding.current, output.write.path), check=check)
        attempt = _Attempt(right, publisher)
        output.attempts.append(attempt)
        return attempt

    def stage(self, write: vault.PlannedWrite, workspace: vault._BatchWorkspace, restoring: bool) -> vault._ArtifactCustody | None:
        output = self.outputs.get(write.path)
        if output is None:
            return None
        if restoring:
            # Settle the installed forward image before allocating a new restore Copy and right.
            output.ready = self._prepare(output, restoring=True)
        image = output.before if restoring else output.after
        assert image is not None
        return self.copies.stage_artifact(self._image(image.image_id), logical_path=write.path,
            temporary_parent=workspace.path, check=None if restoring else lambda: self._check_sources(output))

    def publication(self, write: vault.PlannedWrite, artifact: vault._WorkspaceArtifact, restoring: bool) -> vault._ArtifactCustody | None:
        output = self.outputs.get(write.path)
        if output is None:
            return None
        attempt = output.ready or self._prepare(output, restoring=restoring)
        output.ready = None
        return attempt.publisher.custody

    def removed(self, write: vault.PlannedWrite, identity: vault.PathIdentity) -> None:
        output = self.outputs.get(write.path)
        if output is None:
            return
        installed = next((attempt.publisher for attempt in reversed(output.attempts)
                          if attempt.publisher.installation is not None), None)
        if installed is None or installed.allocation is None:
            raise ValueError("generated inverse removal has no installed allocation")
        allocation = installed.allocation
        native_identity = self.native.get(allocation.native.binding_id).identity
        if native_identity is None or (native_identity.device, native_identity.inode) != (identity.device, identity.inode):
            raise ValueError("generated inverse removal has a different native allocation")
        self.copies.closed(allocation, native_identity, "removed")

    def finished(self) -> None:
        self.settlement_attempted = True
        try:
            self.copies.release_ahead()
            for output in self.outputs.values():
                for attempt in output.attempts:
                    self.copies.unstarted(attempt.publisher)
                self._resolve(output.write.path)
        except (ValueError, sqlite3.Error, OSError, held_fs.HeldFsError, source_native_custody.NativeCustodyUnavailable):
            raise held_fs.CustodyAcknowledgementPending("generated Slot settlement is pending") from None


def batch(root: Path | None, writes: Sequence[vault.PlannedWrite],
          previous: vault._BatchPayloadCustody | None, *, lifetime: ExitStack) -> vault._BatchPayloadCustody | None:
    planned = [write for write in writes if write.generated_image is not None]
    if not planned:
        return previous
    if root is None:
        raise ValueError("generated output custody requires its vault root")
    owner = _GeneratedCopies(root, planned)

    def finish_unsettled():
        if not owner.settlement_attempted:
            try:
                owner.finished()
            except held_fs.CustodyAcknowledgementPending:
                pass

    # Callbacks preserve the caller's exception, including immutable domain errors.
    lifetime.callback(owner.store.close)
    lifetime.callback(finish_unsettled)
    own = vault._BatchPayloadCustody(owner.stage, owner.publication, owner.removed, owner.finished)
    if previous is None:
        return own

    def artifact(hook, *args):
        first, second = getattr(previous, hook)(*args), getattr(own, hook)(*args)
        if first is not None and second is not None:
            raise ValueError("native artifact has two custody owners")
        return first or second

    def removed(*args):
        previous.removed(*args)
        own.removed(*args)

    def finished():
        try:
            if previous.finished is not None:
                previous.finished()
        finally:
            own.finished()

    return vault._BatchPayloadCustody(lambda *args: artifact("stage", *args),
        lambda *args: artifact("publication", *args), removed, finished)
