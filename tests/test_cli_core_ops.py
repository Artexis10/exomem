"""The registry-driven product CLI operations (`ask_memory`/`read_memory`/`remember` …).

Drives `exomem.__main__.main` in-process with explicit argv against a temp vault,
asserting the human vs `--json` envelope output and the 0/1/2 exit-code contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exomem.__main__ import main

_INSIGHT = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    try:
        code = main(argv)
    except SystemExit as e:  # argparse usage errors
        code = e.code if isinstance(e.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_ask_memory_json_envelope(vault: Path, capsys) -> None:
    code, out, _ = _run(["ask_memory", "metabolism", "--mode", "keyword", "--json"], capsys)
    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert isinstance(payload["data"], list)
    assert payload["data"], "keyword ask_memory for 'metabolism' should surface fixture notes"


def test_ask_memory_human_output(vault: Path, capsys) -> None:
    code, out, _ = _run(["ask_memory", "metabolism", "--mode", "keyword"], capsys)
    assert code == 0
    assert ".md" in out
    assert '"success"' not in out


def test_read_memory_reads_a_page(vault: Path, capsys) -> None:
    code, out, _ = _run(
        ["read_memory", "Notes/Insights/progressive-disclosure-without-mode-fragmentation", "--json"],
        capsys,
    )
    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert payload["data"]["frontmatter"]["type"] == "insight"


def test_review_memory_attention_runs(vault: Path, capsys) -> None:
    code, out, _ = _run(["review_memory", "--mode", "attention", "--limit", "5", "--json"], capsys)
    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    data = payload["data"]
    assert {"items", "summary", "shown", "total", "truncated", "upstream_truncated"} <= set(data)
    assert data["shown"] == len(data["items"]) <= 5
    code2, out2, _ = _run(
        ["review_memory", "--mode", "attention", "--categories", "stale_review", "--json"],
        capsys,
    )
    assert code2 == 0
    data2 = json.loads(out2.strip().splitlines()[-1])["data"]
    surfaced = {c for it in data2["items"] for c in it["categories"]}
    assert surfaced <= {"stale_review"}


def test_review_memory_activation_runs(vault: Path, capsys) -> None:
    code, out, _ = _run(
        ["review_memory", "--mode", "activation", "--limit", "3", "--json"],
        capsys,
    )

    assert code == 0
    data = json.loads(out.strip().splitlines()[-1])["data"]
    assert data["coverage"]["eligible_pages"] > 0
    assert data["shown"] == len(data["items"]) <= 3
    assert all(item["ref"].startswith("exomem://review/") for item in data["items"])


def test_review_item_context_runs_from_cli(vault: Path, capsys) -> None:
    code, out, _ = _run(
        ["review_memory", "--mode", "activation", "--limit", "1", "--json"],
        capsys,
    )
    assert code == 0
    item = json.loads(out.strip().splitlines()[-1])["data"]["items"][0]

    code, out, _ = _run(
        [
            "review_item_context",
            item["ref"],
            "--expected-fingerprint",
            item["fingerprint"],
            "--max-body-chars",
            "200",
            "--json",
        ],
        capsys,
    )

    assert code == 0
    data = json.loads(out.strip().splitlines()[-1])["data"]
    assert data["item"]["ref"] == item["ref"]
    assert data["target"]["path"] == item["path"]


def test_review_memory_audit_runs(vault: Path, capsys) -> None:
    code, out, _ = _run(["review_memory", "--mode", "audit", "--json"], capsys)
    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert "findings" in payload["data"]


def test_remember_write(vault: Path, capsys) -> None:
    code, out, _ = _run(
        [
            "remember",
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


def test_remember_unicode_title_with_explicit_slug(vault: Path, capsys) -> None:
    code, out, err = _run(
        [
            "remember",
            "--title", "睡眠",
            "--slug", "sleep",
            "--content", "## 要約\n\n本文。\n",
            "--json",
        ],
        capsys,
    )
    assert code == 0, err
    data = json.loads(out.strip().splitlines()[-1])["data"]
    assert data["path"].endswith("/sleep.md")
    text = (vault / data["path"]).read_text(encoding="utf-8")
    frontmatter = text.removeprefix("---\n").split("\n---\n", 1)[0]
    assert yaml.safe_load(frontmatter)["title"] == "睡眠"


def test_remember_field_escape(vault: Path, capsys) -> None:
    code, out, _ = _run(
        [
            "remember",
            "--title", "Field escape works",
            "--content", "# Field escape works\n\n## Question\n\nq\n",
            "--field", "note_type=research-note",
            "--field", "project=project-alpha",
            "--json",
        ],
        capsys,
    )
    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert "Project Alpha" in payload["data"]["path"]


def test_edit_memory_value_plain_string(vault: Path, capsys) -> None:
    code, out, err = _run(
        ["edit_memory", _INSIGHT, "--why", "set domain", "--field", "domain",
         "--value", "retrieval", "--json"],
        capsys,
    )
    assert code == 0, err
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert payload["data"]["new_value"] == "retrieval"
    assert "domain: retrieval" in (vault / _INSIGHT).read_text(encoding="utf-8")


def test_malformed_field_exits_2(vault: Path, capsys) -> None:
    code, _out, err = _run(
        [
            "remember",
            "--title", "x",
            "--content", "# x\n\n## Claim\n\ny\n",
            "--field", "bogus",
        ],
        capsys,
    )
    assert code == 2
    assert "Error [USAGE]" in err
    assert "KEY=VALUE" in err


def test_tier2_op_disabled_emits_unavailable(
    vault: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_TIER2", "1")
    code, _out, err = _run(["query_dataset", "some.csv"], capsys)
    assert code == 2
    assert "Error [UNAVAILABLE]" in err
    assert "tier-2 disabled" in err
    assert "query_dataset" in err


def test_missing_required_arg_exits_2(vault: Path, capsys) -> None:
    code, _out, err = _run(["read_memory"], capsys)
    assert code == 2
    assert "Error [USAGE]" in err


def test_op_error_exits_1_with_code(vault: Path, capsys) -> None:
    code, _out, err = _run(["read_memory", "Notes/Insights/does-not-exist"], capsys)
    assert code == 1
    assert "Error [NOT_FOUND]" in err


def test_op_error_json_envelope(vault: Path, capsys) -> None:
    code, out, _ = _run(["read_memory", "Notes/Insights/does-not-exist", "--json"], capsys)
    assert code == 1
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is False
    assert payload["error"]["code"] == "NOT_FOUND"


def test_unknown_field_key_rejected(vault: Path, capsys) -> None:
    code, _out, err = _run(
        [
            "remember",
            "--title", "x",
            "--content", "# x\n\n## Claim\n\ny\n",
            "--field", "bogus=1",
        ],
        capsys,
    )
    assert code == 1
    assert "UNKNOWN_PARAM" in err


def test_simple_ask_alias_uses_compact_product_defaults(vault: Path, capsys) -> None:
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


def test_generated_remember_command_writes(vault: Path, capsys) -> None:
    code, out, err = _run(
        [
            "remember",
            "--content",
            "# Product command memory\n\n## Claim\n\nProduct commands write through canonical note.\n",
            "--title",
            "Product command memory",
            "--json",
        ],
        capsys,
    )
    assert code == 0, err
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    written = vault / payload["data"]["path"]
    assert written.exists()
    assert "Product commands write through canonical note" in written.read_text(encoding="utf-8")


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
    assert "/Sources/Other/" in source_payload["data"]["source"]["path"]

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


def test_simple_review_human_output_and_triage(vault: Path, capsys) -> None:
    code, out, err = _run(["review", "--limit", "1"], capsys)
    assert code == 0, err
    assert "Epistemic Inbox" in out
    assert "exomem://review/" in out
    assert '"items"' not in out

    code2, out2, err2 = _run(["review", "--limit", "1", "--json"], capsys)
    assert code2 == 0, err2
    item = json.loads(out2.strip().splitlines()[-1])["data"]["items"][0]

    code3, out3, err3 = _run(
        ["review", "dismiss", item["ref"], "--why", "reviewed", "--json"],
        capsys,
    )
    assert code3 == 0, err3
    triage = json.loads(out3.strip().splitlines()[-1])["data"]
    assert triage["state"] == "dismissed"
    assert (vault / "Knowledge Base/.review-state.json").exists()

    code4, out4, err4 = _run(
        ["review", "reopen", item["ref"], "--json"], capsys
    )
    assert code4 == 0, err4
    assert json.loads(out4.strip().splitlines()[-1])["data"]["state"] == "open"
