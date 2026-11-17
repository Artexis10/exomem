"""Source-owned hydration reservations and exact native copy settlement."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing, nullcontext
from pathlib import Path
from typing import TypeVar

from . import (
    generated_fragment_store,
    held_fs,
    memory_refs,
    native_copy_custody,
    originals,
    source_custody,
    source_native_custody,
)
from .cli_ops import OpError

_T = TypeVar("_T")


def _metadata(root: Path, work: Callable[[], _T]) -> _T:
    """Use brief actual boundaries; a download never retains a metadata checkout."""
    from .collection_store import preview
    from .vault_store import media as store_media
    from .writer_lease import active_manager

    def guarded():
        manager = active_manager()
        boundary = manager._mutation_coordinator_for(root)
        guard = (nullcontext() if boundary.current_thread_holds_boundary()
                 and not boundary.current_thread_borrows_boundary() else
                 manager.mutation_guard(root, operation="source_original_custody", holder_kind="background"))
        with guard:
            phase = preview.source_metadata_phase(root)
            bound = preview.bound_writer(root)
            if phase is None and bound is not None and bound.connection.in_transaction:
                # A rollback must not erase canonical Copy custody after plaintext survives in a native file.
                raise OpError("SOURCE_CUSTODY_BOUNDARY_REQUIRED", "Source original custody requires an idle collection handle")
            with phase.source_metadata() if phase is not None else nullcontext():
                return work()

    if store_media.off_store_thread(root):
        return store_media.on_store_thread(root, guarded, operation="source_original_custody")
    return guarded()


class HydrationCopy:
    """Keep one expected original copy and its temporary allocation under their existing owners."""

    def __init__(self, root: Path, snapshot, ledger, token: str, reference: str, manifest):
        self.root, self.snapshot, self.reference = root, snapshot, reference
        self.store = generated_fragment_store.GeneratedFragmentStore(root)
        self.native = source_native_custody.NativeCustody(root)
        self.copies = native_copy_custody.NativeCopyCustody(root, store=self.store, native=self.native)
        self.publication = None
        self._mark(ledger, token, None)
        try:
            head = self.store.initialize()
            source_custody.register_owner(root, reference, source_custody.OwnerLocator(
                owner=generated_fragment_store.OWNER, locator=head.catalogue_id, version=str(head.version)))
            declared = manifest.originals
            if declared is not None and any(
                    item.sha256 == snapshot.binary_sha256 and item.size == snapshot.binary_size for item in declared):
                image = self.store.begin_whole_image(whole_source_ref=reference,
                    image_sha256=snapshot.binary_sha256, image_size=snapshot.binary_size)
            else:
                # Legacy or contradictory declarations cannot prove whole-Source ownership or independence.
                image = self.store.begin_opaque_image(source_refs=(), image_sha256=snapshot.binary_sha256,
                    image_size=snapshot.binary_size, unknown_through_epoch=head.epoch)
            self.publication = self.copies.publication_artifact(
                self.copies.prepare(image, logical_path=snapshot.path), check=self._check)
        except BaseException:
            self.store.close()
            raise

    @staticmethod
    def _mark(ledger, token, binding):
        with closing(ledger._connect()) as conn, conn:
            conn.execute("UPDATE media_access SET source_custody_json=? WHERE token=?",
                         (json.dumps({"version": 1, "binding_id": binding}, separators=(",", ":")), token))

    def _check(self):
        source_custody.require_publishable(self.root, self.reference)
        if originals.original_state(self.root, self.snapshot.path) != self.snapshot:
            raise OpError("SOURCE_CUSTODY_TARGET_CHANGED", "the original binding changed during acquisition")

    def reserve(self, name, ledger, token):
        def work():
            self.publication.custody.temporary.reserve(name)
            self._mark(ledger, token, self.publication.temporary.native.binding_id)
        _metadata(self.root, work)

    def bind(self, name, file):
        _metadata(self.root, lambda: self.publication.custody.temporary.bind(name, file))

    def install(self, file, digest, work):
        def guarded():
            self._check()
            self.publication.custody.verified(file, digest)
            return work(self.publication.custody)
        return _metadata(self.root, guarded)

    def finish(self, name) -> bool:
        """Close only witnessed temporary effects; a failed cleanup retains both obligations."""
        def work():
            publication = self.publication
            if publication is None:
                return False
            temporary = publication.temporary
            if temporary is None:
                self.copies.unstarted(publication)
                return not publication.allocation_started
            if publication.installation is not None:
                publication.custody.temporary.release(name, self.native.get(temporary.native.binding_id).identity,
                                                       "installed")
                return True
            observation = self.native.cleanup(temporary.native.binding_id)
            if observation.state != "closed" or observation.outcome not in ("removed", "not_written"):
                return False
            identity = self.native.get(temporary.native.binding_id).identity
            publication.custody.temporary.release(name, identity, observation.outcome)
            return True
        try:
            return _metadata(self.root, work)
        except (ValueError, OpError, sqlite3.Error, OSError, held_fs.HeldFsError,
                source_native_custody.NativeCustodyUnavailable, held_fs.CustodyAcknowledgementPending):
            return False
        finally:
            self.store.close()


def prepare(root: Path, snapshot, ledger, token) -> HydrationCopy | None:
    """Use enrolled Source custody; ordinary unregistered originals retain their existing path."""
    if not source_custody.available():
        return None

    def selection():
        binding = originals.bound_companion(root, snapshot.path.relative_to(root).as_posix())
        if binding is None or hashlib.sha256(binding[2]).hexdigest() != snapshot.sidecar_hash:
            raise OpError("SOURCE_CUSTODY_TARGET_CHANGED", "the original companion changed")
        reference = memory_refs.ref_from_markdown(binding[2].decode("utf-8"))
        if reference is None:
            return None
        manifest, _guard = source_custody.read_manifest(root, reference)
        if manifest is None:
            return None
        source_custody.require_publishable(root, reference)
        return reference, manifest

    selected = selection()
    if selected is None:
        return None

    def work():
        current = selection()
        if current is None or (current[0], current[1].allocated_hash) != (selected[0], selected[1].allocated_hash):
            raise OpError("SOURCE_CUSTODY_TARGET_CHANGED", "Source enrollment changed before original acquisition")
        return HydrationCopy(root, snapshot, ledger, token, *current)

    return _metadata(root, work)
