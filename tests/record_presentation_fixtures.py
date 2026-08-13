from __future__ import annotations

from pathlib import Path

from exomem import structured_collections as collections

COLLECTION_PATH = "Knowledge Base/Records/Observed/_collection.md"
ITEM_KEY = "11111111-1111-4111-8111-111111111111"


def manifest_text(*, two_tables: bool = False, presentation: bool = True) -> str:
    second_field = "" if not two_tables else """
    qualifiers:
      type: array
      required: true
      items:
        type: object
"""
    second_table = "" if not two_tables else """
    - field: qualifiers
      label: Qualifiers
      columns:
        - field: kind
          type: string
        - field: text
          type: string
"""
    recipe = "" if not presentation else f"""record_presentation:
  version: 1
  summary:
    - field: subject
      label: Subject
    - observed_on
  tables:
    - field: measurements
      label: Measurements
      columns:
        - field: name
          label: Name
          type: string
        - field: value
          label: Value
          type: string
        - field: unit
          label: Unit
          type: string
        - field: canceled
          label: Canceled
          type: boolean
        - field: source
          label: Source
          type: link
          link_kind: note
{second_table}  notes:
    - field: note
      label: Note
  details:
    - field: provenance
      label: Provenance
"""
    return f"""---
type: collection
exomem_id: 44444444-4444-4444-8444-444444444444
title: Observed measurements
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [observed_on]
  fields:
    observed_on:
      type: date
      required: true
    subject:
      type: string
      required: true
    measurements:
      type: array
      required: true
      items:
        type: object
{second_field}    note:
      type: string
    provenance:
      type: string
{recipe}---
"""


def values(*, child_count: int = 2) -> dict[str, object]:
    rows = [
        {
            "name": "Below threshold" if index == 0 else f"Observation {index + 1}",
            "value": "<5" if index == 0 else (None if index == 1 else str(index + 1)),
            "unit": "unit/mL",
            "canceled": index == 1,
            "source": "[[Sources/Observed]]",
            "private": f"not projected {index}",
        }
        for index in range(child_count)
    ]
    return {
        "observed_on": "2026-08-13",
        "subject": "Sample <A>",
        "measurements": rows,
        "note": "Preserve qualifier: fasting | repeated",
        "provenance": "Imported exactly; no interpretation.",
    }


def setup_collection(
    vault: Path, *, two_tables: bool = False, presentation: bool = True
) -> collections.CollectionManifest:
    activity = vault / "Knowledge Base/log.md"
    activity.parent.mkdir(parents=True, exist_ok=True)
    activity.write_text("# Activity\n", encoding="utf-8")
    path = vault / COLLECTION_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        manifest_text(two_tables=two_tables, presentation=presentation), encoding="utf-8"
    )
    (path.parent / "Items").mkdir()
    return collections.load_manifest(vault, path)
