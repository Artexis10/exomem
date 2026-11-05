"""Closed owner proofs for reuse of canonical file-source terms."""

from __future__ import annotations

import functools
import hashlib
import time
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path

from .. import find_corpus, memory_refs, runtime_resources, vault
from . import artifact_census


@dataclass(frozen=True, slots=True)
class SourceReleaseBasis:
    """Effective authority facts; no principal, session object or bearer."""

    policy: str
    audience: str
    purpose: str | None
    connector: tuple
    ownership: tuple
    grants: tuple
    revisions: tuple
    tombstones: frozenset[str]


def current_source_release_basis(root: Path, *, purpose: str | None = None) -> SourceReleaseBasis | None:
    """Verify incoming authority, normalizing conversations by their effective grants."""
    from .. import semantic_index
    from ..collection_store import ownership
    from . import egress as owner

    who = owner.effective_principal().with_purpose(purpose)
    policy = owner.policy_module.load(root)
    if not who.resolved or policy.blocked or owner.bound_writer(root) is not None:
        return None
    if who.authorization_session_id is not None and who.verified_authorization_session is None:
        return None
    connection = None
    try:
        declared = owner._declared_purpose(root, who, purpose)
        grants = ()
        context = who.verified_authorization_session
        if context is not None:
            if who.issuer_family != context.issuer_family:
                return None
            now = int(time.time())
            connection = owner.store.open_authorization_session_connection(root)
            custody = owner.authorization_custody.load_authorization_custody(root, now=now)
            owner.authorization_session_lifecycle.status_verified_session(
                connection, custody=custody, context=context, now=now,
            )

            def resolve(path):
                with runtime_resources.semantic_preparation(root, root / path) as prepared:
                    raw = prepared.source.encode("utf-8")
                    digest = hashlib.sha256(raw).hexdigest()
                    page = find_corpus.parse_page(root / path, 0, root, content=raw)
                    if page is None:
                        return None
                    scopes = owner.membership_module.evaluate_snapshot(page, policy, content_hash=digest)
                    prepared.guard.recheck(root)
                    return owner.authorization_session_authority.SessionMembership(path, digest, tuple(sorted(scopes)))

            active = owner.authorization_session_authority.active_session_grants_for_projection_resolver(
                connection, context=context, audience=who.audience_id, purpose=declared,
                resolve=resolve, policy_fingerprint=policy.fingerprint, now=now,
            )
            grants = tuple(sorted({(path, tuple(sorted(grant.scope_ids)), grant.ceiling,
                                    tuple(sorted(zip(grant.paths, grant.fingerprints, strict=True))))
                                   for path, grant in active}))
        return SourceReleaseBasis(
            policy.fingerprint, who.audience_id, declared,
            owner.connector_boundary.admission_identity(root, who, policy), ownership.current(root).binding,
            grants, (semantic_index.PARSER_VERSION, owner._project_page_origin, owner.filter_withheld_entries,
                     owner.scrubber.scrub_value), owner.lifecycle.tombstoned_paths(root),
        )
    except Exception:  # noqa: BLE001 - unknown owner state declines reuse, leaving canonical preparation authoritative
        return None
    finally:
        if connection is not None:
            connection.close()


@dataclass(frozen=True, slots=True)
class SourceReleaseProof:
    """Only supported file decisions, name resolutions and owner fences."""

    basis: SourceReleaseBasis
    source: str | None
    paths: tuple[vault.PathGuard, ...]
    decisions: tuple[tuple[str, int | None], ...]
    names: tuple[tuple[str, tuple[str, ...]], ...]
    directories: tuple[tuple[str, str, str | None], ...]
    registries: tuple[tuple, ...]
    namespace: artifact_census.NamespaceProof | None
    references: tuple[tuple[str, tuple[str, ...]], ...]
    pending: tuple | None


