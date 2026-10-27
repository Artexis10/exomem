"""Clock components alone must not name a note about unrelated measurements."""

from pathlib import Path

import pytest
from test_working_set_carry import seed_ordinary_notes
from test_working_set_index import _seed_structure, _write

from exomem import commands, lexstore, working_set_index, working_set_runtime

# This module tests the `anchors` packet, the rollback mode; `maps` is the shipped default.
pytestmark = pytest.mark.usefixtures("anchors_packet")


def test_clock_digits_do_not_carry_an_unrelated_measurement_note(vault: Path) -> None:
    _seed_structure(vault)
    seed_ordinary_notes(vault)
    target = "Knowledge Base/Notes/Fastener measurements.md"
    _write(
        vault / target,
        "---\ntype: note\nstatus: active\n---\n# Fastener measurements\n\n"
        "- [fact] The spare fastener has a 17 mm head and a 43 mm shaft. ^size\n",
    )
    lexstore.ensure_fresh(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    working_set_runtime.reset_caches_for_tests()

    packet = commands.op_activate_context(vault, turn="should I set off at 17:43 tomorrow?")

    assert target not in {item["path"] for item in packet["anchors"]}, packet
    assert not any(item["provenance"]["path"] == target for item in packet["units"]), packet


def test_a_complete_numeric_title_is_carried_beside_another_named_domain(vault: Path) -> None:
    """A text domain must not suppress a separately named numeric title."""
    _seed_structure(vault)
    seed_ordinary_notes(vault)
    numeric = "Knowledge Base/Notes/Numeric schedule.md"
    other = "Knowledge Base/Notes/Quorra vantry.md"
    _write(
        vault / numeric,
        "---\ntype: note\nstatus: active\n---\n# 17 43\n\n"
        "- [fact] The next session lasts two days. ^duration\n",
    )
    _write(
        vault / other,
        "---\ntype: note\nstatus: active\n---\n# Quorra vantry\n\n"
        "- [fact] The quorra vantry review lasts nine days. ^duration\n",
    )
    lexstore.ensure_fresh(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    working_set_runtime.reset_caches_for_tests()

    packet = commands.op_activate_context(vault, turn="17 43. Quorra vantry.")

    assert {numeric, other} <= {item["provenance"]["path"] for item in packet["units"]}, packet


def test_a_letter_pair_does_not_license_another_pages_numeric_body_match(vault: Path) -> None:
    """Sharing a numeric stem does not give every candidate letter-bearing support."""
    _seed_structure(vault)
    seed_ordinary_notes(vault)
    target = "Knowledge Base/Notes/Fastener measurements.md"
    other = "Knowledge Base/Notes/Quorra vantry.md"
    _write(
        vault / target,
        "---\ntype: note\nstatus: active\n---\n# Fastener measurements\n\n"
        "- [fact] The spare fastener has a 17 mm head and a 43 mm shaft. ^size\n",
    )
    _write(
        vault / other,
        "---\ntype: note\nstatus: active\n---\n# Quorra vantry\n\n"
        "- [fact] The quorra vantry review lasts nine days. ^duration\n",
    )
    lexstore.ensure_fresh(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    working_set_runtime.reset_caches_for_tests()

    packet = commands.op_activate_context(vault, turn="17 43 quorra vantry")

    paths = {item["provenance"]["path"] for item in packet["units"]}
    assert other in paths, packet
    assert target not in paths, packet
    assert target not in {item["path"] for item in packet["anchors"]}, packet


@pytest.mark.parametrize("common_number", [False, True])
def test_numeric_title_fragments_cannot_carry_a_longer_title(
    vault: Path, common_number: bool,
) -> None:
    """Neither two rare numbers nor one rare number names a longer title."""
    _seed_structure(vault)
    seed_ordinary_notes(vault)
    target = "Knowledge Base/Notes/Fastener sizes.md"
    _write(
        vault / target,
        "---\ntype: note\nstatus: active\n---\n# Fastener 17 43 dimensions\n\n"
        "- [fact] The spare fastener fits the smaller bracket. ^fit\n",
    )
    if common_number:
        for index in range(20):
            _write(
                vault / f"Knowledge Base/Notes/Measurement record {index}.md",
                f"# Measurement record {index}\n\nThe common measurement is 17 mm.\n",
            )
    lexstore.ensure_fresh(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    working_set_runtime.reset_caches_for_tests()

    packet = commands.op_activate_context(vault, turn="17 43")

    # The existing title menu may suggest the page; it must not load its memory.
    assert packet["abstained"] is True, packet
    assert packet["abstention"]["reason"] == "unresolved", packet
    assert packet["units"] == [], packet
    assert packet["pointers"] == [], packet
