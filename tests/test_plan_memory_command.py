from __future__ import annotations

from pathlib import Path

import pytest
from test_planning_mutation import _manifest

from exomem.cli_ops import OpError


def test_plan_memory_is_registered_as_a_product_command() -> None:
    from exomem.commands import product_commands_for

    names = {command.name for command in product_commands_for("mcp")}

    assert "plan_memory" in names


def test_plan_memory_exposes_exactly_the_six_planning_actions() -> None:
    from exomem.plan_memory import ACTIONS

    assert ACTIONS == frozenset({"inspect", "create", "query", "add", "update", "triage"})


def test_plan_memory_create_uses_shared_preflight_for_planning(tmp_path: Path) -> None:
    from exomem.plan_memory import plan_memory

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")

    created = plan_memory(
        tmp_path,
        "create",
        manifest_path="Knowledge Base/Planning/Work/_collection.md",
        manifest_text=_manifest(),
        why="create planning collection",
    )

    assert created["operation"] == "create"


def test_plan_memory_saved_view_composes_with_hierarchy_controls(tmp_path: Path) -> None:
    from exomem import planning
    from exomem.plan_memory import plan_memory

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    planning.create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    plan_memory(tmp_path, "add", collection=collection, item={"title": "inbox"}, why="capture")

    result = plan_memory(
        tmp_path,
        "query",
        collection=collection,
        view="inbox",
        hierarchy_mode="descendants",
        hierarchy_depth=1,
        hierarchy_limit=1,
    )

    assert result["hierarchy"]["mode"] == "descendants"
    assert result["hierarchy"]["max_depth"] == 1
    assert result["hierarchy"]["max_nodes"] == 1


@pytest.mark.parametrize("field, value", [("why", "bad\ud800why"), ("body", "bad\ud800body")])
def test_plan_memory_mutation_text_surrogates_are_bounded_planning_errors(
    tmp_path: Path, field: str, value: str
) -> None:
    from exomem import planning
    from exomem.plan_memory import plan_memory

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    planning.create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    arguments: dict[str, object] = {"collection": collection, "item": {"title": "inbox"}, "why": "capture"}
    arguments[field] = value

    with pytest.raises(OpError, match="^INVALID_PLAN:"):
        plan_memory(tmp_path, "add", **arguments)


# --- Planning inventory before a selector is known (design D4) ------------------


def _planning_vault(tmp_path: Path) -> None:
    (tmp_path / "Knowledge Base").mkdir(exist_ok=True)
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")


def _second_manifest() -> str:
    return _manifest().replace(
        "exomem_id: 2db90f18-70df-4e41-986e-2d7d7db1caca", "exomem_id: 7c1c9d02-4a54-4a2f-9d61-05f1f4d2a0b1"
    ).replace("title: Planning work", "title: Planning goals")


def _write_planning_release_rules(vault: Path, *, hidden: str) -> None:
    """Planning is visible to the external audience except one withheld subtree."""
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "planning.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nname: Planning\n"
        'paths: ["Planning/**"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "external.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 6\n',
        encoding="utf-8",
    )
    (root / "scopes" / "blocked.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC5\nname: blocked\n"
        f'paths: ["{hidden}"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "blocked.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC6\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FC5"]\naudience: external\nceiling: 0\n',
        encoding="utf-8",
    )


def test_plan_inspect_without_a_collection_lists_the_planning_inventory(tmp_path: Path) -> None:
    """A fresh session has no selector, and guessing one is how duplicates start.

    Records already answers "what is here?" before a collection is known; Planning
    made the same question unanswerable, so an agent either asked the user for a
    path or captured into a collection it invented.
    """
    from record_fixtures import copy_x3_fixture

    from exomem import planning
    from exomem.plan_memory import plan_memory

    _planning_vault(tmp_path)
    copy_x3_fixture(tmp_path)
    work = planning.create_collection(
        tmp_path, "Knowledge Base/Planning/Work/_collection.md", _manifest(), why="create work"
    )
    goals = planning.create_collection(
        tmp_path, "Knowledge Base/Planning/Goals/_collection.md", _second_manifest(), why="create goals"
    )

    inventory = plan_memory(tmp_path, "inspect")

    assert inventory["kind"] == "planning_inventory"
    assert inventory["report_only"] is True
    assert [entry["manifest_path"] for entry in inventory["collections"]] == sorted(
        ["Knowledge Base/Planning/Work/_collection.md", "Knowledge Base/Planning/Goals/_collection.md"]
    )
    assert {entry["collection_id"] for entry in inventory["collections"]} == {
        work["collection_id"],
        goals["collection_id"],
    }
    assert all(entry["semantic_profile"] == "planning" for entry in inventory["collections"])
    assert all(entry["natural_key"] == ["title"] for entry in inventory["collections"])


