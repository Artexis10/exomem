"""The bounded post-RRF pass must be an optimisation, never a ranking change.

`apply_post_rrf_multipliers` loads every fused candidate's page to score it, and
`page_of` on a cold page is a file read plus a policy admission check. On a
2.4k-page vault that measured ~500 pages loaded to return 10 results — the
largest single read-path term in #283.

The fix skips candidates that provably cannot reach the top `top_n`, using the
config-declared ceiling on every multiplier. "Provably" is the whole claim, so
these tests compare the bounded pass against the unbounded one over randomised
inputs rather than a hand-picked case, and check the exact tuples — score and
tie-break order — not just the path set.
"""

from __future__ import annotations

import random
from dataclasses import replace

from exomem import find_policy
from exomem.ranking_config import DEFAULT_RANKING

COMPILED = "insight"  # gets compiled_boost
SOURCE = "source"  # gets source_penalty


class _Page:
    """Minimal stand-in for ParsedPage: only the multiplier inputs matter."""

    def __init__(self, page_type: str | None, status: str | None, updated: str | None):
        self.page_type = page_type
        self.status = status
        self.updated = updated
        self.media_type = None


def _corpus(rng: random.Random, size: int) -> tuple[list[tuple[str, float]], dict[str, _Page]]:
    """A descending-score fused list plus pages spanning every multiplier branch."""
    pages: dict[str, _Page] = {}
    for i in range(size):
        path = f"Knowledge Base/Notes/page-{i:04d}.md"
        pages[path] = _Page(
            rng.choice([COMPILED, SOURCE, "note", None]),
            rng.choice(["active", "superseded", None]),
            rng.choice(["2026-08-20", "2020-01-01", None]),
        )
    scores = sorted((rng.random() * 10.0 for _ in range(size)), reverse=True)
    fused = list(zip(sorted(pages), scores, strict=True))
    fused.sort(key=lambda t: -t[1])
    return fused, pages


def _both_passes(
    fused,
    pages,
    *,
    top_n,
    query="topic",
    usage_map=None,
    config=None,
    prefer_compiled=True,
    prefer_active=True,
):
    cfg = config or DEFAULT_RANKING
    loaded: list[str] = []

    def counting_page_of(path: str):
        loaded.append(path)
        return pages.get(path)

    kwargs = dict(
        prefer_compiled=prefer_compiled,
        prefer_active=prefer_active,
        temporal=True,
        usage_map=usage_map,
    )
    full = find_policy.apply_post_rrf_multipliers(
        list(fused), query, cfg, page_of=pages.get, **kwargs
    )
    bounded = find_policy.apply_post_rrf_multipliers(
        list(fused), query, cfg, page_of=counting_page_of, top_n=top_n, **kwargs
    )
    return full, bounded, loaded


def test_the_bounded_prefix_is_identical_over_many_random_corpora() -> None:
    rng = random.Random(20260821)
    for trial in range(40):
        size = rng.choice([60, 200, 600])
        top_n = rng.choice([10, 30, 50])
        fused, pages = _corpus(rng, size)

        full, bounded, _ = _both_passes(fused, pages, top_n=top_n)

        # Exact tuples, not just paths: a wrong tie-break or a dropped
        # multiplier would keep the path set and still be a ranking change.
        assert bounded[:top_n] == full[:top_n], f"trial {trial} size={size} top_n={top_n}"


def test_the_bounded_pass_returns_every_candidate() -> None:
    """The tail is deprioritised, never dropped — callers may still walk past top_n."""
    rng = random.Random(7)
    fused, pages = _corpus(rng, 300)

    full, bounded, _ = _both_passes(fused, pages, top_n=25)

    assert len(bounded) == len(fused)
    assert {p for p, _ in bounded} == {p for p, _ in full}


def test_the_bounded_pass_actually_stops_early() -> None:
    """A guard that never fires is not an optimisation."""
    rng = random.Random(11)
    fused, pages = _corpus(rng, 600)

    _full, _bounded, loaded = _both_passes(fused, pages, top_n=50)

    assert len(loaded) < 200, f"loaded {len(loaded)} of {len(fused)}"


def test_a_superseded_tail_candidate_cannot_be_promoted_past_the_cutoff() -> None:
    """The bound must hold in the direction that could hide a real winner.

    Multipliers here are penalties as well as boosts, so the cutoff has to be
    driven by the maximum reachable score. Give the tail the most favourable
    page in the corpus and prove it still lands where a full pass puts it.
    """
    pages = {f"Knowledge Base/Notes/page-{i:03d}.md": _Page("note", None, None) for i in range(200)}
    boosted = "Knowledge Base/Notes/page-199.md"
    pages[boosted] = _Page(COMPILED, "active", "2026-08-20")
    fused = [(f"Knowledge Base/Notes/page-{i:03d}.md", 100.0 - i) for i in range(200)]

    full, bounded, _ = _both_passes(fused, pages, top_n=20)

    assert bounded[:20] == full[:20]


