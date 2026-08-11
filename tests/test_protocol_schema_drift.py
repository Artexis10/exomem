from __future__ import annotations

from pathlib import Path


def test_committed_schemas_match_export(tmp_path: Path) -> None:
    from protocol.models import export_json_schemas

    fresh = {path.name: path.read_bytes() for path in export_json_schemas(tmp_path)}
    committed_dir = Path("benchmarks/protocol/schema")
    committed = {path.name: path.read_bytes() for path in committed_dir.glob("*.schema.json")}
    assert fresh == committed


def test_only_comparative_run_manifest_schema_moves_to_v2(tmp_path: Path) -> None:
    from protocol.models import export_json_schemas

    names = {path.name for path in export_json_schemas(tmp_path)}
    assert "run-manifest.v2.schema.json" in names
    assert "run-manifest.v1.schema.json" not in names
    assert "case-trace.v1.schema.json" in names
    assert "memorybench-run-plan.v1.schema.json" in names


def test_memorybench_contract_schemas_are_committed(tmp_path: Path) -> None:
    from protocol.models import export_json_schemas

    fresh = {path.name for path in export_json_schemas(tmp_path)}
    expected = {
        "memorybench-run-plan.v1.schema.json",
        "memorybench-export.v1.schema.json",
        "memorybench-private-gold.v1.schema.json",
        "guest-cleanup-plan.v1.schema.json",
        "guest-cleanup.v1.schema.json",
    }
    assert {
        name
        for name in fresh
        if name.startswith("memorybench-") or name.startswith("guest-cleanup")
    } == expected
    committed = {path.name for path in Path("benchmarks/protocol/schema").glob("*.schema.json")}
    assert expected <= committed
