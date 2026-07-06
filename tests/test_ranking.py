"""EXOMEM_DISABLE_RANKING: the reranker never preloads or loads (the 'lite' knob)."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import embeddings, readiness, warmup


@pytest.fixture(autouse=True)
def _reset_readiness() -> None:
    readiness.reset()
    yield
    readiness.reset()


def test_ranking_enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_RANKING", raising=False)
    assert embeddings.ranking_enabled() is True
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    assert embeddings.ranking_enabled() is False


def test_warm_all_skips_reranker_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reranker getter is never called when ranking is disabled, but the component
    is still marked ready (bge + CLIP preload normally)."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    monkeypatch.setattr(warmup, "warm_caches", lambda vr, **_kw: {})
    monkeypatch.setattr(embeddings, "get_model", lambda: object())

    def _forbidden():
        raise AssertionError("reranker must not load when EXOMEM_DISABLE_RANKING is set")

    monkeypatch.setattr(embeddings, "get_reranker", _forbidden)
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: object())
    monkeypatch.setattr(embeddings, "clip_enabled", lambda: True)

    warmup.warm_all(tmp_path)

    assert readiness.is_ready("embeddings") is True
    assert readiness.is_ready("reranker") is True  # ready even though never loaded
    assert readiness.is_ready("clip") is True


def test_reranker_name_default() -> None:
    """Default reranker unchanged (the env override is resolved at import; default here)."""
    assert embeddings.RERANKER_NAME == "BAAI/bge-reranker-base"
