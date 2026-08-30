"""Policy-first publication ordering for governed vault consolidation.

This module is deliberately narrower than the complete apply coordinator.  It
binds the approved deterministic content partition and will not invoke a batch
publisher until the existing governance transaction returns its exact committed
policy terminal.  Crash classification, content materialization, and recovery
remain separate layers built on these immutable batch descriptors.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn, Protocol

from .. import held_fs, vault
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
    "BatchStateObservation",
    "ContentBatch",
    "PolicyActivationTerminal",
    "PolicyFirstPublicationResult",
    "PolicyFirstPublicationUnavailable",
    "classify_content_batch_state",
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
class BatchStateObservation:
    """Content-free exact-state classification for one canonical batch."""

    batch_ordinal: int
    action_count: int
    state: Literal["prior", "final", "equivalent", "mixed"]


@dataclass(frozen=True, slots=True)
class PolicyFirstPublicationResult:
    policy_terminal: PolicyActivationTerminal
    partition_digest: str
    publication_boundary_ordinal: int
    committed_batch_ordinals: tuple[int, ...]


class BatchJournal(Protocol):
    """Durable receipt-first journal boundary supplied by the full coordinator."""

    def batch_status(self, batch: ContentBatch) -> str:
        """Return the exact durable status for this immutable batch."""

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


def _observed_file_state(
    filesystem: held_fs.HeldFilesystem,
    stack: ExitStack,
    destination_path: str,
) -> tuple[str, str]:
    path = PurePosixPath(destination_path)
    parent_path = path.parent.as_posix()
    parent_result = filesystem.parent(parent_path)
    if parent_result.error is not None:
        if parent_result.error.code == "MISSING":
            return "absent", "0" * 64
        _fail()
    parent = stack.enter_context(parent_result.require())
    file_result = filesystem.file(parent, path.name)
    if file_result.error is not None:
        if file_result.error.code == "MISSING":
            return "absent", "0" * 64
        _fail()
    file = stack.enter_context(file_result.require())
    if file.identity.kind != "file" or file.identity.link_count != 1:
        _fail()
    return "present", _digest(filesystem.sha256(file).require())


def _matches_action_state(
    action: Mapping[str, object],
    observed: tuple[str, str],
    *,
    prefix: Literal["expected_before", "planned_after"],
) -> bool:
    return observed == (action[f"{prefix}_state"], action[f"{prefix}_sha256"])


def _validated_batch_writes(
    *,
    vault_root: Path,
    actions: tuple[Mapping[str, object], ...],
    writes: Iterable[vault.PlannedWrite],
) -> tuple[vault.PlannedWrite, ...]:
    root = Path(os.path.abspath(vault_root))
    required = {
        str(action["destination_path"]): action
        for action in actions
        if (
            action["planned_after_state"] == "present"
            and (
                action["expected_before_state"] != action["planned_after_state"]
                or action["expected_before_sha256"] != action["planned_after_sha256"]
            )
        )
    }
    checked: dict[str, vault.PlannedWrite] = {}
    for write in writes:
        if not isinstance(write, vault.PlannedWrite):
            _fail()
        try:
            relative = Path(os.path.abspath(write.path)).relative_to(root).as_posix()
        except ValueError:
            _fail()
        action = required.get(relative)
        if action is None or relative in checked:
            _fail()
        if isinstance(write.content, str):
            content_digest = hashlib.sha256(write.content.encode("utf-8")).hexdigest()
        elif isinstance(write.content, vault.PreparedBinaryContent):
            content_digest = _digest(write.content.sha256)
        else:  # pragma: no cover - PlannedWrite carries the closed content union
            _fail()
        expected_missing = action["expected_before_state"] == "absent"
        expected_hash = (
            vault.MISSING_CONTENT_HASH
            if expected_missing
            else action["expected_before_sha256"]
        )
        if (
            content_digest != action["planned_after_sha256"]
            or write.expected_hash != expected_hash
            or write.create_only is not expected_missing
        ):
            _fail()
        checked[relative] = write
    if frozenset(checked) != frozenset(required):
        _fail()
    return tuple(checked[path] for path in required)


def classify_content_batch_state(
    *,
    vault_root: Path,
    content_actions: object,
    batch: ContentBatch,
) -> BatchStateObservation:
    """Classify canonical destinations as exact prior/final or unsafe mixed state."""

    try:
        actions = consolidation_plan.validate_content_actions(content_actions)
        partition = consolidation_plan.derive_journal_batch_partition(actions)
        batches = _content_batches(partition)
        if not 0 <= batch.ordinal < len(batches) or batches[batch.ordinal] != batch:
            _fail()
        selected = tuple(
            action for action in actions if action["batch_ordinal"] == batch.ordinal
        )
        if (
            len(selected) != batch.action_count
            or selected[0]["ordinal"] != batch.first_action_ordinal
            or selected[-1]["ordinal"] != batch.last_action_ordinal
            or len({action["destination_path"] for action in selected}) != len(selected)
        ):
            _fail()
        with ExitStack() as stack:
            filesystem = stack.enter_context(held_fs.acquire(Path(vault_root)).require())
            observed = tuple(
                _observed_file_state(
                    filesystem,
                    stack,
                    str(action["destination_path"]),
                )
                for action in selected
            )
            prior = all(
                _matches_action_state(action, state, prefix="expected_before")
                for action, state in zip(selected, observed, strict=True)
            )
            final = all(
                _matches_action_state(action, state, prefix="planned_after")
                for action, state in zip(selected, observed, strict=True)
            )
    except PolicyFirstPublicationUnavailable:
        raise
    except (held_fs.HeldFsError, OSError, ValueError):
        _fail()
    state: Literal["prior", "final", "equivalent", "mixed"]
    if prior and final:
        state = "equivalent"
    elif prior:
        state = "prior"
    elif final:
        state = "final"
    else:
        state = "mixed"
    return BatchStateObservation(
        batch_ordinal=batch.ordinal,
        action_count=batch.action_count,
        state=state,
    )


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
        status = journal.batch_status(batch)
        if status not in {"prior", "prepared", "final"}:
            _fail()
        observation = classify_content_batch_state(
            vault_root=Path(vault_root),
            content_actions=content_actions,
            batch=batch,
        )
        if status == "final":
            if observation.state not in {"final", "equivalent"}:
                _fail()
        elif status == "prepared" and observation.state in {"final", "equivalent"}:
            journal.commit_batch(batch)
        else:
            if observation.state not in {"prior", "equivalent"}:
                _fail()
            if status == "prior":
                journal.prepare_batch(batch)
            if observation.state != "equivalent":
                actions = consolidation_plan.validate_content_actions(content_actions)
                selected = tuple(
                    action
                    for action in actions
                    if action["batch_ordinal"] == batch.ordinal
                )
                writes = _validated_batch_writes(
                    vault_root=Path(vault_root),
                    actions=selected,
                    writes=materialize_batch(batch),
                )
                vault.batch_atomic_write(
                    writes,
                    vault_root=Path(vault_root),
                    post_commit_fanout=False,
                )
                observation = classify_content_batch_state(
                    vault_root=Path(vault_root),
                    content_actions=content_actions,
                    batch=batch,
                )
                if observation.state not in {"final", "equivalent"}:
                    _fail()
            journal.commit_batch(batch)
        committed.append(batch.ordinal)
    return PolicyFirstPublicationResult(
        policy_terminal=terminal,
        partition_digest=partition.digest,
        publication_boundary_ordinal=batches[0].ordinal,
        committed_batch_ordinals=tuple(committed),
    )
