"""Published canonical capture metadata and bounded, governed inventory reads."""
from __future__ import annotations

import base64
import binascii
import json
import threading
from bisect import bisect_left, bisect_right, insort
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from . import memory_refs, note_types, source_capture, vault
from .kbdir import managed_join

MAX_PAGE_SIZE = 200


@dataclass(frozen=True, slots=True)
class CaptureEntry:
    key: tuple[str, str, str, str, str]
    source_ref: str
    source_hash: str
    capture_digest: str
    chunk_digest: str | None
    chunk_count: int | None
    # Passage search reads this cut for the same bytes instead of cutting the Source again.
    chunk_cut: bytes | None = None

    @property
    def path(self) -> str:
        return self.key[4]


@dataclass(frozen=True, slots=True)
class CaptureInput:
    page: source_capture.CapturePageInput
    entry: CaptureEntry | None
    invalid_identity: bool = False
    signature: tuple[int, ...] | None = None


def raw_path(root: Path, path: str) -> bool:
    # The two raw lanes and Markdown extension are fixed canonical storage contracts.
    return path.lower().endswith(".md") and any(path.startswith(managed_join(f"{lane}/")) for lane in vault.APPEND_ONLY_KB_SUBPATHS)


def input_from_bytes(root: Path, path: str, data: bytes) -> CaptureInput:
    from . import embedding_index, find_corpus, passage_evidence

    page = source_capture.capture_page_input(root, path, data)
    fields = page.fields
    if "source_identity" not in fields:
        return CaptureInput(page, None, "capture_input_digest" in fields)
    try:
        identity = source_capture.validate_identity(fields["source_identity"])
        digest = source_capture.validate_sha256(fields.get("capture_input_digest"))
        reference = memory_refs.ref_from_markdown(data.decode("utf-8"))
        if reference is None or not note_types.Basis(root).selects(fields.get("type"), note_types.raw):
            raise ValueError("canonical identity lacks a Source witness")
        chunk_digest = None
        count = None
        cut = None
        if "declared_passages" in fields:
            parsed = find_corpus.parse_page(root / path, 0, root, content=data, resolved_relative=path)
            chunks = passage_evidence.declared_chunks(parsed) if parsed is not None else None
            if not chunks:
                raise ValueError("invalid passage declaration")
            chunk_digest = embedding_index.ordered_chunk_digest(chunk.text for chunk in chunks)
            count = len(chunks)
            cut = passage_evidence.packed_cut(chunks)
        return CaptureInput(page, CaptureEntry((identity["system"], identity["record_id"], identity["revision"], reference, path), reference,
                                               page.sha256, digest, chunk_digest, count, cut))
    except (ValueError, TypeError, UnicodeError):
        return CaptureInput(page, None, True)


def _conflicting_paths(keys) -> frozenset[str]:
    return frozenset(path for before, after in zip(keys, keys[1:], strict=False) if before[:3] == after[:3]
                     for path in (before[4], after[4]))


