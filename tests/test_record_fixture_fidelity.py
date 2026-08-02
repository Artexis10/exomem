from __future__ import annotations

import csv
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
    assert "10+" in log and "10!" in log and "| ?" in log and "Overhead press | 8-12 |" in log
    assert "```markdown" in log and "## 2099-01-01 Decoy" in log
    assert "# Push template" not in log


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
