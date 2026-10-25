"""Canonical collection subjects and request-local authorization.

Only persisted identity metadata participates in a current-state decision.
Payloads and projected views are never an authorization source.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path
from typing import Any

from .. import access, capabilities, find_corpus, vault
from .. import structured_collections as collections
from ..find_types import ParsedPage
from ..governance import authorization_session_authority as authority
from ..governance import (
    authorization_session_lifecycle,
    connector_boundary,
    egress,
    lifecycle,
    membership,
    policy,
    raw_protection,
    store,
)
from ..governance.decisions import Decision, decide
from ..governance.principal import RequestPrincipal, effective_principal
from ..planning_values import PlanningValues
from . import tokens, types

#: Row decisions one summary release may stream when policy varies by row; past it, a typed refusal.
MAX_SUMMARY_ROW_DECISIONS = 100_000
RELEASE_LIMIT = "COLLECTION_RELEASE_LIMIT"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def manifest_metadata(text: str) -> str:
    data, _, _ = vault.parse_frontmatter(text, strict=True)
    if not isinstance(data, dict):
        raise ValueError("canonical manifest metadata is invalid")
    for name in ("tags", "classes"):
        if name in data and data[name] is not None and not isinstance(data[name], list):
            raise ValueError("canonical manifest metadata is invalid")
    page = ParsedPage(Path("_collection.md"), "_collection.md", data, "", "", 0)
    return _json({
        "projects": sorted({value.lower() for value in find_corpus.all_projects(data)}),
        "tags": sorted(set(page.tags)),
        "classes": sorted({str(value).lower() for value in (data.get("classes") or [])}),
    })


def row_metadata(schema: collections.ItemSchema, values: Mapping[str, Any], metadata: str) -> str:
    projects = _metadata(metadata)["projects"]
    selected = {}
    for name in ("tags", "classes"):
        if name in schema.fields and name in values:
            collections.validate_field_value(name, values[name], schema.fields[name])
            selected[name] = values[name]
    page = ParsedPage(Path("item.md"), "item.md", selected, "", "", 0)
    classes = selected.get("classes") or []
    if not isinstance(classes, list):
        raise ValueError("canonical row classes must be a list")
    return _json({"projects": projects, "tags": sorted(set(page.tags)),
                  "classes": sorted({str(value).lower() for value in classes})})


def held_metadata(schema: collections.ItemSchema, candidate: Mapping[str, Any], metadata: str,
                  before: Mapping[str, Any] | None = None) -> str:
    attempted = candidate.get("item", {})
    if candidate.get("action") == "update":
        if before is None:
            raise ValueError("held target is unavailable")
        attempted = dict(before)
        attempted.update(candidate.get("changes", {}))
        for name in candidate.get("delete_fields", ()):
            attempted.pop(name, None)
    return row_metadata(schema, attempted, metadata)


def _pass_release_limit(error: collections.CollectionError) -> None:
    """Re-raise the typed summary release limit, which a generic not-found refusal would hide."""
    if error.code == RELEASE_LIMIT:
        raise error


def _metadata(raw: str) -> dict[str, list[str]]:
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"projects", "tags", "classes"}:
        raise ValueError("canonical subject metadata is missing or invalid")
    for values in data.values():
        if not isinstance(values, list) or any(not isinstance(value, str) or value != value.lower()
                                               for value in values):
            raise ValueError("canonical subject metadata is invalid")
        if values != sorted(set(values)):
            raise ValueError("canonical subject metadata is not normalized")
    return data


@dataclass(frozen=True, slots=True)
class CanonicalGrantBasis:
    logical_vault_id: str
    store_id: str
    identity: str
    version: int
    payload_hash: str
    subject: membership.MetadataSubject
    default_audience: str
    manifest_version: int
    manifest_hash: str
    type_name: str
    type_version: int
    declaration_hash: str
    domain: str
    quarantined: bool = False

    @property
    def fingerprint(self) -> str:
        from dataclasses import asdict

        payload = asdict(self)
        if not self.quarantined:
            del payload["quarantined"]  # Existing non-quarantined grants retain their original fingerprints.
        return hashlib.sha256(
            f"exomem.collection-grant.{self.domain}.v1\0".encode() + _json(payload).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalSubject:
    collection_id: str
    row_id: int | str | None
    basis: CanonicalGrantBasis


def content_available(path, identity, access_rules, access_blocked, tombstones):
    """Share the current content floor across canonical subjects and generated history."""
    return (not access_blocked
            and not lifecycle.is_tombstoned_in(tombstones, path)
            and not (identity and lifecycle.is_tombstoned_in(tombstones, identity))
            and not access._matches(access_rules["excluded"], access._kb_relative(path)))


def subject_policy(basis, scope_ids, effective):
    """Apply the registry's default audience before using the shared decision kernel."""
    if basis.default_audience == "owner":
        scopes = {key: replace(scope, default_deny=True) for key, scope in effective.scopes.items()}
        if not scope_ids:
            default = f"collection-type-default:{basis.type_name}:{basis.type_version}"
            scopes[default] = policy.Scope(id=default, source="collection-type", default_deny=True)
            scope_ids = (default,)
        effective = replace(effective, scopes=scopes)
    return scope_ids, effective


@dataclass(frozen=True, slots=True)
class SummaryRelease:
    """A summary collection's release for one operation, decided without holding its row subjects."""

    manifest: CanonicalSubject
    decision: Decision
    row_prefix: str
    rows: int
    released: int
    held: tuple[tuple[CanonicalSubject, Decision], ...]
    snapshot: str

    @property
    def complete(self) -> bool:
        return self.released == self.rows and all(decision.level >= 6 for _subject, decision in self.held)


@dataclass(frozen=True, slots=True)
class _SummaryPolicyRelease:
    """One operation's policy decisions, before a reporting caller counts rows."""

    manifest: CanonicalSubject
    decision: Decision
    row_prefix: str
    held: tuple[tuple[CanonicalSubject, Decision], ...]
    container: str
    uniform_rows_complete: bool | None
    derived_rows: int | None


@dataclass(frozen=True, slots=True)
class _ReleaseSelection:
    """Inspection inputs belonging only to this operation's read snapshot."""

    catalog: tuple[CanonicalSubject, ...]
    released: tuple[CanonicalSubject, ...]
    manifest: CanonicalSubject
    inspection_basis: tuple | None
    grant_decisions: dict[str, Decision]
    snapshot: str
    contributors: tuple[CanonicalSubject, ...]
    notice_decisions: tuple[Decision, ...] | None


_METADATA_SEAL = object()


@dataclass(frozen=True, slots=True)
class _InspectionState:
    manifest: CanonicalSubject
    contributors: tuple[CanonicalSubject, ...]
    # None carries an inventory's unknown counts, never a numeric row claim.
    snapshot: str | None


@dataclass(frozen=True, slots=True)
class _QueryHistoryState:
    collection_id: str
    manifest_path: str
    profile: str
    snapshot: str
    history_hash: str
    owners: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _CanonicalInspectionEvidence:
    """Producer provenance with an opaque handle identity, never a resource."""

    root: Path
    handle: object
    store_identity: tuple[tuple[str, str], ...]
    principal: RequestPrincipal
    purpose: str | None
    selections: tuple[_InspectionState, ...]
    references: tuple[tuple[tuple[str | int, ...], str], ...]
    payload_hash: str
    _seal: object
    inventory: bool = False
    history: _QueryHistoryState | None = None

    def __reduce__(self) -> object:
        raise TypeError("canonical inspection evidence is process-local")


@dataclass(frozen=True, slots=True)
class _CanonicalCoverageEvidence:
    """Current contributors and stable receipt owners for one episode coverage result."""

    root: Path
    handle: object
    store_identity: tuple[tuple[str, str], ...]
    principal: RequestPrincipal
    purpose: str | None
    contributors: tuple[tuple[str, str, str, CanonicalSubject], ...]
    references: tuple[tuple[tuple[str | int, ...], str], ...]
    payload_hash: str
    _seal: object

    def __reduce__(self) -> object:
        raise TypeError("canonical coverage evidence is process-local")


class _CanonicalMetadataProjection(dict[str, Any]):
    """JSON-compatible output with private canonical metadata provenance."""

    def __init__(self, value, evidence) -> None:
        super().__init__(value)
        self._canonical_metadata_evidence = evidence

    def __reduce__(self) -> object:
        raise TypeError("canonical metadata projection is process-local")

    def __copy__(self):
        return dict(self)

    def __deepcopy__(self, memo):
        # Ordinary consumers may copy the public data, never its provenance.
        from copy import deepcopy

        return deepcopy(dict(self), memo)


def _metadata_evidence(value: Any) -> _CanonicalInspectionEvidence | _CanonicalCoverageEvidence | None:
    if type(value) is not _CanonicalMetadataProjection:
        return None
    evidence = getattr(value, "_canonical_metadata_evidence", None)
    if (type(evidence) not in (_CanonicalInspectionEvidence, _CanonicalCoverageEvidence)
            or evidence._seal is not _METADATA_SEAL):
        OperationAuthorization.refuse()
    return evidence


def _inspection_evidence(value: Any) -> _CanonicalInspectionEvidence | None:
    evidence = _metadata_evidence(value)
    return evidence if type(evidence) is _CanonicalInspectionEvidence else None


def _inspection_reference(value, location):
    try:
        for key in location:
            value = value[key]
        return value
    except (KeyError, IndexError, TypeError):
        return None


def _seal_inspection_projection(value, evidence):
    return _seal_metadata_projection(value, evidence)


def _coverage_receipts(value):
    """Validate the fixed receipt fields and readback tokens emitted by episode_workflow.coverage."""
    if (not isinstance(value, Mapping) or value.get("action") != "coverage"
            or type(value.get("receipts")) is not list
            or value.get("coverage_current") not in ("verified", "changed")):
        OperationAuthorization.refuse()
    identities = set()
    for receipt in value["receipts"]:
        if (type(receipt) is not dict or receipt.keys() != {
                "candidate_key", "leaf_id", "operation_id", "receipt_digest", "path", "readback"}
                or any(type(receipt[name]) is not str or not receipt[name]
                       for name in ("candidate_key", "leaf_id", "operation_id", "receipt_digest"))
                # nosemgrep: ep-word-membership -- episode_workflow.coverage fixes these wire readback tokens.
                or receipt["readback"] not in ("verified", "changed", "unavailable")
                or (receipt["path"] is None) != (receipt["readback"] == "unavailable")
                or receipt["path"] is not None and type(receipt["path"]) is not str):
            OperationAuthorization.refuse()
        identity = receipt["leaf_id"], receipt["operation_id"]
        if identity in identities:
            OperationAuthorization.refuse()
        identities.add(identity)
    return value["receipts"]


