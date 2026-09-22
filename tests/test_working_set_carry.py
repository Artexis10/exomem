"""Design D3 — a turn that resolves no anchor may still be CARRIED by recall.

`lexical_evidence` ranks the anchor catalogue only, and `_page_anchor_kind`
admits no ordinary note, so a decision that lives in a research note is
unreachable by construction: the packet abstains however plainly the turn
uses that note's own words. The carry is the one retrieval-alone case. When
nothing is named and recall has ONE clearly dominant compiled page, that
page's units are served, marked as carried.

Everything about it refuses rather than ranks. A near tie carries nothing, a
raw-material page is not a candidate at all, an unproven catalogue carries
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
CARRY_TURN = "what did we decide about the quillon vantry window"
#: A turn that lands equally on two near-twin notes. Two hits inside the
#: separation band are noise, never a guess.
TIE_TURN = "where did the tarn rollover cadence end up"
#: The dominant page `CARRY_TURN` reaches.
CARRY_PAGE = "Knowledge Base/Notes/Research/quillon-vantry-window.md"
#: Raw-material pages that outrank it and must never be candidates.
CARRY_SOURCE = "Knowledge Base/Sources/quillon-vantry-window-transcript.md"
CARRY_EVIDENCE = "Knowledge Base/Evidence/quillon-vantry-window-receipt.md"

#: The reviewer's stub case. Ordinary English throughout; the only page it
#: touches is a two-line note titled "Meeting notes".
STUB_TURN = (
    "I have been going back and forth about this all week and I still do not "
    "know what the right call is, so before the meeting tomorrow I would like "
    "to settle the pending decision one way or the other"
)
#: The reviewer's genuine case: a turn naming a page by its own distinctive
#: words, which a vault of any size leaves distinctive.
GENUINE_TURN = (
    "can you remind me what the review concluded about the kelvane "
    "throughput ceiling and whether we ever raised it"
)
GENUINE_PAGE = "Knowledge Base/Notes/Research/kelvane-throughput-review.md"
#: A page whose distinctive words are ORDINARY English that the stemmer does
#: not settle on in one pass: "collapse" stems to "collaps", and stemming
#: that again gives "collap", which no page in the catalogue contains.
DOUBLE_STEM_TURN = "what did we decide about the collapse browse window"
DOUBLE_STEM_PAGE = "Knowledge Base/Notes/Research/collapse-browse-window.md"
STUB_PAGE = "Knowledge Base/Notes/Inbox/meeting-notes.md"
#: One ordinary note's worth of prose. Every word the stub turn shares with
#: `STUB_PAGE` appears here, which is the point: in a vault that contains
#: ordinary notes, "meeting", "pending" and "decision" are ordinary words.
#: A corpus with no ordinary prose in it has no ordinary words to measure
#: against, and rarity measured there says only that the corpus is small.
_ORDINARY_PROSE = (
    "This note records an ordinary week of work. The meeting on Tuesday was "
    "short and the pending decision was carried over again; the call is still "
    "open and the team will settle it before the review. Nothing here changes "
    "the ceiling or the cycle, and the summary is unchanged from last week."
)
#: Ordinary notes seeded at every corpus size, `bulk` on top of them. Enough
#: to put the everyday words past the rare cap AND to carry the corpus past
#: `RETRIEVAL_CARRY_MIN_PAGES`, since below that the carry does not run at
#: all — a vault too small to measure rarity against is not a vault the
#: carry can read a name out of.
_ORDINARY_NOTES = 100
#: Prose for the proximity corpus: it deliberately uses every everyday word
#: of the proximity turns except the two each finding turns on.
_PROXIMITY_PROSE = (
    "This note records an ordinary week. I am flying out next week and wanted "
    "to walk around for a while if there is time; I keep trying to remember "
    "whether the change to the lane split was decided in the same month or a "
    "month apart, and whether the team was involved. We will decide about it "
    "before the review. Nothing here changes the cycle or the ceiling, and the "
    "summary is unchanged. The meeting was short, the pending decision carried "
    "over, the call is open and the team will settle it. I also need to book a "
    "venue at some point this month and reschedule the dentist, and to ask "
    "whether the run was still reconciled on a Tuesday, since the end of it "
    "was never written down and the freight side has changed twice."
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def seed_ordinary_notes(vault: Path, count: int = _ORDINARY_NOTES) -> None:
    """The notes any real vault is mostly made of.

    They do two jobs at once, and both are the point rather than padding:
    they make everyday words everyday, so rarity can tell a name from a
    word, and they carry the corpus past `RETRIEVAL_CARRY_MIN_PAGES`, below
    which there is nothing to measure rarity against.
    """
    journal = vault / "Knowledge Base" / "Notes" / "Journal"
    for index in range(max(0, count)):
        _write(
            journal / f"ordinary-note-{index:05d}.md",
            "---\ntype: note\nstatus: active\nupdated: 2026-08-01\n---\n\n"
            f"# Ordinary note {index:05d}\n\n## Summary\n\n"
            f"- [note] {_ORDINARY_PROSE} ^o-{index}\n",
        )


def _seed_carry_pages(vault: Path) -> None:
    """One dominant research note, two raw-material pages that outrank it, and
    two near-twins that must tie rather than carry. None is an anchor."""
    kb = vault / "Knowledge Base"
    _write(
        kb / "Notes" / "Research" / "quillon-vantry-window.md",
        """---
type: research-note
status: active
updated: 2026-09-10
---

# Quillon vantry window

## Summary

Why the quillon vantry window was widened, and what the decision rests on.

- [decision] The quillon vantry window was widened to nine minutes after
  the narrow window starved the vantry queue twice in one week. ^q-decision
- [finding] A quillon parcel that misses its window is retried whole, so a
  narrow window costs more work than it saves. ^q-finding
- [constraint] The quillon vantry window never exceeds twelve minutes: past
  that the queue's own retention drops parcels on the floor. ^q-constraint
""",
    )
    _write(
        kb / "Sources" / "quillon-vantry-window-transcript.md",
        """---
type: source
status: active
updated: 2026-09-09
---

# Quillon vantry window transcript

Raw transcript. Quillon vantry window, quillon vantry window, quillon
vantry decided, quillon window vantry, vantry window quillon vantry.
""",
    )
    _write(
        kb / "Evidence" / "quillon-vantry-window-receipt.md",
        """---
type: evidence
status: active
updated: 2026-09-09
---

# Quillon vantry window receipt

Preserved record. Quillon vantry window, quillon vantry window, quillon
window decided, quillon vantry window, vantry quillon window vantry.
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
    seed_ordinary_notes(vault)
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


