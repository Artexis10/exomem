"""End-to-end product paths for the five-action Records command."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from record_fixtures import (
    copy_dataset_fixture,
    copy_vehicle_maintenance_fixture,
    copy_x3_fixture,
    x3_template_directory,
)

from exomem import bm25, recall_policy, record_governance
from exomem.cli_ops import OpError
from exomem.commands import op_record_memory
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
from exomem.structured_collections import load_manifest


def _activity_log(vault: Path) -> None:
    path = vault / "Knowledge Base" / "log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Activity\n", encoding="utf-8")


def _collection_path(vault: Path, fixture: Path) -> str:
    return (fixture / "_collection.md").relative_to(vault).as_posix()


def _source_hash(result: dict[str, object], suffix: str) -> str:
    versions = result["source_versions"]
    assert isinstance(versions, list)
    return next(
        version["hash"]
        for version in versions
        if isinstance(version, dict) and isinstance(version.get("path"), str)
        and version["path"].endswith(suffix)
    )


def _files(vault: Path) -> set[str]:
    return {path.relative_to(vault).as_posix() for path in vault.rglob("*") if path.is_file()}


def _write_l6_rule(vault: Path, *, paths: str) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "records.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        "name: Records\n"
        f'paths: ["{paths}"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "external.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
        "audience: external\nceiling: 6\n",
        encoding="utf-8",
    )


def _withhold(vault: Path, relative: str) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes" / "withheld.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FZZ\n"
        "name: Withheld\n"
        f'paths: ["{relative}"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "withheld.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FZY\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FZZ"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )


def _release_link_targets(vault: Path) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes" / "evidence.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAA\n"
        "name: Record link targets\n"
        'paths: ["Evidence/**", "Assets/**"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "external.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FAA"]\n'
        "audience: external\nceiling: 6\n",
        encoding="utf-8",
    )


def _deny_external_records(vault: Path) -> None:
    (vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )


def test_x3_public_record_path_keeps_manual_templates_and_derived_views_ephemeral(
    tmp_path: Path,
) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    collection = _collection_path(tmp_path, fixture)
    log = fixture / "Training Log.md"
    template_root = x3_template_directory(tmp_path)
    archive_before = (fixture / "Historical Reps (undated).md").read_bytes()
    templates_before = {
        name: (template_root / name).read_bytes() for name in ("X3 Push.md", "X3 Pull.md")
    }

    # This is deliberately ordinary template insertion: no command owns or rewrites it.
    inserted = "\n".join(
        (
            (template_root / "X3 Push.md").read_text(encoding="utf-8").replace("{{date}}", "2026-08-03"),
            (template_root / "X3 Pull.md").read_text(encoding="utf-8").replace("{{date}}", "2026-08-02"),
        )
    )
    log.write_text(
        log.read_text(encoding="utf-8").replace(
            "## Sessions (newest first)\n", f"## Sessions (newest first)\n\n{inserted}\n", 1
        ),
        encoding="utf-8",
    )

    before = op_record_memory(
        tmp_path,
        action="query",
        collection=collection,
        columns=["occurred_on", "title", "status", "movements"],
        limit=100,
    )
    assert any(row["occurred_on"] == "2026-08-03" for row in before["rows"])
    assert (fixture / "Historical Reps (undated).md").read_bytes() == archive_before
    assert {name: (template_root / name).read_bytes() for name in templates_before} == templates_before

    key = "77777777-7777-4777-8777-777777777777"
    appended = op_record_memory(
        tmp_path,
        action="append",
        collection=collection,
        item={
            "occurred_on": "2026-08-04",
            "title": "Pull",
            "status": "completed",
            "movements": [{"movement": "Deadlift", "band": "grey", "repetitions": "22"}],
        },
        item_key=key,
        expected_container_hash=_source_hash(before, "Training Log.md"),
        why="log a completed X3 session",
    )
    assert appended["operation"] == "append"
    assert appended["affected_paths"] == [log.relative_to(tmp_path).as_posix()]

    after_append = op_record_memory(
        tmp_path,
        action="query",
        collection=collection,
        columns=["occurred_on", "title", "movements"],
        limit=100,
    )
    added = next(row for row in after_append["rows"] if row["record_id"] == key)
    updated = op_record_memory(
        tmp_path,
        action="update",
        collection=collection,
        item_key=key,
        changes={"title": "Push"},
        expected_container_hash=_source_hash(after_append, "Training Log.md"),
        expected_item_version=added["item_version"],
        why="correct the workout type",
    )
    assert updated["operation"] == "update"
    assert updated["affected_paths"] == [log.relative_to(tmp_path).as_posix()]

    files_before_views = _files(tmp_path)
    views = {
        output: op_record_memory(
            tmp_path,
            action="query",
            collection=collection,
            columns=["occurred_on", "title", "movements"],
            date_from="2026-06-01",
            date_to="2026-08-31",
            date_column="occurred_on",
            limit=100,
            output_format=output,
        )
        for output in ("json", "markdown", "csv")
    }
    assert _files(tmp_path) == files_before_views
    assert all(view["derived"] is True for view in views.values())
    assert views["json"]["rendered"].startswith("{")
    assert "| occurred_on" in views["markdown"]["rendered"]
    assert views["csv"]["rendered"].startswith("# collection_id:")
    assert "occurred_on,title,movements" in views["csv"]["rendered"]
    # Generic machinery reports observed values; it never assigns a success/regression judgment.
    assert "regression" not in views["markdown"]["rendered"].lower()

    manifest = load_manifest(tmp_path, tmp_path / collection)
    descriptor = record_governance.project_manifest(tmp_path, manifest)
    assert descriptor["plans"] == [{"reference": "exomem://memory/81947000-4c22-46e4-9874-23fed028314b", "query": {"filters": {"status": "completed"}, "limit": 24}}]

    # A direct human edit remains canonical and query-visible, while audit records its drift.
    log.write_text(log.read_text(encoding="utf-8").replace("Deadlift | grey | 22", "Deadlift | grey | 23", 1), encoding="utf-8")
    reconciled = op_record_memory(
        tmp_path, action="query", collection=collection, columns=["movements"], limit=100
    )
    assert any(
        movement["repetitions"] == "23"
        for row in reconciled["rows"]
        for movement in row.get("movements", [])
        if movement.get("movement") == "Deadlift"
    )
    assert op_record_memory(tmp_path, action="inspect", collection=collection)["audit"]["status"] == "gap"

    bm25.clear_cache()
    assert log.relative_to(tmp_path).as_posix() not in {
        path for path, _score in bm25.search(tmp_path, "Deadlift", k=10)
    }
    assert not recall_policy.is_recall_candidate(tmp_path, log)

    _write_l6_rule(tmp_path, paths="Records/**")
    _deny_external_records(tmp_path)
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        with pytest.raises(OpError, match="^COLLECTION_NOT_FOUND:"):
            op_record_memory(tmp_path, action="inspect", collection=collection)


def test_vehicle_governance_filters_before_reduction_and_allows_exact_correction(
    tmp_path: Path,
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    collection = _collection_path(tmp_path, fixture)
    withheld = fixture / "Events" / "withheld" / "2026-06-01-inspection.md"
    evidence = tmp_path / "Knowledge Base" / "Evidence" / "Receipts" / "oil-change.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("# Oil-change receipt\n", encoding="utf-8")
    asset = tmp_path / "Knowledge Base" / "Assets" / "Vehicle.md"
    asset.parent.mkdir(parents=True)
    asset.write_text("# Vehicle\n", encoding="utf-8")
    _write_l6_rule(tmp_path, paths="Records/**")
    _release_link_targets(tmp_path)
    _withhold(tmp_path, withheld.relative_to(tmp_path).as_posix())

    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        released = op_record_memory(
            tmp_path,
            action="query",
            collection=collection,
            columns=["occurred_on", "provider", "amount", "receipt"],
            aggregate="sum:amount",
            limit=100,
        )
    assert released["total_matched"] == 2
    assert released["aggregate"] == {"sum": 92.5, "n": 2}
    assert "Inspection Centre" not in released["rendered"]
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        released_rows = op_record_memory(
            tmp_path,
            action="query",
            collection=collection,
            columns=["occurred_on", "provider", "amount", "receipt"],
            limit=100,
        )
    assert any(
        row.get("receipt") == "[[Evidence/Receipts/oil-change]]" for row in released_rows["rows"]
    )

    with request_scope(owner_principal()):
        unrestricted = op_record_memory(
            tmp_path,
            action="query",
            collection=collection,
            columns=["provider", "status"],
            limit=100,
        )
    target = next(row for row in unrestricted["rows"] if row["provider"] == "Northside Garage")
    with request_scope(owner_principal()):
        corrected = op_record_memory(
            tmp_path,
            action="update",
            collection=collection,
            item_key=target["record_id"],
            changes={"status": "scheduled"},
            expected_container_hash=unrestricted["snapshot"],
            expected_item_version=target["item_version"],
            why="correct the maintenance state",
        )
    assert corrected["affected_paths"] == [
        "Knowledge Base/Records/vehicle-maintenance/Events/released/2026-06-01-oil.md"
    ]
    assert "status: scheduled" in (fixture / "Events" / "released" / "2026-06-01-oil.md").read_text(encoding="utf-8")


def test_dataset_public_path_enforces_caps_snapshot_drift_and_read_only_storage(
    tmp_path: Path,
) -> None:
    fixture = copy_dataset_fixture(tmp_path)
    _activity_log(tmp_path)
    collection = _collection_path(tmp_path, fixture)
    source = fixture / "readings.csv"

    first = op_record_memory(
        tmp_path,
        action="query",
        collection=collection,
        columns=["reading_id", "category", "value"],
        limit=1,
    )
    assert first["returned"] == 1
    assert first["truncated"] is True
    assert first["continuation"]

    source.write_text(source.read_text(encoding="utf-8") + "r-073,2026-07-01,manual,73\n", encoding="utf-8")
    with pytest.raises(OpError, match="^STALE_RECORD_SNAPSHOT:"):
        op_record_memory(
            tmp_path,
            action="query",
            collection=collection,
            columns=["reading_id", "category", "value"],
            limit=1,
            continuation=first["continuation"],
        )

    before = source.read_bytes()
    with pytest.raises(OpError, match="^UNSUPPORTED_RECORD_MUTATION:"):
        op_record_memory(
            tmp_path,
            action="append",
            collection=collection,
            item={
                "reading_id": "r-074",
                "occurred_on": "2026-07-02",
                "category": "manual",
                "value": 74,
            },
            item_key="r-074",
            expected_container_hash=hashlib.sha256(before).hexdigest(),
            why="datasets remain query-only",
        )
    assert source.read_bytes() == before