def test_the_planning_inventory_carries_no_records_layer_keys(tmp_path: Path) -> None:
    """`legacy_trackers` is a Records artifact; Planning has no such thing.

    Shipping the key empty invites a reader to conclude the sweep ran and found
    nothing, when in fact Planning never sweeps. Absent says the true thing.
    """
    from exomem import planning
    from exomem.plan_memory import plan_memory

    _planning_vault(tmp_path)
    planning.create_collection(
        tmp_path, "Knowledge Base/Planning/Work/_collection.md", _manifest(), why="create work"
    )

    inventory = plan_memory(tmp_path, "inspect")

    assert "legacy_trackers" not in inventory
    assert "legacy_trackers" not in inventory["truncated"]
    assert inventory["truncated"] == {"collections": False}


def test_the_records_inventory_still_carries_the_legacy_tracker_sweep(tmp_path: Path) -> None:
    """The other half of the same decision: Records does sweep, so it reports."""
    from exomem.record_governance import inventory_collections

    _planning_vault(tmp_path)

    inventory = inventory_collections(tmp_path, semantic_profile="records")

    assert inventory["legacy_trackers"] == []
    assert inventory["truncated"] == {"collections": False, "legacy_trackers": False}


def test_the_planning_inventory_creates_nothing(tmp_path: Path) -> None:
    from exomem import planning
    from exomem.plan_memory import plan_memory

    _planning_vault(tmp_path)
    planning.create_collection(
        tmp_path, "Knowledge Base/Planning/Work/_collection.md", _manifest(), why="create work"
    )
    before = sorted(path.as_posix() for path in tmp_path.rglob("*"))

    plan_memory(tmp_path, "inspect")

    assert sorted(path.as_posix() for path in tmp_path.rglob("*")) == before


def test_the_planning_inventory_omits_a_withheld_manifest(tmp_path: Path) -> None:
    from exomem import planning
    from exomem.governance.principal import RequestPrincipal, request_scope
    from exomem.plan_memory import plan_memory

    _planning_vault(tmp_path)
    planning.create_collection(
        tmp_path, "Knowledge Base/Planning/Work/_collection.md", _manifest(), why="create work"
    )
    planning.create_collection(
        tmp_path, "Knowledge Base/Planning/Goals/_collection.md", _second_manifest(), why="create goals"
    )
    _write_planning_release_rules(tmp_path, hidden="Planning/Goals/**")

    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        inventory = plan_memory(tmp_path, "inspect")

    assert [entry["manifest_path"] for entry in inventory["collections"]] == [
        "Knowledge Base/Planning/Work/_collection.md"
    ]


@pytest.mark.parametrize("action", ["query", "add", "update", "triage"])
def test_the_inventory_form_does_not_widen_the_other_planning_actions(
    tmp_path: Path, action: str
) -> None:
    from exomem.plan_memory import plan_memory

    _planning_vault(tmp_path)
    arguments: dict[str, object] = {
        "query": {},
        "add": {"item": {"title": "intent"}, "why": "capture"},
        "update": {
            "plan_id": "991acdd4-16b9-4396-8220-2cb37b7e8516",
            "expected_container_hash": "a" * 64,
            "expected_item_version": "b" * 64,
            "why": "update",
        },
        "triage": {
            "plan_id": "991acdd4-16b9-4396-8220-2cb37b7e8516",
            "expected_container_hash": "a" * 64,
            "expected_item_version": "b" * 64,
            "transition": {"status": "planned"},
            "why": "triage",
        },
    }[action]

    with pytest.raises(OpError) as raised:
        plan_memory(tmp_path, action, **arguments)

    assert raised.value.code == "INVALID_PLAN_ARGUMENTS"
