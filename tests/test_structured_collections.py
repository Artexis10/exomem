from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from record_fixtures import (
    copy_dataset_fixture,
    copy_vehicle_maintenance_fixture,
    copy_x3_fixture,
)

from exomem import memory_refs, record_formats, records
from exomem import structured_collections as collections

COLLECTION_ID = "bf7d5ef7-2e68-4b5f-8e4e-f0f58eb9ccaf"
READER_V2_FIXTURE = Path(__file__).parent / "fixtures/records/reader-v2"


def _manifest(
    collection_id: str = COLLECTION_ID,
    *,
    profile: str = "records",
    source: str = "events.md",
    version: int = 1,
) -> str:
    return f"""---
type: collection
exomem_id: {collection_id}
title: Maintenance events
semantic_profile: {profile}
collection_version: {version}
schema_version: 2
lifecycle: active
storage:
  strategy: markdown-items
  source: {source}
  format_version: 1
item_schema:
  natural_key: [occurred_on, title]
  fields:
    occurred_on:
      type: date
      required: true
    title:
      type: string
      required: true
    mileage:
      type: integer
    evidence:
      type: array
      items:
        type: link
templates:
  - path: Templates/maintenance.md
    default_properties:
      status: planned
views:
  latest:
    sort: [occurred_on, desc]
links:
  plans:
    - reference: exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7
      query:
        filters:
          asset: car
        limit: 12
---

Human-readable contract.
"""


def _write_manifest(vault: Path, relative: str, content: str | None = None) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or _manifest(), encoding="utf-8")
    return path


