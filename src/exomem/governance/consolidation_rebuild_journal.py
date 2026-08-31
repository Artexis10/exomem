"""Durable exact-state journal for consolidation derivative rebuilds."""

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
from . import consolidation_plan, consolidation_rebuild

JOURNAL_SCHEMA = "exomem.consolidation-rebuild-journal/v1"

_DESCRIPTOR_ID = "consolidation-tree"
_OWNER = "consolidation.run"
_BINDING_SCHEMA = "exomem.consolidation-rebuild-journal-binding/v1"
_BINDING_DOMAIN = _BINDING_SCHEMA.encode("ascii")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_ID = re.compile(r"[0-9a-f]{64}:committed\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_MAX_JOURNAL_BYTES = 512 * 1024
_MAX_SAFE_INTEGER = (1 << 53) - 1
_STATUSES = frozenset({"prior", "prepared", "final"})
_BASIS_FIELDS = frozenset(
    {
        "run_id",
        "operation_id",
        "request_digest",
        "plan_digest",
        "partition_digest",
        "content_batch_journal_digest",
        "content_effects_digest",
        "last_content_terminal_event_id",
        "last_content_terminal_payload_digest",
        "canonical_census_digest",
    }
)
_RECORD_FIELDS = _BASIS_FIELDS | frozenset({"schema", "binding_digest", "revision", "components"})
_PRIOR_FIELDS = frozenset({"component", "status"})
_PREPARED_FIELDS = _PRIOR_FIELDS | frozenset({"artifact_fingerprint"})
_FINAL_FIELDS = _PREPARED_FIELDS | frozenset(
    {"terminal_event_id", "terminal_payload_digest", "effect_journal_digest"}
)

__all__ = [
    "JOURNAL_SCHEMA",
    "ConsolidationRebuildJournalEntry",
    "ConsolidationRebuildJournalState",
    "ConsolidationRebuildJournalStore",
    "ConsolidationRebuildJournalUnavailable",
]


class ConsolidationRebuildJournalUnavailable(RuntimeError):
    """Stable content-free refusal for a missing, changed, or corrupt journal."""

    code = "CONSOLIDATION_REBUILD_JOURNAL_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("consolidation rebuild journal is unavailable")


@dataclass(frozen=True, slots=True)
class ConsolidationRebuildJournalEntry:
    component: str
    status: str
    artifact_fingerprint: str | None
    terminal_event_id: str | None
    terminal_payload_digest: str | None
    effect_journal_digest: str | None


@dataclass(frozen=True, slots=True)
class ConsolidationRebuildJournalState:
    schema: str
    run_id: str
    operation_id: str
    request_digest: str
    plan_digest: str
    partition_digest: str
    content_batch_journal_digest: str
    content_effects_digest: str
    last_content_terminal_event_id: str
    last_content_terminal_payload_digest: str
    canonical_census_digest: str
    binding_digest: str
    revision: int
    components: tuple[ConsolidationRebuildJournalEntry, ...]
    state_digest: str


def _fail() -> NoReturn:
    raise ConsolidationRebuildJournalUnavailable from None


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _event_id(value: object) -> str:
    if type(value) is not str or _EVENT_ID.fullmatch(value) is None:
        _fail()
    return value


def _uuid4(value: object) -> str:
    if type(value) is not str or _UUID4.fullmatch(value) is None:
        _fail()
    return value


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_SAFE_INTEGER:
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
    except (UnicodeDecodeError, json.JSONDecodeError, ConsolidationRebuildJournalUnavailable):
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


def _basis_value(state: ConsolidationRebuildJournalState) -> dict[str, object]:
    return {
        "run_id": state.run_id,
        "operation_id": state.operation_id,
        "request_digest": state.request_digest,
        "plan_digest": state.plan_digest,
        "partition_digest": state.partition_digest,
        "content_batch_journal_digest": state.content_batch_journal_digest,
        "content_effects_digest": state.content_effects_digest,
        "last_content_terminal_event_id": state.last_content_terminal_event_id,
        "last_content_terminal_payload_digest": state.last_content_terminal_payload_digest,
        "canonical_census_digest": state.canonical_census_digest,
    }


def _binding_digest(state: ConsolidationRebuildJournalState) -> str:
    try:
        payload = consolidation_plan.canonical_closed_jcs(
            {"schema": _BINDING_SCHEMA, **_basis_value(state)}
        )
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    framed = (
        len(_BINDING_DOMAIN).to_bytes(4, "big")
        + _BINDING_DOMAIN
        + len(payload).to_bytes(8, "big")
        + payload
    )
    return hashlib.sha256(framed).hexdigest()


def _entry_value(entry: ConsolidationRebuildJournalEntry) -> dict[str, object]:
    value: dict[str, object] = {"component": entry.component, "status": entry.status}
    if entry.status in {"prepared", "final"}:
        value["artifact_fingerprint"] = entry.artifact_fingerprint
    if entry.status == "final":
        value["terminal_event_id"] = entry.terminal_event_id
        value["terminal_payload_digest"] = entry.terminal_payload_digest
        value["effect_journal_digest"] = entry.effect_journal_digest
    return value


def _state_value(state: ConsolidationRebuildJournalState) -> dict[str, object]:
    return {
        "schema": state.schema,
        **_basis_value(state),
        "binding_digest": state.binding_digest,
        "revision": state.revision,
        "components": tuple(_entry_value(entry) for entry in state.components),
    }


def _state_bytes(state: ConsolidationRebuildJournalState) -> bytes:
    try:
        return consolidation_plan.canonical_closed_jcs(_state_value(state))
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()


def _parse_entry(value: object, *, component: str) -> ConsolidationRebuildJournalEntry:
    if not isinstance(value, Mapping):
        _fail()
    status = value.get("status")
    if status not in _STATUSES:
        _fail()
    fields = {
        "prior": _PRIOR_FIELDS,
        "prepared": _PREPARED_FIELDS,
        "final": _FINAL_FIELDS,
    }[status]
    row = _mapping(value, fields)
    if row["component"] != component:
        _fail()
    if status == "prior":
        return ConsolidationRebuildJournalEntry(
            component=component,
            status=status,
            artifact_fingerprint=None,
            terminal_event_id=None,
            terminal_payload_digest=None,
            effect_journal_digest=None,
        )
    artifact = _digest(row["artifact_fingerprint"])
    if status == "prepared":
        return ConsolidationRebuildJournalEntry(
            component=component,
            status=status,
            artifact_fingerprint=artifact,
            terminal_event_id=None,
            terminal_payload_digest=None,
            effect_journal_digest=None,
        )
    return ConsolidationRebuildJournalEntry(
        component=component,
        status=status,
        artifact_fingerprint=artifact,
        terminal_event_id=_event_id(row["terminal_event_id"]),
        terminal_payload_digest=_digest(row["terminal_payload_digest"]),
        effect_journal_digest=_digest(row["effect_journal_digest"]),
    )


def _parse_state(raw: bytes) -> ConsolidationRebuildJournalState:
    value = _mapping(_decode(raw), _RECORD_FIELDS)
    if value["schema"] != JOURNAL_SCHEMA:
        _fail()
    rows = value["components"]
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        _fail()
    if len(rows) != len(consolidation_rebuild.DERIVATIVE_COMPONENTS):
        _fail()
    entries = tuple(
        _parse_entry(row, component=component)
        for row, component in zip(rows, consolidation_rebuild.DERIVATIVE_COMPONENTS, strict=True)
    )
    status_rank = {"final": 0, "prepared": 1, "prior": 2}
    ranks = tuple(status_rank[entry.status] for entry in entries)
    if tuple(sorted(ranks)) != ranks or ranks.count(1) > 1:
        _fail()
    expected_revision = 1 + sum(
        2 if entry.status == "final" else 1 if entry.status == "prepared" else 0
        for entry in entries
    )
    state = ConsolidationRebuildJournalState(
        schema=JOURNAL_SCHEMA,
        run_id=_uuid4(value["run_id"]),
        operation_id=_uuid4(value["operation_id"]),
        request_digest=_digest(value["request_digest"]),
        plan_digest=_digest(value["plan_digest"]),
        partition_digest=_digest(value["partition_digest"]),
        content_batch_journal_digest=_digest(value["content_batch_journal_digest"]),
        content_effects_digest=_digest(value["content_effects_digest"]),
        last_content_terminal_event_id=_event_id(value["last_content_terminal_event_id"]),
        last_content_terminal_payload_digest=_digest(value["last_content_terminal_payload_digest"]),
        canonical_census_digest=_digest(value["canonical_census_digest"]),
        binding_digest=_digest(value["binding_digest"]),
        revision=_integer(value["revision"], minimum=expected_revision),
        components=entries,
        state_digest=hashlib.sha256(raw).hexdigest(),
    )
    if state.revision != expected_revision or state.binding_digest != _binding_digest(state):
        _fail()
    return state


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
    except ConsolidationRebuildJournalUnavailable:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail()


class ConsolidationRebuildJournalStore:
    """CAS-persist one rebuild's immutable basis and component terminals."""

    def __init__(self, vault_root: Path | str, *, run_id: str):
        self.vault_root = Path(vault_root).absolute()
        self.run_id = _uuid4(run_id)
        self.path = (
            self.vault_root
            / kb_dirname()
            / "_Consolidation"
            / "runs"
            / self.run_id
            / "rebuild.json"
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

    def _load_locked(self) -> tuple[ConsolidationRebuildJournalState, bytes]:
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
        plan_digest: str,
        partition_digest: str,
        content_batch_journal_digest: str,
        content_effects_digest: str,
        last_content_terminal_event_id: str,
        last_content_terminal_payload_digest: str,
        canonical_census_digest: str,
    ) -> ConsolidationRebuildJournalState:
        candidate = ConsolidationRebuildJournalState(
            schema=JOURNAL_SCHEMA,
            run_id=self.run_id,
            operation_id=_uuid4(operation_id),
            request_digest=_digest(request_digest),
            plan_digest=_digest(plan_digest),
            partition_digest=_digest(partition_digest),
            content_batch_journal_digest=_digest(content_batch_journal_digest),
            content_effects_digest=_digest(content_effects_digest),
            last_content_terminal_event_id=_event_id(last_content_terminal_event_id),
            last_content_terminal_payload_digest=_digest(last_content_terminal_payload_digest),
            canonical_census_digest=_digest(canonical_census_digest),
            binding_digest="0" * 64,
            revision=1,
            components=tuple(
                ConsolidationRebuildJournalEntry(
                    component=component,
                    status="prior",
                    artifact_fingerprint=None,
                    terminal_event_id=None,
                    terminal_payload_digest=None,
                    effect_journal_digest=None,
                )
                for component in consolidation_rebuild.DERIVATIVE_COMPONENTS
            ),
            state_digest="0" * 64,
        )
        candidate = replace(candidate, binding_digest=_binding_digest(candidate))
        raw = _state_bytes(candidate)
        target = replace(candidate, state_digest=hashlib.sha256(raw).hexdigest())
        with _authority(self.vault_root, mutation=True):
            existing = self._read_optional()
            if existing is None:
                self._publish(raw, require_missing=True)
                return target
            current = _parse_state(existing)
            if current.run_id == self.run_id and _basis_value(current) == _basis_value(target):
                return current
            _fail()

    def load(self) -> ConsolidationRebuildJournalState:
        with _authority(self.vault_root, mutation=False):
            state, _raw = self._load_locked()
            return state

    def _component_index(self, component: object) -> int:
        if type(component) is not str:
            _fail()
        try:
            return consolidation_rebuild.DERIVATIVE_COMPONENTS.index(component)
        except ValueError:
            _fail()

    def record_component_result(
        self,
        component: str,
        artifact_fingerprint: str,
    ) -> ConsolidationRebuildJournalState:
        ordinal = self._component_index(component)
        fingerprint = _digest(artifact_fingerprint)
        with _authority(self.vault_root, mutation=True):
            current, raw = self._load_locked()
            entry = current.components[ordinal]
            if entry.status in {"prepared", "final"}:
                if entry.artifact_fingerprint == fingerprint:
                    return current
                _fail()
            if entry.status != "prior" or any(
                prior.status != "final" for prior in current.components[:ordinal]
            ):
                _fail()
            entries = list(current.components)
            entries[ordinal] = replace(
                entry,
                status="prepared",
                artifact_fingerprint=fingerprint,
            )
            return self._replace_locked(current, raw, components=tuple(entries))

    def finalize_component(
        self,
        component: str,
        artifact_fingerprint: str,
        *,
        terminal_event_id: str,
        terminal_payload_digest: str,
        effect_journal_digest: str,
    ) -> ConsolidationRebuildJournalState:
        ordinal = self._component_index(component)
        fingerprint = _digest(artifact_fingerprint)
        event_id = _event_id(terminal_event_id)
        payload_digest = _digest(terminal_payload_digest)
        journal_digest = _digest(effect_journal_digest)
        with _authority(self.vault_root, mutation=True):
            current, raw = self._load_locked()
            entry = current.components[ordinal]
            if entry.status == "final":
                if (
                    entry.artifact_fingerprint == fingerprint
                    and entry.terminal_event_id == event_id
                    and entry.terminal_payload_digest == payload_digest
                    and entry.effect_journal_digest == journal_digest
                ):
                    return current
                _fail()
            if entry.status != "prepared" or entry.artifact_fingerprint != fingerprint:
                _fail()
            entries = list(current.components)
            entries[ordinal] = replace(
                entry,
                status="final",
                terminal_event_id=event_id,
                terminal_payload_digest=payload_digest,
                effect_journal_digest=journal_digest,
            )
            return self._replace_locked(current, raw, components=tuple(entries))

    def _replace_locked(
        self,
        current: ConsolidationRebuildJournalState,
        raw: bytes,
        *,
        components: tuple[ConsolidationRebuildJournalEntry, ...],
    ) -> ConsolidationRebuildJournalState:
        candidate = replace(
            current,
            revision=current.revision + 1,
            components=components,
            state_digest="0" * 64,
        )
        target_raw = _state_bytes(candidate)
        target = replace(candidate, state_digest=hashlib.sha256(target_raw).hexdigest())
        self._publish(target_raw, expected_sha256=hashlib.sha256(raw).hexdigest())
        return target
