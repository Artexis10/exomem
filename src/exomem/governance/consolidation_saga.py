"""Policy-first publication ordering for governed vault consolidation.

This module is deliberately narrower than the complete apply coordinator.  It
binds the approved deterministic content partition and will not invoke a batch
publisher until the existing governance transaction returns its exact committed
policy terminal.  Crash classification, content materialization, and recovery
remain separate layers built on these immutable batch descriptors.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol

from .. import vault
from . import consolidation_plan

POLICY_ACTIVATION_TERMINAL_SCHEMA = (
    "exomem.consolidation-policy-activation-terminal/v1"
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_COMMITTED_EVENT = re.compile(r"([0-9a-f]{64}):committed\Z")
_BATCH_FIELDS = frozenset(
    {
        "batch_ordinal",
        "first_action_ordinal",
        "last_action_ordinal",
        "action_count",
        "publication_boundary",
        "action_set_digest",
        "prior_fingerprint",
        "prepared_fingerprint",
        "final_fingerprint",
    }
)

__all__ = [
    "POLICY_ACTIVATION_TERMINAL_SCHEMA",
    "BatchJournal",
    "ContentBatch",
    "PolicyActivationTerminal",
    "PolicyFirstPublicationResult",
    "PolicyFirstPublicationUnavailable",
    "publish_policy_first",
]


class PolicyFirstPublicationUnavailable(RuntimeError):
    """Stable refusal for malformed policy terminals or batch partitions."""

    code = "CONSOLIDATION_PUBLICATION_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("consolidation publication is unavailable")


@dataclass(frozen=True, slots=True)
class PolicyActivationTerminal:
    schema: str
    policy_fingerprint: str
    intent_event_id: str
    prepared_fingerprint: str
    active_fingerprint: str
    terminal_event_id: str


@dataclass(frozen=True, slots=True)
class ContentBatch:
    ordinal: int
    first_action_ordinal: int
    last_action_ordinal: int
    action_count: int
    publication_boundary: bool
    action_set_digest: str
    prior_fingerprint: str
    prepared_fingerprint: str
    final_fingerprint: str


@dataclass(frozen=True, slots=True)
class PolicyFirstPublicationResult:
    policy_terminal: PolicyActivationTerminal
    partition_digest: str
    publication_boundary_ordinal: int
    committed_batch_ordinals: tuple[int, ...]


class BatchJournal(Protocol):
    """Durable receipt-first journal boundary supplied by the full coordinator."""

    def prepare_batch(self, batch: ContentBatch) -> object:
        """Persist the exact intent/prepared transition before publication."""

    def commit_batch(self, batch: ContentBatch) -> object:
        """Persist the exact final/terminal transition after publication."""


def _fail() -> NoReturn:
    raise PolicyFirstPublicationUnavailable from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _policy_terminal(
    value: object,
    *,
    expected_policy_fingerprint: str,
) -> PolicyActivationTerminal:
    if not isinstance(value, PolicyActivationTerminal):
        _fail()
    intent_id = _digest(value.intent_event_id)
    terminal_match = (
        _COMMITTED_EVENT.fullmatch(value.terminal_event_id)
        if isinstance(value.terminal_event_id, str)
        else None
    )
    if (
        value.schema != POLICY_ACTIVATION_TERMINAL_SCHEMA
        or value.policy_fingerprint != _digest(expected_policy_fingerprint)
        or terminal_match is None
        or terminal_match.group(1) != intent_id
    ):
        _fail()
    return PolicyActivationTerminal(
        schema=value.schema,
        policy_fingerprint=value.policy_fingerprint,
        intent_event_id=intent_id,
        prepared_fingerprint=_digest(value.prepared_fingerprint),
        active_fingerprint=_digest(value.active_fingerprint),
        terminal_event_id=value.terminal_event_id,
    )


def _content_batches(
    partition: consolidation_plan.CanonicalObject,
) -> tuple[ContentBatch, ...]:
    if not isinstance(partition, consolidation_plan.CanonicalObject):
        _fail()
    try:
        checked = consolidation_plan.parse_journal_batch_partition(
            partition.canonical_bytes
        )
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    if checked != partition:
        _fail()
    raw_batches = checked.preimage["batches"]
    if not isinstance(raw_batches, tuple):
        _fail()
    batches: list[ContentBatch] = []
    for ordinal, raw in enumerate(raw_batches):
        if not isinstance(raw, Mapping) or frozenset(raw) != _BATCH_FIELDS:
            _fail()
        batches.append(
            ContentBatch(
                ordinal=ordinal,
                first_action_ordinal=int(raw["first_action_ordinal"]),
                last_action_ordinal=int(raw["last_action_ordinal"]),
                action_count=int(raw["action_count"]),
                publication_boundary=raw["publication_boundary"] is True,
                action_set_digest=_digest(raw["action_set_digest"]),
                prior_fingerprint=_digest(raw["prior_fingerprint"]),
                prepared_fingerprint=_digest(raw["prepared_fingerprint"]),
                final_fingerprint=_digest(raw["final_fingerprint"]),
            )
        )
    return tuple(batches)


def publish_policy_first(
    *,
    content_actions: object,
    approved_partition_digest: str,
    expected_policy_fingerprint: str,
    activate_policy: Callable[[], PolicyActivationTerminal],
    journal: BatchJournal,
    vault_root: Path,
    materialize_batch: Callable[[ContentBatch], Iterable[vault.PlannedWrite]],
) -> PolicyFirstPublicationResult:
    """Activate the reviewed policy terminal before invoking any content batch."""

    partition = consolidation_plan.derive_journal_batch_partition(content_actions)
    batches = _content_batches(partition)
    if partition.digest != _digest(approved_partition_digest):
        _fail()
    terminal = _policy_terminal(
        activate_policy(),
        expected_policy_fingerprint=expected_policy_fingerprint,
    )
    committed: list[int] = []
    for batch in batches:
        journal.prepare_batch(batch)
        writes = tuple(materialize_batch(batch))
        vault.batch_atomic_write(
            writes,
            vault_root=Path(vault_root),
            post_commit_fanout=False,
        )
        journal.commit_batch(batch)
        committed.append(batch.ordinal)
    return PolicyFirstPublicationResult(
        policy_terminal=terminal,
        partition_digest=partition.digest,
        publication_boundary_ordinal=batches[0].ordinal,
        committed_batch_ordinals=tuple(committed),
    )
