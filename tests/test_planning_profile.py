from __future__ import annotations

from pathlib import Path

import pytest


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
    kind: {type: string}
    status: {type: string}
    lifecycle: {type: string}
    priority: {type: string}
    commitment: {type: string}
    horizon: {type: string}
    health: {type: string}
---
"""


def _manifest_with_audit() -> str:
    return _manifest().replace(
        "lifecycle: active\n",
        "lifecycle: active\nplan_audit: {version: 1, head: abcdef012345abcdef012345}\n",
    )


def test_planning_manifest_must_stay_in_the_planning_layer(tmp_path: Path) -> None:
    from exomem.structured_collections import CollectionError, load_manifest

    path = tmp_path / "Knowledge Base" / "Elsewhere" / "Work" / "_collection.md"
    path.parent.mkdir(parents=True)
    path.write_text(_manifest(), encoding="utf-8")

    with pytest.raises(CollectionError, match="^INVALID_COLLECTION_PATH:"):
        load_manifest(tmp_path, path.relative_to(tmp_path))


def test_planning_item_reference_uses_the_planning_namespace() -> None:
    from exomem import structured_collections

    assert (
        structured_collections.plan_ref(
            "2db90f18-70df-4e41-986e-2d7d7db1caca",
            "991acdd4-16b9-4396-8220-2cb37b7e8516",
        )
        == "exomem://plan/2db90f18-70df-4e41-986e-2d7d7db1caca/991acdd4-16b9-4396-8220-2cb37b7e8516"
    )


def test_planning_manifest_uses_a_planning_audit_head(tmp_path: Path) -> None:
    from exomem.structured_collections import load_manifest

    path = tmp_path / "Knowledge Base" / "Planning" / "Work" / "_collection.md"
    path.parent.mkdir(parents=True)
    path.write_text(_manifest_with_audit(), encoding="utf-8")

    assert (
        load_manifest(tmp_path, path.relative_to(tmp_path)).audit_head == "abcdef012345abcdef012345"
    )


def test_planning_profile_uses_distinct_identity_and_audit_names() -> None:
    from exomem.collection_profiles import PLANNING_PROFILE, RECORDS_PROFILE

    assert PLANNING_PROFILE.item_id_property == "plan_id"
    assert PLANNING_PROFILE.manifest_audit_property == "plan_audit"
    assert PLANNING_PROFILE.reference_namespace == "plan"
    assert PLANNING_PROFILE != RECORDS_PROFILE


def test_planning_creation_refuses_a_manifest_missing_core_fields(tmp_path: Path) -> None:
    from exomem.planning import create_collection
    from exomem.structured_collections import CollectionError

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    incomplete = _manifest().replace(
        """    kind: {type: string}
    status: {type: string}
    lifecycle: {type: string}
    priority: {type: string}
    commitment: {type: string}
    horizon: {type: string}
    health: {type: string}
""",
        "",
    )

    with pytest.raises(CollectionError, match="INVALID_PLAN"):
        create_collection(
            tmp_path,
            "Knowledge Base/Planning/Incomplete/_collection.md",
            incomplete,
            why="attempt invalid planning collection",
        )


def test_markdown_item_adapter_reads_a_planning_item_by_plan_id(tmp_path: Path) -> None:
    from exomem import record_formats
    from exomem.structured_collections import load_manifest

    directory = tmp_path / "Knowledge Base" / "Planning" / "Work"
    items = directory / "Items"
    items.mkdir(parents=True)
    (directory / "_collection.md").write_text(_manifest(), encoding="utf-8")
    (items / "one.md").write_text(
        """---
