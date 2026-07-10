from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, memory_schema
from exomem.__main__ import main


def _seed_pages(vault: Path, count: int = 5) -> list[Path]:
    schema_dir = vault / "Knowledge Base" / "_Schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "SKILL.md").write_text("# Test schema\n", encoding="utf-8")
    paths: list[Path] = []
    for index in range(count):
        path = vault / "Knowledge Base" / "Notes" / f"page-{index}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "type: insight\n"
            "project: atlas\n"
            "status: active\n"
            f"category: {'alpha' if index % 2 else 'beta'}\n"
            "---\n\n"
            f"# Page {index}\n\n"
            "## Claim\n\n"
            f"Corpus-backed claim {index}.\n\n"
            "- supports [[Knowledge Base/Notes/future]]\n",
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def test_inference_is_conservative_below_five_pages(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_pages(vault, count=4)

    result = memory_schema.infer_contract(
        vault, name="atlas-insights", project="atlas", page_type="insight"
    )

    assert result["sample_size"] == 4
    assert result["required_threshold"]["eligible"] is False
    assert result["frequencies"]["fields"]["status"]["frequency"] == 1.0
    assert not any(rule["required"] for rule in result["proposal"]["fields"].values())
    assert not any(rule["required"] for rule in result["proposal"]["blocks"].values())


def test_inference_profiles_fields_blocks_relations_and_enums(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_pages(vault)

    result = commands.op_schema_memory(
        vault,
        operation="infer",
        name="atlas-insights",
        project="atlas",
        page_type="insight",
    )
    proposal = result["proposal"]

    assert result["sample_size"] == 5
    assert proposal["fields"]["status"]["required"] is True
    assert proposal["fields"]["category"]["enum"] == ["alpha", "beta"]
    assert proposal["blocks"]["claim"]["required"] is True
    assert proposal["relations"]["supports"]["required"] is True
    assert proposal["unknown_fields"] == "allow"


def test_contract_save_requires_hash_for_overwrite(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_pages(vault)
    first = commands.op_schema_memory(
        vault,
        operation="infer",
        name="atlas-insights",
        project="atlas",
        page_type="insight",
        save=True,
    )
    contract_path = vault / first["saved"]["path"]
    original = contract_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="CONTRACT_EXISTS"):
        commands.op_schema_memory(
            vault, operation="infer", name="atlas-insights", project="atlas", save=True
        )
    with pytest.raises(ValueError, match="STALE_CONTRACT"):
        commands.op_schema_memory(
            vault,
            operation="infer",
            name="atlas-insights",
            project="atlas",
            save=True,
            expected_hash="stale",
        )
    assert contract_path.read_text(encoding="utf-8") == original

    overwritten = commands.op_schema_memory(
        vault,
        operation="infer",
        name="atlas-insights",
        project="atlas",
        page_type="insight",
        save=True,
        expected_hash=first["saved"]["content_hash"],
    )
    assert overwritten["saved"]["created"] is False


def test_validate_and_diff_report_corpus_drift_without_mutating_pages(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    pages = _seed_pages(vault)
    saved = commands.op_schema_memory(
        vault,
        operation="infer",
        name="atlas-insights",
        project="atlas",
        page_type="insight",
        save=True,
    )
    changed = pages[0].read_text(encoding="utf-8")
    changed = changed.replace("status: active\n", "")
    changed = changed.replace("## Claim\n\n", "## Detail\n\n")
    changed += "\n- contradicts [[Knowledge Base/Notes/future]]\n"
    pages[0].write_text(changed, encoding="utf-8")
    before = pages[0].read_text(encoding="utf-8")

    validation = commands.op_schema_memory(
        vault, operation="validate", name="atlas-insights", strict=True
    )
    diff = commands.op_schema_memory(vault, operation="diff", name="atlas-insights")

    spans = {finding["span"] for finding in validation["findings"]}
    assert validation["valid"] is False
    assert validation["strict_failed"] is True
    assert "frontmatter.status" in spans
    assert "body.block:claim" in spans
    assert diff["changed"] is True
    assert "status" in diff["changes"]["fields"]["required_removed"]
    assert "claim" in diff["changes"]["blocks"]["required_removed"]
    assert "contradicts" in diff["changes"]["relations"]["added"]
    assert diff["content_hash"] == saved["saved"]["content_hash"]
    assert pages[0].read_text(encoding="utf-8") == before


def test_schema_memory_registry_parity_and_strict_cli_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = tmp_path / "vault"
    pages = _seed_pages(vault)
    commands.op_schema_memory(
        vault,
        operation="infer",
        name="atlas-insights",
        project="atlas",
        page_type="insight",
        save=True,
    )
    pages[0].write_text(
        pages[0].read_text(encoding="utf-8").replace("status: active\n", ""),
        encoding="utf-8",
    )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))

    product = next(
        command for command in commands.PRODUCT_COMMANDS
        if command.name == "schema_memory"
    )
    assert product.surfaces == frozenset({"mcp", "rest", "cli"})
    assert product.routes == ("schema_memory",)
    exit_code = main(
        [
            "schema_memory",
            "--operation",
            "validate",
            "--name",
            "atlas-insights",
            "--strict",
            "--json",
        ]
    )
    assert exit_code == 1
    assert '"strict_failed": true' in capsys.readouterr().out
