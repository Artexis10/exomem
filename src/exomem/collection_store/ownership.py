"""Complete canonical collection ownership and whole-result read validation."""

from __future__ import annotations

import copy
import inspect
import json
import sqlite3
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from .. import collection_profiles, held_fs, state_migration, state_paths, vault
from .. import structured_collections as collections
from ..planning_values import PlanningValues
from . import authority, connection, legacy_originals, schema, tokens


def unavailable():
    from ..cli_ops import OpError

    return OpError("COLLECTION_STORE_UNAVAILABLE", "collection ownership is unavailable", "Retry shortly.")


def _manager(root):
    from ..writer_lease import active_manager, get_manager
    from .runtime import _SERVED, _SERVING

    runtime = _SERVING.get(Path(root).resolve())
    return active_manager() or (runtime.manager if runtime is not None else _SERVED.get(Path(root).resolve())) or get_manager()


def relevant(path, *, ancestors=False):
    """Profile placement proves disjoint knowledge without consulting collection SQL."""
    if path is None:
        return True
    key = collections._portable_path_key(str(path).replace("\\", "/").strip("/"))
    roots = tuple(collections._portable_path_key(vault.kb_dirname() + "/" + profile.placement_layer)
                  for profile in collection_profiles.PROFILES.values())
    return any(key == root or key.startswith(root + "/") or ancestors and
               (not key or root.startswith(key + "/")) for root in roots)


def enrollment(root):
    """Trusted host placement and enrollment, never a generated declaration."""
    directory = state_paths.vault_state_dir(root)
    manifest = state_migration._load_manifest(directory, vault_root=root)
    if manifest is not None and state_migration.manifest_status(manifest) != "complete":
        raise unavailable()
    recorded = frozenset() if manifest is None else frozenset(manifest["descriptors"])
    owned = recorded & state_migration._store_owned_ids()
    return (str(Path(root).resolve()), str(directory), tuple(sorted(owned))), owned


@dataclass(frozen=True)
class Proof:
    binding: tuple
    marker: dict | None
    admitted: frozenset[str] = frozenset()

    @property
    def entries(self):
        return () if self.marker is None else self.marker["collections"]

    def selected(self, root, selector):
        return None if self.marker is None else authority.selected_entry(root, self.marker, selector)

    def owner(self, root, path, *, ancestors=False):
        if self.marker is None:
            return None
        path = collections._reference_key(Path(root), path) or path
        if not relevant(path, ancestors=ancestors):
            return None
        owner = authority.owned_entry(root, self.marker, path)
        if owner is None and ancestors:
            key = collections._portable_path_key(path).rstrip("/")
            owner = next((entry for entry in self.entries if any(
                collections._portable_path_key(region).startswith(key + "/") or not key
                for region, _ in authority._owned_regions(entry))), None)
        return owner


def declarations(root, conn, *, generation=1):
    """Validate the complete immutable declaration namespace, also at verified ingestion boundaries."""
    sid = conn.execute("SELECT value FROM store_meta WHERE key=?", (schema.META_STORE_ID,)).fetchone()[0]
    rows = conn.execute(
        "SELECT c.collection_id,c.manifest_path,c.source_path,c.layout,m.manifest_text,m.manifest_hash "
        "FROM collections c LEFT JOIN collection_manifests m ON m.collection_id=c.collection_id "
        "AND m.manifest_version=c.manifest_version ORDER BY c.collection_id"
    ).fetchall()
    if any(text is None or tokens.manifest_hash(text) != digest for _cid, _path, _source, _layout, text, digest in rows):
        raise unavailable()
    # Every call rereads and rechecks the rows, so a re-proof still sees any change; within a
    # request, the parse of an unchanged namespace is reused. Each caller gets its own marker. The
    # vocabulary overlay is read once per request too; `finish` re-proves in a fresh scope.
    key = (str(root), sid, generation, tuple((*row[:4], row[5]) for row in rows))
    marker, declared = state_paths.request_memo(
        "ownership_declarations", key, lambda: _declarations(root, sid, rows, generation))
    return copy.deepcopy(marker), declared


