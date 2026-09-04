from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

from exomem.init import init_vault
from exomem.schema import load_source_schema
from lme import adapter as adapter_module
from lme.adapter import LmeExomemAdapter, lme_profile
from lme.dataset import load_dataset
from membench.adapters.base import AdapterEnvironmentError
from membench.adapters.exomem_local import ExomemLocalAdapter
from protocol.models import CaseGold, CaseHandle, DatasetIdentity


FIXTURE = Path("benchmarks/lme/fixtures/mini.json")


def _primed_adapter(vault: Path) -> LmeExomemAdapter:
    init_vault(vault)
    adapter = LmeExomemAdapter()
    adapter._vault = vault
    adapter._schema = load_source_schema(vault)
    return adapter


def _identity() -> DatasetIdentity:
    return DatasetIdentity(id="longmemeval", variant="mini", source="local", revision="fixture-pin", sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), case_count=2)


def test_adapter_subclasses_product_core_and_keeps_default_capabilities_on() -> None:
    adapter = LmeExomemAdapter()
    assert isinstance(adapter, ExomemLocalAdapter)
    assert adapter._search_kwargs() == {"scope": "kb", "detail": "full", "mode": "hybrid"}
    settings = lme_profile().settings
    assert settings["EXOMEM_DISABLE_EMBEDDINGS"] == ""
    assert settings["EXOMEM_ALLOW_CPU_TORCH"] == "1"
    assert settings["HF_HUB_OFFLINE"] == "1"
    assert settings["TRANSFORMERS_OFFLINE"] == "1"
    assert "EXOMEM_DISABLE_CLIP" not in settings
    assert "EXOMEM_DISABLE_MEDIA_EXTRACTION" not in settings


def test_lme_profile_selects_cpu_without_probing_ambient_accelerators(monkeypatch) -> None:
    from exomem import accel, mode

    ambient = {
        "EXOMEM_MODE": "performance", "EXOMEM_DEVICE": "cuda",
        "EXOMEM_EMBED_DEVICE": "cuda:1", "EXOMEM_CLIP_DEVICE": "mps",
        "CUDA_VISIBLE_DEVICES": "0",
    }
    for key, value in ambient.items():
        monkeypatch.setenv(key, value)

    def unexpected_probe(**kwargs):
        pytest.fail("the CPU benchmark profile probed an accelerator")

    monkeypatch.setattr(accel, "_auto_device", unexpected_probe)
    adapter = LmeExomemAdapter()
    adapter._set_env(lme_profile().settings)
    try:
        for override in (None, "EXOMEM_EMBED_DEVICE", "EXOMEM_CLIP_DEVICE"):
            assert accel.select_device(override_env=override) == "cpu"
        assert mode.resolve_mode() == "normal"
        assert os.environ["CUDA_VISIBLE_DEVICES"] == ""
    finally:
        adapter._restore_env()
    assert {key: os.environ[key] for key in ambient} == ambient


def test_each_question_uses_an_isolated_vault_and_captures_session_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    questions = load_dataset(FIXTURE).questions[:2]
    # The test suite's global offline pin keeps capture lightweight. Production
    # lme_profile explicitly turns the semantic lane back on and refuses load failure.
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    first = _primed_adapter(tmp_path / "first")
    second = _primed_adapter(tmp_path / "second")
    from lme.normalize import neutralize

    first.ingest_case(neutralize(questions[0], _identity()), CaseHandle(case_id=questions[0].question_id, case_ordinal=1, question_date=questions[0].question_date_text))
    second.ingest_case(neutralize(questions[1], _identity()), CaseHandle(case_id=questions[1].question_id, case_ordinal=2, question_date=questions[1].question_date_text))

    first_text = "\n".join(page.text for page in first.export_state().pages)
    second_text = "\n".join(page.text for page in second.export_state().pages)
    timestamp = questions[0].sessions[0].timestamp_text
    assert f"captured: {timestamp}" in first_text
    assert f"Session timestamp: {timestamp}" in first_text
    assert "Vorstead" in first_text
    assert "Vorstead" not in second_text


def test_retrieval_clock_reaches_the_actual_read_side_date_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import find as find_module
    from exomem import find_policy, structured_filters
    from exomem.ranking_config import DEFAULT_RANKING

    question = load_dataset(FIXTURE).questions[0]
    adapter = LmeExomemAdapter()
    observed: dict[str, dt.date] = {}

    def fake_search(query: str, limit: int):
        del limit
        observed["find"] = find_module.date.today()
        observed["structured_filters"] = structured_filters.date.today()
        find_policy.apply_post_rrf_multipliers(
            [],
            query + " today",
            DEFAULT_RANKING,
            prefer_compiled=True,
            prefer_active=True,
            temporal=True,
            page_of=lambda _path: None,
        )
        observed["find_policy"] = find_policy.date.today()
        return []

    monkeypatch.setattr(adapter, "search", fake_search)
    assert adapter.retrieve_question(question) == []
    assert set(observed.values()) == {question.question_date.date()}


@pytest.mark.parametrize("value", [
    load_dataset(FIXTURE).questions[0],
    CaseGold(case_id="c", answer="a", answer_session_ids=[], question_type="knowledge-update", question="q"),
    type("AnswerBearing", (), {"answer": "secret"})(),
])
def test_ingest_case_rejects_gold_bearing_values(value: object) -> None:
    with pytest.raises(TypeError):
        LmeExomemAdapter().ingest_case(value, CaseHandle(case_id="c", case_ordinal=1, question_date="2026-01-01T00:00:00Z"))  # type: ignore[arg-type]


def test_missing_semantic_prerequisite_names_capability_and_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_find_spec = importlib.util.find_spec

    def missing_sentence_transformers(name: str, *args, **kwargs):
        if name == "sentence_transformers":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(adapter_module.importlib.util, "find_spec", missing_sentence_transformers)
    with pytest.raises(
        AdapterEnvironmentError,
        match=r"sentence_transformers.*uv sync --extra embeddings.*warm.*cache",
    ):
        LmeExomemAdapter().setup(tmp_path / "provider", lme_profile())


@pytest.mark.skipif(
    importlib.util.find_spec("sentence_transformers") is None,
    reason="requires the embeddings extra and a warm Hugging Face cache",
)
def test_run_question_uses_the_real_product_lifecycle(tmp_path: Path) -> None:
    question = load_dataset(FIXTURE).questions[0]
    try:
        retrieved = LmeExomemAdapter().run_question(question, tmp_path / "question", dataset_identity=_identity(), case_ordinal=1)
    except AdapterEnvironmentError as exc:
        # The importability skipif above cannot see a cold model cache under
        # the profile's offline pins; an environment fault here is the same
        # precondition failure, never a product defect — skip, don't fail.
        pytest.skip(f"semantic prerequisites unavailable (cold model cache): {exc}")
    assert isinstance(retrieved, list)
