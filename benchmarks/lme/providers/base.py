"""The deliberately narrow, gold-free direct-provider boundary."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from membench.adapters.base import Profile
from protocol.models import CaseHandle, LaneReadiness, ProtocolEvent


@dataclass(frozen=True)
class ProviderHit:
    """Normalized retrieval response retained in trace artifacts."""

    hit_id: str
    text: str
    score: float


@runtime_checkable
class DirectProvider(Protocol):
    """Only neutral protocol events and a case handle may cross this boundary."""

    def setup(self, profile: Profile | None) -> None: ...
    def ingest_case(self, events: Sequence[ProtocolEvent], handle: CaseHandle) -> object: ...
    def retrieve(self, question_text: str, top_k: int) -> list[ProviderHit]: ...
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
