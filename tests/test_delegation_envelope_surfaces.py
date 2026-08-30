"""The envelope on the surfaces a person and an agent actually read.

Two vocabularies meet in the dispositions view and they must never share a
column. A family `off` is a review-state decision about one KIND of signal; an
envelope `off` means the agent does not initiate one CLASS of action. Printed in
one list the word says two different things, and the reader has no way to tell
which — so the blocks are structurally separate and each says which it is.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _nag_governance_helpers import overdue_prediction, scratch_page

from exomem import commands, envelope, prominence

FAMILY = "prediction_window"
FAMILY_REF = f"exomem://review/family/{FAMILY}"
QUIET_WHY = "too_frequent: fires more than it helps in this vault"


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(path))
    monkeypatch.delenv("EXOMEM_PROMINENCE", raising=False)
    monkeypatch.delenv("EXOMEM_SURFACE", raising=False)
    monkeypatch.delenv("EXOMEM_HOSTED_CELL", raising=False)
    return path


# ------------------------------------------------------------------ 4.1 the view


def test_the_dispositions_view_carries_two_structurally_separate_blocks(
    config, vault: Path
) -> None:
    prominence.write_prominence("balanced")
    envelope.set_disposition("structural_suggestions", "off")
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=QUIET_WHY)

    view = commands.op_review_memory(vault, mode="dispositions")

    family_rows = view["dispositions"]
    envelope_classes = view["envelope"]["classes"]

    # The family block holds the family and only families.
    assert [row["family"] for row in family_rows] == [FAMILY]
    assert family_rows[0]["disposition"] == "quiet"
    assert family_rows[0]["reason"] == "too_frequent"
    assert not set(envelope.ACTION_CLASSES) & {row["family"] for row in family_rows}

    # The envelope block holds the classes and only classes.
    assert set(envelope_classes) == set(envelope.ACTION_CLASSES)
    assert FAMILY not in envelope_classes
    assert envelope_classes["structural_suggestions"] == {
        "ceiling": "advisory",
        "disposition": "off",
        "provenance": "override",
    }
    assert envelope_classes["disclosure"]["provenance"] == "governance-owned"
    assert envelope_classes["disclosure"]["disposition"] is None


def test_the_view_says_which_off_is_which(config, vault: Path) -> None:
    """`off` on its own is ambiguous across the two vocabularies."""
    note = commands.op_review_memory(vault, mode="dispositions")["envelope"]["note"].lower()

    assert "does not initiate" in note
    assert "family" in note


def test_the_envelope_block_is_present_even_when_no_family_is_quiet(
    config, vault: Path
) -> None:
    """The two blocks are independent; an empty family list is not an empty view."""
    view = commands.op_review_memory(vault, mode="dispositions")

    assert view["dispositions"] == []
    assert set(view["envelope"]["classes"]) == set(envelope.ACTION_CLASSES)
    assert view["envelope"]["level"] == prominence.resolve()


def test_a_family_decision_moves_no_envelope_class(config, vault: Path) -> None:
    prominence.write_prominence("balanced")
    overdue_prediction(vault)
    scratch_page(vault)
    before = commands.op_review_memory(vault, mode="dispositions")["envelope"]["classes"]

    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=QUIET_WHY)
    commands.op_triage_memory(vault, ref=FAMILY_REF, action="normal")

    after = commands.op_review_memory(vault, mode="dispositions")["envelope"]["classes"]
    assert after == before
