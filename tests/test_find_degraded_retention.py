"""Degraded-corroboration retention at the hybrid page seam (find.py).

Measured defect: with the vector/CLIP lanes producing no candidates
(EXOMEM_DISABLE_EMBEDDINGS, absent/empty embedding index, warming or failed
lane), the in-KB retention seam vetoed every BM25-only candidate unless the
page contained ALL query stems — and interrogative phrasing ("How many…")
contributes stems that never appear in stored text, so the product returned
zero hits while its own BM25 lane ranked the correct page first.

These tests pin the fix: when the semantic lanes are structurally absent for
the query, retention relaxes from all-stems to a STRICT MAJORITY of the
query's whitespace words (whole-word presence: every BM25 subtoken stem of
the word must appear, so compounds like `reference-marker-xyz` need all
parts and trailing punctuation cannot mask a match) — and at least one
present word must be a CONTENT word, so function-word overlap ("what is the
… of the …") cannot carry the majority against paragraph-length prose.
Exactly half is still vetoed; a live vector lane keeps the strict all-stems
veto; keyword mode's conjunctive contract is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import embeddings as embeddings_module
from exomem import find as find_module
from exomem.kbdir import kb_dirname

QUERY = "How many points did the drift index measure?"

# Deviation from the drafted brief's example ("Which vendor performed the
# security audit?"): that phrasing shares the stem "the" with the drift page's
# mandated sentence, so it does not satisfy this test's own definition — "a
# query sharing NO stems with any page". "our" replaces "the" so the
# zero-overlap property actually holds against every page below: no lane can
# produce a candidate at all, the strictest irrelevance case.
IRRELEVANT_QUERY = "Which vendor performed our security audit?"

DRIFT_PAGE = "Sources/2026-07-02-field-check.md"
OTHER_PAGE = "Sources/2026-06-30-kernel-upgrade.md"

# The one-liner distractors deliberately share no stem with either query
# above, so the irrelevance case has zero lexical overlap with the whole
# vault. The paragraph page is the opposite trap: realistic prose dense in
# function words ("what/we/did/about/the/is/of/all/that/in/do") that shares
# NO content word with the veto queries below — the page a function-word
# majority would wrongly retain.
_PAGES: dict[str, tuple[str, str]] = {
    DRIFT_PAGE: (
        "Field check",
        "Field check: the drift index for Project X measured 41.3 points.",
    ),
    OTHER_PAGE: (
        "Kernel upgrade log",
        "Rebooted after patching. Zero regressions observed in canary jobs.",
    ),
    "Sources/2026-06-29-budget-summary.md": (
        "Quarterly budget summary",
        "Travel, hardware, and cloud spend stayed within plan this quarter.",
    ),
    "Sources/2026-06-28-sourdough.md": (
        "Sourdough starter care",
        "Feeding schedule: equal parts flour and water, twice daily at room temperature.",
    ),
    "Sources/2026-06-27-greenhouse-controller.md": (
        "Greenhouse irrigation controller notes",
        "What we did about the greenhouse irrigation controller is worth"
        " recording. The controller manages six drip lines, and we calibrated"
        " all of them in early spring. Fertilizer dosing runs on a timed"
        " cycle; the pump primes itself, and a float valve keeps the"
        " reservoir topped up. When afternoon readings climbed, we suspected"
        " the enclosure was overheating, so we added a shade panel and a"
        " small vent fan. That change kept the electronics cool through"
        " summer. Do check the filter screens monthly, because algae buildup"
        " slows the flow and stresses the pump.",
    ),
}

# Unanswerable questions that clear the >1/2 word majority against the
# paragraph page purely on function words; their content words (security,
# audit, state, art, end) appear nowhere in the vault.
FUNCTION_WORD_QUERIES = (
    "What did we do about the security audit?",
    "what is the state of the art",
    "did we do all of that in the end",
)


@pytest.fixture
def degraded_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Minimal isolated vault: 4 markdown sources, no embedding index.

    The suite-wide autouse fixture already sets EXOMEM_DISABLE_EMBEDDINGS=1
    and EXOMEM_DISABLE_CLIP=1, so hybrid find() runs exactly the degraded
    (BM25 + keyword only) profile under test.
    """
    root = tmp_path / "vault"
    for rel, (title, body) in _PAGES.items():
        page_path = root / kb_dirname() / rel
        page_path.parent.mkdir(parents=True, exist_ok=True)
        updated = page_path.name[:10]
        page_path.write_text(
            f"---\ntype: source\nupdated: {updated}\n---\n\n# {title}\n\n{body}\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    find_module.clear_cache()
    embeddings_module.clear_embedding_indexes()
    return root


def test_degraded_hybrid_retains_bm25_top_hit(degraded_vault: Path) -> None:
    """An answerable NL question must retain the BM25 top-ranked page.

    "How many"/"did" contribute stems absent from the stored sentence, so the
    all-stems veto used to drop the only correct candidate and return [].
    """
    hits = find_module.find(degraded_vault, query=QUERY)
    assert hits, "degraded hybrid returned zero page hits for an answerable query"
    top = hits[0]
    assert top.path.endswith("field-check.md")
    # Honest lane provenance: this is a BM25 retention, not a vector match.
    assert top.bm25_rank == 1
    assert top.vector_rank is None
    # Stem-anchored "why": the excerpt shows the matched evidence.
    assert "drift index" in top.excerpt
    assert "41.3" in top.excerpt


def test_keyword_mode_conjunctive_contract_unchanged(degraded_vault: Path) -> None:
    """mode="keyword" keeps its documented conjunctive precision contract."""
    assert find_module.find(degraded_vault, query=QUERY, mode="keyword") == []


def test_no_shared_stem_query_still_returns_nothing(degraded_vault: Path) -> None:
    """A query sharing no stem with any page stays empty in degraded hybrid."""
    assert find_module.find(degraded_vault, query=IRRELEVANT_QUERY) == []


def test_strict_veto_when_vector_lane_is_active(
    degraded_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relaxed retention is conditioned on LANE availability, not page membership.

    Simulate a live vector lane that ranked a different page: the candidate
    bundle then carries a non-empty vector ranking, so the strict all-stems
    veto must still drop the BM25-only drift page (embeddings-active behavior
    is unchanged by the fix).
    """
    real = find_module.find_candidates.collect_candidates
    other_rel = f"{kb_dirname()}/{OTHER_PAGE}"

    def _with_active_vector_lane(vault_root, **kwargs):
        bundle = real(vault_root, **kwargs)
        bundle.vector_ranking = [other_rel]
        return bundle

    monkeypatch.setattr(
        find_module.find_candidates, "collect_candidates", _with_active_vector_lane
    )
    hits = find_module.find(degraded_vault, query=QUERY)
    assert all(not h.path.endswith("field-check.md") for h in hits)


def test_exactly_half_coverage_is_vetoed(degraded_vault: Path) -> None:
    """Two-word query, one word present: 1/2 is NOT a strict majority.

    Mirrors the shape of the suite's conjunctive-precision pin (a real word
    plus a garbage token) inside this vault: the sourdough page is the BM25
    candidate, but 1 of 2 query words present must still be vetoed.
    """
    assert find_module.find(degraded_vault, query="sourdough zzzzunknownzzzz") == []


def test_hyphenated_word_with_partial_subtokens_is_vetoed(degraded_vault: Path) -> None:
    """A compound word is present only when ALL its subtokens appear.

    "drift-index-zzzz" is a single whitespace word; the drift page has
    drift+index but not zzzz, so the word is absent -> 0/1 coverage -> vetoed
    (exact-marker-style queries stay precise in the degraded profile).
    """
    assert find_module.find(degraded_vault, query="drift-index-zzzz") == []


def test_fully_present_compound_and_punctuated_words_count(degraded_vault: Path) -> None:
    """Whole-word presence uses subtoken stems, not the raw whitespace token.

    "drift-index" (both subtokens present) and "measurements?" (punctuation
    stripped; stem matches "measured") are each present -> 2/2 majority ->
    retained. Pins the tokenization so presence semantics cannot drift to
    whole-string stemming (which would score 0/2 and veto).
    """
    hits = find_module.find(degraded_vault, query="drift-index measurements?")
    assert hits
    assert hits[0].path.endswith("field-check.md")


def test_function_word_majority_is_vetoed(degraded_vault: Path) -> None:
    """A function-word majority alone must not retain — a content anchor is required.

    Against paragraph-length prose every one of these questions clears the
    strict word majority purely on function words (what/did/we/do/about/the/
    is/of/all/that/in), while the words that carry their meaning appear
    nowhere in the vault. Retention must still be vetoed for every page.
    """
    for query in FUNCTION_WORD_QUERIES:
        assert find_module.find(degraded_vault, query=query) == [], query


def test_content_word_majority_retains_against_paragraph(degraded_vault: Path) -> None:
    """A genuinely relevant question keeps working against paragraph prose.

    Majority coverage where the present words include real content words
    (greenhouse, irrigation, controller, overheat) retains the page at rank 1.
    """
    hits = find_module.find(
        degraded_vault, query="did the greenhouse irrigation controller overheat?"
    )
    assert hits
    assert hits[0].path.endswith("greenhouse-controller.md")


# ---------------------------------------------------------------- tokenizer v2


_MULTILINGUAL_PAGES: dict[str, tuple[str, str]] = {
    "Notes/tower-height.md": (
        "展望台の記録",
        "東京タワーの高さは三百三十三メートルです。展望台は二つあります。",
    ),
    "Notes/meeting-minutes.md": (
        "議事録",
        "会議の議事録を共有しました。次回は来週の水曜日です。",
    ),
    "Notes/delivery-check.md": (
        "Lieferung",
        "Zölvarn prüft die Lieferung am Montag.",
    ),
    "Notes/budget.md": (
        "Budget",
        "Travel and hardware spend stayed within plan.",
    ),
}


@pytest.fixture
def multilingual_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    for rel, (title, body) in _MULTILINGUAL_PAGES.items():
        page_path = root / kb_dirname() / rel
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(
            f"---\ntype: note\ntitle: {title}\nupdated: 2026-09-01\n---\n\n# {title}\n\n{body}\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    find_module.clear_cache()
    embeddings_module.clear_embedding_indexes()
    return root


def test_japanese_question_retains_its_page_by_a_bigram_majority(multilingual_vault: Path) -> None:
    """An unspaced run is one query word, present when most of its bigrams are:
    the particles and question words of a Japanese question need not appear."""
    hits = find_module.find(multilingual_vault, query="東京タワーの高さは何メートル")
    assert hits
    assert hits[0].path.endswith("tower-height.md")
    assert "東京タワー" in hits[0].excerpt


def test_japanese_query_sharing_one_bigram_is_vetoed(multilingual_vault: Path) -> None:
    """東京 alone of 東京の天気予報 is one bigram of six: BM25 nominates the
    tower page, and the majority rule drops it."""
    assert find_module.find(multilingual_vault, query="東京の天気予報") == []


def test_accent_free_query_retains_the_accented_page(multilingual_vault: Path) -> None:
    hits = find_module.find(multilingual_vault, query="zolvarn lieferung")
    assert hits
    assert hits[0].path.endswith("delivery-check.md")
    assert "Zölvarn" in hits[0].excerpt


def test_cjk_page_passes_the_strict_veto_when_the_vector_lane_is_live(
    multilingual_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = find_module.find_candidates.collect_candidates
    other_rel = f"{kb_dirname()}/Notes/budget.md"

    def _with_active_vector_lane(vault_root, **kwargs):
        bundle = real(vault_root, **kwargs)
        bundle.vector_ranking = [other_rel]
        return bundle

    monkeypatch.setattr(
        find_module.find_candidates, "collect_candidates", _with_active_vector_lane
    )
    hits = find_module.find(multilingual_vault, query="議事録 共有")
    assert any(hit.path.endswith("meeting-minutes.md") for hit in hits)
    vetoed = find_module.find(multilingual_vault, query="議事録 天気予報")
    assert not any(hit.path.endswith("meeting-minutes.md") for hit in vetoed)


def test_query_word_groups_count_an_unspaced_run_as_one_word() -> None:
    from exomem import find_policy

    groups = find_policy.query_word_stem_groups(
        "the drift-index 東京タワー Python入門 Zölvarn 抹茶の用意 についてですか"
    )
    assert [(sorted(stems), function, required) for stems, function, required in groups] == [
        (["the"], True, 1),
        (["drift", "index"], False, 2),
        (sorted(["東京", "京タ", "タワ", "ワー"]), False, 3),
        (["python"], False, 1),
        (["入門"], False, 1),
        (["zölvarn"], False, 1),
        # A run's content is its bigrams without hiragana...
        (sorted(["抹茶", "用意"]), False, 2),
        # ...unless every bigram holds hiragana.
        (sorted(["につ", "つい", "いて", "てで", "です", "すか"]), False, 4),
    ]
    groups = groups[:6]
    present, total, content = find_policy.stem_word_coverage(
        frozenset({"the", "東京", "京タ", "タワ", "python", "zolvarn"}), groups
    )
    assert (present, total, content) == (3, 6, 2)


def test_stem_gates_read_cjk_and_folded_query_words(multilingual_vault: Path) -> None:
    from exomem import find_results

    tower = find_module._CACHE.get(
        multilingual_vault / kb_dirname() / "Notes/tower-height.md", multilingual_vault
    )
    delivery = find_module._CACHE.get(
        multilingual_vault / kb_dirname() / "Notes/delivery-check.md", multilingual_vault
    )
    assert find_results.stem_tokens_present(tower, "東京タワーの高さ")
    assert not find_results.stem_tokens_present(tower, "東京の天気予報")
    assert find_results.stem_tokens_present(delivery, "zolvarn lieferung")
    assert find_module._any_stem_present(tower, "タワー")
    assert find_module._any_stem_present(delivery, "zolvarn")
    minutes = find_module._CACHE.get(
        multilingual_vault / kb_dirname() / "Notes/meeting-minutes.md", multilingual_vault
    )
    # 議事, 事録 and 共有 are all this query's content; its particles are not.
    assert find_results.stem_tokens_present(minutes, "議事録はいつ共有")

    from types import SimpleNamespace

    long_body = "静かな記録。" * 60 + "東京タワーの高さは三百メートル。" + "別の話題。" * 60
    excerpt = find_results.stem_anchored_excerpt(SimpleNamespace(body=long_body), "高さ")
    assert "東京タワーの高さ" in excerpt
    folded_body = "x " * 200 + "Zölvarn prüft die Lieferung." + " y" * 200
    excerpt = find_results.stem_anchored_excerpt(SimpleNamespace(body=folded_body), "zolvarn")
    assert "Zölvarn prüft" in excerpt


def test_a_japanese_x_no_y_query_retains_its_page(multilingual_vault: Path) -> None:
    """議事録はいつ共有 shares every content bigram with the minutes page and
    few particle bigrams: a majority of ALL bigrams would drop it."""
    hits = find_module.find(multilingual_vault, query="議事録はいつ共有")
    assert hits and hits[0].path.endswith("meeting-minutes.md")


def test_a_japanese_particle_only_query_finds_nothing(multilingual_vault: Path) -> None:
    for query in ("についてですか", "はいつですか", "それはなんですか"):
        assert find_module.find(multilingual_vault, query=query) == [], query


def test_typographic_english_query_words_pass_the_stem_gate() -> None:
    """Intended change: under v1 a query word with a curly apostrophe or an em
    dash was stemmed whole and never matched; v2 splits it at the punctuation
    exactly as an ASCII apostrophe or hyphen always split."""
    from types import SimpleNamespace

    from exomem import bm25, find_results

    page = SimpleNamespace(
        stem_set=frozenset(bm25.tokenize("What's on the harbor crane schedule? Don't wait."))
    )
    for word in ("what\u2019s", "don\u2019t", "harbor\u2014crane"):
        assert find_results.stem_tokens_present(page, word), word
    assert not find_results.stem_tokens_present(page, "harbor\u2014dredger")
