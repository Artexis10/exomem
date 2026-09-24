"""Hybrid fusion when the lexical lanes cannot see the dense lead.

The vector lane is the only lane that matches a page written in another
language than the query. When its strongest candidate shares no content word
with the query, the lexical lanes are blind to it, and their votes rank other
pages by vocabulary overlap with the query: mostly pages in the query's own
language that share a word or two with it. Reciprocal-rank fusion then let one
such partial match plus a weaker dense vote outrank the dense lead (the
language-bias twin bar of the multilingual recall design, §17.6.2).

With the dense lead lexically invisible, a lexical lane stops voting for a page
that matches the query only in part AND is written mostly in another letter
script than the dense lead (`find_policy.dominant_script`). Such a page keeps
its dense and other votes. A page holding every content word of the query, or
written in the lead's script, keeps its lexical votes, and when the dense lead
shares a content word with the query fusion is unchanged.

The script test has no threshold, so a lead in the query's own script can
never cost a same-script page its vote: English cannot regress. The price is
that a Latin-script query with an English answer (German, Estonian) gets no
protection; that twin bar stays open.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import embeddings as embeddings_module
from exomem import find as find_module
from exomem.kbdir import kb_dirname

_GOLD = "Notes/Patterns/retry-with-backoff.md"
_POISON = "Notes/Languages/de/wiederholungspruefung.md"
_OTHER = "Notes/Languages/de/kantine.md"
_RU_POISON = "Notes/Languages/ru/povtornaya-proverka.md"
_RU_FULL = "Notes/Languages/ru/zaderzhka-postavki.md"
_REVIEW = "Notes/Failures/incident-review.md"
_STUB = "Notes/Daily/incident-log.md"

_PAGES: dict[str, tuple[str, str]] = {
    _GOLD: (
        "Retry with backoff",
        "Retries wait an exponentially growing, jittered delay so that failing "
        "clients do not hammer a recovering service. Since 2026 the API client "
        "logs every retry as JSON.",
    ),
    _POISON: (
        "Wiederholungsprüfung",
        "Die Wiederholung der Prüfung findet mit dem Ausbilder im Schulungsraum statt.",
    ),
    _OTHER: (
        "Kantine",
        "Die Kantine bietet montags Müsli mit Brötchen an.",
    ),
    # Shares three index stems with the English gold (api, 2026, json), which
    # is what a page in another language typically carries: loanwords, numbers.
    _RU_POISON: (
        "Повторная проверка",
        "Повторная проверка ключей API в 2026 году: результаты сохраняются в JSON.",
    ),
    _RU_FULL: (
        "Задержка поставки",
        "Поставка пришла с задержкой из-за погоды.",
    ),
    _REVIEW: (
        "Incident review",
        "The incident review found that failing clients did not wait for a recovering service.",
    ),
    # An English stub: it shares almost no vocabulary with any other page.
    _STUB: ("Incident log", "Incident sev1 paged on-call."),
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


def _unguarded(vault: Path, monkeypatch: pytest.MonkeyPatch, query: str) -> list[str]:
    monkeypatch.setattr(
        find_module.find_candidates,
        "_lexical_visibility",
        lambda **_kwargs: find_module.find_candidates._LEXICALLY_VISIBLE,
    )
    find_module.clear_cache()
    return _ranked(vault, query)


def test_a_partial_match_in_another_script_does_not_outrank_an_invisible_dense_lead(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Russian query shares one word with the Russian poison and nothing
    # with the English gold, which the dense lane ranks first. The poison also
    # shares a few loanwords and a number with the gold: that does not matter.
    _plant_vector_lane(monkeypatch, [_GOLD, _RU_POISON, _OTHER])
    ranked = _ranked(vault, "повторные попытки с экспоненциальной задержкой")
    assert ranked[0] == _GOLD
    assert ranked.index(_RU_POISON) > ranked.index(_GOLD)


def test_a_full_lexical_match_keeps_its_votes_against_an_invisible_dense_lead(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every content word of the query is on the full-match page, which the
    # dense lane ranks below the invisible English page: its lexical votes stand.
    _plant_vector_lane(monkeypatch, [_GOLD, _OTHER, _RU_POISON, _RU_FULL])
    ranked = _ranked(vault, "поставка с задержкой")
    assert ranked[0] == _RU_FULL


def test_a_partial_match_in_the_leads_script_keeps_its_votes_even_in_another_language(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A German query with an English answer: both are Latin script, so the
    # German look-alike keeps its lexical votes exactly as without the guard.
    # This is the open part of the twin bar (German and Estonian).
    query = "Wiederholungsversuche mit exponentieller Rücksetzung"
    _plant_vector_lane(monkeypatch, [_GOLD, _POISON, _OTHER])
    guarded = _ranked(vault, query)
    assert guarded == _unguarded(vault, monkeypatch, query)


def test_fusion_is_unchanged_when_the_dense_lead_shares_a_content_word(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dense lead is the poison itself, which the query's words reach, so
    # the lexical lanes can see it and every lexical vote stands.
    _plant_vector_lane(monkeypatch, [_POISON, _GOLD, _OTHER])
    guarded = _ranked(vault, "Wiederholung mit Ausbilder")
    assert guarded == _unguarded(vault, monkeypatch, "Wiederholung mit Ausbilder")
    assert guarded[0] == _POISON


def test_a_query_of_function_words_only_is_fused_as_before(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_vector_lane(monkeypatch, [_GOLD, _RU_POISON, _OTHER])
    guarded = _ranked(vault, "what is the")
    assert guarded == _unguarded(vault, monkeypatch, "what is the")


@pytest.mark.parametrize("partial", [_REVIEW, _STUB])
def test_an_english_partial_match_under_an_english_lead_keeps_its_votes(
    vault: Path, monkeypatch: pytest.MonkeyPatch, partial: str
) -> None:
    # An English query with no relevant page: the dense lead is an English page
    # the query's words do not reach, and an English page matches one word. The
    # stub shares almost no vocabulary with the lead; it is English all the same,
    # so its lexical vote stands, exactly as without the guard.
    query = "incident severity sev1"
    _plant_vector_lane(monkeypatch, [_GOLD, _OTHER, partial])
    guarded = _ranked(vault, query)
    assert guarded == _unguarded(vault, monkeypatch, query)
    assert guarded[0] == partial
