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


def test_a_store_that_cannot_be_read_is_an_unknown_layout_and_the_remedy_names_the_store(
    vault: Path, monkeypatch
) -> None:
    """A store that cannot be read raises something other than `LayoutError`; the report must still answer."""
    from exomem.vault_store import read

    def broken(_root: Path, _rel: str):
        raise sqlite3.DatabaseError("store unreadable")

    monkeypatch.setattr(read, "owned_bytes", broken)

    found = layout_report.report(vault)

    assert found["layout"] == layout_report.UNKNOWN
    assert found["diagnostics"] == {"code": "DatabaseError"}
    assert "store" in found["remedy"]


def test_a_record_in_the_other_layouts_folder_gets_a_remedy_that_keeps_the_folder_its_layout_needs(
    vault: Path, capsys
) -> None:
    """The record in the wrapper says `root`, and `root` keeps its schema folder at the vault root.

    Telling the person to remove the vault-root folder as the leftover would delete the folder
    that the layout needs and keep the folder that it does not.
    """
    _lay_out_schema_at_the_vault_root(vault)
    _write(vault, WRAPPER_RECORD, "layout: root\n")

    status = _status(vault, capsys)

    assert status["layout"] == "root"
    assert status["leftover_schema_folders"] == ["_Schema"]
    assert f"Move {WRAPPER_RECORD} to {RECORD}" in status["remedy"]
    assert f"remove {KB}/_Schema/" in status["remedy"]


def test_doctor_reports_no_layout_when_the_vault_path_check_failed(tmp_path: Path) -> None:
    """A folder with no schema contract is no vault. Reading its layout as `nested` would pass a check that read nothing."""
    not_a_vault = tmp_path / "empty"
    not_a_vault.mkdir()

    check = _doctor_layout_check(not_a_vault)

    assert check.status == "warn"
    assert check.details == {"layout": None, "depends_on": "vault.path"}
    assert "vault.path" in check.message


def test_a_failure_outside_the_store_does_not_send_the_person_to_the_store(
    vault: Path, monkeypatch
) -> None:
    """A wrapper name with a separator in it fails before any store is read. Blaming the store would send the person to the wrong place."""
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "inner/outer")

    found = layout_report.report(vault)

    assert found["layout"] == layout_report.UNKNOWN
    assert found["diagnostics"] == {"code": "ValueError"}
    assert "ValueError" in found["remedy"]
    assert "store" not in found["remedy"]


def test_a_failure_with_an_empty_code_is_named_by_its_type_not_as_none(vault: Path, monkeypatch) -> None:
    class Coded(Exception):
        code = None

    def broken(_root: Path):
        raise Coded()

    monkeypatch.setattr(layout, "read_record", broken)

    found = layout_report.report(vault)

    assert found["diagnostics"] == {"code": "Coded"}
    assert "None" not in found["outcome"] + found["remedy"]


def test_doctor_warns_on_an_unresolved_layout_even_when_the_error_names_no_remedy(
    vault: Path, monkeypatch
) -> None:
    """The test is the layout's state. A remedy is only text that may be missing, as for a code with no entry."""

    def refuse(_root: Path):
        raise layout.LayoutError("LAYOUT_WITHOUT_ENTRY", "The layout cannot be decided.")

    monkeypatch.setattr(layout, "inspect", refuse)

    check = _doctor_layout_check(vault)

    assert check.remediation is None
    assert check.status == "warn"

