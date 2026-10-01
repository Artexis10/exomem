"""Canonical collection subjects and request-local authorization.

Only persisted identity metadata participates in a current-state decision.
Payloads and projected views are never an authorization source.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .. import access, find_corpus, records, structured_collections as collections, vault
from ..find_types import ParsedPage
from ..governance import authorization_session_authority as authority
from ..governance import authorization_session_lifecycle, egress, lifecycle, membership, policy, store
from ..governance.decisions import Decision, decide
from ..governance.principal import effective_principal
from . import types


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


def subjects(conn: sqlite3.Connection, cid: str, logical_vault_id: str) -> tuple[CanonicalSubject, ...]:
    row = conn.execute(
        "SELECT c.manifest_path,c.source_path,c.layout,c.manifest_version,m.manifest_hash,m.governance_json,"
        "c.type_name,c.type_version,t.declaration_hash,t.declaration_json,ct.builtin "
        "FROM collections c JOIN collection_manifests m "
        "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version "
        "JOIN collection_type_versions t ON t.name=c.type_name AND t.version=c.type_version "
        "JOIN collection_types ct ON ct.name=c.type_name WHERE c.collection_id=?", (cid,),
    ).fetchone()
    identity = conn.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()
    if row is None or identity is None or not identity[0]:
        raise ValueError("canonical subject is unavailable")
    path, source_path, layout, version, content_hash, metadata, name, type_version, declaration_hash, declaration, builtin = row
    declared = types.parse_declaration(json.loads(declaration), builtin=bool(builtin))
    if (declared.name, declared.version) != (name, type_version):
        raise ValueError("canonical type identity differs")
    manifest_meta = _metadata(metadata)

    def make(ref, row_id, policy_path, item_type, raw_metadata, row_version, payload, domain):
        meta = _metadata(raw_metadata)
        subject = membership.MetadataSubject(
            policy_path, (ref,), tuple(meta["projects"]), tuple(meta["tags"]),
            (item_type.lower(),), tuple(meta["classes"]),
        )
        basis = CanonicalGrantBasis(logical_vault_id, identity[0], ref, row_version, payload,
                                    subject, declared.default_audience, version, content_hash,
                                    name, type_version, declaration_hash, domain)
        return CanonicalSubject(cid, row_id, basis)

    result = [make(path, None, path, "collection", metadata, version, content_hash, "manifest")]
    for row_id, key, row_version, payload, view_path, raw_metadata in conn.execute(
        "SELECT row_id,item_key,row_version,payload_hash,view_path,governance_json "
        "FROM items WHERE collection_id=? ORDER BY row_id", (cid,),
    ):
        # Projects are inherited from the exact current manifest contract.
        meta = _metadata(raw_metadata)
        if meta["projects"] != manifest_meta["projects"]:
            raise ValueError("canonical row project differs from its manifest")
        policy_path = source_path if layout == "markdown-log" else view_path
        result.append(make(f"exomem://{declared.item_type}/{cid}/{key}", row_id, policy_path,
                           declared.item_type, raw_metadata, row_version, payload, "row"))
    for held_id, path, raw_metadata, payload in conn.execute(
        "SELECT held_id,view_path,governance_json,governance_hash FROM held_candidates "
        "WHERE collection_id=? ORDER BY held_id", (cid,),
    ):
        result.append(make(f"exomem://collection-held/{cid}/{held_id}", held_id, path,
                           "held-record", raw_metadata, 1, payload, "held"))
    return tuple(result)


class OperationAuthorization:
    """One policy/time/access/authority snapshot, with grants grouped by subject."""

    def __init__(self, root: Path, conn: sqlite3.Connection, *, mutation: bool) -> None:
        self.root, self.conn, self.mutation = root, conn, mutation
        self.policy = policy.load(root)
        self.who = effective_principal()
        self.now = int(time.time())
        self.tombstones = lifecycle.tombstoned_paths(root)
        _, self.access, self.access_blocked = access._policy_state(root)
        self.context = self.who.verified_authorization_session
        self.purpose = self.who.purpose
        self.authority = None
        self.grants = {}
        self.catalogs = {}
        self.file_decisions = {}
        self.loaded = set()
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
                self.catalogs[cid] = subjects(self.conn, cid, self.logical_vault_id)
            catalog = self.catalogs[cid]
            if cid not in self.loaded:
                self.loaded.add(cid)
                if self.authority is not None and not self.failed and not self.policy.empty and not self.policy.blocked:
                    for offset in range(0, len(catalog), 16384):
                        entries = tuple(self.membership(subject) for subject in catalog[offset:offset + 16384])
                        matched = authority.active_session_grants_for_projection_catalog(
                            self.authority, context=self.context, audience=self.who.audience_id,
                            purpose=self.purpose, catalog=entries,
                            policy_fingerprint=self.policy.fingerprint, now=self.now,
                        )
                        for identity, grant in matched:
                            self.grants.setdefault(identity, []).append(grant)
            return catalog
        except (ValueError, TypeError, sqlite3.Error,
                authorization_session_lifecycle.AuthorizationSessionUnavailable):
            self.refuse()

    def membership(self, subject: CanonicalSubject) -> authority.SessionMembership:
        return authority.SessionMembership(subject.basis.identity, subject.basis.fingerprint,
                                           tuple(sorted(membership.evaluate_metadata(subject.basis.subject, self.policy))))

    def decision(self, subject: CanonicalSubject) -> Decision:
        basis = subject.basis
        path = basis.subject.path
        if self.failed or self.policy.blocked or (not self.who.resolved and not self.policy.empty) or self.access_blocked:
            return Decision(0)
        if lifecycle.is_tombstoned_in(self.tombstones, path) or lifecycle.is_tombstoned_in(self.tombstones, basis.identity):
            return Decision(0)
        relative = access._kb_relative(path)
        if access._matches(self.access["excluded"], relative):
            return Decision(0)
        if self.mutation and (access._matches(self.access["readonly"], relative)
                              or relative.split("/", 1)[0].casefold() in {value.casefold() for value in access._APPEND_ONLY}):
            return Decision(0)
        current = self.membership(subject)
        grants = list(self.policy.grants)
        for grant in self.grants.get(basis.identity, ()):
            if current in grant.membership:
                grants.append(policy.StandingGrant(grant.grant_id, "authorization-session", grant.scope_ids,
                                                   grant.audience, grant.ceiling))
        effective = self.policy
        scope_ids = current.scope_ids
        if basis.default_audience == "owner":
            scopes = {key: replace(scope, default_deny=True) for key, scope in effective.scopes.items()}
            if not scope_ids:
                default = f"collection-type-default:{basis.type_name}:{basis.type_version}"
                scopes[default] = policy.Scope(id=default, source="collection-type", default_deny=True)
                scope_ids = (default,)
            effective = replace(effective, scopes=scopes)
        return decide(scope_ids, audience=self.who.audience_id, purpose=self.purpose,
                      policy=effective, active_grants=grants)

    def require_collection(self, cid: str, *, complete: bool = True, refresh=False) -> tuple[CanonicalSubject, ...]:
        catalog = self.catalog(cid, refresh=refresh)
        required = catalog if complete else catalog[:1]
        if any(self.decision(subject).level < 6 for subject in required):
            self.refuse()
        return catalog

    def allows_file(self, path: str) -> bool:
        """Gate ancillary reads without reopening policy or session authority."""
        if path in self.file_decisions:
            return self.file_decisions[path].level >= 6
        owners = self.conn.execute(
            "SELECT collection_id,manifest_path,source_path FROM collections "
            "WHERE manifest_path=? OR source_path=? OR collection_id IN "
            "(SELECT collection_id FROM items WHERE view_path=? UNION ALL "
            "SELECT collection_id FROM held_candidates WHERE view_path=?)",
            (path, path, path, path),
        ).fetchall()
        if not owners:
            for cid, manifest_path, source_path, layout in self.conn.execute(
                "SELECT collection_id,manifest_path,source_path,layout FROM collections",
            ):
                held_directory = (Path(manifest_path).parent / records._HELD_DIRECTORY).as_posix()
                reserved = path.startswith(held_directory + "/") or (
                    layout == "markdown-items" and path.startswith(source_path + "/")
                )
                if layout == "markdown-log" and path.startswith(source_path + "#"):
                    try:
                        records._validate_item_key(path[len(source_path) + 1:])
                        reserved = True
                    except collections.CollectionError:
                        pass
                if reserved:
                    owners.append((cid, manifest_path, source_path))
        if owners:
            if len(owners) != 1:
                return False
            cid, manifest_path, source_path = owners[0]
            catalog = self.catalog(cid)
            if path == manifest_path:
                targets = catalog[:1]
            elif path == source_path:
                targets = tuple(subject for subject in catalog if subject.row_id is None
                                or isinstance(subject.row_id, int))
            else:
                row_ids = {row[0] for row in self.conn.execute(
                    "SELECT row_id FROM items WHERE collection_id=? AND view_path=? UNION ALL "
                    "SELECT held_id FROM held_candidates WHERE collection_id=? AND view_path=?",
                    (cid, path, cid, path),
                )}
                if not row_ids:
                    return False
                targets = tuple(subject for subject in catalog if subject.row_id is None
                                or subject.row_id in row_ids)
            return all(self.decision(subject).level >= 6 for subject in targets)
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
                page, relative = vault.resolve_under_vault(self.root, path, must_exist=True, must_be_file=True)
                if relative != path:
                    return False
                fingerprint = hashlib.sha256(vault.read_bytes_without_pinning(page)).hexdigest()
                current = authority.SessionMembership(path, fingerprint, tuple(sorted(scope_ids)))
                matched = authority.active_session_grants_for_projection_catalog(
                    self.authority, context=self.context, audience=self.who.audience_id,
                    purpose=self.purpose, catalog=(current,), policy_fingerprint=self.policy.fingerprint,
                    now=self.now,
                )
                grants.extend(policy.StandingGrant(grant.grant_id, "authorization-session", grant.scope_ids,
                                                  grant.audience, grant.ceiling)
                              for identity, grant in matched if identity == path and current in grant.membership)
            decision = decide(scope_ids, audience=self.who.audience_id, purpose=self.purpose,
                              policy=self.policy, active_grants=grants)
        except (membership.MembershipUnresolved, vault.VaultPathError, OSError, ValueError,
                sqlite3.Error, authorization_session_lifecycle.AuthorizationSessionUnavailable):
            pass
        finally:
            self.file_decisions[path] = decision
        return decision.level >= 6

    @staticmethod
    def refuse() -> None:
        raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")

    def authorized_rows(self, cid: str) -> tuple[list[dict[str, Any]], str, set[str]]:
        """Decode only released rows; the manifest is an independent L6 gate."""
        catalog = self.require_collection(cid, complete=False)
        allowed = [subject for subject in catalog[1:] if self.decision(subject).level >= 6]
        rows = []
        row_ids = [subject.row_id for subject in allowed if isinstance(subject.row_id, int)]
        for offset in range(0, len(row_ids), 512):
            batch = row_ids[offset:offset + 512]
            cursor = self.conn.execute(f"SELECT * FROM items WHERE row_id IN ({','.join('?' for _ in batch)})", batch)
            names = [column[0] for column in cursor.description]
            rows.extend(dict(zip(names, row, strict=True)) for row in cursor)
        rows.sort(key=lambda row: row["view_path"])
        snapshot = hashlib.sha256(b"exomem.collection-visible.v1\0" + _json([
            catalog[0].basis.fingerprint,
            [(subject.row_id, subject.basis.version) for subject in allowed if isinstance(subject.row_id, int)],
        ]).encode()).hexdigest()
        if self.policy.empty and len(allowed) == len(catalog) - 1:
            from . import tokens

            generation, audit_head = self.conn.execute(
                "SELECT generation,audit_head FROM collections WHERE collection_id=?", (cid,),
            ).fetchone()
            snapshot = tokens.container_hash(cid, generation, audit_head)
        return rows, snapshot, {subject.row_id for subject in allowed if isinstance(subject.row_id, str)}


def resolve_bound_membership(root: Path, identity: str, candidate: policy.Policy,
                             logical_vault_id: str) -> authority.SessionMembership | None:
    """Resolve dark-store grant subjects through the explicit trusted binding."""
    from .preview import bound_writer

    writer = bound_writer(root)
    if writer is not None:
        for (cid,) in writer.connection.execute("SELECT collection_id FROM collections"):
            for subject in subjects(writer.connection, cid, logical_vault_id):
                if subject.basis.identity == identity:
                    return authority.SessionMembership(identity, subject.basis.fingerprint,
                        tuple(sorted(membership.evaluate_metadata(subject.basis.subject, candidate))))
    if identity.startswith("exomem://"):
        raise authorization_session_lifecycle.AuthorizationSessionUnavailable
    return None
