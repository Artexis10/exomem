from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from exomem import memory_refs
from exomem import structured_collections as collections

COLLECTION_ID = "bf7d5ef7-2e68-4b5f-8e4e-f0f58eb9ccaf"
OTHER_ID = "b7fe66c0-ef6e-4e14-b22f-5855c6e37bf4"


def _manifest(collection_id: str = COLLECTION_ID, *, source: str = "events.md") -> str:
    return f"""---
type: collection
exomem_id: {collection_id}
title: Review contract
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-log
  source: {source}
  format_version: 1
item_schema:
  natural_key: [occurred_on]
  fields:
    occurred_on:
      type: date
      required: true
    status:
      type: enum
      enum: [completed, partial]
    metadata:
      type: object
templates:
  - path: template.md
    default_properties:
      nested:
        labels: [one]
views:
  latest:
    query:
      filters:
        status: completed
links:
  plans:
    - reference: exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7
      query:
        filters:
          nested:
            status: completed
        limit: 12
governance:
  release:
    tiers: [shared]
---
"""


def _write_manifest(vault: Path, relative: str, content: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_manifest_refuses_nonexistent_canonical_leaf_below_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    manifest = _write_manifest(
        vault,
        "Knowledge Base/Records/review/_collection.md",
        _manifest(source="canonical/missing.md"),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (manifest.parent / "canonical").symlink_to(outside, target_is_directory=True)

    with pytest.raises(collections.CollectionError) as excinfo:
        collections.load_manifest(vault, manifest)

    assert excinfo.value.code == "INVALID_COLLECTION_PATH"


@pytest.mark.parametrize("kind", ("directory", "fifo", "symlink"))
def test_resolve_keeps_unsafe_collection_filesystem_forms_distinct_from_absence(
    tmp_path: Path, kind: str
) -> None:
    vault = tmp_path / "vault"
    records = vault / "Knowledge Base/Records"
    records.mkdir(parents=True)
    target = records / "unsafe" / "_collection.md"
    target.parent.mkdir()
    if kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFOs are not supported on this platform")
        os.mkfifo(target)
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        try:
            (records / "unsafe").unlink()
            (records / "unsafe").symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks/reparse points are unavailable")

    for selector in (target.relative_to(vault), target):
        with pytest.raises(collections.CollectionError) as excinfo:
            collections.resolve_collection(vault, selector)

        assert excinfo.value.code == "INVALID_COLLECTION_PATH"


def test_resolve_normalizes_safe_absence_like_an_authorized_withheld_selector(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    missing = "Knowledge Base/Records/missing/_collection.md"
    existing = _write_manifest(vault, "Knowledge Base/Records/existing/_collection.md", _manifest())

    with pytest.raises(collections.CollectionError) as absent:
        collections.resolve_collection(vault, missing)
    with pytest.raises(collections.CollectionError) as absolute_absent:
        collections.resolve_collection(vault, vault / missing)
    with pytest.raises(collections.CollectionError) as withheld:
        collections.resolve_collection(vault, existing.relative_to(vault), authorize_path=lambda _path: False)

    assert (absent.value.code, absent.value.reason) == (withheld.value.code, withheld.value.reason)
    assert (absolute_absent.value.code, absolute_absent.value.reason) == (
        withheld.value.code,
        withheld.value.reason,
    )


@pytest.mark.parametrize("target", ("outside/_collection.md", "Knowledge Base/../outside/_collection.md"))
def test_resolve_rejects_unsafe_absolute_collection_selectors(tmp_path: Path, target: str) -> None:
    vault = tmp_path / "vault"
    candidate = tmp_path / target

    with pytest.raises(collections.CollectionError) as excinfo:
        collections.resolve_collection(vault, candidate)

    assert excinfo.value.code == "INVALID_COLLECTION_PATH"


def test_discovery_stops_after_cap_plus_one_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    kb = vault / "Knowledge Base"
    kb.mkdir(parents=True)
    candidates = []
    for number in range(3):
        candidate = kb / f"candidate-{number}" / "_collection.md"
        candidate.parent.mkdir()
        candidate.write_text("candidate\n", encoding="utf-8")
        candidates.append(candidate)

    def bounded_candidates(self: Path, pattern: str):
        assert self == kb
        assert pattern == "_collection.md"
        yield from candidates
        raise AssertionError("discovery read past its cap probe")

    monkeypatch.setattr(Path, "rglob", bounded_candidates)

    with pytest.raises(collections.CollectionError) as excinfo:
        collections.discover_collections(vault, max_candidates=2, max_raw_candidates=2)

    assert excinfo.value.code == "COLLECTION_DISCOVERY_LIMIT"


def test_discovery_counts_only_authorized_candidates_before_parsing_the_next_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    for name in ("first", "second", "third"):
        _write_manifest(vault, f"Knowledge Base/Records/{name}/_collection.md", _manifest())

    third = vault / "Knowledge Base/Records/third/_collection.md"
    original = collections.load_manifest

    def no_third_parse(root: Path, path: Path | str):
        assert Path(path) != third
        return original(root, path)

    monkeypatch.setattr(collections, "load_manifest", no_third_parse)
    with pytest.raises(collections.CollectionError) as excinfo:
        collections.discover_collections(vault, max_candidates=2, max_raw_candidates=3)

    assert excinfo.value.code == "COLLECTION_DISCOVERY_LIMIT"


def test_schema_inference_consumes_at_most_its_requested_sample_bound() -> None:
    def rows():
        yield {"value": 1}
        yield {"value": 2}
        raise AssertionError("inference consumed past max_rows")

    proposal = collections.infer_schema(rows(), max_rows=2)

    assert proposal.sample_count == 2
    assert proposal.fields["value"].type == "integer"


def test_item_identity_normalizes_uuid_and_natural_keys_reject_nonfinite_numbers() -> None:
    uppercase = COLLECTION_ID.upper()
    identity = collections.ItemIdentity(uppercase, "item")
    assert identity.collection_id == COLLECTION_ID
    assert identity.reference().startswith(f"exomem://record/{COLLECTION_ID}/")

    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            collections.natural_key_serialization(1, ("value",), {"value": value})


def test_manifest_nested_data_is_deeply_immutable_and_schema_values_are_strict(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    manifest_path = _write_manifest(
        vault, "Knowledge Base/Records/review/_collection.md", _manifest()
    )

    manifest = collections.load_manifest(vault, manifest_path)

    with pytest.raises(TypeError):
        manifest.storage.descriptor["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        manifest.templates[0].default_properties["nested"]["new"] = "value"  # type: ignore[index]
    with pytest.raises(AttributeError):
        manifest.views["latest"]["query"]["filters"]["status"].append("other")  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        manifest.links.plans[0].query["filters"]["nested"]["status"] = "partial"  # type: ignore[index]
    with pytest.raises(TypeError):
        manifest.governance["release"]["tiers"][0] = "private"  # type: ignore[index]

    with pytest.raises(collections.CollectionError) as excinfo:
        manifest.schema.validate(
            {"occurred_on": "2026-07-01", "status": "invented", "metadata": {}}
        )
    assert excinfo.value.code == "SCHEMA_ENUM"
    with pytest.raises(collections.CollectionError) as excinfo:
        manifest.schema.validate(
            {
                "occurred_on": "2026-07-01",
                "status": "completed",
                "metadata": {"nested": object()},
            }
        )
    assert excinfo.value.code == "SCHEMA_FIELD_TYPE"

    malformed = _write_manifest(
        vault,
        "Knowledge Base/Records/malformed/_collection.md",
        _manifest().replace("enum: [completed, partial]", "enum: []"),
    )
    with pytest.raises(collections.CollectionError) as excinfo:
        collections.load_manifest(vault, malformed)
    assert excinfo.value.code == "INVALID_ITEM_SCHEMA"


def test_manifest_freeze_refuses_yaml_alias_cycles_deep_values_and_large_values(
    tmp_path: Path,
) -> None:
    del tmp_path
    cycle = yaml.safe_load("&cycle {self: *cycle}")
    deep: dict[str, object] = {"leaf": "value"}
    for number in range(12):
        deep = {f"level_{number}": deep}
    large = {"values": list(range(257))}

    for value in (cycle, deep, large):
        with pytest.raises(collections.CollectionError) as excinfo:
            collections._freeze_mapping(value)
        assert excinfo.value.code == "INVALID_COLLECTION_MANIFEST"


def test_schema_inference_refuses_field_and_provenance_overflow_without_overreading() -> None:
    fields = {f"field_{number}": number for number in range(129)}
    with pytest.raises(collections.CollectionError) as excinfo:
        collections.infer_schema([fields])
    assert excinfo.value.code == "SCHEMA_INFERENCE_FIELD_LIMIT"

    def provenance():
        for number in range(collections._MAX_INFERENCE_PROVENANCE + 1):
            yield Path(f"source-{number}.md")
        raise AssertionError("provenance consumed past its cap probe")

    with pytest.raises(collections.CollectionError) as excinfo:
        collections.infer_schema([{"value": 1}], source_paths=provenance())
    assert excinfo.value.code == "SCHEMA_INFERENCE_PROVENANCE_LIMIT"


def test_uuid_resolution_ignores_unrelated_duplicate_collection_ids(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    selected = _write_manifest(
        vault, "Knowledge Base/Records/selected/_collection.md", _manifest(COLLECTION_ID)
    )
    _write_manifest(vault, "Knowledge Base/Records/other-a/_collection.md", _manifest(OTHER_ID))
    _write_manifest(vault, "Knowledge Base/Records/other-b/_collection.md", _manifest(OTHER_ID))

    resolved = collections.resolve_collection(vault, memory_refs.memory_ref(COLLECTION_ID))

    assert resolved.path == selected.relative_to(vault).as_posix()


def test_object_field_rejects_non_json_value_without_allowing_a_type_bypass() -> None:
    schema = collections.ItemSchema(
        1,
        MappingProxyType({"metadata": collections.FieldSpec("object")}),
    )

    with pytest.raises(collections.CollectionError) as excinfo:
        schema.validate({"metadata": {"unsafe": object()}})

    assert excinfo.value.code == "SCHEMA_FIELD_TYPE"
