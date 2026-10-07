"""Canonical collection subjects and request-local authorization.

Only persisted identity metadata participates in a current-state decision.
Payloads and projected views are never an authorization source.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .. import access, find_corpus, records, vault
from .. import structured_collections as collections
from ..find_types import ParsedPage
from ..governance import authorization_session_authority as authority
from ..governance import (
    authorization_session_lifecycle,
    egress,
    lifecycle,
    membership,
    policy,
    store,
)
from ..governance.decisions import Decision, decide
from ..governance.principal import OWNER_AUDIENCE, RequestPrincipal, effective_principal
from . import tokens, types

#: A summary collection's subjects: released to the owner alone, whatever configured rules say,
#: until field release governs summary rows (S1.5b). A non-owner sees no rows, pages or counts.
OWNER_ONLY = "owner-only"
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

    @property
    def fingerprint(self) -> str:
        from dataclasses import asdict

        return hashlib.sha256(
            f"exomem.collection-grant.{self.domain}.v1\0".encode() + _json(asdict(self)).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalSubject:
    collection_id: str
    row_id: int | str | None
    basis: CanonicalGrantBasis


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


_INSPECTION_SEAL = object()


@dataclass(frozen=True, slots=True)
class _CanonicalInspectionEvidence:
    """Producer provenance with an opaque handle identity, never a resource."""

    root: Path
    handle: object
    store_identity: tuple[tuple[str, str], ...]
    principal: RequestPrincipal
    purpose: str | None
    manifest: CanonicalSubject
    contributors: tuple[CanonicalSubject, ...]
    snapshot: str
    references: tuple[tuple[tuple[str | int, ...], str], ...]
    payload_hash: str
    _seal: object

    def __reduce__(self) -> object:
        raise TypeError("canonical inspection evidence is process-local")


class _CanonicalInspectionProjection(dict[str, Any]):
    """JSON-compatible inspection with private canonical metadata provenance."""

    def __init__(self, value, evidence: _CanonicalInspectionEvidence) -> None:
        super().__init__(value)
        self._canonical_inspection_evidence = evidence

    def __reduce__(self) -> object:
        raise TypeError("canonical inspection projection is process-local")

    def __copy__(self):
        return dict(self)

    def __deepcopy__(self, memo):
        # Ordinary consumers may copy the public data, never its provenance.
        from copy import deepcopy

        return deepcopy(dict(self), memo)


def _inspection_evidence(value: Any) -> _CanonicalInspectionEvidence | None:
    if type(value) is not _CanonicalInspectionProjection:
        return None
    evidence = getattr(value, "_canonical_inspection_evidence", None)
    if type(evidence) is not _CanonicalInspectionEvidence or evidence._seal is not _INSPECTION_SEAL:
        OperationAuthorization.refuse()
    return evidence


def _inspection_reference(value, location):
    try:
        for key in location:
            value = value[key]
        return value
    except (KeyError, IndexError, TypeError):
        return None


def _seal_inspection_projection(value, evidence):
    """Only the producer and trusted terminal transformations call this."""
    references = tuple((location, path) for location, path in evidence.references
                       if _inspection_reference(value, location) == path)
    evidence = replace(evidence, references=references,
                       payload_hash=hashlib.sha256(_json(value).encode()).hexdigest())
    return _CanonicalInspectionProjection(value, evidence)


def subjects(conn: sqlite3.Connection, cid: str, logical_vault_id: str, *, identity=None,
             view_path=None) -> tuple[CanonicalSubject, ...]:
    return tuple(iter_subjects(conn, cid, logical_vault_id, identity=identity, view_path=view_path))


def iter_subjects(conn: sqlite3.Connection, cid: str, logical_vault_id: str, *, identity=None,
                  view_path=None, include_held=True, batch_size=128,
                  query_order=False) -> Iterator[CanonicalSubject]:
    """Stream canonical metadata, yielding the manifest before opening row cursors."""
    if type(batch_size) is not int or not 1 <= batch_size <= 128:
        raise ValueError("canonical subject batches must contain 1 to 128 rows")
    if identity is not None and view_path is not None:
        raise ValueError("canonical subject lookup must have one selector")
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
    # Summary rows have no view file: they take the manifest's subject, and stay
    # owner-only until field release governs them (S1.5b).
    summary = view_mode == "summary"
    audience = OWNER_ONLY if summary else declared.default_audience

    def make(ref, row_id, policy_path, item_type, raw_metadata, row_version, payload, domain):
        meta = _metadata(raw_metadata)
        subject = membership.MetadataSubject(
            policy_path, (ref,), tuple(meta["projects"]), tuple(meta["tags"]),
            (item_type.lower(),), tuple(meta["classes"]),
        )
        basis = CanonicalGrantBasis(logical_vault_id, store_identity[0], ref, row_version, payload,
                                    subject, audience, version, content_hash,
                                    name, type_version, declaration_hash, domain)
        return CanonicalSubject(cid, row_id, basis)

    if identity in (None, path):
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
    if not include_held:
        return
    with closing(conn.execute(
        "SELECT held_id,view_path,governance_json,governance_hash FROM held_candidates "
        "WHERE collection_id=?" + held_filter + " ORDER BY held_id",
        (cid, *held_args),
    )) as cursor:
        while batch := cursor.fetchmany(batch_size):
            for held_id, path, raw_metadata, payload in batch:
                yield make(f"exomem://collection-held/{cid}/{held_id}", held_id, path,
                           "held-record", raw_metadata, 1, payload, "held")


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
        from .inspection import InspectionCache

        self.conn = conn
        self.inspections = InspectionCache()
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
        self.inspections.clear()
        self.states.clear()
        self.contracts.clear()
        self.memberships.clear()
        self.decisions.clear()
        self.replacements.clear()
        self.observed = self._stamp()

    def begin(self):
        self.check()
        self.pending = {}
        self.unmanaged = False
        self.replacements = {}
        self.accounted = self.conn.total_changes
        self.prepared = {}

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
        elif changed or self.unmanaged:
            self.inspections.clear()
            self.states.clear()
            self.contracts.clear()
            self.memberships.clear()
            self.decisions.clear()
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

    def __init__(self, root: Path, conn: sqlite3.Connection, *, mutation: bool, cache=None) -> None:
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

    def _decision_key(self, subject: CanonicalSubject):
        return (_bound(subject, "unbound").basis, self.policy.fingerprint,
                self.who.audience_id, self.purpose)

    def decision(self, subject: CanonicalSubject, *, session=True) -> Decision:
        basis = subject.basis
        path = basis.subject.path
        if self.failed or self.policy.blocked or (not self.who.resolved and not self.policy.empty) or self.access_blocked:
            return Decision(0)
        if basis.default_audience == OWNER_ONLY and not (self.who.resolved
                                                         and self.who.audience_id == OWNER_AUDIENCE):
            return Decision(0)
        if self.tombstones and (lifecycle.is_tombstoned_in(self.tombstones, path)
                               or lifecycle.is_tombstoned_in(self.tombstones, basis.identity)):
            return Decision(0)
        if self.access["excluded"] or self.mutation:
            relative = access._kb_relative(path)
            if access._matches(self.access["excluded"], relative):
                return Decision(0)
            if self.mutation and (access._matches(self.access["readonly"], relative)
                                  or relative.split("/", 1)[0].casefold() in {value.casefold() for value in access._APPEND_ONLY}):
                return Decision(0)
        key = self._decision_key(subject)
        active = self.grants.get(basis.identity, ()) if session else ()
        if not active and key in self.cache.decisions:
            return self.cache.decisions[key]
        scope_ids = self.cache.scopes(subject, self.policy)
        grants = list(self.policy.grants)
        if active:
            basis = _bound(subject, self.logical_vault_id).basis
        for grant in active:
            if authority.SessionMembership(basis.identity, basis.fingerprint, scope_ids) in grant.membership:
                grants.append(policy.StandingGrant(grant.grant_id, "authorization-session", grant.scope_ids,
                                                   grant.audience, grant.ceiling))
        effective = self.policy
        if basis.default_audience in {"owner", OWNER_ONLY}:
            scopes = {key: replace(scope, default_deny=True) for key, scope in effective.scopes.items()}
            if not scope_ids:
                default = f"collection-type-default:{basis.type_name}:{basis.type_version}"
                scopes[default] = policy.Scope(id=default, source="collection-type", default_deny=True)
                scope_ids = (default,)
            effective = replace(effective, scopes=scopes)
        decision = decide(scope_ids, audience=self.who.audience_id, purpose=self.purpose,
                          policy=effective, active_grants=grants)
        if not active:
            self.cache._remember(self.cache.decisions, key, decision, 65536)
        return decision

    def _release_dependency(self):
        return (self.policy.fingerprint, self.who.audience_id, self.who.resolved, self.purpose,
                self.access_fingerprint, self.access_blocked, tuple(sorted(self.tombstones)), self.mutation)

    def uniform_release(self) -> bool:
        """No row-varying policy, grant, exclusion, tombstone or session: one decision per path and audience."""
        return (self.policy.empty and not self.policy.scopes and not self.policy.rules
                and not self.policy.grants and not self.tombstones and not self.access["excluded"]
                and not (self.mutation and self.access["readonly"])
                and self.context is None and not self.failed)

    def summary_release(self, cid: str) -> SummaryRelease | None:
        """Decide a summary collection without a row-subject state; None for an items collection.

        Summary rows share the manifest's path, projects and owner-only
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
        rows = self.conn.execute("SELECT COUNT(*) FROM items WHERE collection_id=?", (cid,)).fetchone()[0]
        container = tokens.container_hash(cid, generation, audit_head)
        visible = hashlib.sha256(b"exomem.collection-summary-visible.v1\0" + manifest.basis.fingerprint.encode())
        prefix = f"exomem://{item_type}/{cid}/"
        varies = (self._summary_rows_vary(cid, prefix)
                  or any(identity.startswith(prefix) for identity in self.grants)
                  or any(cid in tombstone.casefold() for tombstone in self.tombstones))
        if not varies:
            released = 0
            if rows:
                key = self.conn.execute("SELECT item_key FROM items WHERE collection_id=? ORDER BY row_id LIMIT 1",
                                        (cid,)).fetchone()[0]
                sample = next(iter(subjects(self.conn, cid, self.logical_vault_id, identity=prefix + key)))
                released = rows if self.decision(sample).level >= 6 else 0
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
            visible.update(container.encode())
        release = SummaryRelease(manifest, decision, prefix, rows, released, tuple(held), "")
        release = replace(release, snapshot=container if release.complete else visible.hexdigest())
        self.summary_memo = {memo: release}
        return release

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

    def require_collection(self, cid: str, *, complete: bool = True, refresh=False) -> tuple[CanonicalSubject, ...]:
        try:
            head = self.summary_manifest(cid)
            if head is not None:
                if head[1].level < 6 or (complete and not self.summary_release(cid).complete):
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
                        decision = self.cache.decisions.get(self._decision_key(subject))
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
        from . import views

        locations = [("contract", "path"), ("contract", "storage", "source")]
        locations.extend(("source_versions", index, "path")
                         for index in range(len(payload["source_versions"])))
        references = tuple((location, value) for location in locations
                           if isinstance(value := _inspection_reference(payload, location), str))
        return _CanonicalInspectionEvidence(
            self.root.resolve(), handle._inspection_identity, tuple(sorted(views.store_identity(self.conn).items())),
            self.who, self.purpose, selection.manifest, selection.contributors,
            selection.snapshot, references, "", _INSPECTION_SEAL,
        )

    def validate_inspection_projection(self, payload, handle):
        """Fresh canonical admission; physical views cannot change this metadata."""
        from . import views

        evidence = _inspection_evidence(payload)
        if evidence is None:
            return None
        try:
            if (evidence.root != self.root.resolve() or evidence.handle is not handle._inspection_identity
                    or evidence.principal != self.who or evidence.purpose != self.purpose
                    or evidence.store_identity != tuple(sorted(views.store_identity(self.conn).items()))
                    or evidence.payload_hash != hashlib.sha256(_json(payload).encode()).hexdigest()):
                self.refuse()
            cid = evidence.manifest.collection_id
            selection = self.inspection_selection(cid)
            contributors_match = (
                selection.contributors == evidence.contributors
                or frozenset(selection.contributors) == frozenset(evidence.contributors)
            )
            if (selection.manifest != evidence.manifest or not contributors_match
                    or selection.snapshot != evidence.snapshot):
                self.refuse()
            return evidence
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            self.refuse()

    def visible_snapshot(self, cid: str, allowed: tuple[CanonicalSubject, ...]) -> str:
        state = self.cache.state(cid, full=True)
        manifest = _bound(state.subjects[state.epoch[0]], self.logical_vault_id)
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

    def projection_subjects(self, path: str) -> tuple[CanonicalSubject, ...] | None:
        """None is an ordinary file; an empty tuple is an unbound owned path."""
        from . import authority as collection_authority

        projection = self.conn.execute(
            "SELECT collection_id,row_id,kind FROM projection_state WHERE path=?", (path,),
        ).fetchone()
        owners = {row[0] for row in self.conn.execute(
            "SELECT collection_id,manifest_path,source_path FROM collections "
            "WHERE manifest_path=? OR source_path=? OR collection_id IN "
            "(SELECT collection_id FROM items WHERE view_path=? UNION ALL "
            "SELECT collection_id FROM held_candidates WHERE view_path=? UNION ALL "
            "SELECT collection_id FROM projection_state WHERE path=?)",
            (path, path, path, path, path),
        )}
        if projection is not None:
            owners.add(projection[0])
        for cid, manifest_path, source_path, layout in self.conn.execute(
            "SELECT collection_id,manifest_path,source_path,layout FROM collections",
        ):
            directory = Path(manifest_path).parent
            roots = ((directory / records._HELD_DIRECTORY).as_posix(),
                     (directory / "_history").as_posix())
            log_item = False
            if layout == "markdown-log" and path.startswith(source_path + "#"):
                try:
                    records._validate_item_key(path[len(source_path) + 1:])
                    log_item = True
                except collections.CollectionError:
                    pass
            if (any(path == root or path.startswith(root + "/") for root in roots)
                    or path == (directory / "_history.md").as_posix()
                    or (layout == "markdown-items" and
                        (path == source_path or path.startswith(source_path + "/")))
                    or log_item):
                owners.add(cid)
        raw = collection_authority.read_marker(self.root)
        if raw is not None:
            marker = collection_authority.parse_marker(self.root, raw)
            entries = {cid: collection_authority.selected_entry(self.root, marker, cid) for cid in owners}
            intent = collection_authority.pending_create(self.conn)
            if intent is not None and intent["collection_id"] in owners and entries[intent["collection_id"]] is None:
                return ()
            owners = {cid for cid, entry in entries.items() if entry is not None}
            for cid in owners:
                collection_authority.require_selected(self.conn, marker, entries[cid])
            entry = collection_authority.selected_entry(self.root, marker, path)
            if entry is not None and owners != {entry["collection_id"]}:
                return ()
            if projection is not None and projection[0] not in owners:
                projection = None
        if not owners:
            return None
        if projection is not None and projection[2] not in {"manifest", "item", "held", "log"}:
            return ()
        if len(owners) != 1:
            return ()
        cid = next(iter(owners))
        manifest_path, source_path, layout = self.conn.execute(
            "SELECT manifest_path,source_path,layout FROM collections WHERE collection_id=?", (cid,),
        ).fetchone()
        self._load_grants()
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
            decisions = tuple(self.decision(subject) for subject in targets)
            if path != targets[0].basis.identity:
                if decisions[0].level < 6:
                    return Decision(0)
                decisions = decisions[1:]
            decision = egress._meet_decisions(decisions)
            if decision.level == 0:
                return decision
            if content is not None:
                from . import views

                cursor = self.conn.execute("SELECT * FROM projection_state WHERE path=?", (path,))
                row = cursor.fetchone()
                if row is None:
                    return Decision(0)
                projection = dict(zip((column[0] for column in cursor.description), row, strict=True))
                if projection["kind"] not in {"manifest", "item", "held"}:
                    return Decision(0)
                if projection["kind"] == "item" and not any(
                    subject.row_id == projection["row_id"] for subject in targets[1:]
                ):
                    return Decision(0)
                manifest = manifest_for(projection["collection_id"])
                _version, rendered = views.render_view(
                    self.conn, views.store_identity(self.conn), projection, manifest,
                )
                if content != rendered.encode("utf-8"):
                    return Decision(0)
                parsed = find_corpus.parse_page(self.root / path, 0, self.root, content=content)
                if parsed is None:
                    return Decision(0)
                extra = set(membership.evaluate_snapshot(
                    parsed, self.policy, content_hash=hashlib.sha256(content).hexdigest(),
                )) - set(decision.scope_ids)
                if extra:
                    decision = egress._meet_decisions((decision, decide(
                        extra, audience=self.who.audience_id, purpose=self.purpose,
                        policy=self.policy, active_grants=self.policy.grants,
                    )))
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
        self, path: str, content_sha256: str | Callable[[], str] | None = None
    ) -> bool:
        # A digest proved on demand belongs to one call, so that decision is not cached.
        key = (
            path
            if content_sha256 is None
            else (path, content_sha256) if isinstance(content_sha256, str) else None
        )
        if key in self.file_decisions:
            return self.file_decisions[key].level >= 6
        decision = Decision(0)
        try:
            if (self.failed or self.policy.blocked or self.access_blocked
                    or (not self.who.resolved and not self.policy.empty)
                    or lifecycle.is_tombstoned_in(self.tombstones, path)
                    or access._matches(self.access["excluded"], access._kb_relative(path))):
                return False
            scope_ids = membership.evaluate_path_only(self.root, path, self.policy).require_classified()
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
        from .typed_storage import hydrate

        hydrate(self.conn, rows)
        rows.sort(key=lambda row: (row["view_path"] or "", row["item_key"]))
        return rows, self.visible_snapshot(cid, allowed), {
            subject.row_id for subject in allowed if isinstance(subject.row_id, str)
        }


    def _summary_rows(self, cid: str, release: SummaryRelease) -> tuple[list[dict[str, Any]], str, set[str]]:
        """A whole summary snapshot only within the interactive visit and deadline bounds.

        Larger reads page through a collection query with its continuation;
        this never returns some of the rows as if they were all of them.
        """
        from ..query_engine.runtime import QueryLimits
        from .typed_storage import hydrate

        limits = QueryLimits()
        if release.released > limits.max_row_visits:
            raise collections.CollectionError(
                "QUERY_COST_LIMIT", "summary rows exceed one bounded read; page them with a collection query",
                {"released_rows": release.released, "max_row_visits": limits.max_row_visits})
        deadline = time.monotonic() + limits.timeout_ms / 1000
        rows = []
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
                    rows.extend(hydrate(self.conn, batch))
        rows.sort(key=lambda row: row["item_key"])
        held = {subject.row_id for subject, decision in release.held if decision.level >= 6}
        return rows, release.snapshot, held

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
