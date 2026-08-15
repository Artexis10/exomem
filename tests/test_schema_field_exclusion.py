from __future__ import annotations

from pathlib import Path

import pytest

from exomem import audit as audit_module
from exomem import create_file as create_file_module
from exomem import (
    planning,
    recall_policy,
    record_formats,
    record_governance,
    records,
    vault,
)
from exomem import set_frontmatter_field as set_frontmatter_field_module
from exomem import structured_collections as collections

COLLECTION_ID = "12345678-1234-4abc-8def-123456789abc"
RECORD_ID = "11111111-1111-4111-8111-111111111111"
SECOND_RECORD_ID = "22222222-2222-4222-8222-222222222222"


def _seed_vault(root: Path) -> None:
    kb = root / "Knowledge Base"
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "log.md").write_text("# Activity\n", encoding="utf-8")


def _field_block(name: str, *, required: bool = False) -> str:
    return (
        f"    {name}:\n"
        "      type: string\n"
        + ("      required: true\n" if required else "")
    )


def _records_manifest(
    *, excluded_field: str | None = None, note_field: str | None = None
) -> str:
    storage = (
        "storage:\n"
        "  strategy: markdown-log\n"
        "  source: Records.md\n"
        "  format_version: 1\n"
        "  section:\n"
        "    level: 2\n"
        "    title: Records\n"
        "  item_heading:\n"
        "    level: 3\n"
        "    fields:\n"
        "      - name: title\n"
        "        type: string\n"
        "    separator: ' · '\n"
        f"    note:\n      field: {note_field}\n      open: ' ('\n      close: ')'\n"
        "  insertion: newest-first\n"
        "  child_rows:\n"
        "    prefix: '- '\n"
        "    delimiter: '|'\n"
        "    fields: [value]\n"
        "    container_field: entries\n"
        if note_field is not None
        else "storage:\n  strategy: markdown-items\n  source: Items\n  format_version: 1\n"
    )
    fields = _field_block("title", required=True)
    if note_field is not None:
        fields += "    entries:\n      type: array\n      items:\n        type: object\n"
    if excluded_field is not None:
        fields += _field_block(excluded_field)
    return (
        "---\n"
        "type: collection\n"
        f"exomem_id: {COLLECTION_ID}\n"
        "title: Test records\n"
        "semantic_profile: records\n"
        "collection_version: 1\n"
        "schema_version: 1\n"
        "lifecycle: active\n"
        f"{storage}"
        "item_schema:\n"
        "  natural_key: [title]\n"
        "  fields:\n"
        f"{fields}"
        "---\n"
    )


def _planning_manifest(*, excluded_field: str | None = None) -> str:
    fields = "".join(
        _field_block(name, required=name == "title")
        for name in (
            "title",
            "kind",
            "status",
            "lifecycle",
            "priority",
            "commitment",
            "horizon",
            "health",
            "area",
            "parent",
        )
    )
    if excluded_field is not None:
        fields += _field_block(excluded_field)
    return (
        "---\n"
        "type: collection\n"
        f"exomem_id: {COLLECTION_ID}\n"
        "title: Planning work\n"
        "semantic_profile: planning\n"
        "collection_version: 1\n"
        "schema_version: 1\n"
        "lifecycle: active\n"
        "storage:\n"
        "  strategy: markdown-items\n"
        "  source: Items\n"
        "  format_version: 1\n"
        "item_schema:\n"
        "  natural_key: [title]\n"
        "  fields:\n"
        f"{fields}"
        "---\n"
    )


def _plant_grandfathered_records(root: Path, field: str = "confidence") -> Path:
    """Plant directly because governed collection creation refuses this after the fence."""
    _seed_vault(root)
    directory = root / "Knowledge Base" / "Records" / "Grandfathered"
    items = directory / "Items"
    items.mkdir(parents=True)
    manifest = directory / "_collection.md"
    manifest.write_text(_records_manifest(excluded_field=field), encoding="utf-8")
    (items / f"{RECORD_ID}.md").write_text(
        "---\n"
        "type: record\n"
        f"collection_id: {COLLECTION_ID}\n"
        f"record_id: {RECORD_ID}\n"
        "schema_version: 1\n"
        "title: Existing\n"
        f"{field}: '0.75'\n"
        "---\n\nGrandfathered item.\n",
        encoding="utf-8",
    )
    return manifest