def _coverage_references(value, contributors):
    owners = {(leaf, operation, path) for leaf, operation, path, _subject in contributors}
    return tuple((("receipts", index, "path"), receipt["path"])
                 for index, receipt in enumerate(_coverage_receipts(value))
                 if (receipt["leaf_id"], receipt["operation_id"], receipt["path"]) in owners)


def _history_references(value, state):
    from .. import record_governance

    history = value.get("agent_history")
    if history is None or not record_governance._valid_agent_history(history, semantic_profile=state.profile):
        OperationAuthorization.refuse()
    identities = [event["transition_id"] for event in history["events"]]
    if len(set(identities)) != len(identities):
        OperationAuthorization.refuse()
    return tuple((("agent_history", "events", index, "canonical_path"), event["canonical_path"])
                 for index, event in enumerate(history["events"])
                 if (event["transition_id"], event["canonical_path"]) in state.owners)


def _seal_metadata_projection(value, evidence):
    """Only the producer and trusted terminal transformations call this."""
    if type(evidence) is _CanonicalCoverageEvidence:
        # File receipt removal can shift indexes; only the original sealed owners receive new slots.
        references = _coverage_references(value, evidence.contributors)
    elif evidence.history is not None:
        # History slots follow authenticated transitions when terminal filtering shifts indexes.
        references = _history_references(value, evidence.history)
    elif evidence.inventory:
        # Filtering file entries can shift canonical rows; identity, not position, owns each slot.
        owners = {(state.manifest.collection_id, state.manifest.basis.subject.path)
                  for state in evidence.selections}
        references = tuple((("collections", index, "manifest_path"), row["manifest_path"])
                           for index, row in enumerate(value["collections"])
                           if (row.get("collection_id"), row.get("manifest_path")) in owners)
    else:
        references = tuple((location, path) for location, path in evidence.references
                           if _inspection_reference(value, location) == path)
    evidence = replace(evidence, references=references,
                       payload_hash=hashlib.sha256(_json(value).encode()).hexdigest())
    return _CanonicalMetadataProjection(value, evidence)


def subjects(conn: sqlite3.Connection, cid: str, logical_vault_id: str, *, identity=None,
             view_path=None) -> tuple[CanonicalSubject, ...]:
    return tuple(iter_subjects(conn, cid, logical_vault_id, identity=identity, view_path=view_path))


def subject_factory(conn: sqlite3.Connection, cid: str, logical_vault_id: str):
    """Load one canonical contract for streamed or selected subject construction."""
    row = conn.execute(
        "SELECT c.manifest_path,c.source_path,c.layout,c.manifest_version,m.manifest_hash,m.governance_json,"
        "c.type_name,c.type_version,t.declaration_hash,t.declaration_json,ct.builtin,c.view_mode "
        "FROM collections c JOIN collection_manifests m "
        "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version "
        "JOIN collection_type_versions t ON t.name=c.type_name AND t.version=c.type_version "
        "JOIN collection_types ct ON ct.name=c.type_name WHERE c.collection_id=?", (cid,),
    ).fetchone()
    store_identity = conn.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()
    if row is None or store_identity is None or not store_identity[0]:
        raise ValueError("canonical subject is unavailable")
    (path, source_path, layout, version, content_hash, metadata, name, type_version, declaration_hash,
     declaration, builtin, view_mode) = row
    declared = types.parse_declaration(json.loads(declaration), builtin=bool(builtin))
    if (declared.name, declared.version) != (name, type_version):
        raise ValueError("canonical type identity differs")
    manifest_meta = _metadata(metadata)
    # Summary rows have no view file; ordinary declared row policy still governs them.
    audience = declared.default_audience

    def make(ref, row_id, policy_path, item_type, raw_metadata, row_version, payload, domain, quarantined=False):
        meta = _metadata(raw_metadata)
        subject = membership.MetadataSubject(
            policy_path, (ref,), tuple(meta["projects"]), tuple(meta["tags"]),
            (item_type.lower(),), tuple(meta["classes"]),
        )
        basis = CanonicalGrantBasis(logical_vault_id, store_identity[0], ref, row_version, payload,
                                    subject, audience, version, content_hash,
                                    name, type_version, declaration_hash, domain, bool(quarantined))
        return CanonicalSubject(cid, row_id, basis)

    return path, source_path, layout, view_mode, manifest_meta, declared, make, metadata, version, content_hash


def iter_subjects(conn: sqlite3.Connection, cid: str, logical_vault_id: str, *, identity=None,
                  view_path=None, row_key=None, include_held=True, batch_size=128,
                  query_order=False) -> Iterator[CanonicalSubject]:
    """Stream canonical metadata; a row key selects no manifest or held candidate."""
    if type(batch_size) is not int or not 1 <= batch_size <= 128:
        raise ValueError("canonical subject batches must contain 1 to 128 rows")
    if sum(selector is not None for selector in (identity, view_path, row_key)) > 1:
        raise ValueError("canonical subject lookup must have one selector")
    (path, source_path, layout, view_mode, manifest_meta, declared, make,
     metadata, version, content_hash) = subject_factory(conn, cid, logical_vault_id)
    summary = view_mode == "summary"
    if identity in (None, path) and row_key is None:
        yield make(path, None, path, "collection", metadata, version, content_hash, "manifest")
    item_key = identity.rsplit("/", 1)[-1] if identity and identity.startswith(f"exomem://{declared.item_type}/{cid}/") else None
    held_key = identity.rsplit("/", 1)[-1] if identity and identity.startswith(f"exomem://collection-held/{cid}/") else None
    item_filter, held_filter, item_args, held_args = "", "", (), ()
    if identity:
        item_filter, held_filter = " AND item_key=?", " AND held_id=?"
        item_args, held_args = (item_key,), (held_key,)
    elif view_path is not None:
        item_filter = held_filter = " AND view_path=?"
        item_args = held_args = (view_path,)
    elif row_key is not None:
        item_filter, item_args = " AND item_key=?", (row_key,)
    # Admission needs no result ordering; item keys follow the existing index
    # without a sorter whose spill files escape the private TEMP-table quota.
    item_order = "item_key" if query_order else "row_id"
    with closing(conn.execute(
        "SELECT row_id,item_key,row_version,payload_hash,view_path,governance_json "
        "FROM items WHERE collection_id=?" + item_filter + " ORDER BY " + item_order,
        (cid, *item_args),
    )) as cursor:
        while batch := cursor.fetchmany(batch_size):
            for row_id, key, row_version, payload, view_path, raw_metadata in batch:
                # Projects are inherited from the exact current manifest contract.
                meta = _metadata(raw_metadata)
                if meta["projects"] != manifest_meta["projects"]:
                    raise ValueError("canonical row project differs from its manifest")
                policy_path = path if summary else source_path if layout == "markdown-log" else view_path
                yield make(f"exomem://{declared.item_type}/{cid}/{key}", row_id, policy_path,
                           declared.item_type, raw_metadata, row_version, payload, "row")
    if not include_held or row_key is not None:
        return
    # Legacy summary corrections have no persisted floor; canonical source ownership supplies the same held basis.
    with closing(conn.execute(
        "SELECT held_id,view_path,governance_json,governance_hash,quarantined OR "
        "(kind='view-correction' AND EXISTS (SELECT 1 FROM projection_state p "
        "WHERE p.collection_id=h.collection_id AND p.kind='summary' AND p.path="
        "json_extract(CASE WHEN json_valid(h.diagnostics_json) THEN h.diagnostics_json END,'$[0].path'))) "
        "FROM held_candidates h "
        "WHERE collection_id=?" + held_filter + " ORDER BY held_id",
        (cid, *held_args),
    )) as cursor:
        while batch := cursor.fetchmany(batch_size):
            for held_id, path, raw_metadata, payload, quarantined in batch:
                yield make(f"exomem://collection-held/{cid}/{held_id}", held_id, path,
                           "held-record", raw_metadata, 1, payload, "held", quarantined)


def held_item_key(candidate_json: str) -> str | None:
    """The item key a held candidate names, or None for a new item; ValueError when it is unreadable.

    A refused write and a view correction both record the item they would change here.
    """
    if not isinstance(candidate_json, str):
        raise ValueError("held candidate is unreadable")
    candidate = json.loads(candidate_json)
    key = candidate.get("item_key") if isinstance(candidate, dict) else ()
    if key is not None and not isinstance(key, str):
        raise ValueError("held candidate is unreadable")
    return key


def held_shows_every_row(candidate_json: str) -> bool:
    """Whether a held candidate is a whole log view, which shows every row of its collection."""
    candidate = json.loads(candidate_json) if isinstance(candidate_json, str) else None
    return isinstance(candidate, dict) and candidate.get("view") == "log"


def held_targets(conn: sqlite3.Connection, hold: CanonicalSubject) -> tuple[CanonicalSubject, ...]:
    """The existing items a hold would change or show: its one item, every row for a whole log view,
    or none when it proposes a new item."""
    row = conn.execute("SELECT candidate_json FROM held_candidates WHERE collection_id=? AND held_id=?",
                       (hold.collection_id, hold.row_id)).fetchone()
    key = None if row is None else held_item_key(row[0])
    if key is not None:
        return tuple(islice(iter_subjects(conn, hold.collection_id, hold.basis.logical_vault_id, row_key=key), 1))
    if row is not None and held_shows_every_row(row[0]):
        return tuple(subject for subject in iter_subjects(conn, hold.collection_id, hold.basis.logical_vault_id,
                                                          include_held=False) if subject.basis.domain == "row")
    return ()


