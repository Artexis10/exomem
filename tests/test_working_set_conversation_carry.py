"""The conversation carry and the precedence ladder (tasks 3.1, 3.2).

An anaphoric turn of any length that reached nothing by its own words is
carried from the newest earlier USER turn that named a subject: a single
`partial` anchor, `generation.carried_by = "conversation"`. It sits fifth on
the ladder: after the anchor override, resolution, referential recency and the
shipped follow-up carry (each unchanged), before the retrieval carry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conversation_vault import seed
from test_governance_egress import _reset_caches
from test_working_set_carry import CARRY_PAGE, CARRY_TURN, _seed_carry_pages
from test_working_set_hot_projection import _live, _one_old_tick
from test_working_set_index import _seed_planning, _seed_structure

from exomem import (
    commands,
    lexstore,
    working_set_heat,
    working_set_index,
    working_set_resolve,
    working_set_runtime,
)

analyze = working_set_resolve.analyze_turn

RICH = (
    "Given everything above, how did her numbers compare with last year, and "
    "should we change anything before the next one?"
)


@pytest.fixture
def cvault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _seed_structure(vault)
    _seed_planning(vault)
    _seed_carry_pages(vault)
    seed(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    _reset_caches()
    _one_old_tick(vault)
    _live(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_heat.reset_for_tests()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return vault


def _user(text: str) -> dict:
    return {"role": "user", "text": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "text": text}


def _activate(vault: Path, turn: str, **kwargs) -> dict:
    return commands.op_activate_context(vault, turn=turn, **kwargs)


def _titles(packet: dict) -> list[str]:
    return [anchor["title"] for anchor in packet["anchors"]]


# --------------------------------------------------------------------------- #
# The anaphor set
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "turn",
    [
        "how did her results compare",
        "what did his team decide",
        "is that still true",
        "can you compare this with last year",
        "what about those",
        "which of these matters",
        "the second option looks better to me",
        "I prefer the first one",
        "the former was cheaper than the latter",
        "and what about the next quarter",
        "continue",
    ],
)
def test_an_anaphoric_turn_is_recognised(turn: str) -> None:
    assert analyze(turn).anaphoric, turn


@pytest.mark.parametrize(
    "turn",
    [
        "how is the weather looking for Sunday afternoon",
        "tell me about the quarterly review schedule",
    ],
)
def test_a_turn_with_no_anaphor_is_not_anaphoric(turn: str) -> None:
    assert not analyze(turn).anaphoric, turn


def test_length_is_not_a_criterion() -> None:
    assert analyze(RICH).anaphoric and len(analyze(RICH).tokens) > 8


# --------------------------------------------------------------------------- #
# The carry
# --------------------------------------------------------------------------- #

THREAD = {"recent": [_user("How did Ottilie Marsh do in the spring?"), _assistant("Fine, all things told.")]}


def test_a_rich_follow_up_keeps_the_conversations_subject(cvault: Path) -> None:
    plain = _activate(cvault, RICH)
    assert plain["abstention"] == {"reason": "unresolved"}
    carried = _activate(cvault, RICH, conversation=THREAD)
    assert carried["abstained"] is False
    (anchor,) = carried["anchors"]
    assert anchor["title"] == "Ottilie Marsh"
    assert anchor["status"] == "partial"
    assert anchor["evidence"] == ["conversation"]
    assert anchor["origin"] == "conversation"
    assert carried["generation"]["carried_by"] == "conversation"
    assert carried["units"], "the carried anchor's material is served under the ordinary lanes"


def test_the_newest_subject_wins_over_an_older_one(cvault: Path) -> None:
    packet = _activate(
        cvault,
        "what are the risks with that?",
        conversation={
            "recent": [
                _user("the Harbor Lantern Budget is over"),
                _assistant("It is."),
                _user("did the Kestrel Hiring Plan slip?"),
            ]
        },
    )
    assert _titles(packet) == ["Kestrel Hiring Plan"]
    assert "Harbor Lantern" not in str([unit["text"] for unit in packet["units"]])


def test_two_subjects_in_the_newest_entry_abstain_ambiguous_and_choose_nothing(cvault: Path) -> None:
    packet = _activate(
        cvault,
        "what are the risks with that?",
        conversation={"recent": [_user("the Kestrel Hiring Plan and the Marlow Quay Survey both slipped")]},
    )
    assert packet["abstention"] == {"reason": "ambiguous"}
    assert {item["title"] for item in packet["ambiguity"]} == {"Kestrel Hiring Plan", "Marlow Quay Survey"}
    assert packet["units"] == []
    assert "carried_by" not in packet["generation"]


def test_a_refs_only_conversation_never_carries(cvault: Path) -> None:
    packet = _activate(
        cvault, RICH, conversation={"refs": ["Knowledge Base/Entities/People/Ottilie Marsh.md"]}
    )
    assert packet["abstention"] == {"reason": "unresolved"}
    assert "carried_by" not in packet["generation"]


def test_assistant_entries_are_never_walked(cvault: Path) -> None:
    packet = _activate(
        cvault, RICH, conversation={"recent": [_assistant("Ottilie Marsh did well in the spring round.")]}
    )
    assert packet["abstention"] == {"reason": "unresolved"}


def test_a_turn_that_names_its_own_subject_is_not_carried(cvault: Path) -> None:
    packet = _activate(
        cvault,
        "how is Ottilie Marsh doing with her rota?",
        conversation={"recent": [_user("did the Kestrel Hiring Plan slip?")]},
    )
    assert packet["generation"].get("carried_by") is None
    assert "Kestrel Hiring Plan" not in _titles(packet)
    assert packet["anchors"][0]["title"] == "Ottilie Marsh"


def test_a_carried_anchor_never_enters_the_token(cvault: Path) -> None:
    packet = _activate(cvault, RICH, conversation=THREAD)
    payload = working_set_runtime.decode_continuity(packet["continuity"])
    assert payload["refs"] == []


def test_without_a_conversation_nothing_is_carried(cvault: Path) -> None:
    packet = _activate(cvault, RICH)
    assert "carried_by" not in packet["generation"]


# --------------------------------------------------------------------------- #
# The precedence ladder (3.2)
# --------------------------------------------------------------------------- #

KESTREL_ENTRY = {"recent": [_user("did the Kestrel Hiring Plan slip again?")]}


def test_referential_recency_still_wins(cvault: Path) -> None:
    named = _activate(cvault, "tell me about Ottilie Marsh")
    plain = _activate(cvault, "continue", continuity=named["continuity"])
    with_conversation = _activate(
        cvault, "continue", continuity=named["continuity"], conversation=KESTREL_ENTRY
    )
    assert _titles(plain) == _titles(with_conversation) == ["Ottilie Marsh"]
    assert with_conversation["generation"].get("carried_by") == plain["generation"].get("carried_by")
    assert with_conversation["generation"].get("carried_by") != "conversation"


def test_the_shipped_follow_up_carry_still_wins(cvault: Path) -> None:
    named = _activate(cvault, "tell me about Ottilie Marsh")
    working_set_runtime.reset_caches_for_tests()
    plain = _activate(cvault, "what about the second one?", continuity=named["continuity"])
    with_conversation = _activate(
        cvault, "what about the second one?", continuity=named["continuity"], conversation=KESTREL_ENTRY
    )
    assert plain["generation"]["carried_by"] == "follow_up"
    assert with_conversation["generation"]["carried_by"] == "follow_up"
    assert _titles(with_conversation) == _titles(plain) == ["Ottilie Marsh"]


def test_the_conversation_carry_precedes_the_retrieval_carry(cvault: Path) -> None:
    turn = f"{CARRY_TURN} and what do we think of that"
    baseline = _activate(cvault, turn)
    assert baseline["generation"].get("carried_by") == "retrieval", "the fixture must admit a recall carry"
    packet = _activate(cvault, turn, conversation=THREAD)
    assert packet["generation"]["carried_by"] == "conversation"
    assert _titles(packet) == ["Ottilie Marsh"]
    assert CARRY_PAGE not in [unit["provenance"]["path"] for unit in packet["units"]]


def test_an_ambiguous_conversation_carry_stops_the_ladder(cvault: Path) -> None:
    turn = f"{CARRY_TURN} and what do we think of that"
    packet = _activate(
        cvault,
        turn,
        conversation={"recent": [_user("the Kestrel Hiring Plan and the Marlow Quay Survey both slipped")]},
    )
    assert packet["abstention"] == {"reason": "ambiguous"}
    assert CARRY_PAGE not in str(packet["anchors"]) + str(packet["units"])


def test_a_conversation_that_names_nothing_falls_through_to_the_retrieval_carry(cvault: Path) -> None:
    turn = f"{CARRY_TURN} and what do we think of that"
    packet = _activate(cvault, turn, conversation={"recent": [_user("the harbour was quiet today")]})
    assert packet["generation"]["carried_by"] == "retrieval"


def test_every_existing_carry_is_untouched_without_a_conversation(cvault: Path) -> None:
    assert _activate(cvault, CARRY_TURN)["generation"]["carried_by"] == "retrieval"
