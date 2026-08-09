from __future__ import annotations

from pathlib import Path


def test_committed_schemas_match_export(tmp_path: Path) -> None:
    from protocol.models import export_json_schemas

    fresh = {path.name: path.read_bytes() for path in export_json_schemas(tmp_path)}
    committed_dir = Path("benchmarks/protocol/schema")
    committed = {path.name: path.read_bytes() for path in committed_dir.glob("*.schema.json")}
    assert fresh == committed