@dataclass(frozen=True, slots=True)
class CaptureInventory:
    inputs: Mapping[str, CaptureInput]
    neighbors: Mapping[str, frozenset[str]]
    canonical: frozenset[str]
    incomplete: frozenset[str]
    buckets: Mapping[tuple[str, ...], tuple[tuple[str, str, str, str, str], ...]]
    entries: Mapping[str, CaptureEntry]
    store_identity: tuple[str, str, int] | None = None
    raw_stamp: tuple[str, int] | None = None
    type_dependency: tuple[str, str] | None = None
    identity_conflicts: frozenset[str] = frozenset()

    @classmethod
    def build(cls, root: Path, inputs: dict[str, CaptureInput], *, store_identity=None) -> CaptureInventory:
        neighbors: dict[str, set[str]] = {}
        for path, item in inputs.items():
            neighbors.setdefault(path, set())
            for target, *_binding in item.page.bindings:
                neighbors[path].add(target)
                neighbors.setdefault(target, set()).add(path)
        canonical, incomplete = source_capture.classify_capture_inputs({p: i.page for p, i in inputs.items()})
        incomplete |= frozenset(p for p in canonical if inputs[p].invalid_identity)
        entries = {p: inputs[p].entry for p in canonical if inputs[p].entry is not None}
        buckets: dict[tuple[str, ...], list] = {}
        for item in entries.values():
            for scope in (item.key[:1], item.key[:2]):
                buckets.setdefault(scope, []).append(item.key)
        conflicts = frozenset(path for scope, keys in buckets.items() if len(scope) == 2 for path in _conflicting_paths(sorted(keys)))
        return cls(MappingProxyType(inputs), MappingProxyType({p: frozenset(n) for p, n in neighbors.items()}), canonical, incomplete,
                   MappingProxyType({s: tuple(sorted(keys)) for s, keys in buckets.items()}), MappingProxyType(entries), store_identity, None, note_types.Basis(root).loaded_dependency(), conflicts)

    def with_delta(self, changes: Mapping[str, CaptureInput | None]) -> CaptureInventory:
        """Reclassify connected bindings, including unchanged originals of removed owners."""
        if not changes:
            return self
        inputs = dict(self.inputs)
        neighbors = dict(self.neighbors)
        affected = set(changes)
        pending = list(changes)
        while pending:
            path = pending.pop()
            for neighbor in self.neighbors.get(path, ()):
                if neighbor not in affected:
                    affected.add(neighbor)
                    pending.append(neighbor)
        for path, item in changes.items():
            old = inputs.pop(path, None)
            if old is not None:
                for target, *_binding in old.page.bindings:
                    neighbors[target] = neighbors.get(target, frozenset()) - {path}
            if old is not None:
                for target, *_binding in old.page.bindings:
                    reverse = inputs.get(target)
                    if reverse is None or not any(binding[0] == path for binding in reverse.page.bindings):
                        neighbors[path] = neighbors.get(path, frozenset()) - {target}
                    else:
                        neighbors[target] = neighbors.get(target, frozenset()) | {path}
            if item is not None:
                inputs[path] = item
                for target, *_binding in item.page.bindings:
                    neighbors[path] = neighbors.get(path, frozenset()) | {target}
                    neighbors[target] = neighbors.get(target, frozenset()) | {path}
        pending = list(affected)
        while pending:
            for neighbor in neighbors.get(pending.pop(), ()):
                if neighbor not in affected:
                    affected.add(neighbor)
                    pending.append(neighbor)
        component = {p: inputs[p].page for p in affected if p in inputs}
        canonical, incomplete = source_capture.classify_capture_inputs(component)
        incomplete |= frozenset(p for p in canonical if inputs[p].invalid_identity)
        canonical = (self.canonical - affected) | canonical
        incomplete = (self.incomplete - affected) | incomplete
        entries = dict(self.entries)
        buckets = dict(self.buckets)
        touched: dict[tuple[str, ...], list] = {}
        for path in affected:
            old = entries.pop(path, None)
            new = inputs[path].entry if path in canonical and path in inputs else None
            for item, adding in ((old, False), (new, True)):
                if item is None:
                    continue
                for scope in (item.key[:1], item.key[:2]):
                    if scope not in touched:
                        touched[scope] = list(buckets.get(scope, ()))
                    keys = touched[scope]
                    if adding:
                        insort(keys, item.key)
                    else:
                        keys.pop(bisect_left(keys, item.key))
            if new is not None:
                entries[path] = new
        conflicts = self.identity_conflicts
        for scope, keys in touched.items():
            if len(scope) == 2:
                conflicts = (conflicts - _conflicting_paths(buckets.get(scope, ()))) | _conflicting_paths(keys)
        buckets.update((scope, tuple(keys)) for scope, keys in touched.items())
        return CaptureInventory(MappingProxyType(inputs), MappingProxyType(neighbors), frozenset(canonical), frozenset(incomplete),
                                MappingProxyType(buckets), MappingProxyType(entries), self.store_identity, self.raw_stamp, self.type_dependency, conflicts)


def collect(root: Path, inputs: dict[str, CaptureInput]) -> CaptureInventory:
    """Complete the cold publisher's raw inputs, including semantically excluded pages."""
    from . import find_corpus
    from .collection_store.views import store_identity
    from .vault_store import ownership, read

    identity = None
    if find_corpus.store_serves(root):
        with read.snapshot(root) as view:
            observed = store_identity(view.connection)
            identity = (observed["store_id"], observed["instance_id"], view.revision)
            for lane in vault.APPEND_ONLY_KB_SUBPATHS:
                prefix = managed_join(f"{lane}/")
                paths = [meta.path for meta in view.pages(prefix) if raw_path(root, meta.path) and meta.path not in inputs]
                for start in range(0, len(paths), read.KEYS_PER_QUERY):
                    for path, data in view.bodies(paths[start:start + read.KEYS_PER_QUERY]).items():
                        if ownership.classify(path) is not ownership.Ownership.PAGE:
                            raise ValueError("raw inventory page lacks canonical store ownership")
                        if path not in inputs:
                            inputs[path] = input_from_bytes(root, path, data)
    else:
        for path, info in source_capture.raw_markdown_files(root):
            inputs[path] = read_input(root, path, info)
    return CaptureInventory.build(root, inputs, store_identity=identity)


