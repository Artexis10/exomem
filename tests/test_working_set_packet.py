"""Task 4.1 — bounded role lanes and the working-memory packet.

The packet is the product: a bounded, auditable, provenance-bearing answer to
"what should be in working memory for this turn". These tests pin its shape, its
budget arithmetic, the units-before-pages ordering, supersession marking, the
Records-first current-state rule, the abstained shape, and the graph depths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import working_set, working_set_index, working_set_resolve

PACKET_KEYS = {
    "anchors",
    "roles",
    "units",
    "pointers",
    "current_state",
    "missing",
    "ambiguity",
    "budget",
    "generation",
    "abstained",
}


def _item(
    role: str,
    *,
    level: str = "unit",
    ref: str = "",
    text: str = "x" * 40,
    lifecycle: str = "active",
    updated: str = "2026-09-01",
    provenance: dict | None = None,
) -> working_set.LaneItem:
    return working_set.LaneItem(
        role=role,
        level=level,
        ref=ref or f"{role}:{level}",
        path=f"Knowledge Base/Notes/{role}.md",
        title=role.title(),
        text=text,
        lifecycle=lifecycle,
        updated=updated,
        anchor="anchor",
        provenance=dict(provenance or {}),
    )


def _generation() -> dict:
    return {"freshness_key": "k", "index_generation": 3, "roles_hash": "abc", "roles_source": "shipped"}


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


def test_packet_carries_exactly_the_declared_blocks() -> None:
    packet = working_set.build_packet(
        items=(_item("resources"),),
        anchors=({"ref": "a", "title": "A", "kind": "resource", "status": "resolved", "evidence": ["exact_alias"]},),
        roles=({"id": "resources", "source": "anchor_default", "lane": "units"},),
        current_state=(),
        ambiguity=(),
        missing=(),
        budget_chars=4000,
        generation=_generation(),
        status="resolved",
    )

    assert PACKET_KEYS <= set(packet)
    assert set(packet) - PACKET_KEYS <= {"abstention"}
    assert packet["budget"] == {"limit_chars": 4000, "used_chars": 40}
    assert packet["generation"]["index_generation"] == 3
    unit = packet["units"][0]
    assert set(unit) == {"ref", "role", "text", "lifecycle", "updated", "provenance"}


def test_unit_text_is_capped() -> None:
    packet = working_set.build_packet(
        items=(_item("resources", text="y" * 1000),),
        anchors=(),
        roles=(),
        current_state=(),
        ambiguity=(),
        missing=(),
        budget_chars=4000,
        generation=_generation(),
        status="resolved",
    )

    assert len(packet["units"][0]["text"]) == working_set.MAX_UNIT_CHARS


def test_packet_never_carries_the_due_state_carrier() -> None:
    packet = working_set.build_packet(
        items=(_item("resources"),),
        anchors=(),
        roles=(),
        current_state=(),
        ambiguity=(),
        missing=(),
        budget_chars=4000,
        generation=_generation(),
        status="resolved",
    )

    assert "due_state" not in packet
    assert "due" not in packet


# --------------------------------------------------------------------------- #
# Caps, budget, ordering
# --------------------------------------------------------------------------- #


def test_per_role_cap_holds() -> None:
    items = tuple(_item("resources", ref=f"r{i}") for i in range(10))
    packet = working_set.build_packet(
        items=items,
        anchors=(),
        roles=({"id": "resources", "source": "anchor_default", "lane": "units"},),
        current_state=(),
        ambiguity=(),
        missing=(),
        budget_chars=8000,
        generation=_generation(),
        status="resolved",
    )

    kept = [unit for unit in packet["units"] if unit["role"] == "resources"]
    assert len(kept) == working_set.MAX_ITEMS_PER_ROLE
    assert len(packet["pointers"]) == 10 - working_set.MAX_ITEMS_PER_ROLE


def test_budget_is_never_exceeded_and_overflow_becomes_pointers() -> None:
    items = tuple(
        _item(role, ref=f"{role}-{i}", text="z" * 300)
        for role in ("resources", "constraints", "current_state", "methods")
        for i in range(3)
    )
    packet = working_set.build_packet(
        items=items,
        anchors=(),
        roles=(),
        current_state=(),
        ambiguity=(),
        missing=(),
        budget_chars=700,
        generation=_generation(),
        status="resolved",
    )

    assert packet["budget"]["used_chars"] <= 700
    assert packet["units"]
    assert packet["pointers"]
    assert len(packet["units"]) + len(packet["pointers"]) == len(items)
    for pointer in packet["pointers"]:
        assert set(pointer) == {"ref", "role", "title", "reason"}
        assert "text" not in pointer


def test_units_precede_pages() -> None:
    items = (
        _item("resources", level="page", ref="page-1"),
        _item("resources", level="unit", ref="unit-1"),
    )
    packet = working_set.build_packet(
        items=items,
        anchors=(),
        roles=(),
        current_state=(),
        ambiguity=(),
        missing=(),
        budget_chars=4000,
        generation=_generation(),
        status="resolved",
    )

    assert [unit["ref"] for unit in packet["units"]] == ["unit-1", "page-1"]


def test_role_priority_order_survives_the_budget() -> None:
    items = (
        _item("methods", ref="late", text="m" * 300),
        _item("resources", ref="early", text="r" * 300),
    )
    packet = working_set.build_packet(
        items=items,
        anchors=(),
        roles=(
            {"id": "resources", "source": "anchor_default", "lane": "units"},
            {"id": "methods", "source": "turn_cue", "lane": "units"},
        ),
        current_state=(),
        ambiguity=(),
        missing=(),
        budget_chars=320,
        generation=_generation(),
        status="resolved",
    )

    assert [unit["ref"] for unit in packet["units"]] == ["early"]
    assert [pointer["ref"] for pointer in packet["pointers"]] == ["late"]


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def test_superseded_unit_is_marked_with_its_successor() -> None:
    packet = working_set.build_packet(
        items=(
            _item(
                "precedents",
                lifecycle="superseded",
                provenance={"superseded_by": ["Knowledge Base/Notes/next.md"]},
            ),
        ),
        anchors=(),
        roles=(),
        current_state=(),
        ambiguity=(),
        missing=(),
        budget_chars=4000,
        generation=_generation(),
        status="resolved",
    )

    unit = packet["units"][0]
    assert unit["lifecycle"] == "superseded"
    assert unit["provenance"]["superseded_by"] == ["Knowledge Base/Notes/next.md"]


def test_a_superseded_unit_is_dropped_when_its_successor_is_present() -> None:
    successor = _item("precedents", ref="new", text="n" * 40)
    superseded = _item(
        "precedents",
        ref="old",
        lifecycle="superseded",
        provenance={"superseded_by": [successor.path]},
    )
    packet = working_set.build_packet(
        items=(superseded, successor),
        anchors=(),
        roles=(),
        current_state=(),
        ambiguity=(),
        missing=(),
        budget_chars=4000,
        generation=_generation(),
        status="resolved",
    )

    refs = [unit["ref"] for unit in packet["units"]]
    assert refs == ["new"]
    assert all(unit["lifecycle"] != "superseded" for unit in packet["units"])


# --------------------------------------------------------------------------- #
# Abstention
# --------------------------------------------------------------------------- #


def test_abstained_packet_injects_nothing() -> None:
    packet = working_set.abstained_packet(
        reason="unresolved",
        budget_chars=4000,
        generation=_generation(),
    )

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unresolved"}
    assert packet["units"] == []
    assert packet["pointers"] == []
    assert packet["current_state"] == []
    assert packet["budget"]["used_chars"] == 0


def test_ambiguous_turn_runs_no_lane() -> None:
    packet = working_set.abstained_packet(
        reason="ambiguous",
        budget_chars=4000,
        generation=_generation(),
        ambiguity=({"ref": "north", "title": "North", "kind": "hub", "neighbourhood_size": 2},),
    )

    assert packet["abstention"] == {"reason": "ambiguous"}
    assert packet["units"] == []
    assert packet["ambiguity"][0]["ref"] == "north"


# --------------------------------------------------------------------------- #
# Graph depth
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "depth"),
    [("resolved", 2), ("partial", 1), ("unresolved", 0)],
)
def test_graph_depth_follows_anchor_status(status: str, depth: int) -> None:
    assert working_set.graph_depth_for(status) == depth


def test_budget_is_clamped_to_the_declared_range() -> None:
    assert working_set.clamp_budget(None) == working_set.DEFAULT_BUDGET_CHARS
    assert working_set.clamp_budget(1) == working_set.MIN_BUDGET_CHARS
    assert working_set.clamp_budget(99999) == working_set.MAX_BUDGET_CHARS
    assert working_set.clamp_budget(2000) == 2000


# --------------------------------------------------------------------------- #
# Current state, over a real vault
# --------------------------------------------------------------------------- #


@pytest.fixture
def stateful_vault(vault: Path) -> Path:
    from test_working_set_index import _seed_planning, _seed_structure

    _seed_structure(vault)
    _seed_planning(vault)
    items = vault / "Knowledge Base" / "Records" / "Depot Stock" / "Items"
    items.mkdir(parents=True, exist_ok=True)
    (items / "2026-09-10-sled.md").write_text(
        """---