def _plant_grandfathered_planning(root: Path, field: str = "confidence") -> Path:
    """Plant directly because governed Planning creation refuses this after the fence."""
    _seed_vault(root)
    directory = root / "Knowledge Base" / "Planning" / "Grandfathered"
    items = directory / "Items"
    items.mkdir(parents=True)
    manifest = directory / "_collection.md"
    manifest.write_text(_planning_manifest(excluded_field=field), encoding="utf-8")
    (items / f"{RECORD_ID}.md").write_text(
        "---\n"
        "type: plan\n"
        f"collection_id: {COLLECTION_ID}\n"
        f"plan_id: {RECORD_ID}\n"
        "schema_version: 1\n"
        "title: Existing\n"
        "kind: work-item\n"
        "status: candidate\n"
        "lifecycle: active\n"
        "priority: none\n"
        "commitment: uncommitted\n"
        "horizon: inbox\n"
        "health: unknown\n"
        f"{field}: '0.75'\n"
        "---\n\nGrandfathered plan.\n",
        encoding="utf-8",
    )
    return manifest


def _assert_excluded(
    error: (
        create_file_module.CreateFileError
        | set_frontmatter_field_module.SetFrontmatterError
        | collections.CollectionError
    ),
    field: str,
) -> None:
    assert error.code == "EXCLUDED_FIELD"
    assert field.strip().casefold() in error.reason.casefold()


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (("title", "Confidence", "decay_at"), "Confidence"),
        ((None, 42, "DECAY_AT"), "DECAY_AT"),
        (("title", " expires_at "), " expires_at "),
    ],
)
def test_first_excluded_field_returns_original_name_and_reason(
    names: tuple[object, ...], expected: str
) -> None:
    result = vault.first_excluded_field(names)

    assert result is not None
    assert result == (expected, vault.excluded_frontmatter_reason(expected))


@pytest.mark.parametrize("names", [(), (None, 42, object()), ("title", "status")])
def test_first_excluded_field_returns_none_for_clean_input(names: tuple[object, ...]) -> None:
    assert vault.first_excluded_field(names) is None


def test_shared_error_code_is_used_by_existing_file_surfaces(tmp_path: Path) -> None:
    assert vault.EXCLUDED_FIELD_CODE == "EXCLUDED_FIELD"

    with pytest.raises(create_file_module.CreateFileError) as create_error:
        create_file_module.create_file(
            tmp_path,
            path="outside-any-valid-root.md",
            content="body",
            frontmatter={"Expires_At": "2026-09-01"},
        )
    assert create_error.value.code == vault.EXCLUDED_FIELD_CODE

    path = tmp_path / "Knowledge Base" / "Notes" / "note.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntype: note\n---\nbody\n", encoding="utf-8")
    with pytest.raises(set_frontmatter_field_module.SetFrontmatterError) as set_error:
        set_frontmatter_field_module.set_frontmatter_field(
            tmp_path,
            path="Knowledge Base/Notes/note.md",
            field=" expires_at ",
            value="2026-09-01",
            why="exercise exclusion",
        )
    assert set_error.value.code == vault.EXCLUDED_FIELD_CODE


def test_grandfathered_collection_stays_loadable_queryable_and_recallable(tmp_path: Path) -> None:
    """Plant directly because governed collection creation refuses this after the fence."""
    path = _plant_grandfathered_records(tmp_path)

    loaded = collections.load_manifest(tmp_path, path)
    resolved = collections.resolve_collection(tmp_path, path)
    discovered = collections.discover_collections(tmp_path)
    queried = record_governance.query_collection(tmp_path, path)

    assert loaded.path == resolved.path
    assert [manifest.path for manifest in discovered] == [loaded.path]
    assert queried.rows[0]["confidence"] == "0.75"
    assert record_formats.load_adapter(tmp_path, loaded).read().records[0].values["confidence"] == "0.75"
    assert recall_policy.is_recall_candidate(tmp_path, path)