type: plan
collection_id: 2db90f18-70df-4e41-986e-2d7d7db1caca
plan_id: 991acdd4-16b9-4396-8220-2cb37b7e8516
schema_version: 1
title: Capture planning work
---
\nHuman context.\n""",
        encoding="utf-8",
    )

    manifest = load_manifest(tmp_path, directory / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()

    assert [item.identity.key for item in snapshot.records] == [
        "991acdd4-16b9-4396-8220-2cb37b7e8516"
    ]


def test_planning_capture_defaults_are_explicit_and_areas_have_no_delivery_state() -> None:
    from exomem.planning import normalize_item
    from exomem.structured_collections import CollectionError

    assert normalize_item({"title": "Keep this for later"}) == {
        "title": "Keep this for later",
        "kind": "work-item",
        "status": "candidate",
        "lifecycle": "active",
        "priority": "none",
        "commitment": "uncommitted",
        "horizon": "inbox",
        "health": "unknown",
    }
    with pytest.raises(CollectionError, match="^INVALID_PLAN:"):
        normalize_item({"title": "Area", "kind": "area", "status": "active"})


def test_markdown_item_renderer_uses_planning_system_fields(tmp_path: Path) -> None:
    from exomem import record_formats
    from exomem.structured_collections import load_manifest

    directory = tmp_path / "Knowledge Base" / "Planning" / "Work"
    directory.mkdir(parents=True)
    (directory / "_collection.md").write_text(_manifest(), encoding="utf-8")
    manifest = load_manifest(tmp_path, directory / "_collection.md")

    rendered = record_formats.render_markdown_item(
        manifest,
        {"title": "Capture planning work"},
        "991acdd4-16b9-4396-8220-2cb37b7e8516",
        audit_correlation="abcdef012345abcdef012345",
    )

    assert "type: plan" in rendered
    assert "plan_id: 991acdd4-16b9-4396-8220-2cb37b7e8516" in rendered
    assert "# exomem-plan-audit: abcdef012345abcdef012345" in rendered


def test_markdown_item_renderer_round_trips_nested_planning_descriptors(tmp_path: Path) -> None:
    from exomem import record_formats, vault
    from exomem.structured_collections import load_manifest

    directory = tmp_path / "Knowledge Base" / "Planning" / "Work"
    directory.mkdir(parents=True)
    (directory / "_collection.md").write_text(_manifest(), encoding="utf-8")
    manifest = load_manifest(tmp_path, directory / "_collection.md")
    descriptors = [
        {
            "kind": "openspec",
            "ref": "openspec/changes/add-multi-horizon-planning",
            "label": "Planning v1 contract",
        }
    ]

    rendered = record_formats.render_markdown_item(
        manifest,
        {"title": "Capture planning work", "execution": descriptors},
        "991acdd4-16b9-4396-8220-2cb37b7e8516",
    )

    frontmatter, _body, _marker = vault.parse_frontmatter(rendered, strict=True)
    assert frontmatter["execution"] == descriptors


def test_manifest_audit_renderer_uses_planning_audit_name(tmp_path: Path) -> None:
    from exomem import record_formats

    source = _manifest().replace("lifecycle: active\n", "lifecycle: active\n")

    rendered = record_formats.render_manifest_audit_head(
        source, "abcdef012345abcdef012345", semantic_profile="planning"
    )

    assert "plan_audit: {version: 1, head: abcdef012345abcdef012345}" in rendered
    assert "record_audit" not in rendered


def test_planning_inspection_is_report_only_and_exposes_current_contract(tmp_path: Path) -> None:
    from exomem.planning import inspect

    directory = tmp_path / "Knowledge Base" / "Planning" / "Work"
    directory.mkdir(parents=True)
    (directory / "_collection.md").write_text(_manifest(), encoding="utf-8")

    result = inspect(tmp_path, "Knowledge Base/Planning/Work/_collection.md")

    assert result["kind"] == "collection"
    assert result["report_only"] is True
    assert result["contract"]["semantic_profile"] == "planning"


def test_planning_inspection_reports_semantically_invalid_direct_edits(tmp_path: Path) -> None:
    from exomem.planning import inspect

    directory = tmp_path / "Knowledge Base" / "Planning" / "Work"
    items = directory / "Items"
    items.mkdir(parents=True)
    manifest = _manifest()
    (directory / "_collection.md").write_text(manifest, encoding="utf-8")
    item = items / "991acdd4-16b9-4396-8220-2cb37b7e8516.md"
    item.write_text(
        """---
