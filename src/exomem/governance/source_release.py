"""Closed owner proofs for reuse of canonical file-source terms."""

from __future__ import annotations

import functools
import hashlib
import os
import sys
import time
from collections.abc import Callable, Iterable
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
    observed_hashes: dict = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)
    names: dict = field(default_factory=dict)
    directories: dict = field(default_factory=dict)
    registries: dict = field(default_factory=dict)
    namespace: artifact_census.NamespaceProof | None = None
    references: dict = field(default_factory=dict)
    pending: tuple | None = None
    memo: dict = field(default_factory=dict, repr=False)
    preparation_owner: runtime_resources.SemanticPreparation | None = field(default=None, repr=False)
    memory: Callable[[SourceReleaseCapture, tuple, int], None] | None = field(default=None, repr=False)

    def admit(self, *values, extra=0):
        if self.memory is not None:
            # These maps are the closed capture schema, not customer vocabulary.
            overlap = 3 * sum(sys.getsizeof(value) for value in (
                self.paths, self.observed_hashes, self.decisions, self.names, self.directories, self.registries, self.references,
            ))
            self.memory(self, values, extra + overlap)

    def finish(self) -> SourceReleaseProof | None:
        if not self.supported or self.basis is None:
            return None
        if self.source is not None and (self.source not in self.paths or self.source not in self.decisions):
            return None
        self.admit()
        proof = SourceReleaseProof(
            self.basis, self.source, tuple(self.paths.values()), tuple(sorted(self.decisions.items())),
            tuple(sorted(self.names.items())), tuple(sorted(self.directories.values())),
            tuple(sorted(self.registries.values())), self.namespace,
            tuple(sorted(self.references.items())), self.pending,
        )
        return proof if validate_source_release_proofs(
            self.root, self.basis, (proof,), memo=self.memo, preparation_owner=self.preparation_owner,
            memory=lambda extra=0: self.admit(proof, extra=extra),
        )[0] else None


_CAPTURE: ContextVar[SourceReleaseCapture | None] = ContextVar("source_release", default=None)


def capture_active(root: Path) -> bool:
    """Whether this request observes owner dependencies for this vault."""
    capture = _CAPTURE.get()
    return capture is not None and capture.root == root


@contextmanager
def source_release_proof(root: Path, basis: SourceReleaseBasis | None, *, source_ref: str | None = None, memo=None,
                         preparation_owner=None, memory=None):
    """Observe canonical preparation; the observer never changes release decisions."""
    capture = SourceReleaseCapture(root, basis, source=source_ref, preparation_owner=preparation_owner, memory=memory)
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
        capture.admit(extra=4 * len(path) * (path.count("/") + 1) + 256 * (path.count("/") + 1))
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
                    with runtime_resources.parser_preparation(0, owner=capture.preparation_owner) as preparation:
                        preparation.reserve(65536)
                        guard = vault.PathGuard.capture(root, path, leaf_policy="stable")
                        digest = vault._leaf_hash(root / path, guard.leaf_identity)
                        guard = replace(guard, leaf_policy="content", expected_content_hash=digest)
                        guard.recheck(root)
        capture.admit(guard)
        capture.paths[path] = guard
    except (OSError, ValueError, runtime_resources.PreparationBudgetExceeded, runtime_resources.PreparationCapacityBusy):
        unsupported()


def observed_content(root: Path, path: str, digest: str) -> None:
    """Bind a dependency to bytes actually read by its decision owner."""
    capture = _CAPTURE.get()
    if capture is None or capture.root != root:
        return
    path_dependency(root, path)
    capture.admit(digest)
    previous = capture.observed_hashes.setdefault(path, digest)
    guard = capture.paths.get(path)
    if previous != digest or guard is None or guard.leaf_policy == "absent":
        unsupported()
        return
    # A pre-read stat or hash cannot stand for a later read across an A -> B -> A write.
    capture.paths[path] = replace(guard, leaf_policy="content", expected_content_hash=digest)


