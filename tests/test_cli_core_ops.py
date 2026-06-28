"""The registry-driven CLI core operations (`kb find`/`get`/`audit`/`note` …).

Drives `kb_mcp.__main__.main` in-process with explicit argv against a temp vault,
asserting the human vs `--json` envelope output and the 0/1/2 exit-code contract.
"""

from __future__ import annotations

import json
from pathlib import Path

from kb_mcp.__main__ import main


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    try:
        code = main(argv)
    except SystemExit as e:  # argparse usage errors
        code = e.code if isinstance(e.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_find_json_envelope(vault: Path, capsys) -> None:
    code, out, _ = _run(["find", "metabolism", "--mode", "keyword", "--json"], capsys)
    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert isinstance(payload["data"], list)
    assert payload["data"], "keyword find for 'metabolism' should surface fixture notes"


def test_find_human_output(vault: Path, capsys) -> None:
    code, out, _ = _run(["find", "metabolism", "--mode", "keyword"], capsys)
    assert code == 0
    assert ".md" in out  # one path-per-line human listing, not an envelope
    assert '"success"' not in out


def test_get_reads_a_page(vault: Path, capsys) -> None:
    code, out, _ = _run(
        ["get", "Notes/Insights/progressive-disclosure-without-mode-fragmentation", "--json"],
        capsys,
    )
    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert payload["data"]["frontmatter"]["type"] == "insight"


def test_audit_runs(vault: Path, capsys) -> None:
    code, out, _ = _run(["audit", "--json"], capsys)
    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert "findings" in payload["data"]


def test_note_write(vault: Path, capsys) -> None:
    code, out, _ = _run(
        [
            "note",
            "--note-type", "insight",
            "--title", "CLI can write",
            "--content", "# CLI can write\n\n## Claim\n\nThe kb CLI writes notes.\n",
            "--json",
        ],
        capsys,
    )
    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    written = vault / payload["data"]["path"]
    assert written.exists()
    assert "CLI can write" in written.read_text(encoding="utf-8")


def test_note_field_escape(vault: Path, capsys) -> None:
    """`note`'s type-specific args go through the repeatable --field key=value escape."""
    code, out, _ = _run(
        [
            "note",
            "--note-type", "research-note",
            "--title", "Field escape works",
            "--content", "# Field escape works\n\n## Question\n\nq\n",
            "--field", "project=project-alpha",
            "--json",
        ],
        capsys,
    )
    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert "Project Alpha" in payload["data"]["path"]


def test_missing_required_arg_exits_2(vault: Path, capsys) -> None:
    code, _out, err = _run(["get"], capsys)  # `path` is required
    assert code == 2
    assert "Error [USAGE]" in err


def test_op_error_exits_1_with_code(vault: Path, capsys) -> None:
    code, _out, err = _run(["get", "Notes/Insights/does-not-exist"], capsys)
    assert code == 1
    assert "Error [NOT_FOUND]" in err


def test_op_error_json_envelope(vault: Path, capsys) -> None:
    code, out, _ = _run(["get", "Notes/Insights/does-not-exist", "--json"], capsys)
    assert code == 1
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is False
    assert payload["error"]["code"] == "NOT_FOUND"


def test_unknown_field_key_rejected(vault: Path, capsys) -> None:
    code, _out, err = _run(
        [
            "note",
            "--note-type", "insight",
            "--title", "x",
            "--content", "# x\n\n## Claim\n\ny\n",
            "--field", "bogus=1",
        ],
        capsys,
    )
    assert code == 1
    assert "UNKNOWN_PARAM" in err
