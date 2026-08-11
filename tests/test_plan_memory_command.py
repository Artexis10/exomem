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
