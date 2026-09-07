"""LongMemEval ingestion/retrieval over the existing Exomem product core."""

from __future__ import annotations

import datetime as dt
import importlib.util
from collections.abc import Sequence
from pathlib import Path

from membench.adapters.base import AdapterEnvironmentError, OpResult, Profile
from membench.adapters.exomem_local import ExomemLocalAdapter

from protocol.events import LeakageError
from protocol.leakage import scan_ingest
from protocol.models import CaseGold, CaseHandle, DatasetIdentity, ProtocolEvent

from .dataset import LmeQuestion
from .normalize import ingest_field_groups, neutralize
from .exomem_capture import capture_payload


def lme_profile() -> Profile:
    """Capability-complete profile with deterministic, non-amputating pins."""

    return Profile(
        name="lme-product-defaults",
        settings={
            # Empty is the product's set-only spelling for enabled. It also
            # activates ExomemLocalAdapter's semantic-load refusal in setup.
            "EXOMEM_DISABLE_EMBEDDINGS": "",
            "EXOMEM_DISABLE_WARMUP": "1",
            "EXOMEM_DISABLE_FILE_WATCHER": "1",
            "EXOMEM_DISABLE_MODE_WATCH": "1",
            "EXOMEM_DISABLE_CORPUS_CACHE": "1",
            "EXOMEM_VEC_BACKEND": "numpy",
            "EXOMEM_LEXICAL_BACKEND": "python",
            "EXOMEM_MODE": "normal",
            "EXOMEM_DEVICE": "cpu",
            "EXOMEM_EMBED_DEVICE": "cpu",
            "EXOMEM_CLIP_DEVICE": "cpu",
            "CUDA_VISIBLE_DEVICES": "",
            # The canonical lane is CPU/numpy even on hosts that expose an
            # unusable NVIDIA driver. Device pins above select CPU; this
            # separate doctor allowance alone does not select a device.
            "EXOMEM_ALLOW_CPU_TORCH": "1",
            # Model weights must already exist in the local cache. A cache
            # miss is the semantic environment fault above, never a network
            # side effect hidden inside an otherwise offline benchmark run.
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        },
    )


