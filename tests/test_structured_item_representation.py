from __future__ import annotations

from pathlib import Path

import pytest

from exomem import record_formats
from exomem import structured_collections as collections


def _planning_manifest(*, recipes: str = "") -> str:
    return f"""---
type: collection
exomem_id: 8f031c81-1661-405b-8d25-e639175af49f
title: Product plan
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
    title: {{type: string, required: true}}
    status: {{type: string}}
    horizon: {{type: string}}
    note: {{type: string}}
    parent: {{type: link, link_kind: plan}}
{recipes}---
"""


def _records_manifest(*, recipes: str = "") -> str:
    return f"""---
type: collection
exomem_id: a9cc4e2b-ae4b-441c-be6d-5fef00cfef16
title: Observed events
semantic_profile: records
collection_version: 1
records_reader: 2
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [occurred_on, title, event_type]
  fields:
    occurred_on: {{type: date, required: true}}
    title: {{type: string, required: true}}
    event_type: {{type: string, required: true}}
    status: {{type: string}}
    note: {{type: string}}
    source: {{type: link, link_kind: note}}
{recipes}---
"""


def _parse(tmp_path: Path, text: str, *, profile: str) -> collections.CollectionManifest:
    path = tmp_path / "Knowledge Base" / ("Planning" if profile == "planning" else "Records")
    path = path / "Example" / "_collection.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return collections.parse_manifest_bytes(tmp_path, path, text.encode())


def test_shared_recipes_parse_to_closed_immutable_models(tmp_path: Path) -> None:
    manifest = _parse(
        tmp_path,
        _planning_manifest(
            recipes="""item_filename:
  version: 1
  fields: [title]
item_presentation:
  version: 1
  title: title
  summary:
    - field: status
      label: Status
    - horizon
  long_text:
    - field: note
      label: Context
  relationships:
    - field: parent
      label: Parent
"""
        ),
        profile="planning",
    )

    assert manifest.item_filename is not None
    assert manifest.item_filename.version == 1
    assert manifest.item_filename.fields == ("title",)
    assert manifest.item_presentation is not None
    assert manifest.item_presentation.title.field == "title"
    assert manifest.item_presentation.summary[0].label == "Status"
    assert manifest.item_presentation.long_text[0].field == "note"
    assert manifest.item_presentation.relationships[0].field == "parent"


@pytest.mark.parametrize(
    "recipes",
    [
        """item_filename:
  version: 2
  fields: [title]
""",
        """item_filename:
  version: 1
  fields: [title]
  separator: _
""",
        """item_filename:
  version: 1
  fields: [status]
""",
        """item_filename:
  version: 1
  fields: [title, horizon]
""",
        """item_presentation:
  version: 1
  title: absent
""",
        """item_presentation:
  version: 1
  title: title
  summary: [title, title]
""",
        """item_presentation:
  version: 1
  title: title
  relationships: [note]
""",
        """item_presentation:
  version: 1
  title: title
  unknown: true
""",
    ],
)
def test_invalid_planning_recipe_refuses_eagerly(tmp_path: Path, recipes: str) -> None:
    with pytest.raises(collections.CollectionError, match="INVALID_ITEM_(FILENAME|PRESENTATION)"):
        _parse(tmp_path, _planning_manifest(recipes=recipes), profile="planning")


def test_records_filename_accepts_immutable_natural_key_and_rejects_mutable_state(
    tmp_path: Path,
) -> None:
    valid = _parse(
        tmp_path,
        _records_manifest(
            recipes="""item_filename:
  version: 1
  fields: [occurred_on, title, event_type]
"""
        ),
        profile="records",
    )
    assert valid.item_filename is not None
    assert valid.item_filename.fields == ("occurred_on", "title", "event_type")

    mutable_natural_key = _records_manifest(
        recipes="""item_filename:
  version: 1
  fields: [status]
"""
    ).replace(
        "natural_key: [occurred_on, title, event_type]",
        "natural_key: [occurred_on, title, event_type, status]",
    )
    with pytest.raises(collections.CollectionError, match="INVALID_ITEM_FILENAME"):
        _parse(tmp_path, mutable_natural_key, profile="records")


def test_records_cannot_declare_legacy_and_shared_presentations(tmp_path: Path) -> None:
    recipes = """item_presentation:
  version: 1
  title: title
  summary: [event_type]
record_presentation:
  version: 1
  tables:
    - field: observations
      columns:
        - field: value
          type: string
"""
    text = _records_manifest(recipes=recipes).replace(
        "    source: {type: link, link_kind: note}\n",
        "    source: {type: link, link_kind: note}\n"
        "    observations:\n"
        "      type: array\n"
        "      items: {type: object}\n",
    )

    with pytest.raises(collections.CollectionError, match="INVALID_ITEM_PRESENTATION"):
        _parse(tmp_path, text, profile="records")


def test_recipe_bounds_refuse_unbounded_field_lists(tmp_path: Path) -> None:
    summary = "\n".join("    - title" for _ in range(17))
    recipes = f"""item_presentation:
  version: 1
  title: title
  summary:
{summary}
"""

    with pytest.raises(collections.CollectionError, match="INVALID_ITEM_PRESENTATION"):
        _parse(tmp_path, _planning_manifest(recipes=recipes), profile="planning")


def test_filename_renderer_reuses_human_portable_sanitization(tmp_path: Path) -> None:
    manifest = _parse(
        tmp_path,
        _planning_manifest(
            recipes="""item_filename:
  version: 1
  fields: [title]
"""
        ),
        profile="planning",
    )

    path = collections.render_item_path(
        manifest,
        {"title": "Q3: revenue / margin — sõbra review"},
        "11111111-1111-4111-8111-111111111111",
    )

    assert path == "Knowledge Base/Planning/Example/Items/Q3 revenue margin — sõbra review.md"


