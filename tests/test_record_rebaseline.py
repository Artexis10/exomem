from __future__ import annotations

from pathlib import Path

from record_fixtures import copy_x3_fixture

from exomem import record_formats, record_governance, records
from exomem import structured_collections as collections


def _activity_log(root: Path) -> None:
    path = root / "Knowledge Base/log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Activity\n", encoding="utf-8")


def _item() -> dict[str, object]:
    return {
        "occurred_on": "2026-08-03",
        "title": "Pull",
        "status": "completed",
        "movements": [{"movement": "Deadlift", "band": "grey", "repetitions": "22"}],
    }


def _rebaseline(root: Path, manifest: collections.CollectionManifest, why: str) -> None:
    snapshot = record_formats.load_adapter(root, manifest).read()
    report = records.inspect_audit_gap(root, manifest.path)
    records.rebaseline_collection(
        root,
        manifest.path,
        expected_manifest_hash=manifest.manifest_version.hash,
        expected_container_hash=records.lifecycle_guards(manifest, snapshot)["expected_container_hash"],
        acknowledged_gap_codes=tuple(report["gaps"]),
        why=why,
    )


def test_multiple_rebaselines_persist_newest_first_across_restart(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item(),
        item_key="55555555-5555-4555-8555-555555555555",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="record session",
    )
    first = collections.load_manifest(tmp_path, manifest.path)
    path = tmp_path / first.path
    path.write_text(path.read_text(encoding="utf-8").replace("title:", "title: First", 1), encoding="utf-8")
    _rebaseline(tmp_path, collections.load_manifest(tmp_path, first.path), "acknowledge first edit")
    path.write_text(path.read_text(encoding="utf-8").replace("title:", "title: Second", 1), encoding="utf-8")
    _rebaseline(tmp_path, collections.load_manifest(tmp_path, first.path), "acknowledge second edit")
    restarted = records.inspect_audit_gap(tmp_path, first.path)
    assert restarted["status"] == "acknowledged_gap"
    assert [item["rationale"] for item in restarted["discontinuities"]] == [
        "acknowledge second edit",
        "acknowledge first edit",
    ]
    query = record_governance.query_collection(tmp_path, collections.load_manifest(tmp_path, first.path), limit=1)
    projected = record_governance.project_query_result(
        query,
        collections.load_manifest(tmp_path, first.path),
        agent_history=records.agent_audit_history(tmp_path, first.path),
    )
    assert projected["agent_history"]["discontinuity"]["rationale"] == "acknowledge second edit"
    assert [item["rationale"] for item in projected["agent_history"]["discontinuities"]] == [
        "acknowledge second edit", "acknowledge first edit"
    ]


def test_rebaseline_refuses_invalid_saved_view_before_checkpointing(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    initial = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path, manifest.path, item=_item(), item_key="12121212-1212-4212-8212-121212121212",
        expected_container_hash=initial.source_versions[-1].hash, why="establish audit history",
    )
    path = tmp_path / manifest.path
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "item_schema:", "views:\n  invalid:\n    query:\n      filters: invalid\nitem_schema:", 1
        ), encoding="utf-8"
    )
    changed = collections.load_manifest(tmp_path, manifest.path)
    snapshot = record_formats.load_adapter(tmp_path, changed).read()
    before = path.read_bytes()
    report = records.inspect_audit_gap(tmp_path, changed.path)

    with __import__("pytest").raises(collections.CollectionError, match="INVALID_SAVED_VIEW"):
        records.rebaseline_collection(
            tmp_path, changed.path, expected_manifest_hash=changed.manifest_version.hash,
            expected_container_hash=records.lifecycle_guards(changed, snapshot)["expected_container_hash"],
            acknowledged_gap_codes=report["gaps"], why="do not checkpoint invalid views",
        )
    assert path.read_bytes() == before


def test_rebaseline_refuses_withheld_declared_template_before_checkpointing(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    initial = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path, manifest.path, item=_item(), item_key="23232323-2323-4232-8232-232323232323",
        expected_container_hash=initial.source_versions[-1].hash, why="establish audit history",
    )
    path = tmp_path / manifest.path
    path.write_text(path.read_text(encoding="utf-8").replace("title:", "title: Direct", 1), encoding="utf-8")
    changed = collections.load_manifest(tmp_path, manifest.path)
    snapshot = record_formats.load_adapter(tmp_path, changed).read()
    before = path.read_bytes()
    report = records.inspect_audit_gap(tmp_path, changed.path)
    denied = "Knowledge Base/Templates/Records/Health/X3/X3 Pull.md"
    released = record_governance.full_release_filter
    monkeypatch.setattr(
        record_governance, "full_release_filter",
        lambda root: lambda candidate: released(root)(candidate) and candidate != denied,
    )

    with __import__("pytest").raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
        records.rebaseline_collection(
            tmp_path, changed.path, expected_manifest_hash=changed.manifest_version.hash,
            expected_container_hash=records.lifecycle_guards(changed, snapshot)["expected_container_hash"],
            acknowledged_gap_codes=report["gaps"], why="do not checkpoint withheld template",
        )
    assert path.read_bytes() == before