def test_load_manifest_keeps_profile_neutral_contract_and_opaque_plan_link(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = _write_manifest(vault, "Knowledge Base/Records/Maintenance/_collection.md")

    manifest = collections.load_manifest(vault, path)

    assert manifest.collection_id == COLLECTION_ID
    assert manifest.semantic_profile == "records"
    assert manifest.storage.strategy == "markdown-items"
    assert manifest.storage.source == "Knowledge Base/Records/Maintenance/events.md"
    assert manifest.schema.natural_key == ("occurred_on", "title")
    assert manifest.schema.validate({"occurred_on": "2026-07-01", "title": "Service"}) == {
        "occurred_on": "2026-07-01",
        "title": "Service",
    }
    assert manifest.templates[0].path == (
        "Knowledge Base/Records/Maintenance/Templates/maintenance.md"
    )
    assert manifest.links.plans[0].reference == (
        "exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7"
    )
    assert manifest.links.plans[0].query == {"filters": {"asset": "car"}, "limit": 12}


def test_records_reader_floor_refuses_a_v2_marker_for_a_predecessor_reader(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    shutil.copytree(READER_V2_FIXTURE / "Knowledge Base", vault / "Knowledge Base")
    expected = json.loads((READER_V2_FIXTURE / "expected.json").read_text(encoding="utf-8"))
    relative = "Knowledge Base/Records/ReaderV2/_collection.md"
    before = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in sorted(vault.rglob("*"))
        if path.is_file()
    }
    path = vault / relative
    fixture = path.read_bytes()
    manifest = collections.load_manifest(vault, path)
    history = records.agent_audit_history(vault, manifest.path)
    status = records.inspect_audit_gap(vault, manifest.path)

    assert manifest.audit_head is not None
    assert history["status"] == "ok"
    assert history["events"][0]["operation"] == "revise"
    assert collections.resolve_saved_view(manifest, "latest").definition == {
        "query": {"descending": True, "filters": [], "sort_by": "occurred_on"}
    }
    assert hashlib.sha256(json.dumps(history, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == expected["history_sha256"]
    assert hashlib.sha256(json.dumps(status, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == expected["status_sha256"]
    assert {path: hashlib.sha256(value).hexdigest() for path, value in before.items()} == expected["fixture_sha256"]
    with pytest.raises(collections.CollectionError) as raised:
        collections.parse_manifest_bytes(vault, relative, fixture, records_reader_version=1)

    assert raised.value.code == "RECORDS_READER_VERSION_UNSUPPORTED"
    assert {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in sorted(vault.rglob("*"))
        if path.is_file()
    } == before


def test_reader_two_lifecycle_transition_is_never_traversed_by_reader_one(tmp_path: Path) -> None:
    """The v2 marker is emitted by the real revise path, not fixture decoration."""
    fixture = copy_x3_fixture(tmp_path)
    log = tmp_path / "Knowledge Base/log.md"
    log.write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item={
            "occurred_on": "2026-08-03", "title": "Pull", "status": "completed",
            "movements": [{"movement": "Deadlift", "band": "grey", "repetitions": "22"}],
        },
        item_key="11111111-1111-4111-8111-111111111111",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="record a session",
    )
    current = collections.load_manifest(tmp_path, manifest.path)
    current_snapshot = record_formats.load_adapter(tmp_path, current).read()
    records.revise_collection(
        tmp_path,
        current.path,
        manifest_text=(tmp_path / current.path).read_text(encoding="utf-8").replace(
            "title:", "title: Revised", 1
        ),
        expected_manifest_hash=current.manifest_version.hash,
        expected_container_hash=records.lifecycle_guards(current, current_snapshot)["expected_container_hash"],
        why="clarify title",
    )
    produced = (tmp_path / current.path).read_bytes()
    assert collections.load_manifest(tmp_path, current.path).audit_head is not None
    assert "revise" in {
        event["operation"] for event in records.agent_audit_history(tmp_path, current.path)["events"]
    }
    with pytest.raises(collections.CollectionError, match="RECORDS_READER_VERSION_UNSUPPORTED"):
        collections.parse_manifest_bytes(tmp_path, current.path, produced, records_reader_version=1)
    assert (tmp_path / current.path).read_bytes() == produced


def test_records_manifest_ignores_an_unowned_legacy_plan_audit_mapping(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = _write_manifest(
        vault,
        "Knowledge Base/Records/Maintenance/_collection.md",
        _manifest().replace("lifecycle: active\n", "lifecycle: active\nplan_audit: broken\n"),
    )

    assert collections.load_manifest(vault, path).semantic_profile == "records"


def test_load_manifest_uses_the_bounded_descriptor_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    path = _write_manifest(vault, "Knowledge Base/Records/Maintenance/_collection.md")

    monkeypatch.setattr(
        Path, "read_bytes", lambda _self: (_ for _ in ()).throw(AssertionError("unguarded read"))
    )

    assert (
        collections.load_manifest(vault, path).path
        == "Knowledge Base/Records/Maintenance/_collection.md"
    )


def test_load_manifest_refuses_a_source_alias_of_its_own_manifest(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = _write_manifest(
        vault,
        "Knowledge Base/Records/Maintenance/_collection.md",
        _manifest(source="_COLLECTION.md"),
    )

    with pytest.raises(collections.CollectionError) as raised:
        collections.load_manifest(vault, path)

    assert raised.value.code == "INVALID_COLLECTION_PATH"


def test_load_manifest_allows_future_planning_but_rejects_unknown_profiles(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    planning = _write_manifest(
        vault,
        "Knowledge Base/Planning/Initiatives/_collection.md",
        _manifest(profile="planning"),
    )
    assert collections.load_manifest(vault, planning).semantic_profile == "planning"

    unknown = _write_manifest(
        vault,
        "Knowledge Base/Records/Unknown/_collection.md",
        _manifest(profile="forecast"),
    )
    with pytest.raises(collections.CollectionError) as excinfo:
        collections.load_manifest(vault, unknown)
    assert excinfo.value.code == "UNSUPPORTED_COLLECTION_PROFILE"


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        ("collection_version: 2", "UNSUPPORTED_COLLECTION_VERSION"),
        ("format_version: 2", "UNSUPPORTED_STORAGE_FORMAT_VERSION"),
        ("source: ../outside.md", "INVALID_COLLECTION_PATH"),
    ],
)
def test_load_manifest_refuses_unsupported_versions_and_path_escapes(
    tmp_path: Path, replacement: str, code: str
) -> None:
    vault = tmp_path / "vault"
    original = (
        "collection_version: 1"
        if replacement.startswith("collection")
        else "format_version: 1"
        if replacement.startswith("format")
        else "source: events.md"
    )
    path = _write_manifest(
        vault,
        "Knowledge Base/Records/Maintenance/_collection.md",
        _manifest().replace(original, replacement),
    )

    with pytest.raises(collections.CollectionError) as excinfo:
        collections.load_manifest(vault, path)
    assert excinfo.value.code == code


def test_discovery_authorizes_paths_before_parsing_and_only_releasable_duplicates_are_ambiguous(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    released = _write_manifest(vault, "Knowledge Base/Records/Released/_collection.md")
    withheld = _write_manifest(
        vault,
        "Knowledge Base/Records/Withheld/_collection.md",
        "---\nthis is: [not valid yaml\n---\n",
    )

    discovered = collections.discover_collections(
        vault,
        authorize_path=lambda path: path != withheld.relative_to(vault).as_posix(),
    )
    assert [item.path for item in discovered] == [released.relative_to(vault).as_posix()]

    duplicate = _write_manifest(vault, "Knowledge Base/Records/Duplicate/_collection.md")
    with pytest.raises(collections.CollectionError) as excinfo:
        collections.resolve_collection(
            vault,
            COLLECTION_ID,
            authorize_path=lambda path: path != withheld.relative_to(vault).as_posix(),
        )
    assert excinfo.value.code == "AMBIGUOUS_COLLECTION"
    assert str(duplicate) not in excinfo.value.reason


def test_discovery_skips_symlink_escape_and_resolves_memory_reference(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = _write_manifest(vault, "Knowledge Base/Records/Maintenance/_collection.md")
    outside = tmp_path / "outside.md"
    outside.write_text(_manifest(), encoding="utf-8")
    escaped = vault / "Knowledge Base" / "Records" / "escaped" / "_collection.md"
    escaped.parent.mkdir(parents=True)
    try:
        escaped.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    resolved = collections.resolve_collection(vault, memory_refs.memory_ref(COLLECTION_ID))
    assert resolved.path == path.relative_to(vault).as_posix()
    assert collections.discover_collections(vault) == (resolved,)


def test_collection_scoped_record_reference_and_natural_key_are_canonical() -> None:
    fields = {
        "occurred_on": "2026-07-01",
        "title": "cafe\u0301",
        "repetitions": None,
        "load": 12.5,
        "completed": True,
    }

    serialized = collections.natural_key_serialization(
        2,
        ("occurred_on", "title", "repetitions", "load", "completed"),
        fields,
        field_types={"occurred_on": "date"},
    )
    assert json.loads(serialized) == [
        2,
        [
            ["occurred_on", "2026-07-01"],
            ["title", "café"],
            ["repetitions", None],
            ["load", 12.5],
            ["completed", True],
        ],
    ]

    key = collections.inferred_item_key(COLLECTION_ID, serialized)
    ref = collections.record_ref(COLLECTION_ID, "service / 2026-07-01")
    assert collections.parse_record_ref(ref) == (COLLECTION_ID, "service / 2026-07-01")
    assert collections.ItemIdentity(COLLECTION_ID, key, inferred=True).reference().endswith(key)


def test_advisory_schema_inference_has_provenance_and_never_writes(tmp_path: Path) -> None:
    source = tmp_path / "events.md"
    source.write_text("manual source\n", encoding="utf-8")
    before = source.read_bytes()

    proposal = collections.infer_schema(
        [
            {"occurred_on": "2026-07-01", "mileage": 12000, "status": "completed"},
            {"occurred_on": "2026-07-02", "mileage": 12100, "status": "scheduled"},
        ],
        source_paths=[source],
    )

    assert proposal.advisory is True
    assert proposal.sample_count == 2
    assert proposal.provenance == (str(source),)
    assert proposal.fields["occurred_on"].type == "date"
    assert proposal.fields["mileage"].type == "integer"
    assert source.read_bytes() == before


def test_manifestless_tracker_is_inspection_only_compatibility_surface(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    tracker = vault / "Knowledge Base/Records/Training Log.md"
    tracker.parent.mkdir(parents=True)
    tracker.write_text("---\ntype: tracker\n---\n\n# Training\n", encoding="utf-8")

    legacy = collections.inspect_legacy_tracker(vault, tracker)

    assert legacy.inspect_only is True
    assert legacy.path == "Knowledge Base/Records/Training Log.md"
    assert legacy.collection_id.startswith("legacy-")


def test_saved_view_normalizes_legacy_shapes_and_binds_its_definition(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = _write_manifest(
        vault,
        "Knowledge Base/Records/Maintenance/_collection.md",
        _manifest().replace(
            "  latest:\n    sort: [occurred_on, desc]",
            "  focused:\n"
            "    query:\n"
            "      filters:\n"
            "        title: Service\n"
            "      columns: [title]\n"
            "      limit: 7\n"
            "    sort: [occurred_on, desc]\n"
            f"    source_snapshot: {'a' * 64}",
        ),
    )

    manifest = collections.load_manifest(vault, path)
    view = collections.resolve_saved_view(manifest, "focused")

    assert view.definition == {
        "query": {
            "filters": [{"column": "title", "op": "eq", "value": "Service"}],
            "columns": ["title"],
            "sort_by": "occurred_on",
            "descending": True,
            "limit": 7,
        },
        "source_snapshot": "a" * 64,
    }
    original_identity = view.identity
    path.write_text(
        path.read_text(encoding="utf-8").replace("limit: 7", "limit: 8"), encoding="utf-8"
    )
    assert collections.resolve_saved_view(
        collections.load_manifest(vault, path), "focused"
    ).identity != original_identity


def test_manifest_eagerly_normalizes_views_and_keeps_one_located_diagnostic_per_invalid_view(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    path = _write_manifest(
        vault,
        "Knowledge Base/Records/Maintenance/_collection.md",
        _manifest().replace(
            "  latest:\n    sort: [occurred_on, desc]",
            "  recent:\n"
            "    query:\n"
            "      columns: [occurred_on]\n"
            "    sort: [occurred_on, desc]\n"
            "  broken:\n"
            "    query:\n"
            "      columns: [unknown]",
        ),
    )

    manifest = collections.load_manifest(vault, path)

    assert collections.resolve_saved_view(manifest, "recent").definition == {
        "query": {
            "filters": [],
            "columns": ["occurred_on"],
            "sort_by": "occurred_on",
            "descending": True,
        }
    }
    assert manifest.view_diagnostics == (
        collections.CollectionDiagnostic(
            "INVALID_SAVED_VIEW", "saved view columns are invalid", "views.broken"
        ),
    )


@pytest.mark.parametrize(
    "replacement",
    (
        "  latest:\n    query:\n      filters:\n        unknown: value",
        "  latest:\n    source_snapshot: not-a-source-hash",
        "  latest:\n    sort: [unknown, sideways]",
    ),
)
def test_manifest_refuses_malformed_saved_views(tmp_path: Path, replacement: str) -> None:
    vault = tmp_path / "vault"
    path = _write_manifest(
        vault,
        "Knowledge Base/Records/Maintenance/_collection.md",
        _manifest().replace("  latest:\n    sort: [occurred_on, desc]", replacement),
    )

    manifest = collections.load_manifest(vault, path)
    with pytest.raises(collections.CollectionError) as raised:
        collections.resolve_saved_view(manifest, "latest")

    assert raised.value.code == "INVALID_SAVED_VIEW"


@pytest.mark.parametrize(
    "view",
    (
        {"query": {"columns": [["title"]]}},
        {"query": {"filters": [{"column": "title", "op": ["eq"], "value": "Service"}]}},
        {"sort": ["title", ["desc"]]},
        {"query": {"filters": {("title",): "Service"}}},
        {"query": {"filters": [{"column": {"title": "name"}, "value": "Service"}]}},
    ),
)
def test_saved_view_normalization_refuses_nonscalar_yaml_json_shapes(view: object) -> None:
    with pytest.raises(collections.CollectionError) as raised:
        collections._normalize_saved_view(view, {"title", "occurred_on"})

    assert raised.value.code == "INVALID_SAVED_VIEW"


def test_legacy_tracker_authorizes_before_parsing_and_refuses_symlinks(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    tracker = vault / "Knowledge Base/Records/Training Log.md"
    tracker.parent.mkdir(parents=True)
    tracker.write_text("---\ntype: tracker\n---\n", encoding="utf-8")

    with pytest.raises(collections.CollectionError) as raised:
        collections.inspect_legacy_tracker(
            vault,
            tracker,
            authorize_path=lambda _path: False,
        )
    assert raised.value.code == "COLLECTION_NOT_FOUND"

    linked = vault / "Knowledge Base/Records/linked.md"
    try:
        linked.symlink_to(tracker)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(collections.CollectionError) as raised:
        collections.inspect_legacy_tracker(vault, linked)
    assert raised.value.code == "INVALID_COLLECTION_PATH"


def test_acceptance_fixtures_cover_log_item_and_query_only_dataset_storage(tmp_path: Path) -> None:
    x3 = copy_x3_fixture(tmp_path / "x3")
    vehicle = copy_vehicle_maintenance_fixture(tmp_path / "vehicle")
    dataset = copy_dataset_fixture(tmp_path / "dataset")

    assert collections.load_manifest(tmp_path / "x3", x3 / "_collection.md").storage.strategy == (
        "markdown-log"
    )
    assert (
        collections.load_manifest(tmp_path / "vehicle", vehicle / "_collection.md").storage.strategy
        == "markdown-items"
    )
    assert (
        collections.load_manifest(tmp_path / "dataset", dataset / "_collection.md").storage.strategy
        == "dataset"
    )
