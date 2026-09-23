"""The reranker runs only where its declared coverage holds.

`BAAI/bge-reranker-base` was trained on English and Chinese. Measured over the
multilingual recall fixture with bge-m3 recall (2026-09-23), it moved a Russian
morphology gold from rank 1 to 3 and a Russian twin gold out of the top ten,
and on German and Estonian queries whose answer is an English page it put the
same-language look-alike first again (twins 1 -> 2 and 3, Estonian
cross-language 2 -> 6). English improved (golden NDCG@10 0.931 -> 0.962).

Coverage is declared per reranker, as data: the scripts it reads, and whether it
can judge a query against a passage in another language. No language is
detected. A query is outside the scripts when most of its letters are in
scripts the reranker does not declare. A request crosses languages when the
dense lead shares no content word with the query and fusion found the lexical
lanes voting in another vocabulary (or matching no content word at all). Either
keeps the fused order, explicit rerank included.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, embeddings, find_policy, readiness, writer_lease
from exomem import find as find_module
from exomem.kbdir import kb_dirname


@pytest.fixture(autouse=True)
def _reset_find_state() -> None:
    find_module.clear_cache()
    readiness.reset()
    writer_lease.reset_managers_for_tests()
    yield
    find_module.clear_cache()
    readiness.reset()
    writer_lease.reset_managers_for_tests()


# ------------------------------------------------------------------ declared data


def test_the_default_reranker_reads_latin_and_han_and_not_across_languages() -> None:
    coverage = find_policy.reranker_coverage("BAAI/bge-reranker-base")
    assert coverage.scripts == frozenset({"latin", "han"})
    assert coverage.cross_lingual is False


def test_the_multilingual_opt_in_reranker_reads_every_script_across_languages() -> None:
    coverage = find_policy.reranker_coverage("BAAI/bge-reranker-v2-m3")
    assert coverage.scripts is None
    assert coverage.cross_lingual is True


def test_an_undeclared_reranker_is_not_gated() -> None:
    coverage = find_policy.reranker_coverage("example/unknown-reranker")
    assert coverage.scripts is None
    assert coverage.cross_lingual is True


@pytest.mark.parametrize(
    ("query", "script"),
    [
        ("circuit breaker retries", "latin"),
        ("Wiederholungsprüfung für Auszubildende", "latin"),
        ("Ремонт крыши гаража", "cyrillic"),
        ("倉庫 在庫 確認", "han"),
        ("サーバーのバックアップ", "kana"),
        ("창고 재고", "hangul"),
        ("λ calculus notes", "latin"),
        ("2026-09-23 !!", None),
    ],
)
def test_a_querys_dominant_script_is_read_from_its_letters(query: str, script: str | None) -> None:
    assert find_policy.dominant_script(query) == script


@pytest.mark.parametrize(
    ("query", "covered"),
    [
        ("circuit breaker retries", True),
        ("倉庫 在庫 確認", True),
        ("Ремонт крыши гаража", False),
        ("サーバーのバックアップ", False),
        ("2026-09-23", True),
    ],
)
def test_script_coverage_of_the_default_reranker(query: str, covered: bool) -> None:
    coverage = find_policy.reranker_coverage("BAAI/bge-reranker-base")
    assert find_policy.reranker_reads_query(coverage, query) is covered


# ------------------------------------------------------------------ find seam

_GOLD = "Notes/Patterns/retry-with-backoff.md"
_POISON = "Notes/Languages/de/wiederholungspruefung.md"
_OTHER = "Notes/Languages/de/kantine.md"
_RUSSIAN = "Notes/Languages/ru/krysha.md"

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
    _OTHER: ("Kantine", "Die Kantine bietet montags Müsli mit Brötchen an."),
    _RUSSIAN: ("Ремонт крыши гаража", "Крыша гаража протекала после дождей."),
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
    monkeypatch.setattr(embeddings, "ranking_enabled", lambda: True)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)
    embeddings.clear_embedding_indexes()
    return root


def _plant(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> list[int]:
    ranked = [f"{kb_dirname()}/{rel}" for rel in order]

    class FakeIndex:
        def search(self, _query_vector, *, k: int, allowed_paths=None):
            return [
                (path, 0, "chunk", 0.9 - 0.05 * position)
                for position, path in enumerate(ranked)
                if allowed_paths is None or path in allowed_paths
            ][:k]

    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: FakeIndex())
    monkeypatch.setattr(embeddings, "embed_texts", lambda _texts, *, is_query: [[0.1, 0.2]])
    calls: list[int] = []

    def score(_query: str, passages: list[str]) -> list[float]:
        calls.append(len(passages))
        return [float(index) for index in range(len(passages))]

    monkeypatch.setattr(embeddings, "rerank_pairs", score)
    return calls


def _rerank_profile(vault: Path, query: str) -> dict:
    result = commands.op_ask_memory(
        vault,
        query=query,
        limit=5,
        mode="hybrid",
        scope="kb-only",
        graph=False,
        rerank=True,
        detail="compact",
        explain=True,
    )
    return result["retrieval_profile"]["rerank"]


def test_a_query_outside_the_rerankers_scripts_keeps_the_fused_order(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _plant(monkeypatch, [_RUSSIAN, _GOLD])
    profile = _rerank_profile(vault, "Ремонт крыши гаража")
    assert (profile["decision"], profile["reason"]) == ("skipped", "query_script_not_covered")
    assert calls == []


def test_a_cross_language_request_keeps_the_fused_order(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _plant(monkeypatch, [_GOLD, _POISON, _OTHER])
    profile = _rerank_profile(vault, "Wiederholungsversuche mit exponentieller Rücksetzung")
    assert (profile["decision"], profile["reason"]) == ("skipped", "cross_language_not_covered")
    assert calls == []


def test_a_cross_lingual_reranker_still_runs_on_a_cross_language_request(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _plant(monkeypatch, [_GOLD, _POISON, _OTHER])
    monkeypatch.setattr(embeddings, "RERANKER_NAME", "BAAI/bge-reranker-v2-m3")
    profile = _rerank_profile(vault, "Wiederholungsversuche mit exponentieller Rücksetzung")
    assert profile["decision"] == "ran"
    assert calls


def test_a_same_language_request_in_a_covered_script_is_reranked(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _plant(monkeypatch, [_POISON, _GOLD, _OTHER])
    profile = _rerank_profile(vault, "Wiederholung mit Ausbilder")
    assert profile["decision"] == "ran"
    assert calls
