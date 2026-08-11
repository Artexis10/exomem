"""Negative retrieval control: it never retains or returns any material."""

from __future__ import annotations

from collections.abc import Sequence

from membench.adapters.base import Profile
from protocol.models import CaseHandle, LaneReadiness, ProtocolEvent

from .base import ProviderHit, ProviderSessionContext, RetrievalPurpose, require_neutral


class NullDirectProvider:
    #: By-design declaration read by the runner's canary evaluation.  A control
    #: that stores nothing can never retrieve its own presence canary; scoring
    #: that as "unverifiable" would invalidate the very floor this row exists to
    #: provide, so the runner treats presence as inconclusive-by-design here and
    #: still lets a cross-case or never-ingested hit contaminate the run.
    retains_nothing = True

    def setup(self, profile: Profile | None, context: ProviderSessionContext) -> None:
        del profile, context

    def ingest_case(self, events: Sequence[ProtocolEvent], handle: CaseHandle) -> tuple[()]:
        require_neutral(events, handle)
        return ()

    def retrieve(self, question_text: str, top_k: int, purpose: RetrievalPurpose) -> list[ProviderHit]:
        del question_text, top_k, purpose
        return []

    def export_state(self) -> tuple[()]:
        return ()

    def cleanup(self) -> None:
        return None

    def variant_id(self) -> str:
        return "no-memory"

    def readiness(self) -> list[LaneReadiness]:
        return [LaneReadiness(lane=self.variant_id(), requested=True, verified=True, method="config-state", evidence="negative control")]
