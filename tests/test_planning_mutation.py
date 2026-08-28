from __future__ import annotations

from pathlib import Path

import pytest

from exomem import structured_collections as collections
from exomem.governance.principal import RequestPrincipal, request_scope
from exomem.structured_collections import CollectionError


def _manifest(*, human_owned: bool = False) -> str:
    representation = (
        ""
        if not human_owned
        else """item_filename:
  version: 1
  fields: [title]
item_presentation:
  version: 1
  title: title
  summary: [kind, status, lifecycle, priority, commitment, horizon]
"""
    )
    return f"""---
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
{representation}---
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
    created = collections.load_manifest(
        tmp_path, tmp_path / "Knowledge Base/Planning/Work/_collection.md"
    )
    assert created.item_filename is not None
    assert created.item_filename.fields == ("title",)
    assert created.item_presentation is not None
    assert set(created.views) == {"inbox", "week", "month", "quarter", "year", "multi-year"}


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

    monkeypatch.setattr(
        planning.record_governance, "require_candidate_manifest_visibility", refuse_candidate
    )
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
        tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items" / "Keep this for later.md"
    ).read_text(encoding="utf-8")
    assert receipt["operation"] == "add"
    assert receipt["plan_id"] == "991acdd4-16b9-4396-8220-2cb37b7e8516"
    assert "status: candidate" in item
    assert "# exomem-plan-audit:" in item


def test_planning_human_representation_is_written_atomically_and_path_stays_stable(
    tmp_path: Path,
) -> None:
    from exomem import record_formats
    from exomem.planning import add, create_collection, update

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(
        tmp_path,
        manifest_path,
        _manifest(human_owned=True),
        why="create readable planning collection",
    )

    added = add(
        tmp_path,
        manifest_path,
        item={"title": "Ship the customer graph"},
        plan_id="991acdd4-16b9-4396-8220-2cb37b7e8516",
        why="capture visible product work",
    )
    path = (
        tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items" / "Ship the customer graph.md"
    )
    first = path.read_text(encoding="utf-8")
    assert added["affected_paths"] == [path.relative_to(tmp_path).as_posix()]
    assert "# Ship the customer graph" in first
    assert "**Status:** candidate" in first
    assert "exomem-item-presentation:v1" in first

    snapshot = record_formats.load_adapter(
        tmp_path,
        collections.load_manifest(tmp_path, tmp_path / manifest_path),
    ).read()
    record = snapshot.records[0]
    changed = update(
        tmp_path,
        manifest_path,
        plan_id=record.identity.key,
        changes={"title": "Offer the customer graph"},
        expected_container_hash=added["after_container_hash"],
        expected_item_version=record.source.hash,
        why="rename the outcome without moving its identity projection",
    )

    assert changed["affected_paths"] == [path.relative_to(tmp_path).as_posix()]
    assert path.is_file()
    assert not path.with_name("Offer the customer graph.md").exists()
    second = path.read_text(encoding="utf-8")
    assert "# Offer the customer graph" in second
    assert second.count("exomem-item-presentation:v1") == 2


def test_planning_inspection_projects_human_representation_drift(tmp_path: Path) -> None:
    from exomem.planning import add, create_collection, inspect

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(
        tmp_path,
        manifest_path,
        _manifest(human_owned=True),
        why="create readable planning collection",
    )
    added = add(
        tmp_path,
        manifest_path,
        item={"title": "Ship the customer graph"},
        why="capture visible product work",
    )
    item_path = tmp_path / added["affected_paths"][0]
    item_path.write_text(
        item_path.read_text(encoding="utf-8").replace(
            "# Ship the customer graph", "# Changed generated heading", 1
        ),
        encoding="utf-8",
    )

    inspected = inspect(tmp_path, manifest_path)

    assert inspected["presentation"]["counts"]["authored_presentation"] == 1


def test_planning_manifest_lifecycle_validates_revises_and_publishes_profile_audit(
    tmp_path: Path,
) -> None:
    from exomem import record_formats, records, vault
    from exomem.planning import create_collection, inspect, revise, validate

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_validation = validate(
        tmp_path,
        mode="create",
        manifest_path=manifest_path,
        manifest_text=_manifest(human_owned=True),
    )
    assert create_validation["valid"] is True
    assert not (tmp_path / manifest_path).exists()
    create_collection(
        tmp_path,
        manifest_path,
        _manifest(human_owned=True),
        why="create readable planning collection",
    )
    current = collections.load_manifest(tmp_path, tmp_path / manifest_path)
    snapshot = record_formats.load_adapter(tmp_path, current).read()
    proposal = (
        (tmp_path / manifest_path)
        .read_text(encoding="utf-8")
        .replace("title: Planning work", "title: Product programme", 1)
    )

    revision = validate(
        tmp_path,
        mode="revision",
        collection=manifest_path,
        manifest_text=proposal,
    )
    assert revision["lifecycle_guards"] == records.lifecycle_guards(current, snapshot)
    receipt = revise(
        tmp_path,
        manifest_path,
        manifest_text=proposal,
        **revision["lifecycle_guards"],
        why="give the programme its reader-facing name",
    )

    assert receipt["_plan_receipt"] == "exomem.planning-mutation"
    assert receipt["operation"] == "revise"
    frontmatter, _body, _marker = vault.parse_frontmatter(
        (tmp_path / manifest_path).read_text(encoding="utf-8")
    )
    assert frontmatter["plan_audit"]["version"] == 2
    assert inspect(tmp_path, manifest_path)["audit"]["status"] == "ok"
    assert (
        records.agent_audit_history(tmp_path, manifest_path)["events"][0]["operation"]
        == "plan_revise"
    )


def test_planning_revision_and_rebaseline_require_exact_current_guards_and_gaps(
    tmp_path: Path,
) -> None:
    from exomem import record_formats
    from exomem.planning import add, create_collection, inspect, rebaseline, revise, validate

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    create_collection(tmp_path, manifest_path, _manifest(), why="create planning collection")
    proposal = (
        (tmp_path / manifest_path)
        .read_text(encoding="utf-8")
        .replace("title: Planning work", "title: Revised planning", 1)
    )
    revision = validate(
        tmp_path,
        mode="revision",
        collection=manifest_path,
        manifest_text=proposal,
    )
    add(tmp_path, manifest_path, item={"title": "New work"}, why="change the container")
    with pytest.raises(CollectionError, match="STALE_RECORD"):
        revise(
            tmp_path,
            manifest_path,
            manifest_text=proposal,
            **revision["lifecycle_guards"],
            why="refuse stale revision",
        )

    current = collections.load_manifest(tmp_path, tmp_path / manifest_path)
    snapshot = record_formats.load_adapter(tmp_path, current).read()
    item_path = tmp_path / snapshot.records[0].source.path
    item_path.write_text(
        item_path.read_text(encoding="utf-8").replace("title: New work", "title: Direct work", 1),
        encoding="utf-8",
    )
    gap = inspect(tmp_path, manifest_path)
    assert gap["audit"]["gaps"]
    with pytest.raises(CollectionError, match="STALE_RECORD"):
        rebaseline(
            tmp_path,
            manifest_path,
            **gap["lifecycle_guards"],
            acknowledged_gap_codes=[*gap["audit"]["gaps"], "invented-gap"],
            why="do not bless an invented gap",
        )
    receipt = rebaseline(
        tmp_path,
        manifest_path,
        **gap["lifecycle_guards"],
        acknowledged_gap_codes=gap["audit"]["gaps"],
        why="acknowledge the exact direct edit",
    )
    assert receipt["operation"] == "rebaseline"


@pytest.mark.parametrize("horizon", ["now", "next", "later"])
def test_planning_manifest_validation_refuses_noncanonical_horizon_views(
    tmp_path: Path, horizon: str
) -> None:
    from exomem.planning import validate

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest = _manifest().removesuffix("---\n") + (
        "views:\n"
        "  focus:\n"
        "    query:\n"
        "      filters:\n"
        "        - {column: horizon, op: eq, value: " + horizon + "}\n"
        "---\n"
    )

    with pytest.raises(CollectionError, match="INVALID_SAVED_VIEW"):
        validate(
            tmp_path,
            mode="create",
            manifest_path="Knowledge Base/Planning/Views/_collection.md",
            manifest_text=manifest,
        )


def test_planning_manifest_validation_accepts_canonical_horizon_views(tmp_path: Path) -> None:
    from exomem.planning import validate

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest = _manifest().removesuffix("---\n") + (
        "views:\n"
        "  focus:\n"
        "    query:\n"
        "      filters:\n"
        "        - {column: horizon, op: in, value: [week, quarter]}\n"
        "---\n"
    )

    result = validate(
        tmp_path,
        mode="create",
        manifest_path="Knowledge Base/Planning/Views/_collection.md",
        manifest_text=manifest,
    )

    assert result["valid"] is True


def test_planning_update_requires_current_hashes_and_replaces_authored_properties(
    tmp_path: Path,
) -> None:
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
        tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items" / "Keep this for later.md"
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
        tmp_path / "Knowledge Base" / "Planning" / "Work" / "Items" / "Keep this for later.md"
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
        collections.natural_key_serialization(
            1, ["title"], {"title": "Keep this for later"}, field_types={"title": "string"}
        ),
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