def _bound(subject: CanonicalSubject, logical_vault_id: str) -> CanonicalSubject:
    if subject.basis.logical_vault_id == logical_vault_id:
        return subject
    return replace(subject, basis=replace(subject.basis, logical_vault_id=logical_vault_id))


#: Row subjects the writer's release cache keeps across collections before shedding them.
_MAX_CACHED_SUBJECTS = 65536
_MAX_CACHED_STATES = 8


@dataclass(slots=True)
class _CollectionState:
    epoch: tuple
    subjects: dict[str, CanonicalSubject]
    summaries: OrderedDict
    occupied_path_keys: Counter[str]
    #: False once eviction shed the row subjects; the manifest subject, the
    #: per-dependency release summaries and the item path keys stay current.
    complete: bool = True


class ReleaseCache:
    """Handle-owned pure state; proposed identities belong to the transaction."""

    def __init__(self, conn):
        from .history import PrefixProofs
        from .inspection import InspectionCache

        self.conn = conn
        self.inspections = InspectionCache()
        self.history = PrefixProofs()
        self.history_windows = OrderedDict()
        self.history_sweep = None
        self.history_sweep_before = None
        self.history_sweep_next = None
        self.history_sweep_staged = False
        self.history_queue_next = {}
        self.inspections.reserve(sys.getsizeof(self.history_windows))
        self.states = OrderedDict()
        self.contracts = OrderedDict()
        self.memberships = OrderedDict()
        self.decisions = OrderedDict()
        self.pending = None
        self.replacements = {}
        self.observed = self._stamp()
        self.accounted = conn.total_changes
        self.unmanaged = False

    def _stamp(self):
        return (self.conn.total_changes, self.conn.execute("PRAGMA data_version").fetchone()[0],
                self.conn.execute("PRAGMA schema_version").fetchone()[0])

    def check(self):
        if self.pending is not None:
            stamp = self._stamp()
            if stamp[0] != self.accounted or stamp[1:] != self.observed[1:] or self.unmanaged:
                self.unmanaged = True
                self.clear()
        else:
            changed = self._stamp() != self.observed
            if changed or self.unmanaged:
                # total_changes does not rewind on ROLLBACK. An unknown transaction
                # stays non-reusable until its owner leaves that transaction.
                self.unmanaged = self.conn.in_transaction and (changed or self.unmanaged)
                self.clear()

    def clear(self):
        self.discard_history()
        self.inspections.clear()
        self.history.clear()
        self.states.clear()
        self.contracts.clear()
        self.memberships.clear()
        self.decisions.clear()
        self.replacements.clear()
        self.observed = self._stamp()

    def discard_history(self, cid=None, *, new_row=False):
        changed = False
        for key, window in tuple(self.history_windows.items()):
            if (cid is None or key[0] == cid) and (not new_row or window.unresolved):
                del self.history_windows[key]
                changed = True
        unresolved = (self.history_sweep is not None and self.history_sweep.unresolved
                      and (cid is None or self.history_sweep.key[0] == cid))
        unresolved |= (self.history_sweep_staged and self.history_sweep_next is not None
                       and self.history_sweep_next.unresolved and (cid is None or self.history_sweep_next.key[0] == cid))
        changed |= unresolved
        if not new_row or changed:
            if self.history_sweep is not None and (cid is None or self.history_sweep.key[0] == cid):
                self.history_sweep = None
            if self.history_sweep_staged and (self.history_sweep_next is None or cid is None
                                             or self.history_sweep_next.key[0] == cid):
                self.history_sweep_next, self.history_sweep_staged = None, False
            self.history_queue_next = {key: value for key, value in self.history_queue_next.items()
                                       if cid is not None and key[0] != cid}
        self._charge_history()
        return changed

    def _charge_history(self):
        from .inspection import _size

        size = sys.getsizeof(self.history_windows)
        size += sum(_size(key) + _size(window) for key, window in self.history_windows.items())
        size += _size((self.history_sweep, self.history_sweep_next, self.history_sweep_before, self.history_queue_next))
        self.inspections.reserve(size)
        return size

    def remember_history(self, key, window):
        from .history import MAX_READER_WINDOWS

        self.history_windows[key] = window
        self.history_windows.move_to_end(key)
        # One handle keeps at most sixteen reader windows within its existing inspection byte budget.
        while self.history_windows:
            self._charge_history()
            if len(self.history_windows) <= MAX_READER_WINDOWS and self.inspections._bytes <= self.inspections.maximum_bytes:
                return
            evicted, _ = self.history_windows.popitem(last=False)
            self._forget_history_coverage(evicted[0])
        self._charge_history()

    def _forget_history_coverage(self, cid):
        for key, window in tuple(self.history_windows.items()):
            if key[0] == cid and window.queued is not None:
                self.history_windows[key] = replace(window, queued=None)
        self.history_queue_next = {key: value for key, value in self.history_queue_next.items() if key[0] != cid}

    def stage_history_queue(self, key, checkpoint):
        if key in self.history_windows:
            self.history_queue_next[key] = checkpoint
        self._charge_history()

    def stage_history_sweep(self, sweep):
        self.history_sweep_next, self.history_sweep_staged = sweep, True
        self._charge_history()
        if self.inspections._bytes > self.inspections.maximum_bytes:
            self.inspections.clear()
        while self.history_sweep_next is not None and self.history_sweep_next.counts \
                and self.inspections._bytes > self.inspections.maximum_bytes:
            self.history_sweep_next = replace(self.history_sweep_next, counts=self.history_sweep_next.counts[:-1],
                                              excess_domains=True)
            self._charge_history()
        while self.history_windows and self.inspections._bytes > self.inspections.maximum_bytes:
            evicted, _ = self.history_windows.popitem(last=False)
            self._forget_history_coverage(evicted[0])
            self._charge_history()

    def queued_history_sweep(self):
        return self.history_sweep_next if self.history_sweep_staged else self.history_sweep

    def begin(self):
        self.check()
        self.pending = {}
        self.unmanaged = False
        self.replacements = {}
        self.accounted = self.conn.total_changes
        self.prepared = {}
        self.history_sweep_before = self.history_sweep
        self.history_sweep_next, self.history_sweep_staged = None, False
        self.history_queue_next = {}

    def account(self, changes):
        if self.pending is not None:
            self.accounted += changes

    def touch(self, cid, identity=None):
        if self.pending is not None:
            self.pending.setdefault(cid, set()).add(identity)

    def prepare(self):
        self.check()
        if not self.unmanaged and self.conn.total_changes == self.accounted:
            # Only a cached state takes committed deltas; an uncached collection needs none.
            self.prepared = {cid: {identity: next(iter(subjects(self.conn, cid, "unbound", identity=identity)), None)
                                  for identity in identities if identity is not None}
                             for cid, identities in self.pending.items()
                             if None not in identities and (cid in self.replacements or cid in self.states)}
        self.prepared_stamp = self._stamp()

    def finish(self, committed):
        changed = self.conn.total_changes != self.observed[0]
        if committed and not self.unmanaged and self.conn.total_changes == self.accounted:
            for cid, identities in self.pending.items():
                state = self.replacements.get(cid) or self.states.get(cid)
                if state is None:
                    continue
                if None in identities:
                    self.inspections.discard(cid)
                    self.states.pop(cid, None)
                    if cid in self.replacements:
                        self._remember_state(cid, state)
                else:
                    for identity, current in self.prepared.get(cid, {}).items():
                        if state.complete:
                            previous = state.subjects.get(identity)
                            if previous is not None and isinstance(previous.row_id, int):
                                key = collections._portable_path_key(previous.basis.subject.path)
                                state.occupied_path_keys[key] -= 1
                                if not state.occupied_path_keys[key]:
                                    del state.occupied_path_keys[key]
                            if current is not None:
                                state.subjects[identity] = current
                                if isinstance(current.row_id, int):
                                    state.occupied_path_keys[collections._portable_path_key(current.basis.subject.path)] += 1
                            else:
                                state.subjects.pop(identity, None)
                        elif current is not None and isinstance(current.row_id, int):
                            # Items are never deleted and keep their view path, so a
                            # shed state only needs each new row's path key.
                            key = collections._portable_path_key(current.basis.subject.path)
                            state.occupied_path_keys[key] = state.occupied_path_keys[key] or 1
                        for _denied, dirty in state.summaries.values():
                            dirty[identity] = current
            self._evict()
            if self.history_sweep_staged:
                self.history_sweep = self.history_sweep_next
            for key, checkpoint in self.history_queue_next.items():
                if key in self.history_windows:
                    self.history_windows[key] = replace(self.history_windows[key], queued=checkpoint)
        elif changed or self.unmanaged:
            self.discard_history()
            self.inspections.clear()
            self.history.clear()
            self.states.clear()
            self.contracts.clear()
            self.memberships.clear()
            self.decisions.clear()
        if not committed:
            self.history.clear()
            self.discard_history()
        if not committed:
            self.history_sweep = self.history_sweep_before
        self.history_sweep_before = None
        self.history_sweep_next, self.history_sweep_staged = None, False
        self.history_queue_next = {}
        self._charge_history()
        self.pending = None
        self.replacements = {}
        self.prepared = {}
        self.observed = self.prepared_stamp if committed else self._stamp()
        self.unmanaged = False

    @staticmethod
    def _remember(cache, key, value, maximum):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > maximum:
            cache.popitem(last=False)
        return value

    def _remember_state(self, cid, state):
        self.states[cid] = state
        self.states.move_to_end(cid)
        self._evict(keep=cid)

    def _evict(self, keep=None):
        """Shed least-recent row subjects before whole states, never a state's release summaries.

        A write past the cap keeps the manifest subject and complete-release
        summary it needs next, so it re-evaluates only its changed subjects.
        The caller still holding ``keep`` sees it whole until the next boundary.
        """
        total = sum(len(state.subjects) for state in self.states.values())
        for cid, state in self.states.items():
            if total <= _MAX_CACHED_SUBJECTS:
                break
            if cid == keep or not state.complete:
                continue
            total -= len(state.subjects) - 1
            state.subjects = {state.epoch[0]: state.subjects[state.epoch[0]]}
            state.complete = False
            self.inspections.discard(cid)
        while len(self.states) > _MAX_CACHED_STATES:
            evicted, _ = self.states.popitem(last=False)
            self.inspections.discard(evicted)

    def epoch(self, cid):
        row = self.conn.execute(
            "SELECT c.manifest_path,c.source_path,c.layout,c.manifest_version,c.type_name,c.type_version,"
            "m.manifest_hash,m.governance_json,t.declaration_hash,t.declaration_json,ct.builtin,c.view_mode "
            "FROM collections c JOIN collection_manifests m ON m.collection_id=c.collection_id "
            "AND m.manifest_version=c.manifest_version JOIN collection_type_versions t "
            "ON t.name=c.type_name AND t.version=c.type_version JOIN collection_types ct ON ct.name=c.type_name "
            "WHERE c.collection_id=?", (cid,),
        ).fetchone()
        if row is None:
            raise ValueError("canonical subject is unavailable")
        return row

    def state(self, cid, *, full=False):
        """The collection's release state; ``full`` callers also need every row subject."""
        self.check()
        epoch = self.epoch(cid)
        current = self.replacements.get(cid) or self.states.get(cid)
        if full and current is not None and not current.complete:
            # Streaming recomputation of shed subjects replaces the shed state.
            self.states.pop(cid, None)
            current = None
        unexplained = self.pending is not None and self.unmanaged
        if current is None or current.epoch != epoch or unexplained:
            self.inspections.discard(cid)
            entries = {s.basis.identity: s for s in subjects(self.conn, cid, "unbound")}
            occupied = Counter(collections._portable_path_key(subject.basis.subject.path)
                               for subject in entries.values() if isinstance(subject.row_id, int))
            current = _CollectionState(epoch, entries, OrderedDict(), occupied)
            if epoch[11] == "summary":
                # Summary release never needs row subjects; a residual caller's state is not retained.
                return current
            if self.pending is not None and (self.pending or self.unmanaged
                                            or self.conn.total_changes != self.observed[0]):
                self.replacements[cid] = current
            else:
                self._remember_state(cid, current)
        return current

    def overlay(self, cid):
        if self.pending is None or cid in self.replacements:
            return {}
        return {identity: next(iter(subjects(self.conn, cid, "unbound", identity=identity)), None)
                for identity in self.pending.get(cid, ()) if identity is not None}

    def point(self, identity):
        if identity.startswith("exomem://"):
            pieces = identity[len("exomem://"):].split("/")
            if len(pieces) != 3:
                return None
            cid = pieces[1]
            found = self.conn.execute("SELECT collection_id FROM collections WHERE collection_id=?", (cid,)).fetchone()
        else:
            found = self.conn.execute("SELECT collection_id FROM collections WHERE manifest_path=?", (identity,)).fetchone()
        if found is None:
            return None
        # Resolve current identity, including proposed content, never a previous grant basis.
        return next(iter(subjects(self.conn, found[0], "unbound", identity=identity)), None)

    def scopes(self, subject, candidate):
        key = (candidate.fingerprint, _bound(subject, "unbound").basis)
        if key not in self.memberships:
            self._remember(self.memberships, key, tuple(sorted(membership.evaluate_metadata(subject.basis.subject, candidate))), 65536)
        return self.memberships[key]