def test_delete_then_revise_repairs_a_grandfathered_collection(tmp_path: Path) -> None:
    path = _plant_grandfathered_records(tmp_path)
    manifest = collections.load_manifest(tmp_path, path)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    record = snapshot.records[0]

    records.update_record(
        tmp_path,
        manifest,
        item_key=record.identity.key,
        changes={},
        delete_fields=("confidence",),
        expected_container_hash=snapshot.snapshot,
        expected_item_version=record.source.hash,
        why="remove excluded field",
    )
    current = collections.load_manifest(tmp_path, path)
    current_snapshot = record_formats.load_adapter(tmp_path, current).read()
    before = path.read_bytes()
    proposed = path.read_text(encoding="utf-8").replace(
        "    confidence:\n      type: string\n", ""
    )

    result = records.revise_collection(
        tmp_path,
        current,
        manifest_text=proposed,
        expected_manifest_hash=current.manifest_version.hash,
        expected_container_hash=records.lifecycle_guards(current, current_snapshot)[
            "expected_container_hash"
        ],
        why="remove excluded schema field",
    )

    assert result["operation"] == "revise"
    assert path.read_bytes() != before
    assert "confidence" not in collections.load_manifest(tmp_path, path).schema.fields


def test_rebaseline_never_refuses_a_grandfathered_collection_as_excluded(tmp_path: Path) -> None:
    path = _plant_grandfathered_records(tmp_path)
    manifest = collections.load_manifest(tmp_path, path)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    record = snapshot.records[0]
    records.update_record(
        tmp_path,
        manifest,
        item_key=record.identity.key,
        changes={"title": "Still grandfathered"},
        expected_container_hash=snapshot.snapshot,
        expected_item_version=record.source.hash,
        why="make a governed transition",
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace("title: Test records", "title: Direct edit"),
        encoding="utf-8",
    )
    current = collections.load_manifest(tmp_path, path)
    current_snapshot = record_formats.load_adapter(tmp_path, current).read()
    gap = records.inspect_audit_gap(tmp_path, current)

    try:
        result = records.rebaseline_collection(
            tmp_path,
            current,
            expected_manifest_hash=current.manifest_version.hash,
            expected_container_hash=records.lifecycle_guards(current, current_snapshot)[
                "expected_container_hash"
            ],
            acknowledged_gap_codes=tuple(gap["gaps"]),
            why="acknowledge direct edit",
        )
    except collections.CollectionError as error:
        assert error.code != "EXCLUDED_FIELD"
        raise

    assert result["operation"] == "rebaseline"


def test_unrelated_update_ignores_stored_excluded_value(tmp_path: Path) -> None:
    path = _plant_grandfathered_records(tmp_path)
    manifest = collections.load_manifest(tmp_path, path)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    record = snapshot.records[0]

    result = records.update_record(
        tmp_path,
        manifest,
        item_key=record.identity.key,
        changes={"title": "Unrelated change"},
        expected_container_hash=snapshot.snapshot,
        expected_item_version=record.source.hash,
        why="change an unrelated field",
    )

    assert result["operation"] == "update"
    current = record_formats.load_adapter(
        tmp_path, collections.load_manifest(tmp_path, path)
    ).read().records[0]
    assert current.values["confidence"] == "0.75"


@pytest.mark.parametrize("field", ["confidence", "DECAY_AT", "Expires_At"])
def test_create_file_raw_markdown_refuses_excluded_frontmatter(
    tmp_path: Path, field: str
) -> None:
    _seed_vault(tmp_path)
    target = tmp_path / "Knowledge Base" / "Notes" / "raw.md"
    with pytest.raises(create_file_module.CreateFileError) as raised:
        create_file_module.create_file(
            tmp_path,
            path="Knowledge Base/Notes/raw.md",
            content=f"---\ntype: note\n{field}: value\n---\nbody\n",
        )

    _assert_excluded(raised.value, field)
    assert not target.exists()