def _declarations(root, sid, rows, generation):
    entries, declarations = [], []
    # Ownership is caller-independent; vocabulary admission can itself require this unfinished proof.
    planning_values = PlanningValues(root, server_side=True)
    for cid, path, source, layout, text, _digest in rows:
        manifest = collections.parse_manifest_bytes(root, path, text.encode(), planning_values=planning_values)
        if (manifest.collection_id, manifest.storage.source, manifest.storage.strategy) != (cid, source, layout):
            raise unavailable()
        entries.append(authority.marker_entry(cid, path, sid, manifest.semantic_profile, source, layout))
        # Row saves advance the manifest's audit head without changing namespace ownership.
        declarations.append((cid, path, source, layout, manifest.semantic_profile))
    if not entries:
        return None, ()
    marker = authority.parse_marker(root, json.dumps({
        "version": 2, "mode": "store", "default_authority": "file", "store_id": sid,
        "authority_epoch": 1, "collection_store_fence": {"capability": "collections-store-v1",
        "generation": generation}, "collections": entries,
    }))
    return marker, tuple(declarations)


def _canonical(root, conn, placement, recorded, *, private=False, restored=None, manager=None):
    meta = legacy_originals.metadata(conn)
    sid, instance = meta[schema.META_STORE_ID], meta[schema.META_INSTANCE_ID]
    intent = authority.pending_create(conn)
    active = False
    fence = None
    required = None
    if (recorded or restored is not None
            or intent is not None and (intent["kind"] == authority.EXPORT and intent.get("verified")
                                      or intent["kind"] == authority.MIGRATE and not private)):
        from ..writer_lease import CollectionStoreFenceState
        from . import admission, snapshot

        manager = manager or _manager(root)
        if manager.client is None:
            raise unavailable()
        if private:
            fence = manager.client.collection_store_fence()
        else:
            # Status replaces the fence read; its coordinator transaction can contend briefly.
            status = manager.client.status()
            required = status.collection_store_head
            if required is not None:
                fence = CollectionStoreFenceState(status.required_collection_store_capability, True,
                                                  required.store_id, status.collection_store_fence_generation)
            else:
                fence = manager.client.collection_store_fence()
                if ((status.required_collection_store_capability is not None
                     and status.required_collection_store_capability != fence.capability)
                        or (status.collection_store_fence_generation is not None
                            and status.collection_store_fence_generation != fence.generation)
                        or not fence.enrolled and status.required_collection_store_capability is not None):
                    raise unavailable()
        active = fence.enrolled and fence.store_id == sid
        if not fence.enrolled:
            from .export import completed_retirement_binding

            completed = completed_retirement_binding(root, manager.client, sid)
            if completed is not None:
                return Proof((placement, sid, instance, "exported", completed), None)
            if not private and intent is not None and intent["kind"] == authority.EXPORT:
                retired = manager.client.retired_collection_store_fence()
                expected = authority.parse_marker(root, intent["expected_marker"])
                if (retired.store_id != sid or retired.generation != fence.generation
                        or expected["store_id"] != sid
                        or expected["collection_store_fence"]["generation"] != fence.generation):
                    raise unavailable()
                required = retired.collection_store_head
        if fence.enrolled and not active:
            raise unavailable()
        if required is not None and not admission.holds_head(
                conn, required, sid, instance, lineage=snapshot.validated_lineage(meta)):
            # Stale recovery declarations must not release a later collection as an ordinary file.
            # Readers retry relevant reads until sufficient recovery state arrives.
            raise unavailable()
    if not active and not private and intent is None and not recorded and restored is None:
        # An incidental dark preview database cannot enroll a file-only vault.
        return Proof((placement, None), None)
    marker, declared = declarations(root, conn, generation=1 if fence is None else max(1, fence.generation))
    if marker is None:
        if active or intent is not None:
            raise unavailable()
        return Proof((placement, None), None)
    entries = marker["collections"]
    admitted = {entry["collection_id"] for entry in entries} if active or private else set()
    transition = None
    if intent is not None:
        authority.require_intent_binding(conn, intent)
        participants = authority.intent_collection_ids(intent)
        if participants - {entry["collection_id"] for entry in entries}:
            raise unavailable()
        if intent["kind"] == authority.EXPORT:
            # Export retires the whole store; structural evidence admits none of its rows.
            admitted.clear()
        expected = authority.parse_marker(root, intent["expected_marker"]) if intent["expected_marker"] else None
        target = expected if intent["kind"] == authority.EXPORT else authority.parse_marker(root, intent["target_marker"])
        if target["store_id"] != sid:
            raise unavailable()
        for entry in target["collections"]:
            authority.require_selected(conn, target, entry, root=root)
        for item in intent["collections"]:
            entry = authority.selected_entry(root, marker, item["collection_id"])
            if entry is None or (entry["manifest_path"], entry["source_path"]) != (item["manifest_path"], item["source_path"]):
                raise unavailable()
        status = authority.intent_status(root, conn, intent)
        if status != "marker_admitted":
            admitted -= participants
        transition = (json.dumps(intent, sort_keys=True, separators=(",", ":")), status)
        if intent["kind"] == authority.MIGRATE and not private and not active:
            # Only an unchanged first pre-cut import leaves files authoritative; a wrong refusal costs a retry.
            if (not recorded and restored is None and expected is None
                    and participants == {entry["collection_id"] for entry in entries}
                    and "base_head" not in intent and "supersedes" not in intent and status == "pending"
                    and fence is not None and not fence.enrolled and required is None
                    and target["collection_store_fence"]["generation"] == fence.generation + 1):
                return Proof((placement, sid, instance, (fence.enrolled, fence.generation), declared, transition), None)
            raise unavailable()
    if restored is not None and not private and not active and not (
            intent is not None and intent["kind"] == authority.EXPORT and required is not None):
        # An unadmitted backup cannot grant file authority; relevant readers must retry adoption.
        raise unavailable()
    return Proof((placement, sid, instance, None if fence is None else (fence.enrolled, fence.generation),
                  declared, transition), marker, frozenset(admitted))


