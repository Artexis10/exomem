"""Negative retrieval control: it never retains or returns any material."""

from __future__ import annotations

from collections.abc import Sequence

from membench.adapters.base import Profile
from protocol.models import CaseHandle, LaneReadiness, ProtocolEvent

from .base import ProviderHit, require_neutral


class NullDirectProvider:
    def setup(self, profile: Profile | None) -> None:
        del profile

    def ingest_case(self, events: Sequence[ProtocolEvent], handle: CaseHandle) -> tuple[()]:
        require_neutral(events, handle)
        return ()

    def retrieve(self, question_text: str, top_k: int) -> list[ProviderHit]:
        del question_text, top_k
        return []

    def export_state(self) -> tuple[()]:
        return ()

    def cleanup(self) -> None:
        return None

    def variant_id(self) -> str:
        return "no-memory"

    def readiness(self) -> list[LaneReadiness]:
        return [LaneReadiness(lane=self.variant_id(), requested=True, verified=True, method="config-state", evidence="negative control")]
