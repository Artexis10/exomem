from __future__ import annotations

from pathlib import Path

import pytest
from test_planning_mutation import _manifest

from exomem.governance.principal import RequestPrincipal, request_scope


def _write_planning_rule(vault: Path, ceiling: int) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "planning.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5F01\n"
        "name: Planning\n"
        'paths: ["Planning/**"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "external.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5F02\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5F01"]\n'
        "audience: external\n"
        f"ceiling: {ceiling}\n",
        encoding="utf-8",
    )


def test_planning_authorizes_manifest_before_loading_its_identity(tmp_path: Path) -> None:
    from exomem.planning import query
    from exomem.structured_collections import CollectionError

    directory = tmp_path / "Knowledge Base" / "Planning" / "Work"
    directory.mkdir(parents=True)
    (directory / "_collection.md").write_text(_manifest(), encoding="utf-8")

    with pytest.raises(CollectionError, match="^COLLECTION_NOT_FOUND:"):
        query(
            tmp_path,
            "Knowledge Base/Planning/Work/_collection.md",
            authorize_path=lambda _path: False,
        )


@pytest.mark.parametrize("ceiling", range(7))
def test_planning_requires_l6_for_every_query_and_inspection_artifact(
    tmp_path: Path, ceiling: int
) -> None:
    from exomem.planning import create_collection, inspect, query
    from exomem.structured_collections import CollectionError

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    _write_planning_rule(tmp_path, ceiling)

    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        if ceiling == 6:
            assert query(tmp_path, collection)["rows"] == []
            assert inspect(tmp_path, collection)["contract"]["collection_id"]
        else:
            for operation in (query, inspect):
                with pytest.raises(CollectionError, match="^COLLECTION_NOT_FOUND:"):
                    operation(tmp_path, collection)


