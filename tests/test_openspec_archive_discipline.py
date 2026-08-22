"""Contract tests for the active OpenSpec archive-debt gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_openspec_archive_discipline.py"


def _write_tasks(root: Path, change: str, body: str) -> None:
    target = root / "openspec" / "changes" / change
    target.mkdir(parents=True)
    (target / "tasks.md").write_text(body, encoding="utf-8")


def _run(root: Path, *, json_output: bool = False) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(SCRIPT), "--root", str(root)]
    if json_output:
        argv.append("--json")
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def test_fully_checked_active_change_fails_and_names_the_archive_action(tmp_path: Path) -> None:
    _write_tasks(tmp_path, "shipped-change", "- [x] implementation\n- [X] verification\n")

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "shipped-change" in result.stdout
    assert "openspec archive shipped-change" in result.stdout


def test_json_output_is_machine_readable_and_deterministically_sorted(tmp_path: Path) -> None:
    _write_tasks(tmp_path, "zeta", "- [x] done\n")
    _write_tasks(tmp_path, "alpha", "- [x] done\n")

    result = _run(tmp_path, json_output=True)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload == {
        "active_change_count": 2,
        "complete_active_changes": ["alpha", "zeta"],
        "status": "archive_debt",
        "unchecked_active_changes": [],
        "unclassified_active_changes": [],
    }


def test_any_unchecked_task_keeps_an_active_change_in_progress(tmp_path: Path) -> None:
    _write_tasks(tmp_path, "in-progress", "- [x] first\n  - [ ] second\n")

    result = _run(tmp_path, json_output=True)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["complete_active_changes"] == []
    assert payload["unchecked_active_changes"] == ["in-progress"]


def test_missing_empty_malformed_and_archived_tasks_do_not_claim_completion(
    tmp_path: Path,
) -> None:
    changes = tmp_path / "openspec" / "changes"
    (changes / "missing-tasks").mkdir(parents=True)
    _write_tasks(tmp_path, "no-checkboxes", "# Tasks\n\nNothing enumerated yet.\n")
    _write_tasks(tmp_path, "malformed", "- [x] real task\n- [maybe] ambiguous task\n")
    archived = changes / "archive" / "2026-08-20-finished"
    archived.mkdir(parents=True)
    (archived / "tasks.md").write_text("- [x] done\n", encoding="utf-8")

    result = _run(tmp_path, json_output=True)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["complete_active_changes"] == []
    assert payload["unchecked_active_changes"] == []
    assert payload["unclassified_active_changes"] == [
        {"change": "malformed", "reason": "malformed_checkbox"},
        {"change": "missing-tasks", "reason": "missing_tasks"},
        {"change": "no-checkboxes", "reason": "no_task_checkboxes"},
    ]


def test_ci_validates_all_openspec_records_before_the_archive_debt_gate() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    validation = "npm exec --yes @fission-ai/openspec -- validate --all --strict"
    audit = "python scripts/check_openspec_archive_discipline.py"

    assert validation in workflow
    assert audit in workflow
    assert workflow.index(validation) < workflow.index(audit)
    assert "validate --specs --strict" not in workflow
