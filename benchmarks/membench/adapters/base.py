"""Adapter contract: a strict superset of the external Track-A provider shape.

An adapter declares its capabilities; anything undeclared is reported
``unsupported`` by scorers — never emulated, never scored as zero. Harness or
environment faults raise :class:`AdapterEnvironmentError`, which invalidates
the run rather than counting as a contender loss.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


class Capability(str, enum.Enum):
    INGEST_API = "ingest_api"
    FILE_DROP = "file_drop"
    SEARCH = "search"
    CONTEXT_BUNDLE = "context_bundle"
    GRAPH_TRAVERSAL = "graph_traversal"
    GOVERNED_VIEWS = "governed_views"
    STATE_EXPORT = "state_export"
    NATIVE_HEALTH_AUDIT = "native_health_audit"
    DELETE = "delete"
    SUPERSEDE = "supersede"
    AS_OF_QUERY = "as_of_query"
    HOOK_ACTIVATION = "hook_activation"
    #: The provider answers in its own words, with its own attribution and its
    #: own decision to abstain. Without it the harness builds the answer, and
    #: every dimension scoring a property of the ANSWER — provenance,
    #: abstention, calibration — scores the harness rather than the contender.
    NATIVE_ANSWER = "native_answer"


#: Three-state governance measurement vocabulary (spec: "Governed Views Are
#: Wired, Not Simulated"). An adapter MAY expose a ``governance_state``
#: attribute with one of these values; absence means ``default_open`` — an
#: explicitly ungoverned vault measured as the default-open surface. "wired"
#: is legitimate ONLY when the adapter also declares
#: :attr:`Capability.GOVERNED_VIEWS`; the runner enforces that equivalence.
GOVERNANCE_STATES = frozenset({"wired", "default_open", "unsupported"})


class AdapterUnsupported(RuntimeError):
    """The provider does not support this capability (an honest result)."""


class AdapterEnvironmentError(RuntimeError):
    """Harness/setup/environment fault — invalidates the run, never a loss."""


@dataclass(frozen=True)
class Profile:
    """A named, fully recorded provider configuration."""

    name: str
    settings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Hit:
    rank: int
    provider_path: str
    title: str | None
    excerpt: str | None
    sentinels: tuple[str, ...]
    raw: dict
    text: str | None = None  # full stored text (answering); dropped from run artifacts


@dataclass(frozen=True)
class NativeAnswer:
    """A contender's own answer: its words, its attribution, its abstention.

    ``citations`` is a **closed** claim — the sources the provider says it
    used. The harness never adds to it, even when the answer text quotes a
    document carrying a sentinel, because the measured property is what the
    system claims as its basis and not what strings happen to appear in its
    output. (The extractive path keeps harvesting sentinels from text; there
    the quote *is* the claim, since nothing else could be.)

    ``abstained`` is likewise the provider's judgment. The extractive answerer
    can only abstain when retrieval returned nothing, which is why exomem
    abstained 0 times in 240 answers on seed-1 while 52 queries required it —
    a product that knows when to decline had no way to say so.
    """

    text: str
    citations: tuple[str, ...] = ()
    abstained: bool = False
    #: ``None`` leaves hedging to add-only normalization; set it explicitly
    #: when the provider expresses uncertainty structurally rather than in prose.
    hedged: bool | None = None
    clarification_question: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OpResult:
    seq: int
    op: str
    source_id: str | None
    ok: bool
    latency_ms: float
    detail: str | None = None


@dataclass(frozen=True)
class StateExportPage:
    path: str
    text: str


@dataclass(frozen=True)
class StateExport:
    pages: tuple[StateExportPage, ...]


@runtime_checkable
class MemoryAdapter(Protocol):
    name: str
    supports_group_reuse: bool

    def capabilities(self) -> frozenset[Capability]: ...

    def setup(self, workdir: Path, profile: Profile) -> None: ...

    def ingest(self, corpus_dir: Path, native_dir: Path) -> list[OpResult]: ...

    # ``persona`` is additive and optional: the runner passes it ONLY to
    # adapters declaring :attr:`Capability.GOVERNED_VIEWS`, so an adapter
    # written against the two-argument shape keeps working unchanged.
    def search(self, query: str, limit: int, *, persona: str | None = None) -> list[Hit]: ...

    def export_state(self) -> StateExport: ...

    def cleanup(self) -> None: ...

    def version_info(self) -> dict[str, str]: ...


_FACTORIES: dict[str, Callable[..., MemoryAdapter]] = {}


def register_adapter(name: str, factory: Callable[..., MemoryAdapter]) -> None:
    if name in _FACTORIES:
        raise ValueError(f"duplicate adapter {name}")
    _FACTORIES[name] = factory


def adapter_factories() -> dict[str, Callable[..., MemoryAdapter]]:
    return dict(_FACTORIES)


def create_adapter(name: str, **kwargs: object) -> MemoryAdapter:
    try:
        factory = _FACTORIES[name]
    except KeyError as exc:
        raise KeyError(f"unknown adapter {name!r}; known: {sorted(_FACTORIES)}") from exc
    return factory(**kwargs)