def test_create_file_overwrite_refuses_excluded_frontmatter_without_changing_bytes(
    tmp_path: Path,
) -> None:
    _seed_vault(tmp_path)
    target = tmp_path / "Knowledge Base" / "Notes" / "existing.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\ntype: note\n---\noriginal\n", encoding="utf-8")
    before = target.read_bytes()

    with pytest.raises(create_file_module.CreateFileError) as raised:
        create_file_module.create_file(
            tmp_path,
            path="Knowledge Base/Notes/existing.md",
            content="---\ntype: note\ndecay_at: 2026-09-01\n---\nreplacement\n",
            overwrite=True,
        )

    _assert_excluded(raised.value, "decay_at")
    assert target.read_bytes() == before


def test_create_file_refuses_hand_authored_collection_schema(tmp_path: Path) -> None:
    _seed_vault(tmp_path)
    relative = "Knowledge Base/Records/Hand Authored/_collection.md"

    with pytest.raises(create_file_module.CreateFileError) as raised:
        create_file_module.create_file(
            tmp_path,
            path=relative,
            content=_records_manifest(excluded_field="Confidence"),
        )

    _assert_excluded(raised.value, "Confidence")
    assert not (tmp_path / relative).exists()


@pytest.mark.parametrize("operation", ["create", "validate_create"])
@pytest.mark.parametrize("field", ["confidence", "DECAY_AT", "Expires_At"])
def test_records_collection_create_surfaces_refuse_excluded_schema_field(
    tmp_path: Path, operation: str, field: str
) -> None:
    _seed_vault(tmp_path)
    relative = "Knowledge Base/Records/New/_collection.md"
    manifest = _records_manifest(excluded_field=field)

    with pytest.raises(collections.CollectionError) as raised:
        if operation == "create":
            records.create_collection(tmp_path, relative, manifest, why="create records")
        else:
            records.validate_collection_create(tmp_path, relative, manifest)

    _assert_excluded(raised.value, field)
    assert raised.value.details["field"] == field
    assert not (tmp_path / relative).exists()


@pytest.mark.parametrize("operation", ["revise", "validate_revision"])
def test_records_collection_revision_surfaces_refuse_before_representation_check(
    tmp_path: Path, operation: str
) -> None:
    _seed_vault(tmp_path)
    relative = "Knowledge Base/Records/New/_collection.md"
    records.create_collection(
        tmp_path, relative, _records_manifest(), why="create clean records"
    )
    current = collections.load_manifest(tmp_path, relative)
    snapshot = record_formats.load_adapter(tmp_path, current).read()
    before = (tmp_path / relative).read_bytes()
    proposed = _records_manifest(excluded_field="confidence").replace(
        "source: Items", "source: OtherItems"
    )

    with pytest.raises(collections.CollectionError) as raised:
        if operation == "revise":
            records.revise_collection(
                tmp_path,
                current,
                manifest_text=proposed,
                expected_manifest_hash=current.manifest_version.hash,
                expected_container_hash=records.lifecycle_guards(current, snapshot)[
                    "expected_container_hash"
                ],
                why="try excluded revision",
            )
        else:
            records.validate_collection_revision(tmp_path, current, proposed)

    _assert_excluded(raised.value, "confidence")
    assert raised.value.details["field"] == "confidence"
    assert (tmp_path / relative).read_bytes() == before


def test_markdown_log_note_field_cannot_use_an_excluded_name(tmp_path: Path) -> None:
    _seed_vault(tmp_path)
    relative = "Knowledge Base/Records/Log/_collection.md"

    with pytest.raises(collections.CollectionError) as raised:
        records.create_collection(
            tmp_path,
            relative,
            _records_manifest(note_field="Expires_At"),
            why="create log collection",
        )

    _assert_excluded(raised.value, "Expires_At")
    assert raised.value.details["field"] == "Expires_At"
    assert not (tmp_path / relative).exists()


