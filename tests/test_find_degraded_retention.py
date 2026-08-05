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
