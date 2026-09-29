"""How conversation evidence enters resolution (tasks 2.4 to 2.8).

The current turn always outranks the conversation. `focus` is the agent's
statement of the current turn (worded kinds only, labelled by origin);
`recent` and `refs` yield one subordinate qualifier, `conversation`, with three
effects: promotion, a tie-break and (later slice) a carry.
"""

from __future__ import annotations

import importlib.abc
import json
import sys
from pathlib import Path

import pytest
from conversation_vault import seed

from exomem import commands, lexstore, working_set_index, working_set_resolve, working_set_runtime

TURN = "Ottilie asked about the grant"
BOTH_PROJECTS = "what about the Kestrel Hiring Plan and the Marlow Quay Survey"


@pytest.fixture
def cvault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    seed(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return vault


def _user(text: str) -> dict:
    return {"role": "user", "text": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "text": text}


def _by_title(packet: dict) -> dict[str, dict]:
    return {anchor["title"]: anchor for anchor in packet["anchors"]}


def _stripped(packet: dict) -> dict:
    """A packet with the parts that vary per call, and the conversation state, removed."""
    body = json.loads(json.dumps(packet))
    body.pop("continuity", None)
    body.pop("timings", None)
    body["generation"].pop("conversation", None)
    return body


# --------------------------------------------------------------------------- #
# The evidence rule (unit level)
# --------------------------------------------------------------------------- #

status_for = working_set_resolve._status_for_evidence


def test_conversation_is_a_registered_evidence_kind() -> None:
    assert "conversation" in working_set_resolve.EVIDENCE_KINDS


def test_conversation_never_resolves_alone_or_with_qualifiers_only() -> None:
    assert status_for(frozenset({"conversation"})) == "partial"
    assert status_for(frozenset({"conversation", "category_match", "usage_prior"})) == "partial"
    assert status_for(frozenset({"conversation", "continuity"})) == "partial"


def test_conversation_with_one_contact_kind_resolves_like_continuity() -> None:
    assert status_for(frozenset({"rare_term", "conversation"})) == "resolved"
    assert status_for(frozenset({"lexical_overlap", "conversation"})) == "resolved"
    assert status_for(frozenset({"rare_term", "conversation", "continuity"})) == "resolved"
    assert status_for(frozenset({"rare_term"})) == "partial"


# --------------------------------------------------------------------------- #
# focus: a second segment of the current turn (2.4)
# --------------------------------------------------------------------------- #


def test_focus_supplies_a_second_domain_labelled_by_origin(cvault: Path) -> None:
    packet = commands.op_activate_context(
        cvault, turn="Ottilie asked what we do next", conversation={"focus": "the Tidewater Grant"}
    )
    anchors = _by_title(packet)
    assert anchors["Ottilie Marsh"]["origin"] == "turn"
    grant = anchors["Tidewater Grant"]
    assert grant["status"] == "resolved" and grant["origin"] == "focus"
    assert set(grant["evidence"]) <= {"exact_alias", "lexical_overlap", "claims_match"}
    assert "agent_choice" not in grant["evidence"]


def test_a_name_in_both_segments_is_turn_and_focus(cvault: Path) -> None:
    packet = commands.op_activate_context(
        cvault, turn="Ottilie asked about the Tidewater Grant", conversation={"focus": "the Tidewater Grant"}
    )
    assert _by_title(packet)["Tidewater Grant"]["origin"] == "turn_and_focus"


def test_focus_runs_no_recall_and_no_embedding(cvault: Path) -> None:
    packet = commands.op_activate_context(
        cvault, turn="thoughts on this?", conversation={"focus": "the Tidewater Grant"}
    )
    for anchor in packet["anchors"]:
        assert not {"retrieval", "vector_band"} & set(anchor["evidence"])


def test_segments_never_pair(cvault: Path) -> None:
    packet = commands.op_activate_context(
        cvault, turn="we talked about harbor", conversation={"focus": "lantern"}
    )
    hub = _by_title(packet).get("Harbor Lantern Budget")
    assert hub is None or (hub["status"] != "resolved" and "exact_alias" not in hub["evidence"])


def _essence(packet: dict) -> dict:
    """What a caller acts on: the anchors, the material and the verdict."""
    return {
        "anchors": packet["anchors"],
        "units": [unit["ref"] for unit in packet["units"]],
        "abstention": packet.get("abstention"),
        "carried_by": packet["generation"].get("carried_by"),
    }


def test_a_referential_turn_stays_referential_when_focus_names_nothing(cvault: Path) -> None:
    first = commands.op_activate_context(cvault, turn="tell me about Ottilie Marsh")
    token = first["continuity"]
    plain = commands.op_activate_context(cvault, turn="continue", continuity=token)
    with_focus = commands.op_activate_context(
        cvault, turn="continue", continuity=token, conversation={"focus": "nothing in this vault"}
    )
    assert plain["anchors"], "the referential turn resolves from recency"
    assert _essence(with_focus) == _essence(plain)


def test_a_referential_turn_resolves_the_focus_anchor_on_its_own_evidence(cvault: Path) -> None:
    packet = commands.op_activate_context(
        cvault, turn="continue", conversation={"focus": "the Tidewater Grant"}
    )
    grant = _by_title(packet)["Tidewater Grant"]
    assert grant["status"] == "resolved" and grant["origin"] == "focus"


def test_focus_cannot_settle_the_turns_own_ambiguity(cvault: Path) -> None:
    packet = commands.op_activate_context(
        cvault, turn=BOTH_PROJECTS, conversation={"focus": "the Kestrel Hiring Plan"}
    )
    assert packet["abstained"] and packet["abstention"] == {"reason": "ambiguous"}
    origins = {item["title"]: item["origin"] for item in packet["ambiguity"]}
    assert origins == {"Kestrel Hiring Plan": "turn_and_focus", "Marlow Quay Survey": "turn"}
    assert "disambiguated_by" not in packet["generation"]


# --------------------------------------------------------------------------- #
# Attachment-derived cues travel in focus (2.5)
# --------------------------------------------------------------------------- #


class _MediaImportGuard(importlib.abc.MetaPathFinder):
    NAMES = (
        "exomem.clip_index",
        "exomem.image_tags",
        "exomem.media_processing",
        "exomem.media_worker",
        "exomem.media_worker_child",
        "exomem.media_jobs",
    )

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.NAMES:
            self.attempts.append(fullname)
        return None


def test_cue_only_focus_resolves_a_content_free_turn_and_opens_no_media_module(
    cvault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cue = {"focus": "Ottilie Marsh and Tidewater Grant"}
    assert commands.op_activate_context(cvault, turn="thoughts on this?")["abstention"] == {
        "reason": "unresolved"
    }
    guard = _MediaImportGuard()
    for name in guard.NAMES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [guard, *sys.meta_path])
    packet = commands.op_activate_context(cvault, turn="thoughts on this?", conversation=cue)
    assert guard.attempts == []
    anchors = _by_title(packet)
    assert {a["origin"] for a in anchors.values()} == {"focus"}
    assert anchors["Ottilie Marsh"]["status"] == "resolved"
    assert anchors["Tidewater Grant"]["status"] == "resolved"


def test_a_cue_that_names_nothing_leaves_the_packet_unchanged(cvault: Path) -> None:
    plain = commands.op_activate_context(cvault, turn="thoughts on this?")
    cue = commands.op_activate_context(
        cvault, turn="thoughts on this?", conversation={"focus": "a red bicycle and two gulls"}
    )
    assert _essence(cue) == _essence(plain)
    assert cue["generation"]["conversation"] == "applied"


# --------------------------------------------------------------------------- #
# The conversation qualifier (2.6)
# --------------------------------------------------------------------------- #


def test_the_conversation_promotes_a_partial_second_domain(cvault: Path) -> None:
    plain = commands.op_activate_context(cvault, turn=TURN)
    assert _by_title(plain)["Tidewater Grant"]["status"] == "partial"
    packet = commands.op_activate_context(
        cvault, turn=TURN, conversation={"recent": [_user("we finished the Tidewater Grant call")]}
    )
    anchors = _by_title(packet)
    grant = anchors["Tidewater Grant"]
    assert grant["status"] == "resolved"
    assert sorted(grant["evidence"]) == ["conversation", "rare_term"]
    assert grant["origin"] == "turn", "a qualifier never changes an anchor's origin"
    assert anchors["Ottilie Marsh"]["status"] == "resolved"


def test_a_visible_ref_is_a_conversation_qualifier(cvault: Path) -> None:
    packet = commands.op_activate_context(
        cvault,
        turn=TURN,
        conversation={"refs": ["Knowledge Base/Notes/Insights/tidewater-grant-hub.md"]},
    )
    assert _by_title(packet)["Tidewater Grant"]["status"] == "resolved"


def test_the_conversation_alone_resolves_nothing(cvault: Path) -> None:
    turn = "How is the weather looking on Sunday afternoon?"
    plain = commands.op_activate_context(cvault, turn=turn)
    packet = commands.op_activate_context(
        cvault,
        turn=turn,
        conversation={
            "recent": [_user("Ottilie Marsh, the Tidewater Grant and the Kestrel Hiring Plan all matter")],
            "refs": ["Knowledge Base/Notes/Insights/tidewater-grant-hub.md"],
        },
    )
    assert packet["abstention"] == plain["abstention"] == {"reason": "unresolved"}
    assert _essence(packet) == _essence(plain)


def test_words_split_across_entries_do_not_form_a_name(cvault: Path) -> None:
    packet = commands.op_activate_context(
        cvault,
        turn="Ottilie asked about the budget",
        conversation={"recent": [_user("we should ask about the Harbor"), _user("Lantern shipments arrived")]},
    )
    hub = _by_title(packet).get("Harbor Lantern Budget")
    assert hub is None or "conversation" not in hub["evidence"]


def test_only_the_newest_three_user_and_two_assistant_entries_are_read(cvault: Path) -> None:
    filler = "nothing to see here"
    oldest_user = commands.op_activate_context(
        cvault,
        turn=TURN,
        conversation={
            "recent": [_user("the Tidewater Grant"), _user(filler), _user(filler), _user(filler)]
        },
    )
    assert _by_title(oldest_user)["Tidewater Grant"]["status"] == "partial"
    third_newest_user = commands.op_activate_context(
        cvault,
        turn=TURN,
        conversation={"recent": [_user("the Tidewater Grant"), _user(filler), _user(filler)]},
    )
    assert _by_title(third_newest_user)["Tidewater Grant"]["status"] == "resolved"
    oldest_assistant = commands.op_activate_context(
        cvault,
        turn=TURN,
        conversation={
            "recent": [_assistant("the Tidewater Grant"), _assistant(filler), _assistant(filler)]
        },
    )
    assert _by_title(oldest_assistant)["Tidewater Grant"]["status"] == "partial"
    second_newest_assistant = commands.op_activate_context(
        cvault,
        turn=TURN,
        conversation={"recent": [_assistant("the Tidewater Grant"), _assistant(filler)]},
    )
    assert _by_title(second_newest_assistant)["Tidewater Grant"]["status"] == "resolved"


# --------------------------------------------------------------------------- #
# The tie-break (2.7)
# --------------------------------------------------------------------------- #


def test_exactly_one_conversation_bearing_competitor_settles_the_turn(cvault: Path) -> None:
    plain = commands.op_activate_context(cvault, turn=BOTH_PROJECTS)
    assert plain["abstention"] == {"reason": "ambiguous"}
    packet = commands.op_activate_context(
        cvault, turn=BOTH_PROJECTS, conversation={"recent": [_user("did the Kestrel Hiring Plan slip again?")]}
    )
    assert packet["abstained"] is False
    assert packet["generation"]["disambiguated_by"] == "conversation"
    anchors = _by_title(packet)
    assert anchors["Kestrel Hiring Plan"]["status"] == "resolved"
    assert anchors["Marlow Quay Survey"]["status"] == "partial"
    assert packet["ambiguity"] == []


def test_two_conversation_bearing_competitors_keep_the_ambiguity(cvault: Path) -> None:
    packet = commands.op_activate_context(
        cvault,
        turn=BOTH_PROJECTS,
        conversation={"recent": [_user("both the Kestrel Hiring Plan and the Marlow Quay Survey slipped")]},
    )
    assert packet["abstention"] == {"reason": "ambiguous"}
    assert "disambiguated_by" not in packet["generation"]
    assert {item["title"] for item in packet["ambiguity"]} == {"Kestrel Hiring Plan", "Marlow Quay Survey"}


def test_no_conversation_bearing_competitor_keeps_the_ambiguity(cvault: Path) -> None:
    packet = commands.op_activate_context(
        cvault, turn=BOTH_PROJECTS, conversation={"recent": [_user("the harbour was quiet today")]}
    )
    assert packet["abstention"] == {"reason": "ambiguous"}


# --------------------------------------------------------------------------- #
# The drowning guard (2.8)
# --------------------------------------------------------------------------- #


def test_a_long_conversation_about_one_subject_does_not_resolve_it_for_another(cvault: Path) -> None:
    about_the_budget = [
        _user("the Harbor Lantern Budget is over by a third"),
        _assistant("The Harbor Lantern Budget overrun comes from the glass."),
        _user("can the Harbor Lantern Budget absorb it?"),
        _assistant("Only if the Harbor Lantern Budget reserve is used."),
        _user("then the Harbor Lantern Budget needs a new review"),
        _assistant("Agreed, the Harbor Lantern Budget review should come first."),
    ]
    packet = commands.op_activate_context(
        cvault,
        turn="Now the Kestrel Hiring Plan: who signs off the offers?",
        conversation={"recent": about_the_budget, "refs": ["Knowledge Base/Notes/Insights/harbor-lantern-budget-hub.md"]},
    )
    assert set(_by_title(packet)) == {"Kestrel Hiring Plan"}
    assert "Harbor Lantern" not in json.dumps([u["text"] for u in packet["units"]])
    assert not any("harbor-lantern" in unit["ref"] for unit in packet["units"])


def test_promoted_material_keeps_within_a_third_of_the_budget_and_comes_last(cvault: Path) -> None:
    limit = 1500
    packet = commands.op_activate_context(
        cvault,
        turn=TURN,
        max_chars=limit,
        conversation={"recent": [_user("we finished the Tidewater Grant call")]},
    )
    anchors = _by_title(packet)
    ottilie, grant = anchors["Ottilie Marsh"]["path"], anchors["Tidewater Grant"]["path"]
    assert anchors["Tidewater Grant"]["status"] == "resolved"
    paths = [unit["provenance"]["path"] for unit in packet["units"]]
    assert ottilie in paths and grant in paths
    assert paths.index(ottilie) < paths.index(grant)
    promoted = sum(len(unit["text"]) for unit in packet["units"] if unit["provenance"]["path"] == grant)
    assert promoted <= limit // 3