type: record
collection_id: 6f0f2b4c-1d3f-4a71-9c3d-2f9b5d2a7c11
record_id: 11111111-1111-4111-8111-111111111111
schema_version: 1
observed_on: 2026-09-10
asset: Cargo Sled
state: in storage abroad
---

The sled is in the northern depot, abroad.
""",
        encoding="utf-8",
    )
    (items / "2026-08-01-sled.md").write_text(
        """---
type: record
collection_id: 6f0f2b4c-1d3f-4a71-9c3d-2f9b5d2a7c11
record_id: 22222222-2222-4222-8222-222222222222
schema_version: 1
observed_on: 2026-08-01
asset: Cargo Sled
state: at home
---

The sled was at home.
""",
        encoding="utf-8",
    )
    return vault


def test_current_state_comes_from_records_first(stateful_vault: Path) -> None:
    from exomem import working_set_state

    index = working_set_index.WorkingSetIndex(stateful_vault)
    index.rebuild()
    rows = working_set_resolve.facts_from_rows(index.anchors())
    collection = next(row for row in rows if row.kind == "collection")

    entries = working_set_state.current_state_for(
        stateful_vault,
        anchors=(
            working_set_resolve.ResolvedAnchor(
                anchor_id=collection.anchor_id,
                path=collection.path,
                ref=collection.ref,
                title=collection.title,
                kind=collection.kind,
                lifecycle=collection.lifecycle,
                status="resolved",
                evidence=("claims_match", "lexical_overlap"),
                categories=collection.categories,
                neighbourhood=collection.neighbourhood,
            ),
        ),
    )

    assert entries, "a claiming Records collection must supply current state"
    entry = entries[0]
    assert entry["source"] == "records"
    assert entry["as_of"] == "2026-09-10"
    assert "abroad" in entry["statement"]
    assert set(entry) == {"anchor", "source", "as_of", "statement"}


def test_compile_abstains_on_a_turn_that_reaches_nothing(stateful_vault: Path) -> None:
    working_set_index.WorkingSetIndex(stateful_vault).rebuild()
    packet = working_set.compile_packet(
        stateful_vault,
        turn="zzz qqq unrelated gibberish",
        budget_chars=4000,
    )

    assert packet["abstained"] is True
    assert packet["abstention"]["reason"] == "unresolved"
    assert packet["units"] == []
    assert packet["budget"]["used_chars"] == 0


def test_compile_returns_a_bounded_packet_for_a_resolved_turn(stateful_vault: Path) -> None:
    working_set_index.WorkingSetIndex(stateful_vault).rebuild()
    packet = working_set.compile_packet(
        stateful_vault,
        turn="I'm planning to tow the Cargo Sled north — how much depot stock is left?",
        budget_chars=2000,
    )

    assert packet["abstained"] is False
    assert packet["anchors"]
    assert {anchor["status"] for anchor in packet["anchors"]} <= {"resolved", "partial"}
    assert packet["roles"]
    assert packet["budget"]["used_chars"] <= 2000
    assert packet["generation"]["index_generation"] >= 1
    assert packet["generation"]["roles_hash"]
    for anchor in packet["anchors"]:
        assert all(kind in working_set_resolve.EVIDENCE_KINDS for kind in anchor["evidence"])
