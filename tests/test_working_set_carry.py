"""Design D3 — a turn that resolves no anchor may still be CARRIED by recall.

`lexical_evidence` ranks the anchor catalogue only, and `_page_anchor_kind`
admits no ordinary note, so a decision that lives in a research note is
unreachable by construction: the packet abstains however plainly the turn
uses that note's own words. The carry is the one retrieval-alone case. When
nothing is named and recall has ONE clearly dominant compiled page, that
page's units are served, marked as carried.

Everything about it refuses rather than ranks. A near tie carries nothing, a
`Sources/` page is not a candidate at all, an unproven catalogue carries
nothing, an exhausted budget carries nothing, and a named anchor always wins
— resolution that resolved anything never reaches the carry at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_working_set_index import _seed_planning, _seed_structure

from exomem import (
    find_types,
    lexstore,
    request_budget,
    working_set,
    working_set_index,
    working_set_runtime,
)

#: A turn that names no anchor of the fixture catalogue and repeats one
#: research note's own words. Invented, generic vocabulary throughout.
CARRY_TURN = "what did we decide about the quillon batching window"
#: A turn that lands equally on two near-twin notes. Two hits inside the
#: separation band are noise, never a guess.
TIE_TURN = "where did the tarn rollover cadence end up"
#: The dominant page `CARRY_TURN` reaches.
CARRY_PAGE = "Knowledge Base/Notes/Research/quillon-batching-window.md"
#: A `Sources/` page that outranks it and must never be a candidate.
CARRY_SOURCE = "Knowledge Base/Sources/quillon-batching-window-transcript.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_carry_pages(vault: Path) -> None:
    """One dominant research note, a `Sources/` page that outranks it, and two
    near-twins that must tie rather than carry. None of them is an anchor."""
    kb = vault / "Knowledge Base"
    _write(
        kb / "Notes" / "Research" / "quillon-batching-window.md",
        """---
type: research-note
status: active
updated: 2026-09-10
---

# Quillon batching window

## Summary

Why the quillon batching window was widened, and what the decision rests on.

- [decision] The quillon batching window was widened to nine minutes after
  the narrow window starved the batching queue twice in one week. ^q-decision
- [finding] A quillon batch that misses its window is retried whole, so a
  narrow window costs more work than it saves. ^q-finding
- [constraint] The quillon batching window never exceeds twelve minutes: past
  that the queue's own retention drops batches on the floor. ^q-constraint
""",
    )
    _write(
        kb / "Sources" / "quillon-batching-window-transcript.md",
        """---
type: source
status: active
updated: 2026-09-09
---

# Quillon batching window transcript

Raw transcript. Quillon batching window, quillon batching window, quillon
batching decided, quillon window batching, batching window quillon batching.
""",
    )
    _write(
        kb / "Notes" / "Research" / "tarn-rollover-cadence-north.md",
        """---
type: research-note
status: active
updated: 2026-09-11
---

# Tarn rollover cadence north

## Summary

- [decision] The tarn rollover cadence was moved to the northern slot.
  ^t-north
""",
    )
    _write(
        kb / "Notes" / "Research" / "tarn-rollover-cadence-south.md",
        """---
type: research-note
status: active
updated: 2026-09-11
---

# Tarn rollover cadence south

## Summary

- [decision] The tarn rollover cadence was moved to the southern slot.
  ^t-south
