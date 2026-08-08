from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def test_record_memory_exposes_only_the_five_declared_actions() -> None:
    from exomem import record_memory

    assert record_memory.ACTIONS == frozenset({"inspect", "create", "query", "append", "update"})


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"action": "inspect"}, "INVALID_RECORD_ARGUMENTS"),
        ({"action": "inspect", "collection": "x", "limit": 1}, "INVALID_RECORD_ARGUMENTS"),
        ({"action": "create", "manifest_path": "a", "manifest_text": "x", "why": "because", "scaffold": False, "limit": 1}, "INVALID_RECORD_ARGUMENTS"),
        ({"action": "query", "collection": "x", "view": "recent", "descending": False}, "INVALID_RECORD_ARGUMENTS"),
        ({"action": "append", "collection": "x", "item": {}, "why": "because", "sort_by": "date"}, "INVALID_RECORD_ARGUMENTS"),
        ({"action": "update", "collection": "x", "item_key": "id", "changes": {}, "expected_container_hash": "a", "expected_item_version": "b", "why": "because", "body": "no"}, "INVALID_RECORD_ARGUMENTS"),
        ({"action": "unknown"}, "INVALID_RECORD_ARGUMENTS"),
    ],
)
def test_record_memory_rejects_cross_action_arguments_before_defaults(
    tmp_path: Path, kwargs: dict[str, object], code: str
) -> None:
    from exomem.cli_ops import OpError
    from exomem.record_memory import record_memory

    with pytest.raises(OpError, match=f"^{code}:"):
        record_memory(tmp_path, **kwargs)


def test_record_memory_preserves_explicit_false_and_routes_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import record_memory as subject

    received: dict[str, object] = {}

    manifest = SimpleNamespace(semantic_profile="records")

    def append(root: Path, collection: object, **kwargs: object) -> dict[str, object]:
        received.update(root=root, collection=collection, **kwargs)
        return {"outcome": "committed"}

    monkeypatch.setattr(subject.records, "append_record", append)
    monkeypatch.setattr(
        subject.record_governance, "resolve_collection_for_mutation", lambda *_: manifest
    )

    result = subject.record_memory(
        tmp_path,
        action="append",
        collection="Records/Test/_collection.md",
        item={"name": "one"},
        item_key="key",
        expected_container_hash="hash",
        body="",
        why="test",
    )

    assert result == {"outcome": "committed"}
    assert received == {
        "root": tmp_path,
        "collection": manifest,
        "item": {"name": "one"},
        "item_key": "key",
        "expected_container_hash": "hash",
        "body": "",
        "why": "test",
    }


def test_record_memory_applies_the_query_limit_before_saved_view_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import query_data
    from exomem import record_memory as subject

    manifest = SimpleNamespace(semantic_profile="records")
    received: dict[str, object] = {}
    monkeypatch.setattr(subject.record_governance, "resolve_collection", lambda *_: manifest)

    def query(_root: Path, _manifest: object, **kwargs: object) -> object:
        received.update(kwargs)
        return object()

    monkeypatch.setattr(subject.record_governance, "query_collection", query)
    monkeypatch.setattr(
        subject.record_governance,
        "project_query_result",
        lambda result, resolved, **kwargs: {"result": result, "manifest": resolved, **kwargs},
    )

    result = subject.record_memory(tmp_path, action="query", collection="Records/Test/_collection.md", view="recent")

    assert received["limit"] == query_data.DEFAULT_LIMIT
    assert result["manifest"] is manifest


def test_record_memory_keeps_explicit_false_action_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import record_memory as subject

    create_received: dict[str, object] = {}
    query_received: dict[str, object] = {}
    manifest = SimpleNamespace(semantic_profile="records")
    monkeypatch.setattr(
        subject.records,
        "create_collection",
        lambda root, path, text, **kwargs: create_received.update(
            root=root, path=path, text=text, **kwargs
        )
        or {"outcome": "committed"},
    )
    monkeypatch.setattr(subject.record_governance, "resolve_collection", lambda *_: manifest)
    monkeypatch.setattr(
        subject.record_governance,
        "query_collection",
        lambda _root, _manifest, **kwargs: query_received.update(kwargs) or object(),
    )
    monkeypatch.setattr(subject.record_governance, "project_query_result", lambda *_args, **_kwargs: {})

    subject.record_memory(
        tmp_path,
        action="create",
        manifest_path="Knowledge Base/Records/Test/_collection.md",
        manifest_text=_planning_manifest().replace("semantic_profile: planning", "semantic_profile: records"),
        why="test",
        scaffold=False,
    )
    subject.record_memory(
        tmp_path,
        action="query",
        collection="Knowledge Base/Records/Test/_collection.md",
        descending=False,
        limit=7,
    )

    assert create_received["scaffold"] is False
    assert query_received["descending"] is False
    assert query_received["limit"] == 7


