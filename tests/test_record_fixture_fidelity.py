from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from record_fixtures import (
    copy_dataset_fixture,
    copy_vehicle_maintenance_fixture,
    copy_x3_fixture,
)


def test_x3_fixture_has_the_declared_live_section_legend_archive_and_edge_notation(
    tmp_path: Path,
) -> None:
    fixture = copy_x3_fixture(tmp_path)
    log = (fixture / "Training Log.md").read_text(encoding="utf-8")

    assert fixture == tmp_path / "Knowledge Base" / "Records" / "Health" / "X3"
    assert "## Sessions (newest first)" in log
    assert "## Legend" in log
    assert "Historical Reps (undated).md" not in log
    assert (fixture / "Historical Reps (undated).md").exists()
    assert "21+6" in log and "| " in log and "Overhead Press | white short paraforce |" in log
    assert "### 2026-08-02 · Push" in log
    assert "X3 Push" in log and "X3 Pull" in log


def test_x3_fixture_matches_the_read_only_current_vault_files(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    expected = {
        "Training Log.md": "55d23f86590d15b2a5538ccef3e0e6005e239a424f37269836ef90b791bab527",
        "Historical Reps (undated).md": "5599a2adb84d3e57c80790a8ad15358331c9b77cac3241b0bb160e8bef34ac14",
        "X3 Push.md": "cdc34219e9750d49289c2d027feec72e909e6ed2e1318c4ddb20a50523fa2736",
        "X3 Pull.md": "3d20ea58e746270ac24b5256dbd058bac78bd641ca83fd805f1d7b2e6228f80a",
    }

    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in fixture.glob("*.md")
        if path.name in expected
    } == expected


def test_vehicle_fixture_uses_path_governance_and_complete_cross_domain_fields(
    tmp_path: Path,
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = (fixture / "_collection.md").read_text(encoding="utf-8")

    assert "asset:" in manifest
    assert "odometer:" in manifest
    assert "provider:" in manifest
    assert "services:" in manifest
    assert "next_due_on:" in manifest
    assert "next_due_odometer:" in manifest
    released = fixture / "Events" / "released" / "2026-06-01-oil.md"
    withheld = fixture / "Events" / "withheld" / "2026-06-01-inspection.md"
    assert released.exists() and withheld.exists()
    assert "governance:" not in released.read_text(encoding="utf-8")
    assert "governance:" not in withheld.read_text(encoding="utf-8")


def test_dataset_fixture_has_exact_clean_declared_rows_and_high_cardinality_values(
    tmp_path: Path,
) -> None:
    fixture = copy_dataset_fixture(tmp_path)
    with (fixture / "readings.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))

    assert rows[:4] == [
        {
            "reading_id": "r-001",
            "occurred_on": "2026-01-01",
            "category": "electricity",
            "value": "101",
        },
        {
            "reading_id": "r-002",
            "occurred_on": "2026-02-01",
            "category": "electricity",
            "value": "117",
        },
        {"reading_id": "r-003", "occurred_on": "2026-03-01", "category": "gas", "value": "44"},
        {"reading_id": "r-004", "occurred_on": "2026-04-01", "category": "water", "value": "12"},
    ]
    assert rows[8] == {
        "reading_id": "r-009",
        "occurred_on": "2026-06-01",
        "category": "category-005",
        "value": "5",
    }
    assert len(rows) == 72
    assert len({row["category"] for row in rows}) == 71