""",
    )


@pytest.fixture
def carry_vault(vault: Path) -> Path:
    """A warm vault whose compiled pages include non-anchor research notes."""
    _seed_structure(vault)
    _seed_planning(vault)
    _seed_carry_pages(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


@pytest.fixture
def budget_free():
    """No request budget bound, whatever a previous test left behind."""
    token = request_budget.set_current(None)
    try:
        yield
    finally:
        request_budget.reset_current(token)


def _anchor_statuses(packet: dict) -> list[str]:
    return [str(item.get("status") or "") for item in packet.get("anchors") or ()]


def _unit_paths(packet: dict) -> set[str]:
    return {
        str((unit.get("provenance") or {}).get("path") or "")
        for unit in packet.get("units") or ()
    }


# --------------------------------------------------------------------------- #
# The premise: the dominant page is not, and cannot become, an anchor
# --------------------------------------------------------------------------- #


def test_the_carried_page_is_not_in_the_anchor_catalogue(carry_vault: Path) -> None:
    """Otherwise the carry would be solving a problem that does not exist."""
    index = working_set_index.WorkingSetIndex(carry_vault)
    paths = {str(getattr(row, "path", "") or "") for row in index.anchors()}

    assert CARRY_PAGE not in paths
    assert CARRY_SOURCE not in paths


# --------------------------------------------------------------------------- #
# The scale the two thresholds were derived from
# --------------------------------------------------------------------------- #


def test_the_scale_the_carry_thresholds_were_derived_from(carry_vault: Path) -> None:
    """The measurement `RETRIEVAL_CARRY_FLOOR` was derived from, pinned.

    A threshold read off a measurement is only as good as the measurement
    staying true, so the measurement lives here rather than in a commit
    message. Measured on this vault:

    * a turn repeating one page's own distinctive words scores ~14.5, and the
      only other page that matched at all was the `Sources/` transcript,
      which is not a candidate — a carry;
    * a turn matching several pages on nothing but the generic words
      "decision" and "summary" scores ~5.6-5.7 across all of them — noise,
      refused by the floor;
    * a turn genuinely torn between two pages scores ~13.7 against ~10.2 —
      over the floor, under the separation, refused as the near miss it is.

    The floor therefore sits inside the gap between the noise ceiling and the
    real-match floor, with room on both sides, and the separation test is
    what handles everything above it.
    """
    dominant, state = working_set_runtime.carry_candidates(carry_vault, CARRY_TURN)
    assert state == "available"
    assert dominant[0][1] > working_set.RETRIEVAL_CARRY_FLOOR * 1.5, dominant

    noise, _state = working_set_runtime.carry_candidates(carry_vault, "decision summary")
    assert len(noise) >= 2, noise
    assert max(score for _path, score in noise) < working_set.RETRIEVAL_CARRY_FLOOR, noise
    assert working_set.dominant_carry(noise) is None

    near, _state = working_set_runtime.carry_candidates(
        carry_vault, "summary of the northern slot decision"
    )
    assert len(near) >= 2, near
    assert near[0][1] > working_set.RETRIEVAL_CARRY_FLOOR, near
    assert near[0][1] < working_set.RETRIEVAL_CARRY_SEPARATION * near[1][1], near
    assert working_set.dominant_carry(near) is None


# --------------------------------------------------------------------------- #
# The dominance rule, as pure logic
# --------------------------------------------------------------------------- #


def test_nothing_is_dominant_among_no_hits() -> None:
    assert working_set.dominant_carry(()) is None


def test_a_lone_hit_over_the_floor_is_dominant() -> None:
    """There is no second page for it to be confused with."""
    floor = working_set.RETRIEVAL_CARRY_FLOOR
    assert working_set.dominant_carry((("a.md", floor + 1.0),)) == ("a.md", floor + 1.0)


def test_a_lone_hit_under_the_floor_is_not_dominant() -> None:
    """The degenerate case the floor exists for: nothing matched well, and
    dividing by a runner-up that does not exist would carry the least bad
    page in a corpus that had no answer."""
    assert working_set.dominant_carry((("a.md", working_set.RETRIEVAL_CARRY_FLOOR - 0.1),)) is None


def test_a_hit_inside_the_separation_band_is_not_dominant() -> None:
    floor = working_set.RETRIEVAL_CARRY_FLOOR
    second = floor + 10.0
    top = second * working_set.RETRIEVAL_CARRY_SEPARATION - 0.1
    assert working_set.dominant_carry((("a.md", top), ("b.md", second))) is None


def test_a_hit_clear_of_the_separation_band_is_dominant() -> None:
    floor = working_set.RETRIEVAL_CARRY_FLOOR
    second = floor + 1.0
    top = second * working_set.RETRIEVAL_CARRY_SEPARATION + 0.1
    assert working_set.dominant_carry((("a.md", top), ("b.md", second))) == ("a.md", top)


# --------------------------------------------------------------------------- #
# The candidate set
# --------------------------------------------------------------------------- #


def test_a_hit_under_sources_is_never_a_candidate(carry_vault: Path) -> None:
    """Raw material is what a conclusion was drawn FROM, never durable memory.

    The transcript outranks the compiled note on this very turn, so this is
    not a rule with nothing to bite on: excluding it is what leaves the
    compiled page alone at the top.
    """
    raw = lexstore.search_bm25_result(
        carry_vault,
        "quillon batching window decide",
        working_set_runtime.RETRIEVAL_CARRY_LIMIT,
        scope="kb",
        allow_delta=False,
        min_matched_terms=working_set_runtime.RETRIEVAL_CARRY_MIN_TERMS,
    )
    ranked = [path for path, _score in raw.value or ()]
    assert ranked[:1] == [CARRY_SOURCE], ranked

    hits, state = working_set_runtime.carry_candidates(carry_vault, CARRY_TURN)
    assert state == "available"
    assert [path for path, _score in hits] == [CARRY_PAGE]


def test_a_page_merely_named_sources_is_still_a_candidate() -> None:
    """The exclusion is a directory, not a word in a filename."""
    assert working_set_runtime._under_sources("Knowledge Base/Sources/a.md") is True
    assert working_set_runtime._under_sources("Knowledge Base\\Sources\\a.md") is True
    assert working_set_runtime._under_sources("Knowledge Base/Notes/Sources of error.md") is False


# --------------------------------------------------------------------------- #
# The carried packet
# --------------------------------------------------------------------------- #


def test_a_dominant_hit_carries_that_pages_units(carry_vault: Path, budget_free) -> None:
    packet = working_set.compile_packet(carry_vault, turn=CARRY_TURN, max_chars=4000)

    assert packet["abstained"] is False, packet.get("abstention")
    assert packet["generation"]["carried_by"] == "retrieval"
    assert len(packet["anchors"]) == 1
    anchor = packet["anchors"][0]
    assert anchor["status"] == "retrieval_carried"
    assert anchor["kind"] == "page"
    assert anchor["evidence"] == ["retrieval"]
    assert anchor["path"] == CARRY_PAGE
    assert anchor["ref"] == CARRY_PAGE
    # The anchor says what the page says about itself, rather than asserting.
    assert anchor["title"] == "Quillon batching window"
    assert packet["units"], packet
    assert _unit_paths(packet) == {CARRY_PAGE}


def test_an_ordinary_packet_is_not_marked_as_carried(carry_vault: Path, budget_free) -> None:
    """`carried_by` means something only if it is absent the rest of the time."""
    packet = working_set.compile_packet(
        carry_vault,
        turn="I'm planning to tow the Cargo Sled north — what are its constraints?",
        max_chars=4000,
    )

    assert packet["abstained"] is False, packet.get("abstention")
    assert "carried_by" not in packet["generation"]
    assert "retrieval_carried" not in _anchor_statuses(packet)


def test_two_hits_inside_the_separation_band_carry_nothing(
    carry_vault: Path, budget_free
) -> None:
    """A near tie is noise. The turn abstains exactly as it did before."""
    hits, _state = working_set_runtime.carry_candidates(carry_vault, TIE_TURN)
    assert len(hits) >= 2, hits

    packet = working_set.compile_packet(carry_vault, turn=TIE_TURN, max_chars=4000)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unresolved"}
    assert packet["units"] == []
    assert "carried_by" not in packet["generation"]


def test_a_resolved_anchor_wins_over_a_dominant_hit(carry_vault: Path, budget_free) -> None:
    """A named anchor always wins: the carry never runs when one resolved."""
    packet = working_set.compile_packet(
        carry_vault,
        turn=(
            "I'm planning to tow the Cargo Sled north — what are its constraints, "
            "and what did we decide about the quillon batching window?"
        ),
        max_chars=4000,
    )

    assert packet["abstained"] is False, packet.get("abstention")
    assert "carried_by" not in packet["generation"]
    assert "resolved" in _anchor_statuses(packet)
    assert "retrieval_carried" not in _anchor_statuses(packet)
    assert CARRY_PAGE not in _unit_paths(packet)


def test_a_carried_packet_mints_no_continuity_token(carry_vault: Path, budget_free) -> None:
    """Lane U2 owns referents. A carried page is not a resolution to carry
    forward, and a later turn's `continuity` must not be able to promote one."""
    packet = working_set.compile_packet(carry_vault, turn=CARRY_TURN, max_chars=4000)
    assert packet["generation"]["carried_by"] == "retrieval"

    assert working_set_runtime.mint_continuity(packet, identity="vault-identity") == ""


