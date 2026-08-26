from __future__ import annotations

import json
from pathlib import Path

import pytest


def _empty_vault(vault: Path) -> None:
    activity = vault / "Knowledge Base" / "log.md"
    activity.parent.mkdir(parents=True, exist_ok=True)
    activity.write_text("# Activity\n", encoding="utf-8")


def _files(vault: Path) -> dict[str, bytes]:
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file()
    }


def test_generic_client_can_discover_validate_create_inspect_and_append_from_empty_vault(
    tmp_path: Path,
) -> None:
    from exomem.commands import op_bootstrap, op_record_memory

    _empty_vault(tmp_path)

    bootstrap = op_bootstrap(tmp_path)
    assert bootstrap["records"]["agent_workflow"] == [
        "describe",
        "validate",
        "create",
        "inspect",
        "append",
    ]
    assert bootstrap["records"]["contract_route"] == {
        "tool": "record_memory",
        "arguments": {"action": "describe"},
    }
    assert "json_schema" not in json.dumps(bootstrap["records"])
    assert "manifest_text" not in json.dumps(bootstrap["records"])

    described = op_record_memory(tmp_path, action="describe")
    assert described["contract_version"] == 1
    assert described["manifest_filename"] == "_collection.md"
    assert described["closed_values"] == {
        "semantic_profile": ["planning", "records"],
        "collection_version": [1],
        "storage.strategy": ["dataset", "markdown-items", "markdown-log"],
        "storage.format_version": [1],
        "item_schema.fields.*.type": [
            "array",
            "boolean",
            "date",
            "datetime",
            "enum",
            "integer",
            "link",
            "number",
            "object",
            "string",
        ],
        "views.*.filters.*.op": [
            "contains",
            "eq",
            "exists",
            "gt",
            "gte",
            "icontains",
            "in",
            "lt",
            "lte",
            "missing",
            "ne",
            "nin",
            "startswith",
        ],
        "views.*.aggregate": [
            "count",
            "profile",
            "avg:<field>",
            "distinct:<field>",
            "group:<field>",
            "latest:<field>",
            "max:<field>",
            "min:<field>",
            "sum:<field>",
        ],
    }
    assert described["constraints"]["lifecycle"] == {
        "type": "string",
        "min_length": 1,
        "example": "active",
        "closed_enum": False,
    }
    assert described["json_schema"]["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    example = described["examples"]["minimal"]
    before_validate = _files(tmp_path)
    validation = op_record_memory(
        tmp_path,
        action="validate",
        manifest_path=example["manifest_path"],
        manifest_text=example["manifest_text"],
        scaffold=True,
    )
    assert validation["valid"] is True
    assert validation["would_create"] == [
        example["manifest_path"],
        "Knowledge Base/Records/Examples/Events/Events",
    ]
    assert validation["normalized_contract"]["semantic_profile"] == "records"
    assert _files(tmp_path) == before_validate

    created = op_record_memory(
        tmp_path,
        action="create",
        manifest_path=example["manifest_path"],
        manifest_text=example["manifest_text"],
        scaffold=True,
        why="create the example event collection",
    )
    assert created["outcome"] == "committed"

    inventory = op_record_memory(tmp_path, action="inspect")
    assert inventory["kind"] == "records_inventory"
    assert inventory["collections"] == [
        {
            "collection_id": created["collection_id"],
            "title": "Observed events",
            "manifest_path": example["manifest_path"],
            "semantic_profile": "records",
            "lifecycle": "active",
            "storage_strategy": "markdown-items",
            "natural_key": ["occurred_on", "label"],
        }
    ]
    assert inventory["legacy_trackers"] == []

    inspected = op_record_memory(
        tmp_path,
        action="inspect",
        collection=example["manifest_path"],
    )
    appended = op_record_memory(
        tmp_path,
        action="append",
        collection=example["manifest_path"],
        item=example["append_item"],
        item_key="11111111-1111-4111-8111-111111111111",
        expected_container_hash=inspected["snapshot"],
        why="record the observed event",
    )
    assert appended["outcome"] == "committed"


def test_describe_examples_are_generic_parseable_and_cover_nested_laboratory_values(
    tmp_path: Path,
) -> None:
    from exomem.commands import op_record_memory
    from exomem.structured_collections import parse_manifest_bytes

    _empty_vault(tmp_path)
    described = op_record_memory(tmp_path, action="describe")

    for example in described["examples"].values():
        manifest = parse_manifest_bytes(
            tmp_path,
            example["manifest_path"],
            example["manifest_text"].encode("utf-8"),
        )
        manifest.schema.validate(example["append_item"])

    laboratory = described["examples"]["laboratory_panel"]
    text = laboratory["manifest_text"]
    serialized = json.dumps(laboratory)
    assert "patient" not in text.lower()
    assert "diagnos" not in text.lower()
    assert all(
        token in serialized
        for token in (
            "panel_on",
            "source",
            "specimen_id",
            "analytes",
            "reported_value",
            "comparator",
            "unit",
            "reference_range",
            "cancelled",
            "qualifier",
        )
    )
    assert laboratory["append_item"]["analytes"][0]["comparator"] == "<"


def test_validate_is_read_only_requires_no_reason_and_rejects_mutation_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import record_memory as subject
    from exomem.cli_ops import OpError

    _empty_vault(tmp_path)
    example = subject.record_memory(tmp_path, action="describe")["examples"]["minimal"]

    monkeypatch.setattr(
        subject.records.writer_lease,
        "active_manager",
        lambda: (_ for _ in ()).throw(AssertionError("validate contacted writer authority")),
    )
    result = subject.record_memory(
        tmp_path,
        action="validate",
        manifest_path=example["manifest_path"],
        manifest_text=example["manifest_text"],
    )
    assert result["valid"] is True

    with pytest.raises(OpError, match="^INVALID_RECORD_ARGUMENTS:"):
        subject.record_memory(
            tmp_path,
            action="validate",
            manifest_path=example["manifest_path"],
            manifest_text=example["manifest_text"],
            why="this is forbidden on read-only validation",
        )


@pytest.mark.parametrize(
    ("replacement", "code", "field", "received", "allowed", "example"),
    [
        (
            "semantic_profile: measurements",
            "UNSUPPORTED_COLLECTION_PROFILE",
            "semantic_profile",
            "measurements",
            ["planning", "records"],
            "semantic_profile: records",
        ),
        (
            "collection_version:",
            "UNSUPPORTED_COLLECTION_VERSION",
            "collection_version",
            None,
            [1],
            "collection_version: 1",
        ),
        (
            "strategy: sqlite",
            "UNSUPPORTED_STORAGE_STRATEGY",
            "storage.strategy",
            "sqlite",
            ["dataset", "markdown-items", "markdown-log"],
            "strategy: markdown-items",
        ),
    ],
)
def test_manifest_closed_value_errors_are_actionable(
    tmp_path: Path,
    replacement: str,
    code: str,
    field: str,
    received: object,
    allowed: list[object],
    example: str,
) -> None:
    from exomem.cli_ops import OpError, error_dict
    from exomem.commands import op_record_memory

    _empty_vault(tmp_path)
    described = op_record_memory(tmp_path, action="describe")
    base = described["examples"]["minimal"]
    if field == "semantic_profile":
        manifest_text = base["manifest_text"].replace("semantic_profile: records", replacement)
    elif field == "collection_version":
        manifest_text = base["manifest_text"].replace("collection_version: 1", replacement)
    else:
        manifest_text = base["manifest_text"].replace("strategy: markdown-items", replacement)

    with pytest.raises(OpError) as excinfo:
        op_record_memory(
            tmp_path,
            action="validate",
            manifest_path=base["manifest_path"],
            manifest_text=manifest_text,
        )

    assert error_dict(excinfo.value) == {
        "code": code,
        "message": excinfo.value.message,
        "remediation": excinfo.value.remediation,
        "ok": False,
        "error_code": code,
        "field": field,
        "received": received,
        "allowed": allowed,
        "example": example,
    }


def test_actionable_error_details_remain_json_serializable_for_yaml_dates(
    tmp_path: Path,
) -> None:
    from exomem.cli_ops import OpError, error_dict
    from exomem.commands import op_record_memory

    _empty_vault(tmp_path)
    example = op_record_memory(tmp_path, action="describe")["examples"]["minimal"]
    manifest_text = example["manifest_text"].replace("type: collection", "type: 2026-01-01")

    with pytest.raises(OpError) as excinfo:
        op_record_memory(
            tmp_path,
            action="validate",
            manifest_path=example["manifest_path"],
            manifest_text=manifest_text,
        )

    payload = error_dict(excinfo.value)
    assert payload["received"] == "2026-01-01"
    json.dumps(payload)
    str(excinfo.value)


def test_validate_rejects_markdown_log_heading_fields_missing_from_item_schema(
    tmp_path: Path,
) -> None:
    from exomem.cli_ops import OpError
    from exomem.commands import op_record_memory

    _empty_vault(tmp_path)
    manifest_path = "Knowledge Base/Records/Examples/Training/_collection.md"
    manifest_text = (Path(__file__).parent / "fixtures/records/x3/_collection.md").read_text(
        encoding="utf-8"
    ).replace(
        "      - name: title\n        type: string",
        "      - name: undeclared_title\n        type: string",
    )

    with pytest.raises(OpError, match="^INVALID_STORAGE_DESCRIPTOR:"):
        op_record_memory(
            tmp_path,
            action="validate",
            manifest_path=manifest_path,
            manifest_text=manifest_text,
        )


def test_manifest_json_schema_does_not_reject_parser_accepted_extension_fields(
    tmp_path: Path,
) -> None:
    import yaml
    from jsonschema import validate

    from exomem.commands import op_record_memory
    from exomem.structured_collections import parse_manifest_bytes

    _empty_vault(tmp_path)
    described = op_record_memory(tmp_path, action="describe")
    example = described["examples"]["minimal"]
    manifest_text = example["manifest_text"].replace(
        "      type: string\n      required: true\n    source:",
        "      type: string\n      required: true\n      description: Human label\n    source:",
    )
    parse_manifest_bytes(tmp_path, example["manifest_path"], manifest_text.encode("utf-8"))
    frontmatter = yaml.safe_load(manifest_text.split("---", 2)[1])

    validate(frontmatter, described["json_schema"])


def test_collectionless_inspect_inventories_first_class_and_legacy_without_item_contents(
    tmp_path: Path,
) -> None:
    from exomem.commands import op_record_memory

    _empty_vault(tmp_path)
    described = op_record_memory(tmp_path, action="describe")
    example = described["examples"]["minimal"]
    op_record_memory(
        tmp_path,
        action="create",
        manifest_path=example["manifest_path"],
        manifest_text=example["manifest_text"],
        why="create inventory fixture",
    )
    tracker = tmp_path / "Knowledge Base" / "Records" / "Legacy" / "Log.md"
    tracker.parent.mkdir(parents=True)
    tracker.write_text("---\ntype: tracker\n---\n# Private row\n- secret value\n", encoding="utf-8")

    inventory = op_record_memory(tmp_path, action="inspect")

    assert len(inventory["collections"]) == 1
    assert inventory["legacy_trackers"] == [
        {
            "path": "Knowledge Base/Records/Legacy/Log.md",
            "inspect_only": True,
        }
    ]
    assert "secret value" not in json.dumps(inventory)


def test_collectionless_inspect_authorizes_candidates_before_parsing_or_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import record_governance
    from exomem.commands import op_record_memory

    _empty_vault(tmp_path)
    example = op_record_memory(tmp_path, action="describe")["examples"]["minimal"]
    op_record_memory(
        tmp_path,
        action="create",
        manifest_path=example["manifest_path"],
        manifest_text=example["manifest_text"],
        why="create the visible inventory fixture",
    )
    hidden_manifest = (
        tmp_path / "Knowledge Base" / "Records" / "Hidden" / "_collection.md"
    )
    hidden_manifest.parent.mkdir(parents=True)
    hidden_manifest.write_text("not valid frontmatter", encoding="utf-8")
    hidden_tracker = hidden_manifest.parent / "Secret Log.md"
    hidden_tracker.write_text(
        "---\ntype: tracker\n---\n# Secret tracker title\n",
        encoding="utf-8",
    )

    original = record_governance._authorize

    def authorize(root: Path, relative: str, *, receipt: bool = False) -> bool:
        if "/Hidden/" in relative:
            return False
        return original(root, relative, receipt=receipt)

    monkeypatch.setattr(record_governance, "_authorize", authorize)

    inventory = op_record_memory(tmp_path, action="inspect")

    serialized = json.dumps(inventory)
    assert len(inventory["collections"]) == 1
    assert inventory["legacy_trackers"] == []
    assert "Hidden" not in serialized
    assert "Secret tracker title" not in serialized


def test_denied_manifests_do_not_trip_authorized_discovery_limit(tmp_path: Path) -> None:
    from exomem.structured_collections import discover_collections

    _empty_vault(tmp_path)
    for index in range(3):
        manifest = (
            tmp_path
            / "Knowledge Base"
            / "Records"
            / f"Hidden {index}"
            / "_collection.md"
        )
        manifest.parent.mkdir(parents=True)
        manifest.write_text("not valid frontmatter", encoding="utf-8")

    assert discover_collections(
        tmp_path,
        authorize_path=lambda _path: False,
        max_candidates=2,
        max_raw_candidates=2,
    ) == ()
