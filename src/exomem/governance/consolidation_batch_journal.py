"""Durable exact-state journal for consolidation content batches."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn

from .. import reserved_paths
from ..kbdir import kb_dirname
from . import consolidation_plan, consolidation_saga

JOURNAL_SCHEMA = "exomem.consolidation-content-batch-journal/v1"

_DESCRIPTOR_ID = "consolidation-tree"
_OWNER = "consolidation.run"
_BINDING_SCHEMA = "exomem.consolidation-content-batch-journal-binding/v1"
_BINDING_DOMAIN = _BINDING_SCHEMA.encode("ascii")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_STATUSES = frozenset({"prior", "prepared", "final"})
_MAX_JOURNAL_BYTES = 16 * 1024 * 1024
_MAX_SAFE_INTEGER = (1 << 53) - 1
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
        "status",
    }
)
_RECORD_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "operation_id",
        "request_digest",
        "partition_digest",
        "binding_digest",
        "revision",
        "publication_boundary_ordinal",
        "publication_boundary_committed",
        "batches",
    }
)

__all__ = [
    "JOURNAL_SCHEMA",
    "ConsolidationBatchJournalEntry",
    "ConsolidationBatchJournalState",
    "ConsolidationBatchJournalStore",
    "ConsolidationBatchJournalUnavailable",
]


class ConsolidationBatchJournalUnavailable(RuntimeError):
    """Stable content-free refusal for a missing, changed, or corrupt journal."""

    code = "CONSOLIDATION_BATCH_JOURNAL_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("consolidation batch journal is unavailable")


@dataclass(frozen=True, slots=True)
class ConsolidationBatchJournalEntry:
    ordinal: int
    first_action_ordinal: int
    last_action_ordinal: int
    action_count: int
    publication_boundary: bool
    action_set_digest: str
    prior_fingerprint: str
    prepared_fingerprint: str
    final_fingerprint: str
    status: str


@dataclass(frozen=True, slots=True)
class ConsolidationBatchJournalState:
    schema: str
    run_id: str
    operation_id: str
    request_digest: str
    partition_digest: str
    binding_digest: str
    revision: int
    publication_boundary_ordinal: int
    publication_boundary_committed: bool
    batches: tuple[ConsolidationBatchJournalEntry, ...]
    state_digest: str


def _fail() -> NoReturn:
    raise ConsolidationBatchJournalUnavailable from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _uuid4(value: object) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        _fail()
    return value


def _integer(
    value: object,
    *,
    minimum: int = 0,
    maximum: int = _MAX_SAFE_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail()
    return value


def _mapping(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        _fail()
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _fail()
        value[key] = item
    return value


def _reject_number(_value: str) -> NoReturn:
    _fail()


def _parse_integer(value: str) -> int:
    if value.startswith("-"):
        _fail()
    try:
        parsed = int(value)
    except ValueError:
        _fail()
    return _integer(parsed)


def _decode(raw: bytes) -> Mapping[str, object]:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_JOURNAL_BYTES:
        _fail()
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_int=_parse_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ConsolidationBatchJournalUnavailable):
        _fail()
    if not isinstance(value, Mapping):
        _fail()
    try:
        canonical = consolidation_plan.canonical_closed_jcs(value)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    if canonical != raw:
        _fail()
    return value


def _binding_digest(
    *,
    run_id: str,
    operation_id: str,
    request_digest: str,
    partition_digest: str,
) -> str:
    payload = consolidation_plan.canonical_closed_jcs(
        {
            "schema": _BINDING_SCHEMA,
            "run_id": _uuid4(run_id),
            "operation_id": _uuid4(operation_id),
            "request_digest": _digest(request_digest),
            "partition_digest": _digest(partition_digest),
        }
    )
    framed = (
        len(_BINDING_DOMAIN).to_bytes(4, "big")
        + _BINDING_DOMAIN
        + len(payload).to_bytes(8, "big")
        + payload
    )
    return hashlib.sha256(framed).hexdigest()


def _entry_value(entry: ConsolidationBatchJournalEntry) -> dict[str, object]:
    return {
        "batch_ordinal": entry.ordinal,
        "first_action_ordinal": entry.first_action_ordinal,
        "last_action_ordinal": entry.last_action_ordinal,
        "action_count": entry.action_count,
        "publication_boundary": entry.publication_boundary,
        "action_set_digest": entry.action_set_digest,
        "prior_fingerprint": entry.prior_fingerprint,
        "prepared_fingerprint": entry.prepared_fingerprint,
        "final_fingerprint": entry.final_fingerprint,
        "status": entry.status,
    }


def _partition_value(
    entries: Sequence[ConsolidationBatchJournalEntry],
) -> dict[str, object]:
    return {
        "schema": consolidation_plan.JOURNAL_BATCH_PARTITION_SCHEMA,
        "action_count": sum(entry.action_count for entry in entries),
        "batch_count": len(entries),
        "batches": tuple(
            {
                key: value
                for key, value in _entry_value(entry).items()
                if key != "status"
            }
            for entry in entries
        ),
    }


def _state_value(state: ConsolidationBatchJournalState) -> dict[str, object]:
    return {
        "schema": state.schema,
        "run_id": state.run_id,
        "operation_id": state.operation_id,
        "request_digest": state.request_digest,
        "partition_digest": state.partition_digest,
        "binding_digest": state.binding_digest,
        "revision": state.revision,
        "publication_boundary_ordinal": state.publication_boundary_ordinal,
        "publication_boundary_committed": state.publication_boundary_committed,
        "batches": tuple(_entry_value(entry) for entry in state.batches),
    }


def _state_bytes(state: ConsolidationBatchJournalState) -> bytes:
    try:
        return consolidation_plan.canonical_closed_jcs(_state_value(state))
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()


def _entries_from_partition(
    partition: consolidation_plan.CanonicalObject,
) -> tuple[ConsolidationBatchJournalEntry, ...]:
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
    rows = checked.preimage["batches"]
    if not isinstance(rows, tuple):
        _fail()
    return tuple(
        ConsolidationBatchJournalEntry(
            ordinal=_integer(row["batch_ordinal"]),
            first_action_ordinal=_integer(row["first_action_ordinal"]),
            last_action_ordinal=_integer(row["last_action_ordinal"]),
            action_count=_integer(
                row["action_count"],
                minimum=1,
                maximum=consolidation_plan.MAX_CONTENT_BATCH_ACTIONS,
            ),
            publication_boundary=row["publication_boundary"] is True,
            action_set_digest=_digest(row["action_set_digest"]),
            prior_fingerprint=_digest(row["prior_fingerprint"]),
            prepared_fingerprint=_digest(row["prepared_fingerprint"]),
            final_fingerprint=_digest(row["final_fingerprint"]),
            status="prior",
        )
        for row in rows
        if isinstance(row, Mapping)
    )


def _parse_entry(value: object, *, expected_ordinal: int) -> ConsolidationBatchJournalEntry:
    row = _mapping(value, _BATCH_FIELDS)
    ordinal = _integer(row["batch_ordinal"])
    count = _integer(
        row["action_count"],
        minimum=1,
        maximum=consolidation_plan.MAX_CONTENT_BATCH_ACTIONS,
    )
    first = _integer(row["first_action_ordinal"])
    last = _integer(row["last_action_ordinal"])
    status = row["status"]
    if (
        ordinal != expected_ordinal
        or first > last
        or last != first + count - 1
        or row["publication_boundary"] is not (ordinal == 0)
        or not isinstance(status, str)
        or status not in _STATUSES
    ):
        _fail()
    return ConsolidationBatchJournalEntry(
        ordinal=ordinal,
        first_action_ordinal=first,
        last_action_ordinal=last,
        action_count=count,
        publication_boundary=row["publication_boundary"] is True,
        action_set_digest=_digest(row["action_set_digest"]),
        prior_fingerprint=_digest(row["prior_fingerprint"]),
        prepared_fingerprint=_digest(row["prepared_fingerprint"]),
        final_fingerprint=_digest(row["final_fingerprint"]),
        status=status,
    )


def _parse_state(raw: bytes) -> ConsolidationBatchJournalState:
    value = _mapping(_decode(raw), _RECORD_FIELDS)
    if value["schema"] != JOURNAL_SCHEMA:
        _fail()
    rows = value["batches"]
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence) or not rows:
        _fail()
    entries = tuple(
        _parse_entry(row, expected_ordinal=ordinal)
        for ordinal, row in enumerate(rows)
    )
    if any(
        entry.first_action_ordinal
        != (0 if index == 0 else entries[index - 1].last_action_ordinal + 1)
        for index, entry in enumerate(entries)
    ):
        _fail()
    status_rank = {"final": 0, "prepared": 1, "prior": 2}
    ranks = tuple(status_rank[entry.status] for entry in entries)
    if tuple(sorted(ranks)) != ranks or ranks.count(1) > 1:
        _fail()
    expected_revision = 1 + sum(
        2 if entry.status == "final" else 1 if entry.status == "prepared" else 0
        for entry in entries
    )
    run_id = _uuid4(value["run_id"])
    operation_id = _uuid4(value["operation_id"])
    request_digest = _digest(value["request_digest"])
    partition_digest = _digest(value["partition_digest"])
    try:
        partition = consolidation_plan.parse_journal_batch_partition(
            consolidation_plan.canonical_closed_jcs(_partition_value(entries))
        )
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    if partition.digest != partition_digest:
        _fail()
    binding_digest = _binding_digest(
        run_id=run_id,
        operation_id=operation_id,
        request_digest=request_digest,
        partition_digest=partition_digest,
    )
    boundary_ordinal = _integer(value["publication_boundary_ordinal"])
    boundary_committed = value["publication_boundary_committed"]
    if (
        value["binding_digest"] != binding_digest
        or boundary_ordinal != 0
        or type(boundary_committed) is not bool
        or boundary_committed is not (entries[0].status == "final")
    ):
        _fail()
    return ConsolidationBatchJournalState(
        schema=JOURNAL_SCHEMA,
        run_id=run_id,
        operation_id=operation_id,
        request_digest=request_digest,
        partition_digest=partition_digest,
        binding_digest=binding_digest,
        revision=_integer(value["revision"], minimum=expected_revision, maximum=expected_revision),
        publication_boundary_ordinal=boundary_ordinal,
        publication_boundary_committed=boundary_committed,
        batches=entries,
        state_digest=hashlib.sha256(raw).hexdigest(),
    )


def _content_batch(value: object) -> consolidation_saga.ContentBatch:
    if not isinstance(value, consolidation_saga.ContentBatch):
        _fail()
    return value


def _entry_matches_batch(
    entry: ConsolidationBatchJournalEntry,
    batch: consolidation_saga.ContentBatch,
) -> bool:
    return (
        entry.ordinal == batch.ordinal
        and entry.first_action_ordinal == batch.first_action_ordinal
        and entry.last_action_ordinal == batch.last_action_ordinal
        and entry.action_count == batch.action_count
        and entry.publication_boundary is batch.publication_boundary
        and entry.action_set_digest == batch.action_set_digest
        and entry.prior_fingerprint == batch.prior_fingerprint
        and entry.prepared_fingerprint == batch.prepared_fingerprint
        and entry.final_fingerprint == batch.final_fingerprint
    )


@contextmanager
def _authority(vault_root: Path, *, mutation: bool) -> Iterator[None]:
    try:
        with reserved_paths._subsystem_authority_scope(_OWNER):  # noqa: SLF001
            with reserved_paths._identity_coordination_scope(  # noqa: SLF001
                vault_root,
                descriptor_ids=(_DESCRIPTOR_ID,),
                identity_may_change=mutation,
            ):
                yield
    except ConsolidationBatchJournalUnavailable:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail()


class ConsolidationBatchJournalStore:
    """CAS-persist one run's immutable batch plan and exact transition states."""

    def __init__(self, vault_root: Path | str, *, run_id: str):
        self.vault_root = Path(vault_root).absolute()
        self.run_id = _uuid4(run_id)
        self.path = (
            self.vault_root
            / kb_dirname()
            / "_Consolidation"
            / "runs"
            / self.run_id
            / "content-batches.json"
        )

    def _read(self) -> bytes:
        return reserved_paths._read_owner_bytes(  # noqa: SLF001
            self.vault_root,
            self.path,
            _DESCRIPTOR_ID,
            limit=_MAX_JOURNAL_BYTES,
        )

    def _read_optional(self) -> bytes | None:
        try:
            return self._read()
        except FileNotFoundError:
            return None

    def _publish(
        self,
        raw: bytes,
        *,
        expected_sha256: str | None = None,
        require_missing: bool = False,
    ) -> None:
        reserved_paths._publish_owner_bytes(  # noqa: SLF001
            self.vault_root,
            self.path,
            _DESCRIPTOR_ID,
            raw,
            expected_sha256=expected_sha256,
            require_missing=require_missing,
        )

    def _load_locked(self) -> tuple[ConsolidationBatchJournalState, bytes]:
        try:
            raw = self._read()
        except FileNotFoundError:
            _fail()
        state = _parse_state(raw)
        if state.run_id != self.run_id:
            _fail()
        return state, raw

    def create(
        self,
        *,
        operation_id: str,
        request_digest: str,
        partition: consolidation_plan.CanonicalObject,
    ) -> ConsolidationBatchJournalState:
        operation_id = _uuid4(operation_id)
        request_digest = _digest(request_digest)
        entries = _entries_from_partition(partition)
        if not entries:
            _fail()
        binding_digest = _binding_digest(
            run_id=self.run_id,
            operation_id=operation_id,
            request_digest=request_digest,
            partition_digest=partition.digest,
        )
        candidate = ConsolidationBatchJournalState(
            schema=JOURNAL_SCHEMA,
            run_id=self.run_id,
            operation_id=operation_id,
            request_digest=request_digest,
            partition_digest=partition.digest,
            binding_digest=binding_digest,
            revision=1,
            publication_boundary_ordinal=0,
            publication_boundary_committed=False,
            batches=entries,
            state_digest="0" * 64,
        )
        raw = _state_bytes(candidate)
        target = replace(candidate, state_digest=hashlib.sha256(raw).hexdigest())
        with _authority(self.vault_root, mutation=True):
            existing = self._read_optional()
            if existing is None:
                self._publish(raw, require_missing=True)
                return target
            current = _parse_state(existing)
            if current.run_id != self.run_id:
                _fail()
            if current == target:
                return current
            _fail()

    def load(self) -> ConsolidationBatchJournalState:
        with _authority(self.vault_root, mutation=False):
            state, _raw = self._load_locked()
            return state

    def batch_status(self, batch_value: consolidation_saga.ContentBatch) -> str:
        batch = _content_batch(batch_value)
        with _authority(self.vault_root, mutation=False):
            current, _raw = self._load_locked()
            if not 0 <= batch.ordinal < len(current.batches):
                _fail()
            entry = current.batches[batch.ordinal]
            if not _entry_matches_batch(entry, batch):
                _fail()
            return entry.status

    def _transition(
        self,
        batch_value: consolidation_saga.ContentBatch,
        *,
        target_status: str,
    ) -> ConsolidationBatchJournalState:
        batch = _content_batch(batch_value)
        with _authority(self.vault_root, mutation=True):
            current, raw = self._load_locked()
            if not 0 <= batch.ordinal < len(current.batches):
                _fail()
            entry = current.batches[batch.ordinal]
            if not _entry_matches_batch(entry, batch):
                _fail()
            if entry.status == target_status:
                return current
            if target_status == "prepared":
                if entry.status == "final":
                    return current
                if entry.status != "prior" or any(
                    prior.status != "final"
                    for prior in current.batches[: batch.ordinal]
                ):
                    _fail()
            elif target_status == "final":
                if entry.status != "prepared":
                    _fail()
            else:  # pragma: no cover - the public methods use the closed pair
                _fail()
            entries = list(current.batches)
            entries[batch.ordinal] = replace(entry, status=target_status)
            candidate = replace(
                current,
                revision=current.revision + 1,
                publication_boundary_committed=(
                    current.publication_boundary_committed
                    or (batch.publication_boundary and target_status == "final")
                ),
                batches=tuple(entries),
                state_digest="0" * 64,
            )
            target_raw = _state_bytes(candidate)
            target = replace(
                candidate,
                state_digest=hashlib.sha256(target_raw).hexdigest(),
            )
            self._publish(
                target_raw,
                expected_sha256=hashlib.sha256(raw).hexdigest(),
            )
            return target

    def prepare_batch(
        self,
        batch: consolidation_saga.ContentBatch,
    ) -> ConsolidationBatchJournalState:
        return self._transition(batch, target_status="prepared")

    def commit_batch(
        self,
        batch: consolidation_saga.ContentBatch,
    ) -> ConsolidationBatchJournalState:
        return self._transition(batch, target_status="final")