def test_the_carry_reports_its_own_timing_span(carry_vault: Path, budget_free) -> None:
    timings = find_types.FindTimings()
    working_set.compile_packet(
        carry_vault, turn=CARRY_TURN, max_chars=4000, timings=timings
    )

    assert "working_set.carry" in timings.as_dict()["stages"]


# --------------------------------------------------------------------------- #
# What refuses to carry
# --------------------------------------------------------------------------- #


def test_a_catalogue_that_is_not_available_carries_nothing(
    carry_vault: Path, budget_free, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A carried packet rests entirely on recall. Recall that cannot prove
    itself current is no ground to serve one from."""

    def _stale(*args, **kwargs):
        return lexstore.CatalogQueryResult(
            None, lexstore.CatalogReadiness("stale", False, "sqlite")
        )

    monkeypatch.setattr(lexstore, "search_bm25_result", _stale)

    hits, state = working_set_runtime.carry_candidates(carry_vault, CARRY_TURN)
    assert hits == ()
    assert state == "stale"

    packet = working_set.compile_packet(carry_vault, turn=CARRY_TURN, max_chars=4000)
    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unresolved"}
    assert "carried_by" not in packet["generation"]


def test_an_exhausted_budget_carries_nothing(carry_vault: Path) -> None:
    """The carry is the LAST thing a request that ran out of time should do."""
    budget = request_budget.RequestBudget(seconds=0.0)
    token = request_budget.set_current(budget)
    try:
        assert working_set._carry_by_retrieval(carry_vault, turn=CARRY_TURN) is None
    finally:
        request_budget.reset_current(token)

    assert "working_set.carry" in budget.as_response_block()["skipped"]


def test_an_agent_choice_that_named_nothing_still_abstains_unresolved(
    carry_vault: Path, budget_free
) -> None:
    """`op_activate_context` turns exactly this abstention into the one refusal
    an unknown and a withheld ref share. A carry here would break that."""
    packet = working_set.compile_packet(
        carry_vault,
        turn=CARRY_TURN,
        max_chars=4000,
        anchor="Knowledge Base/Notes/Research/no-such-page.md",
    )

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unresolved"}
    assert "carried_by" not in packet["generation"]
