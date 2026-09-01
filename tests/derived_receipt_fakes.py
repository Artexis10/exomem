"""Deterministic real-shape fake for the frozen derived-receipt protocol."""

from __future__ import annotations

import hashlib
import importlib
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from exomem.derived_receipts import (
        DerivedAdvisoryCandidate,
        DerivedAdvisoryPublication,
        DerivedAdvisoryResult,
        DerivedBatchPath,
        DerivedBatchProof,
        DerivedBatchReceipt,
        DerivedComponent,
        DerivedComponentStatus,
        PendingVisibilityBatch,
        PendingVisibilityRetirement,
        PendingVisibilitySnapshot,
    )


def _protocol():
    return importlib.import_module("exomem.derived_receipts")


class DerivedReceiptProtocolFake:
    """Scriptable fake with the frozen protocol seams and ordered calls.

    ``inject`` queues return values, exceptions, or callables for one seam. A
    callable receives the same positional and keyword arguments as the seam.
    Defaults are deterministic and use the production dataclasses rather than
    dictionaries, so downstream lanes exercise the frozen wire shape.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._injected: dict[str, deque[Any]] = defaultdict(deque)
        self._pending_batches: dict[str, Any] = {}
        self._pending_generation = 0
        self._advisory_results: dict[str, Any] = {}

    def inject(self, seam: str, *outcomes: Any) -> None:
        if seam not in {
            "prepare_batch",
            "prove_committed",
            "publish_pending_visibility",
            "signal_components",
            "component_status",
            "advisory_result_ref",
            "snapshot_pending_visibility",
            "pending_visibility_snapshot_is_current",
            "retire_pending_visibility",
            "read_advisory_result",
            "publish_advisory_result",
        }:
            raise ValueError(f"unknown derived receipt seam: {seam}")
        self._injected[seam].extend(outcomes)

    def call_count(self, seam: str) -> int:
        return sum(call[0] == seam for call in self.calls)

    @property
    def call_order(self) -> tuple[str, ...]:
        return tuple(call[0] for call in self.calls)

    def _record(self, seam: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        self.calls.append((seam, args, dict(kwargs)))
        if not self._injected[seam]:
            return None
        outcome = self._injected[seam].popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(*args, **kwargs)
        return outcome

    def prepare_batch(
        self,
        vault_root: Path,
        *,
        batch_id: str,
        mutation_attempt_digest: str,
        canonical_generation: str,
        checkpoint_id: str,
        paths: Sequence[DerivedBatchPath],
        required_components: Collection[DerivedComponent],
        advisory_target_rel_path: str | None = None,
        advisory_target_fingerprint: str | None = None,
        terminal_replay_until: float | None = None,
        advisory_retention_until: float | None = None,
        now: float | None = None,
    ) -> DerivedBatchReceipt:
        kwargs = {
            "batch_id": batch_id,
            "mutation_attempt_digest": mutation_attempt_digest,
            "canonical_generation": canonical_generation,
            "checkpoint_id": checkpoint_id,
            "paths": paths,
            "required_components": required_components,
            "advisory_target_rel_path": advisory_target_rel_path,
            "advisory_target_fingerprint": advisory_target_fingerprint,
            "terminal_replay_until": terminal_replay_until,
            "advisory_retention_until": advisory_retention_until,
            "now": now,
        }
        injected = self._record("prepare_batch", (vault_root,), kwargs)
        if injected is not None:
            return injected
        protocol = _protocol()
        required = frozenset(required_components)
        prepared_at = float(now if now is not None else 0.0)
        result_ref = None
        if protocol.DerivedComponent.WRITE_ADVISORY in required:
            digest = hashlib.sha256(
                f"{batch_id}:write_advisory:1:{advisory_target_rel_path}:"
                f"{advisory_target_fingerprint}".encode()
            ).hexdigest()[:32]
            result_ref = f"exomem://write-advisory-result/{digest}"
        components = tuple(
            protocol.DerivedComponentStatus(
                batch_id=batch_id,
                component=component,
                revision=1,
                lease_revision=0,
                state="prepared" if component in required else "not_required",
                canonical_generation=canonical_generation,
                attempt_count=0,
                next_attempt_at=prepared_at,
                claim_owner=None,
                claim_expires_at=None,
                failure_code=None,
                advisory_result_ref=(
                    result_ref
                    if component is protocol.DerivedComponent.WRITE_ADVISORY
                    else None
                ),
            )
            for component in protocol.DerivedComponent
        )
        receipt = protocol.DerivedBatchReceipt(
            schema_version=1,
            batch_id=batch_id,
            mutation_attempt_digest=mutation_attempt_digest,
            canonical_generation=canonical_generation,
            checkpoint_id=checkpoint_id,
            state="prepared",
            prepared_at=prepared_at,
            paths=tuple(paths),
            components=components,
        )
        if batch_id not in self._pending_batches and receipt.paths:
            rows = tuple(
                protocol.PendingVisibilityRow(
                    rel_path=path.rel_path,
                    component_revision=1,
                    canonical_generation=canonical_generation,
                    state="prepared",
                )
                for path in receipt.paths
            )
            self._pending_batches[batch_id] = protocol.PendingVisibilityBatch(
                receipt=receipt,
                rows=rows,
            )
            self._pending_generation += 1
        if result_ref is not None and result_ref not in self._advisory_results:
            self._advisory_results[result_ref] = protocol.DerivedAdvisoryResult(
                ref=result_ref,
                batch_id=batch_id,
                component_revision=1,
                target_rel_path=advisory_target_rel_path,
                target_fingerprint=advisory_target_fingerprint,
                state="pending",
                candidates=(),
                failure_code=None,
                publication_revision=1,
                retention_deadline=float(
                    advisory_retention_until
                    if advisory_retention_until is not None
                    else terminal_replay_until
                ),
                terminal_replay_until=float(terminal_replay_until),
                published_at=None,
                created_at=prepared_at,
                updated_at=prepared_at,
            )
        return receipt

    def prove_committed(
        self,
        vault_root: Path,
        receipt: DerivedBatchReceipt,
        *,
        current_generation: str,
        known_uncommitted: bool = False,
        now: float | None = None,
    ) -> DerivedBatchProof:
        kwargs = {
            "current_generation": current_generation,
            "known_uncommitted": known_uncommitted,
            "now": now,
        }
        injected = self._record("prove_committed", (vault_root, receipt), kwargs)
        if injected is not None:
            return injected
        protocol = _protocol()
        ready = tuple(
            status.component
            for status in receipt.components
            if status.state != "not_required"
        )
        return protocol.DerivedBatchProof(
            batch_id=receipt.batch_id,
            outcome="ready",
            canonical_generation=current_generation,
            path_states=tuple("after" for _path in receipt.paths),
            ready_components=ready,
            canonical_replay_authorized=False,
        )

    def publish_pending_visibility(
        self,
        vault_root: Path,
        receipt: DerivedBatchReceipt,
        *,
        publisher: Callable[[Path, DerivedBatchReceipt], bool] | None = None,
        now: float | None = None,
    ) -> bool:
        kwargs = {"publisher": publisher, "now": now}
        injected = self._record(
            "publish_pending_visibility", (vault_root, receipt), kwargs
        )
        if injected is not None:
            return bool(injected)
        if publisher is None:
            raise RuntimeError("pending visibility publisher is required")
        if not publisher(vault_root, receipt):
            raise RuntimeError("pending visibility publisher did not prove publication")
        pending = self._pending_batches.get(receipt.batch_id)
        if pending is not None:
            protocol = _protocol()
            self._pending_batches[receipt.batch_id] = protocol.PendingVisibilityBatch(
                receipt=pending.receipt,
                rows=tuple(replace(row, state="live") for row in pending.rows),
            )
            self._pending_generation += 1
        return True

    def signal_components(
        self,
        vault_root: Path,
        receipt: DerivedBatchReceipt,
    ) -> None:
        injected = self._record("signal_components", (vault_root, receipt), {})
        if injected is not None:
            return injected
        return None

    def component_status(
        self,
        vault_root: Path,
        receipt: DerivedBatchReceipt,
        component: DerivedComponent,
    ) -> DerivedComponentStatus:
        injected = self._record(
            "component_status", (vault_root, receipt, component), {}
        )
        if injected is not None:
            return injected
        return next(
            status for status in receipt.components if status.component is component
        )

    def advisory_result_ref(
        self,
        vault_root: Path,
        receipt: DerivedBatchReceipt,
    ) -> str | None:
        injected = self._record("advisory_result_ref", (vault_root, receipt), {})
        if injected is not None:
            return injected
        protocol = _protocol()
        return next(
            status.advisory_result_ref
            for status in receipt.components
            if status.component is protocol.DerivedComponent.WRITE_ADVISORY
        )

    def snapshot_pending_visibility(
        self,
        vault_root: Path,
        *,
        limit: int,
    ) -> PendingVisibilitySnapshot:
        injected = self._record(
            "snapshot_pending_visibility", (vault_root,), {"limit": limit}
        )
        if injected is not None:
            return injected
        if limit <= 0:
            raise ValueError("pending visibility snapshot limit must be positive")
        protocol = _protocol()
        batches = tuple(
            self._pending_batches[batch_id]
            for batch_id in sorted(self._pending_batches)
            if any(row.state != "retired" for row in self._pending_batches[batch_id].rows)
        )
        row_count = sum(len(batch.rows) for batch in batches)
        if row_count > limit:
            return protocol.PendingVisibilitySnapshot(
                outcome="overflow",
                snapshot_generation=self._pending_generation,
                batches=(),
                failure_code="pending_visibility_overflow",
            )
        return protocol.PendingVisibilitySnapshot(
            outcome="complete",
            snapshot_generation=self._pending_generation,
            batches=batches,
        )

    def pending_visibility_snapshot_is_current(
        self,
        vault_root: Path,
        snapshot_generation: int,
    ) -> bool:
        injected = self._record(
            "pending_visibility_snapshot_is_current",
            (vault_root, snapshot_generation),
            {},
        )
        if injected is not None:
            return bool(injected)
        return snapshot_generation == self._pending_generation

    def retire_pending_visibility(
        self,
        vault_root: Path,
        batch: PendingVisibilityBatch,
        *,
        now: float | None = None,
    ) -> PendingVisibilityRetirement:
        injected = self._record(
            "retire_pending_visibility", (vault_root, batch), {"now": now}
        )
        if injected is not None:
            return injected
        protocol = _protocol()
        current = self._pending_batches.get(batch.receipt.batch_id)
        if current is None or current.rows != batch.rows:
            return protocol.PendingVisibilityRetirement(outcome="stale")
        self._pending_batches[batch.receipt.batch_id] = protocol.PendingVisibilityBatch(
            receipt=current.receipt,
            rows=tuple(replace(row, state="retired") for row in current.rows),
        )
        self._pending_generation += 1
        return protocol.PendingVisibilityRetirement(outcome="retired")

    def read_advisory_result(
        self,
        vault_root: Path,
        ref: str,
        *,
        now: float | None = None,
    ) -> DerivedAdvisoryResult | None:
        injected = self._record(
            "read_advisory_result", (vault_root, ref), {"now": now}
        )
        if injected is not None:
            return injected
        result = self._advisory_results.get(ref)
        if result is None:
            return None
        observed_at = float(now if now is not None else 0.0)
        if (
            result.retention_deadline < observed_at
            and result.terminal_replay_until < observed_at
        ):
            return None
        return result

    def publish_advisory_result(
        self,
        vault_root: Path,
        claimed_status: DerivedComponentStatus,
        *,
        state: str,
        candidates: Sequence[DerivedAdvisoryCandidate] = (),
        failure_code: str | None = None,
        observed_target_fingerprint: str,
        now: float | None = None,
    ) -> DerivedAdvisoryPublication:
        kwargs = {
            "state": state,
            "candidates": candidates,
            "failure_code": failure_code,
            "observed_target_fingerprint": observed_target_fingerprint,
            "now": now,
        }
        injected = self._record(
            "publish_advisory_result", (vault_root, claimed_status), kwargs
        )
        if injected is not None:
            return injected
        protocol = _protocol()
        ref = claimed_status.advisory_result_ref
        current = None if ref is None else self._advisory_results.get(ref)
        if (
            current is None
            or claimed_status.component is not protocol.DerivedComponent.WRITE_ADVISORY
            or claimed_status.state != "claimed"
        ):
            return protocol.DerivedAdvisoryPublication(outcome="stale_claim")
        if current.target_fingerprint != observed_target_fingerprint:
            self._advisory_results[ref] = replace(
                current,
                state="superseded",
                candidates=(),
                failure_code=None,
                publication_revision=current.publication_revision + 1,
                published_at=float(now if now is not None else 0.0),
                updated_at=float(now if now is not None else 0.0),
            )
            return protocol.DerivedAdvisoryPublication(outcome="superseded")
        normalized = tuple(candidates)
        if current.state != "pending":
            outcome = (
                "already_published"
                if current.state == state
                and current.candidates == normalized
                and current.failure_code == failure_code
                else "stale_claim"
            )
            return protocol.DerivedAdvisoryPublication(outcome=outcome)
        self._advisory_results[ref] = replace(
            current,
            state=state,
            candidates=normalized,
            failure_code=failure_code,
            publication_revision=current.publication_revision + 1,
            published_at=float(now if now is not None else 0.0),
            updated_at=float(now if now is not None else 0.0),
        )
        return protocol.DerivedAdvisoryPublication(outcome="published")
