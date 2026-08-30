"""Closed derivative-rebuild boundary for governed vault consolidation.

The complete apply coordinator supplies the concrete component rebuilders.  This
module owns the invariant around them: all approved content batches are final,
each component is bound to the same canonical destination census, and canonical
bytes remain unchanged after every component.  Source/archive paths are
deliberately absent from the rebuild context.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

DERIVATIVE_REBUILD_TERMINAL_SCHEMA = (
    "exomem.consolidation-derivative-rebuild-terminal/v1"
)
DERIVATIVE_COMPONENTS = (
    "lexical",
    "embedding",
    "semantic-unit",
    "graph",
    "media",
    "freshness",
    "identity",
    "review",
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)

__all__ = [
    "DERIVATIVE_COMPONENTS",
    "DERIVATIVE_REBUILD_TERMINAL_SCHEMA",
    "DerivativeRebuildContext",
    "DerivativeRebuildResult",
    "DerivativeRebuildTerminal",
    "DerivativeRebuildUnavailable",
    "rebuild_destination_derivatives",
]


class DerivativeRebuildUnavailable(RuntimeError):
    """Content-free refusal for an untrusted or incomplete rebuild state."""

    code = "CONSOLIDATION_DERIVATIVE_REBUILD_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("consolidation derivative rebuild is unavailable")


@dataclass(frozen=True, slots=True)
class DerivativeRebuildContext:
    vault_root: Path
    canonical_census_digest: str


@dataclass(frozen=True, slots=True)
class DerivativeRebuildTerminal:
    schema: str
    component: str
    canonical_census_digest: str
    artifact_fingerprint: str


@dataclass(frozen=True, slots=True)
class DerivativeRebuildResult:
    canonical_census_digest: str
    completed_components: tuple[str, ...]
    terminals: tuple[DerivativeRebuildTerminal, ...]


def _fail() -> NoReturn:
    raise DerivativeRebuildUnavailable from None


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _snapshot(
    snapshot_census: Callable[[Path], str],
    vault_root: Path,
) -> str:
    try:
        return _digest(snapshot_census(vault_root))
    except DerivativeRebuildUnavailable:
        raise
    except Exception:  # noqa: BLE001 - expose only the stable sealed refusal
        _fail()


def _terminal(
    value: object,
    *,
    component: str,
    canonical_census_digest: str,
) -> DerivativeRebuildTerminal:
    if type(value) is not DerivativeRebuildTerminal:
        _fail()
    if (
        value.schema != DERIVATIVE_REBUILD_TERMINAL_SCHEMA
        or value.component != component
        or value.canonical_census_digest != canonical_census_digest
    ):
        _fail()
    return DerivativeRebuildTerminal(
        schema=value.schema,
        component=value.component,
        canonical_census_digest=_digest(value.canonical_census_digest),
        artifact_fingerprint=_digest(value.artifact_fingerprint),
    )


def rebuild_destination_derivatives(
    *,
    vault_root: Path,
    expected_canonical_census_digest: str,
    expected_batch_count: int,
    committed_batch_ordinals: tuple[int, ...],
    snapshot_census: Callable[[Path], str],
    rebuild_component: Callable[
        [str, DerivativeRebuildContext], DerivativeRebuildTerminal
    ],
) -> DerivativeRebuildResult:
    """Run the closed rebuild set without permitting canonical-byte drift.

    The caller cannot start this boundary until the complete deterministic
    content partition is final.  Every component receives only the destination
    root and its approved canonical census digest; no source/archive/database
    input exists.  Abrupt ``BaseException`` interruption remains visible to the
    saga recovery layer, while ordinary component failures become one stable,
    content-free refusal.
    """

    if type(expected_batch_count) is not int or expected_batch_count <= 0:
        _fail()
    if (
        type(committed_batch_ordinals) is not tuple
        or committed_batch_ordinals != tuple(range(expected_batch_count))
        or any(type(value) is not int for value in committed_batch_ordinals)
    ):
        _fail()
    expected = _digest(expected_canonical_census_digest)
    root = Path(os.path.abspath(vault_root))
    if _snapshot(snapshot_census, root) != expected:
        _fail()
    context = DerivativeRebuildContext(
        vault_root=root,
        canonical_census_digest=expected,
    )
    terminals: list[DerivativeRebuildTerminal] = []
    for component in DERIVATIVE_COMPONENTS:
        try:
            rebuilt = rebuild_component(component, context)
        except DerivativeRebuildUnavailable:
            raise
        except Exception:  # noqa: BLE001 - component details cannot cross the boundary
            _fail()
        terminal = _terminal(
            rebuilt,
            component=component,
            canonical_census_digest=expected,
        )
        if _snapshot(snapshot_census, root) != expected:
            _fail()
        terminals.append(terminal)
    return DerivativeRebuildResult(
        canonical_census_digest=expected,
        completed_components=DERIVATIVE_COMPONENTS,
        terminals=tuple(terminals),
    )
