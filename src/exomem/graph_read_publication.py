"""Associate complete graph rows with locally observed canonical generations."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import freshness, graph_sync, view_lag

if TYPE_CHECKING:
    from .mutation_lock import VaultMutationCoordinator
    from .vault import WikilinkResolver


@dataclass(frozen=True)
class Publication:
    token: tuple[tuple[str, str], ...]
    consumer: freshness.FreshnessCheckpoint
    recall: freshness.RecallFreshnessCheckpoint
    resolver: WikilinkResolver
    topology: str
    lineage: object


@dataclass(frozen=True)
class _Bridge:
    epoch: graph_sync.GraphPublicationEpoch
    consumer: freshness.FreshnessCheckpoint
    recall: freshness.RecallFreshnessCheckpoint


@dataclass(frozen=True)
class _TargetProof:
    path: str
    digest: str | None
    signature: freshness.FileSignature | None


@dataclass(frozen=True)
class _State:
    bridge: _Bridge
    publication: Publication
    coordinator: VaultMutationCoordinator
    reader_lock: threading.RLock
    # Unverified bytes cost optional context until they can authorize retained rows.
    pending: tuple[_TargetProof, ...] | None = None


@dataclass(frozen=True)
class PreparedPublication:
    root: Path
    previous: _State | None
    state: _State


@dataclass(frozen=True)
class _Observation:
    root: Path
    state: _State
    epoch: graph_sync.GraphPublicationEpoch


_lock = threading.Lock()
_states: dict[Path, _State] = {}


def _policy(checkpoint: freshness.RecallFreshnessCheckpoint) -> tuple[str, str]:
    return checkpoint.policy_version, checkpoint.access_policy_fingerprint


def _current(root: Path, bridge: _Bridge) -> bool:
    return (
        graph_sync.canonical_publication_epoch(root) == bridge.epoch
        and freshness.consumer_checkpoint(root, "vault") == bridge.consumer
        and freshness.live_recall_checkpoint(root, "vault") == bridge.recall
    )


def prepare_complete(
    root: Path,
    metadata: dict[str, str],
    resolver: WikilinkResolver | None,
    *,
    full_source_proof: bool = False,
    coordinator: VaultMutationCoordinator | None = None,
) -> PreparedPublication | None:
    """Prepare a complete publication before the caller takes canonical authority.

    Only the existing complete-source proof can establish a lost association.
    The supplied resolver must already be detached from mutable writer state.
    """
    from .epistemic_graph import (
        _checkpoint_from_value,
        _graph_sync_acknowledgement,
        _resolver_topology_fingerprint,
    )
    from .writer_lease import active_manager

    try:
        root = Path(root).resolve()
        with _lock:
            previous = _states.get(root)
        epoch = graph_sync.canonical_publication_epoch(root)
        acknowledgement = _graph_sync_acknowledgement(metadata)
        # These keys belong to the fixed graph metadata schema, not vocabulary.
        if epoch.checkpoint is None:
            if any(
                key in metadata
                for key in (
                    "graph_sync_checkpoint",
                    "graph_sync_generation",
                    "graph_sync_digest",
                )
            ):
                return None
        elif acknowledgement != epoch.checkpoint:
            return None
        consumer = freshness.consumer_checkpoint(root, "vault")
        recall = freshness.live_recall_checkpoint(root, "vault")
        topology = metadata.get("recall_resolver_topology", "")
        if (
            consumer.triple is None
            or recall is None
            or _checkpoint_from_value(metadata.get("recall_projection_checkpoint")) != recall
            or metadata.get("recall_policy_version") != recall.policy_version
            or metadata.get("recall_access_fingerprint") != recall.access_policy_fingerprint
            or len(topology) != 64
        ):
            return None
        bridge = _Bridge(epoch, consumer, recall)
        owner = coordinator or (
            previous.coordinator
            if previous is not None
            else active_manager()._mutation_coordinator_for(root)
        )
        associated = (
            previous is not None and previous.pending is None and previous.bridge == bridge
            and owner.lock_path.resolve() == previous.coordinator.lock_path.resolve()
        )
        if not associated and not full_source_proof:
            return None
        reuse = (
            previous is not None
            and previous.publication.topology == topology
            and _policy(previous.publication.recall) == _policy(recall)
        )
        if reuse:
            resolver = previous.publication.resolver
        elif resolver is None or _resolver_topology_fingerprint(resolver) != topology:
            return None
        lineage = previous.publication.lineage if associated else object()
        publication = Publication(
            tuple(sorted(metadata.items())),
            consumer,
            recall,
            resolver,
            topology,
            lineage,
        )
        return PreparedPublication(
            root, previous,
            _State(
                previous.bridge if associated else bridge, publication, owner,
                previous.reader_lock if previous is not None else threading.RLock(),
            ),
        )
    except Exception:  # noqa: BLE001 - optional retention never changes the committed result
        return None


def install_prepared(prepared: PreparedPublication) -> bool:
    """Install RAM state after the caller's exact ticket check and successful commit.

    The caller holds canonical authority and validates its durable epoch,
    recall and policy ticket. This step performs no filesystem or SQLite work.
    """
    with _lock:
        if _states.get(prepared.root) is not prepared.previous:
            return False
        _states[prepared.root] = prepared.state
    return True


def record_complete(
    root: Path, metadata: dict[str, str], resolver: WikilinkResolver | None, *,
    full_source_proof: bool = False, coordinator: VaultMutationCoordinator | None = None,
) -> bool:
    """Record a committed, source-checked publication under canonical authority."""
    prepared = prepare_complete(
        root, metadata, resolver, full_source_proof=full_source_proof, coordinator=coordinator,
    )
    try:
        return (
            prepared is not None
            and _current(prepared.root, prepared.state.bridge)
            and install_prepared(prepared)
        )
    except Exception:  # noqa: BLE001 - optional retention cannot change the commit result
        return False


def published(root: Path) -> Publication | None:
    root = Path(root).resolve()
    with _lock:
        state = _states.get(root)
        return state.publication if state is not None else None


def forget(root: Path) -> None:
    root = Path(root).resolve()
    with _lock:
        _states.pop(root, None)


def clear() -> None:
    """Release retained publications when the caller invalidates all resolver proofs."""
    with _lock:
        _states.clear()


@contextmanager
def read_boundary(
    root: Path, *, operation: str, deadline: float | None = None,
    coordinator: VaultMutationCoordinator | None = None,
) -> Iterator[bool]:
    """Queue short reader proofs within their budget, without waiting for writers.

    The mutex prevents another reader's proof from looking like a busy writer.
    Expiry costs optional context; neither lock covers queries or hydration.
    """
    root = Path(root).resolve()
    with _lock:
        state = _states.get(root)
    if state is None or state.pending is not None:
        yield False
        return
    owner = state.coordinator
    if coordinator is not None and coordinator.lock_path.resolve() != owner.lock_path.resolve():
        yield False
        return
    # A canonical holder must never wait behind a reader that probes its boundary.
    if owner.current_thread_holds_boundary():
        with owner.try_hold(operation=operation, holder_kind="graph") as held:
            yield held and (deadline is None or time.monotonic() < deadline)
        return
    acquired = (
        state.reader_lock.acquire(blocking=False) if deadline is None
        else state.reader_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
    )
    if not acquired:
        yield False
        return
    try:
        if deadline is not None and time.monotonic() >= deadline:
            yield False
            return
        with owner.try_hold(operation=operation, holder_kind="graph") as held:
            with _lock:
                current = _states.get(root)
            yield (
                held and current is not None and current.pending is None
                and current.reader_lock is state.reader_lock
                and current.publication.lineage is state.publication.lineage
                and (deadline is None or time.monotonic() < deadline)
            )
    finally:
        state.reader_lock.release()


def admissible(
    root: Path, publication: Publication, visible: Callable[[str], bool], *,
    deadline: float | None = None,
) -> bool:
    """Admit retained rows only through an exact current local association."""
    try:
        root = Path(root).resolve()
        with _lock:
            state = _states.get(root)
        if state is None:
            return False
        with read_boundary(root, operation="graph_read_publication", deadline=deadline) as held:
            if not held:
                return False
            with _lock:
                current = _states.get(root)
            if (
                current is not state
                or publication.lineage is not state.publication.lineage
                or _policy(publication.recall) != _policy(state.bridge.recall)
                or not _current(root, state.bridge)
            ):
                return False
        # Release decisions may read policy and sources; keep them outside the boundary.
        if not view_lag.consumer_lag_only_withheld(
            root,
            publication.consumer,
            visible,
            pending=True,
        ):
            return False
        with read_boundary(root, operation="graph_read_publication_recheck", deadline=deadline) as held:
            if not held:
                return False
            with _lock:
                current = _states.get(root)
            return (
                current is not None
                and current.bridge is state.bridge
                and current.publication.lineage is publication.lineage
                and _current(root, state.bridge)
            )
    except Exception:  # noqa: BLE001 - uncertainty keeps the existing unavailable result
        return False


def _callback_paths(root: Path, paths: tuple[Path | str, ...]) -> set[str | None]:
    return {
        freshness.registry_rel(root, str(path if path.is_absolute() else root / path))
        for value in paths
        for path in (Path(value),)
    }


def begin_observation(
    root: Path,
    changed: tuple[Path, ...],
    deleted: tuple[Path | str, ...],
) -> _Observation | None:
    """Capture the predecessor before corpus publication, without waiting."""
    try:
        root = Path(root).resolve()
        with _lock:
            state = _states.get(root)
        if state is None:
            return None
        with state.coordinator.try_hold(operation="graph_observation_begin") as held:
            if not held:
                return None
            with _lock:
                if _states.get(root) is not state:
                    return None
            epoch = graph_sync.canonical_publication_epoch(root)
            checkpoint = epoch.checkpoint
            previous = state.bridge.epoch.checkpoint
            if (
                checkpoint is None
                or checkpoint.scope != "paths"
                or checkpoint.generation != (previous.generation if previous else 0) + 1
                or not 0 < len(checkpoint.paths) <= view_lag.MAX_DECIDED_PATHS
                or freshness.consumer_checkpoint(root, "vault") != state.bridge.consumer
                or freshness.live_recall_checkpoint(root, "vault") != state.bridge.recall
            ):
                return None
            changed_paths = _callback_paths(root, changed)
            deleted_paths = _callback_paths(root, deleted)
            if None in changed_paths | deleted_paths or changed_paths & deleted_paths:
                return None
            if any(
                rel not in (deleted_paths if digest is None else changed_paths)
                for rel, digest in checkpoint.paths
            ):
                return None
            return _Observation(root, state, epoch)
    except Exception:  # noqa: BLE001 - optional association failure cannot fail publication
        return None


def finish_observation(root: Path, observation: _Observation | None) -> None:
    """Stage a contiguous association; prove bytes after the outer writer releases."""
    if observation is None:
        return

    try:
        root = Path(root).resolve()
        if root != observation.root:
            return
        prior = observation.state
        with prior.coordinator.try_hold(operation="graph_observation_finish") as held:
            if not held:
                return
            with _lock:
                current = _states.get(root)
            if (
                current is None
                or current.bridge is not prior.bridge
                or current.publication.lineage is not prior.publication.lineage
                or graph_sync.canonical_publication_epoch(root) != observation.epoch
            ):
                return
            consumer = freshness.consumer_checkpoint(root, "vault")
            recall = freshness.live_recall_checkpoint(root, "vault")
            if recall is None or _policy(recall) != _policy(prior.bridge.recall):
                return
            delta = freshness.delta_since(root, "vault", prior.bridge.consumer)
            checkpoint = observation.epoch.checkpoint
            assert checkpoint is not None
            changed = {rel for rel, digest in checkpoint.paths if digest is not None}
            deleted = {rel for rel, digest in checkpoint.paths if digest is None}
            if (
                not delta.complete
                or delta.to != consumer
                or _callback_paths(root, tuple(delta.changed)) != changed
                or _callback_paths(root, tuple(delta.deleted)) != deleted
            ):
                return
            signatures = {
                freshness.registry_rel(root, path): signature
                for path, signature in delta.target_signatures
            }
            if signatures.keys() != changed:
                return
            recall_delta = freshness.recall_delta_since_live(root, "vault", prior.bridge.recall)
            if (
                recall_delta is None
                or not recall_delta.complete
                or recall_delta.requires_source_proof
                or recall_delta.to != recall
            ):
                return
            # Coalesce repeated endpoints without dropping an earlier unproved mutation.
            targets = {target.path: target for target in current.pending or ()}
            targets.update({
                rel: _TargetProof(rel, digest, signatures.get(rel))
                for rel, digest in checkpoint.paths
            })
            if len(targets) > view_lag.MAX_DECIDED_PATHS:
                return
            bridge = _Bridge(observation.epoch, consumer, recall)
            if not _current(root, bridge):
                return
            with _lock:
                current = _states.get(root)
                if (
                    current is not None
                    and current.bridge is prior.bridge
                    and current.publication.lineage is prior.publication.lineage
                ):
                    pending = _State(
                        bridge, current.publication, current.coordinator, current.reader_lock,
                        tuple(targets[path] for path in sorted(targets)),
                    )
                    _states[root] = pending
                    current.coordinator.defer_optional_after_release(
                        (finish_observation, root), lambda: _complete_observation(root, pending),
                    )
    except Exception:  # noqa: BLE001 - optional association failure cannot fail publication
        return


def _complete_observation(root: Path, pending: _State) -> None:
    """Hash the pending endpoint union without either mutation guard, then compare and swap."""
    from . import vault

    try:
        with _lock:
            if _states.get(root) is not pending:
                return
        guards = []
        for target in pending.pending or ():
            if target.digest is None:
                guard = vault.PathGuard.capture(root, target.path, leaf_policy="absent")
            else:
                signature = target.signature
                if signature is None or freshness.stat_signature(root / target.path) != signature:
                    raise ValueError("graph observation target changed")
                _data, content = vault.read_bounded_guarded_bytes(
                    root, target.path, limit=signature[2], expected_hash=target.digest,
                )
                # Keep the descriptor-bound identity; finalization reads no file contents.
                guard = replace(content, leaf_policy="stable", expected_content_hash=None)
            guards.append((target, guard))
        with pending.coordinator.try_hold(operation="graph_observation_complete") as held:
            if not held:
                return
            with _lock:
                if _states.get(root) is not pending:
                    return
            if not _current(root, pending.bridge):
                raise ValueError("graph observation epoch changed")
            for target, guard in guards:
                guard.recheck(root)
                if target.signature is not None and (
                    freshness.stat_signature(root / target.path) != target.signature
                ):
                    raise ValueError("graph observation target changed")
            with _lock:
                if _states.get(root) is pending:
                    _states[root] = replace(pending, pending=None)
    except Exception:  # noqa: BLE001 - unexplained bytes withdraw only the optional association
        with _lock:
            if _states.get(root) is pending:
                _states.pop(root, None)