def _seed_prose_corpus(vault: Path, *, bulk: int = 0) -> None:
    """A vault that reads like a vault: ordinary notes, one page the turn can
    name, and one stub that shares only ordinary words with it."""
    _seed_structure(vault)
    _seed_planning(vault)
    kb = vault / "Knowledge Base"
    _write(
        kb / "Notes" / "Research" / "kelvane-throughput-review.md",
        """---
type: research-note
status: active
updated: 2026-09-10
---

# Kelvane throughput review

## Summary

- [decision] The kelvane throughput ceiling was raised to eleven units after
  the review found the old ceiling idle for most of the cycle. ^k-decision
- [finding] Raising the kelvane ceiling costs nothing while the cycle is
  idle. ^k-finding
""",
    )
    _write(
        kb / "Notes" / "Inbox" / "meeting-notes.md",
        """---
type: note
status: active
updated: 2026-08-01
---

# Meeting notes

## Summary

- [note] Decision pending. ^m-1
""",
    )
    _write(
        kb / "Notes" / "Research" / "collapse-browse-window.md",
        """---
type: research-note
status: active
updated: 2026-09-12
---

# Collapse browse window

## Summary

- [decision] The collapse browse window was shortened after the collapse
  browse pass was found to run twice per cycle. ^c-decision
""",
    )
    seed_ordinary_notes(vault, _ORDINARY_NOTES + max(0, bulk))
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()


@pytest.fixture
def prose_vault(vault: Path) -> Path:
    """`_seed_prose_corpus` at its smallest representative size."""
    _seed_prose_corpus(vault)
    return vault


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


def _page_item(path: str) -> working_set.LaneItem:
    """One lane item for `path` carrying NO title of its own, so the anchor
    entry has to find one somewhere else."""
    return working_set.LaneItem(
        role="precedents",
        level="unit",
        ref=f"{path}#unit-x",
        path=path,
        title="",
        text="A unit whose lane knew no page title.",
        lifecycle="active",
        updated="2026-09-01",
        anchor=path,
    )


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
    assert CARRY_EVIDENCE not in paths


# --------------------------------------------------------------------------- #
# Named contact: the rarity gate the dominance test rests on
# --------------------------------------------------------------------------- #


def test_the_rare_document_cap_is_corpus_relative() -> None:
    """A fixed cap is the same mistake a fixed score floor was.

    "Distinctive" is a statement about THIS corpus. Half a percent of the
    indexed pages is the share; the floor of three keeps the cap usable on a
    vault too small for a share to mean anything.
    """
    assert working_set.rare_document_cap(0) == 3
    assert working_set.rare_document_cap(38) == 3
    assert working_set.rare_document_cap(600) == 3
    assert working_set.rare_document_cap(1539) == 8
    assert working_set.rare_document_cap(20000) == 100


def test_a_corpus_too_small_to_measure_rarity_carries_nothing(vault: Path) -> None:
    """Rarity needs a corpus.

    The reviewer's stub case at fixture scale: thirty-odd pages, a two-line
    note titled "Meeting notes" whose one unit reads "Decision pending", and
    an ordinary turn about a meeting and a pending decision. On a vault that
    small every one of those words IS rare by measurement — "meeting"
    appears on exactly one page — so the rarity gate has nothing to judge
    with and the stub was carried at 12.02. Below
    `RETRIEVAL_CARRY_MIN_PAGES` the carry does not run at all.
    """
    _seed_structure(vault)
    _seed_planning(vault)
    _write(
        vault / "Knowledge Base" / "Notes" / "Inbox" / "meeting-notes.md",
        """---
type: note
status: active
updated: 2026-08-01
---

# Meeting notes

## Summary

- [note] Decision pending. ^m-1
""",
    )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()

    _rare, pages, state = working_set_runtime.rare_turn_terms(
        vault, working_set_runtime.content_stems(STUB_TURN)
    )
    assert state == "available"
    assert pages < working_set.RETRIEVAL_CARRY_MIN_PAGES, pages

    hits, state = working_set_runtime.carry_candidates(vault, STUB_TURN)
    assert state == "available"
    assert hits == (), hits

    packet = working_set.compile_packet(vault, turn=STUB_TURN, max_chars=4000)
    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unresolved"}
    assert "carried_by" not in packet["generation"]


def test_a_corpus_large_enough_to_measure_rarity_still_carries(prose_vault: Path) -> None:
    """The over-restriction half: past the floor the gate does its own work
    again, and the page the turn names is served."""
    _rare, pages, state = working_set_runtime.rare_turn_terms(
        prose_vault, working_set_runtime.content_stems(GENUINE_TURN)
    )
    assert state == "available"
    assert pages >= working_set.RETRIEVAL_CARRY_MIN_PAGES, pages

    hits, state = working_set_runtime.carry_candidates(prose_vault, GENUINE_TURN)
    assert state == "available"
    assert [path for path, _score in hits] == [GENUINE_PAGE], hits


def test_a_word_the_stemmer_does_not_settle_on_still_ranks(prose_vault: Path) -> None:
    """The ranking query takes the turn's WORDS; only the rarity gate takes
    its stems.

    Snowball is not idempotent — 237 of 6,225 words in this repository change
    under a second pass, "collapse" to "collaps" to "collap" — so handing the
    already-stemmed terms to a query that stems what it is given issues the
    MATCH for terms no page contains. The page still surfaced, because one
    term in the turn happened to be stable, and it scored 3e-06 where the
    same page for the same turn scores in the tens. A score that small is
    indistinguishable from noise, and everything downstream of it — the
    separation test, the sanity bound — was comparing numbers that meant
    nothing.
    """
    hits, state = working_set_runtime.carry_candidates(prose_vault, DOUBLE_STEM_TURN)

    assert state == "available"
    assert [path for path, _score in hits] == [DOUBLE_STEM_PAGE], hits
    assert hits[0][1] > 10.0, hits
    dominant = working_set.dominant_carry(hits)
    assert dominant is not None and dominant[0] == DOUBLE_STEM_PAGE


# --------------------------------------------------------------------------- #
# Proximity: two distinctive words far apart are a coincidence
# --------------------------------------------------------------------------- #


