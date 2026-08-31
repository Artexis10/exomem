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
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, NoReturn, Protocol

from .. import held_fs, reserved_paths, vault
from . import (
    consolidation_plan,
    consolidation_receipts,
    consolidation_seal,
    policy_publication,
)

if TYPE_CHECKING:
    from . import consolidation_effect_coordinator

POLICY_ACTIVATION_TERMINAL_SCHEMA = (
    "exomem.consolidation-policy-activation-terminal/v1"
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_COMMITTED_EVENT = re.compile(r"([0-9a-f]{64}):committed\Z")
_POLICY_GATE_PHASES = frozenset(
    {"policy-active", "publishing", "rebuilding", "verifying"}
)
_DEFAULT_POLICY_GATE_PHASES = frozenset({"policy-active", "publishing"})
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
    "publish_content_batch_receipt_first",
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


def _consolidation_receipt_event(
    records: list[dict[str, object]],
    *,
    event_id: str,
    phase: Literal["intent", "committed"],
) -> Mapping[str, object]:
    matching = [
        record
        for record in records
        if record.get("event_type") == "consolidation"
        and record.get("phase") == phase
        and record.get("event_id") == event_id
    ]
    if len(matching) != 1:
        _fail()
    try:
        return consolidation_receipts.validate_nested(
            matching[0].get("consolidation_event"),
            outer_phase=phase,
        )
    except consolidation_receipts.ConsolidationReceiptUnavailable:
        _fail()


def _verify_policy_terminal_receipt(
    *,
    vault_root: Path,
    vault_binding_digest: str,
    terminal: object,
    expected_policy_fingerprint: str,
    allowed_seal_phases: frozenset[str] = _DEFAULT_POLICY_GATE_PHASES,
) -> PolicyActivationTerminal:
    """Bind the content gate to the exact durable policy receipt chain."""

    if (
        not isinstance(allowed_seal_phases, frozenset)
        or not allowed_seal_phases
        or not allowed_seal_phases <= _POLICY_GATE_PHASES
    ):
        _fail()
    checked = _policy_terminal(
        terminal,
        expected_policy_fingerprint=expected_policy_fingerprint,
    )
    try:
        records = consolidation_receipts._active_records(  # noqa: SLF001
            Path(vault_root)
        )
    except consolidation_receipts.ConsolidationReceiptUnavailable:
        _fail()
    active_intent = _consolidation_receipt_event(
        records,
        event_id=checked.intent_event_id,
        phase="intent",
    )
    active_terminal = _consolidation_receipt_event(
        records,
        event_id=checked.terminal_event_id,
        phase="committed",
    )
    prepare_terminal = _consolidation_receipt_event(
        records,
        event_id=str(active_intent["semantic_parent_event_id"]),
        phase="committed",
    )
    active_evidence = active_intent["evidence"]
    terminal_evidence = active_terminal["evidence"]
    if (
        not isinstance(active_evidence, Mapping)
        or not isinstance(terminal_evidence, Mapping)
        or active_intent["kind"] != "policy-active"
        or active_intent["target_digest"] != checked.active_fingerprint
        or active_evidence.get("policy_active_digest")
        != checked.active_fingerprint
        or active_evidence.get("policy_fingerprint")
        != checked.policy_fingerprint
        or active_terminal["kind"] != "policy-active"
        or active_terminal["target_digest"] != checked.active_fingerprint
        or active_terminal["observed_digest"] != checked.active_fingerprint
        or terminal_evidence != active_evidence
        or active_terminal["semantic_parent_event_id"]
        != checked.intent_event_id
        or active_terminal["semantic_parent_payload_digest"]
        != active_intent["payload_digest"]
        or prepare_terminal["kind"] != "policy-prepare"
        or prepare_terminal["target_digest"] != checked.prepared_fingerprint
        or prepare_terminal["observed_digest"]
        != checked.prepared_fingerprint
        or prepare_terminal["payload_digest"]
        != active_intent["semantic_parent_payload_digest"]
        or prepare_terminal["run_id"] != active_intent["run_id"]
        or prepare_terminal["operation_id"] != active_intent["operation_id"]
        or prepare_terminal["request_digest"] != active_intent["request_digest"]
        or int(prepare_terminal["effect_ordinal"]) + 1
        != int(active_intent["effect_ordinal"])
    ):
        _fail()
    try:
        seal = consolidation_seal.ConsolidationSealStore(vault_root).load(
            vault_binding_digest=_digest(vault_binding_digest),
        )
        current_now = int(time.time())
        if current_now < 0:
            _fail()
        _custody, active_snapshot = (
            policy_publication.load_active_authority_snapshot(
                vault_root,
                now=current_now,
            )
        )
    except (
        consolidation_seal.ConsolidationSealUnavailable,
        policy_publication.GovernanceError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        _fail()
    if (
        seal.kind != "consolidation-sealed"
        or seal.phase not in allowed_seal_phases
        or seal.run_id != active_intent["run_id"]
        or seal.operation_id != active_intent["operation_id"]
        or active_snapshot.active.policy_fingerprint
        != checked.policy_fingerprint
        or active_snapshot.policy.fingerprint != checked.policy_fingerprint
    ):
        _fail()
    return checked


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


def _validated_batch_removals(
    *,
    vault_root: Path,
    actions: tuple[Mapping[str, object], ...],
) -> tuple[tuple[str, held_fs.StableIdentity, str], ...]:
    removals: list[tuple[str, held_fs.StableIdentity, str]] = []
    for action in actions:
        before = action["expected_before_state"]
        after = action["planned_after_state"]
        kind = action["action"]
        if after == "absent":
            if before != "present" or kind != "remove":
                _fail()
            relative = str(action["destination_path"])
            try:
                identity = reserved_paths.inspect_generic_file(vault_root, relative)
            except reserved_paths.ReservedPathLeafError:
                _fail()
            removals.append(
                (relative, identity, _digest(action["expected_before_sha256"]))
            )
        elif kind == "remove":
            _fail()
    return tuple(removals)


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
    vault_binding_digest: str,
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
    terminal = _verify_policy_terminal_receipt(
        vault_root=Path(vault_root),
        vault_binding_digest=vault_binding_digest,
        terminal=activate_policy(),
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


def _content_batch_classification_digest(batch: ContentBatch) -> str:
    value = {
        "schema": "exomem.consolidation-content-batch-classification/v1",
        "batch_ordinal": batch.ordinal,
        "action_set_digest": batch.action_set_digest,
        "prior_fingerprint": batch.prior_fingerprint,
        "prepared_fingerprint": batch.prepared_fingerprint,
        "final_fingerprint": batch.final_fingerprint,
    }
    encoded = consolidation_plan.canonical_closed_jcs(value)
    domain = b"exomem.consolidation-content-batch-classification/v1"
    framed = (
        len(domain).to_bytes(4, "big")
        + domain
        + len(encoded).to_bytes(8, "big")
        + encoded
    )
    return hashlib.sha256(framed).hexdigest()


def publish_content_batch_receipt_first(
    *,
    content_actions: object,
    batch: ContentBatch,
    event: consolidation_receipts.ConsolidationEvent,
    journal: consolidation_effect_coordinator.ConsolidationEffectJournalStore,
    vault_root: Path,
    materialize_batch: Callable[[ContentBatch], Iterable[vault.PlannedWrite]],
    timestamp: str | None = None,
) -> consolidation_effect_coordinator.EffectExecutionResult:
    """Publish one real content batch through the receipt-first effect engine."""

    from . import consolidation_effect_coordinator

    partition = consolidation_plan.derive_journal_batch_partition(content_actions)
    batches = _content_batches(partition)
    if (
        not isinstance(batch, ContentBatch)
        or not 0 <= batch.ordinal < len(batches)
        or batches[batch.ordinal] != batch
        or not isinstance(event, consolidation_receipts.ConsolidationEvent)
    ):
        _fail()
    try:
        payload = consolidation_receipts.validate_nested(
            event.payload,
            outer_phase="intent",
        )
    except consolidation_receipts.ConsolidationReceiptUnavailable:
        _fail()
    evidence = payload.get("evidence")
    if (
        payload.get("kind") != "content-batch"
        or payload.get("phase") != "publishing"
        or payload.get("batch_ordinal") != batch.ordinal
        or payload.get("prior_digest") != batch.prior_fingerprint
        or payload.get("prepared_digest") != batch.prepared_fingerprint
        or payload.get("target_digest") != batch.final_fingerprint
        or not isinstance(evidence, Mapping)
        or evidence.get("batch_manifest_digest") != batch.action_set_digest
        or evidence.get("classification_digest")
        != _content_batch_classification_digest(batch)
    ):
        _fail()

    def classify_observation(
        *,
        equivalent_as: Literal["prior", "target"],
    ) -> consolidation_effect_coordinator.EffectObservation:
        observed = classify_content_batch_state(
            vault_root=Path(vault_root),
            content_actions=content_actions,
            batch=batch,
        )
        if observed.state == "prior" or (
            observed.state == "equivalent" and equivalent_as == "prior"
        ):
            return consolidation_effect_coordinator.EffectObservation(
                state="prior",
                digest=batch.prior_fingerprint,
            )
        if observed.state in {"final", "equivalent"}:
            return consolidation_effect_coordinator.EffectObservation(
                state="target",
                digest=batch.final_fingerprint,
            )
        return consolidation_effect_coordinator.EffectObservation(
            state="mixed",
            digest=_content_batch_classification_digest(batch),
        )

    def classify() -> consolidation_effect_coordinator.EffectObservation:
        return classify_observation(equivalent_as="target")

    def classify_unprepared() -> consolidation_effect_coordinator.EffectObservation:
        return classify_observation(equivalent_as="prior")

    def apply() -> None:
        actions = consolidation_plan.validate_content_actions(content_actions)
        selected = tuple(
            action for action in actions if action["batch_ordinal"] == batch.ordinal
        )
        writes = _validated_batch_writes(
            vault_root=Path(vault_root),
            actions=selected,
            writes=materialize_batch(batch),
        )
        removals = _validated_batch_removals(
            vault_root=Path(vault_root),
            actions=selected,
        )
        vault.batch_atomic_write(
            writes,
            vault_root=Path(vault_root),
            post_commit_fanout=False,
        )
        for relative, identity, expected_sha256 in removals:
            reserved_paths.unlink_generic_file(
                Path(vault_root),
                relative,
                expected_identity=identity,
                expected_sha256=expected_sha256,
            )

    return consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
        vault_root=Path(vault_root),
        event=event,
        journal=journal,
        classify=classify,
        classify_unprepared=classify_unprepared,
        apply_effect=apply,
        timestamp=timestamp,
    )
