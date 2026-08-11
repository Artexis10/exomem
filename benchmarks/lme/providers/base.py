"""The deliberately narrow, gold-free direct-provider boundary."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from membench.adapters.base import Profile
from protocol.models import CaseHandle, LaneReadiness, ProtocolEvent


@dataclass(frozen=True)
class ProviderHit:
    """Normalized retrieval response retained in trace artifacts."""

    hit_id: str
    text: str
    score: float


class RetrievalPurpose(str, Enum):
    SCORED_RETRIEVAL = "scored-retrieval"
    POSITIVE_PROBE = "positive-probe"
    ABSENCE_PROBE_EXPECTED_EMPTY = "absence-probe-expected-empty"


@dataclass(frozen=True)
class ProviderSessionContext:
    """Runner-owned identity and contained paths for one provider instance."""

    run_id: str
    session_id: str
    namespace: str
    work_root: Path
    evidence_root: Path


@dataclass(frozen=True)
class ProviderRuntimeBinding:
    """Runner-owned observations; providers never supply cleanup verdicts."""

    required_surface_ids: tuple[str, ...]
    observe: Callable[[ProviderSessionContext, object], tuple[dict[str, object], ...]]


@dataclass(frozen=True)
class ProviderSpec:
    factory: Callable[[], object]
    descriptor: str
    namespace_kind: str
    derive_namespace: Callable[[str, str], str]
    runtime_binding: ProviderRuntimeBinding


@runtime_checkable
class DirectProvider(Protocol):
    """Only neutral protocol events and a case handle may cross this boundary."""

    def setup(self, profile: Profile | None, context: ProviderSessionContext) -> None: ...
    def ingest_case(self, events: Sequence[ProtocolEvent], handle: CaseHandle) -> object: ...
    def retrieve(self, question_text: str, top_k: int, purpose: RetrievalPurpose) -> list[ProviderHit]: ...
    def export_state(self) -> object: ...
    def cleanup(self) -> None: ...
    def variant_id(self) -> str: ...
    def readiness(self) -> list[LaneReadiness]: ...


def require_neutral(events: Sequence[ProtocolEvent], handle: CaseHandle) -> None:
    """Mirror the adapter boundary: gold-bearing shapes are structurally refused."""

    if not isinstance(handle, CaseHandle) or not isinstance(events, Sequence):
        raise TypeError("direct providers accept Sequence[ProtocolEvent] plus CaseHandle")
    if not events or any(not isinstance(event, ProtocolEvent) or hasattr(event, "answer") for event in events):
        raise TypeError("direct providers accept only neutral ProtocolEvent values")
    if any(event.case_id != handle.case_id for event in events):
        raise TypeError("direct provider events must match the neutral case handle")
