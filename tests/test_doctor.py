"""`kb-mcp doctor` install-readiness preflight.

The checks stay torch-free in the suite: profile-specific dependency availability
is exercised by stubbing the import-spec seam rather than importing heavy extras.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kb_mcp import doctor as doctor_module
from kb_mcp.__main__ import main


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    try:
        code = main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_doctor_lean_passes_with_fixture_vault(vault: Path) -> None:
    report = doctor_module.doctor(vault=str(vault))

    assert report.profile == "lean"
    assert report.success is True
    checks = {c.id: c for c in report.checks}
    assert checks["python.version"].status == "pass"
    assert checks["vault.path"].status == "pass"
    assert checks["command.registry"].status == "pass"


def test_doctor_json_cli(vault: Path, capsys) -> None:
    code, out, err = _run(["doctor", "--vault", str(vault), "--json"], capsys)

    assert code == 0, err
    payload = json.loads(out)
    assert payload["success"] is True
    assert payload["profile"] == "lean"
    assert {"id", "status", "message", "remediation"} <= set(payload["checks"][0])


def test_doctor_missing_vault_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_VAULT_PATH", raising=False)

    report = doctor_module.doctor()

    assert report.success is False
    vault_check = next(c for c in report.checks if c.id == "vault.path")
    assert vault_check.status == "fail"
    assert "--vault" in (vault_check.remediation or "")


def test_doctor_profile_missing_dependency_fails(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_find_spec = doctor_module.importlib.util.find_spec

    def fake_find_spec(name: str):
        if name == "sentence_transformers":
            return None
        return real_find_spec(name)

    monkeypatch.delenv("KB_MCP_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(doctor_module.importlib.util, "find_spec", fake_find_spec)

    report = doctor_module.doctor(vault=str(vault), profile="hybrid")

    assert report.success is False
    dep = next(c for c in report.checks if c.id == "dep.sentence-transformers")
    assert dep.status == "fail"
    assert "uv sync --extra embeddings" in (dep.remediation or "")


def test_doctor_human_output_includes_remediation(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.delenv("KB_MCP_VAULT_PATH", raising=False)

    code, out, err = _run(["doctor"], capsys)

    assert code == 1
    assert err == ""
    assert "FAIL" in out
    assert "vault.path" in out
    assert "fix:" in out


def test_doctor_unknown_profile_exits_2(capsys) -> None:
    code, _out, err = _run(["doctor", "--profile", "bogus"], capsys)

    assert code == 2
    assert "invalid choice" in err