def read_canonical(root: Path, path: str) -> bytes | None:
    """One selected proof worker read through the active canonical storage mode."""
    from . import find_corpus, runtime_resources
    from .vault_store import read

    if find_corpus.store_serves(root):
        with read.snapshot(root) as view:
            meta = view.page_meta(path)
            if meta is None or meta.size > runtime_resources.SEMANTIC_SOURCE_MAX_BYTES:
                return None
            page = view.page(path)
            return page.body if page is not None else None
    try:
        size = (root / path).lstat().st_size
        if size > runtime_resources.SEMANTIC_SOURCE_MAX_BYTES:
            return None
        return vault.read_bounded_guarded_bytes(root, path, limit=size)[0]
    except FileNotFoundError:
        return None


def _decode_cursor(cursor: str | None, scope: tuple[str, ...]) -> tuple[str, str, str, str, str] | None:
    if cursor is None:
        return None
    try:
        # The cursor's fields and five-part key are a fixed wire format.
        decoded = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
        if (not isinstance(decoded, dict) or decoded.get("scope") != list(scope)
                or set(decoded) != {"scope", "after"} or not isinstance(decoded["after"], list)
                or len(decoded["after"]) != 5 or not all(isinstance(p, str) for p in decoded["after"])
                or tuple(decoded["after"][:len(scope)]) != scope):
            raise ValueError
        return tuple(decoded["after"])
    except (ValueError, UnicodeError, binascii.Error):
        raise ValueError("INVALID_CURSOR: cursor must come from this inventory scope") from None


def inventory(root: Path, *, system: str, record_id: str | None = None, cursor: str | None = None, limit: int = MAX_PAGE_SIZE) -> dict:
    from . import embeddings, semantic_contract, semantic_drain
    from .cli_ops import OpError

    try:
        source_capture.require_authority(root)
        if not isinstance(system, str) or not system:
            raise ValueError("INVALID_SYSTEM: system must be a nonempty string")
        if record_id is not None and (not isinstance(record_id, str) or not record_id):
            raise ValueError("INVALID_RECORD_ID: record_id must be a nonempty string")
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"INVALID_LIMIT: limit must be from 1 to {MAX_PAGE_SIZE}")
        scope = (system,) if record_id is None else (system, record_id)
        after = _decode_cursor(cursor, scope)
    except ValueError as error:
        code, _, message = str(error).partition(": ")
        raise OpError(code, message or code) from None
    publication = semantic_contract.borrow_capture_inventory(root)
    if publication is None:
        return {"summary": "Captured Source inventory is pending publication proof.", "state": "pending", "complete": False,
                "generation": None, "items": [], "next_cursor": None}
    view, generation = publication
    bucket = view.buckets.get(scope, ())
    start = bisect_right(bucket, after) if after is not None else 0
    keys = bucket[start:start + limit]
    selected = [view.entries[key[4]] for key in keys]
    index = embeddings.get_embedding_index(root)
    observations = index.chunk_inventory(
        {item.path: (item.chunk_digest, item.chunk_count) for item in selected if item.chunk_digest is not None},
        source_hashes={item.path: item.source_hash for item in selected},
    )
    from .embedding_index import RecallSpaceUnread, read_published_identity

    active_space = next((value[2] for value in observations.values() if value[2] is not None), None)
    if not observations:
        try:
            identity = read_published_identity(root)
        except RecallSpaceUnread:
            identity = None
        active_space = identity.identity_digest if identity is not None else None
    items = []
    for item in selected:
        state, count, space = observations.get(item.path, ("not_applicable", None, active_space))
        if state == "unknown" and space is not None:
            semantic_drain.request_inventory_proof(root, item, space)
        conflict = item.path in view.identity_conflicts
        # Field names and index states belong to the fixed inventory response schema.
        items.append({"source_identity": dict(zip(("system", "record_id", "revision"), item.key[:3], strict=True)),
                      "source_ref": item.source_ref, "capture_input_digest": item.capture_digest, "source_hash": item.source_hash,
                      "identity_conflict": conflict, "index_state": state, "expected_chunks": item.chunk_count,
                      "indexed_chunks": count, "recall_space_id": space,
                      "failure": {"code": "index_publication_failed", "source_hash": item.source_hash, "revision": item.key[2]} if state == "failed" else None})
    more = start + len(keys) < len(bucket)
    next_cursor = base64.urlsafe_b64encode(json.dumps({"scope": scope, "after": keys[-1]}, separators=(",", ":")).encode()).decode() if more else None
    complete = not view.incomplete and not view.identity_conflicts
    return {"summary": f"Observed {len(items)} captured Sources.", "state": "observed" if complete else "partial", "complete": complete,
            "classification_state": "complete" if not view.incomplete else "incomplete",
            "identity_conflict_count": len(view.identity_conflicts),
            "generation": generation, "items": items, "next_cursor": next_cursor}


