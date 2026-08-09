"""LongMemEval ingestion/retrieval over the existing Exomem product core."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from membench.adapters.base import AdapterEnvironmentError, OpResult, Profile
from membench.adapters.exomem_local import ExomemLocalAdapter

from .dataset import LmeQuestion, LmeSession
from .normalize import neutral_tags, neutral_title, neutralize, render_neutral_session


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
        capability = "text embedding model"
        try:
            super().setup(workdir, profile)
            if not profile.settings.get("EXOMEM_DISABLE_CLIP"):
                capability = "CLIP semantic model"
                from exomem import embeddings

                embeddings.get_clip_model()
        except AdapterEnvironmentError as exc:
            self.cleanup()
            raise AdapterEnvironmentError(
                f"missing semantic capability: the {capability} could not load; "
                "run `uv sync --extra embeddings` and warm the Hugging Face embedding "
                f"model cache before an offline LME run ({exc})"
            ) from exc
        except Exception as exc:
            self.cleanup()
            raise AdapterEnvironmentError(
                f"missing semantic capability: the {capability} could not load "
                f"({type(exc).__name__}: {str(exc)[:160]}); run "
                "`uv sync --extra embeddings` and warm the Hugging Face embedding "
                "model cache before an offline LME run"
            ) from exc

    @staticmethod
    def _slug(question: LmeQuestion, session: LmeSession) -> str:
        digest = hashlib.sha256(
            f"{question.question_id}:{session.session_id}".encode("utf-8")
        ).hexdigest()[:10]
        return f"lme-session-{digest}"

    def ingest_question(self, question: LmeQuestion) -> tuple[OpResult, ...]:
        """Capture every session through ``op_capture_source`` at its own time."""

        if not isinstance(question, LmeQuestion):
            raise TypeError("ingest_question accepts LmeQuestion, never gold-bearing records")
        if self._vault is None or self._schema is None:
            raise AdapterEnvironmentError("adapter not set up")
        from exomem import commands, find as find_module, temporal

        results: list[OpResult] = []
        events = neutralize(question)
        for sequence, session in enumerate(question.sessions):
            import time

            started = time.perf_counter()
            session_ordinal = sequence + 1
            source_id = f"session-{session_ordinal}"
            session_events = [event for event in events if event.session_ordinal == session_ordinal]
            try:
                # The product writer owns frontmatter. Clocking that public
                # write is how the session timestamp lands in `captured:`
                # without post-editing an append-only governed Source.
                with mock.patch.object(temporal, "now", return_value=session.timestamp):
                    captured = commands.op_capture_source(
                        self._vault,
                        self._schema,
                        content=render_neutral_session(session_events),
                        title=neutral_title(1, session_ordinal),
                        slug=self._slug(question, session),
                        source_type="session",
                        tags=neutral_tags(),
                    )
                source = captured.get("source") if isinstance(captured, dict) else None
                path = source.get("path") if isinstance(source, dict) else None
                if not isinstance(path, str):
                    raise AdapterEnvironmentError("capture_source returned no source path")
                self._register_source_path(source_id, path)
                ok, detail = True, None
            except AdapterEnvironmentError:
                raise
            except Exception as exc:  # failures remain in the question denominator
                ok, detail = False, f"{type(exc).__name__}: {exc}"
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
        """Retrieve at ``question_date`` using hybrid product defaults."""

        from exomem import find as find_module
        from exomem import find_policy, structured_filters, temporal

        pinned_day = question.question_date.date()

        class RetrievalDate(dt.date):
            @classmethod
            def today(cls) -> dt.date:
                return cls(pinned_day.year, pinned_day.month, pinned_day.day)

        # The read path imports ``date`` into three modules, so patch each
        # consumer for exactly one search call. ``temporal.now`` remains pinned
        # for read-side helpers that use the product clock abstraction.
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(find_policy, "date", RetrievalDate))
            stack.enter_context(mock.patch.object(find_module, "date", RetrievalDate))
            stack.enter_context(mock.patch.object(structured_filters, "date", RetrievalDate))
            stack.enter_context(
                mock.patch.object(temporal, "now", return_value=question.question_date)
            )
            hits = self.search(question.question, limit)
        return [hit.text or hit.excerpt or "" for hit in hits if hit.text or hit.excerpt]

    def run_question(
        self, question: LmeQuestion, workdir: Path, *, limit: int = 10
    ) -> list[str]:
        """Set up, ingest, retrieve, and clean up one isolated question vault."""

        self.setup(Path(workdir), lme_profile())
        try:
            ingested = self.ingest_question(question)
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
