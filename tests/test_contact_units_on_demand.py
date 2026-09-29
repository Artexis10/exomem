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

from exomem import commands, context_roles
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


def test_the_shipped_contact_role_is_cue_only() -> None:
    role = context_roles.load_roles().roles["contact"]
    assert role.anchor_defaults == frozenset()
    assert role.categories == frozenset({"contact"})
    assert role.lane == "units"


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
