"""Reusable structured-collection acceptance fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

FIXTURES = Path(__file__).with_name("fixtures") / "records"


def copy_x3_fixture(destination: Path) -> Path:
    target = _copy_fixture_at("x3", destination, "Health/X3")
    template_target = x3_template_directory(destination)
    template_target.mkdir(parents=True, exist_ok=True)
    for name in ("X3 Push.md", "X3 Pull.md"):
        shutil.move(target / name, template_target / name)
    crlf_variant = target / "Training Log CRLF no final newline.md"
    crlf_variant.write_bytes(
        (target / "Training Log.md").read_bytes().replace(b"\n", b"\r\n").rstrip(b"\r\n")
    )
    return target


def x3_template_directory(destination: Path) -> Path:
    """Return the ordinary Obsidian template location for the copied X3 fixture."""
    return destination / "Knowledge Base" / "Templates" / "Records" / "Health" / "X3"


def copy_vehicle_maintenance_fixture(destination: Path) -> Path:
    return _copy_fixture("vehicle-maintenance", destination)


def copy_dataset_fixture(destination: Path) -> Path:
    return _copy_fixture("dataset", destination)


def _copy_fixture(name: str, destination: Path) -> Path:
    return _copy_fixture_at(name, destination, name)


def _copy_fixture_at(name: str, destination: Path, target_name: str) -> Path:
    target = destination / "Knowledge Base" / "Records" / target_name
    shutil.copytree(FIXTURES / name, target)
    return target


LEDGER_COLLECTION_PATH = "Knowledge Base/Records/Publications/_collection.md"

LEDGER_MANIFEST_TEXT = """---
type: collection
exomem_id: 5e9d1e2f-8f0a-4b6c-9d21-6f0f4a2b7c31
title: Publications ledger
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Entries
  format_version: 1
item_schema:
  natural_key: [published_on, slug]
  fields:
    published_on:
      type: date
      required: true
    slug:
      type: string
      required: true
    published_at:
      type: datetime
    word_count:
      type: integer
    exact_text:
      type: string
      required: true
    channels:
      type: array
      items:
        type: string
    details:
      type: object
    metrics:
      type: array
      items:
        type: object
---

One ordinary Markdown file per published entry.
"""


def setup_ledger_collection(vault: Path, *, source: str = "Entries") -> Path:
    """Write a generic publications-ledger Markdown-item collection.

    Deliberately impersonal: a ledger of published entries, with no accounts and
    no people, so the fixture teaches the contract without carrying real content.
    """
    activity = vault / "Knowledge Base/log.md"
    activity.parent.mkdir(parents=True, exist_ok=True)
    activity.write_text("# Activity\n", encoding="utf-8")
    path = vault / LEDGER_COLLECTION_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        LEDGER_MANIFEST_TEXT.replace("  source: Entries\n", f"  source: {source}\n"),
        encoding="utf-8",
    )
    if source != ".":
        (path.parent / source).mkdir(parents=True, exist_ok=True)
    return path


def ledger_item(**overrides: object) -> dict[str, object]:
    """A complete, valid ledger candidate that callers mutate per test."""
    item: dict[str, object] = {
        "published_on": "2026-09-08",
        "slug": "quarterly-note",
        "published_at": "2026-09-08T09:30:00+00:00",
        "word_count": 42,
        "exact_text": "First paragraph.\n\nSecond paragraph.",
        "channels": ["ledger", "digest"],
        "details": {"format": "long", "revision": 2},
        "metrics": [{"name": "views", "value": 12}, {"name": "replies", "value": 3}],
    }
    item.update(overrides)
    return item