def directory_dependency(root: Path, directory: Path, name: str, resolved: str | None) -> None:
    capture = _CAPTURE.get()
    if capture is not None and capture.root == root:
        try:
            relative = directory.relative_to(root).as_posix()
            capture.admit(relative, name, resolved)
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
        value = (*key, definitions.binding_revision, snapshot.content_hash, snapshot.effective_digest)
        capture.admit(key, value)
        capture.registries[key] = value


def names_dependency(root: Path, names, resolved) -> None:
    capture = _CAPTURE.get()
    if capture is not None and capture.root == root:
        from ..working_set_index import normalize

        capture.admit(resolved, extra=256 * len(names))
        capture.names.update((normalize(name), resolved.get(normalize(name), ())) for name in names)


def namespace_dependency(root: Path, proof: artifact_census.NamespaceProof | None) -> None:
    capture = _CAPTURE.get()
    if capture is not None and capture.root == root:
        capture.admit(proof)
        if proof is None or capture.namespace is not None and capture.namespace != proof:
            unsupported()
        else:
            capture.namespace = proof


def _pending_identity(projection) -> tuple:
    if projection is None:
        return ()
    return (tuple(sorted(projection.paths_by_id.items())), projection.absent_paths,
            tuple(sorted(projection.refs_by_path.items())), projection.blocked_paths)


def pending_references_dependency(root: Path, projection) -> None:
    capture = _CAPTURE.get()
    if capture is None or capture.root != root:
        return
    capture.admit(projection)
    observed = _pending_identity(projection)
    if (capture.pending is not None and capture.pending != observed
            or projection is not None and projection.blocked_paths):
        unsupported()
    capture.pending = observed


def references_dependency(root: Path, snapshot, resolved) -> None:
    capture = _CAPTURE.get()
    if capture is None or capture.root != root or not resolved:
        return
    if snapshot is None:
        unsupported()  # A scan answers canonically but supplies no reusable owner proof.
        return
    capture.admit(resolved)
    for identity, paths in resolved.items():
        previous = capture.references.setdefault(identity, paths)
        if previous != paths or snapshot.reference_owners.get(identity, ()) != paths:
            unsupported()


def _current_registry_tuple(root, subject, scope):
    """Read only the physical identities through their canonical selection and loader."""
    from ..vocabulary import instances, registry, registry_spec

    spec = current = None
    try:
        spec = instances.select(root, registry_spec(subject), scope)
        current = registry.load(spec, root)
        return spec.binding_revision, current.content_hash, current.effective_digest
    finally:
        spec = current = None


