"""The registry-driven CLI core operations (`kb find`/`get`/`audit`/`note` …).

Drives `exomem.__main__.main` in-process with explicit argv against a temp vault,
asserting the human vs `--json` envelope output and the 0/1/2 exit-code contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem.__main__ import main

_INSIGHT = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"


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


def test_attention_runs(vault: Path, capsys) -> None:
    """`kb attention` is the registry-generated CLI surface of the review queue."""
    code, out, _ = _run(["attention", "--limit", "5", "--json"], capsys)
    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    data = payload["data"]
    assert {"items", "summary", "shown", "total", "truncated", "upstream_truncated"} <= set(data)
    assert data["shown"] == len(data["items"]) <= 5
    # category subset is accepted on the CLI too (list[str] via repeated/append flag)
    code2, out2, _ = _run(["attention", "--categories", "stale_review", "--json"], capsys)
    assert code2 == 0
    data2 = json.loads(out2.strip().splitlines()[-1])["data"]
    surfaced = {c for it in data2["items"] for c in it["categories"]}
    assert surfaced <= {"stale_review"}


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


def test_edit_value_plain_string(vault: Path, capsys) -> None:
    """`kb edit --field K --value <plain string>` works without quoting as JSON.

    `value` is a genuine union (coercion tag "json"); on the CLI a bare unquoted
    string must be taken as itself, not rejected as BAD_JSON.
    """
    code, out, err = _run(
        ["edit", _INSIGHT, "--why", "set domain", "--field", "domain",
         "--value", "retrieval", "--json"],
        capsys,
    )
    assert code == 0, err
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert payload["data"]["new_value"] == "retrieval"
    assert "domain: retrieval" in (vault / _INSIGHT).read_text(encoding="utf-8")


def test_malformed_field_exits_2(vault: Path, capsys) -> None:
    """A `--field` token with no `=` is a usage error → exit 2 (not exit 1)."""
    code, _out, err = _run(
        [
            "note",
            "--note-type", "insight",
            "--title", "x",
            "--content", "# x\n\n## Claim\n\ny\n",
            "--field", "bogus",  # no KEY=VALUE separator
        ],
        capsys,
    )
    assert code == 2
    assert "Error [USAGE]" in err
    assert "KEY=VALUE" in err


def test_tier2_op_disabled_emits_unavailable(
    vault: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`kb <tier2-op>` with EXOMEM_DISABLE_TIER2 set names the gap, exit 2."""
    monkeypatch.setenv("EXOMEM_DISABLE_TIER2", "1")
    code, _out, err = _run(["query_data", "some.csv"], capsys)
    assert code == 2
    assert "Error [UNAVAILABLE]" in err
    assert "tier-2 disabled" in err
    assert "query_data" in err


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

def test_simple_ask_alias_uses_compact_find_defaults(vault: Path, capsys) -> None:
    code, out, err = _run(["ask", "metabolism", "--json"], capsys)
    assert code == 0, err
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert isinstance(payload["data"], list)
    assert payload["data"], "ask should surface fixture notes"
    assert "excerpt" not in payload["data"][0]


def test_simple_ask_alias_can_request_deep_context(vault: Path, capsys) -> None:
    code, out, err = _run(["ask", "metabolism", "--deep", "--json"], capsys)
    assert code == 0, err
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert {"hits", "pack"} <= set(payload["data"])


def test_simple_remember_alias_routes_to_note(vault: Path, capsys) -> None:
    code, out, err = _run(
        [
            "remember",
            "# Simple action memory\n\n## Claim\n\nSimple aliases write through note.\n",
            "--title",
            "Simple action memory",
            "--json",
        ],
        capsys,
    )
    assert code == 0, err
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    written = vault / payload["data"]["path"]
    assert written.exists()
    assert "Simple aliases write through note" in written.read_text(encoding="utf-8")


def test_simple_capture_alias_routes_to_source_and_evidence(vault: Path, capsys) -> None:
    code, out, err = _run(
        [
            "capture",
            "raw source body",
            "--title",
            "Simple raw source",
            "--source-type",
            "other",
            "--json",
        ],
        capsys,
    )
    assert code == 0, err
    source_payload = json.loads(out.strip().splitlines()[-1])
    assert source_payload["success"] is True
    assert "/Sources/Other/" in source_payload["data"]["path"]

    code2, out2, err2 = _run(
        [
            "capture",
            "proof body",
            "--as",
            "evidence",
            "--scope",
            "simple-case",
            "--category",
            "receipts",
            "--filename",
            "proof.txt",
            "--json",
        ],
        capsys,
    )
    assert code2 == 0, err2
    evidence_payload = json.loads(out2.strip().splitlines()[-1])
    assert evidence_payload["success"] is True
    assert "Evidence/simple-case/receipts/proof.txt" in evidence_payload["data"]["path"]


def test_simple_review_connect_and_maintain_aliases(vault: Path, capsys) -> None:
    code, out, err = _run(["review", "--limit", "3", "--json"], capsys)
    assert code == 0, err
    review_payload = json.loads(out.strip().splitlines()[-1])
    assert review_payload["success"] is True
    assert "items" in review_payload["data"]

    code2, out2, err2 = _run(["maintain", "--json"], capsys)
    assert code2 == 0, err2
    maintain_payload = json.loads(out2.strip().splitlines()[-1])
    assert maintain_payload["success"] is True
    assert "findings" in maintain_payload["data"]

    code3, out3, err3 = _run(
        [
            "connect",
            "--path",
            "Notes/Insights/progressive-disclosure-without-mode-fragmentation",
            "--json",
        ],
        capsys,
    )
    assert code3 == 0, err3
    connect_payload = json.loads(out3.strip().splitlines()[-1])
    assert connect_payload["success"] is True
    assert isinstance(connect_payload["data"], list)
