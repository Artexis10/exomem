"""U7 — `anchor` accepts any page a packet listed, not only an index anchor.

Design D3's retrieval carry already proved that an ordinary compiled page
(a research note, never an index anchor) can carry a packet: `_carried_packet`
and the units lane over it. This closes the other half — the agent naming
that SAME page itself with `anchor=` used to raise `INVALID_ANCHOR`, because
`override_candidates` only ever looked in the activation index's own rows.
It now falls back to the retrieval carry's own eligibility test
(`working_set._eligible_agent_page`) and reuses `_carried_packet` unchanged,
at the soundness rule's own "agent decides alone" outcome
(`status="resolved"`, `evidence=("agent_choice",)`) rather than invented
vocabulary.

The one refusal an unknown ref and a withheld one share now also covers raw
material, a navigation page, and a retired page named this way: none of
those is a second, distinguishable answer, because that would turn the
argument into an existence oracle.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_governance_egress import _external, _reset_caches, write_rule, write_scope
from test_working_set_carry import (
    CARRY_PAGE,
    CARRY_SOURCE,
    _seed_carry_pages,
    _seed_live_cell,
    _write,
)
from test_working_set_index import _seed_planning, _seed_structure

from exomem import commands, lexstore, working_set, working_set_index, working_set_runtime
from exomem.governance.principal import request_scope

#: A turn with nothing in the fixture catalogue's vocabulary at all. The
#: override branch never reads the turn's words to decide WHICH page —
#: `anchor` alone does that — so any turn this deliberately empty proves the
#: page was reached on the agent's choice and nothing else.
NONSENSE_TURN = "zqxwvu plonktastic frobnitz quibblewhomp"

#: A real anchor of `carry_vault`'s own catalogue (`_seed_structure`), for
#: the regression case: an `anchor` naming an index row must still behave
#: exactly as it did before this change.
REAL_ANCHOR = "Knowledge Base/Products/Cargo Sled.md"


@pytest.fixture
def carry_vault(vault: Path) -> Path:
    """A warm vault whose compiled pages include non-anchor research notes.

    Local copy of `test_working_set_carry.carry_vault` rather than a
    cross-file fixture import: importing a fixture function and also using
    its name as a test parameter reads to static analysis as redefinition
    (ruff F811), and no other module in this suite imports one across files.
    """
    _seed_structure(vault)
    _seed_planning(vault)
    _seed_carry_pages(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


def _anchor(packet: dict) -> dict:
    (only,) = packet["anchors"]
    return only


def test_an_agent_picked_research_note_returns_its_units_not_invalid_anchor(
    carry_vault: Path,
) -> None:
    """CARRY_PAGE is not an anchor of this index (`_seed_carry_pages`'s own
    docstring: "None is an anchor") — the exact ref an `unresolved` or
    `retrieval_named` packet would have listed for the agent to pick."""
    packet = commands.op_activate_context(
        carry_vault, turn=NONSENSE_TURN, anchor=CARRY_PAGE
    )

    assert packet["abstained"] is False, packet.get("abstention")
    anchor = _anchor(packet)
    assert anchor["ref"] == CARRY_PAGE
    assert anchor["kind"] == "page"
    assert anchor["status"] == "resolved"
    assert anchor["evidence"] == ["agent_choice"]
    assert packet["generation"]["carried_by"] == "agent_choice"
    assert packet["units"], "the page's own units must be served"
    assert all(
        unit["provenance"]["path"] == CARRY_PAGE for unit in packet["units"]
    ), packet["units"]


def test_a_real_anchor_override_is_unaffected(carry_vault: Path) -> None:
    """The half that must not change: an `anchor` naming an index row never
    reaches the new fallback (`resolution.status` is already `resolved`), so
    it keeps the ordinary role lanes and generation shape it always had."""
    packet = commands.op_activate_context(
        carry_vault, turn=NONSENSE_TURN, anchor=REAL_ANCHOR
    )

    assert packet["abstained"] is False, packet.get("abstention")
    anchor = _anchor(packet)
    assert anchor["ref"] == REAL_ANCHOR
    assert anchor["kind"] != "page"
    assert anchor["status"] == "resolved"
    assert anchor["evidence"] == ["agent_choice"]
    assert "carried_by" not in packet["generation"]


def test_the_withheld_ref_and_an_unknown_ref_receive_the_identical_refusal(
    carry_vault: Path,
) -> None:
    """The agent-pick fallback must cross the SAME egress guard a resolved
    override always did: a page this audience may not see is refused with
    the one word an unknown ref gets, never a second, distinguishable
    answer."""
    write_scope(carry_vault, paths="Knowledge Base/Notes/Research/*", name="Research")
    write_rule(carry_vault, ceiling=0)
    _reset_caches()
    working_set_runtime.reset_caches_for_tests()

    with pytest.raises(ValueError) as unknown:
        with request_scope(_external()):
            commands.op_activate_context(
                carry_vault,
                turn=NONSENSE_TURN,
                anchor="Knowledge Base/Notes/Research/no-such-page.md",
            )

    with pytest.raises(ValueError) as withheld:
        with request_scope(_external()):
            commands.op_activate_context(
                carry_vault, turn=NONSENSE_TURN, anchor=CARRY_PAGE
            )

    assert str(unknown.value) == str(withheld.value)
    assert "quillon" not in str(withheld.value).lower()
    assert "no-such-page" not in str(unknown.value)


@pytest.mark.parametrize(
    "make_ref",
    [
        pytest.param(lambda vault: CARRY_SOURCE, id="raw_material"),
        pytest.param(lambda vault: "Knowledge Base/index.md", id="navigation"),
    ],
)
def test_raw_material_and_navigation_refs_are_refused_like_unknown(
    carry_vault: Path, make_ref
) -> None:
    ref = make_ref(carry_vault)

    with pytest.raises(ValueError) as ineligible:
        commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=ref)

    with pytest.raises(ValueError) as unknown:
        commands.op_activate_context(
            carry_vault, turn=NONSENSE_TURN, anchor="Knowledge Base/Nowhere/absent.md"
        )

    assert str(ineligible.value) == str(unknown.value)


def test_a_retired_page_is_refused_like_unknown(carry_vault: Path) -> None:
    retired = "Knowledge Base/Notes/Research/quillon-vantry-window-old.md"
    _write(
        carry_vault / retired,
        """---
