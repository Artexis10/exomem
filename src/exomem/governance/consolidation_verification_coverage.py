"""Closed-world coverage rows for governed-consolidation verification."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from . import egress

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_CONDITIONAL_MUTATION_OUTCOMES = frozenset(
    {
        "apply-conditional",
        "dry-run-default",
        "dry-run-opt-in",
        "mutation",
        "save-conditional",
        "validation",
    }
)
_SELECTOR_OUTCOMES = _CONDITIONAL_MUTATION_OUTCOMES | {"structure"}
_PROJECTOR_ADAPTERS = frozenset(
    {
        "artifact-reference",
        "binary",
        "hit",
        "not-applicable",
        "page",
        "structure",
        *_SELECTOR_OUTCOMES,
    }
)
_RECEIPT_OUTCOMES = frozenset(
    {
        "artifact-reference",
        "binary",
        "frames",
        "graph",
        "hits",
        "mutation",
        "page",
        "structure",
        *_SELECTOR_OUTCOMES,
    }
)
_TOMBSTONE_GATES = frozenset(
    {
        "artifact-reference",
        "binary",
        "frames",
        "graph",
        "hits",
        "not-applicable",
        "page",
        "structure",
        *_SELECTOR_OUTCOMES,
    }
)
_PROBE_DISPOSITIONS = frozenset({"positive-negative", "seal-only"})
_SEAL_DISPOSITIONS = frozenset({"mutation", "read", "transfer"})

__all__ = [
    "ConsolidationVerificationCoverageUnavailable",
    "VerificationCoverageBranch",
    "build_coverage_inventory",
]


class ConsolidationVerificationCoverageUnavailable(RuntimeError):
    """Content-free refusal for an incomplete release/verification inventory."""

    code = "CONSOLIDATION_VERIFICATION_COVERAGE_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class VerificationCoverageBranch:
    branch_id: str
    branch_kind: Literal["command", "product", "selector", "route"]
    probe_disposition: str
    seal_disposition: str
    projector_adapter: str
    receipt_outcome: str
    tombstone_gate: str


def _fail() -> NoReturn:
    raise ConsolidationVerificationCoverageUnavailable from None


def _identifier(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail()
    return value


def _closed_row(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        _fail()
    return value


def _text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if type(value) is not str or not value:
        _fail()
    return value


def _disposition(row: Mapping[str, object], field: str, allowed: frozenset[str]) -> str:
    value = _text(row, field)
    if value not in allowed:
        _fail()
    return value


def _adapter(row: Mapping[str, object], field: str, allowed: frozenset[str]) -> str:
    value = _text(row, field)
    parts = value.split("+")
    if parts != sorted(set(parts)) or not set(parts) <= allowed:
        _fail()
    return value


def _command_rows(
    registry: Mapping[str, Any],
) -> list[VerificationCoverageBranch]:
    try:
        capabilities = egress.command_capability_registry(registry)
    except (RuntimeError, TypeError):
        _fail()
    rows: list[VerificationCoverageBranch] = []
    for name, raw in capabilities.items():
        command_name = _identifier(name)
        capability = _closed_row(
            raw,
            frozenset({"projector", "receipt", "tombstone"}),
        )
        command = registry.get(name)
        if command is None or type(getattr(command, "read_only", None)) is not bool:
            _fail()
        projector = _adapter(capability, "projector", _PROJECTOR_ADAPTERS)
        rows.append(
            VerificationCoverageBranch(
                branch_id=f"command:{command_name}",
                branch_kind="command",
                probe_disposition=(
                    "seal-only"
                    if not command.read_only and projector == "not-applicable"
                    else "positive-negative"
                ),
                seal_disposition="read" if command.read_only else "mutation",
                projector_adapter=projector,
                receipt_outcome=_adapter(capability, "receipt", _RECEIPT_OUTCOMES),
                tombstone_gate=_adapter(capability, "tombstone", _TOMBSTONE_GATES),
            )
        )
    return rows


def _product_rows(
    product_registry: Mapping[str, Any], command_registry: Mapping[str, Any]
) -> list[VerificationCoverageBranch]:
    try:
        capabilities = egress.alias_capability_registry(product_registry, command_registry)
    except (RuntimeError, TypeError):
        _fail()
    rows: list[VerificationCoverageBranch] = []
    for name, raw in capabilities.items():
        product_name = _identifier(name)
        capability = _closed_row(
            raw,
            frozenset({"projector", "receipt", "tombstone"}),
        )
        command = product_registry.get(name)
        if command is None or type(getattr(command, "read_only", None)) is not bool:
            _fail()
        projector = _adapter(capability, "projector", _PROJECTOR_ADAPTERS)
        rows.append(
            VerificationCoverageBranch(
                branch_id=f"product:{product_name}",
                branch_kind="product",
                probe_disposition=(
                    "seal-only"
                    if not command.read_only and projector == "not-applicable"
                    else "positive-negative"
                ),
                seal_disposition="read" if command.read_only else "mutation",
                projector_adapter=projector,
                receipt_outcome=_adapter(capability, "receipt", _RECEIPT_OUTCOMES),
                tombstone_gate=_adapter(capability, "tombstone", _TOMBSTONE_GATES),
            )
        )
    return rows


def _selector_rows(
    capabilities: Mapping[tuple[str, str], Mapping[str, Mapping[str, str]]],
) -> list[VerificationCoverageBranch]:
    rows: list[VerificationCoverageBranch] = []
    for key, branches in capabilities.items():
        if type(key) is not tuple or len(key) != 2 or not isinstance(branches, Mapping):
            _fail()
        command_name = _identifier(key[0])
        selector_name = _identifier(key[1])
        for value, raw in branches.items():
            selector_value = _identifier(value)
            capability = _closed_row(raw, frozenset({"outcome", "tombstone"}))
            outcome = _text(capability, "outcome")
            if outcome not in _SELECTOR_OUTCOMES:
                _fail()
            tombstone = _text(capability, "tombstone")
            if tombstone != ("not-applicable" if outcome == "mutation" else outcome):
                _fail()
            mutating = outcome in _CONDITIONAL_MUTATION_OUTCOMES
            rows.append(
                VerificationCoverageBranch(
                    branch_id=(f"selector:{command_name}.{selector_name}={selector_value}"),
                    branch_kind="selector",
                    probe_disposition="seal-only" if outcome == "mutation" else "positive-negative",
                    seal_disposition="mutation" if mutating else "read",
                    projector_adapter=outcome,
                    receipt_outcome=outcome,
                    tombstone_gate=tombstone,
                )
            )
    return rows


def _route_rows(
    capabilities: Mapping[str, Mapping[str, str]],
) -> list[VerificationCoverageBranch]:
    rows: list[VerificationCoverageBranch] = []
    fields = frozenset({"projector", "receipt", "tombstone", "seal", "probe"})
    for route, raw in capabilities.items():
        route_name = _identifier(route)
        capability = _closed_row(raw, fields)
        rows.append(
            VerificationCoverageBranch(
                branch_id=f"route:{route_name}",
                branch_kind="route",
                probe_disposition=_disposition(capability, "probe", _PROBE_DISPOSITIONS),
                seal_disposition=_disposition(capability, "seal", _SEAL_DISPOSITIONS),
                projector_adapter=_adapter(capability, "projector", _PROJECTOR_ADAPTERS),
                receipt_outcome=_adapter(capability, "receipt", _RECEIPT_OUTCOMES),
                tombstone_gate=_adapter(capability, "tombstone", _TOMBSTONE_GATES),
            )
        )
    return rows


def build_coverage_inventory(
    command_registry: Mapping[str, Any],
    *,
    product_registry: Mapping[str, Any] | None = None,
    selector_capabilities: Mapping[tuple[str, str], Mapping[str, Mapping[str, str]]] | None = None,
    route_capabilities: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[VerificationCoverageBranch, ...]:
    """Generate one complete inventory from the live release registries."""

    if not isinstance(command_registry, Mapping):
        _fail()
    try:
        selectors = (
            egress.selector_capability_registry()
            if selector_capabilities is None
            else selector_capabilities
        )
        routes = (
            egress.non_command_route_capability_registry()
            if route_capabilities is None
            else route_capabilities
        )
        rows = [
            *_command_rows(command_registry),
            *(
                _product_rows(product_registry, command_registry)
                if product_registry is not None
                else ()
            ),
            *_selector_rows(selectors),
            *_route_rows(routes),
        ]
    except ConsolidationVerificationCoverageUnavailable:
        raise
    except (KeyError, RuntimeError, TypeError, ValueError):
        _fail()
    ordered = tuple(sorted(rows, key=lambda row: row.branch_id))
    if len({row.branch_id for row in ordered}) != len(ordered):
        _fail()
    return ordered
