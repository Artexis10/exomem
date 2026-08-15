"""Planning `motivation`: a bounded list of `exomem://memory/` refs.

Planning items remain outside recall and the graph (see
`tests/test_planning_recall.py`); `motivation` is a reference from a plan to
the knowledge that motivates it, never the reverse, and never a relation-graph
edge. These tests cover the field's shape, its query filter, and that its
absence behaves exactly as before this change.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_planning_mutation import _manifest

from exomem import planning
from exomem.structured_collections import CollectionError

REF_A = "exomem://memory/5c252e6f-2639-4ee4-819a-fc9099200e1a"
REF_B = "exomem://memory/7a6d9f2e-2b3d-4a6b-8a3e-9a1e2f3b4c5d"


def _manifest_with_motivation() -> str:
    return _manifest().replace(
        "    health:\n      type: string\n",
        "    health:\n      type: string\n"
        "    motivation:\n      type: array\n      items: {type: string}\n",
    )


def _seed_collection_with(tmp_path: Path, manifest_text: str) -> str:
    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    from exomem.planning import create_collection

    create_collection(tmp_path, manifest_path, manifest_text, why="create planning collection")
    return manifest_path


def _seed_collection(tmp_path: Path) -> str:
    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    from exomem.planning import create_collection

    create_collection(
        tmp_path, manifest_path, _manifest_with_motivation(), why="create planning collection"
    )
    return manifest_path


# -- Pure shape validation (normalize_item), no vault I/O -------------------


def test_normalize_item_accepts_a_valid_motivation_list() -> None:
    from exomem.planning import normalize_item

    values = normalize_item({"title": "Ship the migration", "motivation": [REF_A, REF_B]})

    assert values["motivation"] == [REF_A, REF_B]


def test_normalize_item_refuses_more_than_sixteen_motivation_entries() -> None:
    from exomem.planning import normalize_item

    refs = [f"exomem://memory/00000000-0000-4000-8000-{index:012d}" for index in range(17)]

    with pytest.raises(CollectionError, match="^INVALID_PLAN:"):
        normalize_item({"title": "Too many motivations", "motivation": refs})


def test_normalize_item_accepts_exactly_sixteen_motivation_entries() -> None:
    from exomem.planning import normalize_item

    refs = [f"exomem://memory/00000000-0000-4000-8000-{index:012d}" for index in range(16)]

    values = normalize_item({"title": "At the bound", "motivation": refs})

    assert values["motivation"] == refs


def test_normalize_item_refuses_a_malformed_motivation_reference() -> None:
    from exomem.planning import normalize_item

    with pytest.raises(CollectionError, match="^INVALID_PLAN:"):
        normalize_item({"title": "Bad ref", "motivation": ["not-a-memory-ref"]})


def test_normalize_item_refuses_a_non_list_motivation() -> None:
    from exomem.planning import normalize_item

    with pytest.raises(CollectionError, match="^INVALID_PLAN:"):
        normalize_item({"title": "Not a list", "motivation": REF_A})


def test_normalize_item_refuses_a_plan_reference_as_motivation() -> None:
    """Non-goal: motivation cites knowledge, never another plan."""
    from exomem.planning import normalize_item

    plan_ref = "exomem://plan/2db90f18-70df-4e41-986e-2d7d7db1caca/991acdd4-16b9-4396-8220-2cb37b7e8516"

    with pytest.raises(CollectionError, match="^INVALID_PLAN:"):
        normalize_item({"title": "Wrong namespace", "motivation": [plan_ref]})


def test_normalize_item_omits_motivation_when_absent() -> None:
    """Absence must behave exactly as today: no key is defaulted in."""
    from exomem.planning import normalize_item

    values = normalize_item({"title": "Keep this for later"})

    assert "motivation" not in values
    assert values == {
        "title": "Keep this for later",
        "kind": "work-item",
        "status": "candidate",
        "lifecycle": "active",
        "priority": "none",
        "commitment": "uncommitted",
        "horizon": "inbox",
        "health": "unknown",
    }


# -- Serialization round trip -------------------------------------------


def test_markdown_item_renderer_round_trips_motivation_refs(tmp_path: Path) -> None:
    from exomem import record_formats, vault
    from exomem.structured_collections import load_manifest

    directory = tmp_path / "Knowledge Base" / "Planning" / "Work"
    directory.mkdir(parents=True)
    (directory / "_collection.md").write_text(_manifest_with_motivation(), encoding="utf-8")
    manifest = load_manifest(tmp_path, directory / "_collection.md")

    rendered = record_formats.render_markdown_item(
        manifest,
        {"title": "Ship the migration", "motivation": [REF_A, REF_B]},
        "991acdd4-16b9-4396-8220-2cb37b7e8516",
    )

    frontmatter, _body, _marker = vault.parse_frontmatter(rendered, strict=True)
    assert frontmatter["motivation"] == [REF_A, REF_B]


# -- add()/query() integration -------------------------------------------


def test_planning_add_and_query_round_trip_motivation(tmp_path: Path) -> None:
    from exomem.planning import add, query

    manifest_path = _seed_collection(tmp_path)

    added = add(
        tmp_path,
        manifest_path,
        item={"title": "Ship the migration", "motivation": [REF_A, REF_B]},
        why="capture motivated work",
    )

    result = query(tmp_path, manifest_path)

    assert result["rows"][0]["plan_id"] == added["plan_id"]
    assert result["rows"][0]["motivation"] == [REF_A, REF_B]


def test_planning_add_refuses_more_than_sixteen_motivation_entries(tmp_path: Path) -> None:
    from exomem.planning import add

    manifest_path = _seed_collection(tmp_path)
    refs = [f"exomem://memory/00000000-0000-4000-8000-{index:012d}" for index in range(17)]

    with pytest.raises(CollectionError, match="^INVALID_PLAN:"):
        add(tmp_path, manifest_path, item={"title": "Too many", "motivation": refs}, why="capture")


def test_planning_add_refuses_a_malformed_motivation_reference(tmp_path: Path) -> None:
    from exomem.planning import add

    manifest_path = _seed_collection(tmp_path)

    with pytest.raises(CollectionError, match="^INVALID_PLAN:"):
        add(
            tmp_path,
            manifest_path,
            item={"title": "Bad ref", "motivation": ["not-a-ref"]},
            why="capture",
        )


def test_planning_add_refuses_a_non_list_motivation(tmp_path: Path) -> None:
    from exomem.planning import add

    manifest_path = _seed_collection(tmp_path)

    with pytest.raises(CollectionError, match="^INVALID_PLAN:"):
        add(tmp_path, manifest_path, item={"title": "Not a list", "motivation": REF_A}, why="capture")


def test_planning_add_without_motivation_behaves_exactly_as_today(tmp_path: Path) -> None:
    from exomem.planning import add, query

    manifest_path = _seed_collection(tmp_path)

    add(tmp_path, manifest_path, item={"title": "Keep this for later"}, why="capture future work")
    result = query(tmp_path, manifest_path)

    assert "motivation" not in result["rows"][0]


def test_motivation_query_filter_selects_the_referencing_item(tmp_path: Path) -> None:
    from exomem.planning import add, query

    manifest_path = _seed_collection(tmp_path)
    motivated = add(
        tmp_path,
        manifest_path,
        item={"title": "Motivated by a belief", "motivation": [REF_A]},
        why="capture motivated work",
    )
    add(
        tmp_path,
        manifest_path,
        item={"title": "Motivated by something else", "motivation": [REF_B]},
        why="capture other work",
    )
    add(tmp_path, manifest_path, item={"title": "Unmotivated work"}, why="capture plain work")

    result = query(
        tmp_path,
        manifest_path,
        filters=[{"column": "motivation", "op": "contains", "value": REF_A}],
    )

    assert [row["plan_id"] for row in result["rows"]] == [motivated["plan_id"]]
    assert result["total_matched"] == 1


def test_motivation_does_not_satisfy_the_required_parent_relation(tmp_path: Path) -> None:
    """Non-goal: motivation never stands in for the plan graph."""
    from exomem.planning import add

    manifest_path = _seed_collection(tmp_path)

    with pytest.raises(CollectionError, match="INVALID_PLAN_RELATION"):
        add(
            tmp_path,
            manifest_path,
            item={
                "title": "Committed without a parent",
                "kind": "initiative",
                "status": "active",
                "commitment": "committed",
                "horizon": "quarter",
                "motivation": [REF_A],
            },
            why="capture committed initiative",
        )


def test_motivation_update_can_be_deleted_like_other_optional_fields(tmp_path: Path) -> None:
    from exomem.planning import add, update

    manifest_path = _seed_collection(tmp_path)
    added = add(
        tmp_path,
        manifest_path,
        item={"title": "Ship the migration", "motivation": [REF_A]},
        why="capture motivated work",
    )

    receipt = update(
        tmp_path,
        manifest_path,
        plan_id=added["plan_id"],
        changes={"motivation": None},
        expected_container_hash=added["after_container_hash"],
        expected_item_version=added["after_item_hash"],
        why="drop motivation",
    )

    assert receipt["operation"] == "update"
    item_path = (
        tmp_path
        / "Knowledge Base"
        / "Planning"
        / "Work"
        / "Items"
        / f"{added['plan_id']}.md"
    )
    assert "motivation" not in item_path.read_text(encoding="utf-8")


def test_legacy_untyped_motivation_field_does_not_brick_the_collection(tmp_path: Path) -> None:
    """A vault that declared its own `motivation` field keeps working.

    The governed ref-list shape is enforced only where the manifest declares
    `motivation` as an array. Without that gate, `query` — which normalizes every
    stored record — would refuse the whole collection because of one legacy item,
    leaving it unqueryable and unrepairable.
    """
    manifest = _manifest().replace(
        "    health:\n      type: string\n",
        "    health:\n      type: string\n    motivation:\n      type: string\n",
    )
    collection = _seed_collection_with(tmp_path, manifest)

    planning.add(
        tmp_path,
        collection,
        item={"title": "Legacy", "motivation": "because the customer asked"},
        why="legacy free-text motivation predating the governed contract",
    )

    rows = planning.query(tmp_path, collection)["rows"]
    assert any(row.get("motivation") == "because the customer asked" for row in rows)


def test_motivation_deletion_relies_on_optional_membership(tmp_path: Path) -> None:
    """Pin `_OPTIONAL` membership as load-bearing, not incidental.

    With `motivation` declared required, deletion is refused either way — but by
    a different layer, and the code says which. In `_OPTIONAL` the Planning check
    passes and the schema layer refuses with SCHEMA_REQUIRED_FIELD; drop it from
    `_OPTIONAL` and Planning refuses first with INVALID_PLAN. Asserting the code
    makes membership observable rather than decorative.
    """
    manifest = _manifest().replace(
        "    health:\n      type: string\n",
        "    health:\n      type: string\n"
        "    motivation:\n      type: array\n      required: true\n"
        "      items: {type: string}\n",
    )
    collection = _seed_collection_with(tmp_path, manifest)
    added = planning.add(
        tmp_path,
        collection,
        item={"title": "Required motivation", "motivation": [REF_A]},
        why="seed an item whose motivation is declared required",
    )

    with pytest.raises(CollectionError) as raised:
        planning.update(
            tmp_path,
            collection,
            plan_id=added["plan_id"],
            changes={"motivation": None},
            expected_container_hash=added["after_container_hash"],
            expected_item_version=added["after_item_hash"],
            why="deleting a required field must be refused by the schema layer",
        )

    assert raised.value.code == "SCHEMA_REQUIRED_FIELD"