def _seed_proximity_corpus(vault: Path) -> None:
    """A corpus whose ordinary prose uses every everyday word of the turns
    below, so whatever is left rare is rare because the word is name-shaped
    and not because the fixture is thin."""
    _seed_structure(vault)
    _seed_planning(vault)
    kb = vault / "Knowledge Base"
    _write(
        kb / "Notes" / "Research" / "harbour-ledger.md",
        """---
type: research-note
status: active
updated: 2026-09-10
---

# Harbour ledger

## Summary

- [decision] The harbour ledger is reconciled each Tuesday before the lisbon
  freight window opens. ^h-1
- [finding] A harbour ledger left unreconciled past the lisbon window costs a
  whole cycle. ^h-2
""",
    )
    journal = kb / "Notes" / "Journal"
    for index in range(200):
        _write(
            journal / f"proximity-note-{index:05d}.md",
            "---\ntype: note\nstatus: active\nupdated: 2026-08-01\n---\n\n"
            f"# Proximity note {index:05d}\n\n## Summary\n\n"
            f"- [note] {_PROXIMITY_PROSE} ^p-{index}\n",
        )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()


def test_two_distinctive_words_far_apart_are_a_coincidence(vault: Path) -> None:
    """The finding: a turn about a trip that happens to use two of a page's
    words.

    "flying to lisbon ... walk around the harbour" shares `lisbon` and
    `harbour` with a page about a harbour ledger and a lisbon freight
    window. Both words are genuinely distinctive — the corpus is 235 pages
    of prose using every other word of the turn — and the page was carried
    at 18.51. Nine tokens apart in an ordinary sentence they are two things
    the speaker mentioned, not a name.
    """
    _seed_proximity_corpus(vault)

    turn = (
        "I am flying to lisbon next week and wanted to walk around the harbour "
        "if there is time"
    )
    rare, pages, _state = working_set_runtime.rare_turn_terms(
        vault, working_set_runtime.content_stems(turn)
    )
    assert pages >= working_set.RETRIEVAL_CARRY_MIN_PAGES, pages
    assert set(rare) == {"lisbon", "harbour"}, rare

    hits, state = working_set_runtime.carry_candidates(vault, turn)
    assert state == "available"
    assert hits == (), hits


def test_the_same_two_words_as_a_phrase_are_a_name(vault: Path) -> None:
    """The over-restriction half, and the whole point of a window: the same
    two words said together ARE how a page is named."""
    _seed_proximity_corpus(vault)

    turn = "what did we decide about the lisbon harbour"
    rare, _pages, _state = working_set_runtime.rare_turn_terms(
        vault, working_set_runtime.content_stems(turn)
    )
    # Exactly two, so nothing but the window can admit this page.
    assert set(rare) == {"lisbon", "harbour"}, rare

    hits, state = working_set_runtime.carry_candidates(vault, turn)

    assert state == "available"
    assert [path for path, _score in hits] == [
        "Knowledge Base/Notes/Research/harbour-ledger.md"
    ], hits


def test_three_distinctive_words_scattered_are_not_a_name(vault: Path) -> None:
    """The flat three-anywhere path admitted a page the turn never named.

    Measured: a long travel sentence mentioning three place names about
    forty tokens apart carried a freight rota that happens to list all
    three, at 26.19 and alone. Three scattered names are three things the
    speaker mentioned; a page that lists many places will contain any three
    of them. There is one admission path now — a phrase — and a third rare
    stem may raise the score but never admits.
    """
    _seed_proximity_corpus(vault)

    turn = (
        "I keep meaning to ask whether the lisbon run is still reconciled on a "
        "Tuesday, because nobody has updated the ledger since then, and the "
        "harbour end of it was never written down anywhere"
    )
    rare, _pages, _state = working_set_runtime.rare_turn_terms(
        vault, working_set_runtime.content_stems(turn)
    )
    page_words = {"lisbon", "ledger", "harbour"}
    assert page_words <= set(rare), rare
    assert working_set_runtime.adjacent_rare_pairs(turn, sorted(page_words)) == (), turn

    hits, state = working_set_runtime.carry_candidates(vault, turn)

    assert state == "available"
    assert hits == (), hits


def test_the_proximity_window_is_measured_on_the_turns_own_tokens() -> None:
    """Pure logic: which rare stems the turn said close enough together."""
    pairs = working_set_runtime.adjacent_rare_pairs(
        "the lisbon harbour window", ("lisbon", "harbour")
    )
    assert pairs == (("harbour", "lisbon"),)

    far = working_set_runtime.adjacent_rare_pairs(
        "I am flying to lisbon next week and wanted to walk around the harbour",
        ("lisbon", "harbour"),
    )
    assert far == ()


def test_a_stub_sharing_only_ordinary_words_is_never_carried(prose_vault: Path) -> None:
    """The finding this gate exists for.

    A two-line stub titled "Meeting notes" whose one unit reads "Decision
    pending." used to be carried for an ordinary turn about a meeting and a
    pending decision, and the hook injected "Decision pending." as durable
    memory. It shares "meeting", "pending" and "decision" with the turn —
    three stems, and under `min_matched_terms` over ALL stems that was
    contact. None of them is distinctive in a corpus that contains ordinary
    prose, so none of them counts now, and the turn abstains as it should.
    """
    hits, state = working_set_runtime.carry_candidates(prose_vault, STUB_TURN)

    assert state == "available"
    assert hits == (), hits
    assert working_set.dominant_carry(hits) is None


def test_the_page_a_turn_names_is_carried_at_every_corpus_size(prose_vault: Path) -> None:
    """The other half: the gate must not refuse the page the turn DID name.

    "kelvane" and "throughput" occur on one page however large the corpus
    grows, which is exactly what makes them a name rather than a word.
    """
    hits, state = working_set_runtime.carry_candidates(prose_vault, GENUINE_TURN)

    assert state == "available"
    assert [path for path, _score in hits] == [GENUINE_PAGE], hits
    dominant = working_set.dominant_carry(hits)
    assert dominant is not None and dominant[0] == GENUINE_PAGE


@pytest.mark.timeout(900)
@pytest.mark.parametrize("bulk", [200, 2000])
def test_the_gate_holds_as_the_corpus_grows(vault: Path, bulk: int) -> None:
    """Both halves again at 200 and 2000 added pages.

    The absolute score floor this replaced failed exactly here: `-bm25()` is
    not corpus-invariant, so the page the turn named scored 13.16 at bulk 0,
    6.81 at bulk 200 (refused) and was outranked by a filler note at bulk
    2000. Rarity is measured against the corpus, so it does not drift with
    it, and a filler note shares no distinctive word with anything.
    """
    _seed_prose_corpus(vault, bulk=bulk)

    stub, state = working_set_runtime.carry_candidates(vault, STUB_TURN)
    assert state == "available"
    assert stub == (), stub

    genuine, state = working_set_runtime.carry_candidates(vault, GENUINE_TURN)
    assert state == "available"
    assert [path for path, _score in genuine] == [GENUINE_PAGE], genuine
    assert not any("bulk" in path for path, _score in genuine), genuine
    dominant = working_set.dominant_carry(genuine)
    assert dominant is not None and dominant[0] == GENUINE_PAGE