type: plan
collection_id: 2db90f18-70df-4e41-986e-2d7d7db1caca
plan_id: 991acdd4-16b9-4396-8220-2cb37b7e8516
schema_version: 1
title: Manually edited plan
kind: work-item
status: active
lifecycle: active
priority: high
commitment: uncommitted
horizon: inbox
health: unknown
---
""",
        encoding="utf-8",
    )
    before = item.read_bytes()

    result = inspect(tmp_path, "Knowledge Base/Planning/Work/_collection.md")

    assert any(diagnostic["code"] == "INVALID_PLAN" for diagnostic in result["diagnostics"])
    assert item.read_bytes() == before


def test_planning_saved_views_use_plan_id_not_records_identity(tmp_path: Path) -> None:
    from exomem.structured_collections import CollectionError, load_manifest, resolve_saved_view

    path = tmp_path / "Knowledge Base" / "Planning" / "Work" / "_collection.md"
    path.parent.mkdir(parents=True)
    manifest_text = _manifest().rsplit("---\n", 1)[0] + """views:
  pinned:
    query:
      filters:
        - column: plan_id
          op: eq
          value: 991acdd4-16b9-4396-8220-2cb37b7e8516
---
"""
    path.write_text(manifest_text, encoding="utf-8")

    manifest = load_manifest(tmp_path, path)
    assert resolve_saved_view(manifest, "pinned").definition["query"]["filters"][0]["column"] == "plan_id"

    path.write_text(manifest_text.replace("plan_id", "record_id"), encoding="utf-8")
    invalid = load_manifest(tmp_path, path)
    with pytest.raises(CollectionError, match="INVALID_SAVED_VIEW"):
        resolve_saved_view(invalid, "pinned")


def test_an_all_digit_audit_head_survives_a_yaml_round_trip() -> None:
    """A head with no letters in it must not come back as an integer.

    The head is 24 characters of `uuid4().hex`, so roughly one in 79,000 draws
    contains no letter at all. Written bare into a YAML flow mapping, that one
    is typed as an integer rather than a string, and the reader -- which
    requires a str -- rejects the manifest with INVALID_COLLECTION_AUDIT. The
    collection is then unreadable for good.

    It is nondeterministic per write, which is exactly how it presented in
    #549: a single product-E2E failure on main that passed on a rerun of the
    identical tree, because the rerun drew a different id.

    Both writer branches are covered here. The append branch adds the mapping;
    the update branch rewrites the head scalar in place, and its replacement
    span includes the surrounding quotes -- so an unquoted replacement would
    strip them and reintroduce the defect on the very next mutation.
    """
    from exomem import record_formats, structured_collections, vault

    all_digits = "123456789012345678901234"
    successor = "987654321098765432109876"
    source = "---\ntype: plan\nsemantic_profile: planning\n---\nbody\n"

    appended = record_formats.render_manifest_audit_head(
        source, all_digits, semantic_profile="planning"
    )
    updated = record_formats.render_manifest_audit_head(
        appended, successor, semantic_profile="planning"
    )

    for stage, document, expected in (
        ("append", appended, all_digits),
        ("update", updated, successor),
    ):
        parsed, _body, _marker = vault.parse_frontmatter(document, strict=True)
        head = parsed["plan_audit"]["head"]
        assert isinstance(head, str), (
            f"{stage} wrote the head unquoted, so YAML typed it as "
            f"{type(head).__name__} and the manifest no longer loads"
        )
        assert structured_collections.plan_audit_head(parsed) == expected


def test_a_manifest_already_written_with_a_bare_all_digit_head_still_loads() -> None:
    """Recovery for manifests written before the head was quoted.

    Quoting stops new occurrences; it does nothing for a collection that
    already has one on disk, and that collection is otherwise permanently
    unreadable. The width is fixed at 24, so the integer YAML handed back
    zero-pads to exactly the string that was written.
    """
    from exomem import structured_collections, vault

    bare = "plan_audit: {version: 1, head: 123456789012345678901234}"
    document = f"---\ntype: plan\nsemantic_profile: planning\n{bare}\n---\nbody\n"

    parsed, _body, _marker = vault.parse_frontmatter(document, strict=True)

    assert not isinstance(parsed["plan_audit"]["head"], str), (
        "this fixture is meant to reproduce the untyped-scalar shape"
    )
    assert structured_collections.plan_audit_head(parsed) == "123456789012345678901234"
