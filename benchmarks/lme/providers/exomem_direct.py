"""Direct-provider facade over the established leak-free Exomem adapter."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from membench.adapters.base import Profile
from membench.adapters.base import AdapterEnvironmentError
from protocol.custody import retire_child_directory
from protocol.models import CaseHandle, LaneReadiness, ProtocolEvent
from protocol.readiness import semantic_doctor_readiness

from ..adapter import LmeExomemAdapter, lme_profile
from .base import ProviderHit, ProviderSessionContext, RetrievalPurpose, require_neutral


class ExomemDirectProvider:
    """Adapt the legacy adapter to the narrow direct-provider contract.

    The adapter remains the only code that writes and searches the product
    vault.  This wrapper merely supplies its workdir/profile requirements and
    retains neutral case metadata without overriding the product clock.
    """

    def __init__(self) -> None:
        self._adapter = LmeExomemAdapter()
        self._context: ProviderSessionContext | None = None
        self._question_date: dt.datetime | None = None
        self._profile: Profile | None = None
        self.last_doctor_report: dict | None = None

    def setup(self, profile: Profile | None, context: ProviderSessionContext) -> None:
        self._profile = profile or lme_profile()
        self._context = context
        try:
            context.work_root.mkdir(parents=True, exist_ok=True)
            self._adapter.setup(context.work_root, self._profile)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise AdapterEnvironmentError("direct provider setup failed") from exc

    def ingest_case(self, events: Sequence[ProtocolEvent], handle: CaseHandle) -> tuple[str, ...]:
        require_neutral(events, handle)
        self._question_date = dt.datetime.fromisoformat(handle.question_date.replace("Z", "+00:00"))
        try:
            results = self._adapter.ingest_case(events, handle)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise RuntimeError("direct provider ingestion failed") from exc
        self._adapter.last_ingest_results = results
        if any(not result.ok for result in results):
            raise RuntimeError("direct provider ingestion failed")
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
            try:
                self._close_writer_runtime()
            finally:
                self._adapter.cleanup()
            if self._context is not None:
                for component in ("vault", "logs", "leases"):
                    retire_child_directory(
                        self._context.work_root,
                        component,
                        max_entries=100_000,
                        max_depth=64,
                    )
        finally:
            self._context = None
            self._question_date = None
            self._adapter.last_ingest_results = ()

    def _close_writer_runtime(self) -> None:
        """Retire only this case's in-process leases before its FD is reusable.

        The product caches managers by configured path. A runner capability
        path can name a different inode in the next case after FD reuse.
        Do not reset the process-wide cache or resolve the held path back to
        an unprotected filesystem name. Setup failures can create a manager,
        so find owned entries even when adapter setup did not return.
        The runner calls this only after synchronous provider operations
        return; the local lease manager does not count active mutations.
        """
        if self._context is None:
            return
        from exomem import writer_lease

        state_dir = self._context.work_root / "leases"
        with writer_lease._MANAGERS_LOCK:
            owned = [
                (config, manager) for config, manager in writer_lease._MANAGERS.items()
                if config.state_dir == state_dir
            ]
            for config, manager in owned:
                with manager._lock:
                    if manager._renewer is not None and manager._renewer.is_alive():
                        raise RuntimeError("direct provider lease renewer is still active")
                    manager.close()
                    handle = manager.idempotency._owner_lock_handle
                    if handle is not None:
                        handle.close()
                        manager.idempotency._owner_lock_handle = None
                    del writer_lease._MANAGERS[config]

    def variant_id(self) -> str:
        return "exomem-source-only"

    def readiness(self) -> list[LaneReadiness]:
        profile = self._profile or lme_profile()
        disabled = bool(profile.settings.get("EXOMEM_DISABLE_EMBEDDINGS"))
        if disabled:
            return [LaneReadiness(lane="semantic", requested=False, verified=False, method="doctor-check", evidence="semantic lane not requested")]
        if self._adapter._vault is None:
            raise RuntimeError("readiness called before setup")
        from exomem import doctor

        report = doctor.doctor(vault=str(self._adapter._vault), profile="hybrid").as_dict()
        self.last_doctor_report = report
        return [semantic_doctor_readiness(report)]