@dataclass(slots=True)
class SourceReleaseCapture:
    root: Path
    basis: SourceReleaseBasis | None
    source: str | None = None
    supported: bool = True
    paths: dict = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)
    names: dict = field(default_factory=dict)
    directories: dict = field(default_factory=dict)
    registries: dict = field(default_factory=dict)
    namespace: artifact_census.NamespaceProof | None = None
    references: dict = field(default_factory=dict)
    pending: tuple | None = None
    memo: dict = field(default_factory=dict, repr=False)

    def finish(self) -> SourceReleaseProof | None:
        if not self.supported or self.basis is None:
            return None
        if self.source is not None and (self.source not in self.paths or self.source not in self.decisions):
            return None
        proof = SourceReleaseProof(
            self.basis, self.source, tuple(self.paths.values()), tuple(sorted(self.decisions.items())),
            tuple(sorted(self.names.items())), tuple(sorted(self.directories.values())),
            tuple(sorted(self.registries.values())), self.namespace,
            tuple(sorted(self.references.items())), self.pending,
        )
        return proof if validate_source_release_proofs(self.root, self.basis, (proof,), memo=self.memo)[0] else None


_CAPTURE: ContextVar[SourceReleaseCapture | None] = ContextVar("source_release", default=None)


@contextmanager
def source_release_proof(root: Path, basis: SourceReleaseBasis | None, *, source_ref: str | None = None, memo=None):
    """Observe canonical preparation; the observer never changes release decisions."""
    capture = SourceReleaseCapture(root, basis, source=source_ref)
    if memo is not None:
        capture.memo = memo
    token = _CAPTURE.set(capture)
    try:
        yield capture
    finally:
        _CAPTURE.reset(token)


def unsupported() -> None:
    capture = _CAPTURE.get()
    if capture is not None:
        capture.supported = False


def path_dependency(root: Path, path: str, *, guard=None) -> None:
    capture = _CAPTURE.get()
    if capture is None or capture.root != root or path in capture.paths:
        return
    from ..collection_store import ownership
    from . import egress

    if egress.raw_protection.marked(path) or not egress._is_markdown_path(path) or ownership.relevant(path):
        unsupported()
        return
    try:
        if guard is None:
            try:
                info = (root / path).stat()
            except FileNotFoundError:
                guard = vault.PathGuard.capture(root, path, leaf_policy="absent")
            else:
                if (vault.STAT_GENERATION_TRUSTED and max(info.st_mtime_ns, info.st_ctime_ns)
                        < time.time_ns() - vault.GENERATION_RACY_WINDOW_NS):
                    guard = vault.PathGuard.capture(root, path, leaf_policy="generation",
                                                    expected_generation=vault.stat_generation(info))
                else:
                    if info.st_size > runtime_resources.SEMANTIC_SOURCE_MAX_BYTES:
                        unsupported()
                        return
                    # The maintained guard hashes in 64 KiB chunks; retaining another target body would double read memory.
                    with runtime_resources.parser_preparation(0) as preparation:
                        preparation.reserve(65536)
                        guard = vault.PathGuard.capture(root, path, leaf_policy="stable")
                        digest = vault._leaf_hash(root / path, guard.leaf_identity)
                        guard = replace(guard, leaf_policy="content", expected_content_hash=digest)
                        guard.recheck(root)
        capture.paths[path] = guard
    except (OSError, ValueError, runtime_resources.PreparationBudgetExceeded, runtime_resources.PreparationCapacityBusy):
        unsupported()


def directory_dependency(root: Path, directory: Path, name: str, resolved: str | None) -> None:
    capture = _CAPTURE.get()
    if capture is not None and capture.root == root:
        try:
            relative = directory.relative_to(root).as_posix()
            capture.directories[relative, name] = relative, name, resolved
        except (ValueError, OSError):
            unsupported()


def registry_dependency(root: Path, definitions) -> None:
    capture = _CAPTURE.get()
    if capture is None or capture.root != root:
        return
    if definitions is None:
        unsupported()
        return
    for subject, snapshot in definitions.snapshots.items():
        key = subject, definitions.instance_id
        capture.registries[key] = (*key, definitions.binding_revision, snapshot.content_hash, snapshot.effective_digest)


def names_dependency(root: Path, names, resolved) -> None:
    capture = _CAPTURE.get()
    if capture is not None and capture.root == root:
        from ..working_set_index import normalize

        capture.names.update((normalize(name), resolved.get(normalize(name), ())) for name in names)


def namespace_dependency(root: Path) -> None:
    capture = _CAPTURE.get()
    if capture is not None and capture.root == root:
        proof = capture.memo.get("namespace")
        if proof is None:
            proof = artifact_census.current_namespace_proof(root)
            if proof is not None:
                capture.memo["namespace"] = proof
        if proof is None:
            unsupported()
        else:
            capture.namespace = proof


