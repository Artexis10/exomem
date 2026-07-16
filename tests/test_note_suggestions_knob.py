"""note(suggestions=) gates ONLY the related-links pass; dedupe stays on.

The corpus-aware block in note() runs two independent passes: the
link-suggestion query (a find-class nicety) and the near-dup/contradiction
embedding sweep (a dedupe GUARDRAIL the skill's discipline depends on).
`suggestions=False` must skip the first and keep the second; the default
(True) runs both — concurrently, but that's an implementation detail these
tests deliberately don't pin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import corpus_aware
from exomem import note as note_module


@pytest.fixture
def corpus_spies(vault: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Enable the corpus block, spy on both passes, stub the heavy paths."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    calls = {"suggest": 0, "cosine": 0}

    def fake_suggest(*a, **k):
        calls["suggest"] += 1
        return []

    def fake_cosines(*a, **k):
        calls["cosine"] += 1
        return {}

    monkeypatch.setattr(corpus_aware, "suggest_related", fake_suggest)
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", fake_cosines)
    # Keep the post-write sidecar sync from touching the embedding model.
    from exomem import embeddings

    monkeypatch.setattr(embeddings, "upsert_after_write", lambda *a, **k: None)
    return calls


def test_default_runs_suggestions_and_guardrail(
    vault: Path, corpus_spies: dict[str, int]
) -> None:
    note_module.note(
        vault,
        content="# Knob default probe\n\nBody.",
        note_type="insight",
        title="Knob default probe",
        status="draft",
    )
    assert corpus_spies == {"suggest": 1, "cosine": 1}


def test_suggestions_false_skips_related_keeps_dedupe(
    vault: Path, corpus_spies: dict[str, int]
) -> None:
    note_module.note(
        vault,
        content="# Knob off probe\n\nBody.",
        note_type="insight",
        title="Knob off probe",
        suggestions=False,
        status="draft",
    )
    assert corpus_spies == {"suggest": 0, "cosine": 1}
