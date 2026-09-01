from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from exomem import commands, hosted_plugins
from exomem.init import init_vault

REPO_ROOT = Path(__file__).resolve().parents[1]


def _local_product_caller(vault: Path, calls: list[tuple[str, dict[str, Any]]]):
    def call_tool(name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        copied = dict(arguments)
        calls.append((name, copied))
        if name == "remember":
            return commands.op_remember(vault, **copied)
        if name == "read_memory":
            try:
                return commands.op_read_memory(vault, **copied)
            except ValueError as error:
                code = str(error).partition(":")[0]
                return {"success": False, "error": {"code": code}}
        raise AssertionError(f"unexpected fixture tool: {name}")

    return call_tool


def _fresh_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    vault = tmp_path / "vault"
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    init_vault(vault)
    return vault


def test_checked_marketplace_fixture_executes_through_real_product_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _fresh_vault(tmp_path, monkeypatch)
    fixture = hosted_plugins._load_marketplace_review_fixture(REPO_ROOT)
    calls: list[tuple[str, dict[str, Any]]] = []

    result = hosted_plugins.seed_marketplace_review_fixture(
        fixture, _local_product_caller(vault, calls)
    )

    notes = fixture["payload"]["notes"]
    assert result == {
        "fixture_version": fixture["fixture_version"],
        "payload_sha256": fixture["payload_sha256"],
        "note_count": len(notes),
        "verified": True,
    }
    for note in notes:
        remember_calls = [
            arguments
            for name, arguments in calls
            if name == "remember" and arguments["slug"] == note["key"]
        ]
        assert len(remember_calls) == 2
        assert remember_calls[0] == {
            "title": note["title"],
            "slug": note["key"],
            "content": note["content"],
            "note_type": "insight",
            "suggestions": False,
            "validate_only": True,
        }
        assert remember_calls[1]["content"] == note["content"]
        assert remember_calls[1]["title"] == note["title"]
        assert remember_calls[1]["slug"] == note["key"]
        assert remember_calls[1]["note_type"] == "insight"
        assert remember_calls[1]["suggestions"] is False
        assert remember_calls[1]["draft_id"]
        assert remember_calls[1]["draft_hash"]
        assert remember_calls[1]["draft_token"]

        page = commands.op_read_memory(
            vault, path=f"Knowledge Base/Notes/Insights/{note['key']}.md"
        )
        assert page["frontmatter"]["title"] == note["title"]
        assert page["body"] == f"# {note['title']}\n\n{note['content'].rstrip()}\n"


def test_executable_fixture_validation_rejects_semantically_invalid_hashed_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _fresh_vault(tmp_path, monkeypatch)
    fixture = copy.deepcopy(hosted_plugins._load_marketplace_review_fixture(REPO_ROOT))
    fixture["payload"]["notes"][0]["content"] = (
        "## Prior Work\n\nThis prose has no recognized semantic unit."
    )
    fixture["payload_sha256"] = hosted_plugins._sha256(
        hosted_plugins._canonical_json(fixture["payload"])
    )

    with pytest.raises(
        hosted_plugins.MarketplaceFixtureSeedError, match="missing_semantic_unit"
    ):
        hosted_plugins.seed_marketplace_review_fixture(
            fixture, _local_product_caller(vault, [])
        )


def test_fixture_seed_rerun_accepts_only_exact_verified_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _fresh_vault(tmp_path, monkeypatch)
    fixture = hosted_plugins._load_marketplace_review_fixture(REPO_ROOT)
    hosted_plugins.seed_marketplace_review_fixture(
        fixture, _local_product_caller(vault, [])
    )
    rerun_calls: list[tuple[str, dict[str, Any]]] = []

    result = hosted_plugins.seed_marketplace_review_fixture(
        fixture, _local_product_caller(vault, rerun_calls)
    )

    assert result["verified"] is True
    assert len(rerun_calls) == len(fixture["payload"]["notes"])
    assert {name for name, _ in rerun_calls} == {"read_memory"}


def test_fixture_seed_refuses_existing_path_mismatch_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _fresh_vault(tmp_path, monkeypatch)
    fixture = hosted_plugins._load_marketplace_review_fixture(REPO_ROOT)
    hosted_plugins.seed_marketplace_review_fixture(
        fixture, _local_product_caller(vault, [])
    )
    target = vault / "Knowledge Base/Notes/Insights/review-project-brief.md"
    mismatched = target.read_text(encoding="utf-8").replace(
        "generic project", "different project", 1
    )
    target.write_text(mismatched, encoding="utf-8")
    rerun_calls: list[tuple[str, dict[str, Any]]] = []

    with pytest.raises(hosted_plugins.MarketplaceFixtureSeedError, match="readback mismatch"):
        hosted_plugins.seed_marketplace_review_fixture(
            fixture, _local_product_caller(vault, rerun_calls)
        )

    assert target.read_text(encoding="utf-8") == mismatched
    assert [name for name, _ in rerun_calls] == ["read_memory"]
