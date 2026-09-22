"""Task 4.1 — bounded role lanes and the working-memory packet.

The packet is the product: a bounded, auditable, provenance-bearing answer to
"what should be in working memory for this turn". These tests pin its shape, its
budget arithmetic, the units-before-pages ordering, supersession marking, the
Records-first current-state rule, the abstained shape, and the graph depths.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import working_set, working_set_index, working_set_resolve

PACKET_KEYS = {
    "recent_context",
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
        max_chars=4000,
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
        max_chars=4000,
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
        max_chars=4000,
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
        max_chars=8000,
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
        max_chars=700,
        generation=_generation(),
        status="resolved",
    )

    assert packet["budget"]["used_chars"] <= 700
    assert packet["units"]
    assert packet["pointers"]
    # Pointers are prose too, so the ceiling bounds them: overflow past the
    # budget is not reported at all rather than breaking the bound.
    assert len(packet["units"]) + len(packet["pointers"]) <= len(items)
    for pointer in packet["pointers"]:
        assert set(pointer) == {"ref", "role", "title", "why", "reason"}
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
        max_chars=4000,
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
        max_chars=320,
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
        max_chars=4000,
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
        max_chars=4000,
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
        max_chars=4000,
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
        max_chars=4000,
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
    [("resolved", 2), ("partial", 0), ("unresolved", 0)],
)
def test_graph_depth_follows_anchor_status(status: str, depth: int) -> None:
    """No lane runs for a `partial` anchor at all (review round 3, BLOCKER 2):
    depth 0, not the prior depth-1 case that let its neighbourhood leak into
    `units`/`pointers`/`current_state`.
    """
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
        max_chars=4000,
    )

    assert packet["abstained"] is True
    assert packet["abstention"]["reason"] == "unresolved"
    # Abstention injects no ANSWER — no units, no pointers, no current state.
    # Working continuity is the one exception and it is not an answer: it says
    # what was recently worked on, which is true of the session whatever this
    # turn reached. Its cost is pinned to the block's own arithmetic so the
    # abstained packet cannot quietly start carrying anything else.
    assert packet["units"] == []
    assert packet["pointers"] == []
    assert packet["current_state"] == []
    assert packet["budget"]["used_chars"] == sum(
        len(str(entry.get("title") or "")) + len(str(entry.get("statement") or ""))
        for entry in packet["recent_context"]
    )


def test_compile_returns_a_bounded_packet_for_a_resolved_turn(stateful_vault: Path) -> None:
    working_set_index.WorkingSetIndex(stateful_vault).rebuild()
    packet = working_set.compile_packet(
        stateful_vault,
        turn="I'm planning to tow the Cargo Sled north — how much depot stock is left?",
        max_chars=2000,
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


# --------------------------------------------------------------------------- #
# Review round: honest budget accounting, wikilink-safe cuts, truncation markers
# --------------------------------------------------------------------------- #


def _prose_chars(packet: dict) -> int:
    """Every prose field the amended spec counts against the budget."""
    return (
        sum(len(unit["text"]) for unit in packet["units"])
        + sum(len(entry.get("statement") or "") for entry in packet["current_state"])
        + sum(
            len(pointer.get("title") or "") + len(pointer.get("why") or "")
            for pointer in packet["pointers"]
        )
    )


def test_used_chars_counts_every_prose_field() -> None:
    packet = working_set.build_packet(
        items=tuple(
            _item(role, ref=f"{role}-{i}", text="z" * 200)
            for role in ("resources", "constraints", "methods")
            for i in range(4)
        ),
        anchors=(),
        roles=(),
        current_state=(
            {
                "anchor": "a",
                "source": "records",
                "as_of": "2026-09-10",
                "statement": "state: " + "s" * 120,
            },
        ),
        ambiguity=(),
        missing=(),
        max_chars=900,
        generation=_generation(),
        status="resolved",
    )

    assert packet["budget"]["used_chars"] == _prose_chars(packet)
    assert packet["budget"]["used_chars"] <= 900


def test_the_budget_bounds_pointer_prose_too() -> None:
    """Overflow becomes pointers, and pointers are not free."""
    packet = working_set.build_packet(
        items=tuple(
            _item("resources", ref=f"r-{i}", text="z" * 300) for i in range(30)
        ),
        anchors=(),
        roles=({"id": "resources", "source": "anchor_default", "lane": "units"},),
        current_state=(),
        ambiguity=(),
        missing=(),
        max_chars=500,
        generation=_generation(),
        status="resolved",
    )

    assert packet["budget"]["used_chars"] <= 500
    assert packet["budget"]["used_chars"] == _prose_chars(packet)


def test_pointers_carry_a_why() -> None:
    packet = working_set.build_packet(
        items=tuple(_item("resources", ref=f"r-{i}") for i in range(6)),
        anchors=(),
        roles=({"id": "resources", "source": "anchor_default", "lane": "units"},),
        current_state=(),
        ambiguity=(),
        missing=(),
        max_chars=8000,
        generation=_generation(),
        status="resolved",
    )

    assert packet["pointers"]
    for pointer in packet["pointers"]:
        assert set(pointer) == {"ref", "role", "title", "why", "reason"}
        assert pointer["why"], "a pointer must say what admitted it"
        assert "text" not in pointer


def test_unit_text_is_never_cut_inside_a_wikilink() -> None:
    """A half-written wikilink is both unreadable and unscannable.

    The egress guard finds a withheld page by matching `[[…]]`, so a cut that
    leaves `[[norther` hides the reference from the scan as well as from the
    reader.
    """
    head = "x" * (working_set.MAX_UNIT_CHARS - 12)
    packet = working_set.build_packet(
        items=(_item("resources", text=f"{head}[[northern-corridor-hub]] tail"),),
        anchors=(),
        roles=(),
        current_state=(),
        ambiguity=(),
        missing=(),
        max_chars=8000,
        generation=_generation(),
        status="resolved",
    )

    text = packet["units"][0]["text"]
    assert len(text) <= working_set.MAX_UNIT_CHARS
    assert text.count("[[") == text.count("]]")
    assert "[[norther" not in text or "]]" in text


def test_a_closed_wikilink_inside_the_cap_survives() -> None:
    packet = working_set.build_packet(
        items=(_item("resources", text="see [[northern-corridor-hub]] for context"),),
        anchors=(),
        roles=(),
        current_state=(),
        ambiguity=(),
        missing=(),
        max_chars=8000,
        generation=_generation(),
        status="resolved",
    )

    assert "[[northern-corridor-hub]]" in packet["units"][0]["text"]


def test_a_truncated_lane_is_reported(stateful_vault: Path) -> None:
    """A lane that hits its read limit says so instead of silently dropping units."""
    notes = stateful_vault / "Knowledge Base" / "Notes" / "Patterns"
    notes.mkdir(parents=True, exist_ok=True)
    for index in range(working_set.UNIT_LANE_LIMIT + 30):
        (notes / f"bulk-constraint-{index:04d}.md").write_text(
            f"""---
