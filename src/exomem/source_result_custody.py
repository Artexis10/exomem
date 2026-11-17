"""Exact private result publications backed by canonical image and Copy custody."""

from __future__ import annotations

import hashlib
import json
import logging
import pickle
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import capture_attempts, source_custody
from . import generated_fragment_store as generated
from . import source_result_attribution as attribution
from .source_capture import validate_sha256

log = logging.getLogger(__name__)
OWNER = "source_result_custody"  # This token selects the closed private result owner protocol.
_Opaque = Annotated[str, Field(min_length=1, max_length=1024)]


class Pointer(BaseModel):
    """The existing row incarnation; no caller-authored value supplies ownership."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    # These two tables are the existing capture-result persistence protocol.
    kind: Literal["mutation", "capture"]
    key: _Opaque
    digest: _Opaque
    attempt_id: _Opaque
    commit_token: _Opaque

    def locator(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    publication_id: _Opaque
    ledger_id: _Opaque
    boundary_id: _Opaque
    pointer: Pointer
    prior_publication_id: _Opaque | None
    # Prepared precedes Copy allocation; closed retains only content-free closure evidence.
    state: Literal["prepared", "selected", "closed"] = "prepared"
    image_id: _Opaque | None = None
    copy_id: _Opaque | None = None
    payload_sha256: str | None = None
    recipe: attribution.Recipe | None = None
    # The existing owners fix these serialization formats; opaque is for non-decoding producer integrations.
    encoding: Literal["pickle", "failure-json", "capture-json", "opaque"] = "opaque"
    closure: Literal["no_effect", "retired"] | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.payload_sha256 is not None:
            validate_sha256(self.payload_sha256)
        if self.state == "selected" and any(value is None for value in (
                self.image_id, self.copy_id, self.payload_sha256, self.recipe)):
            raise ValueError("selected Source result lacks its exact publication")
        if (self.state == "closed") != (self.closure is not None):
            raise ValueError("Source result closure differs from its publication state")
        if self.state == "closed" and self.recipe is not None:
            raise ValueError("closed Source result retains obsolete attribution")
        return self

    def encode(self) -> str:
        value = self.model_dump_json()
        if len(value.encode()) > generated.MAX_RECORD_BYTES:
            raise ValueError("Source result witness exceeds its metadata bound")
        return value


class PublicationChanged(RuntimeError):
    """A delayed result cannot replace a different attempt or publication."""


class Journal:
    """One private witness table; canonical Copies remain the obligation inventory."""

    def __init__(self, store):
        self.store = store
        self.ledger_id = validate_sha256(store.capture_attempts.ledger_id)
        with store._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS source_result_publications ("
                         "publication_id TEXT PRIMARY KEY, kind TEXT NOT NULL, row_key TEXT NOT NULL, "
                         "state TEXT NOT NULL, data TEXT NOT NULL)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS source_result_selected ON "
                         "source_result_publications(kind,row_key) WHERE state='selected'")

    def _check(self, conn) -> None:
        row = conn.execute("SELECT ledger_id FROM capture_attempt_ledger WHERE singleton=1").fetchone()
        if row is None or row[0] != self.ledger_id:
            raise PublicationChanged("Source result physical ledger changed")

    def read(self, conn, publication_id: str) -> Binding | None:
        self._check(conn)
        row = conn.execute("SELECT publication_id,kind,row_key,state,data FROM "
                           "source_result_publications WHERE publication_id=?", (publication_id,)).fetchone()
        return self._decode(row)

    def selected(self, conn, kind: str, key: str) -> Binding | None:
        """The caller reads payload and selection in this same SQLite snapshot."""
        self._check(conn)
        row = conn.execute("SELECT publication_id,kind,row_key,state,data FROM "
                           "source_result_publications WHERE kind=? AND row_key=? AND state='selected'",
                           (kind, key)).fetchone()
        return self._decode(row)

    def _decode(self, row) -> Binding | None:
        if row is None:
            return None
        value = Binding.model_validate_json(row[4])
        if ((value.publication_id, value.pointer.kind, value.pointer.key, value.state) != row[:4]
                or value.ledger_id != self.ledger_id):
            raise PublicationChanged("Source result witness has a different physical binding")
        return value

    def _put(self, conn, value: Binding) -> None:
        conn.execute("INSERT INTO source_result_publications VALUES (?,?,?,?,?) "
                     "ON CONFLICT(publication_id) DO UPDATE SET state=excluded.state,data=excluded.data",
                     (value.publication_id, value.pointer.kind, value.pointer.key, value.state, value.encode()))

    def prepare(self, pointer: Pointer, *, boundary_id: str,
                prior_publication_id: str | None) -> Binding:
        value = Binding(publication_id=uuid.uuid4().hex, ledger_id=self.ledger_id,
                        boundary_id=boundary_id, pointer=pointer, prior_publication_id=prior_publication_id)
        with self.store._connect() as conn:
            self._check(conn)
            self._put(conn, value)
        return value

    def publish(self, value: Binding, write: Callable[[sqlite3.Connection], int]) -> Binding | None:
        """Select bytes and map together; retain the displaced Copy's closure witness."""
        value = Binding.model_validate_json(value.encode())
        if value.state != "selected":
            raise ValueError("Source result publication requires a complete selected witness")
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self.read(conn, value.publication_id)
            selected = self.selected(conn, value.pointer.kind, value.pointer.key)
            if (current is None or current.state != "prepared" or current.pointer != value.pointer
                    or current.boundary_id != value.boundary_id
                    or (selected.publication_id if selected else None) != value.prior_publication_id):
                raise PublicationChanged("Source result publication right changed")
            if selected is not None:
                payload = _row_payload(conn, selected.pointer)
                if payload is None or hashlib.sha256(_bytes(payload)).hexdigest() != selected.payload_sha256:
                    raise PublicationChanged("Source result selected payload changed")
            if write(conn) != 1:
                raise PublicationChanged("Source result row incarnation changed")
            closed = None
            if selected is not None:
                closed = selected.model_copy(update={"state": "closed", "recipe": None, "closure": "retired"})
                self._put(conn, closed)
            self._put(conn, value)
            return closed

    def close_prepared(self, publication_id: str) -> Binding | None:
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            value = self.read(conn, publication_id)
            if value is not None and value.state == "prepared":
                value = value.model_copy(update={"state": "closed", "closure": "no_effect", "recipe": None})
                self._put(conn, value)
            return value

    def close_rows(self, conn, *, kind: str, keys: tuple[str, ...]) -> None:
        """Join the owner's existing deletion transaction, without canonical I/O per expired row."""
        self._check(conn)
        close_rows(conn, kind=kind, keys=keys)

    def forget_closed(self, value: Binding) -> None:
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self.read(conn, value.publication_id)
            if current != value or value.state != "closed":
                raise PublicationChanged("Source result closure witness changed")
            conn.execute("DELETE FROM source_result_publications WHERE publication_id=?", (value.publication_id,))


