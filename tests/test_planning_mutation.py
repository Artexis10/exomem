from __future__ import annotations

from pathlib import Path

import pytest

from exomem.governance.principal import RequestPrincipal, request_scope
from exomem.structured_collections import CollectionError


def _manifest() -> str:
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
  source: Items
  format_version: 1
item_schema:
  natural_key: [title]
  fields:
    title:
      type: string
      required: true
    kind:
      type: string
    status:
      type: string
    lifecycle:
      type: string
    priority:
      type: string
    commitment:
      type: string
    horizon:
      type: string
    health:
      type: string
    area:
      type: string
    parent:
      type: string
---
"""


def test_planning_create_uses_the_shared_guarded_writer_and_planning_receipt(
    tmp_path: Path,
) -> None:
    from exomem.planning import create_collection

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")

    receipt = create_collection(
        tmp_path,
        "Knowledge Base/Planning/Work/_collection.md",
        _manifest(),
        why="capture intended work",
    )

    assert receipt["_plan_receipt"] == "exomem.planning-mutation"
    assert receipt["operation"] == "create"
    assert (tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items").is_dir()


def test_planning_create_gates_candidate_path_before_parsing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import planning

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    parsed = False

    def refuse_candidate(*_args: object) -> None:
        raise CollectionError("COLLECTION_NOT_FOUND", "collection was not found")

    def fail_if_parsed(*_args: object, **_kwargs: object) -> object:
        nonlocal parsed
        parsed = True
        raise AssertionError("Planning parsed caller bytes before candidate admission")

    monkeypatch.setattr(planning.record_governance, "require_candidate_manifest_visibility", refuse_candidate)
    monkeypatch.setattr(planning.collections, "parse_manifest_bytes", fail_if_parsed)

    with pytest.raises(CollectionError, match="^COLLECTION_NOT_FOUND:"):
        planning.create_collection(
            tmp_path,
            "Knowledge Base/Planning/Hidden/_collection.md",
            "---\nnot: [valid\n---\n",
            why="hidden candidate",
        )

    assert not parsed


def test_planning_create_hides_malformed_candidate_bytes_like_a_missing_collection(
    tmp_path: Path,
) -> None:
    from exomem import planning

    root = tmp_path / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True)
    (root / "rules").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    (root / "scopes" / "planning.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        "name: Hidden planning\n"
        'paths: ["Planning/**"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "external.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
        "audience: external\n"
        "ceiling: 0\n",
        encoding="utf-8",
    )
    malformed = "---\nnot: [valid\n---\n"
    outcomes: list[tuple[str, str]] = []

    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        for path in (
            "Knowledge Base/Planning/Hidden/_collection.md",
            "Knowledge Base/Planning/Missing/_collection.md",
        ):
            with pytest.raises(CollectionError) as raised:
                planning.create_collection(tmp_path, path, malformed, why="hidden candidate")
            outcomes.append((raised.value.code, raised.value.reason))

    assert outcomes == [("COLLECTION_NOT_FOUND", "collection was not found")] * 2


def test_planning_add_applies_capture_defaults_and_writes_a_plan_item(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, manifest_path, _manifest(), why="create planning collection")

    receipt = add(
        tmp_path,
        manifest_path,
        item={"title": "Keep this for later"},
        plan_id="991acdd4-16b9-4396-8220-2cb37b7e8516",
        why="capture future work",
    )

    item = (
        tmp_path
        / "Knowledge Base"
        / "Planning"
        / "Work"
        / "Items"
        / "991acdd4-16b9-4396-8220-2cb37b7e8516.md"
    ).read_text(encoding="utf-8")
    assert receipt["operation"] == "add"
    assert receipt["plan_id"] == "991acdd4-16b9-4396-8220-2cb37b7e8516"
    assert "status: candidate" in item
    assert "# exomem-plan-audit:" in item


def test_planning_update_requires_current_hashes_and_replaces_authored_properties(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, update

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, manifest_path, _manifest(), why="create planning collection")
    added = add(
        tmp_path,
        manifest_path,
        item={"title": "Keep this for later"},
        plan_id="991acdd4-16b9-4396-8220-2cb37b7e8516",
        why="capture future work",
    )

    receipt = update(
        tmp_path,
        manifest_path,
        plan_id="991acdd4-16b9-4396-8220-2cb37b7e8516",
        changes={"priority": "high"},
        expected_container_hash=added["after_container_hash"],
        expected_item_version=added["after_item_hash"],
        why="prioritize intended work",
    )

    assert receipt["operation"] == "update"
    item = (
        tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items" / "991acdd4-16b9-4396-8220-2cb37b7e8516.md"
    ).read_text(encoding="utf-8")
    assert "priority: high" in item
    assert item.count("# exomem-plan-audit:") == 1
    assert "exomem-record-audit" not in item


def test_planning_update_deletes_optional_values_and_replaces_complete_body(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, update

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, manifest_path, _manifest(), why="create planning collection")
    added = add(
        tmp_path,
        manifest_path,
        item={"title": "Keep this for later"},
        plan_id="991acdd4-16b9-4396-8220-2cb37b7e8516",
        body="old body",
        why="capture future work",
    )

    update(
        tmp_path,
        manifest_path,
        plan_id="991acdd4-16b9-4396-8220-2cb37b7e8516",
        changes={"health": None},
        body="complete new body",
        expected_container_hash=added["after_container_hash"],
        expected_item_version=added["after_item_hash"],
        why="replace authored intent",
    )

    item = (
        tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items" / "991acdd4-16b9-4396-8220-2cb37b7e8516.md"
    ).read_text(encoding="utf-8")
    assert "health:" not in item
    assert item.endswith("\ncomplete new body")


def test_planning_extension_strings_reject_surrogates_before_rendering(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection
    from exomem.structured_collections import CollectionError

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    manifest = _manifest().replace(
        "    health:\n      type: string\n",
        "    health:\n      type: string\n    domain:\n      type: string\n",
    )
    create_collection(tmp_path, manifest_path, manifest, why="create planning collection")

    with pytest.raises(CollectionError, match="^INVALID_PLAN:"):
        add(
            tmp_path,
            manifest_path,
            item={"title": "intent", "domain": "bad\ud800text"},
            why="capture intent",
        )


def test_planning_triage_allows_only_explicit_transition_fields(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, triage

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, manifest_path, _manifest(), why="create planning collection")
    added = add(
        tmp_path,
        manifest_path,
        item={"title": "Keep this for later"},
        plan_id="991acdd4-16b9-4396-8220-2cb37b7e8516",
        why="capture future work",
    )

    receipt = triage(
        tmp_path,
        manifest_path,
        plan_id="991acdd4-16b9-4396-8220-2cb37b7e8516",
        transition={"status": "planned", "commitment": "considering", "horizon": "quarter"},
        expected_container_hash=added["after_container_hash"],
        expected_item_version=added["after_item_hash"],
        why="triage intended work",
    )

    assert receipt["operation"] == "triage"


def test_planning_inspection_reports_a_direct_edit_without_repairing_it(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, inspect

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, manifest_path, _manifest(), why="create planning collection")
    add(tmp_path, manifest_path, item={"title": "Keep this for later"}, why="capture future work")
    item = next((tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items").glob("*.md"))
    original = item.read_text(encoding="utf-8")
    item.write_text(original.replace("priority: none", "priority: high"), encoding="utf-8")

    result = inspect(tmp_path, manifest_path)

    assert result["audit"]["status"] == "gap"
    assert item.read_text(encoding="utf-8") == original.replace("priority: none", "priority: high")


def test_planning_update_refuses_crossing_the_area_boundary(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, update

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, collection, _manifest(), why="create planning collection")
    added = add(tmp_path, collection, item={"title": "Keep this for later"}, why="capture intent")

    with pytest.raises(CollectionError, match="INVALID_PLAN"):
        update(
            tmp_path,
            collection,
            plan_id=added["plan_id"],
            changes={
                "kind": "area",
                "status": None,
                "priority": None,
                "commitment": None,
                "horizon": None,
            },
            expected_container_hash=added["after_container_hash"],
            expected_item_version=added["after_item_hash"],
            why="attempt invalid conversion",
        )


# --- Planning inherits the derived identity (design D3) -------------------------


def test_planning_add_without_a_plan_id_derives_the_title_natural_key(tmp_path: Path) -> None:
    """Planning declares `natural_key: [title]` and goes through the same writer.

    Nothing here is Planning-specific: `add` forwards `plan_id` as `item_key`, so a
    capture with no caller-chosen id must land on the derived identity rather than a
    fresh `uuid4` that makes the same intent captureable twice.
    """
    from exomem import structured_collections as collections
    from exomem.planning import add, create_collection

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    manifest = create_collection(
        tmp_path, manifest_path, _manifest(), why="create planning collection"
    )

    added = add(
        tmp_path,
        manifest_path,
        item={"title": "Keep this for later"},
        why="capture future work",
    )

    assert added["plan_id"] == collections.inferred_item_key(
        manifest["collection_id"],
        collections.natural_key_serialization(1, ["title"], {"title": "Keep this for later"}, field_types={"title": "string"}),
    )


def test_planning_add_of_the_same_title_with_different_fields_refuses(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, manifest_path, _manifest(), why="create planning collection")
    added = add(
        tmp_path,
        manifest_path,
        item={"title": "Keep this for later"},
        why="capture future work",
    )

    with pytest.raises(CollectionError, match="RECORD_ID_CONFLICT"):
        add(
            tmp_path,
            manifest_path,
            item={"title": "Keep this for later", "priority": "high"},
            expected_container_hash=added["after_container_hash"],
            why="capture the same intent differently",
        )