type: pattern
status: active
updated: 2026-09-01
---

# Bulk constraint {index:04d}

- [constraint] Never exceed {index} kilograms when towing the Cargo Sled. ^bulk-{index}
""",
            encoding="utf-8",
        )

    # Startup owns the catalogue build; an interactive unit lane refuses to
    # become it, so warm it explicitly before measuring the lane's own limit.
    from exomem import lexstore

    lexstore.ensure_fresh(stateful_vault)
    working_set_index.WorkingSetIndex(stateful_vault).reset()
    working_set_index.WorkingSetIndex(stateful_vault).rebuild()
    from exomem import context_roles

    result = working_set._units_lane(
        stateful_vault,
        context_roles.load_roles().roles["constraints"],
        neighbourhood=frozenset(
            f"Knowledge Base/Notes/Patterns/bulk-constraint-{index:04d}.md"
            for index in range(working_set.UNIT_LANE_LIMIT + 30)
        ),
    )
    assert result.truncated is True


def test_current_state_is_resolved_once_per_compile(
    stateful_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import working_set_state

    working_set_index.WorkingSetIndex(stateful_vault).rebuild()
    calls: list[int] = []
    real = working_set_state.current_state_for

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(working_set_state, "current_state_for", counting)

    packet = working_set.compile_packet(
        stateful_vault,
        turn="I'm planning to tow the Cargo Sled north — how much depot stock is left?",
        max_chars=2000,
    )

    assert packet["abstained"] is False
    assert len(calls) == 1, f"current state resolved {len(calls)} times"


def test_every_current_state_source_fills_a_statement(stateful_vault: Path) -> None:
    from exomem import working_set_resolve, working_set_state

    index = working_set_index.WorkingSetIndex(stateful_vault)
    index.rebuild()
    rows = working_set_resolve.facts_from_rows(index.anchors())
    anchors = tuple(
        working_set_resolve.ResolvedAnchor(
            anchor_id=row.anchor_id,
            path=row.path,
            ref=row.ref,
            title=row.title,
            kind=row.kind,
            lifecycle=row.lifecycle,
            status="resolved",
            evidence=("exact_alias",),
            categories=row.categories,
            neighbourhood=row.neighbourhood,
        )
        for row in rows
        if row.kind in working_set_state.STATEFUL_KINDS
    )

    entries = working_set_state.current_state_for(stateful_vault, anchors=anchors)

    assert entries
    for entry in entries:
        assert entry["source"] in {"records", "profile", "note"}
        assert entry["statement"], f"{entry['source']} produced no statement"
        assert len(entry["statement"]) <= working_set_state.STATEMENT_MAX_CHARS


# --------------------------------------------------------------------------- #
# Round two: budget loss is reported, truncation is exact
# --------------------------------------------------------------------------- #


def _fat(role: str, ref: str, *, text: str = "z" * 300) -> working_set.LaneItem:
    """An item whose pointer is expensive, so the budget can starve it."""
    return working_set.LaneItem(
        role=role,
        level="unit",
        ref=ref,
        path=f"Knowledge Base/Notes/{ref}.md",
        title="T" * 120,
        text=text,
        lifecycle="active",
        updated="2026-09-01",
        anchor="anchor",
        why="W" * 120,
    )


def test_no_item_vanishes_without_a_pointer_or_a_marker() -> None:
    """The reviewer's shape: 8 items at max_chars=500 lost 6 with an empty missing[].

    The invariant is accounting, not a count: every candidate leaves the packet as
    a unit, as a pointer, or as a `budget` marker naming its role. Pointer prose
    is now cheap enough that this particular shape keeps all eight, which is a
    better outcome than the reviewer measured — and the assertion below holds
    either way, which is the point.
    """
    items = tuple(_item("resources", ref=f"r-{i}", text="z" * 300) for i in range(8))
    packet = working_set.build_packet(
        items=items,
        anchors=(),
        roles=({"id": "resources", "source": "anchor_default", "lane": "units"},),
        current_state=(),
        ambiguity=(),
        missing=(),
        max_chars=500,
        generation=_generation(),
        status="resolved",
    )

    kept = len(packet["units"]) + len(packet["pointers"])
    markers = {entry["role"] for entry in packet["missing"] if entry["reason"] == "budget"}
    assert kept == len(items) or markers == {"resources"}
    assert packet["budget"]["used_chars"] <= 500


def test_items_lost_to_the_budget_are_reported_once_per_role() -> None:
    packet = working_set.build_packet(
        items=tuple(_fat("resources", f"r-{i}") for i in range(8)),
        anchors=(),
        roles=({"id": "resources", "source": "anchor_default", "lane": "units"},),
        current_state=(),
        ambiguity=(),
        missing=(),
        max_chars=500,
        generation=_generation(),
        status="resolved",
    )

    kept = len(packet["units"]) + len(packet["pointers"])
    assert kept < 8, "this shape must lose items to the budget"
    budget_markers = [
        entry for entry in packet["missing"] if entry["reason"] == "budget"
    ]
    assert budget_markers == [{"role": "resources", "reason": "budget"}]
    assert packet["budget"]["used_chars"] <= 500


def test_a_role_that_loses_nothing_gets_no_budget_marker() -> None:
    packet = working_set.build_packet(
        items=(_item("resources", text="short"),),
        anchors=(),
        roles=({"id": "resources", "source": "anchor_default", "lane": "units"},),
        current_state=(),
        ambiguity=(),
        missing=(),
        max_chars=8000,
        generation=_generation(),
        status="resolved",
    )

    assert [entry for entry in packet["missing"] if entry["reason"] == "budget"] == []


def test_each_losing_role_gets_its_own_budget_marker() -> None:
    packet = working_set.build_packet(
        items=tuple(
            _fat(role, f"{role}-{i}")
            for role in ("resources", "constraints")
            for i in range(6)
        ),
        anchors=(),
        roles=(
            {"id": "resources", "source": "anchor_default", "lane": "units"},
            {"id": "constraints", "source": "anchor_default", "lane": "units"},
        ),
        current_state=(),
        ambiguity=(),
        missing=(),
        max_chars=700,
        generation=_generation(),
        status="resolved",
    )

    roles = sorted(
        entry["role"] for entry in packet["missing"] if entry["reason"] == "budget"
    )
    assert roles == ["constraints", "resources"]


def test_an_exact_limit_read_does_not_claim_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """`lane_truncated` must mean "there was more", not "the read was full".

    Reading one row past the limit and slicing is what makes the marker exact: a
    read that returns exactly `UNIT_LANE_LIMIT` rows has no evidence that a
    single further unit existed.
    """
    import exomem.find as find_module
    from exomem import context_roles

    role = context_roles.load_roles().roles["constraints"]
    calls: list[int] = []

    def fake_units(_vault_root, **kwargs):
        calls.append(int(kwargs["limit"]))
        return [
            SimpleNamespace(
                unit_ref=f"u-{index}",
                parent_path="Knowledge Base/Notes/Patterns/p.md",
                parent_title="P",
                parent_updated="2026-09-01",
                parent_superseded_by=[],
                content="text",
                excerpt="text",
                category="constraint",
                kind="claim",
            )
            for index in range(working_set.UNIT_LANE_LIMIT)
        ]

    monkeypatch.setattr(find_module, "_find_semantic_units", fake_units)
    result = working_set._units_lane(
        Path("/nonexistent"),
        role,
        neighbourhood=frozenset({"Knowledge Base/Notes/Patterns/p.md"}),
    )

    assert calls == [working_set.UNIT_LANE_LIMIT + 1], (
        "the lane must read one past its limit so the marker can be exact"
    )
    assert result.truncated is False
    assert len(result.items) == working_set.UNIT_LANE_LIMIT


# --------------------------------------------------------------------------- #
# Recent context — the always-on working-continuity block
# --------------------------------------------------------------------------- #


def _recent(
    name: str,
    *,
    why: str = "edited",
    statement: str | None = None,
    title: str | None = None,
) -> dict:
    entry: dict = {
        "ref": f"Knowledge Base/Notes/{name}.md",
        "path": f"Knowledge Base/Notes/{name}.md",
        "title": title if title is not None else name.replace("-", " "),
        "kind": "note",
        "why": why,
        "as_of": "2026-09-20",
    }
    if statement is not None:
        entry["statement"] = statement
    return entry


def test_recent_context_is_the_first_declared_block() -> None:
    """Working continuity is what a fresh session opens with, so it leads."""
    assert working_set.PACKET_BLOCKS[0] == "recent_context"
    assert working_set.PACKET_BLOCKS.index("recent_context") < working_set.PACKET_BLOCKS.index(
        "current_state"
    )


def test_a_resolved_packet_carries_recent_context_before_current_state() -> None:
    packet = working_set.build_packet(
        items=(_item("resources"),),
        anchors=(),
        roles=(),
        current_state=({"anchor": "a", "source": "records", "as_of": "", "statement": "s" * 20},),
        recent_context=(_recent("harbour-refit"),),
        ambiguity=(),
        missing=(),
        max_chars=4000,
        generation=_generation(),
        status="resolved",
    )

    keys = list(packet)
    assert keys.index("recent_context") < keys.index("current_state")
    assert [entry["path"] for entry in packet["recent_context"]] == [
        "Knowledge Base/Notes/harbour-refit.md"
    ]
    assert set(packet["recent_context"][0]) == {
        "ref",
        "path",
        "title",
        "kind",
        "why",
        "as_of",
    }


def test_recent_context_is_budgeted_first_and_counted_in_used_chars() -> None:
    """The block is budgeted before `current_state`, inside the same ceiling."""
    entry = _recent("harbour-refit", statement="status: waiting on the yard")
    cost = len(entry["title"]) + len(entry["statement"])
    packet = working_set.build_packet(
        items=(),
        anchors=(),
        roles=(),
        current_state=(),
        recent_context=(entry,),
        ambiguity=(),
        missing=(),
        max_chars=working_set.MIN_BUDGET_CHARS,
        generation=_generation(),
        status="resolved",
    )

    assert packet["budget"]["used_chars"] == cost


def test_a_recent_entry_that_does_not_fit_is_dropped_whole() -> None:
    """Like `current_state`: a half-sentence about recent work is worse than none."""
    small = _recent("harbour-refit", statement="status: waiting")
    huge = _recent("long-note", statement="x" * (working_set.RECENT_CONTEXT_MAX_CHARS + 50))
    packet = working_set.build_packet(
        items=(),
        anchors=(),
        roles=(),
        current_state=(),
        recent_context=(small, huge),
        ambiguity=(),
        missing=(),
        max_chars=4000,
        generation=_generation(),
        status="resolved",
    )

    assert [entry["path"] for entry in packet["recent_context"]] == [small["path"]]
    assert packet["budget"]["used_chars"] == len(small["title"]) + len(small["statement"])


def test_recent_context_is_capped_at_its_entry_ceiling() -> None:
    packet = working_set.build_packet(
        items=(),
        anchors=(),
        roles=(),
        current_state=(),
        recent_context=tuple(_recent(f"note-{index}") for index in range(12)),
        ambiguity=(),
        missing=(),
        max_chars=8000,
        generation=_generation(),
        status="resolved",
    )

    assert len(packet["recent_context"]) == working_set.RECENT_CONTEXT_MAX_ENTRIES == 8


def test_an_abstained_packet_still_carries_recent_context() -> None:
    """Abstention injects no ANSWER; it still says what was recently worked on."""
    entry = _recent("harbour-refit", statement="status: waiting on the yard")
    packet = working_set.abstained_packet(
        reason="unresolved",
        max_chars=4000,
        generation=_generation(),
        recent_context=(entry,),
    )

    assert packet["abstained"] is True
    assert [item["path"] for item in packet["recent_context"]] == [entry["path"]]
    assert packet["units"] == []
    assert packet["budget"]["used_chars"] == len(entry["title"]) + len(entry["statement"])


def _live_cell(vault: Path) -> None:
    """Seed the freshness registry the way the running service does.

    `recent_context` reads per-path mtimes out of that registry rather than
    walking the vault, so a test that never seeds it is testing the
    not-live fallback, not the block.
    """
    from exomem import file_watcher

    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)


def _touch(path: Path, *, when: float) -> None:
    import os

    os.utime(path, (when, when))


def test_an_unresolved_turn_still_carries_the_recently_edited_page_first(
    stateful_vault: Path,
) -> None:
    """The whole point: "ok continue" resolves nothing and must still say
    what was recently worked on."""
    import time

    working_set_index.WorkingSetIndex(stateful_vault).rebuild()
    newest = stateful_vault / "Knowledge Base" / "Products" / "Cargo Sled.md"
    now = time.time()
    for index, page in enumerate(sorted((stateful_vault / "Knowledge Base").rglob("*.md"))):
        _touch(page, when=now - 10_000 - index)
    _touch(newest, when=now)
    _live_cell(stateful_vault)

    packet = working_set.compile_packet(
        stateful_vault, turn="zzz qqq unrelated gibberish", max_chars=4000
    )

    assert packet["abstained"] is True
    assert packet["recent_context"], "an abstained packet still carries recent context"
    first = packet["recent_context"][0]
    assert first["path"] == "Knowledge Base/Products/Cargo Sled.md"
    assert first["why"] == "edited"
    assert first["title"] == "Cargo Sled"
    assert first["as_of"]


def test_a_resolved_turn_carries_recent_context_as_well(stateful_vault: Path) -> None:
    working_set_index.WorkingSetIndex(stateful_vault).rebuild()
    _live_cell(stateful_vault)

    packet = working_set.compile_packet(
        stateful_vault,
        turn="I'm planning to tow the Cargo Sled north — how much depot stock is left?",
        max_chars=4000,
    )

    assert packet["abstained"] is False
    assert packet["recent_context"]
    assert all(entry["path"] for entry in packet["recent_context"])
    assert len(packet["recent_context"]) <= working_set.RECENT_CONTEXT_MAX_ENTRIES


def test_a_captured_session_page_carries_its_title_and_date_only(
    stateful_vault: Path,
) -> None:
    import time

    sessions = stateful_vault / "Knowledge Base" / "Sources" / "Sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    captured = sessions / "2026-09-21-corridor-call.md"
    captured.write_text(
        "---\ntype: source\nstatus: active\nsummary: a summary nobody asked for\n---\n\nRaw notes.\n",
        encoding="utf-8",
    )
    other = stateful_vault / "Knowledge Base" / "Sources" / "corridor-report.md"
    other.write_text("---\ntype: source\n---\n\nAn ordinary source.\n", encoding="utf-8")
    working_set_index.WorkingSetIndex(stateful_vault).rebuild()
    now = time.time()
    for index, page in enumerate(sorted((stateful_vault / "Knowledge Base").rglob("*.md"))):
        _touch(page, when=now - 10_000 - index)
    _touch(captured, when=now)
    _touch(other, when=now - 1)
    _live_cell(stateful_vault)

    packet = working_set.compile_packet(
        stateful_vault, turn="zzz qqq unrelated gibberish", max_chars=4000
    )

    paths = [entry["path"] for entry in packet["recent_context"]]
    assert "Knowledge Base/Sources/Sessions/2026-09-21-corridor-call.md" in paths
    assert "Knowledge Base/Sources/corridor-report.md" not in paths, (
        "a raw Source page is not working context — only a captured session is"
    )
    entry = packet["recent_context"][paths.index(
        "Knowledge Base/Sources/Sessions/2026-09-21-corridor-call.md"
    )]
    assert entry["why"] == "captured"
    assert entry["as_of"]
    assert "statement" not in entry, "a captured session carries its title and date only"


def test_recent_context_gets_its_own_timing_span(stateful_vault: Path) -> None:
    from exomem import find_types

    working_set_index.WorkingSetIndex(stateful_vault).rebuild()
    _live_cell(stateful_vault)
    timings = find_types.FindTimings()

    working_set.compile_packet(
        stateful_vault, turn="zzz qqq unrelated gibberish", max_chars=4000, timings=timings
    )

    assert "working_set.recent" in timings.as_dict()["stages"]