def test_planning_collection_create_refuses_excluded_schema_field(tmp_path: Path) -> None:
    _seed_vault(tmp_path)
    relative = "Knowledge Base/Planning/Work/_collection.md"

    with pytest.raises(collections.CollectionError) as raised:
        planning.create_collection(
            tmp_path,
            relative,
            _planning_manifest(excluded_field="Expires_At"),
            why="create planning collection",
        )

    _assert_excluded(raised.value, "Expires_At")
    assert not (tmp_path / relative).exists()


@pytest.mark.parametrize("operation", ["append", "update"])
def test_record_item_authored_values_refuse_against_grandfathered_schema(
    tmp_path: Path, operation: str
) -> None:
    path = _plant_grandfathered_records(tmp_path, "Expires_At")
    manifest = collections.load_manifest(tmp_path, path)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()

    with pytest.raises(collections.CollectionError) as raised:
        if operation == "append":
            records.append_record(
                tmp_path,
                manifest,
                item={"title": "New", "Expires_At": "2026-09-01"},
                item_key=SECOND_RECORD_ID,
                expected_container_hash=snapshot.snapshot,
                why="append excluded value",
            )
        else:
            existing = snapshot.records[0]
            records.update_record(
                tmp_path,
                manifest,
                item_key=existing.identity.key,
                changes={"Expires_At": "2026-09-01"},
                expected_container_hash=snapshot.snapshot,
                expected_item_version=existing.source.hash,
                why="update excluded value",
            )

    _assert_excluded(raised.value, "Expires_At")
    assert raised.value.details["field"] == "Expires_At"


def test_append_refuses_excluded_item_before_collection_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("collection resolution ran before the exclusion fence")

    monkeypatch.setattr(
        records.record_governance, "resolve_collection_for_mutation", fail_resolution
    )

    with pytest.raises(collections.CollectionError) as raised:
        records.append_record(
            tmp_path,
            "missing",
            item={"confidence": 0.5},
            why="exercise ordering",
        )

    _assert_excluded(raised.value, "confidence")


def test_update_refuses_excluded_changes_before_writer_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_manager() -> object:
        raise AssertionError("writer lease was requested before the exclusion fence")

    monkeypatch.setattr(records.writer_lease, "active_manager", fail_manager)

    with pytest.raises(collections.CollectionError) as raised:
        records.update_record(
            tmp_path,
            "missing",
            item_key=RECORD_ID,
            changes={"DECAY_AT": "2026-09-01"},
            expected_container_hash="0" * 64,
            expected_item_version="0" * 64,
            why="exercise ordering",
        )

    _assert_excluded(raised.value, "DECAY_AT")


@pytest.mark.parametrize("operation", ["add", "update"])
def test_planning_authored_values_inherit_the_shared_exclusion_fence(
    tmp_path: Path, operation: str
) -> None:
    path = _plant_grandfathered_planning(tmp_path)
    manifest = collections.load_manifest(tmp_path, path)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()

    with pytest.raises(collections.CollectionError) as raised:
        if operation == "add":
            planning.add(
                tmp_path,
                manifest,
                item={"title": "New", "confidence": "0.5"},
                plan_id=SECOND_RECORD_ID,
                expected_container_hash=snapshot.snapshot,
                why="add excluded planning value",
            )
        else:
            record = snapshot.records[0]
            planning.update(
                tmp_path,
                manifest,
                plan_id=record.identity.key,
                changes={"confidence": "0.5"},
                expected_container_hash=snapshot.snapshot,
                expected_item_version=record.source.hash,
                why="update excluded planning value",
            )

    _assert_excluded(raised.value, "confidence")