def _bytes(payload: bytes | str) -> bytes:
    return payload.encode("utf-8") if isinstance(payload, str) else payload


@dataclass(frozen=True)
class Published:
    value: Any
    binding: Binding
    copy: generated.Copy


class ResultCustody:
    """Publish and retire one ledger's results under its actual canonical boundary."""

    def __init__(self, root: Path, *, journal: Journal):
        from .writer_lease import active_manager

        self.root = Path(root).absolute()
        source_custody._boundary(self.root)
        self.manager = active_manager()
        if self.manager.idempotency is not journal.store:
            raise PublicationChanged("Source result belongs to another active ledger")
        self.journal = journal
        self.boundary_id = capture_attempts._boundary_id(self.manager._mutation_coordinator_for(self.root))
        self.catalogue = generated.GeneratedFragmentStore(self.root)

    def close(self) -> None:
        self.catalogue.close()

    def _boundary(self) -> None:
        from .writer_lease import active_manager

        source_custody._boundary(self.root)
        if (active_manager() is not self.manager or self.manager.idempotency is not self.journal.store
                or capture_attempts._boundary_id(self.manager._mutation_coordinator_for(self.root)) != self.boundary_id):
            raise PublicationChanged("Source result canonical boundary changed")

    def _owner(self, binding: Binding) -> source_custody.OwnerLocator:
        return source_custody.OwnerLocator(owner=OWNER, locator=binding.pointer.locator(), version=binding.publication_id)

    def publish(self, pointer: Pointer, value: Any, *, recipe: attribution.Recipe,
                encode: Callable[[Any], bytes | str], write: Callable[[sqlite3.Connection, bytes | str], int],
                prior_publication_id: str | None, encoding: str = "opaque") -> Published:
        self._boundary()
        # Typed producer attribution is checked before either canonical or SQLite publication.
        for reference in recipe.sources:
            source_custody.require_publishable(self.root, reference)
        payload = encode(value)
        digest = hashlib.sha256(_bytes(payload)).hexdigest()
        # One phase commits the image and its reservation; one closing change holds the acknowledgements.
        with generated.unit(self.root):
            prepared = self.journal.prepare(pointer, boundary_id=self.boundary_id,
                                            prior_publication_id=prior_publication_id)
            copy = None
            try:
                head = self.catalogue.initialize()
                self.catalogue.recover()
                for reference in recipe.sources:
                    manifest, _ = source_custody.read_manifest(self.root, reference)
                    if manifest is not None:
                        source_custody.register_owner(self.root, reference, source_custody.OwnerLocator(
                            owner=generated.OWNER, locator=head.catalogue_id, version=str(head.version)))
                image = self.catalogue.begin_opaque_image(source_refs=recipe.sources, image_sha256=digest,
                    image_size=len(_bytes(payload)), unknown_through_epoch=head.epoch if recipe.unknown else 0)
                copy = self.catalogue.reserve_copy(image.image_id, owner=self._owner(prepared),
                    origin_ledger=self.journal.ledger_id, expected_header=image, expected_root=self.root)
                selected = prepared.model_copy(update={"state": "selected", "image_id": image.image_id,
                    "copy_id": copy.copy_id, "payload_sha256": digest, "recipe": recipe, "encoding": encoding})
                # The image and copy reservation must be durable before the row selection names them.
                self.catalogue.barrier()
                displaced = self.journal.publish(selected, lambda conn: write(conn, payload))
            except BaseException:
                try:
                    closed = self.journal.close_prepared(prepared.publication_id)
                    if copy is not None and closed is not None and closed.closure == "no_effect":
                        self.catalogue.ack_copy(copy.copy_id, owner=copy.owner, outcome="no_effect")
                        self.catalogue.flush()
                        self.journal.forget_closed(closed)
                except Exception as error:  # noqa: BLE001 - cleanup cannot replace the original publication failure
                    log.warning("Source result no-effect acknowledgment remains unknown (%s)", type(error).__name__)
                raise
            self.after_sql_publication(selected)
            self._try_acknowledge(copy)
            if displaced is not None and displaced.copy_id is not None:
                observed = self.catalogue.copy_record(displaced.copy_id)
                if observed.state == "observed":
                    self._try_acknowledge(observed.value)
            return Published(value, selected, copy)

    def _try_acknowledge(self, copy: generated.Copy) -> None:
        try:
            self.acknowledge(copy)
        except Exception as error:  # noqa: BLE001 - durable row selection already succeeded; Copy remains an obligation
            log.warning("Source result acknowledgment remains pending (%s)", type(error).__name__)

    def after_sql_publication(self, binding: Binding) -> None:
        """Fault-injection seam after the durable atomic selection, before canonical acknowledgment."""

    def _payload(self, conn, pointer: Pointer):
        return _row_payload(conn, pointer)

    def _closed_copy(self, copy: generated.Copy):
        observed = self.catalogue.copy_record(copy.copy_id)
        if (observed.state == "observed" and observed.value.owner == copy.owner
                and observed.value.image_id == copy.image_id and observed.value.origin_ledger == copy.origin_ledger
                and observed.value.closed):
            return observed.value.outcome
        return None

    def acknowledge(self, copy: generated.Copy) -> Literal["present", "no_effect", "retired", "unknown"]:
        self._boundary()
        if copy.owner.owner != OWNER or copy.origin_ledger != self.journal.ledger_id:
            return "unknown"
        closed = self._closed_copy(copy)
        if closed is not None:
            return closed
        with self.journal.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            binding = self.journal.read(conn, copy.owner.version)
            if binding is None or binding.boundary_id != self.boundary_id or self._owner(binding) != copy.owner:
                return "unknown"
            if binding.state == "prepared":
                # Revocation and no-effect evidence precede releasing the row transaction.
                binding = binding.model_copy(update={"state": "closed", "closure": "no_effect", "recipe": None})
                self.journal._put(conn, binding)
            elif not (binding.state == "closed" and binding.closure == "no_effect" and binding.copy_id is None) and (
                    binding.copy_id != copy.copy_id or binding.image_id != copy.image_id):
                return "unknown"
            if binding.state == "selected":
                payload = self._payload(conn, binding.pointer)
                selected = self.journal.selected(conn, binding.pointer.kind, binding.pointer.key)
                if (payload is None or selected != binding
                        or hashlib.sha256(_bytes(payload)).hexdigest() != binding.payload_sha256):
                    return "unknown"
                outcome = "present"
            else:
                outcome = binding.closure
        # No SQLite connection spans canonical filesystem metadata publication.
        self.catalogue.ack_copy(copy.copy_id, owner=copy.owner, outcome=outcome)
        if binding.state == "closed":
            # The journal forgets its closure only after the canonical acknowledgement is durable.
            self.catalogue.flush()
            self.journal.forget_closed(binding)
        return outcome

    def retire(self, copy: generated.Copy, reference: str) -> Literal["retired", "no_effect", "unknown"]:
        """Redact this exact selected result after the canonical incarnation fence is durable."""
        from .writer_lease import (
            _deserialize_committed_failure_payload,
            _serialize_committed_failure_payload,
        )

        self._boundary()
        if copy.owner.owner != OWNER or copy.origin_ledger != self.journal.ledger_id:
            return "unknown"
        closed = self._closed_copy(copy)
        if closed is not None:
            return closed
        manifest, _ = source_custody.read_manifest(self.root, reference)
        if manifest is None or manifest.retirement is None:
            raise ValueError("Source result retirement requires its canonical incarnation fence")
        with self.journal.store._connect() as conn:
            conn.execute("BEGIN")
            binding = self.journal.read(conn, copy.owner.version)
            if binding is None or binding.boundary_id != self.boundary_id or self._owner(binding) != copy.owner:
                return "unknown"
            payload = self._payload(conn, binding.pointer) if binding.state == "selected" else None
            selected = self.journal.selected(conn, binding.pointer.kind, binding.pointer.key)
        if binding.state != "selected":
            outcome = self.acknowledge(copy)
            return outcome if outcome in ("retired", "no_effect") else "unknown"
        if (binding.copy_id != copy.copy_id or binding.image_id != copy.image_id or selected != binding
                or payload is None or hashlib.sha256(_bytes(payload)).hexdigest() != binding.payload_sha256
                or binding.recipe is None or reference not in binding.recipe.sources):
            return "unknown"
        if binding.encoding == "pickle":
            value, encode = pickle.loads(payload), pickle.dumps  # noqa: S301 - protected private runtime state
        elif binding.encoding == "capture-json":
            value, encode = json.loads(payload), capture_attempts._json
        elif binding.encoding == "failure-json":
            value, encode = _deserialize_committed_failure_payload(payload), _serialize_committed_failure_payload
        else:
            return "unknown"
        retired = frozenset(ref for ref in binding.recipe.sources
            if (owned := source_custody.read_manifest(self.root, ref)[0]) is not None and owned.retirement is not None)
        value, recipe = redact(value, binding.recipe, retired, encoding=binding.encoding)

        def write(conn, raw):
            if binding.pointer.kind == "capture":
                return conn.execute("UPDATE capture_attempts SET receipt=? WHERE ticket=? AND receipt IS ? "
                    "AND state IN ('completed','closed')", (raw, binding.pointer.key, payload)).rowcount
            pointer = binding.pointer
            return conn.execute("UPDATE mutations SET result=? WHERE key=? AND digest=? AND attempt_id=? "
                "AND commit_token=? AND result IS ?", (raw, pointer.key, pointer.digest,
                 pointer.attempt_id, pointer.commit_token, payload)).rowcount

        self.publish(binding.pointer, value, recipe=recipe, encode=encode, write=write,
                     prior_publication_id=binding.publication_id, encoding=binding.encoding)
        return "retired"