@contextmanager
def read_snapshot(root):
    """Keep canonical data and its production ownership proof in the same read-only snapshot."""
    root = Path(root).resolve()
    with ExitStack() as stack:
        manager = _manager(root)
        stack.enter_context(manager.consistency_guard(
            root, operation="collection_ownership_read", holder_kind="collection-read"))
        try:
            placement, recorded = enrollment(root)
            path = connection.store_path(root)
            placement = (*placement, str(path.resolve()))
            restored = None
            if path.exists():
                reader = stack.enter_context(connection.reader_snapshot(path))
            else:
                from ..vault_portability import _checked_collection_replica_structure
                from . import replica

                verified = stack.enter_context(_checked_collection_replica_structure(
                    root, source_path=replica.replica_path(root)
                ))
                reader = verified.connection
                restored = legacy_originals.metadata(reader)
            proof = _canonical(root, reader, placement, recorded, restored=restored, manager=manager)
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, AttributeError,
                collections.CollectionError, connection.CollectionStoreError, held_fs.HeldFsError, RuntimeError):
            raise unavailable() from None
        yield reader, proof


def resolve(root, *, conn=None, private=False, path=None, manager=None):
    """Read every declaration before any principal filter or candidate limit."""
    root = Path(root).resolve()
    try:
        placement, recorded = enrollment(root)
        path = connection.store_path(root) if path is None else Path(path)
        placement = (*placement, str(path.resolve()))
        if conn is not None:
            return _canonical(root, conn, placement, recorded, private=private, manager=manager)
        if not path.exists():
            from ..vault_portability import _checked_collection_replica_structure
            from . import replica

            if recorded:
                from .export import completed_retirement_binding

                manager = manager or _manager(root)
                if manager.client is None:
                    raise unavailable()
                completed = completed_retirement_binding(root, manager.client)
                if completed is not None:
                    return Proof((placement, "exported", completed), None)
            source = replica.replica_path(root)
            if source.is_file():
                with _checked_collection_replica_structure(root, source_path=source) as verified:
                    reader = verified.connection
                    # Only the existing verifier's private snapshot reaches this restore admission assessment.
                    metadata = legacy_originals.metadata(reader)
                    return _canonical(root, reader, placement, recorded, restored=metadata, manager=manager)
            if recorded or authority.read_marker(root) is not None:
                raise unavailable()
            return Proof((placement, None), None)
        from .runtime import reader_pool

        # Ownership declarations use the unchanged metadata wire; payload reads still require the current schema.
        with connection.ownership_reader(path, reader_pool(root, path)) as reader:
            reader.execute("BEGIN")
            return _canonical(root, reader, placement, recorded, private=private, manager=manager)
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, AttributeError, collections.CollectionError,
            connection.CollectionStoreError, held_fs.HeldFsError, RuntimeError):
        raise unavailable() from None


_READ: ContextVar[ReadScope | None] = ContextVar("collection_ownership_read", default=None)


