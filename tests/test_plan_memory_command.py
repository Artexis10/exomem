from __future__ import annotations

from pathlib import Path

import pytest
from test_planning_mutation import _manifest

from exomem.cli_ops import OpError


def test_plan_memory_is_registered_as_a_product_command() -> None:
    from exomem.commands import product_commands_for

    names = {command.name for command in product_commands_for("mcp")}

    assert "plan_memory" in names


def test_plan_memory_exposes_exactly_the_nine_planning_actions() -> None:
    from exomem.plan_memory import ACTIONS

    assert ACTIONS == frozenset(
        {
            "inspect",
            "validate",
            "create",
            "query",
            "add",
            "update",
            "triage",
            "revise",
            "rebaseline",
        }
    )


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


@pytest.mark.parametrize(
    ("item", "expected_message"),
    (
        (
            {"title": "Invalid kind", "kind": "project"},
            "kind must be one of: area, outcome, initiative, work-item",
        ),
        (
            {"title": "Invalid area", "area": "not-a-plan-reference"},
            "area must be exomem://plan/<collection-uuid>/<plan-uuid>",
        ),
    ),
)
def test_plan_memory_add_exposes_bounded_planning_self_correction_guidance(
    tmp_path: Path, item: dict[str, str], expected_message: str
) -> None:
    from exomem import planning
    from exomem.plan_memory import plan_memory

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    planning.create_collection(tmp_path, collection, _manifest(), why="create planning collection")

    with pytest.raises(OpError) as raised:
        plan_memory(
            tmp_path,
            "add",
            collection=collection,
            item=item,
            why="attempt invalid Planning capture",
        )

    assert raised.value.code == "INVALID_PLAN"
    assert raised.value.message == expected_message


def test_plan_memory_validate_supports_exact_create_and_revision_forms(
    tmp_path: Path,
) -> None:
    from exomem import planning
    from exomem.plan_memory import plan_memory

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    candidate_path = "Knowledge Base/Planning/Candidate/_collection.md"

    create_validation = plan_memory(
        tmp_path,
        "validate",
        manifest_path=candidate_path,
        manifest_text=_manifest(),
    )

    assert create_validation["valid"] is True
    collection = "Knowledge Base/Planning/Work/_collection.md"
    planning.create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    current = (tmp_path / collection).read_text(encoding="utf-8")

    revision_validation = plan_memory(
        tmp_path,
        "validate",
        collection=collection,
        manifest_text=current,
    )

    assert revision_validation["valid"] is True
    assert set(revision_validation["lifecycle_guards"]) == {
        "expected_manifest_hash",
        "expected_container_hash",
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"manifest_text": _manifest()},
        {
            "collection": "Knowledge Base/Planning/Work/_collection.md",
            "manifest_path": "Knowledge Base/Planning/Work/_collection.md",
            "manifest_text": _manifest(),
        },
    ],
)
def test_plan_memory_validate_refuses_missing_or_mixed_selectors(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    from exomem.plan_memory import plan_memory

    with pytest.raises(OpError) as raised:
        plan_memory(tmp_path, "validate", **arguments)

    assert raised.value.code == "INVALID_PLAN_ARGUMENTS"


def test_plan_memory_revise_uses_exact_guards_and_a_terminal_planning_receipt(
    tmp_path: Path,
) -> None:
    from exomem import planning
    from exomem.mutation_terminal import valid_collection_receipt
    from exomem.plan_memory import plan_memory

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    planning.create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    current = (tmp_path / collection).read_text(encoding="utf-8")
    validation = plan_memory(
        tmp_path,
        "validate",
        collection=collection,
        manifest_text=current,
    )
    proposed = current.replace("title: Planning work", "title: Readable planning work", 1)

    receipt = plan_memory(
        tmp_path,
        "revise",
        collection=collection,
        manifest_text=proposed,
        expected_manifest_hash=validation["lifecycle_guards"]["expected_manifest_hash"],
        expected_container_hash=validation["lifecycle_guards"]["expected_container_hash"],
        why="make the collection title clearer",
    )

    assert receipt["operation"] == "revise"
    assert receipt["minimum_reader_version"] == 2
    assert valid_collection_receipt(receipt)


def test_plan_memory_read_and_mutation_actions_have_exact_lease_classification() -> None:
    from exomem.commands import invocation_is_read_only, product_commands_for

    command = next(
        command for command in product_commands_for("mcp") if command.name == "plan_memory"
    )

    for action in ("inspect", "validate", "query"):
        assert invocation_is_read_only(command, {"action": action}) is True
    for action in ("create", "add", "update", "triage", "revise", "rebaseline"):
        assert invocation_is_read_only(command, {"action": action}) is False


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
    arguments: dict[str, object] = {
        "collection": collection,
        "item": {"title": "inbox"},
        "why": "capture",
    }
    arguments[field] = value

    with pytest.raises(OpError, match="^INVALID_PLAN:"):
        plan_memory(tmp_path, "add", **arguments)


# Collection-targeted inspection is deliberate: inventory remains available
# through the collection discovery surface, while this finite action never
# guesses which governed Planning collection the caller meant.
def test_plan_inspect_requires_one_collection_selector(tmp_path: Path) -> None:
    from exomem.plan_memory import plan_memory

    with pytest.raises(OpError) as raised:
        plan_memory(tmp_path, "inspect")

    assert raised.value.code == "INVALID_PLAN_ARGUMENTS"


@pytest.mark.parametrize("action", ["query", "add", "update", "triage"])
def test_collection_dependent_actions_reject_a_missing_selector(
    tmp_path: Path, action: str
) -> None:
    from exomem.plan_memory import plan_memory

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
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


def test_plan_memory_update_names_the_unexpected_argument(tmp_path: Path) -> None:
    from exomem.plan_memory import plan_memory

    with pytest.raises(OpError) as raised:
        plan_memory(
            tmp_path,
            "update",
            collection="Knowledge Base/Planning/Work/_collection.md",
            plan_id="991acdd4-16b9-4396-8220-2cb37b7e8516",
            expected_container_hash="a" * 64,
            expected_item_version="b" * 64,
            why="update",
            changes={"status": "done"},
            item={"title": "not allowed here"},
        )

    assert raised.value.code == "INVALID_PLAN_ARGUMENTS"
    assert "unexpected for update: item" in raised.value.message
    assert "not allowed here" not in raised.value.message


def test_plan_memory_update_names_the_missing_argument(tmp_path: Path) -> None:
    from exomem.plan_memory import plan_memory

    with pytest.raises(OpError) as raised:
        plan_memory(
            tmp_path,
            "update",
            collection="Knowledge Base/Planning/Work/_collection.md",
            plan_id="991acdd4-16b9-4396-8220-2cb37b7e8516",
            expected_container_hash="a" * 64,
            why="update",
            changes={"status": "done"},
        )

    assert raised.value.code == "INVALID_PLAN_ARGUMENTS"
    assert "missing for update: expected_item_version" in raised.value.message
    assert "991acdd4-16b9-4396-8220-2cb37b7e8516" not in raised.value.message


def test_plan_memory_query_view_names_the_excluded_shaping_field(tmp_path: Path) -> None:
    from exomem.plan_memory import plan_memory

    with pytest.raises(OpError) as raised:
        plan_memory(
            tmp_path,
            "query",
            collection="Knowledge Base/Planning/Work/_collection.md",
            view="inbox",
            columns=["title"],
        )

    assert raised.value.code == "INVALID_PLAN_ARGUMENTS"
    assert "view excludes shaping fields: columns" in raised.value.message
    assert "inbox" not in raised.value.message