class LmeExomemAdapter(ExomemLocalAdapter):
    """Per-question LongMemEval adapter using product-default retrieval."""

    name = "exomem-lme"

    def __init__(self) -> None:
        super().__init__(
            altitude="raw_source",
            mode="leaf",
            search_style="product-default",
            governance="off",
            answer_mode="harness",
        )
        self.last_ingest_results: tuple[OpResult, ...] = ()

    def setup(self, workdir: Path, profile: Profile) -> None:
        """Name LME's semantic prerequisites before the shared core can degrade."""

        semantic_requested = not profile.settings.get("EXOMEM_DISABLE_EMBEDDINGS", "1")
        if semantic_requested and importlib.util.find_spec("sentence_transformers") is None:
            raise AdapterEnvironmentError(
                "missing semantic capability: sentence_transformers is not installed; "
                "run `uv sync --extra embeddings` and warm the Hugging Face embedding "
                "model cache before an offline LME run"
            )
        try:
            super().setup(workdir, profile)
            if not profile.settings.get("EXOMEM_DISABLE_CLIP"):
                from exomem import embeddings

                embeddings.get_clip_model()
        except AdapterEnvironmentError:
            self.cleanup()
            raise AdapterEnvironmentError(
                "direct provider setup failed"
            )
        except Exception:
            self.cleanup()
            raise AdapterEnvironmentError(
                "direct provider setup failed"
            )

    def ingest_case(self, events: Sequence[ProtocolEvent], handle: CaseHandle) -> tuple[OpResult, ...]:
        """Capture neutral events; gold-bearing dataset records cannot cross this boundary."""

        if not isinstance(handle, CaseHandle) or not isinstance(events, Sequence):
            raise TypeError("ingest_case accepts Sequence[ProtocolEvent] plus CaseHandle")
        if any(not isinstance(event, ProtocolEvent) or hasattr(event, "answer") for event in events):
            raise TypeError("ingest_case accepts only neutral ProtocolEvent values, never answer-bearing objects")
        if not events or any(event.case_id != handle.case_id for event in events):
            raise TypeError("ingest_case events must be non-empty and match the neutral case handle")
        if self._vault is None or self._schema is None:
            raise AdapterEnvironmentError("adapter not set up")
        from exomem import commands, find as find_module

        results: list[OpResult] = []
        grouped: dict[int, list[ProtocolEvent]] = {}
        for event in events:
            grouped.setdefault(event.session_ordinal, []).append(event)
        for sequence, session_ordinal in enumerate(sorted(grouped)):
            import time

            started = time.perf_counter()
            source_id = f"session-{session_ordinal}"
            session_events = grouped[session_ordinal]
            try:
                captured = commands.op_capture_source(
                    self._vault, self._schema, **capture_payload(session_events),
                )
                source = captured.get("source") if isinstance(captured, dict) else None
                path = source.get("path") if isinstance(source, dict) else None
                if not isinstance(path, str):
                    raise AdapterEnvironmentError("capture_source returned no source path")
                self._register_source_path(source_id, path)
                ok, detail = True, None
            except AdapterEnvironmentError:
                raise
            except Exception:  # failures remain in the question denominator
                ok, detail = False, "direct provider ingestion failed"
            results.append(
                OpResult(
                    seq=sequence,
                    op="capture_source",
                    source_id=source_id,
                    ok=ok,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    detail=detail,
                )
            )
        find_module.clear_cache()
        return tuple(results)

    def retrieve_question(self, question: LmeQuestion, *, limit: int = 10) -> list[str]:
        """Retrieve using the same public clock and command as the guest."""

        return self.retrieve_text(question.question, question.question_date, limit=limit)

    def retrieve_text(self, question_text: str, question_date: dt.datetime, *, limit: int = 10) -> list[str]:
        """Hydrate selected hits through read_memory, exactly as the guest does."""
        from exomem import commands

        del question_date  # Session time belongs in evidence, not a private clock patch.
        hits = self.search(question_text, limit)
        texts: list[str] = []
        for hit in hits:
            response = commands.op_read_memory(self._vault, path=hit.provider_path)
            body = response.get("body") if isinstance(response, dict) else None
            if not isinstance(body, str):
                raise AdapterEnvironmentError("read_memory returned no selected body")
            texts.append(body)
        return texts

    def run_question(
        self, question: LmeQuestion, workdir: Path, *, dataset_identity: DatasetIdentity,
        case_ordinal: int, limit: int = 10,
    ) -> list[str]:
        """Set up, ingest, retrieve, and clean up one isolated question vault."""

        self.setup(Path(workdir), lme_profile())
        try:
            events = neutralize(question, dataset_identity)
            handle = CaseHandle(
                case_id=question.question_id, case_ordinal=case_ordinal,
                question_date=question.question_date_text,
            )
            gold = CaseGold(
                case_id=question.question_id, answer=question.answer,
                answer_session_ids=list(question.answer_session_ids), question_type=question.question_type,
                question=question.question,
            )
            content_fields, authored_literals, harness_fields = ingest_field_groups(events, handle, exomem_capture=True)
            findings = scan_ingest(
                content_fields, authored_literals, harness_fields, gold,
                raw_upstream_session_ids=[session.session_id for session in question.sessions],
            )
            if findings:
                raise LeakageError("; ".join(finding.detector for finding in findings))
            ingested = self.ingest_case(events, handle)
            self.last_ingest_results = ingested
            failed = [result for result in ingested if not result.ok]
            if failed:
                details = "; ".join(result.detail or "capture failed" for result in failed)
                raise AdapterEnvironmentError(
                    f"question {question.question_id!r} ingestion failed: {details}"
                )
            return self.retrieve_question(question, limit=limit)
        finally:
            self.cleanup()
