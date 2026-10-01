"""Concurrent contexts, end to end: every domain a turn names is served.

A turn that names two domains ("should I book the autumn trip given the course
schedule?") is about both. Where a domain is an ordinary page rather than an
anchor, the retrieval carry is what reaches it, and that carry used to run only
for a turn that resolved nothing and to serve only when the turn named exactly
one page. Now each phrase the turn names is its own candidate: a phrase naming
one page carries it, beside whatever the resolver resolved, and a phrase two
pages answer to stays a question.

Invented pages throughout; the corpus is the carry suite's own generic one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_working_set_carry import (
    CARRY_PAGE,
    GENUINE_PAGE,
    TIE_TURN,
    _seed_carry_pages,
    _seed_prose_corpus,
)

from exomem import working_set, working_set_runtime

SLED = "Knowledge Base/Products/Cargo Sled.md"
KELVANE_TURN = "what did the review conclude about the kelvane throughput ceiling"
QUILLON_TURN = "what did we decide about the quillon vantry window"
BOTH_TURN = f"{KELVANE_TURN}, and {QUILLON_TURN}"


@pytest.fixture
def domain_vault(vault: Path) -> Path:
    _seed_prose_corpus(vault)
    _seed_carry_pages(vault)
    from exomem import lexstore, working_set_index

    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


def _paths(packet: dict, status: str) -> set[str]:
    return {a["path"] for a in packet["anchors"] if a["status"] == status}


def test_two_pages_named_apart_are_both_carried(domain_vault: Path) -> None:
    packet = working_set.compile_packet(domain_vault, turn=BOTH_TURN, max_chars=6000)

    assert packet["abstained"] is False, packet["anchors"]
    assert _paths(packet, "retrieval_carried") == {GENUINE_PAGE, CARRY_PAGE}
    assert {u["provenance"]["path"] for u in packet["units"]} >= {GENUINE_PAGE, CARRY_PAGE}
    assert packet["generation"]["carried_by"] == "retrieval"


def test_a_resolved_anchor_and_a_named_page_are_both_served(domain_vault: Path) -> None:
    turn = "should I send the cargo sled given what the review concluded about the kelvane throughput ceiling"
    packet = working_set.compile_packet(domain_vault, turn=turn, max_chars=6000)

    assert packet["abstained"] is False
    assert SLED in _paths(packet, "resolved")
    assert _paths(packet, "retrieval_carried") == {GENUINE_PAGE}
    assert GENUINE_PAGE in {u["provenance"]["path"] for u in packet["units"]}


def test_a_phrase_two_pages_answer_to_stays_a_question(domain_vault: Path) -> None:
    """One clean domain is served; the contested one is not guessed."""
    turn = f"{TIE_TURN}, and {KELVANE_TURN}"
    packet = working_set.compile_packet(domain_vault, turn=turn, max_chars=6000)

    assert _paths(packet, "retrieval_carried") == {GENUINE_PAGE}
    assert not any("tarn-rollover" in u["provenance"]["path"] for u in packet["units"])


def test_a_turn_naming_one_page_is_carried_exactly_as_before(domain_vault: Path) -> None:
    packet = working_set.compile_packet(domain_vault, turn=QUILLON_TURN, max_chars=6000)

    assert _paths(packet, "retrieval_carried") == {CARRY_PAGE}
    assert len(packet["anchors"]) == 1


def test_a_resolved_turn_that_names_nothing_else_carries_nothing(domain_vault: Path) -> None:
    packet = working_set.compile_packet(
        domain_vault, turn="can the cargo sled take the extra load?", max_chars=6000
    )

    assert SLED in _paths(packet, "resolved")
    assert _paths(packet, "retrieval_carried") == set()
    assert packet["generation"].get("carried_by") is None
