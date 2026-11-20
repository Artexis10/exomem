"""One record operation's detached Source admission and exact producer evidence."""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps

from .. import record_source_custody, records, source_custody, writer_lease
from . import (
    bulk_inputs,
    connection,
    edit_back,
    held_sources,
    history,
    record_inputs,
    version_sources,
    views,
)

_REQUEST_SERVER = ContextVar("source_record_request_server", default=None)
_ACQUISITION_PHASE = ContextVar("source_record_acquisition_phase", default=None)


@contextmanager
def request_execution(server):
    """Keep request ownership off the store thread while its bounded units visit the writer."""
    token = _REQUEST_SERVER.set(server)
    try:
        yield
    finally:
        _REQUEST_SERVER.reset(token)


def request_server():
    return _REQUEST_SERVER.get()


@dataclass
class BulkRequest:
    collection: str
    arguments: dict
    inputs: bulk_inputs.PreparedBulk


@dataclass
class RecordRequest:
    inputs: record_inputs.PreparedRecord
    plan: DependencyPlan


@dataclass
class DependencyRows:
    identity: dict
    collection_id: str
    references: list
    current_references: list
    explicit: tuple
    source_arguments: tuple
    sources_prepared: bool
    projections: list


def preparing_acquisition():
    return _ACQUISITION_PHASE.get() == "prepare"


def dispatch_request(server, profile, action, values):
    """Visit the selected facade on the store thread, retaining no checkout during acquisition."""
    from . import preview

    def dispatch(phase):
        token = _ACQUISITION_PHASE.set(phase)
        try:
            return store_unit(server, lambda: preview.dispatch(server.root, profile, action, values),
                              wake_maintenance=phase != "prepare")
        finally:
            _ACQUISITION_PHASE.reset(token)

    handled, prepared = dispatch("prepare")
    if not handled or not isinstance(prepared, (BulkRequest, RecordRequest)):
        return handled, prepared
    with ExitStack() as lifetime:
        if isinstance(prepared, BulkRequest):
            bulk_inputs.acquire(server.root, prepared.inputs, why=prepared.arguments["why"],
                                on_reject=prepared.arguments["on_reject"], lifetime=lifetime)
        else:
            record_inputs.acquire(server.root, prepared.inputs, lifetime=lifetime)
        return dispatch(prepared)


@dataclass(frozen=True)
class StoreUnitOutcome:
    value: object = None
    error: BaseException | None = None
    committed: bool = False


def store_unit(server, work, *, wake_maintenance=True):
    """Carry durable commit observation back across the store call's copied execution context."""
    outcome = server.call_store_unit(work, wake_maintenance=wake_maintenance)
    if outcome.committed:
        writer_lease.mark_active_mutation_committed()
    if outcome.error is not None:
        raise outcome.error
    return outcome.value


def needs_acquisition_request(command, arguments):
    """Choose execution placement from the closed origin protocol, never infer what prose means."""
    from collections.abc import Mapping

    from .. import provenance
    from .. import structured_collections as collections

    # Command and action names are the public Records/Planning protocol, not customer vocabulary.
    actions = {"record_memory": {"append", "update", "bulk_upsert"}, "plan_memory": {"add", "update"}}
    if arguments.get("action") not in actions.get(command, ()):
        return False
    if arguments.get("held") is not None:
        return True
    rows = arguments.get("rows", ()) if arguments.get("action") == "bulk_upsert" else (arguments,)
    if not isinstance(rows, (list, tuple)):
        return False
    if arguments.get("action") == "bulk_upsert":
        from .writer import BULK_UPSERT_MAX_ROWS

        if len(rows) > BULK_UPSERT_MAX_ROWS:
            return False  # The ordinary public validator refuses oversized input before placement scans it.
    for row in rows:
        body = row.get("body") if isinstance(row, Mapping) else None
        if not isinstance(body, str):
            continue
        try:
            records._validate_body(body)
            parsed = provenance.parse_origin(body, managed=True, authoring=True)
        except (collections.CollectionError, provenance.OriginError, UnicodeError):
            continue  # The existing leaf reports malformed metadata before it can acquire an input.
        if parsed.payload is not None and any(
                value.get("representation") == "original" for value in parsed.payload["inputs"].values()):
            return True
    return False


