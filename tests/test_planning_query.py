from __future__ import annotations

from pathlib import Path

import pytest
from test_planning_mutation import _manifest

from exomem.structured_collections import CollectionError


def test_planning_query_uses_the_authored_horizon_without_rebucketing(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, manifest_path, _manifest(), why="create planning collection")
    add(
        tmp_path,
        manifest_path,
        item={
            "title": "Quarterly intent",
            "status": "planned",
            "commitment": "considering",
            "horizon": "quarter",
        },
        why="capture quarterly intent",
    )

    result = query(
        tmp_path, manifest_path, filters=[{"column": "horizon", "op": "eq", "value": "quarter"}]
    )

    assert result["total_matched"] == 1
    assert result["rows"][0]["plan_id"]
    assert result["rows"][0]["horizon"] == "quarter"
    assert result["hierarchy"] is None
    assert result["agent_history"] is None
    assert result["generated_at"].endswith("Z")


def test_planning_default_horizon_view_selects_the_authored_bucket(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, manifest_path, _manifest(), why="create planning collection")
    added = add(
        tmp_path,
        manifest_path,
        item={
            "title": "Quarterly intent",
            "status": "planned",
            "commitment": "considering",
            "horizon": "quarter",
        },
        why="capture quarterly intent",
    )

    result = query(tmp_path, manifest_path, view="quarter")

    assert [row["plan_id"] for row in result["rows"]] == [added["plan_id"]]


def test_planning_query_returns_bounded_descendant_edges(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, manifest_path, _manifest(), why="create planning collection")
    outcome_id = "991acdd4-16b9-4396-8220-2cb37b7e8516"
    initiative_id = "d9e3e787-3799-4e52-9f66-aef6f6075d28"
    add(
        tmp_path,
        manifest_path,
        item={"title": "Desired outcome", "kind": "outcome"},
        plan_id=outcome_id,
        why="capture outcome",
    )
    add(
        tmp_path,
        manifest_path,
        item={
            "title": "Initiative",
            "kind": "initiative",
            "parent": f"exomem://plan/2db90f18-70df-4e41-986e-2d7d7db1caca/{outcome_id}",
        },
        plan_id=initiative_id,
        why="capture initiative",
    )

    result = query(
        tmp_path,
        manifest_path,
        filters=[{"column": "plan_id", "op": "eq", "value": outcome_id}],
        hierarchy_mode="descendants",
    )

    assert result["hierarchy"]["roots"] == [outcome_id]
    assert result["hierarchy"]["edges"] == [{"parent": outcome_id, "child": initiative_id}]


def test_planning_saved_views_refuse_conflicting_filters_or_lifecycle(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    add(tmp_path, collection, item={"title": "active inbox"}, why="capture intent")

    with pytest.raises(CollectionError, match="INVALID_PLAN_ARGUMENTS"):
        query(
            tmp_path,
            collection,
            view="inbox",
            filters=[{"column": "title", "op": "eq", "value": "other"}],
        )
    with pytest.raises(CollectionError, match="INVALID_PLAN_ARGUMENTS"):
        query(tmp_path, collection, view="inbox", lifecycle="archived")


def test_planning_hierarchy_caps_roots_as_well_as_expanded_nodes(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    for index in range(3):
        add(tmp_path, collection, item={"title": f"root {index}"}, why="capture intent")

    result = query(
        tmp_path,
        collection,
        hierarchy_mode="descendants",
        hierarchy_limit=1,
        limit=20,
    )

    assert len(result["hierarchy"]["nodes"]) == 1
    assert result["hierarchy"]["truncated"] is True
