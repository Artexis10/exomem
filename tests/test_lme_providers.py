"""RB2 / RM8: every registry row is a real DirectProvider, exercised end to end."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest

FIXTURE = Path("benchmarks/lme/fixtures/mini.json")

# name -> environment the row legitimately needs to run offline.
_EXPECTED_SIGNATURES = {
    "setup": ["profile", "context"],
    "ingest_case": ["events", "handle"],
    "retrieve": ["question_text", "top_k", "purpose"],
    "export_state": [],
    "cleanup": [],
    "variant_id": [],
    "readiness": [],
}


def _identity():
    from protocol.models import DatasetIdentity

    return DatasetIdentity(
        id="longmemeval", variant="mini", source="local", revision="fixture-pin",
        sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), case_count=6,
    )


def _offline_profile():
    """The exomem row needs a real product profile; pin it away from model loads."""

    from membench.adapters.base import Profile

    return Profile(
        name="conformance-offline",
        settings={
            "EXOMEM_DISABLE_EMBEDDINGS": "1", "EXOMEM_DISABLE_WARMUP": "1",
            "EXOMEM_DISABLE_FILE_WATCHER": "1", "EXOMEM_DISABLE_MODE_WATCH": "1",
            "EXOMEM_DISABLE_CORPUS_CACHE": "1", "EXOMEM_DISABLE_CLIP": "1",
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        },
    )


@pytest.mark.parametrize("name", ["exomem-source-only", "hybrid-rag-control", "no-memory"])
def test_every_registered_provider_implements_and_runs_the_direct_boundary(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lme.dataset import load_dataset
    from lme.normalize import neutralize
    from lme.providers.base import DirectProvider, ProviderHit, ProviderSessionContext, RetrievalPurpose
    from lme.providers.registry import provider_factory, registered_provider_names
    from protocol.models import CaseHandle, LaneReadiness

    assert name in registered_provider_names()
    monkeypatch.setenv("PROTOCOL_FIXTURE_EMBEDDER", "1")
    provider = provider_factory(name)()
    assert isinstance(provider, DirectProvider)
    for method, parameters in _EXPECTED_SIGNATURES.items():
        bound = getattr(provider, method)
        assert callable(bound), f"{name}.{method} is not callable"
        assert list(inspect.signature(bound).parameters) == parameters, (
            f"{name}.{method} does not match the DirectProvider signature"
        )

    question = load_dataset(FIXTURE).questions[0]
    events = neutralize(question, _identity())
    handle = CaseHandle(case_id=question.question_id, case_ordinal=1, question_date=question.question_date_text)
    provider.setup(_offline_profile(), ProviderSessionContext("provider-test", "session", "namespace", tmp_path / "work", tmp_path / "evidence"))
    try:
        inserted = provider.ingest_case(events, handle)
        assert isinstance(inserted, tuple)
        hits = provider.retrieve(question.question, 3, RetrievalPurpose.SCORED_RETRIEVAL)
        assert isinstance(hits, list)
        assert all(isinstance(hit, ProviderHit) for hit in hits)
        assert provider.export_state() is not None
        readiness = provider.readiness()
        assert readiness and all(isinstance(lane, LaneReadiness) for lane in readiness)
        assert isinstance(provider.variant_id(), str) and provider.variant_id()
    finally:
        provider.cleanup()


def test_registry_refuses_an_unknown_provider_name() -> None:
    from lme.providers.registry import provider_factory, registered_provider_names

    with pytest.raises(ValueError, match="unknown direct provider"):
        provider_factory("supermemory-cloud")
    assert "supermemory-cloud" not in registered_provider_names()


def test_the_boundary_refuses_a_genuinely_gold_bearing_object() -> None:
    """RM8: gold-bearing shapes are structurally unpassable, not merely discouraged."""

    from lme.dataset import load_dataset
    from lme.providers.base import require_neutral
    from lme.providers.registry import provider_factory
    from protocol.models import CaseGold, CaseHandle

    handle = CaseHandle(case_id="c", case_ordinal=1, question_date="2026-01-01T00:00:00Z")
    gold = CaseGold(case_id="c", answer="violet cedar lantern", answer_session_ids=["answer_1"], question_type="knowledge-update", question="Which lantern?")
    question = load_dataset(FIXTURE).questions[0]
    for value in (gold, [gold], question, [question], [type("AnswerBearing", (), {"answer": "secret"})()]):
        with pytest.raises(TypeError):
            require_neutral(value, handle)  # type: ignore[arg-type]
    for name in ("hybrid-rag-control", "no-memory"):
        with pytest.raises(TypeError):
            provider_factory(name)().ingest_case([gold], handle)  # type: ignore[arg-type]


def test_the_null_control_returns_nothing_for_any_query(tmp_path: Path) -> None:
    from lme.dataset import load_dataset
    from lme.normalize import neutralize
    from lme.providers.base import ProviderSessionContext, RetrievalPurpose
    from lme.providers.null_direct import NullDirectProvider
    from protocol.models import CaseHandle

    question = load_dataset(FIXTURE).questions[0]
    provider = NullDirectProvider()
    provider.setup(None, ProviderSessionContext("null-test", "session", "namespace", tmp_path / "work", tmp_path / "evidence"))
    handle = CaseHandle(case_id=question.question_id, case_ordinal=1, question_date=question.question_date_text)
    assert provider.ingest_case(neutralize(question, _identity()), handle) == ()
    assert provider.retrieve(question.question, 10, RetrievalPurpose.SCORED_RETRIEVAL) == []
    assert provider.export_state() == ()
    assert provider.retains_nothing is True
    provider.cleanup()
