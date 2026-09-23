"""Hybrid fusion when the lexical lanes cannot see the dense lead.

The vector lane is the only lane that matches a page written in another
language than the query. When its strongest candidate shares no content word
with the query, the lexical lanes are blind to it, and their votes rank other
pages by vocabulary overlap with the query: mostly pages in the query's own
language that share a word or two with it. Reciprocal-rank fusion then let one
such partial match plus a weaker dense vote outrank the dense lead (the
language-bias twin bar of the multilingual recall design, §17.6.2).

With the dense lead lexically invisible, a lexical lane stops voting for a page
that matches the query only in part AND shares no vocabulary with the dense
lead (at most a stray loanword or number: a page in another language). Such a
page keeps its dense and other votes. A page holding every content word of the
query, or written in the lead's vocabulary, keeps its lexical votes, and when
the dense lead shares a content word with the query fusion is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import embeddings as embeddings_module
from exomem import find as find_module
from exomem.kbdir import kb_dirname

_GOLD = "Notes/Patterns/retry-with-backoff.md"
_POISON = "Notes/Languages/de/wiederholungspruefung.md"
_FULL = "Notes/Languages/de/verzoegerung.md"
_OTHER = "Notes/Languages/de/kantine.md"
_REVIEW = "Notes/Failures/incident-review.md"

_PAGES: dict[str, tuple[str, str]] = {
    _GOLD: (
        "Retry with backoff",
        "Retries wait an exponentially growing, jittered delay so that failing "
        "clients do not hammer a recovering service.",
    ),
    _POISON: (
        "Wiederholungsprüfung",
        "Die Wiederholung der Prüfung findet mit dem Ausbilder im Schulungsraum statt.",
    ),
    _FULL: (
        "Verzögerung der Lieferung",
        "Die Lieferung kommt mit Verzögerung, weil der Wiederholungsversuch exponentieller "
        "Anfragen scheiterte.",
    ),
    _OTHER: (
        "Kantine",
        "Die Kantine bietet montags Müsli mit Brötchen an.",
    ),
    _REVIEW: (
        "Incident review",
        "The incident review found that failing clients did not wait for a recovering service.",
    ),
}


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    for rel, (title, body) in _PAGES.items():
        page_path = root / kb_dirname() / rel
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(
            f"---\ntype: note\ntitle: {title}\nupdated: 2026-09-01\n---\n\n# {title}\n\n{body}\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    find_module.clear_cache()
    embeddings_module.clear_embedding_indexes()
    return root


def _plant_vector_lane(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    """The dense lane ranks `order` (vault-relative under the KB) best first."""
    ranked = [f"{kb_dirname()}/{rel}" for rel in order]

    class FakeIndex:
        def search(self, _query_vector, *, k: int, allowed_paths=None):
            return [
                (path, 0, "chunk", 0.9 - 0.05 * position)
                for position, path in enumerate(ranked)
                if allowed_paths is None or path in allowed_paths
            ][:k]

    monkeypatch.setattr(embeddings_module, "get_embedding_index", lambda _root: FakeIndex())
    monkeypatch.setattr(embeddings_module, "embed_texts", lambda _texts, *, is_query: [[0.1, 0.2]])


def _ranked(vault: Path, query: str) -> list[str]:
    hits = find_module.find(vault, query=query, limit=10, mode="hybrid", rerank=False, graph=False)
    return [hit.path.removeprefix(f"{kb_dirname()}/") for hit in hits]


def test_a_partial_same_language_match_does_not_outrank_an_invisible_dense_lead(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The German query shares "mit" with the poison and nothing with the
    # English gold, which the dense lane ranks first.
    _plant_vector_lane(monkeypatch, [_GOLD, _POISON, _OTHER])
    ranked = _ranked(vault, "Wiederholungsversuche mit exponentieller Rücksetzung")
    assert ranked[0] == _GOLD
    assert ranked.index(_POISON) > ranked.index(_GOLD)


def test_a_full_lexical_match_keeps_its_votes_against_an_invisible_dense_lead(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every content word of the query is on the full-match page, which the
    # dense lane ranks below the invisible English page: its lexical votes stand.
    _plant_vector_lane(monkeypatch, [_GOLD, _OTHER, _POISON, _FULL])
    ranked = _ranked(vault, "Lieferung mit Verzögerung")
    assert ranked[0] == _FULL


def test_fusion_is_unchanged_when_the_dense_lead_shares_a_content_word(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dense lead is the poison itself, which the query's words reach, so
    # the lexical lanes can see it and every lexical vote stands: the kantine
    # page, a dense runner-up that shares "mit", still takes its BM25 vote.
    _plant_vector_lane(monkeypatch, [_POISON, _GOLD, _OTHER])
    guarded = _ranked(vault, "Wiederholung mit Ausbilder")
    monkeypatch.setattr(
        find_module.find_candidates,
        "_lexical_visibility",
        lambda **_kwargs: find_module.find_candidates.LexicalVisibility(frozenset(), False),
    )
    find_module.clear_cache()
    assert guarded == _ranked(vault, "Wiederholung mit Ausbilder")
    assert guarded[0] == _POISON


def test_a_query_of_function_words_only_is_fused_as_before(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_vector_lane(monkeypatch, [_GOLD, _POISON, _OTHER])
    guarded = _ranked(vault, "what is the")
    monkeypatch.setattr(
        find_module.find_candidates,
        "_lexical_visibility",
        lambda **_kwargs: find_module.find_candidates.LexicalVisibility(frozenset(), False),
    )
    find_module.clear_cache()
    assert guarded == _ranked(vault, "what is the")


def test_a_partial_match_in_the_dense_leads_own_vocabulary_keeps_its_votes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An English query with no relevant page: the dense lead is an English page
    # the query's words do not reach, and the English review page matches one
    # word. Both are English, so nothing about language is at stake and the
    # lexical vote stands, exactly as without the guard.
    _plant_vector_lane(monkeypatch, [_GOLD, _OTHER, _REVIEW])
    guarded = _ranked(vault, "incident severity sev1")
    monkeypatch.setattr(
        find_module.find_candidates,
        "_lexical_visibility",
        lambda **_kwargs: find_module.find_candidates.LexicalVisibility(frozenset(), False),
    )
    find_module.clear_cache()
    assert guarded == _ranked(vault, "incident severity sev1")
    assert guarded[0] == _REVIEW
