"""Bind renderer maps to canonical slots and exact native before-images."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from . import generated_fragment_store as generated
from . import generated_fragments, held_fs, native_copy_custody, source_native_custody, vault

_INPUT_SEAL = object()
_PLAN_SEAL = object()


@dataclass(frozen=True)
class GeneratedInput:
    root: Path
    path: Path
    catalogue_id: str
    checkpoint: int
    physical_scope: str
    guard: vault.PathGuard
    binding: generated.SlotBinding
    mapping: generated_fragments.FragmentPlan
    image_size: int
    current: native_copy_custody.CopyAllocation | None = None
    native_version: held_fs.FileVersion | None = None
    _seal: object = field(repr=False, compare=False, default=None)


@dataclass(frozen=True)
class GeneratedImagePlan:
    """A producer's exact renderer output, bound to its guarded canonical inputs."""

    before: GeneratedInput
    inputs: tuple[GeneratedInput, ...]
    after: generated_fragments.FragmentPlan
    _seal: object = field(repr=False, compare=False, default=None)

    @property
    def fingerprint(self) -> str:
        if self._seal is not _PLAN_SEAL:
            raise ValueError("generated plan is not producer-bound")
        # This private fingerprint fixes its fields; every identity and map comes from bound producer data.
        inputs = [
            {
                "catalogue_id": item.catalogue_id, "physical_scope": item.physical_scope,
                "target": item.guard.target, "checkpoint": item.checkpoint,
                "slot_revision": item.binding.slot.revision if item.binding.slot is not None else None,
                "current_copy_id": item.binding.current.copy_id if item.binding.current is not None else None,
                "candidate_copy_id": item.binding.candidate.copy_id if item.binding.candidate is not None else None,
                "before": item.mapping.model_dump(mode="json"),
            }
            for item in self.inputs
        ]
        return hashlib.sha256(json.dumps({"inputs": inputs, "after": self.after.model_dump(mode="json")},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()

    def validate(self, root: Path, write: vault.PlannedWrite) -> None:
        if (self._seal is not _PLAN_SEAL or self.before._seal is not _INPUT_SEAL
                or self.before.root != Path(root).absolute() or self.before.path != Path(write.path).absolute()
                or self.before.guard != write.guard or not isinstance(write.content, str)):
            raise ValueError("generated plan differs from its guarded write")
        self.after.after.validate_image(write.content.encode("utf-8"))


@dataclass(frozen=True)
class BoundText:
    """Transient rendered bytes retain the canonical inputs of copied excerpts."""

    image: generated_fragments._Image
    inputs: tuple[GeneratedInput, ...]


def _mapping(store: generated.GeneratedFragmentStore, header: generated.ImageHeader,
             data: bytes) -> generated_fragments.FragmentMap:
    if header.whole_source_ref is not None or header.opaque_source_refs is not None or header.image_size != len(data):
        raise ValueError("generated before-image is not a matching fragment image")
    fragments, previous = [], None
    for number in range(header.page_count):
        observation = store.image_page(header.image_id, number)
        if observation.state != "observed" or not isinstance(observation.value, generated.ImagePage):
            raise ValueError("generated before-image page is unavailable")
        page = observation.value
        if page.previous_page_hash != previous:
            raise ValueError("generated before-image page chain differs")
        previous = page.digest()
        fragments.extend(page.fragments)
    if previous != header.last_page_hash:
        raise ValueError("generated before-image page chain is incomplete")
    mapping = generated_fragments.FragmentMap(image_sha256=header.image_sha256, fragments=tuple(fragments))
    mapping.validate_image(data)
    return mapping


def read_before(store: generated.GeneratedFragmentStore, native: source_native_custody.NativeCustody,
                *, path: Path, text: str, guard: vault.PathGuard) -> GeneratedInput:
    """Read one physical slot directly; missing ownership becomes finite input uncertainty."""
    root, path = store.root, Path(path).absolute()
    relative = path.relative_to(root).as_posix()
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    if guard.target != relative or (guard.expects_absence and data) or (
            not guard.expects_absence and (not guard.binds_bytes or guard.expected_content_hash != digest)):
        raise ValueError("generated before-image differs from its byte guard")
    guard.recheck(root)
    head = store.head()
    scope = native.physical_scope
    ledger = native.ledger_id
    locator = generated.file_locator(head.catalogue_id, relative)
    binding = store.read_slot(owner=source_native_custody.OWNER, locator=locator, physical_scope=scope)
    horizon = max(head.epoch, binding.unknown_through_epoch or 0)
    mapping, selected, version = None, None, None
    unknown = ("fragment_native_binding_unavailable",)
    if guard.expects_absence and binding.state == "missing":
        mapping = generated_fragments.FragmentMap(image_sha256=digest)
        horizon, unknown = binding.unknown_through_epoch, ()
    elif binding.state in ("observed", "pending"):
        matches = []
        for copy in (binding.current, binding.candidate):
            if copy is None or copy.closed:
                continue
            try:
                observed = native.observe(copy.owner.version, destination=True)
            except ValueError:
                # An opaque locator from damaged custody does not establish a native binding.
                continue
            allocation = observed.reservation
            if (observed.state == "installed" and observed.matching is True and allocation is not None
                    and allocation.generated_copy_id == copy.copy_id and allocation.image_id == copy.image_id
                    and allocation.destination_relative == relative and copy.origin_ledger == ledger):
                matches.append((copy, allocation, observed.version))
        if len(matches) == 1:
            copy, allocation, version = matches[0]
            header = next((image for image in binding.images if image.image_id == copy.image_id), None)
            try:
                if header is None:
                    raise ValueError("generated before-image header is unavailable")
                mapping = _mapping(store, header, data)
                selected = native_copy_custody.CopyAllocation(copy, allocation)
                if binding.state == "pending":
                    # A complete native witness retains positive fragments; pending metadata still bounds unknown ownership.
                    unknown = ("fragment_catalogue_pending",)
                else:
                    horizon, unknown = binding.unknown_through_epoch, ()
                confirmed = native.observe(allocation.binding_id, destination=True)
                if confirmed.matching is not True or confirmed.version != version:
                    raise ValueError("generated native before-image changed while reading its map")
            except (OSError, ValueError, sqlite3.Error, held_fs.HeldFsError):
                mapping, selected, version = None, None, None
                horizon, unknown = max(head.epoch, binding.unknown_through_epoch or 0), ("fragment_native_binding_unavailable",)
    if store.head() != head:
        raise ValueError("generated catalogue changed while binding renderer input")
    guard.recheck(root)
    if mapping is None:
        mapping = generated_fragments.FragmentMap(image_sha256=digest)
    plan = generated_fragments.FragmentPlan(before_sha256=digest, after=mapping,
        coverage_complete=not unknown, unknown=unknown, unknown_through_epoch=horizon)
    return GeneratedInput(root, path, head.catalogue_id, head.epoch, scope, guard, binding, plan, len(data),
                          selected, version, _INPUT_SEAL)


def seal(before: GeneratedInput, *, content: str, rendered: generated_fragments.FragmentPlan,
         inputs: Iterable[GeneratedInput] = ()) -> GeneratedImagePlan:
    """Seal final renderer output after all count, trim and newline composition."""
    bound = (before, *inputs)
    # Inputs are immutable producer capabilities; callers cannot replace missing provenance with a hash.
    for item in bound:
        if (not isinstance(item, GeneratedInput) or item._seal is not _INPUT_SEAL
                or item.root != before.root or item.catalogue_id != before.catalogue_id
                or item.physical_scope != before.physical_scope):
            raise ValueError("generated renderer input belongs to another owner")
    if rendered.before_sha256 != before.mapping.before_sha256:
        raise ValueError("generated renderer before-image differs")
    rendered.after.validate_image(content.encode("utf-8"))
    horizons = (rendered.unknown_through_epoch, *(item.mapping.unknown_through_epoch for item in bound))
    checkpoint = max(item.checkpoint for item in bound)
    horizon = max(checkpoint if value is None else value for value in horizons)
    return GeneratedImagePlan(before, bound, rendered.model_copy(update={"unknown_through_epoch": horizon}), _PLAN_SEAL)


def already_installed(root: Path, write: vault.PlannedWrite) -> bool:
    """Qualify replay through the native current map, never through equal content hashes alone."""
    plan = write.generated_image
    if not isinstance(plan, GeneratedImagePlan):
        return False
    plan.validate(root, write)
    store = generated.GeneratedFragmentStore(root)
    try:
        text, guard = vault.read_guarded_text(root, write.path)
        native = source_native_custody.NativeCustody(root)
        observed = read_before(store, native, path=write.path, text=text, guard=guard)
        return (observed.current is not None and observed.catalogue_id == plan.before.catalogue_id
                and observed.physical_scope == plan.before.physical_scope and observed.mapping.after == plan.after.after
                and observed.mapping.unknown_through_epoch is not None and plan.after.unknown_through_epoch is not None
                and observed.mapping.unknown_through_epoch >= plan.after.unknown_through_epoch)
    except (OSError, ValueError, sqlite3.Error, held_fs.HeldFsError, source_native_custody.NativeCustodyUnavailable):
        return False
    finally:
        store.close()


class GeneratedProducer:
    """Bind exact renderer inputs once and attach their final ownership plans."""

    def __init__(self, store: generated.GeneratedFragmentStore, native: source_native_custody.NativeCustody):
        self.store, self.native = store, native
        self.inputs: dict[Path, GeneratedInput] = {}

    def before(self, path: Path) -> GeneratedInput:
        path = Path(path).absolute()
        if path not in self.inputs:
            try:
                text, guard = vault.read_guarded_text(self.store.root, path)
            except FileNotFoundError:
                text = ""
                guard = vault.PathGuard.capture(self.store.root, path.relative_to(self.store.root).as_posix(), leaf_policy="absent")
            self.inputs[path] = read_before(self.store, self.native, path=path, text=text, guard=guard)
        return self.inputs[path]

    def before_maps(self, paths: Iterable[Path]) -> dict[str, generated_fragments.FragmentPlan]:
        return {path.relative_to(self.store.root).as_posix(): self.before(path).mapping for path in paths}

    def include(self, inputs: Iterable[GeneratedInput]) -> None:
        catalogue_id, physical_scope = self.store.head().catalogue_id, self.native.physical_scope
        for item in inputs:
            if (not isinstance(item, GeneratedInput) or item._seal is not _INPUT_SEAL
                    or item.root != self.store.root or item.catalogue_id != catalogue_id
                    or item.physical_scope != physical_scope):
                raise ValueError("generated text input belongs to another producer scope")
            existing = self.inputs.get(item.path)
            if existing is not None and existing != item:
                raise ValueError("generated text inputs bind different versions of one path")
            self.inputs[item.path] = item

    def attach(self, write: vault.PlannedWrite, rendered: generated_fragments.FragmentPlan) -> vault.PlannedWrite:
        before = self.before(write.path)
        write.generated_image = seal(before, content=write.content, rendered=rendered,
            inputs=(item for item in self.inputs.values() if item is not before))
        write.generated_image.validate(self.store.root, write)
        return write


@contextmanager
def producer(root: Path, *, initialize: bool = False, preview: bool = False) -> Iterator[GeneratedProducer | None]:
    """Reuse existing file custody independently of current Source enrollment."""
    from .vault_store import availability

    if preview or availability.page_writer(root) is not None:
        yield None
        return
    store = generated.GeneratedFragmentStore(root)
    try:
        if not initialize and not store.path.exists():
            yield None
            return
        from .writer_lease import active_manager

        manager = active_manager()
        boundary = manager._mutation_coordinator_for(root)
        # Planning may precede the commit hold. Read under the actual boundary without advancing its write generation.
        hold = nullcontext() if boundary.current_thread_holds_boundary() else (
            manager.mutation_guard(root, operation="generated_inputs") if initialize
            else manager.consistency_guard(root, operation="generated_inputs", holder_kind="producer", may_borrow=False)
        )
        with hold:
            if initialize:
                store.initialize()
            else:
                store.head()
            yield GeneratedProducer(store, source_native_custody.NativeCustody(root))
    finally:
        store.close()