def test_filename_collision_gets_stable_short_identity_suffix(tmp_path: Path) -> None:
    manifest = _parse(
        tmp_path,
        _planning_manifest(
            recipes="""item_filename:
  version: 1
  fields: [title]
"""
        ),
        profile="planning",
    )
    first = collections.render_item_path(
        manifest,
        {"title": "Same title"},
        "11111111-1111-4111-8111-111111111111",
    )
    second = collections.render_item_path(
        manifest,
        {"title": "same title"},
        "22222222-2222-4222-8222-222222222222",
        occupied_paths=[first],
    )

    assert first.endswith("/Same title.md")
    assert second.endswith("/same title — 22222222.md")
    assert (
        collections.render_item_path(
            manifest,
            {"title": "same title"},
            "22222222-2222-4222-8222-222222222222",
            occupied_paths=[first],
        )
        == second
    )


@pytest.mark.parametrize("title", ['<>:"|?*', "CON", "   "])
def test_filename_renderer_refuses_values_that_cannot_form_a_portable_name(
    tmp_path: Path, title: str
) -> None:
    manifest = _parse(
        tmp_path,
        _planning_manifest(
            recipes="""item_filename:
  version: 1
  fields: [title]
"""
        ),
        profile="planning",
    )

    with pytest.raises(collections.CollectionError, match="UNRENDERABLE_ITEM_FILENAME"):
        collections.render_item_path(
            manifest,
            {"title": title},
            "11111111-1111-4111-8111-111111111111",
        )


def _readable_planning_manifest(tmp_path: Path) -> collections.CollectionManifest:
    return _parse(
        tmp_path,
        _planning_manifest(
            recipes="""item_filename:
  version: 1
  fields: [title]
item_presentation:
  version: 1
  title: title
  summary: [status, horizon]
  long_text:
    - field: note
      label: Context
  relationships:
    - field: parent
      label: Parent
"""
        ),
        profile="planning",
    )


def test_shared_renderer_is_deterministic_readable_and_preserves_authored_markdown(
    tmp_path: Path,
) -> None:
    manifest = _readable_planning_manifest(tmp_path)
    values = {
        "title": "Improve onboarding",
        "status": "candidate",
        "horizon": "quarter",
        "note": "Keep the existing flow, then test it.",
        "parent": "exomem://plan/parent",
        "execution": [{"kind": "repository", "ref": "opaque-private-pointer"}],
    }

    def resolve(kind: str, value: str) -> tuple[str, str] | None:
        assert kind == "plan"
        assert value == "exomem://plan/parent"
        return "Knowledge Base/Planning/Product/Items/Parent outcome.md", "Parent outcome"

    source = "Authored outside the block.\n"
    first = record_formats.splice_item_presentation(
        source, manifest, values, resolve_relationship=resolve
    )
    second = record_formats.splice_item_presentation(
        source, manifest, values, resolve_relationship=resolve
    )

    assert first == second
    assert first.startswith(source)
    assert "# Improve onboarding" in first
    assert "**Status:** candidate" in first
    assert "**Horizon:** quarter" in first
    assert "### Context" in first
    assert "Keep the existing flow, then test it." in first
    assert "[[Knowledge Base/Planning/Product/Items/Parent outcome|Parent outcome]]" in first
    assert "opaque-private-pointer" not in first
    assert "recipe=sha256:" in first and "item=sha256:" in first


def test_shared_renderer_escapes_canonical_text_but_keeps_owned_wikilinks(
    tmp_path: Path,
) -> None:
    manifest = _readable_planning_manifest(tmp_path)
    attack = "[[Private/Target]] **bold** [remote](https://example.invalid)\n# heading"

    rendered = record_formats.splice_item_presentation(
        "",
        manifest,
        {
            "title": attack,
            "status": attack,
            "horizon": "month",
            "note": attack,
            "parent": "parent-id",
        },
        resolve_relationship=lambda _kind, _value: (
            "Knowledge Base/Planning/Product/Items/Safe target.md",
            "Safe target",
        ),
    )

    assert "[[Private/Target]]" not in rendered
    assert "[remote](" not in rendered
    assert "**bold**" not in rendered
    assert "[[Knowledge Base/Planning/Product/Items/Safe target|Safe target]]" in rendered


def test_shared_renderer_refuses_unresolved_relationship_without_target_disclosure(
    tmp_path: Path,
) -> None:
    manifest = _readable_planning_manifest(tmp_path)

    with pytest.raises(collections.CollectionError) as caught:
        record_formats.splice_item_presentation(
            "Authored.\n",
            manifest,
            {
                "title": "Child",
                "status": "candidate",
                "horizon": "month",
                "parent": "secret-target-id",
            },
            resolve_relationship=lambda _kind, _value: None,
        )

    assert caught.value.code == "UNRENDERABLE_ITEM_PRESENTATION"
    assert "secret-target-id" not in str(caught.value)


def test_shared_renderer_replaces_only_its_owned_block(tmp_path: Path) -> None:
    manifest = _readable_planning_manifest(tmp_path)
    values = {"title": "First", "status": "candidate", "horizon": "month"}
    first = record_formats.splice_item_presentation("Before\n\nAfter\n", manifest, values)
    refreshed = record_formats.splice_item_presentation(
        first,
        manifest,
        {"title": "Second", "status": "active", "horizon": "quarter"},
    )

    assert refreshed.count("<!-- exomem-item-presentation:v1") == 1
    assert "# Second" in refreshed and "# First" not in refreshed
    outside = record_formats.remove_item_presentation(refreshed)
    assert outside == "Before\n\nAfter\n"
