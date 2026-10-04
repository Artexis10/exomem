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
    _write,
)

from exomem import lexstore, working_set, working_set_index, working_set_runtime

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
    # One role slot remains for the note; a planning cue would fill the resolved schedule.
    turn = "the cargo sled given what the review concluded about the kelvane throughput ceiling"
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


@pytest.mark.parametrize("cued", [False, True])
def test_carried_pages_share_one_role_schedule(domain_vault: Path, cued: bool) -> None:
    # Catches page-local role caps whose union exceeds the request's six lenses.
    _write(domain_vault / GENUINE_PAGE,
        "---\ntype: research-note\nstatus: active\n---\n# Kelvane throughput review\n\n"
        "- [preference] Kelvane throughput prefers amber gauges. ^pref\n"
        "- [constraint] Kelvane throughput requires the lock. ^limit\n"
        "- [design] Kelvane throughput uses a shared queue. ^design\n")
    _write(domain_vault / CARRY_PAGE,
        "---\ntype: research-note\nstatus: active\n---\n# Quillon vantry window\n\n"
        "- [fact] Quillon vantry uses nine minutes. ^fact\n")
    lexstore.ensure_fresh(domain_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(domain_vault).rebuild()
    turn = "Kelvane throughput review. Quillon vantry window."
    if cued:
        turn += " How do I approach it?"

    packet = working_set.compile_packet(domain_vault, turn=turn, max_chars=8000)

    assert _paths(packet, "retrieval_carried") == {GENUINE_PAGE, CARRY_PAGE}
    expected = ["preferences", "constraints", "resources", "material", "methods", "location"]
    if cued:
        expected = ["methods", *(role for role in expected if role != "methods")]
    assert [role["id"] for role in packet["roles"]] == expected
    assert {"role": "baseline", "reason": "role_limit"} in packet["missing"]
    assert all(item["role"] in expected for item in packet["units"] + packet["pointers"])


@pytest.mark.parametrize("full_schedule", [False, True])
def test_beside_material_uses_remaining_roles_and_one_item_allowance(
    domain_vault: Path, full_schedule: bool,
) -> None:
    # Catches unlisted carried lanes bypassing both resolved-role priority and the material cap.
    for name in ("Pelvane Teravo", "Durnavo Salmex"):
        _write(domain_vault / f"Knowledge Base/Notes/{name}.md",
            f"---\ntype: note\nstatus: active\n---\n# {name}\n\n" + "\n".join(
                f"- [observation] Violet worksheet reading {i}. ^reading-{i}" for i in range(6)
            ))
    lexstore.ensure_fresh(domain_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(domain_vault).rebuild()
    turn = "Cargo Sled. Pelvane Teravo. Durnavo Salmex. Violet worksheet."
    if full_schedule:
        turn += " Should I send it?"

    packet = working_set.compile_packet(domain_vault, turn=turn, max_chars=8000)

    expected = ["identity", "constraints", "resources", "current_state"]
    expected += ["active_plans", "location"] if full_schedule else ["location", "material"]
    assert [role["id"] for role in packet["roles"]] == expected
    material = [item for item in packet["units"] + packet["pointers"] if item["role"] == "material"]
    if full_schedule:
        assert material == []
        assert {"role": "material", "reason": "role_limit"} in packet["missing"]
    else:
        assert len(material) == 3
        assert {"role": "material", "reason": "lane_truncated"} in packet["missing"]
    assert any("400 kg" in unit["text"] for unit in packet["units"])
    assert all(item["role"] in expected for item in packet["units"] + packet["pointers"])


def test_beside_prose_is_a_requires_read_pointer(domain_vault: Path) -> None:
    # Catches a carried page entering assembly without the metadata that keeps prose a pointer.
    path = "Knowledge Base/Notes/Pelvane Teravo.md"
    _write(domain_vault / path,
        "---\ntype: note\nstatus: active\n---\n# Pelvane Teravo\n\n"
        "The violet worksheet must be read before loading.\n")
    lexstore.ensure_fresh(domain_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(domain_vault).rebuild()

    packet = working_set.compile_packet(
        domain_vault, turn="Cargo Sled. Pelvane Teravo. Violet worksheet.", max_chars=8000,
    )

    assert "material" in [role["id"] for role in packet["roles"]]
    assert not any(unit["role"] == "material" for unit in packet["units"])
    assert [(pointer["ref"], pointer["reason"]) for pointer in packet["pointers"]
        if pointer["role"] == "material"] == [(path, "requires_read")]
