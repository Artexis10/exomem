"""One bad manifest must not blind the sweep, and a directory is not "outside the vault".

Two defects in the shared collection abstraction, reproduced here before the fix:

* the inventory sweep aborted on the FIRST mislocated or unparseable
  `_collection.md`, so a single bad file hid every good collection from both
  profiles;
* a collection reference naming a real in-vault directory was refused as
  "outside the governed vault", which sends the caller looking for a path
  problem that does not exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_planning_mutation import _manifest as _planning_manifest

from exomem.cli_ops import OpError

_MISLOCATED_PLANNING_MANIFEST = """---
type: collection
exomem_id: 5c2f4a70-1d63-4c8b-9a9f-2f8b6f0d4e11
title: Misplaced planning work
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
---
"""

_UNPARSEABLE_MANIFEST = "---\nthis is: [not valid yaml\n---\n"

_MISLOCATED_PATH = "Knowledge Base/Records/Misplaced/_collection.md"
_UNPARSEABLE_PATH = "Knowledge Base/Records/Corrupt/_collection.md"
_PLANNING_PATH = "Knowledge Base/Planning/Work/_collection.md"


def _seed_records_collection(vault: Path) -> str:
    """Seed a governed vault holding one released Records collection."""
    from exomem.record_memory import record_memory

    log = vault / "Knowledge Base" / "log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("# Activity\n", encoding="utf-8")
    example = record_memory(vault, "describe")["examples"]["minimal"]
    record_memory(
        vault,
        "create",
        manifest_path=example["manifest_path"],
        manifest_text=example["manifest_text"],
        scaffold=True,
        why="seed the records collection",
    )
    return str(example["manifest_path"])


def _seed_planning_collection(vault: Path) -> str:
    from exomem.plan_memory import plan_memory

    plan_memory(
        vault,
        "create",
        manifest_path=_PLANNING_PATH,
        manifest_text=_planning_manifest(),
        why="seed the planning collection",
    )
    return _PLANNING_PATH


def _write_manifest(vault: Path, relative: str, text: str) -> str:
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return relative


def test_records_inventory_reports_a_mislocated_planning_manifest(tmp_path: Path) -> None:
    from exomem.record_memory import record_memory

    released = _seed_records_collection(tmp_path)
    mislocated = _write_manifest(tmp_path, _MISLOCATED_PATH, _MISLOCATED_PLANNING_MANIFEST)

    inventory = record_memory(tmp_path, "inspect")

    assert [row["manifest_path"] for row in inventory["collections"]] == [released]
    assert inventory["unreadable_manifests"] == [
        {
            "path": mislocated,
            "error_code": "INVALID_COLLECTION_PATH",
            "message": "manifest must stay under Knowledge Base/Planning",
        }
    ]
    assert inventory["truncated"]["unreadable_manifests"] is False


def test_planning_inventory_reports_a_mislocated_planning_manifest(tmp_path: Path) -> None:
    from exomem import record_governance

    _seed_records_collection(tmp_path)
    released = _seed_planning_collection(tmp_path)
    mislocated = _write_manifest(tmp_path, _MISLOCATED_PATH, _MISLOCATED_PLANNING_MANIFEST)

    inventory = record_governance.inventory_collections(tmp_path, semantic_profile="planning")

    assert [row["manifest_path"] for row in inventory["collections"]] == [released]
    assert inventory["unreadable_manifests"] == [
        {
            "path": mislocated,
            "error_code": "INVALID_COLLECTION_PATH",
            "message": "manifest must stay under Knowledge Base/Planning",
        }
    ]
    assert "legacy_trackers" not in inventory


def test_an_unparseable_manifest_is_reported_rather_than_fatal(tmp_path: Path) -> None:
    from exomem.record_memory import record_memory

    released = _seed_records_collection(tmp_path)
    corrupt = _write_manifest(tmp_path, _UNPARSEABLE_PATH, _UNPARSEABLE_MANIFEST)

    inventory = record_memory(tmp_path, "inspect")

    assert [row["manifest_path"] for row in inventory["collections"]] == [released]
    assert [row["path"] for row in inventory["unreadable_manifests"]] == [corrupt]
    assert inventory["unreadable_manifests"][0]["error_code"]
    assert inventory["unreadable_manifests"][0]["message"]


def test_plan_memory_query_with_a_directory_names_the_manifest_requirement(
    tmp_path: Path,
) -> None:
    from exomem.plan_memory import plan_memory

    _seed_records_collection(tmp_path)
    _seed_planning_collection(tmp_path)
    directory = _PLANNING_PATH.rsplit("/", 1)[0]

    with pytest.raises(OpError) as raised:
        plan_memory(tmp_path, "query", collection=directory)

    error = raised.value
    assert error.code == "INVALID_COLLECTION_PATH"
    assert "outside the governed vault" not in error.message
    assert "_collection.md" in error.message
    assert error.remediation == f"{directory}/_collection.md"


def test_record_memory_inspect_with_a_directory_names_the_manifest_requirement(
    tmp_path: Path,
) -> None:
    from exomem.record_memory import record_memory

    released = _seed_records_collection(tmp_path)
    directory = released.rsplit("/", 1)[0]

    with pytest.raises(OpError) as raised:
        record_memory(tmp_path, "inspect", collection=directory)

    error = raised.value
    assert error.code == "INVALID_COLLECTION_PATH"
    assert "outside the governed vault" not in error.message
    assert "_collection.md" in error.message
    assert error.as_public_dict()["remediation"] == f"{directory}/_collection.md"


def test_a_reference_that_escapes_the_vault_still_reports_the_vault_boundary(
    tmp_path: Path,
) -> None:
    from exomem.plan_memory import plan_memory

    _seed_records_collection(tmp_path)

    with pytest.raises(OpError) as raised:
        plan_memory(tmp_path, "query", collection="../escape/_collection.md")

    error = raised.value
    assert error.code == "INVALID_COLLECTION_PATH"
    assert error.message == "collection path is outside the governed vault"
    assert error.remediation is None


def test_a_bare_title_is_not_reported_as_a_path_that_escaped_the_vault(
    tmp_path: Path,
) -> None:
    from exomem.plan_memory import plan_memory

    _seed_records_collection(tmp_path)
    _seed_planning_collection(tmp_path)

    with pytest.raises(OpError) as raised:
        plan_memory(tmp_path, "query", collection="Planning work")

    error = raised.value
    assert error.code == "INVALID_COLLECTION_PATH"
    assert error.message == (
        "collection reference must be the collection's `_collection.md` manifest path "
        "(a title is not a reference)"
    )
    assert error.remediation == "pass the manifest path; use the collection's UUID if you have it"