class OperationAuthorization:
    """One policy/time/access/authority snapshot, with grants grouped by subject."""

    def __init__(self, root: Path, conn: sqlite3.Connection, *, mutation: bool, cache=None,
                 ownership_proof=None) -> None:
        self.root, self.conn, self.mutation = root, conn, mutation
        self.cache = cache or ReleaseCache(conn)
        self.policy = policy.load(root)
        self.who = effective_principal()
        self.now = int(time.time())
        self.tombstones = lifecycle.tombstoned_paths(root)
        self.access_fingerprint, self.access, self.access_blocked = access._policy_state(root)
        self.context = self.who.verified_authorization_session
        self.purpose = self.who.purpose
        self.authority = None
        self.grants = {}
        self.catalogs = {}
        self.file_decisions = {}
        self._presentation_wikilinks = None
        # A verified base proof excludes unpublished file imports in a migration snapshot.
        self._ownership_proof = ownership_proof
        self.points = {}
        self.summary_memo = {}
        self.grants_loaded = False
        self.failed = self.context is not None and not isinstance(
            self.context, authorization_session_lifecycle.AuthorizationSessionContext
        )
        if isinstance(self.context, authorization_session_lifecycle.AuthorizationSessionContext):
            try:
                self.authority = store.open_authorization_session_connection(root)
                self.authority.execute("BEGIN")
                if self.purpose is None:
                    self.purpose = authority.active_session_purpose(
                        self.authority, context=self.context, audience=self.who.audience_id, now=self.now,
                    )
            except (authorization_session_lifecycle.AuthorizationSessionUnavailable,
                    FileNotFoundError, OSError, sqlite3.Error, store.UnsupportedGovernanceSchema):
                self.failed = True
        elif not self.policy.empty:
            self.purpose = egress._declared_purpose(root, self.who, self.purpose)

    def close(self) -> None:
        if self.authority is not None:
            if self.authority.in_transaction:
                self.authority.rollback()
            self.authority.close()
            self.authority = None

    def field_plan(self, manifest):
        from .field_admission import resolve

        return resolve(self, manifest)

    def field_manifest(self, cid):
        row = self.conn.execute(
            "SELECT c.manifest_path,m.manifest_text,m.manifest_hash FROM collections c "
            "JOIN collection_manifests m ON c.collection_id=m.collection_id "
            "AND c.manifest_version=m.manifest_version WHERE c.collection_id=?", (cid,)).fetchone()
        if row is None:
            self.refuse()
        path, text, digest = row
        # Build authorization inputs without re-entering it; field_plan still decides disclosure.
        manifest = collections.parse_manifest_bytes(
            self.root, path, text.encode(), planning_values=PlanningValues(self.root, server_side=True)
        )
        if manifest.collection_id != cid or manifest.manifest_version.hash != digest:
            self.refuse()
        return manifest

    def field_rows(self, cid, rows, snapshot):
        """Read only admitted columns; public versions describe their released values."""
        from io import BytesIO

        from ..query_engine.selected_values import read_selected_tree
        from . import typed_storage

        plan = self.field_plan(self.field_manifest(cid))
        if plan.owner:
            return typed_storage.hydrate(self.conn, rows), snapshot
        layout = (typed_storage.require_layout(self.conn, cid)
                  if typed_storage.collection_encoding(self.conn, cid) != typed_storage.JSON_V1 else None)
        for row in rows:
            if layout is None:
                values = read_selected_tree(BytesIO(row["values_json"].encode()), plan.fields,
                                            max_bytes=256 * 1024, check=lambda: None)
            else:
                values = typed_storage.selected_current_values(self.conn, layout, row["row_id"], plan.fields,
                                                               max_bytes=256 * 1024, check=lambda: None)
            row["values_json"] = _json(values)
            row["body"] = ""
            row["public_version"] = hashlib.sha256(_json([cid, row["item_key"], values]).encode()).hexdigest()
        visible = hashlib.sha256(_json(sorted((row["item_key"], row["public_version"]) for row in rows)).encode()).hexdigest()
        return rows, visible

    @property
    def logical_vault_id(self) -> str:
        return self.context.logical_vault_id if isinstance(
            self.context, authorization_session_lifecycle.AuthorizationSessionContext
        ) else "unbound"

    def catalog(self, cid: str, *, refresh=False) -> tuple[CanonicalSubject, ...]:
        try:
            if refresh or cid not in self.catalogs:
                self.catalogs[cid] = tuple(_bound(subject, self.logical_vault_id)
                                           for subject in self.canonical_subjects(cid))
            catalog = self.catalogs[cid]
            self._load_grants()
            return catalog
        except (ValueError, TypeError, sqlite3.Error,
                authorization_session_lifecycle.AuthorizationSessionUnavailable):
            self.refuse()

    def canonical_subjects(self, cid: str) -> tuple[CanonicalSubject, ...]:
        """Immutable unbound bases; only named grants need a session binding."""
        state = self.cache.state(cid, full=True)
        overlay = self.cache.overlay(cid)
        current = state.subjects
        if overlay:
            current = dict(current)
            for identity, subject in overlay.items():
                if subject is None:
                    current.pop(identity, None)
                else:
                    current[identity] = subject
        self._load_grants()
        return tuple(current.values())

    def _resolve(self, identity):
        if identity in self.points:
            subject = self.points[identity]
        else:
            subject = self.cache.point(identity)
            subject = _bound(subject, self.logical_vault_id) if subject else None
            if getattr(self.cache, "retain_points", True):
                self.points[identity] = subject
        return self.membership(subject) if subject else None

    def _load_grants(self):
        if self.grants_loaded:
            return
        self.grants_loaded = True
        if self.authority is not None and not self.failed:
            try:
                matched = authority.active_session_grants_for_projection_resolver(
                    self.authority, context=self.context, audience=self.who.audience_id,
                    purpose=self.purpose, resolve=self._resolve,
                    policy_fingerprint=self.policy.fingerprint, now=self.now,
                )
                for identity, grant in matched:
                    self.grants.setdefault(identity, []).append(grant)
            except (ValueError, TypeError, sqlite3.Error,
                    authorization_session_lifecycle.AuthorizationSessionUnavailable):
                self.failed = True
        if self.failed:
            self.refuse()

    def membership(self, subject: CanonicalSubject) -> authority.SessionMembership:
        return authority.SessionMembership(subject.basis.identity, subject.basis.fingerprint,
                                           self.cache.scopes(subject, self.policy))

    def _decision_key(self, subject: CanonicalSubject, *, connector_identity: tuple | None = None):
        return (_bound(subject, "unbound").basis, self.policy.fingerprint,
                self.who.audience_id, self.purpose,
                connector_boundary.cache_identity(self.root, self.who, self.policy)
                if connector_identity is None else connector_identity)

    def decision(self, subject: CanonicalSubject, *, session=True) -> Decision:
        """Decide one subject; a hold is released no further than the existing item it would change (A8).

        Without the item's decision, a hold would show a withheld item's values to an audience
        that every selector of the item withholds it from.
        """
        if subject.basis.quarantined and not (raw_protection.is_owner(self.who)
                                               and raw_protection.has_unrestricted_access(self.root, self.who)):
            return Decision(0)
        decision = self._subject_decision(subject, session=session)
        if decision.level == 0 or subject.basis.domain != "held":
            return decision
        try:
            targets = held_targets(self.conn, subject)
        except ValueError:
            # A hold whose item cannot be read could follow any item, so only the owner sees it.
            owner = raw_protection.is_owner(self.who) and raw_protection.has_unrestricted_access(self.root, self.who)
            return decision if owner else Decision(0)
        if not targets:
            return decision
        # A held whole log view is released as the log view is: only when every row it shows is.
        return egress._meet_decisions((decision, *(self._subject_decision(target, session=session)
                                                   for target in targets)))

    def _subject_decision(self, subject: CanonicalSubject, *, session=True) -> Decision:
        basis = subject.basis
        path = basis.subject.path
        if self.failed or self.policy.blocked or (not self.who.resolved and not self.policy.empty) or self.access_blocked:
            return Decision(0)
        if not content_available(path, basis.identity, self.access, self.access_blocked, self.tombstones):
            return Decision(0)
        if self.mutation:
            relative = access._kb_relative(path)
            if (access._matches(self.access["readonly"], relative)
                    or relative.split("/", 1)[0].casefold() in {value.casefold() for value in access._APPEND_ONLY}):
                return Decision(0)
        scope_ids = self.cache.scopes(subject, self.policy)
        ceiling = connector_boundary.decide_scopes(self.root, self.who, scope_ids, compiled=self.policy,
                                                   mutation=self.mutation, path=path)
        if ceiling.level == 0:
            return ceiling
        try:
            key = self._decision_key(subject)
        except connector_boundary.BoundaryUnavailable:
            return Decision(0)
        active = self.grants.get(basis.identity, ()) if session else ()
        if not active and key in self.cache.decisions:
            return self.cache.decisions[key]
        grants = list(self.policy.grants)
        if active:
            basis = _bound(subject, self.logical_vault_id).basis
        for grant in active:
            if authority.SessionMembership(basis.identity, basis.fingerprint, scope_ids) in grant.membership:
                grants.append(policy.StandingGrant(grant.grant_id, "authorization-session", grant.scope_ids,
                                                   grant.audience, grant.ceiling))
        scope_ids, effective = subject_policy(basis, scope_ids, self.policy)
        decision = decide(scope_ids, audience=self.who.audience_id, purpose=self.purpose,
                          policy=effective, active_grants=grants)
        if not active:
            self.cache._remember(self.cache.decisions, key, decision, 65536)
        return decision

    def _release_dependency(self):
        return (self.policy.fingerprint, self.who.audience_id, self.who.resolved, self.purpose,
                self.access_fingerprint, self.access_blocked, tuple(sorted(self.tombstones)), self.mutation,
                connector_boundary.cache_identity(self.root, self.who, self.policy))

    def uniform_release(self) -> bool:
        """No row-varying policy, grant, exclusion, tombstone or session: one decision per path and audience."""
        return (self.policy.empty and not self.policy.scopes and not self.policy.rules
                and not self.policy.grants and not self.tombstones and not self.access["excluded"]
                and not (self.mutation and self.access["readonly"])
                and self.context is None and not self.failed)

    def summary_release(self, cid: str) -> SummaryRelease | None:
        """Decide a summary collection without a row-subject state; None for an items collection.

        Summary rows share the manifest's path, projects and declared
        audience. Unless a scope selects by identity, tag or class, a session
        grant names a row or a tombstone names one, a single row's decision is
        every row's, so time and memory stay independent of the row count.
        Otherwise one stream decides each row, keeps no subject, and refuses
        past ``MAX_SUMMARY_ROW_DECISIONS`` instead of answering partially.
        Held candidates keep their own view paths and are decided one by one.
        """
        memo = (cid, self.conn.total_changes)
        if memo in self.summary_memo:
            return self.summary_memo[memo]
        head = self.summary_manifest(cid)
        if head is None:
            return None
        return self._materialize_summary_release(cid, self._summary_policy_release(cid, head), memo)

    def _summary_policy_release(self, cid, head):
        """Share row variation, held decisions and representative admission with reporting."""
        manifest, decision = head
        if decision.level < 6:
            self.refuse()
        generation, audit_head, declaration, builtin = self.conn.execute(
            "SELECT c.generation,c.audit_head,t.declaration_json,ct.builtin FROM collections c "
            "JOIN collection_type_versions t ON t.name=c.type_name AND t.version=c.type_version "
            "JOIN collection_types ct ON ct.name=c.type_name WHERE c.collection_id=?", (cid,),
        ).fetchone()
        item_type = types.parse_declaration(json.loads(declaration), builtin=bool(builtin)).item_type
        held = []
        for (held_id,) in self.conn.execute(
                "SELECT held_id FROM held_candidates WHERE collection_id=? ORDER BY held_id", (cid,)).fetchall():
            for subject in subjects(self.conn, cid, self.logical_vault_id,
                                    identity=f"exomem://collection-held/{cid}/{held_id}"):
                held.append((subject, self.decision(subject)))
        container = tokens.container_hash(cid, generation, audit_head)
        prefix = f"exomem://{item_type}/{cid}/"
        varies = (self._summary_rows_vary(cid, prefix)
                  or any(identity.startswith(prefix) for identity in self.grants)
                  or any(cid in tombstone.casefold() for tombstone in self.tombstones))
        derived = self.conn.execute("SELECT derived FROM collections WHERE collection_id=?", (cid,)).fetchone()[0]
        derived_rows = None
        uniform_rows_complete = None
        if derived:
            # A derived collection's rows are outside the store and carry no row metadata, so its
            # release is collection-uniform: one representative row decides every logged row.
            if varies:
                raise collections.CollectionError(
                    RELEASE_LIMIT, "a derived collection's rows are released whole or not at all",
                    {"max_row_decisions": 0})
            derived_rows = self.conn.execute("SELECT COALESCE(MAX(row_count_after),0) FROM import_members "
                                             "WHERE collection_id=? AND seq=(SELECT MAX(seq) FROM import_members "
                                             "WHERE collection_id=?)", (cid, cid)).fetchone()[0]
            row = replace(manifest.basis, identity=prefix + "derived", version=1, payload_hash="", domain="row",
                          subject=replace(manifest.basis.subject, refs=(prefix + "derived",), tags=(),
                                          types=(item_type.lower(),), classes=()))
            admitted = self.decision(replace(manifest, row_id=0, basis=row)).level >= 6
            uniform_rows_complete = admitted or derived_rows == 0
        elif not varies:
            first = self.conn.execute("SELECT item_key FROM items WHERE collection_id=? ORDER BY row_id LIMIT 1",
                                      (cid,)).fetchone()
            uniform_rows_complete = first is None
            if first is not None:
                sample = next(iter(subjects(self.conn, cid, self.logical_vault_id, identity=prefix + first[0])))
                uniform_rows_complete = self.decision(sample).level >= 6
        return _SummaryPolicyRelease(manifest, decision, prefix, tuple(held), container,
                                     uniform_rows_complete, derived_rows)

    def _materialize_summary_release(self, cid, selection, memo):
        rows = (selection.derived_rows if selection.derived_rows is not None else
                self.conn.execute("SELECT COUNT(*) FROM items WHERE collection_id=?", (cid,)).fetchone()[0])
        visible = hashlib.sha256(b"exomem.collection-summary-visible.v1\0" + selection.manifest.basis.fingerprint.encode())
        if selection.uniform_rows_complete is not None:
            released = rows if selection.uniform_rows_complete else 0
        else:
            released = visited = 0
            with closing(iter_subjects(self.conn, cid, self.logical_vault_id, identity=None, view_path=None,
                                       include_held=False)) as stream:
                for subject in stream:
                    if subject.row_id is None:
                        continue
                    visited += 1
                    if visited > MAX_SUMMARY_ROW_DECISIONS:
                        raise collections.CollectionError(
                            RELEASE_LIMIT, "summary rows vary by row-level policy past the bound",
                            {"max_row_decisions": MAX_SUMMARY_ROW_DECISIONS})
                    if self.decision(_bound(subject, self.logical_vault_id)).level >= 6:
                        released += 1
                        visible.update(f"{subject.row_id}:{subject.basis.version}\n".encode())
        if released == rows:
            visible.update(selection.container.encode())
        release = SummaryRelease(selection.manifest, selection.decision, selection.row_prefix,
                                 rows, released, selection.held, "")
        release = replace(release, snapshot=selection.container if release.complete else visible.hexdigest())
        self.summary_memo = {memo: release}
        return release

    def _summary_complete(self, cid, head):
        """Admission needs complete access, not a count of every unchanged row."""
        memo = (cid, self.conn.total_changes)
        if memo in self.summary_memo:
            return self.summary_memo[memo].complete
        selection = self._summary_policy_release(cid, head)
        if selection.uniform_rows_complete is None:
            # Varying policy retains the same bounded stream and typed refusals as reporting.
            return self._materialize_summary_release(cid, selection, memo).complete
        return (selection.uniform_rows_complete
                and all(decision.level >= 6 for _subject, decision in selection.held))

    def _summary_rows_vary(self, cid: str, prefix: str) -> bool:
        """Whether a configured scope can select some summary rows and not others.

        Rows share the manifest's path, projects and type. They differ only by
        their own ref, and by tags or classes taken from a declared ``tags`` or
        ``classes`` field (``row_metadata``), so a tag or class selector varies
        only where some manifest version of this collection declared one.
        """
        scopes = self.policy.scopes.values()
        if any(ref.startswith(prefix) for scope in scopes for ref in (*scope.refs, *scope.exclude_refs)):
            return True
        if not any(scope.tags or scope.classes or scope.exclude_tags or scope.exclude_classes for scope in scopes):
            return False
        return self.conn.execute(
            "SELECT 1 FROM collection_manifests WHERE collection_id=? AND (json_type(schema_json,'$.fields.tags') "
            "IS NOT NULL OR json_type(schema_json,'$.fields.classes') IS NOT NULL) LIMIT 1", (cid,),
        ).fetchone() is not None

    def summary_manifest(self, cid: str) -> tuple[CanonicalSubject, Decision] | None:
        """A summary collection's manifest subject and decision from one point read; None for items."""
        found = self.conn.execute(
            "SELECT manifest_path FROM collections WHERE collection_id=? AND view_mode='summary'", (cid,),
        ).fetchone()
        if found is None:
            return None
        self._load_grants()
        manifest = next(iter(subjects(self.conn, cid, self.logical_vault_id, identity=found[0])))
        return manifest, self.decision(manifest)

    def _release_selection(self, cid: str, *, full=False):
        """Reuse base policy work, then overlay this operation's fresh grants."""
        state = self.cache.state(cid, full=full)
        self._load_grants()
        dependency = self._release_dependency()
        if dependency not in state.summaries and not state.complete:
            state = self.cache.state(cid, full=True)
        manifest = _bound(state.subjects[state.epoch[0]], self.logical_vault_id)
        manifest_decision = self.decision(manifest)
        if manifest_decision.level < 6:
            self.refuse()
        if dependency not in state.summaries:
            denied = {identity for identity, subject in state.subjects.items() if subject.row_id is not None
                      and self.decision(subject, session=False).level < 6}
            self.cache._remember(state.summaries, dependency, (denied, {}), 16)
        denied, dirty = state.summaries[dependency]
        changed = {identity.rsplit("/", 1)[-1] for identity, subject in dirty.items()
                   if subject is not None and isinstance(subject.row_id, int)}
        if changed:
            # An item changes without its holds, whose decisions follow it, so they are decided again.
            for held_id, candidate in self.conn.execute(
                    "SELECT held_id,candidate_json FROM held_candidates WHERE collection_id=?", (cid,)):
                identity = f"exomem://collection-held/{cid}/{held_id}"
                try:
                    follows = held_item_key(candidate) in changed or held_shows_every_row(candidate)
                except ValueError:
                    continue  # An unreadable hold follows no item; its decision cannot change with one.
                if follows and identity in state.subjects:
                    dirty.setdefault(identity, state.subjects[identity])
        for identity, subject in dirty.items():
            if subject is not None and self.decision(subject, session=False).level < 6:
                denied.add(identity)
            else:
                denied.discard(identity)
        dirty.clear()
        return state, denied, self.cache.overlay(cid), manifest, manifest_decision

    def _subject(self, cid, state, overlay, identity):
        """A grant-named subject of this collection, from the overlay, the state or the store."""
        if identity in overlay or identity in state.subjects or state.complete:
            return overlay.get(identity, state.subjects.get(identity))
        subject = self.cache.point(identity)
        return subject if subject is not None and subject.collection_id == cid else None

    def require_complete_fields(self, cid: str) -> None:
        manifest = self.field_manifest(cid)
        plan = self.field_plan(manifest)
        # Partial field readers cannot guard full-state writes; a false refusal costs them a retry by the owner.
        if not plan.owner and (plan.whole_fields != set(manifest.schema.fields)):
            self.refuse()

    def require_collection(self, cid: str, *, complete: bool = True, refresh=False) -> tuple[CanonicalSubject, ...]:
        if complete and self.mutation:
            self.require_complete_fields(cid)
        try:
            head = self.summary_manifest(cid)
            if head is not None:
                if head[1].level < 6 or (complete and not self._summary_complete(cid, head)):
                    self.refuse()
                return ()
        except collections.CollectionError as error:
            _pass_release_limit(error)
            self.refuse()
        except (ValueError, TypeError, StopIteration, sqlite3.Error,
                authorization_session_lifecycle.AuthorizationSessionUnavailable):
            self.refuse()
        if complete:
            try:
                state, denied, overlay, _manifest, _decision = self._release_selection(cid)
                count = len(denied)
                for identity, subject in overlay.items():
                    count -= identity in denied
                    if subject is not None and self.decision(subject, session=False).level < 6:
                        count += 1
                # Session authority is fresh and can alter only its named subjects.
                for identity in self.grants:
                    subject = self._subject(cid, state, overlay, identity)
                    if subject is None or subject.row_id is None:
                        continue
                    subject = _bound(subject, self.logical_vault_id)
                    count += ((self.decision(subject).level < 6)
                              - (self.decision(subject, session=False).level < 6))
                if count:
                    self.refuse()
                return ()
            except (ValueError, TypeError, sqlite3.Error,
                    authorization_session_lifecycle.AuthorizationSessionUnavailable):
                self.refuse()
        catalog = self.catalog(cid, refresh=refresh)
        required = catalog if complete else catalog[:1]
        if any(self.decision(subject).level < 6 for subject in required):
            self.refuse()
        return catalog

    def released_subjects(self, cid: str) -> tuple[CanonicalSubject, ...]:
        """Current released identities, without loading payloads or rebinding every row."""
        try:
            state, denied, overlay, _manifest, _decision = self._release_selection(cid, full=True)
            return self._released_subjects(state, denied, overlay)[1]
        except (ValueError, TypeError, sqlite3.Error,
                authorization_session_lifecycle.AuthorizationSessionUnavailable):
            self.refuse()

    def _released_subjects(self, state, base_denied, overlay, grant_decisions=None):
        denied = base_denied
        if overlay or self.grants:
            denied = set(base_denied)
            for identity, subject in overlay.items():
                if subject is not None and self.decision(subject, session=False).level < 6:
                    denied.add(identity)
                else:
                    denied.discard(identity)
            for identity in self.grants:
                subject = overlay.get(identity, state.subjects.get(identity))
                if subject is not None and subject.row_id is not None:
                    decision = self.decision(subject)
                    if grant_decisions is not None:
                        grant_decisions[identity] = decision
                    if decision.level < 6:
                        denied.add(identity)
                    else:
                        denied.discard(identity)
        current = state.subjects
        if overlay:
            current = dict(current)
            for identity, subject in overlay.items():
                if subject is None:
                    current.pop(identity, None)
                else:
                    current[identity] = subject
        return current, tuple(subject for identity, subject in current.items()
                              if subject.row_id is not None and identity not in denied)

    def inspection_basis(self, cid: str):
        """Pure base-profile inputs, never this operation's grant overlay."""
        state, _denied, overlay, _manifest, _decision = self._release_selection(cid)
        # Inspection reuse is deliberately disabled while proposed SQL is live.
        if overlay or self.cache.pending is not None or self.cache.unmanaged:
            return None
        return state.epoch, self._release_dependency()

    def inspection_selection(self, cid: str, *, notices=False) -> _ReleaseSelection:
        try:
            release = self.summary_release(cid)
            if release is not None:
                catalog = (release.manifest, *(subject for subject, _decision in release.held))
                released = tuple(subject for subject, decision in release.held if decision.level >= 6)
                return _ReleaseSelection(
                    catalog, released, release.manifest, None, {}, release.snapshot, released,
                    (release.decision, *(decision for _subject, decision in release.held)) if notices else None,
                )
            state, denied, overlay, manifest, manifest_decision = self._release_selection(cid, full=True)
            grant_decisions = {}
            current, released = self._released_subjects(state, denied, overlay, grant_decisions)
            catalog = tuple(current.values())
            reusable = not overlay and self.cache.pending is None and not self.cache.unmanaged
            basis = (state.epoch, self._release_dependency()) if reusable else None
            notice_decisions = None
            if notices:
                decisions = []
                connector_identity = None
                for subject in catalog:
                    identity = subject.basis.identity
                    if subject.row_id is None:
                        decision = manifest_decision
                    elif identity in grant_decisions:
                        decision = grant_decisions[identity]
                    elif not reusable:
                        decision = self.decision(subject)
                    elif identity in denied:
                        # Pure policy results do not include L0 access/tombstone gates.
                        decision = self.decision(subject, session=False)
                    else:
                        # Only this notice assembly shares the input; terminal admission still obtains fresh authority.
                        if connector_identity is None:
                            connector_identity = connector_boundary.cache_identity(self.root, self.who, self.policy)
                        decision = self.cache.decisions.get(self._decision_key(
                            subject, connector_identity=connector_identity))
                        if decision is None:
                            decision = self.decision(subject, session=False)
                    decisions.append(decision)
                notice_decisions = tuple(decisions)
            return _ReleaseSelection(
                catalog, released, manifest, basis, grant_decisions,
                self.visible_snapshot(cid, released), released, notice_decisions,
            )
        except collections.CollectionError as error:
            _pass_release_limit(error)
            self.refuse()
        except (ValueError, TypeError, StopIteration, sqlite3.Error,
                authorization_session_lifecycle.AuthorizationSessionUnavailable):
            self.refuse()

    def inspection_evidence(self, payload, handle, selection: _ReleaseSelection) -> _CanonicalInspectionEvidence:
        """Bind all released contributors, not merely the public capped list."""
        locations = [("contract", "path"), ("contract", "storage", "source")]
        locations.extend(("source_versions", index, "path")
                         for index in range(len(payload["source_versions"])))
        references = tuple((location, value) for location in locations
                           if isinstance(value := _inspection_reference(payload, location), str))
        state = _InspectionState(selection.manifest, selection.contributors, selection.snapshot)
        return self._inspection_proof(handle, (state,), references)

    def inventory_selection(self, cid: str):
        """Count admitted metadata and retain its release proof in the same snapshot."""
        try:
            release = self.summary_release(cid)
        except collections.CollectionError as error:
            if error.code != RELEASE_LIMIT:
                raise
            return (None, None), self._unknown_inventory_state(cid)
        selection = self.inspection_selection(cid)
        if release is not None:
            committed = release.released
            held = sum(decision.level >= 6 for _subject, decision in release.held)
        else:
            committed = sum(isinstance(subject.row_id, int) for subject in selection.released)
            held = sum(isinstance(subject.row_id, str) for subject in selection.released)
        return (committed, held), _InspectionState(
            selection.manifest, selection.contributors, selection.snapshot)

    def _unknown_inventory_state(self, cid):
        manifest, decision = self.summary_manifest(cid)
        if decision.level < 6:
            self.refuse()
        return _InspectionState(manifest, (), None)

    def inventory_evidence(self, payload, handle, selections):
        """Certify canonical manifest slots; file entries retain ordinary byte validation."""
        evidence = self._inspection_proof(handle, tuple(selections), (), inventory=True)
        return _seal_metadata_projection(payload, evidence)

    def query_history_evidence(self, payload, writer, manifest):
        """Certify audit topology, never current file bytes or another query field."""
        history = payload["agent_history"]
        state = _QueryHistoryState(
            manifest.collection_id, manifest.path, manifest.semantic_profile, payload["snapshot"],
            hashlib.sha256(_json(history).encode()).hexdigest(),
            tuple((event["transition_id"], event["canonical_path"]) for event in history["events"]),
        )
        evidence = self._inspection_proof(writer.handle, (), (), history=state)
        return _seal_metadata_projection(payload, evidence)

    def _inspection_proof(self, handle, selections, references, *, inventory=False, history=None):
        from . import views

        return _CanonicalInspectionEvidence(
            self.root.resolve(), handle._inspection_identity, tuple(sorted(views.store_identity(self.conn).items())),
            self.who, self.purpose, selections, references, "", _METADATA_SEAL, inventory, history,
        )

    def validate_inspection_projection(self, payload, handle, *, writer=None):
        """Fresh canonical admission; physical views cannot change this metadata."""
        evidence = _inspection_evidence(payload)
        if evidence is None:
            return None
        try:
            self._validate_metadata_binding(payload, handle, evidence)
            if evidence.history is not None:
                from .history import agent_history

                state = evidence.history
                manifest = writer._collection(state.collection_id, facade_profile=state.profile)[1]
                if (manifest.path != state.manifest_path or payload.get("collection_id") != state.collection_id
                        or payload.get("snapshot") != state.snapshot
                        or hashlib.sha256(_json(agent_history(writer, manifest)).encode()).hexdigest()
                        != state.history_hash):
                    self.refuse()
                _history_references(payload, state)
            current = []
            for state in evidence.selections:
                cid = state.manifest.collection_id
                if state.snapshot is None:
                    if self._unknown_inventory_state(cid).manifest != state.manifest:
                        self.refuse()
                    current.append(state)
                    continue
                try:
                    selection = self.inspection_selection(cid)
                except collections.CollectionError as error:
                    if not evidence.inventory or error.code != RELEASE_LIMIT:
                        raise
                    # A newly bounded census remains an inventory with unknown counts.
                    limited = self._unknown_inventory_state(cid)
                    if limited.manifest != state.manifest:
                        self.refuse()
                    for row in payload["collections"]:
                        if row["collection_id"] == cid:
                            row["committed"] = row["held"] = None
                    current.append(limited)
                    continue
                contributors_match = (
                    selection.contributors == state.contributors
                    or frozenset(selection.contributors) == frozenset(state.contributors)
                )
                if (selection.manifest != state.manifest or not contributors_match
                        or selection.snapshot != state.snapshot):
                    self.refuse()
                current.append(state)
            evidence = replace(evidence, selections=tuple(current))
            return evidence
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            self.refuse()

    def _validate_metadata_binding(self, payload, handle, evidence):
        from . import views

        if (evidence.root != self.root.resolve() or evidence.handle is not handle._inspection_identity
                or evidence.principal != self.who or evidence.purpose != self.purpose
                or evidence.store_identity != tuple(sorted(views.store_identity(self.conn).items()))
                or evidence.payload_hash != hashlib.sha256(_json(payload).encode()).hexdigest()):
            self.refuse()

    def coverage_evidence(self, payload, handle, contributors):
        """Seal only authenticated coverage receipt paths; retain every current contributor."""
        from . import views

        contributors = tuple(contributors)
        references = _coverage_references(payload, contributors)
        if len(references) != len(contributors):
            self.refuse()
        return _CanonicalCoverageEvidence(
            self.root.resolve(), handle._inspection_identity, tuple(sorted(views.store_identity(self.conn).items())),
            self.who, self.purpose, contributors, references, "", _METADATA_SEAL,
        )

    def validate_coverage_projection(self, payload, writer):
        """Recheck current contributor authority, including receipts removed by earlier filtering."""
        evidence = _metadata_evidence(payload)
        if type(evidence) is not _CanonicalCoverageEvidence:
            self.refuse()
        try:
            self._validate_metadata_binding(payload, writer.handle, evidence)
            _coverage_receipts(payload)
            for _leaf, _operation, path, subject in evidence.contributors:
                row = self.conn.execute(
                    "SELECT item_key FROM items WHERE row_id=? AND collection_id=?",
                    (subject.row_id, subject.collection_id),
                ).fetchone()
                if (row is None
                        or writer.admitted_item_metadata(subject.collection_id, row[0], path) != subject):
                    self.refuse()
            return evidence
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            self.refuse()

    def visible_snapshot(self, cid: str, allowed: tuple[CanonicalSubject, ...]) -> str:
        state = self.cache.state(cid, full=True)
        manifest = _bound(state.subjects[state.epoch[0]], self.logical_vault_id)
        if not connector_boundary.unrestricted(self.root, self.who):
            # Hidden SQL allocations and store history cannot change a limited
            # snapshot; canonical admitted identities own this projection.
            return hashlib.sha256(b"exomem.collection-visible.v2\0" + _json([
                cid, manifest.basis.subject.path, manifest.basis.version,
                manifest.basis.payload_hash, manifest.basis.declaration_hash,
                sorted((subject.basis.subject.refs, subject.basis.subject.path,
                        subject.basis.version, subject.basis.payload_hash)
                       for subject in allowed if subject.row_id is not None),
            ]).encode()).hexdigest()
        current_count = len(state.subjects) - 1
        for identity, subject in self.cache.overlay(cid).items():
            current_count += (subject is not None) - (identity in state.subjects)
        if self.policy.empty and len(allowed) == current_count:
            generation, audit_head = self.conn.execute(
                "SELECT generation,audit_head FROM collections WHERE collection_id=?", (cid,),
            ).fetchone()
            return tokens.container_hash(cid, generation, audit_head)
        return hashlib.sha256(b"exomem.collection-visible.v1\0" + _json([
            manifest.basis.fingerprint,
            [(subject.row_id, subject.basis.version) for subject in allowed if isinstance(subject.row_id, int)],
        ]).encode()).hexdigest()

    def projection_ownership(self):
        """Use the caller's verified base namespace when rendering unpublished staging."""
        from . import ownership
        from .preview import production_bound

        return self._ownership_proof if self._ownership_proof is not None else ownership.current(
            self.root, conn=self.conn, private=not production_bound(self.root)
        )

    def projection_subjects(self, path: str) -> tuple[CanonicalSubject, ...] | None:
        """None is an ordinary file; an empty tuple is an unbound owned path."""
        proof = self.projection_ownership()
        entry = proof.owner(self.root, path)
        if entry is None:
            return None
        cid = entry["collection_id"]
        if cid not in proof.admitted:
            return ()
        owners = {cid}
        projection = self.conn.execute(
            "SELECT collection_id,row_id,kind FROM projection_state WHERE path=?", (path,),
        ).fetchone()
        if projection is not None and projection[0] != cid:
            return ()
        # nosemgrep: ep-word-membership -- The projection_state.kind CHECK fixes these kinds.
        if projection is not None and projection[2] not in {"manifest", "item", "held", "log", "summary", "history"}:
            return ()
        if len(owners) != 1:
            return ()
        cid = next(iter(owners))
        manifest_path, source_path, layout = self.conn.execute(
            "SELECT manifest_path,source_path,layout FROM collections WHERE collection_id=?", (cid,),
        ).fetchone()
        self._load_grants()
        if projection is not None and projection[2] == "history":
            return subjects(self.conn, cid, self.logical_vault_id, identity=manifest_path)
        if projection is not None and projection[2] == "summary":
            release = self.summary_release(cid)
            return (release.manifest,) if release is not None else ()
        if path == manifest_path:
            if projection is not None and projection[2] != "manifest":
                return ()
            return subjects(self.conn, cid, self.logical_vault_id, identity=path)
        if path == source_path and layout == "markdown-log":
            if projection is not None and projection[2] != "log":
                return ()
            return tuple(subject for subject in self.catalog(cid) if subject.row_id is None
                         or isinstance(subject.row_id, int))
        catalog = subjects(self.conn, cid, self.logical_vault_id, view_path=path)
        row_ids = {subject.row_id for subject in catalog if subject.row_id is not None}
        if not row_ids:
            return ()
        if projection is not None and (
            projection[2] not in {"item", "held"}
            or (projection[2] == "item" and projection[1] not in row_ids)
            or (projection[2] == "held" and not any(isinstance(key, str) for key in row_ids))
        ):
            return ()
        return catalog

    def projection_decision(self, path: str, *, content: bytes | None = None,
                            manifest_for=None) -> Decision | None:
        """Authorize canonical subjects, then prove any bytes are their current render."""
        try:
            targets = self.projection_subjects(path)
            if targets is None:
                return None
            if not targets:
                return Decision(0)
            manifest = self.field_manifest(targets[0].collection_id)
            plan = self.field_plan(manifest)
            projection_kind = self.conn.execute("SELECT kind FROM projection_state WHERE path=?", (path,)).fetchone()
            if not plan.owner and projection_kind == ("summary",):
                # The stored page carries private stamps; direct reads use the canonical released overview instead.
                return Decision(0)
            if not plan.owner and plan.whole_fields != set(manifest.schema.fields):
                # File views and audit rationale carry complete-field authority; recipients use admitted record projections.
                return Decision(0)
            decisions = tuple(self.decision(subject) for subject in targets)
            if path != targets[0].basis.identity:
                if decisions[0].level < 6:
                    return Decision(0)
                decisions = decisions[1:]
            decision = egress._meet_decisions(decisions)
            if decision.level == 0:
                return decision
            if projection_kind == ("history",):
                from . import history

                if not self._allows_path_metadata(path, metadata=history.page_subject(path)):
                    return Decision(0)
                context = history.PageContext(self.conn, self.root, manifest, cache=self.cache)
                if not context.exists(path):
                    # A retired page must not disclose a raw-history boundary through an empty matching header.
                    return Decision(0)
            if content is not None:
                from . import legacy_originals, views

                cursor = self.conn.execute("SELECT * FROM projection_state WHERE path=?", (path,))
                row = cursor.fetchone()
                if row is None:
                    return Decision(0)
                projection = dict(zip((column[0] for column in cursor.description), row, strict=True))
                # nosemgrep: ep-word-membership -- The projection_state.kind CHECK fixes these kinds.
                if projection["kind"] not in {"manifest", "item", "held", "summary", "log", "history"}:
                    return Decision(0)
                if projection["kind"] == "item" and not any(
                    subject.row_id == projection["row_id"] for subject in targets[1:]
                ):
                    return Decision(0)
                manifest = manifest_for(projection["collection_id"])
                _version, rendered = views.render_view(
                    self.conn, views.store_identity(self.conn), projection, manifest,
                    root=self.root, cache=self.cache, operation=self,
                )
                if content != rendered.encode("utf-8"):
                    # Migration preserves initial source bytes before asynchronous stamping.
                    if not legacy_originals.current_view_matches(self.conn, projection, content):
                        return Decision(0)
                parsed = find_corpus.parse_page(self.root / path, 0, self.root, content=content)
                if parsed is None:
                    return Decision(0)
                extra = set(membership.evaluate_snapshot(
                    parsed, self.policy, content_hash=hashlib.sha256(content).hexdigest(),
                )) - set(decision.scope_ids)
                if extra:
                    decision = egress._meet_decisions((decision,
                        connector_boundary.decide_scopes(self.root, self.who, extra, compiled=self.policy),
                        decide(extra, audience=self.who.audience_id, purpose=self.purpose,
                               policy=self.policy, active_grants=self.policy.grants)))
            return decision
        except (collections.CollectionError, membership.MembershipUnresolved, ValueError,
                TypeError, KeyError, sqlite3.Error):
            return Decision(0)

    def allows_file(
        self, path: str, *, content_sha256: str | Callable[[], str] | None = None
    ) -> bool:
        """Gate ancillary reads without reopening policy or session authority.

        ``content_sha256`` is a caller-proved digest of the file's current bytes
        (a guarded import source); session grants then bind to it without
        rereading a large file. A callable proves it only when an active session
        grant names the path, so a file nothing else releases is refused unread.
        """
        from ..governance import raw_protection

        if not raw_protection.permits(self.root, path, self.who):
            return False
        projection = self.projection_decision(path)
        if projection is not None:
            return projection.level >= 6
        return self._allows_path_metadata(path, content_sha256)

    def allows_history_path(self, path: str) -> bool:
        """Gate audit topology, not file bytes, including deleted legacy paths."""
        from ..governance import raw_protection

        if not raw_protection.permits(self.root, path, self.who):
            return False
        targets = self.projection_subjects(path)
        if targets:
            return all(self.decision(subject).level >= 6 for subject in targets)
        return self._allows_path_metadata(path)

    def _allows_path_metadata(
        self, path: str, content_sha256: str | Callable[[], str] | None = None, *, metadata=None,
    ) -> bool:
        if not connector_boundary.permits(self.root, path, self.who):
            return False
        # A digest proved on demand belongs to one call, so that decision is not cached.
        key = (
            path
            if content_sha256 is None
            else (path, content_sha256) if isinstance(content_sha256, str) else None
        )
        if metadata is not None:
            key = (key, metadata)
        if key in self.file_decisions:
            return self.file_decisions[key].level >= 6
        decision = Decision(0)
        try:
            if (self.failed or self.policy.blocked or self.access_blocked
                    or (not self.who.resolved and not self.policy.empty)
                    or not content_available(path, None, self.access, self.access_blocked, self.tombstones)):
                return False
            # A renderer supplies canonical metadata; editable bytes never supply this classification proof.
            scope_ids = (membership.evaluate_metadata(metadata, self.policy) if metadata is not None else
                         membership.evaluate_path_only(self.root, path, self.policy).require_classified())
            grants = list(self.policy.grants)
            if self.authority is not None and not self.policy.empty:
                fingerprint = content_sha256
                if fingerprint is None:
                    page, relative = vault.resolve_under_vault(
                        self.root, path, must_exist=True, must_be_file=True
                    )
                    if relative != path:
                        return False
                    fingerprint = hashlib.sha256(vault.read_bytes_without_pinning(page)).hexdigest()
                scopes = tuple(sorted(scope_ids))
                current: list[authority.SessionMembership] = []

                def resolve(identity: str) -> authority.SessionMembership | None:
                    # Called only for paths an active grant names.
                    if identity != path:
                        return None
                    if not current:
                        digest = fingerprint() if callable(fingerprint) else fingerprint
                        current.append(authority.SessionMembership(path, digest, scopes))
                    return current[0]

                matched = authority.active_session_grants_for_projection_resolver(
                    self.authority, context=self.context, audience=self.who.audience_id,
                    purpose=self.purpose, resolve=resolve, policy_fingerprint=self.policy.fingerprint,
                    now=self.now,
                )
                grants.extend(policy.StandingGrant(grant.grant_id, "authorization-session", grant.scope_ids,
                                                  grant.audience, grant.ceiling)
                              for identity, grant in matched if identity == path and current[0] in grant.membership)
            decision = decide(scope_ids, audience=self.who.audience_id, purpose=self.purpose,
                              policy=self.policy, active_grants=grants)
        except (membership.MembershipUnresolved, vault.VaultPathError, OSError, ValueError,
                sqlite3.Error, authorization_session_lifecycle.AuthorizationSessionUnavailable):
            pass
        finally:
            if key is not None:
                self.file_decisions[key] = decision
        return decision.level >= 6

    @staticmethod
    def refuse() -> None:
        raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")

    def authorized_rows(self, cid: str) -> tuple[list[dict[str, Any]], str, set[str]]:
        """Decode only released rows; the manifest is an independent L6 gate."""
        try:
            release = self.summary_release(cid)
        except collections.CollectionError as error:
            _pass_release_limit(error)
            self.refuse()
        except (ValueError, TypeError, StopIteration, sqlite3.Error,
                authorization_session_lifecycle.AuthorizationSessionUnavailable):
            self.refuse()
        if release is not None:
            return self._summary_rows(cid, release)
        allowed = self.released_subjects(cid)
        rows = []
        row_ids = [subject.row_id for subject in allowed if isinstance(subject.row_id, int)]
        for offset in range(0, len(row_ids), 512):
            batch = row_ids[offset:offset + 512]
            cursor = self.conn.execute(f"SELECT * FROM items WHERE row_id IN ({','.join('?' for _ in batch)})", batch)
            names = [column[0] for column in cursor.description]
            rows.extend(dict(zip(names, row, strict=True)) for row in cursor)
        rows, snapshot = self.field_rows(cid, rows, self.visible_snapshot(cid, allowed))
        rows.sort(key=lambda row: (row["view_path"] or "", row["item_key"]))
        return rows, snapshot, {
            subject.row_id for subject in allowed if isinstance(subject.row_id, str)
        }


    def _summary_rows(self, cid: str, release: SummaryRelease) -> tuple[list[dict[str, Any]], str, set[str]]:
        """A whole summary snapshot only within the interactive visit and deadline bounds.

        Larger reads page through a collection query with its continuation;
        this never returns some of the rows as if they were all of them.
        """
        from ..query_engine.runtime import QueryLimits
        limits = QueryLimits()
        if release.released > limits.max_row_visits:
            raise collections.CollectionError(
                "QUERY_COST_LIMIT", "summary rows exceed one bounded read; page them with a collection query",
                {"released_rows": release.released, "max_row_visits": limits.max_row_visits})
        deadline = time.monotonic() + limits.timeout_ms / 1000
        rows = []
        if release.released and self.conn.execute("SELECT derived FROM collections WHERE collection_id=?",
                                                  (cid,)).fetchone()[0]:
            from .derived_rows import refused

            # The store holds none of a derived collection's rows, so this read would answer none of them.
            raise refused("a whole-collection row read", repair="read its rows with a collection query: "
                          + capabilities.active_call_text("record_memory", {"action": "query", "query": {"version": 1}}))
        if release.released:
            uniform = release.released == release.rows
            with closing(self.conn.execute("SELECT * FROM items WHERE collection_id=? ORDER BY row_id", (cid,))) as cursor:
                names = [column[0] for column in cursor.description]
                while batch := cursor.fetchmany(512):
                    if time.monotonic() > deadline:
                        raise collections.CollectionError(
                            "QUERY_TIMEOUT", "summary rows exceed one bounded read; page them with a collection query",
                            {"timeout_ms": limits.timeout_ms})
                    batch = [dict(zip(names, row, strict=True)) for row in batch]
                    if not uniform:
                        batch = [row for row in batch if self._summary_row_released(release, row)]
                    rows.extend(batch)
        rows.sort(key=lambda row: row["item_key"])
        held = {subject.row_id for subject, decision in release.held if decision.level >= 6}
        rows, snapshot = self.field_rows(cid, rows, release.snapshot)
        return rows, snapshot, held

    def _summary_row_released(self, release: SummaryRelease, row: Mapping[str, Any]) -> bool:
        found = subjects(self.conn, release.manifest.collection_id, self.logical_vault_id,
                         identity=release.row_prefix + row["item_key"])
        return bool(found) and self.decision(found[0]).level >= 6


def resolve_bound_membership(root: Path, identity: str, candidate: policy.Policy,
                             logical_vault_id: str) -> authority.SessionMembership | None:
    """Resolve dark-store grant subjects through the explicit trusted binding."""
    from .preview import bound_writer

    writer = bound_writer(root)
    if writer is not None:
        writer._require_operation_context()
        cache = writer.handle.release_cache
        cache.check()
        subject = cache.point(identity)
        if subject is not None:
            subject = _bound(subject, logical_vault_id)
            return authority.SessionMembership(identity, subject.basis.fingerprint,
                                                cache.scopes(subject, candidate))
    if identity.startswith("exomem://"):
        raise authorization_session_lifecycle.AuthorizationSessionUnavailable
    return None
