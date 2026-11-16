"""Status and doctor report a vault's layout, and neither raises nor refuses on what they find."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from exomem import doctor as doctor_module
from exomem import layout, layout_report
from exomem.__main__ import main

KB = "Knowledge Base"
RECORD = f"_Schema/{layout.RECORD_NAME}"
WRAPPER_RECORD = f"{KB}/_Schema/{layout.RECORD_NAME}"


def _write(vault: Path, rel: str, text: str = "") -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _lay_out_schema_at_the_vault_root(vault: Path) -> None:
    """Move the schema folder out of the wrapper, as a vault laid out as `root` holds it.

    The shipped schema sentinel stays reachable at its dot-folder place, so `doctor` still finds the vault.
    """
    shutil.move(vault / KB / "_Schema", vault / "_Schema")
    _write(vault, ".exomem/schema/SKILL.md", "---\nname: exomem\n---\n")


def _status(vault: Path, capsys) -> dict:
    assert main(["status", "--vault", str(vault), "--json"]) == 0
    return json.loads(capsys.readouterr().out)["layout"]


def _doctor_layout_check(vault: Path) -> doctor_module.DoctorCheck:
    """The layout check from a full doctor run, so any other check that raised on this vault fails the test too."""
    report = doctor_module.doctor(vault=str(vault))
    return next(check for check in report.checks if check.id == "vault.layout")


def test_a_plain_nested_vault_reports_nested_and_needs_no_action(vault: Path, capsys) -> None:
    """Every vault that exists today has no record: reporting it as unsettled would warn on all of them."""
    status = _status(vault, capsys)
    check = _doctor_layout_check(vault)

    assert status["layout"] == "nested"
    assert status["record"] is None
    assert status["remedy"] is None
    assert check.status == "pass"
    assert check.message == status["outcome"]


def test_a_vault_root_schema_folder_without_a_record_is_reported_with_the_record_to_write(
    vault: Path, capsys
) -> None:
    """`layout.resolve` refuses this vault. Status and doctor must report it, and name the record, instead."""
    _lay_out_schema_at_the_vault_root(vault)
    with pytest.raises(layout.LayoutError):
        layout.resolve(vault)

    status = _status(vault, capsys)
    check = _doctor_layout_check(vault)

    assert status["layout"] == "unresolved"
    assert status["diagnostics"]["code"] == "LAYOUT_RECORD_MISSING"
    assert RECORD in status["remedy"]
    assert "layout: root" in status["remedy"]
    assert check.status == "warn"
    assert check.remediation == status["remedy"]
    assert check.details["diagnostics"]["code"] == "LAYOUT_RECORD_MISSING"


def test_a_record_with_a_leftover_second_schema_folder_reports_both_and_keeps_the_record_layout(
    vault: Path, capsys
) -> None:
    """A relayout in progress leaves both folders. The record decides, and the leftover is shown, not refused."""
    _lay_out_schema_at_the_vault_root(vault)
    _write(vault, RECORD, "layout: root\n")
    _write(vault, f"{KB}/_Schema/SKILL.md")

    status = _status(vault, capsys)
    check = _doctor_layout_check(vault)

    assert status["layout"] == "root"
    assert status["record"]["path"] == RECORD
    assert status["leftover_schema_folders"] == [f"{KB}/_Schema"]
    assert status["diagnostics"] is None
    assert check.status == "warn"
    assert f"{KB}/_Schema/" in check.message
    assert check.details["record"]["layout"] == "root"


def test_an_invalid_record_is_a_finding_with_its_code_and_the_file_to_fix(vault: Path, capsys) -> None:
    """A bad record must not read as `nested` (that would hide every page) and must not raise."""
    _write(vault, WRAPPER_RECORD, "layout: sideways\n")

    status = _status(vault, capsys)
    check = _doctor_layout_check(vault)

    assert status["layout"] == "unresolved"
    assert status["diagnostics"]["code"] == "LAYOUT_RECORD_INVALID"
    assert WRAPPER_RECORD in status["remedy"]
    assert check.status == "warn"
    assert check.remediation == status["remedy"]


def test_a_read_failure_is_an_unknown_layout_not_an_exception(vault: Path, monkeypatch) -> None:
    """A store that cannot be read raises something other than `LayoutError`; the report must still answer."""

    def broken(_root: Path):
        raise sqlite3.DatabaseError("store unreadable")

    monkeypatch.setattr(layout, "read_record", broken)

    found = layout_report.report(vault)

    assert found["layout"] == layout_report.UNKNOWN
    assert found["diagnostics"] == {"code": "DatabaseError"}
    assert found["remedy"]
