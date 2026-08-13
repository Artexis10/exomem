from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from exomem import commands, record_memory
from exomem.cli_ops import OpError


def _record_command():  # noqa: ANN202
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == "record_memory")


def test_exact_child_and_refresh_are_discoverable_on_the_single_public_command() -> None:
    command = _record_command()
    params = {parameter.name: parameter for parameter in command.params}

    assert "expand_child" in inspect.signature(commands.op_record_memory).parameters
    assert "refresh_presentation" in inspect.signature(commands.op_record_memory).parameters
    assert "declared child" in params["expand_child"].help
    assert "managed Markdown" in params["refresh_presentation"].help
    assert "render" not in {candidate.name for candidate in commands.PRODUCT_COMMANDS}
    assert "migration" not in {candidate.name for candidate in commands.PRODUCT_COMMANDS}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "inspect", "expand_child": "observations"},
        {"action": "append", "refresh_presentation": True},
        {"action": "query", "collection": "x", "refresh_presentation": True},
        {
            "action": "update",
            "collection": "x",
            "item_key": "id",
            "changes": {},
            "expected_container_hash": "a",
            "expected_item_version": "b",
            "why": "reason",
            "refresh_presentation": False,
        },
    ],
)
def test_selector_leakage_and_false_refresh_refuse_before_collection_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object]
) -> None:
    opened = False

    def opening(*_args, **_kwargs):  # noqa: ANN202
        nonlocal opened
        opened = True
        raise AssertionError("collection was opened")

    monkeypatch.setattr(record_memory.record_governance, "resolve_collection", opening)
    monkeypatch.setattr(record_memory.record_governance, "resolve_collection_for_mutation", opening)
    with pytest.raises(OpError, match="INVALID_RECORD_ARGUMENTS"):
        record_memory.record_memory(tmp_path, **kwargs)  # type: ignore[arg-type]
    assert opened is False


def test_mcp_fixture_and_capability_contract_have_matching_bounded_arguments() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/mcp_tool_schemas.json").read_text(encoding="utf-8")
    )
    properties = fixture["record_memory"]["inputSchema"]["properties"]

    assert properties["expand_child"]["anyOf"][0] == {"type": "string"}
    assert "declared child" in properties["expand_child"]["description"]
    assert properties["refresh_presentation"]["anyOf"][0] == {"type": "boolean"}
    assert "managed Markdown" in properties["refresh_presentation"]["description"]