def test_a_turn_with_nothing_distinctive_runs_no_second_query(
    prose_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fewer than two distinctive stems cannot produce a carry, so the
    ranking query is never made: the cheapest refusal is the one that never
    asks."""
    calls: list[str] = []
    real = lexstore.search_bm25_result

    def counting(*args, **kwargs):
        calls.append("bm25")
        return real(*args, **kwargs)

    monkeypatch.setattr(lexstore, "search_bm25_result", counting)

    hits, state = working_set_runtime.carry_candidates(prose_vault, "the meeting decision")

    assert hits == ()
    assert state == "available"
    assert calls == [], "the ranking query ran for a turn that could not carry"


def _seed_named_pages_corpus(vault: Path) -> None:
    """Ordinary prose, two separately-named pages, and a page whose
    predecessor it superseded."""
    _seed_structure(vault)
    _seed_planning(vault)
    kb = vault / "Knowledge Base"
    _write(
        kb / "Notes" / "Research" / "kelvane-throughput.md",
        """---
type: research-note
status: active
updated: 2026-09-10
---

# Kelvane throughput

## Summary

- [decision] The kelvane throughput ceiling was raised to eleven units after
  the review found the old ceiling idle. ^k-1
""",
    )
    _write(
        kb / "Notes" / "Research" / "murran-dispatch.md",
        """---
type: research-note
status: active
updated: 2026-09-11
---

# Murran dispatch

## Summary

- [decision] The murran dispatch lane was split in two so the second lane
  could drain independently. ^m-1
""",
    )
    _write(
        kb / "Notes" / "Research" / "girvan-slot-window.md",
        """---
type: research-note
status: active
updated: 2026-09-12
supersedes: [Knowledge Base/Notes/Research/girvan-slot-window-2025.md]
---

# Girvan slot window

## Summary

- [decision] The girvan slot window is nine minutes, revised from the earlier
  figure after the queue starved twice. ^g-now
""",
    )
    _write(
        kb / "Notes" / "Research" / "girvan-slot-window-2025.md",
        """---
type: research-note
status: superseded
updated: 2025-11-02
superseded_by: [Knowledge Base/Notes/Research/girvan-slot-window.md]
---

# Girvan slot window (2025)

## Summary

- [decision] The girvan slot window is four minutes. ^g-old
""",
    )
    seed_ordinary_notes(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()


def test_a_turn_naming_two_separate_pages_carries_neither(vault: Path) -> None:
    """The reviewer's B2. Both pages are named — each by its own distinctive
    phrase — and the ranking happened to spread them 31.07 against 17.40.

    A score gap between two NAMED pages says nothing about which one the
    turn meant; the same shape measured 19.60 against 19.16 on a turn
    differing only in wording. Serving the higher one is the round-1 failure
    in a new coat: a guess presented as a resolution.
    """
    _seed_named_pages_corpus(vault)

    turn = (
        "I am trying to remember whether the kelvane throughput ceiling change "
        "and the murran dispatch lane split were decided in the same month"
    )
    hits, state = working_set_runtime.carry_candidates(vault, turn)

    assert state == "available"
    assert len(hits) == 2, hits
    assert working_set.dominant_carry(hits) is None

    packet = working_set.compile_packet(vault, turn=turn, max_chars=4000)
    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unresolved"}


def test_a_superseded_predecessor_does_not_block_its_successor(vault: Path) -> None:
    """The prerequisite. A note and the note that superseded it are named by
    the SAME phrase, so counting named pages would refuse every revised page
    in the vault — and the one thing the client must not be handed is the
    stale figure. Lifecycle is decided before the count.
    """
    _seed_named_pages_corpus(vault)

    current = "Knowledge Base/Notes/Research/girvan-slot-window.md"
    superseded = "Knowledge Base/Notes/Research/girvan-slot-window-2025.md"
    assert working_set._is_current_page(vault, current) is True
    assert working_set._is_current_page(vault, superseded) is False

    turn = "what did we decide about the girvan slot window"
    hits, state = working_set_runtime.carry_candidates(vault, turn)

    assert state == "available"
    assert [path for path, _score in hits] == [current], hits
    dominant = working_set.dominant_carry(hits)
    assert dominant is not None and dominant[0] == current


def test_an_archived_page_is_never_carried(vault: Path) -> None:
    """Same rule, the other lifecycle that retires a page."""
    _seed_named_pages_corpus(vault)
    _write(
        vault / "Knowledge Base" / "Notes" / "Research" / "girvan-slot-window.md",
        """---
type: research-note
status: archived
updated: 2026-09-12
---

# Girvan slot window

## Summary

- [decision] The girvan slot window is nine minutes. ^g-now
""",
    )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()

    hits, state = working_set_runtime.carry_candidates(
        vault, "what did we decide about the girvan slot window"
    )

    assert state == "available"
    assert hits == (), hits


# --------------------------------------------------------------------------- #
# The dominance rule, as pure logic
# --------------------------------------------------------------------------- #


def test_nothing_is_dominant_among_no_hits() -> None:
    assert working_set.dominant_carry(()) is None


def test_a_lone_surviving_hit_is_dominant() -> None:
    """Every survivor already carries two distinctive stems, and there is no
    second page for this one to be confused with. No absolute score is
    consulted: `-bm25()` is not comparable between two corpora, which is what
    made the floor this replaced meaningless."""
    assert working_set.dominant_carry((("a.md", 3.0),)) == ("a.md", 3.0)
    assert working_set.dominant_carry((("a.md", 41.0),)) == ("a.md", 41.0)


def test_a_hit_scoring_essentially_nothing_is_never_dominant() -> None:
    """The one sanity bound left, and it has to catch more than exact zero.

    The catalogue returns rows scoring 0.0 at corpus scale, and a
    double-stemmed query once produced 3e-06 for a page that scores 25.2
    when asked properly — a number a bound of "greater than zero" waves
    through and every comparison downstream then treats as a real score.
    A genuine match on one distinctive term scores several units.
    """
    assert working_set.dominant_carry((("a.md", 0.0),)) is None
    assert working_set.dominant_carry((("a.md", 3e-06),)) is None
    assert working_set.dominant_carry((("a.md", 0.9),)) is None
    assert working_set.dominant_carry((("a.md", 6.3),)) == ("a.md", 6.3)


def test_two_named_rows_carry_nothing_however_far_apart_they_score() -> None:
    """Every row that reaches this function passed the naming test, so two
    rows means the turn named two pages — and choosing between them on a
    score is the guess the compiler exists not to make, whatever the gap.

    The score gap is not evidence about which page the turn meant: measured,
    one turn naming two pages equally scored them 19.60 against 19.16, and
    another, differing only in wording, scored 31.07 against 17.40.
    """
    assert working_set.dominant_carry((("a.md", 11.0), ("b.md", 10.0))) is None
    assert working_set.dominant_carry((("a.md", 31.07), ("b.md", 17.40))) is None
    assert working_set.dominant_carry((("a.md", 99.0), ("b.md", 2.0))) is None


# --------------------------------------------------------------------------- #
# The candidate set
# --------------------------------------------------------------------------- #


def test_raw_material_is_never_a_candidate(carry_vault: Path) -> None:
    """Raw material is what a conclusion was drawn FROM, never durable memory.

    Both raw pages outrank the compiled note on this very turn, so this is
    not a rule with nothing to bite on: excluding them is what leaves the
    compiled page alone at the top.
    """
    raw = lexstore.search_bm25_result(
        carry_vault,
        "quillon vantry window decide",
        working_set_runtime.RETRIEVAL_CARRY_LIMIT,
        scope="kb",
        allow_delta=False,
        min_matched_terms=working_set.RETRIEVAL_CARRY_MIN_RARE_TERMS,
    )
    ranked = [path for path, _score in raw.value or ()]
    assert set(ranked[:2]) == {CARRY_SOURCE, CARRY_EVIDENCE}, ranked

    hits, state = working_set_runtime.carry_candidates(carry_vault, CARRY_TURN)
    assert state == "available"
    assert [path for path, _score in hits] == [CARRY_PAGE]


def test_the_raw_material_rule_is_the_index_rule() -> None:
    """One spelling of "raw material", shared with the module that already
    refuses to make an anchor of anything inside those folders — a top-level
    knowledge-base folder, not any segment called `Sources`."""
    assert working_set_runtime.CARRY_EXCLUDED_FOLDERS == (
        working_set_index._RAW_MATERIAL_FOLDERS
    )
    assert working_set_runtime._is_raw_material("Knowledge Base/Sources/a.md") is True
    assert working_set_runtime._is_raw_material("Knowledge Base/Evidence/receipt.md") is True
    assert working_set_runtime._is_raw_material("Knowledge Base\\Sources\\a.md") is True
    assert (
        working_set_runtime._is_raw_material("Knowledge Base/Notes/Sources of error.md") is False
    )
    assert working_set_runtime._is_raw_material("Knowledge Base/Notes/Sources/a.md") is False


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
    assert anchor["title"] == "Quillon vantry window"
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


def test_a_turn_that_names_two_pages_carries_neither(
    carry_vault: Path, budget_free
) -> None:
    """Two near-twin notes, both named by the same turn. The turn abstains
    and the client can ask which one it meant."""
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
            "and what did we decide about the quillon vantry window?"
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


def test_the_carry_costs_only_the_turns_that_would_have_abstained(
    carry_vault: Path, budget_free
) -> None:
    """Its own span, and that span exists only where the turn had nothing.

    The whole cost argument for the carry is that it runs after resolution
    has already given up, so a turn that resolves an anchor pays for none of
    it. A span that showed up on a resolved turn would mean the recall was
    running for every request.
    """
    carried_timings = find_types.FindTimings()
    working_set.compile_packet(
        carry_vault, turn=CARRY_TURN, max_chars=4000, timings=carried_timings
    )
    assert "working_set.carry" in carried_timings.as_dict()["stages"]

    resolved_timings = find_types.FindTimings()
    packet = working_set.compile_packet(
        carry_vault,
        turn="I'm planning to tow the Cargo Sled north — what are its constraints?",
        max_chars=4000,
        timings=resolved_timings,
    )
    assert packet["abstained"] is False, packet.get("abstention")
    assert "working_set.carry" not in resolved_timings.as_dict()["stages"]


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
        assert working_set._carry_by_retrieval(carry_vault, turn=CARRY_TURN) == ()
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


def test_the_carry_refuses_a_stage_the_door_budget_cannot_finish(
    carry_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The carry's query is the SAME shape as the first lexical pass, so on a
    turn where that pass was expensive this one will be too.

    The generic one-second stage reserve is enough to START it and nothing
    interrupts a stage already running, so a request whose first pass took
    three seconds used to begin a second three-second pass with three
    seconds left, overshoot the six-second door budget, and come back
    `unavailable` — which renders nothing and reads as a fault — where it
    used to abstain `unresolved` at half the cost. The gate now asks for
    room proportional to what the first pass actually took.
    """
    budget = request_budget.RequestBudget(seconds=6.0)
    token = request_budget.set_current(budget)
    try:
        # Three seconds already spent, three left: enough for the generic
        # reserve, nowhere near enough for another three-second query.
        monkeypatch.setattr(budget, "deadline", budget.deadline - 3.0)
        carried = working_set._carry_by_retrieval(
            carry_vault, turn=CARRY_TURN, lexical_seconds=3.0
        )
    finally:
        request_budget.reset_current(token)

    assert carried == ()
    assert "working_set.carry" in budget.as_response_block()["skipped"]


def test_the_carry_runs_when_the_first_pass_was_cheap(
    carry_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The over-restriction half: a cheap first pass must not refuse the
    carry just because some budget is bound."""
    budget = request_budget.RequestBudget(seconds=6.0)
    token = request_budget.set_current(budget)
    try:
        monkeypatch.setattr(budget, "deadline", budget.deadline - 3.0)
        carried = working_set._carry_by_retrieval(
            carry_vault, turn=CARRY_TURN, lexical_seconds=0.05
        )
    finally:
        request_budget.reset_current(token)

    assert [path for path, _score in carried] == [CARRY_PAGE]


def test_the_reserve_covers_the_dearest_carry_measured(
    carry_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A carry that runs past its reserve turns a timely `unresolved` into
    `unavailable`, which renders nothing — so the reserve has to cover the
    worst ratio observed, not the typical one.

    Measured against the request's first lexical pass: 0.9x, 1.1x and 1.9x
    on a quiet machine at zero, two hundred and two thousand added pages,
    and 2.0x, 2.3x and 1.5x for the same tip under load.
    """
    assert working_set.RETRIEVAL_CARRY_BUDGET_MULTIPLE >= 2.3

    budget = request_budget.RequestBudget(seconds=6.0)
    token = request_budget.set_current(budget)
    try:
        # One second spent, five left; a first pass costing 2.2 s means the
        # dearest observed carry needs 5.06 s, which does not fit.
        monkeypatch.setattr(budget, "deadline", budget.deadline - 1.0)
        carried = working_set._carry_by_retrieval(
            carry_vault, turn=CARRY_TURN, lexical_seconds=2.2
        )
    finally:
        request_budget.reset_current(token)

    assert carried == ()
    assert "working_set.carry" in budget.as_response_block()["skipped"]


def test_a_carried_page_with_no_readable_units_abstains(
    carry_vault: Path, budget_free, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page can dominate recall and still have nothing the lanes can read:
    its units carry categories no unit role selects, or it has no units at all.

    Serving that as `abstained: false` with an empty `units` block states
    that the turn resolved and the vault had nothing, which is a different
    and false claim — and the hook renders neither material nor the menu an
    `unresolved` abstention would have shown, so the client sees a blank.
    """
    monkeypatch.setattr(
        working_set, "run_lanes", lambda *args, **kwargs: ((), ({"role": "x", "reason": "no_material"},))
    )

    packet = working_set.compile_packet(carry_vault, turn=CARRY_TURN, max_chars=4000)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unresolved"}
    assert packet["units"] == []
    assert packet["anchors"] == []
    assert "carried_by" not in packet["generation"]


def test_a_carried_page_serves_each_of_its_units_once(
    carry_vault: Path, budget_free
) -> None:
    """The carry selects every `units` role, so overlapping categories read
    one page's units several times over. Measured before the fix: five units
    served, three distinct refs, and the hook printed the same sentence
    twice."""
    packet = working_set.compile_packet(carry_vault, turn=CARRY_TURN, max_chars=4000)

    assert packet["generation"]["carried_by"] == "retrieval"
    refs = [unit["ref"] for unit in packet["units"]]
    assert refs, packet
    assert len(refs) == len(set(refs)), refs


def test_a_carried_page_that_is_an_anchor_row_reports_the_indexed_title(
    carry_vault: Path, budget_free, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page can dominate recall and also be an anchor the turn failed to
    resolve. The index already holds its authored title, so the packet says
    that rather than its filename — the path is the last fallback, not the
    first answer.
    """
    hub = "Knowledge Base/Notes/Insights/northern-corridor-hub.md"
    monkeypatch.setattr(
        working_set, "_carry_by_retrieval", lambda *args, **kwargs: ((hub, 12.0),)
    )
    monkeypatch.setattr(
        working_set, "run_lanes", lambda *args, **kwargs: ((_page_item(hub),), ())
    )

    packet = working_set.compile_packet(carry_vault, turn=CARRY_TURN, max_chars=4000)

    assert packet["anchors"][0]["path"] == hub
    assert packet["anchors"][0]["title"] == "Northern corridor"


def test_the_carried_title_lookup_reads_the_rows_once(
    carry_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pass over the anchor rows, not one per row compared.

    The catalogue holds up to `working_set_index.MAX_ANCHORS` rows, and the
    lookup ran inside the request. A scan that happens to be short today is
    still a scan.
    """
    index = working_set_index.WorkingSetIndex(carry_vault)
    reads: list[str] = []
    real = index.anchors

    def counting():
        reads.append("anchors")
        return real()

    monkeypatch.setattr(index, "anchors", counting)
    hub = "Knowledge Base/Notes/Insights/northern-corridor-hub.md"

    assert working_set._indexed_title(index, hub) == "Northern corridor"
    assert working_set._indexed_title(index, "Knowledge Base/Notes/nope.md") == ""
    assert working_set._indexed_title(index, "") == ""
    assert len(reads) <= 2, reads


def test_a_sentence_boundary_ends_the_window() -> None:
    """Two distinctive words on either side of a full stop are two
    sentences, not a phrase — however few tokens separate them.

    Measured: "I am flying out to lisbon next week. The harbour was shut
    last time" paired `lisbon` with `harbour` across the stop and carried a
    harbour ledger at 18.78. A comma is not a boundary: "the girvan, slot
    question" is still one phrase interrupted by punctuation.
    """
    across = working_set_runtime.adjacent_rare_pairs(
        "I am flying out to lisbon next week. The harbour was shut last time",
        ("lisbon", "harbour"),
    )
    assert across == (), across

    for breaker in (".", "!", "?", ";", "\n"):
        assert working_set_runtime.adjacent_rare_pairs(
            f"the lisbon run{breaker} the harbour run", ("lisbon", "harbour")
        ) == (), breaker

    assert working_set_runtime.adjacent_rare_pairs(
        "the girvan, slot question", ("girvan", "slot")
    ) == (("girvan", "slot"),)


def test_a_hyphenated_name_pairs_on_both_of_its_stems() -> None:
    """One raw token can carry two stems. "girvan-slot" is the phrase said
    as tightly as a phrase can be said, and reading only the first stem
    lost the second entirely."""
    assert working_set_runtime.adjacent_rare_pairs(
        "what did we decide about the girvan-slot window", ("girvan", "slot")
    ) == (("girvan", "slot"),)
    assert working_set_runtime.adjacent_rare_pairs(
        "what did o'brien's team decide", ("o", "brien")
    ) == (("brien", "o"),)


def test_the_retired_vocabulary_is_the_trees_own() -> None:
    """Retirement is not a word list this module gets to invent.

    It did: `retired` and `deprecated` appear as a page status nowhere else
    in the tree, while `dropped` — which `activation._INACTIVE_STATUSES`
    has always carried — was missing, so a page the author dropped was
    carried and its unit injected as current memory.

    The set is now the tree's own inactive statuses minus the two that mean
    pre-active rather than retired. Pinned as a relation so the two cannot
    drift apart: a status added there arrives here without anyone noticing
    it needed to.
    """
    from exomem import activation

    assert working_set.RETIRED_PAGE_STATUSES == frozenset(
        activation._INACTIVE_STATUSES
    ) - {"draft", "planned"}
    assert "dropped" in working_set.RETIRED_PAGE_STATUSES
    assert working_set.RETIRED_PAGE_STATUSES == {"archived", "dropped", "superseded"}


def test_a_draft_page_is_still_a_candidate(vault: Path) -> None:
    """A draft is a page the author is still writing, not one they retired,
    and `planned` is the same shape: authored, not yet active, not retired.

    The spec retires superseded and archived pages; treating every non-active
    status as retired swept in `draft`, which is the status a page carries
    while it is being written — exactly the page a turn naming it wants.
    """
    _seed_named_pages_corpus(vault)
    kb = vault / "Knowledge Base" / "Notes" / "Cases"
    for name, status in (
        ("draft.md", "draft"),
        ("planned.md", "planned"),
        ("archived.md", "archived"),
        ("superseded-status.md", "superseded"),
        ("dropped.md", "dropped"),
        ("active.md", "active"),
        ("in-review.md", "in-review"),
    ):
        _write(
            kb / name,
            f"---\ntype: note\nstatus: {status}\nupdated: 2026-09-01\n---\n\n"
            f"# {name}\n\n## Summary\n\n- [note] A page. ^x-1\n",
        )
    _write(
        kb / "no-status.md",
        "---\ntype: note\nupdated: 2026-09-01\n---\n\n# No status\n\n"
        "## Summary\n\n- [note] A page. ^x-2\n",
    )

    def current(name: str) -> bool:
        return working_set._is_current_page(vault, f"Knowledge Base/Notes/Cases/{name}")

    assert current("draft.md") is True
    assert current("planned.md") is True
    assert current("in-review.md") is True
    assert current("active.md") is True
    assert current("no-status.md") is True
    assert current("archived.md") is False
    assert current("superseded-status.md") is False
    assert current("dropped.md") is False


def test_filtering_happens_after_a_wider_fetch(
    prose_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Exactly one named page" must mean one in the corpus, not one among
    whatever survived the ranking limit.

    The limit cut the rows BEFORE raw material and retired pages were
    excluded, so a run of sources at the head could hide a second compiled
    page and turn "two named pages, abstain" into "one named page, carry" —
    the count the whole decision rests on, settled by where the limit
    happened to fall. The rows are stubbed so the ORDER of the cut and the
    filter is what is under test, not a corpus arranged to produce it.
    """
    asked: list[int] = []
    real = lexstore.search_bm25_result

    def ranked(vault_root, query, k, **kwargs):
        asked.append(k)
        real(vault_root, query, k, **kwargs)
        return lexstore.CatalogQueryResult(
            [
                ("Knowledge Base/Sources/one.md", 40.0),
                ("Knowledge Base/Sources/two.md", 39.0),
                ("Knowledge Base/Evidence/three.md", 38.0),
                ("Knowledge Base/Sources/four.md", 37.0),
                (GENUINE_PAGE, 22.0),
                (DOUBLE_STEM_PAGE, 21.0),
            ],
            lexstore.CatalogReadiness("available", True, "sqlite"),
        )

    monkeypatch.setattr(lexstore, "search_bm25_result", ranked)

    hits, state = working_set_runtime.carry_candidates(prose_vault, GENUINE_TURN)

    assert state == "available"
    assert asked == [working_set.RETRIEVAL_CARRY_FETCH], asked
    assert working_set.RETRIEVAL_CARRY_FETCH > 5, "a wider fetch is the point"
    # And the window is never narrower than the number of rows the rarity
    # gate can admit, at any corpus size.
    for pages in (100, 1000, 2000, 2001, 3000, 5000, 10000):
        assert working_set.carry_fetch_size(pages) > working_set.rare_document_cap(pages)
    # Four raw-material rows excluded, two compiled pages left: two named
    # pages, so nothing is carried.
    assert [path for path, _score in hits] == [GENUINE_PAGE, DOUBLE_STEM_PAGE], hits
    assert working_set.dominant_carry(hits) is None


def test_a_turn_that_names_two_pages_lists_them_for_the_client(vault: Path) -> None:
    """An abstention that says nothing leaves the client with an empty
    packet and no way to know that a question would help.

    The turn named two pages; neither is carried, because choosing is the
    guess the compiler exists not to make. But the client can choose, and
    the only thing it needs is their names.
    """
    _seed_named_pages_corpus(vault)

    turn = (
        "I am trying to remember whether the kelvane throughput ceiling change "
        "and the murran dispatch lane split were decided in the same month"
    )
    packet = working_set.compile_packet(vault, turn=turn, max_chars=4000)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unresolved"}
    assert packet["units"] == []
    listed = packet["anchors"]
    assert {item["path"] for item in listed} == {
        "Knowledge Base/Notes/Research/kelvane-throughput.md",
        "Knowledge Base/Notes/Research/murran-dispatch.md",
    }, listed
    assert {item["status"] for item in listed} == {"retrieval_named"}
    assert {item["kind"] for item in listed} == {"page"}
    assert all(item["evidence"] == ["retrieval"] for item in listed)
    # Not `ambiguity`, which means two anchors RESOLVED.
    assert packet["ambiguity"] == []
    # And the titles are the pages' own, not their filenames.
    assert "Kelvane throughput" in {item["title"] for item in listed}


def test_the_hook_renders_the_named_pages_as_its_menu(vault: Path) -> None:
    """The client-facing half: the shipped hook injects the list."""
    from exomem._hooks import exomem_retrieve_nudge as nudge

    _seed_named_pages_corpus(vault)
    turn = (
        "I am trying to remember whether the kelvane throughput ceiling change "
        "and the murran dispatch lane split were decided in the same month"
    )
    packet = working_set.compile_packet(vault, turn=turn, max_chars=4000)

    block = nudge._format_working_set_block(packet, nudge._WORKING_SET_MAX_CHARS)

    assert "kelvane-throughput.md" in block, block
    assert "murran-dispatch.md" in block, block
    assert nudge._block_keeps_the_reminder(packet) is True
    # And the remedy it offers is the one that works on a page that is not
    # an anchor of this index: `anchor=` raises INVALID_ANCHOR for these
    # refs, `read_memory` returns the page.
    closing = block.splitlines()[-1]
    assert "read_memory" in closing, closing
    assert "`anchor`" not in closing, closing


def test_a_dropped_page_is_never_carried(vault: Path) -> None:
    """The measured hole: `status: dropped` was carried and its unit served
    as current memory, because `dropped` was missing from a hand-written
    retirement vocabulary."""
    _seed_named_pages_corpus(vault)
    _write(
        vault / "Knowledge Base" / "Notes" / "Research" / "girvan-slot-window.md",
        """---
type: research-note
status: dropped
updated: 2026-09-12
---

# Girvan slot window

## Summary

- [decision] The girvan slot window is nine minutes. ^g-now
""",
    )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()

    hits, state = working_set_runtime.carry_candidates(
        vault, "what did we decide about the girvan slot window"
    )

    assert state == "available"
    assert hits == (), hits


def test_a_carried_draft_reports_itself_as_a_draft(vault: Path) -> None:
    """A `draft` is a candidate, so a packet can be carried on one — and a
    packet that called it `active` would be telling the reader something the
    page does not say."""
    _seed_named_pages_corpus(vault)
    _write(
        vault / "Knowledge Base" / "Notes" / "Research" / "girvan-slot-window.md",
        """---
type: research-note
status: draft
updated: 2026-09-12
---

# Girvan slot window

## Summary

- [decision] The girvan slot window is nine minutes. ^g-now
""",
    )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()

    packet = working_set.compile_packet(
        vault, turn="what did we decide about the girvan slot window", max_chars=4000
    )

    assert packet["generation"]["carried_by"] == "retrieval"
    assert packet["anchors"][0]["lifecycle"] == "draft", packet["anchors"]


def test_the_fetch_window_outgrows_the_rarity_cap(
    prose_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window has to be wider than the gate, at every corpus size.

    Up to `rare_document_cap(pages)` pages can share one distinctive phrase,
    and that cap passes a fixed ten from 2,001 pages (11 at 2,200, 25 at
    5,000, 50 at 10,000). Above that, retired pages sharing the phrase can
    fill the ranked window on their own, the surviving count reads one, and
    a turn that named a second page carries the first anyway — the exact
    failure the count was introduced to prevent, reappearing through the
    limit instead of through the score.
    """
    assert working_set.carry_fetch_size(100) == working_set.RETRIEVAL_CARRY_FETCH
    assert working_set.carry_fetch_size(2200) == working_set.rare_document_cap(2200) + 1
    assert working_set.carry_fetch_size(10000) == working_set.rare_document_cap(10000) + 1

    # The shape that can actually consume the window: retired pages sharing
    # the phrase, ranked above the two current ones.
    asked: list[int] = []
    real = lexstore.search_bm25_result
    retired = [(f"Knowledge Base/Notes/Old/superseded-{i:02d}.md", 40.0 - i) for i in range(11)]

    def ranked(vault_root, query, k, **kwargs):
        asked.append(k)
        real(vault_root, query, k, **kwargs)
        return lexstore.CatalogQueryResult(
            [*retired, (GENUINE_PAGE, 22.0), (DOUBLE_STEM_PAGE, 21.0)],
            lexstore.CatalogReadiness("available", True, "sqlite"),
        )

    monkeypatch.setattr(lexstore, "search_bm25_result", ranked)
    monkeypatch.setattr(
        working_set,
        "_is_current_page",
        lambda _root, rel: "superseded-" not in rel,
    )
    monkeypatch.setattr(working_set, "rare_document_cap", lambda _pages: 11)

    hits, state = working_set_runtime.carry_candidates(prose_vault, GENUINE_TURN)

    assert state == "available"
    assert asked == [12], asked
    # Eleven retired rows excluded; the two current pages both counted, so
    # the turn named two and carries neither.
    assert [path for path, _score in hits] == [GENUINE_PAGE, DOUBLE_STEM_PAGE], hits
    assert working_set.dominant_carry(hits) is None


# --------------------------------------------------------------------------- #
# The two units meeting: a carried or named packet still leads with working
# continuity
# --------------------------------------------------------------------------- #


def _seed_live_cell(vault: Path) -> None:
    """Seed the freshness registry the way the running service does.

    `recent_context` reads per-path mtimes out of that registry rather than
    walking the vault, so a test that never seeds it exercises the not-live
    fallback instead of the block.
    """
    from exomem import file_watcher

    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)


def _recent_cost(packet: dict) -> int:
    """What the working-continuity block spends, by its own arithmetic."""
    return sum(
        len(str(entry.get("title") or "")) + len(str(entry.get("statement") or ""))
        for entry in packet.get("recent_context") or ()
    )


def test_a_carried_and_a_named_packet_both_lead_with_recent_context(
    vault: Path, budget_free
) -> None:
    """The one thing neither unit could test on its own.

    Working continuity is assembled BEFORE resolution is branched on, so
    every exit from `compile_packet` carries it — including the two this
    design added after it. A carried packet is built by `_carried_packet`
    and a named abstention by its own `abstained_packet` call, and either
    one that forgot to pass the block through would hand a resumed session
    a packet about one page and nothing about what it had been working on:
    exactly the silence the block exists to end, reappearing on the turns
    that reach the newest path.

    The block leading is not the block taking over. It is budgeted first and
    counted INSIDE `max_chars`, never added on top, so a carried packet pays
    for both out of one ceiling and still serves the page's units.
    """
    _seed_named_pages_corpus(vault)
    _seed_live_cell(vault)

    # One named page: carried.
    carried = working_set.compile_packet(
        vault, turn="what did we decide about the girvan slot window", max_chars=4000
    )

    assert carried["abstained"] is False, carried.get("abstention")
    assert carried["generation"]["carried_by"] == "retrieval"
    assert _anchor_statuses(carried) == ["retrieval_carried"]
    assert list(carried)[0] == "recent_context", list(carried)
    assert carried["recent_context"], "a carried packet still says what was worked on"
    assert carried["units"], "and still serves the page it carried"
    assert carried["budget"]["used_chars"] <= 4000

    # Two named pages: no carry, and the list the client can choose from.
    named = working_set.compile_packet(
        vault,
        turn=(
            "I am trying to remember whether the kelvane throughput ceiling change "
            "and the murran dispatch lane split were decided in the same month"
        ),
        max_chars=4000,
    )

    assert named["abstained"] is True
    assert named["abstention"] == {"reason": "unresolved"}
    assert set(_anchor_statuses(named)) == {"retrieval_named"}
    assert list(named)[0] == "recent_context", list(named)
    assert named["recent_context"], "a named abstention still says what was worked on"
    assert named["budget"]["used_chars"] == _recent_cost(named)
    assert named["budget"]["used_chars"] <= 4000


def test_a_carried_packet_pays_for_both_blocks_out_of_one_ceiling(
    vault: Path, budget_free
) -> None:
    """At the budget floor the two blocks share one ceiling rather than
    stacking: the block takes at most half, and the carried page's own
    material spends what is left. A packet that leads with continuity and
    serves nothing is not a carried packet."""
    _seed_named_pages_corpus(vault)
    _seed_live_cell(vault)

    limit = working_set.MIN_BUDGET_CHARS
    packet = working_set.compile_packet(
        vault, turn="what did we decide about the girvan slot window", max_chars=limit
    )

    assert packet["abstained"] is False, packet.get("abstention")
    assert packet["generation"]["carried_by"] == "retrieval"
    assert packet["recent_context"]
    assert packet["units"], "the carried page's material must still be affordable"
    assert _recent_cost(packet) <= limit // 2
    assert packet["budget"]["used_chars"] <= limit
