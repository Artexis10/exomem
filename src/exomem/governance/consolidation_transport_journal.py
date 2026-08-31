"""Owner-only aggregate journal for exact-cell transport verification.

The in-process verification journal is already terminal before transport work
begins.  This store chains a transport plan to that exact terminal and tracks
only monotonic digest outcomes plus receipt/effect references.  Request bodies,
authentication material, process-local route leases, and consolidation
authority never cross this boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, NoReturn, cast

from .. import reserved_paths
from ..kbdir import kb_dirname
from . import (
    consolidation_plan,
    consolidation_transport_verification,
    consolidation_verification_journal,
)

JOURNAL_SCHEMA = "exomem.consolidation-transport-verification-journal/v1"

_BINDING_SCHEMA = "exomem.consolidation-transport-verification-journal-basis/v1"
_BINDING_DOMAIN = _BINDING_SCHEMA.encode("ascii")
_DESCRIPTOR_ID = "consolidation-tree"
_OWNER = "consolidation.run"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_ID = re.compile(r"[0-9a-f]{64}:committed\Z")
_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_MAX_JOURNAL_BYTES = 2 * 1024 * 1024
_MAX_SAFE_INTEGER = (1 << 53) - 1
_STATUSES = frozenset({"prior", "result", "final"})
_KINDS = frozenset(
    {
        "transport-stop",
        "transport-probe",
        "transport-verified",
        "routing-open",
        "complete",
    }
)
_PROBE_FIELDS = frozenset(
    {
        "schema",
        "ordinal",
        "probe_id",
        "probe_kind",
        "surface",
        "contract_digest",
        "expected_result_digest",
        "probe_digest",
    }
)
_EFFECT_BASE_FIELDS = frozenset({"kind", "status"})
_PROBE_EFFECT_BASE_FIELDS = _EFFECT_BASE_FIELDS | frozenset({"probe_ordinal"})
_EFFECT_RESULT_FIELDS = _EFFECT_BASE_FIELDS | frozenset({"result_digest"})
_PROBE_EFFECT_RESULT_FIELDS = _PROBE_EFFECT_BASE_FIELDS | frozenset(
    {"result_digest"}
)
_EFFECT_FINAL_FIELDS = _EFFECT_RESULT_FIELDS | frozenset(
    {"terminal_event_id", "terminal_payload_digest", "effect_journal_digest"}
)
_PROBE_EFFECT_FINAL_FIELDS = _PROBE_EFFECT_RESULT_FIELDS | frozenset(
    {"terminal_event_id", "terminal_payload_digest", "effect_journal_digest"}
)
_RECORD_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "operation_id",
        "request_digest",
        "cutover_plan_digest",
        "apply_journal_digest",
        "verification_manifest_digest",
        "canonical_census_digest",
        "in_process_verification_basis_digest",
        "in_process_verification_result_digest",
        "in_process_verified_terminal_event_id",
        "in_process_verified_terminal_payload_digest",
        "in_process_verified_effect_journal_digest",
        "in_process_verified_effect_ordinal",
        "plan_digest",
        "basis_digest",
        "routing_stop_digest",
        "transport_stop_effect",
        "binding_digest",
        "revision",
        "probes",
        "effects",
    }
)

__all__ = [
    "JOURNAL_SCHEMA",
    "ConsolidationTransportJournalEffect",
    "ConsolidationTransportJournalState",
    "ConsolidationTransportJournalStore",
    "ConsolidationTransportJournalUnavailable",
]


class ConsolidationTransportJournalUnavailable(RuntimeError):
    """Content-free refusal for a missing, changed, or corrupt journal."""

    code = "CONSOLIDATION_TRANSPORT_JOURNAL_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ConsolidationTransportJournalEffect:
    kind: Literal[
        "transport-stop",
        "transport-probe",
        "transport-verified",
        "routing-open",
        "complete",
    ]
    probe_ordinal: int | None
    status: Literal["prior", "result", "final"]
    result_digest: str | None
    terminal_event_id: str | None
    terminal_payload_digest: str | None
    effect_journal_digest: str | None


@dataclass(frozen=True, slots=True)
class ConsolidationTransportJournalState:
    schema: str
    run_id: str
    operation_id: str
    request_digest: str
    cutover_plan_digest: str
    apply_journal_digest: str
    verification_manifest_digest: str
    canonical_census_digest: str
    in_process_verification_basis_digest: str
    in_process_verification_result_digest: str
    in_process_verified_terminal_event_id: str
    in_process_verified_terminal_payload_digest: str
    in_process_verified_effect_journal_digest: str
    in_process_verified_effect_ordinal: int
    plan_digest: str
    basis_digest: str
    routing_stop_digest: str
    binding_digest: str
    revision: int
    probes: tuple[consolidation_transport_verification.TransportProbe, ...]
    effects: tuple[ConsolidationTransportJournalEffect, ...]
    state_digest: str


def _fail() -> NoReturn:
    raise ConsolidationTransportJournalUnavailable from None


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
        ConsolidationTransportJournalUnavailable,
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


def _probe_value(
    probe: consolidation_transport_verification.TransportProbe,
) -> dict[str, object]:
    return {
        "schema": probe.schema,
        "ordinal": probe.ordinal,
        "probe_id": probe.probe_id,
        "probe_kind": probe.probe_kind,
        "surface": probe.surface,
        "contract_digest": probe.contract_digest,
        "expected_result_digest": probe.expected_result_digest,
        "probe_digest": probe.probe_digest,
    }


def _effect_value(effect: ConsolidationTransportJournalEffect) -> dict[str, object]:
    value: dict[str, object] = {"kind": effect.kind, "status": effect.status}
    if effect.kind == "transport-probe":
        value["probe_ordinal"] = effect.probe_ordinal
    if effect.status in {"result", "final"}:
        value["result_digest"] = effect.result_digest
    if effect.status == "final":
        value.update(
            terminal_event_id=effect.terminal_event_id,
            terminal_payload_digest=effect.terminal_payload_digest,
            effect_journal_digest=effect.effect_journal_digest,
        )
    return value


def _basis_value(state: ConsolidationTransportJournalState) -> dict[str, object]:
    return {
        "run_id": state.run_id,
        "operation_id": state.operation_id,
        "request_digest": state.request_digest,
        "cutover_plan_digest": state.cutover_plan_digest,
        "apply_journal_digest": state.apply_journal_digest,
        "verification_manifest_digest": state.verification_manifest_digest,
        "canonical_census_digest": state.canonical_census_digest,
        "in_process_verification_basis_digest": (
            state.in_process_verification_basis_digest
        ),
        "in_process_verification_result_digest": (
            state.in_process_verification_result_digest
        ),
        "in_process_verified_terminal_event_id": (
            state.in_process_verified_terminal_event_id
        ),
        "in_process_verified_terminal_payload_digest": (
            state.in_process_verified_terminal_payload_digest
        ),
        "in_process_verified_effect_journal_digest": (
            state.in_process_verified_effect_journal_digest
        ),
        "in_process_verified_effect_ordinal": state.in_process_verified_effect_ordinal,
        "plan_digest": state.plan_digest,
        "basis_digest": state.basis_digest,
        "routing_stop_digest": state.routing_stop_digest,
        "transport_stop_effect": _effect_value(state.effects[0]),
        "probes": tuple(_probe_value(probe) for probe in state.probes),
    }


def _binding_digest(state: ConsolidationTransportJournalState) -> str:
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


def _state_value(state: ConsolidationTransportJournalState) -> dict[str, object]:
    return {
        "schema": state.schema,
        **_basis_value(state),
        "binding_digest": state.binding_digest,
        "revision": state.revision,
        "effects": tuple(_effect_value(effect) for effect in state.effects),
    }


def _state_bytes(state: ConsolidationTransportJournalState) -> bytes:
    try:
        raw = consolidation_plan.canonical_closed_jcs(_state_value(state))
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail()
    if len(raw) > _MAX_JOURNAL_BYTES:
        _fail()
    return raw


def _parse_probe(
    value: object,
    *,
    ordinal: int,
) -> consolidation_transport_verification.TransportProbe:
    row = _mapping(value, _PROBE_FIELDS)
    if (
        row["schema"]
        != consolidation_transport_verification.TRANSPORT_VERIFICATION_PROBE_SCHEMA
        or _integer(row["ordinal"]) != ordinal
    ):
        _fail()
    try:
        probe = consolidation_transport_verification._checked_probe(  # noqa: SLF001
            {
                "probe_id": row["probe_id"],
                "probe_kind": row["probe_kind"],
                "surface": row["surface"],
                "contract_digest": row["contract_digest"],
                "expected_result_digest": row["expected_result_digest"],
            },
            ordinal=ordinal,
        )
    except consolidation_transport_verification.ConsolidationTransportVerificationUnavailable:
        _fail()
    if _digest(row["probe_digest"]) != probe.probe_digest:
        _fail()
    return probe


def _parse_effect(value: object) -> ConsolidationTransportJournalEffect:
    if not isinstance(value, Mapping):
        _fail()
    kind = value.get("kind")
    status = value.get("status")
    if kind not in _KINDS or status not in _STATUSES:
        _fail()
    is_probe = kind == "transport-probe"
    fields = {
        (False, "prior"): _EFFECT_BASE_FIELDS,
        (False, "result"): _EFFECT_RESULT_FIELDS,
        (False, "final"): _EFFECT_FINAL_FIELDS,
        (True, "prior"): _PROBE_EFFECT_BASE_FIELDS,
        (True, "result"): _PROBE_EFFECT_RESULT_FIELDS,
        (True, "final"): _PROBE_EFFECT_FINAL_FIELDS,
    }[(is_probe, status)]
    row = _mapping(value, fields)
    return ConsolidationTransportJournalEffect(
        kind=cast(
            Literal[
                "transport-stop",
                "transport-probe",
                "transport-verified",
                "routing-open",
                "complete",
            ],
            kind,
        ),
        probe_ordinal=_integer(row["probe_ordinal"]) if is_probe else None,
        status=cast(Literal["prior", "result", "final"], status),
        result_digest=(
            _digest(row["result_digest"])
            if status in {"result", "final"}
            else None
        ),
        terminal_event_id=(
            _event_id(row["terminal_event_id"]) if status == "final" else None
        ),
        terminal_payload_digest=(
            _digest(row["terminal_payload_digest"])
            if status == "final"
            else None
        ),
        effect_journal_digest=(
            _digest(row["effect_journal_digest"])
            if status == "final"
            else None
        ),
    )


def _prior_effects(
    probes: tuple[consolidation_transport_verification.TransportProbe, ...],
) -> tuple[ConsolidationTransportJournalEffect, ...]:
    def prior(
        kind: Literal[
            "transport-stop",
            "transport-probe",
            "transport-verified",
            "routing-open",
            "complete",
        ],
        probe_ordinal: int | None = None,
    ) -> ConsolidationTransportJournalEffect:
        return ConsolidationTransportJournalEffect(
            kind=kind,
            probe_ordinal=probe_ordinal,
            status="prior",
            result_digest=None,
            terminal_event_id=None,
            terminal_payload_digest=None,
            effect_journal_digest=None,
        )

    return (
        prior("transport-stop"),
        *(prior("transport-probe", probe.ordinal) for probe in probes),
        prior("transport-verified"),
        prior("routing-open"),
        prior("complete"),
    )


def _parse_state(raw: bytes) -> ConsolidationTransportJournalState:
    value = _mapping(_decode(raw), _RECORD_FIELDS)
    if value["schema"] != JOURNAL_SCHEMA:
        _fail()
    raw_probes = value["probes"]
    raw_effects = value["effects"]
    if (
        isinstance(raw_probes, (str, bytes))
        or not isinstance(raw_probes, Sequence)
        or not raw_probes
        or isinstance(raw_effects, (str, bytes))
        or not isinstance(raw_effects, Sequence)
    ):
        _fail()
    probes = tuple(
        _parse_probe(item, ordinal=ordinal)
        for ordinal, item in enumerate(raw_probes)
    )
    effects = tuple(_parse_effect(item) for item in raw_effects)
    bound_stop_effect = _parse_effect(value["transport_stop_effect"])
    expected_effects = _prior_effects(probes)
    if tuple((item.kind, item.probe_ordinal) for item in effects) != tuple(
        (item.kind, item.probe_ordinal) for item in expected_effects
    ) or not effects or effects[0] != bound_stop_effect:
        _fail()
    ranks = tuple({"final": 0, "result": 1, "prior": 2}[item.status] for item in effects)
    if tuple(sorted(ranks)) != ranks or ranks.count(1) > 1:
        _fail()
    routing_stop_digest = _digest(value["routing_stop_digest"])
    for effect in effects:
        if effect.status == "prior":
            continue
        if effect.kind == "transport-stop":
            expected_result = routing_stop_digest
        elif effect.kind == "transport-probe":
            if effect.probe_ordinal is None or not 0 <= effect.probe_ordinal < len(probes):
                _fail()
            expected_result = probes[effect.probe_ordinal].expected_result_digest
        else:
            continue
        if effect.result_digest != expected_result:
            _fail()
    expected_revision = 1 + sum(
        2 if effect.status == "final" else 1 if effect.status == "result" else 0
        for effect in effects
    )
    state = ConsolidationTransportJournalState(
        schema=JOURNAL_SCHEMA,
        run_id=_uuid4(value["run_id"]),
        operation_id=_uuid4(value["operation_id"]),
        request_digest=_digest(value["request_digest"]),
        cutover_plan_digest=_digest(value["cutover_plan_digest"]),
        apply_journal_digest=_digest(value["apply_journal_digest"]),
        verification_manifest_digest=_digest(value["verification_manifest_digest"]),
        canonical_census_digest=_digest(value["canonical_census_digest"]),
        in_process_verification_basis_digest=_digest(
            value["in_process_verification_basis_digest"]
        ),
        in_process_verification_result_digest=_digest(
            value["in_process_verification_result_digest"]
        ),
        in_process_verified_terminal_event_id=_event_id(
            value["in_process_verified_terminal_event_id"]
        ),
        in_process_verified_terminal_payload_digest=_digest(
            value["in_process_verified_terminal_payload_digest"]
        ),
        in_process_verified_effect_journal_digest=_digest(
            value["in_process_verified_effect_journal_digest"]
        ),
        in_process_verified_effect_ordinal=_integer(
            value["in_process_verified_effect_ordinal"]
        ),
        plan_digest=_digest(value["plan_digest"]),
        basis_digest=_digest(value["basis_digest"]),
        routing_stop_digest=routing_stop_digest,
        binding_digest=_digest(value["binding_digest"]),
        revision=_integer(value["revision"], minimum=1),
        probes=probes,
        effects=effects,
        state_digest=hashlib.sha256(raw).hexdigest(),
    )
    if state.revision != expected_revision or state.binding_digest != _binding_digest(state):
        _fail()
    return state


def _checked_plan(
    value: object,
) -> consolidation_transport_verification.TransportVerificationPlan:
    if type(value) is not consolidation_transport_verification.TransportVerificationPlan:
        _fail()
    checked = cast(
        consolidation_transport_verification.TransportVerificationPlan,
        value,
    )
    if not checked.probes:
        _fail()
    try:
        plan, _probe = consolidation_transport_verification._checked_plan_member(  # noqa: SLF001
            checked,
            checked.probes[0],
        )
    except consolidation_transport_verification.ConsolidationTransportVerificationUnavailable:
        _fail()
    return plan


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
    except ConsolidationTransportJournalUnavailable:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail()


class ConsolidationTransportJournalStore:
    """CAS-persist one exact transport plan and its ordered effect terminals."""

    def __init__(self, vault_root: Path | str, *, run_id: str) -> None:
        self.vault_root = Path(vault_root).absolute()
        self.run_id = _uuid4(run_id)
        self.path = (
            self.vault_root
            / kb_dirname()
            / "_Consolidation"
            / "runs"
            / self.run_id
            / "transport-verification.json"
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

    def _load_locked(self) -> tuple[ConsolidationTransportJournalState, bytes]:
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
        verification_journal: consolidation_verification_journal.ConsolidationVerificationJournalState,
        transport_plan: consolidation_transport_verification.TransportVerificationPlan,
        transport_stop_effect: ConsolidationTransportJournalEffect,
    ) -> ConsolidationTransportJournalState:
        if type(verification_journal) is not (
            consolidation_verification_journal.ConsolidationVerificationJournalState
        ):
            _fail()
        parent = verification_journal
        terminal = parent.terminal
        if (
            parent.run_id != self.run_id
            or any(entry.status != "final" for entry in parent.probes)
            or terminal.status != "final"
            or terminal.result_digest is None
            or terminal.terminal_event_id is None
            or terminal.terminal_payload_digest is None
            or terminal.effect_journal_digest is None
        ):
            _fail()
        plan = _checked_plan(transport_plan)
        basis = plan.basis
        if (
            type(transport_stop_effect) is not ConsolidationTransportJournalEffect
            or transport_stop_effect.kind != "transport-stop"
            or transport_stop_effect.probe_ordinal is not None
            or transport_stop_effect.status != "final"
            or transport_stop_effect.result_digest != basis.routing_stop_digest
            or transport_stop_effect.terminal_event_id is None
            or transport_stop_effect.terminal_payload_digest is None
            or transport_stop_effect.effect_journal_digest is None
        ):
            _fail()
        if (
            basis.run_id != parent.run_id
            or basis.operation_id != parent.operation_id
            or basis.plan_digest != parent.plan_digest
            or basis.canonical_census_digest != parent.canonical_census_digest
        ):
            _fail()
        effects = list(_prior_effects(plan.probes))
        effects[0] = transport_stop_effect
        candidate = ConsolidationTransportJournalState(
            schema=JOURNAL_SCHEMA,
            run_id=parent.run_id,
            operation_id=parent.operation_id,
            request_digest=parent.request_digest,
            cutover_plan_digest=parent.plan_digest,
            apply_journal_digest=basis.journal_digest,
            verification_manifest_digest=basis.verification_manifest_digest,
            canonical_census_digest=parent.canonical_census_digest,
            in_process_verification_basis_digest=parent.binding_digest,
            in_process_verification_result_digest=terminal.result_digest,
            in_process_verified_terminal_event_id=terminal.terminal_event_id,
            in_process_verified_terminal_payload_digest=(
                terminal.terminal_payload_digest
            ),
            in_process_verified_effect_journal_digest=terminal.effect_journal_digest,
            in_process_verified_effect_ordinal=(
                parent.last_rebuild_effect_ordinal + len(parent.probes) + 1
            ),
            plan_digest=plan.digest,
            basis_digest=basis.digest,
            routing_stop_digest=basis.routing_stop_digest,
            binding_digest="0" * 64,
            revision=3,
            probes=plan.probes,
            effects=tuple(effects),
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

    def load(self) -> ConsolidationTransportJournalState:
        with _authority(self.vault_root, mutation=False):
            state, _raw = self._load_locked()
            return state

    @staticmethod
    def _effect_index(
        state: ConsolidationTransportJournalState,
        *,
        kind: str,
        probe_ordinal: int | None,
    ) -> int:
        matches = [
            index
            for index, effect in enumerate(state.effects)
            if effect.kind == kind and effect.probe_ordinal == probe_ordinal
        ]
        if len(matches) != 1:
            _fail()
        return matches[0]

    @staticmethod
    def _validate_result(
        state: ConsolidationTransportJournalState,
        effect: ConsolidationTransportJournalEffect,
        result_digest: str,
    ) -> str:
        result = _digest(result_digest)
        if effect.kind == "transport-stop":
            expected = state.routing_stop_digest
        elif effect.kind == "transport-probe":
            if (
                effect.probe_ordinal is None
                or not 0 <= effect.probe_ordinal < len(state.probes)
            ):
                _fail()
            expected = state.probes[effect.probe_ordinal].expected_result_digest
        else:
            return result
        if result != expected:
            _fail()
        return result

    def record_transport_effect_result(
        self,
        *,
        kind: str,
        result_digest: str,
        probe_ordinal: int | None = None,
    ) -> ConsolidationTransportJournalState:
        if kind != "transport-probe" and probe_ordinal is not None:
            _fail()
        with _authority(self.vault_root, mutation=True):
            current, raw = self._load_locked()
            index = self._effect_index(
                current,
                kind=kind,
                probe_ordinal=probe_ordinal,
            )
            effect = current.effects[index]
            result = self._validate_result(current, effect, result_digest)
            if effect.status in {"result", "final"}:
                if effect.result_digest == result:
                    return current
                _fail()
            if effect.status != "prior" or any(
                prior.status != "final" for prior in current.effects[:index]
            ):
                _fail()
            effects = list(current.effects)
            effects[index] = replace(
                effect,
                status="result",
                result_digest=result,
            )
            return self._replace_locked(current, raw, effects=tuple(effects))

    def finalize_transport_effect(
        self,
        *,
        kind: str,
        result_digest: str,
        terminal_event_id: str,
        terminal_payload_digest: str,
        effect_journal_digest: str,
        probe_ordinal: int | None = None,
    ) -> ConsolidationTransportJournalState:
        if kind != "transport-probe" and probe_ordinal is not None:
            _fail()
        event_id = _event_id(terminal_event_id)
        payload_digest = _digest(terminal_payload_digest)
        journal_digest = _digest(effect_journal_digest)
        with _authority(self.vault_root, mutation=True):
            current, raw = self._load_locked()
            index = self._effect_index(
                current,
                kind=kind,
                probe_ordinal=probe_ordinal,
            )
            effect = current.effects[index]
            result = self._validate_result(current, effect, result_digest)
            if effect.status == "final":
                if (
                    effect.result_digest == result
                    and effect.terminal_event_id == event_id
                    and effect.terminal_payload_digest == payload_digest
                    and effect.effect_journal_digest == journal_digest
                ):
                    return current
                _fail()
            if effect.status != "result" or effect.result_digest != result:
                _fail()
            effects = list(current.effects)
            effects[index] = replace(
                effect,
                status="final",
                terminal_event_id=event_id,
                terminal_payload_digest=payload_digest,
                effect_journal_digest=journal_digest,
            )
            return self._replace_locked(current, raw, effects=tuple(effects))

    def _replace_locked(
        self,
        current: ConsolidationTransportJournalState,
        raw: bytes,
        *,
        effects: tuple[ConsolidationTransportJournalEffect, ...],
    ) -> ConsolidationTransportJournalState:
        candidate = replace(
            current,
            revision=current.revision + 1,
            effects=effects,
            state_digest="0" * 64,
        )
        target_raw = _state_bytes(candidate)
        target = replace(candidate, state_digest=hashlib.sha256(target_raw).hexdigest())
        self._publish(target_raw, expected_sha256=hashlib.sha256(raw).hexdigest())
        return target
