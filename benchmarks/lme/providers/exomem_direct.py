"""Direct-provider facade over the established leak-free Exomem adapter."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from membench.adapters.base import Profile
from protocol.models import CaseHandle, LaneReadiness, ProtocolEvent

from ..adapter import LmeExomemAdapter, lme_profile
from .base import ProviderHit, ProviderSessionContext, RetrievalPurpose, require_neutral


class ExomemDirectProvider:
    """Adapt the legacy adapter to the narrow direct-provider contract.

    The adapter remains the only code that writes and searches the product
    vault.  This wrapper merely supplies its workdir/profile requirements and
    retains the neutral case clock required for retrieval.
    """

    def __init__(self) -> None:
        self._adapter = LmeExomemAdapter()
        self._context: ProviderSessionContext | None = None
        self._question_date: dt.datetime | None = None
        self._profile: Profile | None = None

    def setup(self, profile: Profile | None, context: ProviderSessionContext) -> None:
        self._profile = profile or lme_profile()
        self._context = context
        context.work_root.mkdir(parents=True, exist_ok=True)
        self._adapter.setup(context.work_root, self._profile)

    def ingest_case(self, events: Sequence[ProtocolEvent], handle: CaseHandle) -> tuple[str, ...]:
        require_neutral(events, handle)
        self._question_date = dt.datetime.fromisoformat(handle.question_date.replace("Z", "+00:00"))
        results = self._adapter.ingest_case(events, handle)
        self._adapter.last_ingest_results = results
        failures = [result for result in results if not result.ok]
        if failures:
            raise RuntimeError("Exomem ingestion failed: " + "; ".join(result.detail or "unknown error" for result in failures))
        return tuple(result.source_id for result in results)

    def retrieve(self, question_text: str, top_k: int, purpose: RetrievalPurpose) -> list[ProviderHit]:
        del purpose
        if self._question_date is None:
            raise RuntimeError("retrieve called before ingest_case")
        hits = self._adapter.retrieve_text(question_text, self._question_date, limit=top_k)
        return [ProviderHit(f"exomem-{index}", text, 0.0) for index, text in enumerate(hits)]

    def export_state(self) -> tuple[object, ...]:
        return tuple(self._adapter.last_ingest_results)

    def cleanup(self) -> None:
        try:
            self._adapter.cleanup()
        finally:
            if self._context is not None and self._context.work_root.exists():
                import shutil
                shutil.rmtree(self._context.work_root)
            self._context = None
            self._question_date = None
            self._adapter.last_ingest_results = ()

    def variant_id(self) -> str:
        return "exomem-source-only"

    def readiness(self) -> list[LaneReadiness]:
        profile = self._profile or lme_profile()
        disabled = bool(profile.settings.get("EXOMEM_DISABLE_EMBEDDINGS"))
        return [LaneReadiness(
            lane="semantic",
            requested=not disabled,
            verified=False,
            method="readiness-unverifiable",
            evidence="semantic readiness is established by recorded known-answer probes",
        )]
