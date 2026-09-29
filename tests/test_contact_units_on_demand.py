"""Contact details live on the person's entity page, in their own `contact`
units, and activation serves them only when the turn asks for them.

`contact` is an open category that no role selects by default: the shipped
`contact` role has no anchor defaults, so an entity anchor never pulls its
contact units in; only a contact cue on the turn selects it. Withheld = absent
is unchanged, since the units are read only through the visible page.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_activate_context_records_bounded_work import _warm
from test_governance_egress import _external, write_rule, write_scope

from exomem import commands, context_intents, context_roles, working_set, working_set_resolve
from exomem import find as find_module
from exomem.governance import egress, membership, policy
from exomem.governance.principal import request_scope

PAGE = "Knowledge Base/Entities/People/Maren Holt.md"


def _person(vault: Path) -> None:
    page = vault / PAGE
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: entity\nentity_type: person\nstatus: active\n"
        "created: 2026-09-01\nupdated: 2026-09-01\naffiliation: loom cooperative\n---\n\n"
        "# Maren Holt\n\n## Summary\n\nMaren Holt teaches the weaving class.\n\n"
        "## Observations\n\n- [fact] Maren Holt runs the Tuesday weaving class ^class\n\n"
        "## Contact\n\n"
        "- [contact] Maren Holt phone is 555-0142 ^phone\n"
        "- [contact] Maren Holt mailing address is 12 Example Lane ^address\n",
        encoding="utf-8",
    )


def _unit_texts(packet: dict) -> list[str]:
    return [unit["text"] for unit in packet.get("units", [])]


def test_the_shipped_contact_role_is_intent_only() -> None:
    role = context_roles.load_roles().roles["contact"]
    assert role.anchor_defaults == frozenset()
    assert role.categories == frozenset({"contact"})
    assert role.lane == "units"
    assert role.intent == "contact" and role.cues == ()


NAMES = context_intents.anchor_terms(["Maren Holt"])

#: Turns that must NOT ask for contact units: the review's probe rows and the
#: raw-substring controls ("call", "address", "phone", "email", "number", "contact").
NO_CONTACT_INTENT = (
    "What is Maren working on this week?",
    "Maren says we should call it done",
    "Maren, please address this issue",
    "Maren lost her headphones",
    "Maren sent the email about the class",
    "call it done",
    "address this issue",
    "phonetic transcription",
    "the email thread about the budget",
    "numbered list",
    "contact lens",
    "Maren wears a contact lens",
    "her contact lenses are new",
    "that's a good address for a speech",
)

#: Turns that must: a contact noun tied to the person, or a reach verb aimed at them.
CONTACT_INTENT = (
    "What is Maren's phone?",
    "What's Maren's phone number?",
    "email Maren about the class",
    "how do I reach Maren",
    "Maren's address",
    "what is her email address",
    "text them the schedule",
    "call her tomorrow",
    "I need their contact details",
    "what is the mailing address for Maren",
)


def _intent(turn: str, names=NAMES) -> bool:
    return context_intents.contact_intent(working_set_resolve.analyze_turn(turn).tokens, names)


@pytest.mark.parametrize("turn", NO_CONTACT_INTENT)
def test_a_turn_without_contact_intent_does_not_select_the_contact_role(turn: str) -> None:
    assert _intent(turn) is False
    selected = context_roles.select_roles(
        context_roles.load_roles(),
        anchor_kinds=("entity",),
        analysis=working_set_resolve.analyze_turn(turn),
        anchor_names=NAMES,
    )
    assert "contact" not in {role["id"] for role in selected}


@pytest.mark.parametrize("turn", CONTACT_INTENT)
def test_a_turn_with_contact_intent_selects_the_contact_role_by_cue(turn: str) -> None:
    assert _intent(turn) is True
    selected = context_roles.select_roles(
        context_roles.load_roles(),
        anchor_kinds=("entity",),
        analysis=working_set_resolve.analyze_turn(turn),
        anchor_names=NAMES,
    )
    assert {"id": "contact", "source": "turn_cue", "lane": "units"} in selected


def test_a_carried_page_serves_contact_only_when_the_intent_is_present() -> None:
    registry = context_roles.load_roles()
    quiet = working_set_resolve.analyze_turn("What is Maren working on this week?")
    asking = working_set_resolve.analyze_turn("What is Maren's phone?")

    assert "contact" not in {r["id"] for r in working_set._carry_roles(registry, quiet, NAMES)}  # noqa: SLF001
    carried = working_set._carry_roles(registry, asking, NAMES)  # noqa: SLF001
    assert {"id": "contact", "source": "turn_cue", "lane": "units"} in carried


def test_an_override_cannot_set_or_change_a_roles_intent(vault: Path) -> None:
    path = context_roles.override_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema_version: 1\nroles:\n  contact:\n    intent: nothing\n", encoding="utf-8"
    )
    context_roles.clear_cache()
    registry = context_roles.load_roles(vault)

    assert registry.roles["contact"].intent == "contact"
    assert any(f["code"] == "unknown_field" for f in registry.findings)


def test_a_turn_about_the_person_without_contact_intent_gets_no_contact_units(
    vault: Path,
) -> None:
    _person(vault)
    _warm(vault)

    packet = commands.op_activate_context(vault, turn="What is Maren Holt working on this week?")

    assert packet["abstained"] is False, packet.get("abstention")
    texts = _unit_texts(packet)
    # The person is still served (their own lede); only the contact units are not.
    assert any("teaches the weaving class" in text for text in texts), texts
    assert not any("555-0142" in text or "Example Lane" in text for text in texts), texts
    assert "contact" not in {role["id"] for role in packet["roles"]}


@pytest.mark.parametrize(
    "turn",
    [
        "Maren Holt says we should call it done",
        "Maren Holt, please address this issue",
        "Maren Holt lost her headphones",
        "Maren Holt sent the email about the class",
    ],
)
def test_the_reviews_probe_turns_get_no_contact_units_end_to_end(vault: Path, turn: str) -> None:
    _person(vault)
    _warm(vault)

    packet = commands.op_activate_context(vault, turn=turn)

    assert "contact" not in {role["id"] for role in packet["roles"]}
    assert not any("555-0142" in t or "Example Lane" in t for t in _unit_texts(packet))


def test_a_turn_asking_for_their_phone_gets_the_contact_units(vault: Path) -> None:
    _person(vault)
    _warm(vault)

    packet = commands.op_activate_context(vault, turn="What is Maren Holt's phone number?")

    assert packet["abstained"] is False, packet.get("abstention")
    assert any(role["id"] == "contact" and role["source"] == "turn_cue" for role in packet["roles"])
    assert any("555-0142" in text for text in _unit_texts(packet)), _unit_texts(packet)


@pytest.fixture
def _clean_governance():
    for reset in (policy._CACHE.clear, membership.clear_memo, egress.clear_decision_memo):  # noqa: SLF001
        reset()
    find_module.clear_cache()
    yield
    for reset in (policy._CACHE.clear, membership.clear_memo, egress.clear_decision_memo):  # noqa: SLF001
        reset()


def test_a_withheld_person_page_takes_its_contact_units_with_it(
    vault: Path, _clean_governance
) -> None:
    """Withheld = absent: a restricted audience that cannot see the entity
    cannot see its Contact section, even with a contact cue on the turn."""
    _person(vault)
    write_scope(vault, paths="Entities/People/**", name="People")
    write_rule(vault, ceiling=0)
    _warm(vault)
    turn = "What is Maren Holt's phone number?"

    with request_scope(_external()):
        restricted = commands.op_activate_context(vault, turn=turn)

    blob = json.dumps(restricted)
    assert "555-0142" not in blob and "Example Lane" not in blob
    assert "Maren Holt" not in blob