type: research-note
status: archived
updated: 2026-08-01
---

# Quillon vantry window (old)

## Summary

- [decision] The quillon vantry window used to be six minutes. ^q-old
""",
    )
    working_set_runtime.reset_caches_for_tests()

    with pytest.raises(ValueError) as retired_error:
        commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=retired)

    with pytest.raises(ValueError) as unknown:
        commands.op_activate_context(
            carry_vault, turn=NONSENSE_TURN, anchor="Knowledge Base/Nowhere/absent.md"
        )

    assert str(retired_error.value) == str(unknown.value)


def test_recent_context_still_leads_and_the_packet_still_fits_its_budget(
    carry_vault: Path,
) -> None:
    _seed_live_cell(carry_vault)

    packet = working_set.compile_packet(
        carry_vault, turn=NONSENSE_TURN, max_chars=4000, anchor=CARRY_PAGE
    )

    assert packet["abstained"] is False, packet.get("abstention")
    assert list(packet)[0] == "recent_context", list(packet)
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]
    assert packet["budget"]["used_chars"] <= 4000


# --------------------------------------------------------------------------- #
# The hook: the recent-context menu tells the agent which follow-up fits
# which entry, and the named-pages menu now offers the remedy that works.
# --------------------------------------------------------------------------- #


def test_a_captured_session_entry_is_labelled_for_read_memory() -> None:
    """A `Sources/Sessions` capture is raw material, so it stays a
    `read_memory` target even though the header's default follow-up for
    every other `recent_context` line is now `activate_context(anchor=...)`.
    """
    from exomem._hooks import exomem_retrieve_nudge as hook

    packet = {
        "recent_context": [
            {
                "ref": "Knowledge Base/Sources/Sessions/2026-09-20.md",
                "path": "Knowledge Base/Sources/Sessions/2026-09-20.md",
                "title": "2026-09-20",
                "kind": "page",
                "why": "captured",
                "as_of": "2026-09-20",
            }
        ],
        "anchors": [],
        "roles": [],
        "units": [],
        "pointers": [],
        "current_state": [],
        "ambiguity": [],
        "missing": [],
        "budget": {"limit_chars": 4000, "used_chars": 0},
        "generation": {},
        "abstained": False,
    }

    block = hook._format_working_set_block(packet, 4000)

    assert "- session:" in block, block
    assert "- recent:" not in block, block
    assert "read_memory" in hook._WORKING_SET_HEADER
    assert "activate_context" in hook._WORKING_SET_HEADER


def test_the_named_pages_menu_now_offers_the_anchor_remedy() -> None:
    """`retrieval_named` pages are exactly what `_eligible_agent_page` now
    admits, so the hook's own closing line for that menu must offer the
    remedy that actually works — `anchor=`, not `read_memory`, which is
    what a page not eligible this way still needs."""
    from exomem._hooks import exomem_retrieve_nudge as hook

    packet = {
        "recent_context": [],
        "anchors": [
            {
                "ref": "Knowledge Base/Notes/Research/a.md",
                "path": "Knowledge Base/Notes/Research/a.md",
                "title": "A",
                "kind": "page",
                "lifecycle": "active",
                "status": "retrieval_named",
                "evidence": ["retrieval"],
            },
            {
                "ref": "Knowledge Base/Notes/Research/b.md",
                "path": "Knowledge Base/Notes/Research/b.md",
                "title": "B",
                "kind": "page",
                "lifecycle": "active",
                "status": "retrieval_named",
                "evidence": ["retrieval"],
            },
        ],
        "roles": [],
        "units": [],
        "pointers": [],
        "current_state": [],
        "ambiguity": [],
        "missing": [],
        "budget": {"limit_chars": 4000, "used_chars": 0},
        "generation": {},
        "abstained": True,
        "abstention": {"reason": "unresolved"},
    }

    block = hook._format_working_set_block(packet, 4000)
    closing = block.splitlines()[-1]

    assert "anchor" in closing, closing
    assert "activate_context" in closing, closing
    assert "read_memory" not in closing, closing


def test_the_deployed_hook_mirror_stays_byte_identical() -> None:
    root = Path(__file__).resolve().parents[1]
    packaged = root / "src" / "exomem" / "_hooks" / "exomem_retrieve_nudge.py"
    deployed = root / "plugins" / "claude-code" / "hooks" / "exomem_retrieve_nudge.py"

    assert deployed.read_bytes() == packaged.read_bytes()
