"""Deterministic real-shape fake for the frozen derived-receipt protocol."""

from __future__ import annotations

import hashlib
import importlib
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from exomem.derived_receipts import (
        DerivedBatchPath,
        DerivedBatchProof,
        DerivedBatchReceipt,
        DerivedComponent,
        DerivedComponentStatus,
    )


def _protocol():
    return importlib.import_module("exomem.derived_receipts")


class DerivedReceiptProtocolFake:
    """Scriptable fake with the exact six protocol seams and ordered calls.

    ``inject`` queues return values, exceptions, or callables for one seam. A
    callable receives the same positional and keyword arguments as the seam.
    Defaults are deterministic and use the production dataclasses rather than
    dictionaries, so downstream lanes exercise the frozen wire shape.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._injected: dict[str, deque[Any]] = defaultdict(deque)

    def inject(self, seam: str, *outcomes: Any) -> None:
        if seam not in {
            "prepare_batch",
            "prove_committed",
            "publish_pending_visibility",
            "signal_components",
            "component_status",
            "advisory_result_ref",
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
                f"{batch_id}:write_advisory:1".encode()
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
        return protocol.DerivedBatchReceipt(
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
