from __future__ import annotations

from pathlib import Path

import pytest

from exomem import audit, commands, memory_schema, semantic_language_registry
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


def test_canonical_relations_drive_contract_inference_validation_and_diff(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    pages = _seed_pages(vault)
    target = "Knowledge Base/Notes/future"
    for page in pages:
        body = page.read_text(encoding="utf-8").replace(
            f"- supports [[{target}]]\n",
            "A generic reference remains [[Knowledge Base/Notes/other]].\n\n"
            "## Relations\n\n"
            f"- supports [[{target}]]\n"
            "- science.unreviewed [[Knowledge Base/Notes/unknown]]\n",
        )
        page.write_text(body, encoding="utf-8")

    inferred = commands.op_schema_memory(
        vault,
        operation="infer",
        name="canonical-relations",
        project="atlas",
        page_type="insight",
        save=True,
    )
    proposal = inferred["proposal"]

    assert proposal["relations"] == {"supports": {"required": True}}
    assert inferred["frequencies"]["relations"]["supports"] == {
        "count": 5,
        "frequency": 1.0,
    }
    assert commands.op_schema_memory(
        vault, operation="validate", name="canonical-relations", strict=True
    )["valid"] is True

    pages[0].write_text(
        pages[0].read_text(encoding="utf-8").replace(
            f"- supports [[{target}]]\n", ""
        ),
        encoding="utf-8",
    )
    validation = commands.op_schema_memory(
        vault, operation="validate", name="canonical-relations", strict=True
    )
    diff = commands.op_schema_memory(
        vault, operation="diff", name="canonical-relations"
    )

    assert "body.relation:supports" in {
        finding["span"] for finding in validation["findings"]
    }
    assert diff["changes"]["relations"]["required_removed"] == ["supports"]


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
        command for command in commands.PRODUCT_COMMANDS if command.name == "schema_memory"
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


def test_relation_inference_is_evidence_backed_and_proposal_first(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    pages = _seed_pages(vault)
    pages[0].write_text(
        pages[0].read_text(encoding="utf-8")
        + "\n- science.replicates: [[Knowledge Base/Notes/future]]\n",
        encoding="utf-8",
    )
    before = pages[0].read_text(encoding="utf-8")

    inferred = commands.op_schema_memory(
        vault,
        operation="infer",
        subject="relations",
        project="atlas",
        include_model_suggestions=True,
    )

    candidate = next(
        item for item in inferred["relations"] if item["raw_relation"] == "science.replicates"
    )
    assert candidate["registry_status"] == "unregistered"
    assert candidate["count"] == 1
    assert candidate["examples"][0]["path"].endswith("page-0.md")
    assert inferred["proposal"]["extensions"]["science.replicates"] == {
        "parent": None,
        "description": None,
    }
    assert inferred["warnings"][0]["code"] == "model_suggestions_unavailable"
    assert pages[0].read_text(encoding="utf-8") == before

    with pytest.raises(ValueError, match="INCOMPLETE_RELATION_PROPOSAL"):
        commands.op_schema_memory(vault, operation="infer", subject="relations", save=True)
    with pytest.raises(ValueError, match="INVALID_RELATION_REGISTRY"):
        commands.op_schema_memory(
            vault,
            operation="infer",
            subject="relations",
            save=True,
            proposal=inferred["proposal"],
        )


def test_reviewed_relation_proposal_saves_and_observed_deletion_is_refused(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    pages = _seed_pages(vault)
    pages[0].write_text(
        pages[0].read_text(encoding="utf-8")
        + "\n- science.replicates: [[Knowledge Base/Notes/future]]\n",
        encoding="utf-8",
    )
    reviewed = {
        "schema_version": 1,
        "extensions": {
            "science.replicates": {
                "parent": "supports",
                "description": "Reports an independent reproduction",
            }
        },
    }
    saved = commands.op_schema_memory(
        vault,
        operation="infer",
        subject="relations",
        save=True,
        proposal=reviewed,
    )["saved"]
    validation = commands.op_schema_memory(
        vault, operation="validate", subject="relations", strict=True
    )
    assert validation["valid"] is True

    with pytest.raises(ValueError, match="OBSERVED_RELATION_DELETION"):
        commands.op_schema_memory(
            vault,
            operation="infer",
            subject="relations",
            project="different-project",
            save=True,
            expected_hash=saved["content_hash"],
            proposal={"schema_version": 1, "extensions": {}},
        )


def test_traversal_profile_governance_validates_diffs_and_saves(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    proposal = {
        "schema_version": 1,
        "profiles": {
            "evidence-only": {
                "extends": "provenance",
                "remove_families": ["citation"],
                "max_nodes": 20,
            }
        },
    }
    diff = commands.op_schema_memory(
        vault, operation="diff", subject="traversal-profiles", proposal=proposal
    )
    assert diff["changed"] is True
    assert diff["changes"]["added"] == ["evidence-only"]
    validated = commands.op_schema_memory(
        vault, operation="validate", subject="traversal-profiles", proposal=proposal
    )
    assert validated["valid"] is True
    saved = commands.op_schema_memory(
        vault,
        operation="infer",
        subject="traversal-profiles",
        proposal=proposal,
        save=True,
    )
    assert saved["saved"]["created"] is True
    assert (
        "evidence-only" in saved["profiles"]
        or "evidence-only"
        in commands.op_schema_memory(vault, operation="infer", subject="traversal-profiles")[
            "profiles"
        ]
    )


def test_relation_registry_audit_is_explicit_and_not_default_attention_noise(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    pages = _seed_pages(vault)
    pages[0].write_text(
        pages[0].read_text(encoding="utf-8")
        + "\n- science.unknown: [[Knowledge Base/Notes/future]]\n",
        encoding="utf-8",
    )
    assert "relation_registry" not in audit.ALL_CATEGORIES
    report = commands.op_audit(vault, categories=["relation_registry"])
    assert report["summary"]["relation_registry"] == 1
    assert report["findings"][0]["meta"]["code"] == "unregistered"


def test_relation_diff_without_proposal_compares_corpus_reality(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    pages = _seed_pages(vault)
    pages[0].write_text(
        pages[0].read_text(encoding="utf-8")
        + "\n- science.replicates: [[Knowledge Base/Notes/future]]\n",
        encoding="utf-8",
    )
    result = commands.op_schema_memory(vault, operation="diff", subject="relations")
    assert result["comparison"] == "corpus"
    assert result["changed"] is True
    assert result["changes"]["added"] == ["science.replicates"]


def _seed_category_pages(vault: Path) -> list[Path]:
    schema_dir = vault / "Knowledge Base" / "_Schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "SKILL.md").write_text("# Test schema\n", encoding="utf-8")
    pages: list[Path] = []
    bodies = (
        "- [Äri Reegel] " + "A" * 190 + " ^long-example\n\n"
        + "".join(f"- [Äri Reegel] Extra example {index}\n" for index in range(5))
        + "\n"
        "## Decision\n- category: configuration\n- id: rich-config\n\nUse SQLite.\n",
        "- [äri-reegel] Keep evidence attached\n"
        "- [configuration] Session lifetime is 30 days\n",
    )
    for index, body in enumerate(bodies):
        path = vault / "Knowledge Base" / "Notes" / f"categories-{index}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "type: insight\n"
            "project: atlas\n"
            "projects:\n"
            "  - atlas\n"
            "  - companion\n"
            "---\n\n"
            f"# Categories {index}\n\n{body}",
            encoding="utf-8",
        )
        pages.append(path)
    return pages


def test_category_inference_profiles_authored_keys_forms_scopes_and_examples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    pages = _seed_category_pages(vault)
    reviewed = {
        "schema_version": 1,
        "categories": {
            "config": {
                "description": "Configuration facts",
                "aliases": ["configuration"],
                "scope": {"projects": ["companion"], "page_types": ["insight"]},
            }
        },
        "kinds": {},
    }
    semantic_language_registry.save_registry(vault, reviewed)
    parse_calls = 0
    original_parse = memory_schema.semantic_units.parse_semantic_units

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(memory_schema.semantic_units, "parse_semantic_units", counted_parse)
    before = [path.read_text(encoding="utf-8") for path in pages]

    first = commands.op_schema_memory(
        vault, operation="infer", subject="categories", project="atlas"
    )
    second = commands.op_schema_memory(
        vault, operation="infer", subject="categories", project="atlas"
    )

    assert first == second
    assert parse_calls == 2 * len(pages)
    assert first["page_count"] == first["sample_size"] == 2
    assert first["unit_count"] == first["observation_count"] == 9
    assert first["proposal"] == reviewed
    assert first["candidate_changes"] == []
    assert first["registry_findings"] == []
    assert [path.read_text(encoding="utf-8") for path in pages] == before

    authored = next(
        item for item in first["categories"] if item["category_key"] == "äri_reegel"
    )
    assert authored["unit_count"] == 7
    assert authored["page_count"] == 2
    assert authored["raw_forms"] == {"Äri Reegel": 6, "äri-reegel": 1}
    assert authored["canonical_collision"] is True
    assert authored["forms"] == {"compact": 7}
    assert authored["page_types"] == {"insight": 7}
    assert authored["projects"] == {"atlas": 7, "companion": 7}
    assert authored["examples"][0]["path"].endswith("categories-0.md")
    assert authored["examples"][0]["anchor"] == "long-example"
    assert authored["examples"][0]["excerpt_truncated"] is True
    assert len(authored["examples"]) == 5

    aliased = next(
        item
        for item in first["categories"]
        if item["category_key"] == "configuration"
    )
    assert aliased["resolved_category"] == "config"
    assert aliased["registry_status"] == "alias"
    assert aliased["unit_count"] == 2
    assert aliased["forms"] == {"compact": 1, "rich": 1}
    assert all(item["category_key"] != "config" for item in first["categories"])
    assert first["normalization_candidates"][0]["basis"] == "shared_authored_normalization"


def test_category_validation_keeps_unknown_open_and_reports_deprecation_and_scope(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed_category_pages(vault)
    proposal = {
        "schema_version": 1,
        "categories": {
            "configuration": {
                "description": "Retired configuration facts",
                "status": "deprecated",
                "replaced_by": "config",
            },
            "config": {
                "description": "Configuration facts",
                "scope": {"projects": ["other"]},
            },
        },
        "kinds": {},
    }

    result = commands.op_schema_memory(
        vault,
        operation="validate",
        subject="categories",
        proposal=proposal,
        strict=True,
    )

    codes = {finding["code"] for finding in result["findings"]}
    assert "deprecated" in codes
    assert "scope_violation" not in codes
    assert result["valid"] is True
    assert result["strict_failed"] is True
    assert "äri_reegel" not in codes

    scoped = {
        **proposal,
        "categories": {
            **proposal["categories"],
            "configuration": {
                "description": "Configuration facts",
                "scope": {"projects": ["other"]},
            },
        },
    }
    scoped_result = commands.op_schema_memory(
        vault, operation="validate", subject="categories", proposal=scoped
    )
    assert "scope_violation" in {
        finding["code"] for finding in scoped_result["findings"]
    }

    invalid = {
        "schema_version": 1,
        "categories": {
            "config": {"description": 42, "aliases": ["shared"]},
            "rule": {"description": "Rules", "aliases": ["shared"]},
        },
        "kinds": {},
    }
    invalid_validation = commands.op_schema_memory(
        vault, operation="validate", subject="categories", proposal=invalid
    )
    invalid_diff = commands.op_schema_memory(
        vault, operation="diff", subject="categories", proposal=invalid
    )
    invalid_codes = {item["code"] for item in invalid_validation["findings"]}
    assert {"invalid_type", "alias_conflict"} <= invalid_codes
    assert invalid_validation["valid"] is False
    assert {item["code"] for item in invalid_diff["registry_findings"]} >= invalid_codes


def test_category_command_diff_and_reviewed_save_preserve_custom_kinds(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed_category_pages(vault)
    current = {
        "schema_version": 1,
        "categories": {},
        "kinds": {
            "protocol": {
                "description": "A repeatable protocol",
                "heading_aliases": ["protocols"],
            }
        },
    }
    created = semantic_language_registry.save_registry(vault, current)
    reviewed = {
        **current,
        "categories": {"config": {"description": "Configuration facts"}},
    }

    diff = commands.op_schema_memory(
        vault, operation="diff", subject="categories", proposal=reviewed
    )
    assert diff["comparison"] == "proposal"
    assert diff["changes"]["categories"]["added"] == ["config"]
    assert diff["changes"]["kinds"] == {"added": [], "removed": [], "modified": {}}

    inferred = commands.op_schema_memory(vault, operation="infer", subject="categories")
    assert inferred["proposal"]["kinds"] == current["kinds"]
    with pytest.raises(ValueError, match="INCOMPLETE_SEMANTIC_LANGUAGE_PROPOSAL"):
        commands.op_schema_memory(
            vault,
            operation="infer",
            subject="categories",
            save=True,
            proposal={"schema_version": 1, "categories": reviewed["categories"]},
            expected_hash=created["content_hash"],
        )
    with pytest.raises(ValueError, match="CATEGORY_SAVE_KIND_CHANGE"):
        commands.op_schema_memory(
            vault,
            operation="infer",
            subject="categories",
            save=True,
            proposal={**reviewed, "kinds": {}},
            expected_hash=created["content_hash"],
        )
    with pytest.raises(ValueError, match="INVALID_SCHEMA_OPERATION"):
        commands.op_schema_memory(
            vault,
            operation="validate",
            subject="categories",
            save=True,
            proposal=reviewed,
        )

    saved = commands.op_schema_memory(
        vault,
        operation="infer",
        subject="categories",
        save=True,
        proposal=reviewed,
        expected_hash=created["content_hash"],
    )["saved"]
    assert saved["created"] is False
    loaded = semantic_language_registry.load_registry(vault)
    assert "protocol" in loaded.kinds
    assert "config" in loaded.categories

    kind_change = {
        **reviewed,
        "kinds": {
            "workflow": {"description": "A workflow"},
        },
    }
    separate = commands.op_schema_memory(
        vault, operation="diff", subject="categories", proposal=kind_change
    )
    assert separate["changes"]["categories"] == {
        "added": [],
        "removed": [],
        "modified": {},
    }
    assert separate["changes"]["kinds"]["added"] == ["workflow"]
    assert separate["changes"]["kinds"]["removed"] == ["protocol"]


def test_category_profile_retains_rich_kind_scoped_to_any_attached_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    schema_dir = vault / "Knowledge Base" / "_Schema"
    schema_dir.mkdir(parents=True)
    (schema_dir / "SKILL.md").write_text("# Test schema\n", encoding="utf-8")
    page = vault / "Knowledge Base" / "Notes" / "multi-project.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\n"
        "type: insight\n"
        "project: atlas\n"
        "projects:\n"
        "  - atlas\n"
        "  - companion\n"
        "---\n\n"
        "# Multi-project page\n\n"
        "## Protocol\n\n"
        "Run the recovery steps in order.\n",
        encoding="utf-8",
    )
    reviewed = {
        "schema_version": 1,
        "categories": {},
        "kinds": {
            "protocol": {
                "description": "A repeatable protocol",
                "scope": {"projects": ["companion"]},
            }
        },
    }
    semantic_language_registry.save_registry(vault, reviewed)
    parse_calls = 0
    original_parse = memory_schema.semantic_units.parse_semantic_units

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(memory_schema.semantic_units, "parse_semantic_units", counted_parse)

    result = commands.op_schema_memory(
        vault,
        operation="infer",
        subject="categories",
        project="atlas",
    )

    assert parse_calls == 1
    assert result["unit_count"] == 1
    assert result["proposal"] == reviewed
    assert result["categories"] == [
        {
            "category_key": "protocol",
            "resolved_category": "protocol",
            "registry_status": "unregistered",
            "replacement": None,
            "resolved_categories": {"protocol": 1},
            "registry_statuses": {"unregistered": 1},
            "replacements": {},
            "unit_count": 1,
            "page_count": 1,
            "raw_forms": {"Protocol": 1},
            "canonical_collision": False,
            "forms": {"rich": 1},
            "page_types": {"insight": 1},
            "projects": {"atlas": 1, "companion": 1},
            "examples": [
                {
                    "path": "Knowledge Base/Notes/multi-project.md",
                    "line": 3,
                    "anchor": None,
                    "raw_category": "Protocol",
                    "excerpt": "Run the recovery steps in order.",
                    "excerpt_truncated": False,
                }
            ],
        }
    ]
