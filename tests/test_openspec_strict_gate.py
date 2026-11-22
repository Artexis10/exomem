"""Contract tests for the strict OpenSpec validation baseline gate.

The gate shells out to an OpenSpec CLI; these tests substitute a stub that
prints a canned ``validate --all --strict --json`` report, so they exercise
key derivation, baselining and the skill-symlink check without the real CLI.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "openspec_strict_gate.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")

LONG_WARNING = {
    "level": "WARNING",
    "path": "requirements[0]",
    "message": "Requirement text is very long (>500 characters). Move examples and edge cases into scenarios.",
}
CHANGE_WARNING = {
    "level": "WARNING",
    "path": "cap-x/spec.md",
    "message": 'ADDED "Delta Long": Requirement text is very long (>500 characters). Move examples.',
}
OTHER_WARNING = {
    "level": "WARNING",
    "path": "overview",
    "message": "Purpose section is too brief (less than 50 characters)",
}
ERROR_ISSUE = {
    "level": "ERROR",
    "path": "requirements[0]",
    "message": "Requirement must have at least one scenario",
}


def _report(extra_spec_issues=None, extra_change_issues=None, valid=False):
    return {
        "items": [
            {
                "id": "demo",
                "type": "spec",
                "valid": valid,
                "issues": [LONG_WARNING, *(extra_spec_issues or [])],
            },
            {
                "id": "add-things",
                "type": "change",
                "valid": valid,
                "issues": [CHANGE_WARNING, *(extra_change_issues or [])],
            },
        ]
    }


def _write_stub(root: Path, report: dict) -> Path:
    stub = root / "fake-openspec.sh"
    payload = json.dumps(report).replace("'", "'\\''")
    stub.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo '2.1.1'; exit 0; fi\n"
        f"echo '{payload}'\n"
        "exit 1\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _init_repo(root: Path) -> None:
    (root / "openspec" / "specs" / "demo").mkdir(parents=True)
    (root / "openspec" / "specs" / "demo" / "spec.md").write_text(
        "# demo Specification\n\n## Requirements\n\n### Requirement: Long One\nbody\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def _run_gate(root: Path, stub: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(GATE), *args, "--", str(stub)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_baseline_roundtrip_and_key_derivation(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    stub = _write_stub(tmp_path, _report())

    write = _run_gate(tmp_path, stub, "--write-baseline")
    assert write.returncode == 0

    keys = json.loads((tmp_path / "openspec" / "strict-baseline.json").read_text())["keys"]
    assert sorted(keys) == [
        "too-long cap-x :: Delta Long",
        "too-long demo :: Long One",
    ]

    check = _run_gate(tmp_path, stub)
    assert check.returncode == 0
    assert "all baselined" in check.stdout


def test_new_warning_and_error_fail_check(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    stub = _write_stub(tmp_path, _report())
    assert _run_gate(tmp_path, stub, "--write-baseline").returncode == 0

    grown = _write_stub(
        tmp_path, _report(extra_spec_issues=[OTHER_WARNING], extra_change_issues=[ERROR_ISSUE])
    )
    result = _run_gate(tmp_path, grown)
    assert result.returncode == 1
    assert "warning spec/demo :: overview" in result.stderr
    assert "ERROR-level issues" in result.stderr


def test_write_baseline_refuses_when_errors_exist(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    stub = _write_stub(tmp_path, _report(extra_spec_issues=[ERROR_ISSUE]))
    result = _run_gate(tmp_path, stub, "--write-baseline")
    assert result.returncode == 1
    assert not (tmp_path / "openspec" / "strict-baseline.json").exists()


def test_tracked_skill_symlink_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    stub = _write_stub(tmp_path, _report())
    assert _run_gate(tmp_path, stub, "--write-baseline").returncode == 0

    target = tmp_path / ".agents" / "skills" / "openspec-x"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("x", encoding="utf-8")
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "openspec-x").symlink_to(
        "../../.agents/skills/openspec-x"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    result = _run_gate(tmp_path, stub)
    assert result.returncode == 1
    assert ".claude/skills/openspec-x" in result.stderr


def test_stale_baseline_keys_report_but_pass(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    stub = _write_stub(tmp_path, _report())
    assert _run_gate(tmp_path, stub, "--write-baseline").returncode == 0

    healed = _write_stub(tmp_path, _report(valid=True) | {"items": _report()["items"][1:]})
    result = _run_gate(tmp_path, healed)
    assert result.returncode == 0
    assert "no longer occur" in result.stdout


def test_requirement_names_follow_the_validators_indexing(tmp_path: Path) -> None:
    """`requirements[i]` names come from every `###` child of `## Requirements`.

    A fenced `### Requirement:` example, a `### Requirement:` outside the
    section, and a non-Requirement `###` header inside it must not shift the
    names: the gate mirrors the parser, so a new overlong requirement cannot
    inherit a baselined name (and vice versa).
    """

    _init_repo(tmp_path)
    (tmp_path / "openspec" / "specs" / "demo" / "spec.md").write_text(
        "# demo Specification\n\n"
        "## Purpose\n\n"
        "### Requirement: Outside Section\n"
        "not inside Requirements\n\n"
        "## Requirements\n\n"
        "### Rationale\n"
        "context only\n\n"
        "```markdown\n"
        "### Requirement: Fenced Example\n"
        "```\n\n"
        "### Requirement: Real Long\n"
        "body\n",
        encoding="utf-8",
    )
    warning_at = lambda i: {
        "level": "WARNING",
        "path": f"requirements[{i}]",
        "message": LONG_WARNING["message"],
    }
    report = {
        "items": [
            {
                "id": "demo",
                "type": "spec",
                "valid": False,
                "issues": [warning_at(0), warning_at(1)],
            }
        ]
    }
    stub = _write_stub(tmp_path, report)
    assert _run_gate(tmp_path, stub, "--write-baseline").returncode == 0

    keys = json.loads((tmp_path / "openspec" / "strict-baseline.json").read_text())["keys"]
    # requirements[0] is the non-Requirement `### Rationale` header, matching
    # the parser; the fenced and outside-section headers are not counted.
    assert keys == ["too-long demo :: Rationale", "too-long demo :: Real Long"]
    assert _run_gate(tmp_path, stub).returncode == 0

    grown = _write_stub(
        tmp_path,
        {"items": [{**report["items"][0], "issues": [warning_at(0), warning_at(1), warning_at(2)]}]},
    )
    result = _run_gate(tmp_path, grown)
    assert result.returncode == 1
    assert "too-long demo :: #2" in result.stderr


def test_missing_git_is_an_internal_error(tmp_path: Path) -> None:
    """Outside a repo the symlink check skips; a missing git binary must not."""

    _init_repo(tmp_path)
    stub = _write_stub(tmp_path, _report())
    assert _run_gate(tmp_path, stub, "--write-baseline").returncode == 0

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "node").symlink_to(shutil.which("node"))
    result = subprocess.run(
        ["node", str(GATE), "--", str(stub)],
        cwd=tmp_path,
        env={"PATH": str(bindir)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "git" in result.stderr


def test_node_below_the_engines_floor_exits_2(tmp_path: Path) -> None:
    """An older Node (e.g. a .nvmrc 20) gets the floor message, not a CLI failure."""

    _init_repo(tmp_path)
    stub = _write_stub(tmp_path, _report())
    wrapper = (
        'Object.defineProperty(process.versions, "node", { value: "20.19.0" });'
        f'import({json.dumps(str(GATE))})'
    )
    result = subprocess.run(
        ["node", "-e", wrapper, "--", str(stub)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "needs Node >= 22.20.0; this is v20.19.0" in result.stderr