@dataclass
class DependencyPlan:
    operation_id: str
    store_id: str
    instance_id: str
    collection_id: str
    references: tuple[str, ...]
    native: edit_back.NativeInputs
    admissions: tuple[source_custody.DependencyAdmission, ...] = ()
    held: dict[str, held_sources.Descriptor] = field(default_factory=dict)
    explicit: tuple = ()
    source_arguments: tuple = ()
    sources_prepared: bool = False
    current_references: tuple[str, ...] = ()
    owners: dict[tuple[str, int, str], version_sources.Binding] = field(default_factory=dict)
    native_by_key: dict = field(init=False, repr=False)
    reference_set: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self):
        self.reference_set = frozenset(self.references)
        self.native_by_key = {}
        for value in self.native.values:
            key = value.projection_path, value.token if value.slot != "offline" else None, value.slot
            if key in self.native_by_key and self.native_by_key[key] != value:
                raise connection.CollectionStoreError("COLLECTION_STORE_INPUT_CHANGED", "native preparation changed during observation")
            self.native_by_key[key] = value

    @property
    def producer(self):
        return source_custody.OwnerLocator(owner="collection_records", locator=self.store_id, version="2")

    @property
    def epochs(self):
        return {value.source_ref: value.epoch_id for value in self.admissions}

    def require_references(self, references):
        if not set(references) <= self.reference_set:
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_INPUT_CHANGED", "retained Source inputs differ from their detached admission")

    def version_bindings(self, conn, inputs, *, transition):
        bindings, completeness = version_sources.plan(conn, inputs, transition=transition,
            operation=self.operation_id, epochs=self.epochs)
        self.require_references(binding.canonical_ref for binding in bindings)
        self.owners.update(((binding.item_key, binding.row_version, binding.canonical_ref), binding) for binding in bindings)
        return bindings, completeness

    def held_descriptor(self, descriptor):
        value = held_sources.Descriptor.model_validate_json(descriptor)
        self.require_references(binding.canonical_ref for binding in value.bindings)
        self.held[value.publication_id] = value

    def native_input(self, path, token, slot, raw, version):
        expected = self.native_by_key.get((path, token if slot != "offline" else None, slot))
        return (expected is not None and expected.state == "present"
                and expected.raw == raw and expected.version == version)

    def witnesses(self, writer):
        """Detach only authenticated versions or exact held publications this operation wrote."""
        identity = views.store_identity(writer.connection)
        if identity != {"store_id": self.store_id, "instance_id": self.instance_id}:
            raise connection.CollectionStoreError("COLLECTION_STORE_INPUT_CHANGED", "dependency producer instance changed")
        result = []
        rows = writer.connection.execute(
            "WITH requested AS (SELECT json_extract(value,'$[0]') AS item_key,"
            "json_extract(value,'$[1]') AS row_version,json_extract(value,'$[2]') AS canonical_ref FROM json_each(?)) "
            "SELECT b.canonical_ref,t.transition_id,t.event_hash,b.auth_effect_ordinal,i.item_key,b.row_version,"
            "v.payload_hash,creation.transition_id "
            # CROSS JOIN keeps the request driving: stores keep no planner statistics, which would scan the collection.
            "FROM requested r CROSS JOIN items i ON i.collection_id=? AND i.item_key=r.item_key "
            "JOIN version_source_bindings b ON b.row_id=i.row_id AND b.row_version=r.row_version AND b.canonical_ref=r.canonical_ref "
            "JOIN txns t ON t.txn_id=b.auth_txn_id "
            "JOIN version_identity v ON v.row_id=b.row_id AND v.row_version=b.row_version "
            "JOIN txns creation ON creation.txn_id=v.txn_id", (json.dumps(tuple(self.owners)), self.collection_id)).fetchall()
        for reference, transition, event_hash, ordinal, key, version, payload, creator in rows:
            expected = self.owners[key, version, reference]
            if (payload, creator) != (expected.payload_hash, expected.creation_transition):
                continue
            version_hash = hashlib.sha256(json.dumps([self.instance_id, transition, event_hash, ordinal, key, version],
                separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            result.append((reference, source_custody.OwnerLocator(owner=self.producer.owner,
                locator=f"{self.store_id}:{transition}:{ordinal}", version=version_hash)))
        if self.held:
            rows = writer.connection.execute(
                "SELECT candidate_json,held_bytes,dependency_json FROM held_candidates "
                "WHERE dependency_json IS NOT NULL AND json_extract(dependency_json,'$.publication_id') "
                "IN (SELECT value FROM json_each(?))",
                (json.dumps(tuple(self.held)),)).fetchall()
            for candidate, raw, descriptor in rows:
                value = held_sources.Descriptor.model_validate_json(descriptor)
                if (self.held.get(value.publication_id) != value
                        or hashlib.sha256(candidate.encode()).hexdigest() != value.candidate_sha256
                        or hashlib.sha256(raw).hexdigest() != value.held_sha256):
                    continue
                digest = hashlib.sha256(json.dumps([self.instance_id, value.model_dump(mode="json")],
                    sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
                witness = source_custody.OwnerLocator(owner=self.producer.owner,
                    locator=f"{self.store_id}:{value.held_id}:{value.publication_id}", version=digest)
                result.extend((binding.canonical_ref, witness) for binding in value.bindings)
        return tuple(result)

    def observe(self, root, witnesses):
        admissions = {value.source_ref: value for value in self.admissions}
        for reference, witness in witnesses:
            if reference in admissions:
                source_custody.observe_dependency(root, admissions[reference], witness=witness)


def record_operation(method):
    """Compose Source phases around the existing record leaf without duplicating its signature."""
    signature = inspect.signature(method)

    @wraps(method)
    def call(writer, *args, **kwargs):
        if kwargs.get("_prepare") is not None or writer._dependency_plan is not None:
            return method(writer, *args, **kwargs)
        bound = signature.bind(writer, *args, **kwargs)
        bound.apply_defaults()
        values = dict(bound.arguments)
        values.pop("self")
        collection = values.pop("collection")
        records._validate_why(values["why"])
        records._validate_hold(values["hold"])
        phase = _ACQUISITION_PHASE.get()
        if phase == "prepare":
            prepared, plan, replay = _prepare_record(writer, method.__name__, collection, values)
            if replay is not None:
                return replay
            return RecordRequest(prepared, plan)
        if isinstance(phase, RecordRequest):
            if collection != phase.inputs.collection or values != phase.inputs.arguments or method.__name__ != phase.inputs.name:
                raise connection.CollectionStoreError("COLLECTION_STORE_INPUT_CHANGED", "record request arguments changed")
            return _commit_record(writer.root, method, collection, values, phase.inputs, phase.plan)
        return _write_record(writer, method, collection, values)

    return call


def bulk_operation(method):
    signature = inspect.signature(method)

    @wraps(method)
    def call(writer, *args, **kwargs):
        if writer._bulk_inputs is not None:
            return method(writer, *args, **kwargs)
        bound = signature.bind(writer, *args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        arguments.pop("self")
        collection = arguments.pop("collection")
        bulk_inputs.validate(arguments["rows"], arguments["why"], arguments["source"], arguments["on_reject"])
        phase = _ACQUISITION_PHASE.get()
        if phase == "prepare":
            prepared, replay = bulk_inputs.detach(writer, collection, arguments)
            return replay if replay is not None else BulkRequest(collection, prepared.arguments, prepared)
        if isinstance(phase, BulkRequest):
            if collection != phase.collection or arguments != phase.arguments:
                raise connection.CollectionStoreError("COLLECTION_STORE_INPUT_CHANGED", "bulk request arguments changed")
            return _commit_bulk(writer.root, method, collection, arguments, phase.inputs)
        return _write_bulk(writer, method, collection, arguments)

    return call


def _bulk_plan(writer, prepared, arguments):
    manifest = prepared.manifest
    effective = bool(prepared.plans) and not (prepared.result["counts"]["rejected"] and arguments["on_reject"] == "abort")
    inputs = prepared.source_inputs if effective else ()
    current = [reference for value in inputs for reference in value.current.references]
    references = [*current, *(reference for value in inputs for reference in value.prior.references)]
    projections = []
    with writer.read_snapshot(mutation=True):
        row, actual, _ = writer._collection(manifest.collection_id, facade_profile="records")
        writer._rows_writable(actual, "bulk_upsert")
        identity = views.store_identity(writer.connection)
        if (identity != prepared.store_identity or writer._container(row) != prepared.before_hash
                or actual.manifest_version != manifest.manifest_version):
            raise connection.CollectionStoreError("COLLECTION_STORE_INPUT_CHANGED", "bulk candidate basis changed")
        if effective and any(not writer._operation.allows_file(path) for path in prepared.source_guards):
            raise connection.CollectionStoreError("COLLECTION_STORE_INPUT_CHANGED", "bulk source authorization changed")
        requested = [(value.before_row_id, value.before_version) for value in inputs if value.before_row_id is not None]
        references.extend(value[0] for value in writer.connection.execute(
            "SELECT b.canonical_ref FROM json_each(?) r CROSS JOIN version_source_bindings b "
            "ON b.row_id=json_extract(r.value,'$[0]') AND b.row_version=json_extract(r.value,'$[1]')",
            (json.dumps(requested),)).fetchall())
        if effective:
            targets = [path for _key, _values, _body, path, _before, _source in prepared.plans if path is not None]
            projections = dependency_window(writer.connection, manifest, targets)
    native = edit_back.dependency_inputs(writer.root, projections)
    for value in native.values:
        if value.raw is not None:
            references.extend(held_sources.raw_facts(value.raw).references)
    return DependencyPlan(str(uuid.uuid4()), identity["store_id"], identity["instance_id"], manifest.collection_id,
        tuple(dict.fromkeys(references)), native, current_references=tuple(dict.fromkeys(current)))


def dependency_window(conn, manifest, targets):
    """The projections a write can displace: its own views, the aggregate views and the two live history pages.

    A numbered history page never changes after it fills, so only the main page and the newest page are read.
    """
    main = history.main_path(manifest)
    cid = manifest.collection_id
    statements = (
        ("SELECT * FROM projection_state WHERE path IN (SELECT value FROM json_each(?))", (json.dumps([*targets, main]),)),
        # The projection schema fixes these aggregate kinds; the dependency-window index bounds each lookup.
        ("SELECT * FROM projection_state WHERE collection_id=? AND kind IN ('manifest','summary','log')", (cid,)),
        # Page numbers grow past four digits, so order by length before text to find the newest page.
        ("SELECT * FROM projection_state WHERE collection_id=? AND kind='history' AND path<>? "
         "ORDER BY length(path) DESC, path DESC LIMIT 1", (cid, main)),
    )
    found = {}
    for statement, parameters in statements:
        cursor = conn.execute(statement, parameters)
        try:
            names = [value[0] for value in cursor.description]
            for value in cursor.fetchall():
                row = dict(zip(names, value, strict=True))
                found.setdefault(row["path"], row)
        finally:
            cursor.close()
    return list(found.values())


def _write_bulk(original, method, collection, arguments):
    from . import preview

    root, manager = original.root, writer_lease.active_manager()
    coordinator = manager._mutation_coordinator_for(root)
    guard = (nullcontext() if coordinator.current_thread_holds_boundary() and not coordinator.current_thread_borrows_boundary()
             else manager.mutation_guard(root, operation=method.__name__, reuse_operation_grant=True))
    context = {"_facade_profile": original._facade_profile,
               "_view_edit": None if original._view_edit is None else dict(original._view_edit)}
    with guard:
        phase = preview.source_metadata_phase(root)
        adapter = nullcontext() if phase is not None else preview.preview_store(root, original.handle)
        with adapter, ExitStack() as lifetime:
            phase = preview.source_metadata_phase(root)
            writer = phase.writer()._value
            for key, value in context.items():
                setattr(writer, key, value)
            prepared, replay = bulk_inputs.detach(writer, collection, arguments)
            if replay is not None:
                return replay
            bulk_inputs.acquire(root, prepared, why=arguments["why"], on_reject=arguments["on_reject"], lifetime=lifetime)
            for key, value in context.items():
                setattr(phase.writer()._value, key, value)
            return _commit_bulk(root, method, collection, arguments, prepared)


def _commit_bulk(root, method, collection, arguments, prepared):
    from . import preview

    phase = preview.source_metadata_phase(root)
    writer = phase.writer()._value
    if arguments != prepared.arguments:
        raise connection.CollectionStoreError("COLLECTION_STORE_INPUT_CHANGED", "bulk request arguments changed")
    plan = _bulk_plan(writer, prepared, arguments)
    return _publish(root, method, collection, arguments, plan, prepared)


def _publish(root, method, collection, arguments, plan, prepared):
    from . import preview

    phase = preview.source_metadata_phase(root)
    writer = phase.writer()._value
    context = {"_facade_profile": writer._facade_profile,
               "_view_edit": None if writer._view_edit is None else dict(writer._view_edit)}
    if plan.references:
        with phase.source_metadata():
            plan.admissions = source_custody.reserve_dependencies(root, plan.references,
                producer=plan.producer, operation_id=plan.operation_id, checkpoint=f"before:{plan.operation_id}")
            source_custody.begin_dependency_effect(root, plan.admissions)
    writer = phase.writer()._value
    for key, value in context.items():
        setattr(writer, key, value)
    writer._dependency_plan = plan
    if isinstance(prepared, bulk_inputs.PreparedBulk):
        writer._bulk_inputs = prepared
    else:
        writer._record_inputs = prepared
    try:
        result = method(writer, collection, **arguments)
    except BaseException:
        try:
            _settle(phase, plan)
        except Exception:  # noqa: BLE001 - preserve the original failure and unresolved epoch
            pass
        raise
    else:
        try:
            _settle(phase, plan)
        except Exception:  # noqa: BLE001 - committed rows cannot roll back for a missing acknowledgement
            result = records._with_warnings(result, ["source_dependency_pending"])
        return result
    finally:
        writer._dependency_plan = writer._bulk_inputs = writer._record_inputs = None


def _dependency_rows(writer, candidate):
    """Select dependency records in the candidate's passive SQL and policy snapshot."""
    from .. import structured_collections as collections

    manifest, held, before = candidate.manifest, candidate.held_basis, candidate.before
    arguments = candidate.arguments
    references = list(held_sources.raw_facts(candidate.body.encode()).references)
    current_references = list(references)
    if held is not None:
        references.extend(held_sources.raw_facts(held["held_bytes"]).references)
        if held["dependency_json"] is not None:
            references.extend(binding.canonical_ref for binding in
                held_sources.Descriptor.model_validate_json(held["dependency_json"]).bindings)
    if before is not None:
        references.extend(record_source_custody.facts(before["body"]).references)
        references.extend(value[0] for value in writer.connection.execute(
            "SELECT canonical_ref FROM version_source_bindings WHERE row_id=? AND row_version=?",
            (before["row_id"], before["row_version"])).fetchall())
    explicit, sources_prepared = (), False
    try:
        # Source and presentation preparation retain their existing guarded metadata reads, never downloads.
        _paths, _guards, resolved = writer._sources(arguments["sources"])
    except collections.CollectionError:
        pass  # The leaf keeps the existing error precedence and held-refusal disposition.
    else:
        explicit, sources_prepared = tuple(resolved), True
        source_refs = [value.canonical_ref for value in resolved if value.canonical_ref is not None]
        references.extend(source_refs)
        current_references.extend(source_refs)
    targets = [before["view_path"]] if before is not None else []
    if held is not None:
        targets.append(held["view_path"])
    projections = dependency_window(writer.connection, manifest, targets)
    return DependencyRows(candidate.store_identity, manifest.collection_id, references, current_references,
                          explicit, tuple(arguments["sources"]), sources_prepared, projections)


def _observe_dependencies(root, selected, candidate):
    native = edit_back.dependency_inputs(root, selected.projections)
    if candidate.adoption is not None and candidate.adoption.native is not None:
        observed = candidate.adoption.native
        if not any(value.path == candidate.adoption.path for value in native.values):
            native = edit_back.NativeInputs((*native.values, *observed.values), native.complete and observed.complete)
    references = list(selected.references)
    for value in native.values:
        if value.raw is not None:
            references.extend(held_sources.raw_facts(value.raw).references)
    return DependencyPlan(str(uuid.uuid4()), selected.identity["store_id"], selected.identity["instance_id"],
        selected.collection_id, tuple(dict.fromkeys(references)), native, explicit=selected.explicit,
        source_arguments=selected.source_arguments, sources_prepared=selected.sources_prepared,
        current_references=tuple(dict.fromkeys(selected.current_references)))


def _write_record(original, method, collection, arguments):
    name = method.__name__
    from . import preview

    root, manager = original.root, writer_lease.active_manager()
    coordinator = manager._mutation_coordinator_for(root)
    guard = (nullcontext() if coordinator.current_thread_holds_boundary() and not coordinator.current_thread_borrows_boundary()
             else manager.mutation_guard(root, operation=name, reuse_operation_grant=True))
    context = {"_facade_profile": original._facade_profile,
               "_view_edit": None if original._view_edit is None else dict(original._view_edit)}
    with guard:
        phase = preview.source_metadata_phase(root)
        if phase is not None and phase.writer()._value.handle is not original.handle:
            raise connection.CollectionStoreError("COLLECTION_STORE_INPUT_CHANGED", "record entry uses another store handle")
        adapter = nullcontext() if phase is not None else preview.preview_store(root, original.handle)
        with adapter, ExitStack() as lifetime:
            phase = preview.source_metadata_phase(root)
            writer = phase.writer()._value
            for key, value in context.items():
                setattr(writer, key, value)
            with ExitStack() as authorization:
                authorization.enter_context(writer._authorization())
                prepared, plan, replay = _prepare_record(writer, name, collection, arguments)
                if replay is not None:
                    return replay
                if (prepared.error is None and not plan.references and not arguments["sources"]
                        and prepared.adoption is None and not prepared.guards
                        and arguments == prepared.arguments and not record_inputs.requires_acquisition(prepared)):
                    # No acquisition or checkout departure intervenes; the leaf owns the same operation's final checks.
                    prepared.recheck_in_leaf = True
                    writer._record_authorization = authorization
                    try:
                        return _publish(root, method, collection, arguments, plan, prepared)
                    finally:
                        writer._record_authorization = None
            record_inputs.acquire(root, prepared, lifetime=lifetime)
            current = phase.writer()._value
            for key, value in context.items():
                setattr(current, key, value)
            return _commit_record(root, method, collection, arguments, prepared, plan)


def _prepare_record(writer, name, collection, arguments):
    with writer.read_snapshot(mutation=True):
        prepared, replay = record_inputs.from_snapshot(writer, name, collection, arguments)
        if replay is not None:
            return None, None, replay
        selected = _dependency_rows(writer, prepared)
    record_inputs.observe(writer, prepared)
    return prepared, _observe_dependencies(writer.root, selected, prepared), None


def _commit_record(root, method, collection, arguments, prepared, plan):
    from . import preview

    writer = preview.source_metadata_phase(root).writer()._value
    if arguments != prepared.arguments:
        raise connection.CollectionStoreError("COLLECTION_STORE_INPUT_CHANGED", "record request arguments changed")
    # Reauthorization belongs to this final unit; preparation's policy never crosses acquisition.
    with writer.read_snapshot(mutation=True):
        record_inputs.recheck_snapshot(writer, prepared)
        selected = _dependency_rows(writer, prepared)
    record_inputs.recheck_native(writer, prepared)
    fresh = _observe_dependencies(root, selected, prepared)
    if (fresh.references != plan.references or fresh.native != plan.native
            or fresh.sources_prepared != plan.sources_prepared):
        raise connection.CollectionStoreError("COLLECTION_STORE_INPUT_CHANGED", "record dependency basis changed")
    return _publish(root, method, collection, arguments, plan, prepared)


def _settle(phase, plan):
    if not plan.admissions:
        return
    writer = phase.writer()._value
    with writer.read_snapshot(mutation=True):
        witnesses = plan.witnesses(writer)
    if witnesses:
        with phase.source_metadata():
            plan.observe(writer.root, witnesses)