def test_record_memory_inspects_a_direct_legacy_tracker(tmp_path: Path) -> None:
    from exomem.record_memory import record_memory

    tracker = tmp_path / "Knowledge Base" / "Records" / "Training Log.md"
    tracker.parent.mkdir(parents=True)
    tracker.write_text("---\ntype: tracker\n---\n# Training Log\n", encoding="utf-8")

    result = record_memory(
        tmp_path, action="inspect", collection="Knowledge Base/Records/Training Log.md"
    )

    assert result == {
        "kind": "legacy_tracker",
        "report_only": True,
        "contract": None,
        "legacy": {
            "collection_id": "legacy-953e9265-ea58-5981-93bd-31f7aaa18afa",
            "path": "Knowledge Base/Records/Training Log.md",
            "inspect_only": True,
        },
        "snapshot": None,
        "source_versions": [],
        "diagnostics": [],
        "audit": {"status": "not_applicable", "gaps": []},
        "saved_views": [],
    }


def _planning_manifest() -> str:
    return """---
type: collection
exomem_id: 2db90f18-70df-4e41-986e-2d7d7db1caca
title: Planning work
semantic_profile: planning
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [title]
  fields:
    title:
      type: string
---
"""


@pytest.mark.parametrize("action", ("query", "append", "update"))
def test_record_memory_refuses_planning_profile_for_noninspection_actions(
    tmp_path: Path, action: str
) -> None:
    from exomem.cli_ops import OpError
    from exomem.record_memory import record_memory

    manifest = tmp_path / "Knowledge Base" / "Planning" / "Work" / "_collection.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(_planning_manifest(), encoding="utf-8")
    kwargs: dict[str, object] = {
        "action": action,
        "collection": manifest.relative_to(tmp_path).as_posix(),
    }
    if action == "append":
        kwargs.update(item={"title": "future work"}, why="capture an intended outcome")
    elif action == "update":
        kwargs.update(
            item_key="work-1",
            changes={"title": "future work"},
            expected_container_hash="a" * 64,
            expected_item_version="b" * 64,
            why="correct an intended outcome",
        )

    with pytest.raises(OpError, match="^RECORDS_PROFILE_REQUIRED:"):
        record_memory(tmp_path, **kwargs)  # type: ignore[arg-type]


def test_record_memory_refuses_creating_a_planning_profile(tmp_path: Path) -> None:
    from exomem.cli_ops import OpError
    from exomem.record_memory import record_memory

    with pytest.raises(OpError, match="^RECORDS_PROFILE_REQUIRED:"):
        record_memory(
            tmp_path,
            action="create",
            manifest_path="Knowledge Base/Planning/Work/_collection.md",
            manifest_text=_planning_manifest(),
            why="capture intended work",
        )


def test_record_memory_registry_is_selector_gated_and_conservatively_annotated() -> None:
    from exomem.commands import commands_for, invocation_is_read_only, product_commands_for

    command = next(command for command in product_commands_for("mcp") if command.name == "record_memory")
    canonical = next(command for command in commands_for("mcp") if command.name == "record_memory")

    assert command.product_actions == ("ask", "review", "save", "update")
    assert canonical.tier == 1
    assert canonical.product_surface == "primary"
    assert canonical.product_actions == command.product_actions
    assert command.mcp_annotations.readOnlyHint is False
    assert command.mcp_annotations.destructiveHint is True
    assert invocation_is_read_only(command, {"action": "inspect", "collection": "x"}) is True
    assert invocation_is_read_only(command, {"action": "query", "collection": "x"}) is True
    assert invocation_is_read_only(command, {"action": "append", "collection": "x"}) is False


@pytest.mark.parametrize(
    "code",
    ["STALE_RECORD", "STALE_RECORD_SNAPSHOT", "RECORD_ID_CONFLICT", "CREATE_ONLY_CONFLICT"],
)
def test_record_conflicts_are_rest_conflicts(code: str) -> None:
    from exomem.cli_ops import http_status_for

    assert http_status_for(code) == 409


def test_record_query_translates_query_data_errors_to_the_public_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import query_data
    from exomem import record_memory as subject
    from exomem.cli_ops import OpError, error_dict

    monkeypatch.setattr(
        subject.record_governance,
        "resolve_collection",
        lambda *_: SimpleNamespace(semantic_profile="records"),
    )

    def reject(*_args: object, **_kwargs: object) -> object:
        raise query_data.QueryDataError("BAD_OP", "unknown filter op")

    monkeypatch.setattr(subject.record_governance, "query_collection", reject)

    with pytest.raises(OpError) as excinfo:
        subject.record_memory(tmp_path, action="query", collection="Records/Test/_collection.md")

    assert error_dict(excinfo.value)["code"] == "BAD_OP"