def mutation_pointer(key: str, row: tuple) -> Pointer:
    """Use the established SELECT digest,state,result,updated_at,owner,attempt_id,commit_token shape."""
    return Pointer(kind="mutation", key=key, digest=row[0], attempt_id=row[5], commit_token=row[6])


def capture_pointer(reservation: capture_attempts.CaptureReservation) -> Pointer:
    return Pointer(kind="capture", key=reservation.ticket, digest=reservation.request_sha256,
                   attempt_id=reservation.ticket, commit_token=reservation.ticket)


def _row_payload(conn, pointer: Pointer):
    if pointer.kind == "mutation":
        row = conn.execute("SELECT digest,attempt_id,commit_token,result FROM mutations WHERE key=?",
                           (pointer.key,)).fetchone()
        if row is None or row[:3] != (pointer.digest, pointer.attempt_id, pointer.commit_token):
            return None
        return row[3]
    row = conn.execute("SELECT reservation,receipt FROM capture_attempts WHERE ticket=?", (pointer.key,)).fetchone()
    if row is None or pointer != capture_pointer(capture_attempts.CaptureReservation.model_validate_json(row[0])):
        return None
    return row[1]


def close_rows(conn, *, kind: str, keys: tuple[str, ...]) -> None:
    """Preserve closure inside existing expiry/deletion transactions, including after restart."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_result_publications'").fetchone() is None:
        return
    if keys:
        # SQLite's existing JSON parser updates this closed witness schema in one bounded batch.
        conn.execute("UPDATE source_result_publications SET state='closed', "
                     "data=json_set(data,'$.state','closed','$.closure','retired','$.recipe',NULL) "
                     "WHERE kind=? AND state='selected' AND row_key IN (SELECT value FROM json_each(?))",
                     (kind, json.dumps(keys)))


def _tree(value):
    from .writer_lease import _CanonicalCommittedFailure

    return {"result": value.result, "payload": value.payload} if isinstance(value, _CanonicalCommittedFailure) else value


def _from_tree(original, tree):
    from .writer_lease import _CanonicalCommittedFailure

    return _CanonicalCommittedFailure(tree["result"], tree["payload"]) if isinstance(original, _CanonicalCommittedFailure) else tree


def _restore(value, recipe):
    from .writer_lease import _CanonicalCommittedFailure

    if isinstance(value, _CanonicalCommittedFailure):
        for part in ("result", "payload"):
            items = tuple(item.model_copy(update={"path": item.path[1:]})
                          for item in recipe.contributions if item.path[:1] == (part,))
            attribution.restore(getattr(value, part), attribution.Recipe(contributions=items, unknown=recipe.unknown))
    else:
        attribution.restore(value, recipe)


def carry_result(before, after):
    """Existing terminal refreshes preserve the same Source-result schema."""
    if not attribution.active():
        return after
    tree = _tree(after)
    attribution.carry(_tree(before), tree)
    _restore(after, attribution.recipe(tree))
    return after


def redact(value, recipe: attribution.Recipe, retired: frozenset[str], *, encoding: str = "pickle"):
    """Apply producer selectors; leave list ordinals stable and preserve historical control facts."""
    affected = [item for item in recipe.contributions if retired.intersection(item.source_refs)]
    if not affected:
        return value, recipe
    omitted = {item.path for item in affected if item.action == "omit"}
    counts = {item.path for item in affected if item.action == "count"}
    missing = object()

    def visit(node, path):
        if path in omitted:
            return missing
        if path in counts:
            return None
        if isinstance(node, dict):
            out = {}
            for key, child in node.items():
                result = visit(child, path + (key,))
                if result is not missing:
                    out[key] = result
            return out
        if isinstance(node, (list, tuple)):
            out = []
            for index, child in enumerate(node):
                result = visit(child, path + (index,))
                out.append({"state": "unavailable", "reason": "source_retired"} if result is missing else result)
            return tuple(out) if isinstance(node, tuple) else out
        return node

    tree = visit(_tree(value), ())
    if tree is missing:
        tree = {}
    kept = tuple(item for item in recipe.contributions if not retired.intersection(item.source_refs)
                 and not any(item.path[:len(path)] == path for path in omitted))
    from .writer_lease import _CanonicalCommittedFailure

    # Committed-failure counts describe the historical batch, not a remaining Source total.
    # Its fixed wire format already represents withheld target paths through omitted_target_count.
    failure_prefixes = (("payload",),) if isinstance(value, _CanonicalCommittedFailure) else ((),) if encoding == "failure-json" else ()
    for prefix in failure_prefixes:
        original = _tree(value)
        current = tree
        for part in prefix:
            original, current = original[part], current[part]
        original_targets = original["outcome"]["targets"]
        target_prefix = prefix + ("outcome", "targets")
        removed = {i for i in range(len(original_targets)) if target_prefix in omitted or target_prefix + (i,) in omitted}
        current["outcome"]["targets"] = [target for i, target in enumerate(original_targets) if i not in removed]
        current["outcome"]["omitted_target_count"] = current["outcome"]["affected_count"] - len(current["outcome"]["targets"])
        kept = tuple(item.model_copy(update={"path": item.path[:len(target_prefix)] + (
            item.path[len(target_prefix)] - sum(i < item.path[len(target_prefix)] for i in removed),
        ) + item.path[len(target_prefix)+1:]}) if item.path[:len(target_prefix)] == target_prefix
            and len(item.path) > len(target_prefix) and isinstance(item.path[len(target_prefix)], int) else item for item in kept)
    remaining = attribution.Recipe(contributions=kept, unknown=recipe.unknown)
    marker = {"state": "partial" if remaining.sources else "unavailable", "reason": "source_retired"}
    if isinstance(tree, dict):
        if isinstance(value, _CanonicalCommittedFailure):
            for part in ("result",):
                if isinstance(tree.get(part), dict):
                    tree[part]["source_projection"] = marker
                    if isinstance(tree[part].get("leaf_result"), dict):
                        tree[part]["leaf_result"]["source_projection"] = marker
        elif encoding != "failure-json":
            tree["source_projection"] = marker
            if isinstance(tree.get("leaf_result"), dict):
                tree["leaf_result"]["source_projection"] = marker
    result = _from_tree(value, tree)
    _restore(result, remaining)
    return result, remaining


_INVOCATION: ContextVar[Invocation | None] = ContextVar("source_result_invocation", default=None)


class Invocation:
    """Keep one Source invocation's selected row incarnation through delayed acknowledgment."""

    def __init__(self, manager, root: Path):
        self.manager, self.root = manager, Path(root)
        self.journal = manager.idempotency.source_results
        self.pointer: Pointer | None = None
        self.binding: Binding | None = None
        self.row_state: str | None = None
        self.value: Any = None
        self.targets: dict[str, tuple[str, ...]] = {}
        self.lifetime = ExitStack()

    def __enter__(self):
        self.lifetime.enter_context(attribution.scope())
        token = _INVOCATION.set(self)
        self.lifetime.callback(_INVOCATION.reset, token)
        return self

    def __exit__(self, *error):
        return self.lifetime.__exit__(*error)

    def pin(self, conn, pointer: Pointer, state: str) -> None:
        selected = self.journal.selected(conn, pointer.kind, pointer.key)
        if selected is not None and selected.pointer != pointer:
            raise PublicationChanged("Source result selection belongs to another row incarnation")
        self.pointer, self.binding, self.row_state = pointer, selected, state

    def _hold(self):
        boundary = self.manager._mutation_coordinator_for(self.root)
        if boundary.current_thread_holds_boundary() and not boundary.current_thread_borrows_boundary():
            return nullcontext()
        return self.manager.absorbing_mutation_guard(self.root, operation="source_result_publication")

    def _enrolled(self, recipe):
        return any(source_custody.read_manifest(self.root, ref)[0] is not None for ref in recipe.sources)

    def _retired(self, recipe):
        return frozenset(reference for reference in recipe.sources
                         if (manifest := source_custody.read_manifest(self.root, reference)[0]) is not None
                         and manifest.retirement is not None)

    def decode(self, value):
        if self.binding is None or self.binding.recipe is None:
            return value
        with self._hold():
            recipe = self.binding.recipe
            _restore(value, recipe)
            retired = self._retired(recipe)
            if retired:
                value, _ = redact(value, recipe, retired, encoding=self.binding.encoding)
                # The exact snapshot may already be displaced; a stale reader can redact locally,
                # but it cannot write that snapshot over the current publication.
                try:
                    if self.pointer.kind == "mutation":
                        self.persist_mutation(self.pointer.key, self.pointer.digest, value,
                            state=self.row_state, allowed=(self.row_state,),
                            encoding=self.binding.encoding)
                    else:
                        self.persist_capture(value)
                except PublicationChanged:
                    pass
            self.value = value
            return value

    def _publish(self, pointer, value, *, state, allowed, encode, write, encoding):
        if self.binding is None and not source_custody.available():
            return None
        if self.binding is None and self.value is None and not self._enrolled(attribution.recipe(_tree(value))):
            # Only a Source's own capture enrolls it, so an unenrolled result needs no boundary;
            # waiting would turn a busy holder into an uncertain acknowledgement of a committed write.
            return None
        with self._hold():
            if self.value is not None:
                carry_result(self.value, value)
            recipe = attribution.recipe(_tree(value))
            if not recipe.sources and self.binding is None:
                return None
            retired = self._retired(recipe)
            value, recipe = redact(value, recipe, retired, encoding=encoding)
            if not self._enrolled(recipe) and self.binding is None:
                return None  # Default-off capture does not create canonical custody.
            owner = ResultCustody(self.root, journal=self.journal)
            try:
                published = owner.publish(pointer, value, recipe=recipe, encode=encode,
                    write=write, prior_publication_id=self.binding.publication_id if self.binding else None, encoding=encoding)
            finally:
                owner.close()
            self.pointer, self.binding, self.row_state, self.value = pointer, published.binding, state, value
            _restore(value, recipe)
            return value

    def persist_mutation(self, key, digest, value, *, state, allowed, encode=None, attempt=None, encoding="pickle"):
        if encode is None:
            from .writer_lease import _serialize_committed_failure_payload

            encode = _serialize_committed_failure_payload if encoding == "failure-json" else pickle.dumps
        pointer = (Pointer(kind="mutation", key=key, digest=digest, attempt_id=attempt.attempt_id,
                           commit_token=attempt.commit_token) if attempt is not None else self.pointer)
        if pointer is None or pointer.kind != "mutation" or pointer.key != key or pointer.digest != digest:
            return None

        def write(conn, payload):
            # SQL states belong to this fixed row protocol; values and identities remain bound parameters.
            placeholders = ",".join("?" for _ in allowed)
            return conn.execute("UPDATE mutations SET result=?,state=?,owner=NULL,updated_at=? WHERE key=? "
                "AND digest=? AND attempt_id=? AND commit_token=? AND state IN (" + placeholders + ")",
                (payload, state, self.journal.store.clock(), key, digest, pointer.attempt_id,
                 pointer.commit_token, *allowed)).rowcount

        return self._publish(pointer, value, state=state, allowed=allowed, encode=encode, write=write, encoding=encoding)

    def persist_capture(self, value, *, reservation=None, retention_seconds=None):
        pointer = capture_pointer(reservation) if reservation is not None else self.pointer
        if pointer is None or pointer.kind != "capture":
            return None
        state = "completed"
        receipt = value.get("leaf_result", value)
        outcome = receipt.get("capture_outcome")

        def write(conn, payload):
            if retention_seconds is None:
                return conn.execute("UPDATE capture_attempts SET receipt=? WHERE ticket=? AND state IN ('completed','closed')",
                                    (payload, pointer.key)).rowcount
            return conn.execute("UPDATE capture_attempts SET state='completed',past_outcome=?,receipt=?,gc_after=? "
                "WHERE ticket=? AND state='accepted'", (outcome, payload,
                 self.journal.store.clock() + retention_seconds, pointer.key)).rowcount

        return self._publish(pointer, value, state=state, allowed=(), encode=capture_attempts._json, write=write, encoding="capture-json")


def current(store) -> Invocation | None:
    invocation = _INVOCATION.get()
    return invocation if invocation is not None and invocation.journal.store is store else None


def record_target(reference: str, path: str) -> None:
    """Bind the actual producer's allocated target, before a batch can retain its failure summary."""
    invocation = _INVOCATION.get()
    if invocation is not None:
        invocation.targets[path] = tuple(dict.fromkeys((*invocation.targets.get(path, ()), reference)))


def attribute_failure(payload):
    invocation = _INVOCATION.get()
    if invocation is not None:
        for index, target in enumerate(payload["outcome"]["targets"]):
            refs = invocation.targets.get(target)
            if refs:
                attribution.bind(payload, refs, paths=(("outcome", "targets", index),))
    return payload
