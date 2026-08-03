from __future__ import annotations

from pathlib import Path

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

    def append(root: Path, collection: str, **kwargs: object) -> dict[str, object]:
        received.update(root=root, collection=collection, **kwargs)
        return {"outcome": "committed"}

    monkeypatch.setattr(subject.records, "append_record", append)

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
        "collection": "Records/Test/_collection.md",
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

    manifest = object()
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
    manifest = object()
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
        manifest_text="---\n---\n",
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

    monkeypatch.setattr(subject.record_governance, "resolve_collection", lambda *_: object())

    def reject(*_args: object, **_kwargs: object) -> object:
        raise query_data.QueryDataError("BAD_OP", "unknown filter op")

    monkeypatch.setattr(subject.record_governance, "query_collection", reject)

    with pytest.raises(OpError) as excinfo:
        subject.record_memory(tmp_path, action="query", collection="Records/Test/_collection.md")

    assert error_dict(excinfo.value)["code"] == "BAD_OP"