class ReadScope:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.initial = None
        self.finished = False

    def proof(self):
        if self.finished:
            raise unavailable()
        if self.initial is None:
            from .preview import bound_writer, production_bound

            writer = bound_writer(self.root)
            manager = _manager(self.root)
            with manager.consistency_guard(
                self.root, operation="collection_ownership_read", holder_kind="collection-read"):
                self.initial = resolve(self.root, conn=None if writer is None else writer.connection,
                    private=writer is not None and not production_bound(self.root),
                    path=writer.handle.path if writer is not None and not production_bound(self.root) else None)
        return self.initial

    def finish(self):
        if self.finished:
            return
        self.finished = True
        if self.initial is None:
            return
        from .preview import bound_writer, production_bound

        manager = _manager(self.root)
        try:
            # A lend starts with no transaction, and fresh placement bypasses the body's resolution memo,
            # so neither this proof nor the body's WAL snapshot is reused.
            with state_paths.outside_resolution_scope(), state_paths.resolution_scope(), \
                    manager.consistency_guard(self.root, operation="collection_ownership_release"):
                writer = bound_writer(self.root)
                private = writer is not None and not production_bound(self.root)
                current = resolve(self.root, private=private, path=writer.handle.path if private else None)
            if current.binding == self.initial.binding:
                return
        except Exception:  # noqa: BLE001 - an unresolved final binding cannot release the earlier payload
            pass
        from ..governance import egress

        collector = egress._collector()
        if collector is not None:
            # No successful-release outcome may describe the rejected whole payload.
            # These are the disclosure receipt protocol's two successful release decisions.
            released = ("released", "release_authorized")
            collector.outcomes[:] = [outcome for outcome in collector.outcomes
                                     if outcome.value.get("decision") not in released]
            collector.path_outcomes = {outcome for outcome in collector.path_outcomes if outcome[1] not in released}
        raise unavailable()


@contextmanager
def read_scope(root, *, finalize=False):
    previous = _READ.get()
    if previous is not None and previous.root == state_paths.resolved_vault_path(root, expanduser=False):
        yield previous
        return
    scope = ReadScope(root)
    token = _READ.set(scope)
    try:
        yield scope
    finally:
        try:
            if finalize:
                scope.finish()
        finally:
            _READ.reset(token)


def current(root, *, conn=None, private=False):
    scope = _READ.get()
    if scope is not None and scope.root == state_paths.resolved_vault_path(root, expanduser=False):
        return scope.proof()
    if conn is None and not private:
        return ReadScope(root).proof()
    with _manager(root).consistency_guard(
            root, operation="collection_ownership_read", holder_kind="collection-read"):
        return resolve(root, conn=conn, private=private)


def owned(root, path, *, ancestors=False):
    if not relevant(path, ancestors=ancestors):
        return None
    return current(root).owner(root, path, ancestors=ancestors)


def file_only(root):
    """The global empty-policy shortcut requires a complete negative, recorded by the enclosing read."""
    try:
        scope = _READ.get()
        if (scope is not None and scope.root == state_paths.resolved_vault_path(root, expanduser=False)
                and scope.initial is not None):
            return scope.initial.marker is None
        _, recorded = enrollment(root)
        if "collections-store-v1" in recorded:
            return False
        return current(root).marker is None
    except Exception:  # noqa: BLE001 - uncertainty retains the ordinary per-path decision
        return False


def finish_read(root):
    scope = _READ.get()
    if scope is not None and scope.root == Path(root).resolve():
        scope.finish()


def reader(function=None, *, vault_root=None):
    """Own a whole-result scope only when the caller has no enclosing consumer scope."""
    if function is None:
        return lambda function: reader(function, vault_root=vault_root)
    first = next(iter(inspect.signature(function).parameters), None)

    @wraps(function)
    def read(*args, **kwargs):
        if vault_root is None and not args and first not in kwargs:
            return function(*args, **kwargs)
        root = vault_root if vault_root is not None else args[0] if args else kwargs[first]
        if root is None:
            return function(*args, **kwargs)
        previous = _READ.get()
        if previous is not None and previous.root == state_paths.resolved_vault_path(root, expanduser=False):
            return function(*args, **kwargs)
        with read_scope(root) as scope:
            try:
                result = function(*args, **kwargs)
            except BaseException:
                scope.finish()
                raise
            scope.finish()
            return result
    return read