@dataclass(eq=False)
class RawWitness:
    """One watcher's raw-input epoch; no second signature map or consumer history."""

    instance: str = field(default_factory=lambda: uuid4().hex)
    epoch: int = 0
    live: bool = False
    seeded: bool = False
    pending: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def activate(self) -> None:
        with self.lock:
            self.live = True

    def observe(self, path: str) -> None:
        with self.lock:
            self.epoch += 1
            self.pending[path] = self.epoch

    def withdraw(self, *, stopped: bool = False) -> None:
        with self.lock:
            self.epoch += 1
            self.seeded = False
            if stopped:
                self.live = False

    def snapshot(self):
        with self.lock:
            return self.epoch, dict(self.pending), self.seeded

    def acknowledge(self, epoch: int, covered: set[str], *, seeded: bool) -> None:
        with self.lock:
            for path in covered:
                if self.pending.get(path, epoch + 1) <= epoch:
                    self.pending.pop(path, None)
            if seeded and self.live:
                self.seeded = True

    def current(self, stamp: tuple[str, int] | None) -> bool:
        with self.lock:
            return self.live and self.seeded and not self.pending and stamp == (self.instance, self.epoch)


_RAW_LOCK = threading.Lock()
_RAW_WITNESSES: dict[str, RawWitness] = {}


def new_raw_witness(root: Path) -> RawWitness:
    witness = RawWitness()
    with _RAW_LOCK:
        previous = _RAW_WITNESSES.get(str(root.resolve()))
        if previous is not None:
            previous.withdraw(stopped=True)
        _RAW_WITNESSES[str(root.resolve())] = witness
    return witness


def raw_witness(root: Path) -> RawWitness | None:
    with _RAW_LOCK:
        return _RAW_WITNESSES.get(str(root.resolve()))


def raw_event_path(root: Path, path: Path, *, directory: bool = False) -> str | None:
    try:
        relative = path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError:
        return None
    if raw_path(root, relative):
        return relative
    if directory:
        for lane in vault.APPEND_ONLY_KB_SUBPATHS:
            prefix = managed_join(lane)
            if relative == "." or prefix == relative or prefix.startswith(relative + "/") or relative.startswith(prefix + "/"):
                return "" if relative == "." else relative
    return None


def file_signature(info) -> tuple[int, ...]:
    """A regular original's filesystem identity and change signature."""
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size


def read_input(root: Path, path: str, info) -> CaptureInput:
    """Read one raw input measured by `info`, measuring again when a commit replaced it meanwhile."""
    for attempt in range(vault.MOVED_READ_ATTEMPTS):
        last = attempt + 1 == vault.MOVED_READ_ATTEMPTS
        before = file_signature(info)
        try:
            data, guard = vault.read_bounded_guarded_bytes(root, path, limit=info.st_size)
            after = file_signature((root / path).lstat())
            guard.recheck(root)
        except vault.PathGuardError as error:
            if last or not vault.moved_during_read(error):
                raise
        else:
            if before == after:
                return replace(input_from_bytes(root, path, data), signature=after)
            if last:
                raise ValueError("canonical raw input changed during publication")
        info = (root / path).lstat()
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


def refreshed(root: Path, view: CaptureInventory, pending: Mapping[str, int], *, census: bool, excluded: frozenset[str]):
    """Strict off-request signature census; hydrate only new, changed or delivered inputs."""
    roots = tuple(path for path in pending if path not in excluded)
    selected_roots = frozenset(roots)
    def selected_path(path):
        parts = path.split("/")
        return "" in selected_roots or any("/".join(parts[:depth]) in selected_roots for depth in range(1, len(parts) + 1))
    if not roots and not census:
        return view, set()
    signatures = dict(source_capture.raw_markdown_files(root, subtrees=() if census else roots))
    selected = set(signatures)
    if census:
        selected.update(view.inputs)
    else:
        selected.update(path for path in view.inputs if selected_path(path))
    changes = {}
    for path in selected - excluded:
        info = signatures.get(path)
        before = view.inputs.get(path)
        forced = selected_path(path)
        if info is None:
            changes[path] = None
        elif before is None or before.signature != file_signature(info) or forced:
            changes[path] = read_input(root, path, info)
    return view.with_delta(changes), set(pending) - excluded