def _pending_identity(root: Path) -> tuple | None:
    projection = memory_refs._pending_reference_projection(root)
    if projection is None:
        return None
    return (tuple(sorted(projection.paths_by_id.items())), projection.absent_paths,
            tuple(sorted(projection.refs_by_path.items())), projection.blocked_paths)


def references_dependency(root: Path, ids: Iterable[str]) -> None:
    capture = _CAPTURE.get()
    ids = tuple(ids)
    if capture is None or capture.root != root or not ids:
        return
    from .. import semantic_contract

    try:
        snapshot = semantic_contract.current_reference_identity_snapshot(root)
        if snapshot is None:
            unsupported()
            return
        capture.references.update((identity, snapshot.reference_owners.get(identity, ())) for identity in ids)
        capture.pending = _pending_identity(root)
    except Exception:  # noqa: BLE001 - a missing proof cannot add a refusal to the canonical read
        unsupported()


def validate_source_release_proofs(
    root: Path, basis: SourceReleaseBasis, proofs: Iterable[SourceReleaseProof], *, memo: dict | None = None,
    check_paths: bool = True,
) -> tuple[bool, ...]:
    """Validate owner facts under the incoming request's freshly verified authority."""
    from .. import semantic_contract, working_set_index
    from ..vocabulary import instances, registry, registry_spec
    from .egress import _physical_directory_match

    memo = memo if memo is not None else {}
    results = []
    for proof in proofs:
        valid = type(proof) is SourceReleaseProof and proof.basis == basis
        try:
            if valid and proof.namespace is not None:
                if "namespace" not in memo:
                    memo["namespace"] = artifact_census.current_namespace_proof(root)
                valid = memo["namespace"] == proof.namespace
            for guard in proof.paths if valid and check_paths else ():
                key = "guard", guard
                if key not in memo:
                    try:
                        guard.recheck(root, memo.setdefault("ancestors", set()))
                        memo[key] = True
                    except (ValueError, OSError):
                        memo[key] = False
                valid = valid and memo[key]
            for relative, name, resolved in proof.directories if valid else ():
                key = "directory", relative
                if key not in memo:
                    import os

                    with os.scandir(root / relative) as scanned:
                        memo[key] = dict.fromkeys(entry.name for entry in scanned)
                valid = valid and _physical_directory_match(memo[key], name) == resolved
            for subject, scope, binding, content, digest in proof.registries if valid else ():
                key = "registry", subject, scope
                if key not in memo:
                    spec = instances.select(root, registry_spec(subject), scope)
                    current = registry.load(spec, root)
                    memo[key] = spec.binding_revision, current.content_hash, current.effective_digest
                valid = valid and memo[key] == (binding, content, digest)
            if valid and proof.names:
                names = tuple(name for name, _paths in proof.names)
                key = "names", names
                if key not in memo:
                    memo[key] = working_set_index.WorkingSetIndex(root).resolve_names(names)
                valid = all(memo[key].get(name, ()) == paths for name, paths in proof.names)
            if valid and proof.references:
                if "references" not in memo:
                    memo["references"] = semantic_contract.current_reference_identity_snapshot(root)
                    memo["pending"] = _pending_identity(root)
                snapshot = memo["references"]
                valid = (snapshot is not None and memo["pending"] == proof.pending
                         and all(snapshot.reference_owners.get(identity, ()) == paths for identity, paths in proof.references))
        except Exception:  # noqa: BLE001 - unsupported or unknown owner proof requires canonical preparation
            valid = False
        results.append(valid)
    return tuple(results)


def capture_decision(function):
    @functools.wraps(function)
    def captured(root, path, *args, **kwargs):
        path_dependency(root, path)
        result = function(root, path, *args, **kwargs)
        capture = _CAPTURE.get()
        if capture is not None:
            from .egress import Decision, _decision_is_page_local

            capture.decisions[path] = result.level if isinstance(result, Decision) else result
            if isinstance(result, Decision) and not _decision_is_page_local(result):
                unsupported()
            if result is None and (path not in capture.paths or capture.paths[path].leaf_policy != "absent"):
                unsupported()
        return result
    return captured
