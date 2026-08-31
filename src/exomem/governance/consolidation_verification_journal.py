"""Owner-only exact-state journal for consolidation verification probes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, NoReturn

from .. import reserved_paths
from ..kbdir import kb_dirname
from . import consolidation_plan, consolidation_verification

JOURNAL_SCHEMA = "exomem.consolidation-verification-journal/v1"

_DESCRIPTOR_ID = "consolidation-tree"
_OWNER = "consolidation.run"
_BINDING_SCHEMA = "exomem.consolidation-verification-basis/v1"
_BINDING_DOMAIN = _BINDING_SCHEMA.encode("ascii")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_ID = re.compile(r"[0-9a-f]{64}:committed\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_MAX_JOURNAL_BYTES = 2 * 1024 * 1024
_MAX_SAFE_INTEGER = (1 << 53) - 1
_STATUSES = frozenset({"prior", "result", "final"})
_PROBE_BASE_FIELDS = frozenset(
    {
        "schema",
        "ordinal",
        "probe_kind",
        "probe_id",
        "executor_id",
        "contract_digest",
        "expected_result_digest",
        "probe_digest",
        "status",
    }
)
_PROBE_RESULT_FIELDS = _PROBE_BASE_FIELDS | frozenset({"result_digest"})
_PROBE_FINAL_FIELDS = _PROBE_RESULT_FIELDS | frozenset(
    {"terminal_event_id", "terminal_payload_digest", "effect_journal_digest"}
)
_TERMINAL_BASE_FIELDS = frozenset({"status"})
_TERMINAL_RESULT_FIELDS = _TERMINAL_BASE_FIELDS | frozenset({"result_digest"})
_TERMINAL_FINAL_FIELDS = _TERMINAL_RESULT_FIELDS | frozenset(
    {"terminal_event_id", "terminal_payload_digest", "effect_journal_digest"}
)
_BASIS_FIELDS = frozenset(
    {
        "run_id",
        "operation_id",
        "request_digest",
        "plan_digest",
        "rebuild_journal_digest",
        "canonical_census_digest",
        "positive_probe_digest",
        "negative_probe_digest",
        "last_rebuild_terminal_event_id",
        "last_rebuild_terminal_payload_digest",
        "last_rebuild_effect_ordinal",
        "probes",
    }
)
_RECORD_FIELDS = _BASIS_FIELDS | frozenset({"schema", "binding_digest", "revision", "terminal"})

__all__ = [
    "JOURNAL_SCHEMA",
    "ConsolidationVerificationJournalEntry",
    "ConsolidationVerificationJournalState",
    "ConsolidationVerificationJournalStore",
    "ConsolidationVerificationJournalTerminal",
    "ConsolidationVerificationJournalUnavailable",
]


class ConsolidationVerificationJournalUnavailable(RuntimeError):
    """Content-free refusal for a missing, changed, or corrupt journal."""

    code = "CONSOLIDATION_VERIFICATION_JOURNAL_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ConsolidationVerificationJournalEntry:
    probe: consolidation_verification.VerificationProbe
    status: Literal["prior", "result", "final"]
    result_digest: str | None
    terminal_event_id: str | None
    terminal_payload_digest: str | None
    effect_journal_digest: str | None


@dataclass(frozen=True, slots=True)
class ConsolidationVerificationJournalTerminal:
    status: Literal["prior", "result", "final"]
    result_digest: str | None
    terminal_event_id: str | None
    terminal_payload_digest: str | None
    effect_journal_digest: str | None


@dataclass(frozen=True, slots=True)
class ConsolidationVerificationJournalState:
    schema: str
    run_id: str
    operation_id: str
    request_digest: str
    plan_digest: str
    rebuild_journal_digest: str
    canonical_census_digest: str
    positive_probe_digest: str
    negative_probe_digest: str
    last_rebuild_terminal_event_id: str
    last_rebuild_terminal_payload_digest: str
    last_rebuild_effect_ordinal: int
    binding_digest: str
    revision: int
    probes: tuple[ConsolidationVerificationJournalEntry, ...]
    terminal: ConsolidationVerificationJournalTerminal
    state_digest: str


def _fail() -> NoReturn:
    raise ConsolidationVerificationJournalUnavailable from None


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
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _reject_number(_value: str) -> NoReturn:
    _fail()


def _parse_integer(value: str) -> int:
    if value.startswith("-"):
        _fail()
    try:
        return _integer(int(value))
    except ValueError:
        _fail()


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
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ConsolidationVerificationJournalUnavailable,
    ):
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


def _probe_value(entry: ConsolidationVerificationJournalEntry) -> dict[str, object]:
    probe = entry.probe
    value: dict[str, object] = {
        "schema": probe.schema,
        "ordinal": probe.ordinal,
        "probe_kind": probe.probe_kind,
        "probe_id": probe.probe_id,
        "executor_id": probe.executor_id,
        "contract_digest": probe.contract_digest,
        "expected_result_digest": probe.expected_result_digest,
        "probe_digest": probe.probe_digest,
        "status": entry.status,
    }
    if entry.status in {"result", "final"}:
        value["result_digest"] = entry.result_digest
    if entry.status == "final":
        value.update(
            terminal_event_id=entry.terminal_event_id,
            terminal_payload_digest=entry.terminal_payload_digest,
            effect_journal_digest=entry.effect_journal_digest,
        )
    return value


def _terminal_value(
    terminal: ConsolidationVerificationJournalTerminal,
) -> dict[str, object]:
    value: dict[str, object] = {"status": terminal.status}
    if terminal.status in {"result", "final"}:
        value["result_digest"] = terminal.result_digest
    if terminal.status == "final":
        value.update(
            terminal_event_id=terminal.terminal_event_id,
            terminal_payload_digest=terminal.terminal_payload_digest,
            effect_journal_digest=terminal.effect_journal_digest,
        )
    return value


def _basis_value(state: ConsolidationVerificationJournalState) -> dict[str, object]:
    return {
        "run_id": state.run_id,
        "operation_id": state.operation_id,
        "request_digest": state.request_digest,
        "plan_digest": state.plan_digest,
        "rebuild_journal_digest": state.rebuild_journal_digest,
        "canonical_census_digest": state.canonical_census_digest,
        "positive_probe_digest": state.positive_probe_digest,
        "negative_probe_digest": state.negative_probe_digest,
        "last_rebuild_terminal_event_id": state.last_rebuild_terminal_event_id,
        "last_rebuild_terminal_payload_digest": state.last_rebuild_terminal_payload_digest,
        "last_rebuild_effect_ordinal": state.last_rebuild_effect_ordinal,
        "probes": tuple(
            {
                key: value
                for key, value in _probe_value(entry).items()
                if key
                not in {
                    "status",
                    "result_digest",
                    "terminal_event_id",
                    "terminal_payload_digest",
                    "effect_journal_digest",
                }
            }
            for entry in state.probes
        ),
    }


def _binding_digest(state: ConsolidationVerificationJournalState) -> str:
    try:
        raw = consolidation_plan.canonical_closed_jcs(
            {"schema": _BINDING_SCHEMA, **_basis_value(state)}
        )
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    framed = (
        len(_BINDING_DOMAIN).to_bytes(4, "big")
        + _BINDING_DOMAIN
        + len(raw).to_bytes(8, "big")
        + raw
    )
    return hashlib.sha256(framed).hexdigest()


def _state_value(state: ConsolidationVerificationJournalState) -> dict[str, object]:
    return {
        "schema": state.schema,
        **{key: value for key, value in _basis_value(state).items() if key != "probes"},
        "binding_digest": state.binding_digest,
        "revision": state.revision,
        "probes": tuple(_probe_value(entry) for entry in state.probes),
        "terminal": _terminal_value(state.terminal),
    }


def _state_bytes(state: ConsolidationVerificationJournalState) -> bytes:
    try:
        raw = consolidation_plan.canonical_closed_jcs(_state_value(state))
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    if len(raw) > _MAX_JOURNAL_BYTES:
        _fail()
    return raw


def _parse_probe(value: object, *, ordinal: int) -> ConsolidationVerificationJournalEntry:
    if not isinstance(value, Mapping):
        _fail()
    status = value.get("status")
    fields = {
        "prior": _PROBE_BASE_FIELDS,
        "result": _PROBE_RESULT_FIELDS,
        "final": _PROBE_FINAL_FIELDS,
    }.get(status)
    if fields is None:
        _fail()
    row = _mapping(value, fields)
    kind = row["probe_kind"]
    if row["schema"] != consolidation_verification.VERIFICATION_PROBE_SCHEMA or kind not in {
        "positive",
        "negative",
    }:
        _fail()
    if _integer(row["ordinal"]) != ordinal:
        _fail()
    probe_kind: Literal["positive", "negative"] = "positive" if kind == "positive" else "negative"
    probe = consolidation_verification._build_probe(  # noqa: SLF001
        {
            "probe_id": row["probe_id"],
            "executor_id": row["executor_id"],
            "contract_digest": row["contract_digest"],
            "expected_result_digest": row["expected_result_digest"],
        },
        ordinal=ordinal,
        probe_kind=probe_kind,
    )
    if _digest(row["probe_digest"]) != probe.probe_digest:
        _fail()
    result_digest = None
    event_id = None
    payload_digest = None
    journal_digest = None
    if status in {"result", "final"}:
        result_digest = _digest(row["result_digest"])
        if result_digest != probe.expected_result_digest:
            _fail()
    if status == "final":
        event_id = _event_id(row["terminal_event_id"])
        payload_digest = _digest(row["terminal_payload_digest"])
        journal_digest = _digest(row["effect_journal_digest"])
    return ConsolidationVerificationJournalEntry(
        probe=probe,
        status=status,
        result_digest=result_digest,
        terminal_event_id=event_id,
        terminal_payload_digest=payload_digest,
        effect_journal_digest=journal_digest,
    )


def _parse_terminal(value: object) -> ConsolidationVerificationJournalTerminal:
    if not isinstance(value, Mapping):
        _fail()
    status = value.get("status")
    fields = {
        "prior": _TERMINAL_BASE_FIELDS,
        "result": _TERMINAL_RESULT_FIELDS,
        "final": _TERMINAL_FINAL_FIELDS,
    }.get(status)
    if fields is None:
        _fail()
    row = _mapping(value, fields)
    result = _digest(row["result_digest"]) if status in {"result", "final"} else None
    return ConsolidationVerificationJournalTerminal(
        status=status,
        result_digest=result,
        terminal_event_id=_event_id(row["terminal_event_id"]) if status == "final" else None,
        terminal_payload_digest=(
            _digest(row["terminal_payload_digest"]) if status == "final" else None
        ),
        effect_journal_digest=(
            _digest(row["effect_journal_digest"]) if status == "final" else None
        ),
    )


def _parse_state(raw: bytes) -> ConsolidationVerificationJournalState:
    value = _mapping(_decode(raw), _RECORD_FIELDS)
    if value["schema"] != JOURNAL_SCHEMA:
        _fail()
    rows = value["probes"]
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence) or not rows:
        _fail()
    probes = tuple(_parse_probe(row, ordinal=ordinal) for ordinal, row in enumerate(rows))
    kinds = tuple(entry.probe.probe_kind for entry in probes)
    if kinds != tuple(sorted(kinds, key={"positive": 0, "negative": 1}.get)):
        _fail()
    if len({entry.probe.probe_id for entry in probes}) != len(probes):
        _fail()
    ranks = tuple({"final": 0, "result": 1, "prior": 2}[entry.status] for entry in probes)
    if tuple(sorted(ranks)) != ranks or ranks.count(1) > 1:
        _fail()
    terminal = _parse_terminal(value["terminal"])
    if terminal.status != "prior" and any(entry.status != "final" for entry in probes):
        _fail()
    expected_revision = (
        1
        + sum(
            2 if entry.status == "final" else 1 if entry.status == "result" else 0
            for entry in probes
        )
        + (2 if terminal.status == "final" else 1 if terminal.status == "result" else 0)
    )
    state = ConsolidationVerificationJournalState(
        schema=JOURNAL_SCHEMA,
        run_id=_uuid4(value["run_id"]),
        operation_id=_uuid4(value["operation_id"]),
        request_digest=_digest(value["request_digest"]),
        plan_digest=_digest(value["plan_digest"]),
        rebuild_journal_digest=_digest(value["rebuild_journal_digest"]),
        canonical_census_digest=_digest(value["canonical_census_digest"]),
        positive_probe_digest=_digest(value["positive_probe_digest"]),
        negative_probe_digest=_digest(value["negative_probe_digest"]),
        last_rebuild_terminal_event_id=_event_id(value["last_rebuild_terminal_event_id"]),
        last_rebuild_terminal_payload_digest=_digest(value["last_rebuild_terminal_payload_digest"]),
        last_rebuild_effect_ordinal=_integer(value["last_rebuild_effect_ordinal"]),
        binding_digest=_digest(value["binding_digest"]),
        revision=_integer(value["revision"], minimum=1),
        probes=probes,
        terminal=terminal,
        state_digest=hashlib.sha256(raw).hexdigest(),
    )
    positive = tuple(entry.probe for entry in probes if entry.probe.probe_kind == "positive")
    negative = tuple(entry.probe for entry in probes if entry.probe.probe_kind == "negative")
    plan = consolidation_verification.build_verification_plan(
        positive_probes=tuple(
            {
                "probe_id": probe.probe_id,
                "executor_id": probe.executor_id,
                "contract_digest": probe.contract_digest,
                "expected_result_digest": probe.expected_result_digest,
            }
            for probe in positive
        ),
        negative_probes=tuple(
            {
                "probe_id": probe.probe_id,
                "executor_id": probe.executor_id,
                "contract_digest": probe.contract_digest,
                "expected_result_digest": probe.expected_result_digest,
            }
            for probe in negative
        ),
    )
    if (
        tuple(entry.probe for entry in probes) != plan.probes
        or state.positive_probe_digest != plan.positive_probe_digest
        or state.negative_probe_digest != plan.negative_probe_digest
        or state.revision != expected_revision
        or state.binding_digest != _binding_digest(state)
    ):
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
    except ConsolidationVerificationJournalUnavailable:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail()


class ConsolidationVerificationJournalStore:
    """CAS-persist a plan-bound verification matrix and its exact terminals."""

    def __init__(self, vault_root: Path | str, *, run_id: str) -> None:
        self.vault_root = Path(vault_root).absolute()
        self.run_id = _uuid4(run_id)
        self.path = (
            self.vault_root
            / kb_dirname()
            / "_Consolidation"
            / "runs"
            / self.run_id
            / "verification.json"
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

    def _load_locked(self) -> tuple[ConsolidationVerificationJournalState, bytes]:
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
        rebuild_journal_digest: str,
        canonical_census_digest: str,
        verification_plan: consolidation_verification.VerificationPlan,
        last_rebuild_terminal_event_id: str,
        last_rebuild_terminal_payload_digest: str,
        last_rebuild_effect_ordinal: int,
    ) -> ConsolidationVerificationJournalState:
        plan = consolidation_verification._checked_plan(verification_plan)  # noqa: SLF001
        candidate = ConsolidationVerificationJournalState(
            schema=JOURNAL_SCHEMA,
            run_id=self.run_id,
            operation_id=_uuid4(operation_id),
            request_digest=_digest(request_digest),
            plan_digest=_digest(plan_digest),
            rebuild_journal_digest=_digest(rebuild_journal_digest),
            canonical_census_digest=_digest(canonical_census_digest),
            positive_probe_digest=plan.positive_probe_digest,
            negative_probe_digest=plan.negative_probe_digest,
            last_rebuild_terminal_event_id=_event_id(last_rebuild_terminal_event_id),
            last_rebuild_terminal_payload_digest=_digest(last_rebuild_terminal_payload_digest),
            last_rebuild_effect_ordinal=_integer(last_rebuild_effect_ordinal),
            binding_digest="0" * 64,
            revision=1,
            probes=tuple(
                ConsolidationVerificationJournalEntry(
                    probe=probe,
                    status="prior",
                    result_digest=None,
                    terminal_event_id=None,
                    terminal_payload_digest=None,
                    effect_journal_digest=None,
                )
                for probe in plan.probes
            ),
            terminal=ConsolidationVerificationJournalTerminal(
                status="prior",
                result_digest=None,
                terminal_event_id=None,
                terminal_payload_digest=None,
                effect_journal_digest=None,
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

    def load(self) -> ConsolidationVerificationJournalState:
        with _authority(self.vault_root, mutation=False):
            state, _raw = self._load_locked()
            return state

    def record_probe_result(
        self,
        probe: consolidation_verification.VerificationProbe,
        result_digest: str,
    ) -> ConsolidationVerificationJournalState:
        if type(probe) is not consolidation_verification.VerificationProbe:
            _fail()
        result = _digest(result_digest)
        with _authority(self.vault_root, mutation=True):
            current, raw = self._load_locked()
            if not 0 <= probe.ordinal < len(current.probes):
                _fail()
            entry = current.probes[probe.ordinal]
            if entry.probe != probe or result != probe.expected_result_digest:
                _fail()
            if entry.status in {"result", "final"}:
                if entry.result_digest == result:
                    return current
                _fail()
            if entry.status != "prior" or any(
                prior.status != "final" for prior in current.probes[: probe.ordinal]
            ):
                _fail()
            entries = list(current.probes)
            entries[probe.ordinal] = replace(
                entry,
                status="result",
                result_digest=result,
            )
            return self._replace_locked(current, raw, probes=tuple(entries))

    def finalize_probe(
        self,
        probe: consolidation_verification.VerificationProbe,
        result_digest: str,
        *,
        terminal_event_id: str,
        terminal_payload_digest: str,
        effect_journal_digest: str,
    ) -> ConsolidationVerificationJournalState:
        result = _digest(result_digest)
        event_id = _event_id(terminal_event_id)
        payload = _digest(terminal_payload_digest)
        journal = _digest(effect_journal_digest)
        with _authority(self.vault_root, mutation=True):
            current, raw = self._load_locked()
            if not 0 <= probe.ordinal < len(current.probes):
                _fail()
            entry = current.probes[probe.ordinal]
            if entry.probe != probe:
                _fail()
            if entry.status == "final":
                if (
                    entry.result_digest == result
                    and entry.terminal_event_id == event_id
                    and entry.terminal_payload_digest == payload
                    and entry.effect_journal_digest == journal
                ):
                    return current
                _fail()
            if entry.status != "result" or entry.result_digest != result:
                _fail()
            entries = list(current.probes)
            entries[probe.ordinal] = replace(
                entry,
                status="final",
                terminal_event_id=event_id,
                terminal_payload_digest=payload,
                effect_journal_digest=journal,
            )
            return self._replace_locked(current, raw, probes=tuple(entries))

    def record_terminal_result(self, result_digest: str) -> ConsolidationVerificationJournalState:
        result = _digest(result_digest)
        with _authority(self.vault_root, mutation=True):
            current, raw = self._load_locked()
            if any(entry.status != "final" for entry in current.probes):
                _fail()
            if current.terminal.status in {"result", "final"}:
                if current.terminal.result_digest == result:
                    return current
                _fail()
            if current.terminal.status != "prior":
                _fail()
            terminal = replace(current.terminal, status="result", result_digest=result)
            return self._replace_locked(current, raw, terminal=terminal)

    def finalize_terminal(
        self,
        result_digest: str,
        *,
        terminal_event_id: str,
        terminal_payload_digest: str,
        effect_journal_digest: str,
    ) -> ConsolidationVerificationJournalState:
        result = _digest(result_digest)
        event_id = _event_id(terminal_event_id)
        payload = _digest(terminal_payload_digest)
        journal = _digest(effect_journal_digest)
        with _authority(self.vault_root, mutation=True):
            current, raw = self._load_locked()
            terminal = current.terminal
            if terminal.status == "final":
                if (
                    terminal.result_digest == result
                    and terminal.terminal_event_id == event_id
                    and terminal.terminal_payload_digest == payload
                    and terminal.effect_journal_digest == journal
                ):
                    return current
                _fail()
            if terminal.status != "result" or terminal.result_digest != result:
                _fail()
            return self._replace_locked(
                current,
                raw,
                terminal=replace(
                    terminal,
                    status="final",
                    terminal_event_id=event_id,
                    terminal_payload_digest=payload,
                    effect_journal_digest=journal,
                ),
            )

    def _replace_locked(
        self,
        current: ConsolidationVerificationJournalState,
        raw: bytes,
        *,
        probes: tuple[ConsolidationVerificationJournalEntry, ...] | None = None,
        terminal: ConsolidationVerificationJournalTerminal | None = None,
    ) -> ConsolidationVerificationJournalState:
        candidate = replace(
            current,
            revision=current.revision + 1,
            probes=current.probes if probes is None else probes,
            terminal=current.terminal if terminal is None else terminal,
            state_digest="0" * 64,
        )
        target_raw = _state_bytes(candidate)
        target = replace(candidate, state_digest=hashlib.sha256(target_raw).hexdigest())
        self._publish(target_raw, expected_sha256=hashlib.sha256(raw).hexdigest())
        return target