class _ValidationFacts:
    """Admitted physical observations for one phase; never guards, verdicts or caller state."""

    __slots__ = ("directories", "registries", "retained_bytes")

    def __init__(self):
        self.directories = {}
        self.registries = {}
        self.retained_bytes = (sys.getsizeof(self) + sys.getsizeof(self.directories)
                               + sys.getsizeof(self.registries) + sys.getsizeof(0) + sys.getsizeof(None))

    def clear(self):
        self.directories.clear()
        self.registries.clear()
        self.retained_bytes = (sys.getsizeof(self) + sys.getsizeof(self.directories)
                               + sys.getsizeof(self.registries) + sys.getsizeof(0) + sys.getsizeof(None))

    def registry_tuple(self, root, subject, scope, *, memory):
        key = current = None
        try:
            key = subject, scope
            if key not in self.registries:
                before = sys.getsizeof(self.registries)
                # The reader returns three SHA-256 identities or their absent markers.
                memory(extra=self.retained_bytes + 3 * before + 1024
                       + sys.getsizeof(subject) + sys.getsizeof(scope))
                current = _current_registry_tuple(root, subject, scope)
                retained = (sys.getsizeof(key) + sys.getsizeof(current)
                            + sum(sys.getsizeof(part) for part in (*key, *current) if part is not None))
                memory(extra=self.retained_bytes + retained + 3 * before + 256)
                self.registries[key] = current
                self.retained_bytes += retained + sys.getsizeof(self.registries) - before
                memory(extra=self.retained_bytes)
            return self.registries[key]
        finally:
            key = current = None

    def registries_current(self, root, *, memory):
        try:
            for (subject, scope), observed in self.registries.items():
                # The fresh fixed-width tuple overlaps all original observations until comparison ends.
                memory(extra=self.retained_bytes + 1024)
                if _current_registry_tuple(root, subject, scope) != observed:
                    return False
            return True
        except (TimeoutError, runtime_resources.PreparationBudgetExceeded,
                runtime_resources.PreparationCapacityBusy, MemoryError):
            raise
        except Exception:  # noqa: BLE001 - an unavailable owner withdraws this phase without replacing its facts
            return False
        finally:
            observed = subject = scope = None

    def match(self, root, relative, name, *, memory):
        from .egress import _physical_directory_match

        entry = names = folded = None
        try:
            entry = self.directories.get(relative)
            if entry is None:
                memory(extra=self.retained_bytes + 256)
                names = {}
                name_bytes = 0
                with os.scandir(root / relative) as scanned:
                    for child in scanned:
                        # scandir owns the bounded entry; admit its name before retaining it.
                        memory(extra=self.retained_bytes + 4 * sys.getsizeof(names) + name_bytes
                               + sys.getsizeof(child.name) + 256)
                        if child.name not in names:
                            names[child.name] = child.name
                            name_bytes += sys.getsizeof(child.name)
                before = sys.getsizeof(self.directories)
                memory(extra=self.retained_bytes + sys.getsizeof(names) + name_bytes + 3 * before
                       + sys.getsizeof(relative) + 256)
                entry = names, None
                self.directories[relative] = entry
                self.retained_bytes += (sys.getsizeof(self.directories) - before + sys.getsizeof(relative)
                                        + sys.getsizeof(entry) + sys.getsizeof(names) + name_bytes)
                memory(extra=self.retained_bytes)
            names, folded = entry
            if name not in names and folded is None:
                memory(extra=self.retained_bytes + 256)
                folded = {}
                key_bytes = 0
                for original in names:
                    folded_length = sum(len(scalar.casefold()) for scalar in original)
                    memory(extra=self.retained_bytes + 4 * sys.getsizeof(folded) + key_bytes
                           + 16 * len(original) + 4 * folded_length + 256)
                    key = original.casefold()
                    if key not in folded:
                        folded[key] = original
                        # An exact name can already own this same string object.
                        key_bytes += sys.getsizeof(key) if key is not names.get(key) else 0
                memory(extra=self.retained_bytes + sys.getsizeof(folded) + key_bytes + 256)
                self.directories[relative] = names, folded
                self.retained_bytes += sys.getsizeof(folded) + key_bytes
                memory(extra=self.retained_bytes)
            if name not in names:
                folded_length = sum(len(scalar.casefold()) for scalar in name)
                memory(extra=self.retained_bytes + 16 * len(name) + 4 * folded_length + 256)
            return _physical_directory_match(names, name, folded)
        finally:
            entry = names = folded = None