def test_withheld_malformed_item_is_identical_to_an_absent_item(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    visible = add(tmp_path, collection, item={"title": "visible"}, why="capture intent")
    hidden_id = "5c252e6f-2639-4ee4-819a-fc9099200e1a"
    hidden = tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items" / f"{hidden_id}.md"
    hidden.write_text("---\nthis: is: malformed\n---\n", encoding="utf-8")

    result = query(
        tmp_path,
        collection,
        authorize_path=lambda path: hidden_id not in path,
    )

    assert result["total_matched"] == 1
    assert [row["plan_id"] for row in result["rows"]] == [visible["plan_id"]]


def test_withheld_duplicate_plan_id_cannot_create_public_ambiguity(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    visible = add(tmp_path, collection, item={"title": "visible"}, why="capture intent")
    original = tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items" / "visible.md"
    duplicate_name = "5c252e6f-2639-4ee4-819a-fc9099200e1a"
    duplicate = original.with_name(f"{duplicate_name}.md")
    duplicate.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")

    result = query(
        tmp_path,
        collection,
        authorize_path=lambda path: duplicate_name not in path,
    )

    assert result["total_matched"] == 1
    assert result["rows"][0]["plan_id"] == visible["plan_id"]
    assert result["rows"][0]["ambiguous"] is False


def test_withheld_items_do_not_consume_the_public_planning_file_cap(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    visible = add(tmp_path, collection, item={"title": "visible"}, why="capture intent")
    items = tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items"
    for index in range(2_001):
        (items / f"hidden-{index:04}.md").write_text("not a Planning item\n", encoding="utf-8")

    result = query(
        tmp_path,
        collection,
        authorize_path=lambda path: "/hidden-" not in path,
    )

    assert result["total_matched"] == 1
    assert result["rows"][0]["plan_id"] == visible["plan_id"]


def test_withheld_item_cannot_shape_planning_totals_order_or_provenance(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    add(tmp_path, collection, item={"title": "Zulu visible"}, why="capture intent")
    hidden = add(tmp_path, collection, item={"title": "000 private"}, why="capture intent")
    add(tmp_path, collection, item={"title": "Alpha visible"}, why="capture intent")

    def allow_visible(path: str) -> bool:
        return not path.endswith("/000 private.md")

    result = query(
        tmp_path,
        collection,
        sort_by="title",
        hierarchy_mode="descendants",
        authorize_path=allow_visible,
    )
    grouped = query(
        tmp_path,
        collection,
        aggregate="group:title",
        authorize_path=allow_visible,
    )

    assert result["total_matched"] == 2
    assert [row["title"] for row in result["rows"]] == ["Alpha visible", "Zulu visible"]
    assert "000 private" not in result["rendered"]
    assert all(hidden["plan_id"] not in source["path"] for source in result["source_versions"])
    assert all(node["plan_id"] != hidden["plan_id"] for node in result["hierarchy"]["nodes"])
    assert grouped["aggregate"] == {
        "groups": [{"value": "Alpha visible", "count": 1}, {"value": "Zulu visible", "count": 1}],
        "n": 2,
        "truncated": False,
    }


def test_hidden_only_edit_preserves_a_released_planning_continuation(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    first = add(tmp_path, collection, item={"title": "first"}, why="capture intent")
    add(tmp_path, collection, item={"title": "hidden"}, why="capture intent")
    second = add(tmp_path, collection, item={"title": "second"}, why="capture intent")

    def allow_visible(path: str) -> bool:
        return not path.endswith("/hidden.md")

    page = query(tmp_path, collection, limit=1, sort_by="title", authorize_path=allow_visible)
    hidden_path = tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items" / "hidden.md"
    hidden_path.write_text(
        hidden_path.read_text(encoding="utf-8").replace("hidden", "changed"), encoding="utf-8"
    )
    continued = query(
        tmp_path,
        collection,
        limit=1,
        sort_by="title",
        continuation=page["continuation"],
        authorize_path=allow_visible,
    )

    assert page["rows"][0]["plan_id"] == first["plan_id"]
    assert continued["rows"][0]["plan_id"] == second["plan_id"]


def test_withheld_item_does_not_disclose_evidence_execution_or_history(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    manifest = _manifest().replace(
        "    health:\n      type: string\n",
        "    health:\n      type: string\n"
        "    progress_evidence:\n      type: array\n      items: {type: object}\n"
        "    execution:\n      type: array\n      items: {type: object}\n",
    )
    create_collection(tmp_path, collection, manifest, why="create planning collection")
    add(tmp_path, collection, item={"title": "visible"}, why="capture intent")
    hidden = add(
        tmp_path,
        collection,
        item={
            "title": "hidden",
            "progress_evidence": [
                {
                    "collection": "exomem://memory/5c252e6f-2639-4ee4-819a-fc9099200e1a",
                    "role": "completion",
                    "view": "private-view",
                }
            ],
            "execution": [{"kind": "repository", "ref": "private/repository"}],
        },
        why="capture private intent",
    )

    result = query(
        tmp_path,
        collection,
        include_agent_history=True,
        authorize_path=lambda path: not path.endswith("/hidden.md"),
    )

    serialized = str(result)
    assert "private-view" not in serialized
    assert "private/repository" not in serialized
    assert hidden["plan_id"] not in serialized


def test_partial_planning_view_refuses_mutation_before_publication(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query, update
    from exomem.structured_collections import CollectionError

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    visible = add(tmp_path, collection, item={"title": "visible"}, why="capture intent")
    add(tmp_path, collection, item={"title": "hidden"}, why="capture intent")
    current = query(tmp_path, collection)
    _write_planning_rule(tmp_path, 6)
    root = tmp_path / "Knowledge Base" / "_Governance"
    (root / "scopes" / "hidden.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5F03\n"
        "name: hidden\n"
        'paths: ["Planning/Work/Items/hidden.md"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "hidden.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5F04\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5F03"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )

    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        with pytest.raises(CollectionError, match="^COLLECTION_NOT_FOUND:"):
            update(
                tmp_path,
                collection,
                plan_id=visible["plan_id"],
                changes={"title": "changed"},
                expected_container_hash=current["snapshot"],
                expected_item_version=next(
                    row["item_version"]
                    for row in current["rows"]
                    if row["plan_id"] == visible["plan_id"]
                ),
                why="change visible intent",
            )


def test_withheld_planning_item_does_not_enter_query_reduction(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, manifest_path, _manifest(), why="create planning collection")
    add(tmp_path, manifest_path, item={"title": "withheld"}, why="capture intent")

    result = query(
        tmp_path,
        manifest_path,
        authorize_path=lambda path: not path.endswith(".md") or path.endswith("_collection.md"),
    )

    assert result["total_matched"] == 0
    assert result["rows"] == []


def test_withheld_parent_does_not_shape_authorized_hierarchy(tmp_path: Path) -> None:
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
        item={"title": "withheld outcome", "kind": "outcome"},
        plan_id=outcome_id,
        why="capture outcome",
    )
    add(
        tmp_path,
        manifest_path,
        item={
            "title": "visible initiative",
            "kind": "initiative",
            "parent": f"exomem://plan/2db90f18-70df-4e41-986e-2d7d7db1caca/{outcome_id}",
        },
        plan_id=initiative_id,
        why="capture initiative",
    )

    result = query(
        tmp_path,
        manifest_path,
        filters=[{"column": "plan_id", "op": "eq", "value": initiative_id}],
        hierarchy_mode="ancestors",
        authorize_path=lambda path: not path.endswith("/withheld outcome.md"),
    )

    assert [node["plan_id"] for node in result["hierarchy"]["nodes"]] == [initiative_id]
    assert result["hierarchy"]["edges"] == []


def test_query_refuses_an_authorized_semantically_invalid_direct_edit(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, query
    from exomem.structured_collections import CollectionError

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    add(tmp_path, collection, item={"title": "candidate"}, why="capture intent")
    item = tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items" / "candidate.md"
    item.write_text(
        item.read_text(encoding="utf-8").replace("status: candidate", "status: active"),
        encoding="utf-8",
    )

    with pytest.raises(CollectionError, match="INVALID_PLAN"):
        query(tmp_path, collection, lifecycle="all")


def test_planning_inspection_uses_its_exact_typed_egress_envelope(tmp_path: Path) -> None:
    from exomem.planning import create_collection, inspect

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, collection, _manifest(), why="create planning collection")

    result = inspect(tmp_path, collection)

    assert set(result) == {
        "kind",
        "report_only",
        "contract",
        "snapshot",
        "source_versions",
        "diagnostics",
        "audit",
        "saved_views",
        "presentation",
        "lifecycle_guards",
    }
    assert set(result["contract"]) == {
        "collection_id",
        "path",
        "title",
        "semantic_profile",
        "schema_version",
        "storage",
    }
    assert set(result["contract"]["storage"]) == {"strategy", "source", "format_version"}


def test_planning_query_refuses_an_invalid_post_assembly_egress_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import planning
    from exomem.structured_collections import CollectionError

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    planning.create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    planning.add(tmp_path, collection, item={"title": "intent"}, why="capture intent")
    monkeypatch.setattr(planning, "_hierarchy", lambda *_args, **_kwargs: {"unexpected": True})

    with pytest.raises(CollectionError, match="PLAN_RESPONSE_TOO_LARGE"):
        planning.query(tmp_path, collection, hierarchy_mode="descendants")
