"""The conversation carry and the precedence ladder (tasks 3.1, 3.2).

An anaphoric turn of any length that reached nothing by its own words is
carried from the newest earlier USER turn that named a subject: a single
`partial` anchor, `generation.carried_by = "conversation"`. It sits fifth on
the ladder: after the anchor override, resolution, referential recency and the
shipped follow-up carry (each unchanged), before the retrieval carry.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conversation_vault import seed
from anaphor_round6_sets import FIFTH_NEGATIVES, FIFTH_POSITIVES, earlier as repeated_earlier
from anaphor_round7_sets import (
    CODE_TURNS, CONTENT_FREE_FIFTH_IDS, CONTENT_FREE_VARIANTS,
    RESIDUAL_TOPIC_SWITCH_IDS, STEM_COLLISION_WORDS, TOPIC_SWITCH_VARIANTS,
)
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
        "which one is cheaper",
        "the other one, please",
        "that one looks better",
        "the second one",
        "and what about the next quarter",
        "continue",
        # Contractions are the pronoun they carry.
        "it's still on track for the autumn?",
        "that's what I meant",
        "I think that's the wrong page",
        "they're late again, aren't they?",
        "is it still on track to finish by June?",
        "can we move it to next week?",
        "is it worth it?",
        "does it need sign-off?",
        "I think that is what she meant",
        "can you check whether it still works",
    ],
)
def test_an_anaphoric_turn_is_recognised(turn: str) -> None:
    # Round 7 surface-only neutral matching discloses these recall losses.
    expected = turn not in {"the second option looks better to me", "that one looks better"}
    assert analyze(turn).anaphoric is expected, turn


#: Ordinary, pronoun-bearing turns that point back at nothing (ruling C1 on
#: #1463): expletive "it", complementiser and relative "that", temporal
#: deixis, a same-turn antecedent and a closing acknowledgement.
PRONOUN_BEARING_NEGATIVES = (
    "Is it possible to install Python 3.13 on my laptop?",
    "It is worth checking the tyre pressure before a long drive.",
    "It looks like rain later, should I bring an umbrella?",
    "It's raining again, what should I cook for dinner?",
    "What time is it in Tokyo right now?",
    "It seems that the library closes early on Sundays.",
    "Would it be okay to swap the rice for quinoa in the recipe?",
    "It turns out the bakery on the corner does gluten-free bread.",
    "It takes forty minutes to walk to the station.",
    "It's hard to say which laptop is better for music.",
    "It's time to book the summer holiday.",
    "Let's switch to the grocery list, can you make it shorter?",
    "I bought a new kettle yesterday and it already leaks.",
    "I think that we should buy groceries on the way home.",
    "My sister said that the museum is free on Sundays.",
    "I know that tomatoes are technically a fruit.",
    "Do you believe that people can learn a language in three months?",
    "I hope that the weather holds for the picnic.",
    "I'm sure that the train leaves at nine.",
    "I read that coffee is fine in moderation.",
    "The recipe that my aunt sent needs two eggs.",
    "The other day I went hiking in the hills.",
    "What should I cook this week for dinner?",
    "We need to plan this week's rota for the kitchen.",
    "These days I mostly read on the train.",
    "Next week I want to try a new running route.",
    "I have a dentist appointment this afternoon, any tips for the nerves?",
    "On the other hand, it rains a lot in Galway.",
    "Thanks, that's all for today.",
)


@pytest.mark.parametrize("turn", PRONOUN_BEARING_NEGATIVES)
def test_a_pronoun_that_points_back_at_nothing_is_not_an_anaphor(turn: str) -> None:
    assert not analyze(turn).anaphoric, turn


def test_the_negative_set_is_at_least_twenty_pronoun_bearing_turns() -> None:
    pronoun = re.compile(r"\b(it|it's|its|that|that's|this|these|next|other)\b", re.IGNORECASE)
    assert len(PRONOUN_BEARING_NEGATIVES) >= 20
    assert all(pronoun.search(turn) for turn in PRONOUN_BEARING_NEGATIVES)


@pytest.mark.parametrize(
    "turn",
    [
        "how is the weather looking for Sunday afternoon",
        "tell me about the quarterly review schedule",
        "Cheers, there is plenty of chat for one day.",
        "one of the plans is late",
        "no one came to the review",
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


@pytest.mark.parametrize(
    "turn",
    [
        "Let's switch to the grocery list, can you make it shorter?",
        "I think that we should run the unit tests now",
        "is it possible to install python 3.13",
        "the other day I went hiking",
        "Thanks, that's all for today.",
    ],
)
def test_an_ordinary_turn_is_never_carried_from_the_conversation(cvault: Path, turn: str) -> None:
    packet = _activate(cvault, turn, conversation=THREAD)
    assert packet["generation"].get("carried_by") != "conversation", turn
    assert "Ottilie Marsh" not in _titles(packet), turn


@pytest.mark.parametrize("turn", ["it's still on track for the autumn?", "that's what I meant"])
def test_a_contracted_anaphor_is_carried(cvault: Path, turn: str) -> None:
    packet = _activate(cvault, turn, conversation=THREAD)
    assert packet["generation"].get("carried_by") == "conversation", turn
    assert _titles(packet) == ["Ottilie Marsh"]


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


@pytest.fixture
def fifth_disclosures_vault(cvault: Path) -> Path:
    from conversation_vault import _notes, _write

    titles = {title for _case, _turn, title in (*FIFTH_NEGATIVES, *FIFTH_POSITIVES)}
    for title in titles:
        _write(
            cvault / f"Knowledge Base/Entities/Projects/{title}.md",
            f"---\ntype: entity\nentity_type: project\nstatus: active\n"
            f"updated: 2026-09-01\n---\n\n# {title}\n\n## Summary\n\n"
            f"{title} is an invented subject for carry regressions.\n{_notes(title)}",
        )
    working_set_index.WorkingSetIndex(cvault).rebuild()
    _reset_caches()
    lexstore.ensure_fresh(cvault)
    working_set_runtime.reset_caches_for_tests()
    return cvault


def _carry_cost(packet: dict) -> int:
    """All served subject prose, including overflow pointers and current state."""
    return (sum(len(u["text"]) for u in packet["units"])
            + sum(len(p["title"]) + len(p["why"]) for p in packet["pointers"])
            + sum(len(s["statement"]) for s in packet["current_state"]))


def _assert_bounded_conversation(packet: dict, title: str, limit: int) -> None:
    assert packet["generation"].get("carried_by") == "conversation"
    assert _titles(packet) == [title]
    assert packet["anchors"][0]["status"] == "partial"
    assert packet["anchors"][0]["evidence"] == ["conversation"]
    assert packet["units"] or packet["pointers"]
    assert _carry_cost(packet) <= limit // 3
    assert packet["budget"]["limit_chars"] == limit


def test_the_compiler_classifies_all_disclosed_fifth_cases(fifth_disclosures_vault: Path) -> None:
    assert len(CONTENT_FREE_FIFTH_IDS | RESIDUAL_TOPIC_SWITCH_IDS) == 20
    for case_id, turn, title in FIFTH_NEGATIVES:
        packet = _activate(fifth_disclosures_vault, turn, max_chars=1200,
                           conversation={"recent": repeated_earlier(title, turn)},
                           session=f"round7-negative-{case_id}")
        # Round 8 local quotations supersede N50/N52/N53's task-only labels.
        if case_id in CONTENT_FREE_FIFTH_IDS - {"N50", "N52", "N53"}:
            _assert_bounded_conversation(packet, title, 1200)
        else:
            assert packet["generation"].get("carried_by") != "conversation", case_id
            assert title not in _titles(packet), case_id


@pytest.mark.parametrize("limit", [500, 600, 700])
def test_round7_conversation_footprint_preserves_the_ordinary_budget(cvault: Path, limit: int) -> None:
    # A long lede and eight facts: partial anchors read the lede lane, while
    # own-word resolution can also serve their ordinary units neighbourhood.
    page = cvault / "Knowledge Base/Entities/People/Ottilie Marsh.md"
    page.write_text(page.read_text().replace(
        "Runs the spring survey rounds for the estuary team.",
        "Ottilie Marsh coordinates the estuary team. "
        + "The estuary survey records distinct observations for the next review. " * 8,
    ))
    working_set_index.WorkingSetIndex(cvault).rebuild()
    lexstore.ensure_fresh(cvault)
    working_set_runtime.reset_caches_for_tests()
    carried = _activate(cvault, "which one should we review", max_chars=limit, conversation=THREAD)
    _assert_bounded_conversation(carried, "Ottilie Marsh", limit)
    named = _activate(cvault, "which one should we review for Ottilie Marsh", max_chars=limit)
    assert named["generation"].get("carried_by") != "conversation"
    assert _carry_cost(named) > limit // 3
    assert named["budget"]["limit_chars"] == limit


def test_round7_the_rest_of_the_budget_keeps_unresolved_recent_context(cvault: Path, monkeypatch) -> None:
    from exomem import working_set

    recent = ({"title": "Recent work", "statement": "A recent detail. " * 20},)
    monkeypatch.setattr(working_set, "_recent_context", lambda *args, **kwargs: recent)
    unresolved = _activate(cvault, "can you review that", max_chars=1000)
    carried = _activate(cvault, "can you review that", max_chars=1000, conversation=THREAD)
    _assert_bounded_conversation(carried, "Ottilie Marsh", 1000)
    assert carried["recent_context"] == unresolved["recent_context"] == list(recent)
    assert carried["budget"]["used_chars"] > 1000 // 3
    assert carried["budget"]["used_chars"] <= 1000


@pytest.mark.parametrize("turn", CONTENT_FREE_VARIANTS)
def test_round7_full_compiler_content_free_variants_use_the_footprint(cvault: Path, turn: str) -> None:
    packet = _activate(cvault, turn, max_chars=1000, conversation=THREAD)
    _assert_bounded_conversation(packet, "Ottilie Marsh", 1000)


@pytest.mark.parametrize("turn", TOPIC_SWITCH_VARIANTS + CODE_TURNS + tuple(
    f"i got a new {word}; can you review it" for word in STEM_COLLISION_WORDS))
def test_round7_full_compiler_topic_switches_do_not_carry(cvault: Path, turn: str) -> None:
    packet = _activate(cvault, turn, max_chars=1000, conversation=THREAD)
    assert packet["generation"].get("carried_by") != "conversation"
    assert "Ottilie Marsh" not in _titles(packet)


def test_the_compiler_restores_temporal_positives_and_excludes_drop_it(fifth_disclosures_vault: Path) -> None:
    failures = []
    for case_id, turn, title in FIFTH_POSITIVES:
        packet = _activate(fifth_disclosures_vault, turn,
                           conversation={"recent": repeated_earlier(title, turn)},
                           session=f"round6-positive-{case_id}")
        if turn == "drop it":
            if packet["generation"].get("carried_by") == "conversation":
                failures.append((case_id, turn, "closing carried"))
        elif (packet["generation"].get("carried_by") != "conversation"
              or _titles(packet) != [title] or not packet["units"]):
            failures.append((case_id, turn, packet["abstention"]))
    assert failures == []


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


#: The earlier user turn repeats the retrieval words, but they are not words
#: of the subject's own name. Shared vocabulary cannot license a carry.
WINDOW_THREAD = {
    "recent": [
        _user("How did Ottilie Marsh do in the spring, and what about the quillon vantry window?"),
        _assistant("Fine, all things told."),
    ]
}


def test_shared_turn_words_do_not_license_the_conversation_carry(cvault: Path) -> None:
    turn = f"{CARRY_TURN} and what do we think of that"
    baseline = _activate(cvault, turn)
    assert baseline["generation"].get("carried_by") == "retrieval", "the fixture must admit a recall carry"
    packet = _activate(cvault, turn, conversation=WINDOW_THREAD)
    assert packet["generation"]["carried_by"] == "retrieval"
    assert "Ottilie Marsh" not in _titles(packet)
    assert CARRY_PAGE in [unit["provenance"]["path"] for unit in packet["units"]]


def test_a_turn_with_new_content_words_is_a_topic_switch_and_never_carried(cvault: Path) -> None:
    """The same turn after a thread that never spoke its words: they are new
    content, so the conversation carry stands aside and recall decides."""
    turn = f"{CARRY_TURN} and what do we think of that"
    packet = _activate(cvault, turn, conversation=THREAD)
    assert packet["generation"]["carried_by"] == "retrieval"
    assert "Ottilie Marsh" not in _titles(packet)


def test_unlicensed_content_does_not_enter_the_ambiguous_carry(cvault: Path) -> None:
    turn = f"{CARRY_TURN} and what do we think of that"
    packet = _activate(
        cvault,
        turn,
        conversation={
            "recent": [
                _user(
                    "the Kestrel Hiring Plan and the Marlow Quay Survey both slipped past the "
                    "quillon vantry window"
                )
            ]
        },
    )
    assert packet["generation"]["carried_by"] == "retrieval"
    assert not {"Kestrel Hiring Plan", "Marlow Quay Survey"} & set(_titles(packet))


def test_a_conversation_that_names_nothing_falls_through_to_the_retrieval_carry(cvault: Path) -> None:
    turn = f"{CARRY_TURN} and what do we think of that"
    packet = _activate(cvault, turn, conversation={"recent": [_user("the harbour was quiet today")]})
    assert packet["generation"]["carried_by"] == "retrieval"


def test_every_existing_carry_is_untouched_without_a_conversation(cvault: Path) -> None:
    assert _activate(cvault, CARRY_TURN)["generation"]["carried_by"] == "retrieval"


# --------------------------------------------------------------------------- #
# The trigger's precision and recall (ruling C1 on #1463, round 3)
# --------------------------------------------------------------------------- #

#: False carries inject the wrong subject. Recall is reported, not gated.
MAX_FALSE_POSITIVE_RATE = 0.05


def _carried(turns, earlier, *, subject_title: str = "") -> list[str]:
    """The licence seam; callers supply the selected subject's actual title.
    Earlier text is retained for identical before/after case inputs but is
    deliberately not passed as licensing evidence."""
    from exomem import working_set_conversation

    return [turn for turn in turns if working_set_conversation.may_carry(
        analyze(turn), subject_title=subject_title,
    )]


@pytest.mark.parametrize("module", ["anaphor_heldout_sets", "anaphor_acceptance_sets"])
def test_the_trigger_meets_the_bar_on_the_held_out_and_acceptance_sets(module: str) -> None:
    import importlib

    sets = importlib.import_module(module)
    title = "Marlow Quay Survey" if module == "anaphor_acceptance_sets" else "Harbour Lantern Budget"
    false_positives = _carried(sets.NEGATIVES, sets.EARLIER, subject_title=title)
    carried = _carried(sets.POSITIVES, sets.EARLIER, subject_title=title)
    assert len(false_positives) <= MAX_FALSE_POSITIVE_RATE * len(sets.NEGATIVES), false_positives
    print(f"{module}: FP {len(false_positives)}/{len(sets.NEGATIVES)}; recall {len(carried)}/{len(sets.POSITIVES)}")


def test_a_new_content_word_is_a_topic_switch_even_after_a_pointing_word() -> None:
    earlier = ({"role": "user", "text": "we need to plan the spring rounds of the marlow quay survey"},)
    for turn in ("does it snow much in oslo in march", "this is a new topic: how do i descale a kettle",
                 "is it normal for a sourdough starter to smell like vinegar", "could it be that the router needs a reboot",
                 "it's been a long day"):
        assert _carried([turn], earlier) == [], turn
    assert _carried(["is that still on track for the rounds?"], earlier) == ["is that still on track for the rounds?"]