def validate_source_release_proofs(
    root: Path, basis: SourceReleaseBasis, proofs: Iterable[SourceReleaseProof], *, memo: dict | None = None,
    check_paths: bool = True, preparation_owner=None, memory=None,
    directory_facts: _ValidationFacts | None = None,
) -> tuple[bool, ...]:
    """Validate owner facts under the incoming request's freshly verified authority."""
    from .. import semantic_contract, working_set_index
    from .egress import _physical_directory_match

    memo = memo if memo is not None else {}
    results = []
    if memory is not None and directory_facts is None:
        memory(extra=512)
    for proof in proofs:
        if type(proof) is not SourceReleaseProof:
            results.append(False)
            continue
        local_facts = (sum(len(guard.ancestors) + 1 for guard in proof.paths)
                       + len(proof.directories) + len(proof.names) + len(proof.registries) + len(proof.references))
        def admit_phase(extra=0, local_bytes=512 + 256 * local_facts):
            amount = local_bytes + extra
            if memory is not None:
                memory(extra=amount)
            elif preparation_owner is not None:
                preparation_owner.reserve(preparation_owner.floor_bytes + 65536 + amount)

        if directory_facts is not None:
            admit_phase(extra=directory_facts.retained_bytes)
        elif memory is not None:
            memory(extra=256 * local_facts)
        elif preparation_owner is not None:
            preparation_owner.reserve(preparation_owner.floor_bytes + 65536 + 256 * local_facts)
        valid = type(proof) is SourceReleaseProof and proof.basis == basis
        try:
            if valid and proof.namespace is not None:
                if memo.get("namespace") is None:
                    memo["namespace"] = artifact_census.current_namespace_proof(root)
                valid = memo["namespace"] == proof.namespace
            for guard in proof.paths if valid and check_paths else ():
                key = "guard", guard
                if key not in memo:
                    try:
                        if guard.leaf_policy == "content":
                            size = guard.expected_content_size or 0
                            with runtime_resources.parser_preparation(size, owner=preparation_owner) as preparation:
                                preparation.reserve(8 * size + 65536)
                                guard.recheck(root, memo.setdefault("ancestors", set()))
                        else:
                            guard.recheck(root, memo.setdefault("ancestors", set()))
                        memo[key] = True
                    except TimeoutError:
                        raise
                    except (ValueError, OSError):
                        memo[key] = False
                valid = valid and memo[key]
            for relative, name, resolved in proof.directories if valid else ():
                if directory_facts is not None:
                    valid = valid and directory_facts.match(
                        root, relative, name, memory=admit_phase,
                    ) == resolved
                    continue
                key = "directory", relative
                if key not in memo:
                    names = {}
                    name_bytes = 0
                    with os.scandir(root / relative) as scanned:
                        for entry in scanned:
                            if preparation_owner is not None:
                                preparation_owner.reserve(preparation_owner.floor_bytes + 65536
                                                          + 3 * sys.getsizeof(names)
                                                          + name_bytes + sys.getsizeof(entry.name) + 256)
                            names[entry.name] = None
                            name_bytes += sys.getsizeof(entry.name)
                    memo[key] = names
                valid = valid and _physical_directory_match(memo[key], name) == resolved
            for subject, scope, binding, content, digest in proof.registries if valid else ():
                if directory_facts is not None:
                    current = directory_facts.registry_tuple(root, subject, scope, memory=admit_phase)
                    valid = valid and current == (binding, content, digest)
                    continue
                key = "registry", subject, scope
                if key not in memo:
                    memo[key] = _current_registry_tuple(root, subject, scope)
                valid = valid and memo[key] == (binding, content, digest)
            if valid and proof.names:
                names = tuple(name for name, _paths in proof.names)
                key = "names", names
                if key not in memo:
                    memo[key] = working_set_index.WorkingSetIndex(root).resolve_names(names)
                valid = all(memo[key].get(name, ()) == paths for name, paths in proof.names)
            if valid and proof.references:
                snapshot = None
                for identity, paths in proof.references:
                    key = "reference", identity
                    if key not in memo:
                        if snapshot is None:
                            snapshot = semantic_contract.current_reference_identity_snapshot(root)
                        if snapshot is None:
                            valid = False
                            break
                        memo[key] = snapshot.reference_owners.get(identity, ())
                    valid = valid and memo[key] == paths
                snapshot = None
            if valid and proof.pending is not None:
                if "pending" not in memo:
                    memo["pending"] = _pending_identity(memory_refs._pending_reference_projection(root))
                valid = memo["pending"] == proof.pending
        except (TimeoutError, runtime_resources.PreparationBudgetExceeded,
                runtime_resources.PreparationCapacityBusy, MemoryError):
            raise
        except Exception:  # noqa: BLE001 - unsupported or unknown owner proof requires canonical preparation
            valid = False
        if directory_facts is not None:
            admit_phase(extra=directory_facts.retained_bytes)
        elif memory is not None:
            memory()
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