def test_a_tie_at_the_cutoff_is_scored_rather_than_skipped() -> None:
    """The cutoff comparison must be strict `<`, not `<=`.

    Random float corpora never tie, so they cannot catch this: with `<=`, a
    candidate whose best reachable score EQUALS the current cutoff is skipped —
    but an equal score is decided by the `(-score, path)` tie-break, which it
    may well win. Here every page is neutral, so adjusted == raw and every
    candidate ties; the full pass then orders purely by path. Input order is
    deliberately the reverse of path order, so skipping on equality returns the
    wrong set entirely.

    `prefer_compiled` is off deliberately. With it on, the ceiling is
    `compiled_boost` (1.15) and `reachable` is strictly ABOVE a tied cutoff, so
    the comparison never reaches equality and the test proves nothing. Status is
    penalty-only, so status-alone puts the ceiling at exactly 1.0 — which is
    what makes `reachable == cutoff` reachable at all.
    """
    paths = [f"Knowledge Base/Notes/page-{i:03d}.md" for i in range(60)]
    pages = {p: _Page("note", None, None) for p in paths}  # every multiplier 1.0
    fused = [(p, 5.0) for p in reversed(paths)]  # tied scores, reversed order

    full, bounded, _ = _both_passes(
        fused, pages, top_n=20, prefer_compiled=False, prefer_active=True
    )

    assert bounded[:20] == full[:20]
    # And the full pass really is path-ordered, so the assertion above has teeth.
    assert [p for p, _ in full[:3]] == paths[:3]


def test_a_negative_raw_score_bounds_upward_not_downward() -> None:
    """A negative score inverts the multiplier bound, so the max must take both.

    A negative lane weight yields negative fused scores. There, multiplying by
    the HIGH multiplier gives the *smallest* value, not the largest — so a
    cutoff computed as `score * high` understates what the candidate can reach
    and would skip a genuine winner.
    """
    paths = [f"Knowledge Base/Notes/page-{i:03d}.md" for i in range(40)]
    # Neutral pages keep their raw negative score. The late candidate is heavily
    # penalised, and a penalty on a negative score moves it TOWARD zero — so it
    # actually outranks everything above it. `score * high` would rate its
    # ceiling far below the cutoff and skip the genuine winner.
    pages = {p: _Page("note", None, None) for p in paths}
    winner = paths[30]
    pages[winner] = _Page(SOURCE, "superseded", None)
    fused = [(p, -1.0 - i * 0.01) for i, p in enumerate(paths)]

    full, bounded, _ = _both_passes(fused, pages, top_n=10)

    assert full[0][0] == winner, "setup: the penalised late candidate must rank first"
    assert bounded[:10] == full[:10]


def test_the_ceiling_is_the_product_of_active_factors_only() -> None:
    low, high = find_policy.multiplier_bounds(
        DEFAULT_RANKING,
        prefer_compiled=True,
        prefer_active=True,
        temporal_active=False,
        usage_active=False,
    )
    assert high == DEFAULT_RANKING.compiled_boost  # status is penalty-only
    assert low == DEFAULT_RANKING.source_penalty * DEFAULT_RANKING.superseded_penalty

    none_active = find_policy.multiplier_bounds(
        DEFAULT_RANKING,
        prefer_compiled=False,
        prefer_active=False,
        temporal_active=False,
        usage_active=False,
    )
    assert none_active == (1.0, 1.0)


def test_a_wider_ceiling_still_produces_the_same_prefix() -> None:
    """An aggressive config widens the bound; it must not break exactness."""
    rng = random.Random(5)
    fused, pages = _corpus(rng, 400)
    wide = replace(DEFAULT_RANKING, compiled_boost=3.0, source_penalty=0.2, superseded_penalty=0.1)

    full, bounded, _ = _both_passes(fused, pages, top_n=30, config=wide)

    assert bounded[:30] == full[:30]


def test_an_explain_request_takes_the_full_pass() -> None:
    """Trace evidence must cover every candidate, so bounding is suppressed."""
    rng = random.Random(3)
    fused, pages = _corpus(rng, 150)
    evidence: dict[str, list[dict[str, float | str]]] = {}
    loaded: list[str] = []

    def counting_page_of(path: str):
        loaded.append(path)
        return pages.get(path)

    find_policy.apply_post_rrf_multipliers(
        list(fused),
        "topic",
        DEFAULT_RANKING,
        prefer_compiled=True,
        prefer_active=True,
        temporal=True,
        page_of=counting_page_of,
        evidence_out=evidence,
        top_n=10,
    )

    assert len(loaded) == len(fused)
    assert len(evidence) == len(fused)


def test_top_n_none_is_byte_identical_to_the_old_behaviour() -> None:
    rng = random.Random(99)
    fused, pages = _corpus(rng, 250)

    full, _bounded, _ = _both_passes(fused, pages, top_n=None)
    again = find_policy.apply_post_rrf_multipliers(
        list(fused),
        "topic",
        DEFAULT_RANKING,
        prefer_compiled=True,
        prefer_active=True,
        temporal=True,
        page_of=pages.get,
    )

    assert full == again