def test_planning_triage_ignores_a_grandfathered_stored_excluded_value(
    tmp_path: Path,
) -> None:
    path = _plant_grandfathered_planning(tmp_path)
    manifest = collections.load_manifest(tmp_path, path)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    record = snapshot.records[0]

    result = planning.triage(
        tmp_path,
        manifest,
        plan_id=record.identity.key,
        transition={
            "status": "planned",
            "commitment": "considering",
            "horizon": "quarter",
        },
        expected_container_hash=snapshot.snapshot,
        expected_item_version=record.source.hash,
        why="triage grandfathered plan",
    )

    assert result["operation"] == "triage"


def test_audit_warns_for_top_level_and_collection_schema_violations(tmp_path: Path) -> None:
    _seed_vault(tmp_path)
    note = tmp_path / "Knowledge Base" / "Notes" / "Insights" / "old.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: insight\nstatus: active\ncreated: 2026-01-01\n"
        "updated: 2026-01-01\nconfidence: 0.5\n---\nbody\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "Knowledge Base" / "Records" / "Old" / "_collection.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(_records_manifest(excluded_field="Expires_At"), encoding="utf-8")

    report = audit_module.audit(tmp_path, categories=["frontmatter_compliance"])
    matches = [
        finding
        for finding in report.findings
        if finding.path in {
            note.relative_to(tmp_path).as_posix(),
            manifest.relative_to(tmp_path).as_posix(),
        }
        and "schema-excluded" in finding.detail
    ]

    assert len(matches) == 2, [finding.as_dict() for finding in report.findings]
    assert {finding.severity for finding in matches} == {"warn"}
    collection_finding = next(finding for finding in matches if finding.path == manifest.relative_to(tmp_path).as_posix())
    assert "delete" in collection_finding.proposed_fix.lower()
    assert "every item" in collection_finding.proposed_fix.lower()
    assert collection_finding.proposed_fix.lower().index("delete") < collection_finding.proposed_fix.lower().index("revise")


def test_manifest_authoring_contract_discloses_excluded_field_names() -> None:
    fields = collections.manifest_authoring_contract()["json_schema"]["properties"][
        "item_schema"
    ]["properties"]["fields"]

    property_names = fields["propertyNames"]
    assert property_names["not"]["enum"] == sorted(vault.EXCLUDED_FRONTMATTER_FIELDS)

    # The enum matches literally, but enforcement casefolds and strips. A client
    # authoring from `describe` alone must learn that from the contract rather
    # than by being refused at runtime, so the description carries it.
    description = property_names["description"].casefold()
    assert "case" in description
    assert "whitespace" in description
    # The second declaration surface is enforced but not modelled in the storage
    # node, so the contract must name it here or it stays undiscoverable.
    assert "note" in description


def test_create_file_refuses_hand_authored_markdown_log_note_field(tmp_path: Path) -> None:
    """The note field is a second declaration surface, and an immutable one.

    A name declared under `storage.item_heading.note.field` becomes a legal item
    key outside `item_schema.fields`. Because it lives in the storage descriptor,
    revising it away refuses with IMMUTABLE_COLLECTION_REPRESENTATION, so a write
    that slips past this fence can never be repaired.
    """
    _seed_vault(tmp_path)
    relative = "Knowledge Base/Records/Hand Authored Log/_collection.md"

    with pytest.raises(create_file_module.CreateFileError) as raised:
        create_file_module.create_file(
            tmp_path,
            path=relative,
            content=_records_manifest(note_field="confidence"),
        )

    _assert_excluded(raised.value, "confidence")
    assert not (tmp_path / relative).exists()


def test_audit_warns_for_markdown_log_note_field_violation(tmp_path: Path) -> None:
    _seed_vault(tmp_path)
    manifest = tmp_path / "Knowledge Base" / "Records" / "OldLog" / "_collection.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(_records_manifest(note_field="confidence"), encoding="utf-8")

    report = audit_module.audit(tmp_path, categories=["frontmatter_compliance"])
    matches = [
        finding
        for finding in report.findings
        if finding.path == manifest.relative_to(tmp_path).as_posix()
        and "schema-excluded" in finding.detail
    ]

    assert len(matches) == 1, [finding.as_dict() for finding in report.findings]
    assert matches[0].severity == "warn"
