from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import initialize_vault_state_offline

from exomem import (
    audit,
    commands,
    epistemic_graph,
    memory_schema,
    relation_registry,
    semantic_language_registry,
    state_paths,
)
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


def test_inference_orders_mixed_field_types_by_contract_type_order(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    schema_dir = vault / "Knowledge Base/_Schema"
    schema_dir.mkdir(parents=True)
    (schema_dir / "SKILL.md").write_text("# Test schema\n", encoding="utf-8")
    notes = vault / "Knowledge Base/Notes"
    notes.mkdir()
    (notes / "null.md").write_text(
        "---\ntype: insight\nmixed: null\n---\n\n# Null\n", encoding="utf-8"
    )
    (notes / "integer.md").write_text(
        "---\ntype: insight\nmixed: 1\n---\n\n# Integer\n", encoding="utf-8"
    )

    result = memory_schema.infer_contract(vault, name="typed-order")

    assert result["proposal"]["fields"]["mixed"]["types"] == ["null", "integer"]
    assert result["frequencies"]["fields"]["mixed"]["types"] == [
        "null",
        "integer",
    ]


def test_contract_inference_and_validation_parse_each_page_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    pages = _seed_pages(vault)
    parse_calls = 0
    original_parse = memory_schema.semantic_units.parse_semantic_units

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(
        memory_schema.semantic_units, "parse_semantic_units", counted_parse
    )
    inferred = memory_schema.infer_contract(
        vault, name="one-parse", project="atlas", page_type="insight"
    )

    assert parse_calls == len(pages)

    parse_calls = 0
    contract = memory_schema.contract_from_dict(inferred["proposal"])
    validation = memory_schema.validate_contract(vault, contract)

    assert validation["valid"] is True
    assert parse_calls == len(pages)


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


def test_named_contract_load_rejects_mismatch_and_symlinks(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    directory = vault / "Knowledge Base/_Schema/contracts"
    directory.mkdir(parents=True)
    requested = directory / "requested.yaml"
    requested.write_text(
        "schema_version: 1\nname: different\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="filename"):
        memory_schema.load_contract(vault, "requested")

    requested.unlink()
    target = tmp_path / "target.yaml"
    target.write_text("schema_version: 1\nname: requested\n", encoding="utf-8")
    requested.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        memory_schema.load_contract(vault, "requested")

    symlink_vault = tmp_path / "symlink-vault"
    schema_directory = symlink_vault / "Knowledge Base/_Schema"
    schema_directory.mkdir(parents=True)
    real_directory = tmp_path / "real-contracts"
    real_directory.mkdir()
    (real_directory / "requested.yaml").write_text(
        "schema_version: 1\nname: requested\n", encoding="utf-8"
    )
    (schema_directory / "contracts").symlink_to(
        real_directory, target_is_directory=True
    )
    with pytest.raises(ValueError, match="symlink"):
        memory_schema.load_contract(symlink_vault, "requested")


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
    initialize_vault_state_offline(vault, source="schema memory CLI fixture")
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
    for page in pages[:3]:
        page.write_text(
            page.read_text(encoding="utf-8")
            + "\n- science.replicates: [[Knowledge Base/Notes/future]]\n",
            encoding="utf-8",
        )
    for page in pages[:2]:
        page.write_text(
            page.read_text(encoding="utf-8")
            + "\n- science.sibling: [[Knowledge Base/Notes/future]]\n",
            encoding="utf-8",
        )
    before = {page: page.read_bytes() for page in pages}
    registry_path = relation_registry.extension_registry_path(vault)

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
    assert candidate["count"] == 3
    assert candidate["examples"][0]["path"].endswith("page-0.md")
    assert inferred["proposal"]["extensions"] == {}
    assert len(inferred["promotion_candidates"]) == 1
    promotion = inferred["promotion_candidates"][0]
    assert {
        key: promotion[key]
        for key in (
            "canonical",
            "aliases",
            "parent",
            "description",
            "direction",
            "count",
        )
    } == {
        "canonical": "vault.science_replicates",
        "aliases": ["science.replicates"],
        "parent": None,
        "description": None,
        "direction": None,
        "count": 3,
    }
    assert [item["path"] for item in promotion["examples"]] == [
        item["path"] for item in candidate["examples"]
    ]
    assert promotion["raw_variants"] == [
        {
            "raw_relation": "science.replicates",
            "count": 3,
            "examples": promotion["examples"],
        }
    ]
    assert promotion["raw_variants_total"] == 1
    assert promotion["raw_variants_truncated"] is False
    sibling = next(
        item for item in inferred["relations"] if item["raw_relation"] == "science.sibling"
    )
    assert sibling["count"] == 2
    assert "science.sibling" not in inferred["proposal"]["extensions"]
    assert inferred["warnings"][0]["code"] == "model_suggestions_unavailable"
    assert {page: page.read_bytes() for page in pages} == before
    assert not registry_path.exists()

    with pytest.raises(ValueError, match="INCOMPLETE_RELATION_PROPOSAL"):
        commands.op_schema_memory(vault, operation="infer", subject="relations", save=True)
    with pytest.raises(ValueError, match="OBSERVED_RELATION_DELETION"):
        commands.op_schema_memory(
            vault,
            operation="infer",
            subject="relations",
            save=True,
            proposal=inferred["proposal"],
        )


def test_relation_inference_preserves_raw_census_and_aggregates_promotions(
    tmp_path: Path,
) -> None:
    notes = tmp_path / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    labels = (
        "applies-to",
        "applies_to",
        "science.replicates",
        "science_replicates",
    )
    for index, label in enumerate(labels):
        (notes / f"{index}.md").write_text(
            "---\ntype: insight\n---\n# Source\n\n"
            f"- {label}: [[Knowledge Base/Notes/target]]\n",
            encoding="utf-8",
        )

    inferred = memory_schema.infer_relation_registry(
        tmp_path, recurrence_threshold=1
    )

    census = {item["raw_relation"]: item for item in inferred["relations"]}
    assert set(census) == set(labels)
    assert all(item["canonical"] is None for item in census.values())
    assert all(item["count"] == 1 for item in census.values())
    for index, label in enumerate(labels):
        assert census[label]["examples"] == [
            {
                "path": f"Knowledge Base/Notes/{index}.md",
                "anchor": "line-3",
            }
        ]

    candidates = inferred["promotion_candidates"]
    assert [
        (item["canonical"], item["aliases"], item["count"])
        for item in candidates
    ] == [
        ("vault.applies_to", ["applies_to"], 2),
        ("vault.science_replicates", ["science.replicates"], 1),
        ("vault.science_replicates_2", ["science_replicates"], 1),
    ]
    applies = candidates[0]
    assert applies["raw_variants_total"] == 2
    assert applies["raw_variants_truncated"] is False
    assert applies["raw_variants"] == [
        {
            "raw_relation": "applies-to",
            "count": 1,
            "examples": census["applies-to"]["examples"],
        },
        {
            "raw_relation": "applies_to",
            "count": 1,
            "examples": census["applies_to"]["examples"],
        },
    ]
    identities = [item["canonical"] for item in candidates] + [
        alias for item in candidates for alias in item["aliases"]
    ]
    assert len(identities) == len(set(identities))


def test_relation_inference_census_is_scoped_aggregate_and_non_authoring(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    notes = vault / "Knowledge Base" / "Notes"
    schema = vault / "Knowledge Base" / "_Schema"
    notes.mkdir(parents=True)
    schema.mkdir()
    (schema / "relation-registry.yaml").write_text(
        """\
schema_version: 1
extensions:
  vault.applies_to:
    parent: relates_to
    description: A synthetic applicability relation.
    direction: directed
    aliases: [applies_to]
  vault.legacy_applies:
    parent: relates_to
    description: A retired synthetic applicability relation.
    direction: directed
    status: deprecated
    replaced_by: vault.applies_to
""",
        encoding="utf-8",
    )
    pages = {
        "old-core.md": "---\ncreated: 2020-01-10\ntype: insight\n---\n- supports [[Target]]\n",
        "old-canonical-extension.md": (
            "---\ncreated: 2020-02-01\ntype: procedure\n---\n- vault.applies_to [[Target]]\n"
        ),
        "old-extension.md": (
            "---\ncaptured: 2020-02-10\ntype: procedure\n---\n- applies_to [[Target]]\n"
        ),
        "old-deprecated.md": (
            "---\ncreated: 2020-03-10\ntype: insight\n---\n- vault.legacy_applies [[Target]]\n"
        ),
        "old-generic.md": "---\ncreated: 2020-04-10\ntype: insight\n---\n- relates_to [[Target]]\n",
        "current-unregistered.md": (
            "---\ncreated: 2026-01-10\ntype: insight\n---\n- unregistered.label: [[Target]]\n"
        ),
        "current-body-only.md": (
            "---\ncreated: 2026-02-10\ntype: insight\n---\nA body-only [[Target]] connection.\n"
        ),
        "undated.md": (
            "---\nupdated: 1999-01-01\ntype: insight\n---\n"
            "Synthetic disconnected page.\n\n"
            "```text\n[[Ignored fenced target]]\n```\n"
            "Escaped \\[\\[Ignored escaped target\\]\\] and `[[Ignored inline target]]`.\n"
        ),
    }
    for name, body in pages.items():
        (notes / name).write_text(body, encoding="utf-8")
    before = {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    derived_state = state_paths.vault_state_dir(vault)
    before_derived = (
        {path: path.read_bytes() for path in derived_state.rglob("*") if path.is_file()}
        if derived_state.exists()
        else {}
    )
    assert not epistemic_graph.sidecar_path(vault).exists()

    all_pages = commands.op_schema_memory(
        vault, operation="infer", subject="relations"
    )
    census = all_pages["census"]
    assert census == {
        "relation_counts": {
            "core": 1,
            "extension": 2,
            "deprecated": 1,
            "generic": 1,
            "unregistered": 1,
        },
        "page_counts": {
            "zero_authored_relation_rows": 2,
            "zero_body_connections": 1,
        },
        "denominators": {
            "sampled": 8,
            "included": 7,
            "undated": 1,
            "excluded": 0,
        },
    }
    assert {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()} == before
    after_derived = (
        {path: path.read_bytes() for path in derived_state.rglob("*") if path.is_file()}
        if derived_state.exists()
        else {}
    )
    assert after_derived == before_derived
    assert not epistemic_graph.sidecar_path(vault).exists()
    serialized = json.dumps(census, sort_keys=True)
    assert "Knowledge Base" not in serialized
    assert "Target" not in serialized
    assert "Synthetic" not in serialized

    current = commands.op_schema_memory(
        vault,
        operation="infer",
        subject="relations",
        page_type="insight",
        date_from="2026-01-01",
        date_to="2026-12-31",
    )["census"]
    assert current == {
        "relation_counts": {
            "core": 0,
            "extension": 0,
            "deprecated": 0,
            "generic": 0,
            "unregistered": 1,
        },
        "page_counts": {
            "zero_authored_relation_rows": 1,
            "zero_body_connections": 0,
        },
        "denominators": {
            "sampled": 6,
            "included": 2,
            "undated": 1,
            "excluded": 3,
        },
    }


def test_relation_inference_reserves_canonical_keys_across_distinct_aliases(
    tmp_path: Path,
) -> None:
    notes = tmp_path / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    for index, label in enumerate(("science.replicates", "science_replicates")):
        (notes / f"{index}.md").write_text(
            "---\ntype: insight\n---\n# Source\n\n## Relations\n"
            f"- {label} [[Knowledge Base/Notes/target]]\n",
            encoding="utf-8",
        )

    inferred = memory_schema.infer_relation_registry(
        tmp_path, recurrence_threshold=1
    )

    assert [
        (item["canonical"], item["aliases"])
        for item in inferred["promotion_candidates"]
    ] == [
        ("vault.science_replicates", ["science.replicates"]),
        ("vault.science_replicates_2", ["science_replicates"]),
    ]


def test_relation_inference_bounds_raw_variant_evidence(tmp_path: Path) -> None:
    notes = tmp_path / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    for index, label in enumerate(
        (
            "applies-to",
            "applies--to",
            "applies---to",
            "applies----to",
            "applies-----to",
            "applies_to",
        )
    ):
        (notes / f"{index}.md").write_text(
            "---\ntype: insight\n---\n# Source\n\n"
            f"- {label}: [[Knowledge Base/Notes/target]]\n",
            encoding="utf-8",
        )

    inferred = memory_schema.infer_relation_registry(
        tmp_path, recurrence_threshold=1
    )

    candidate = inferred["promotion_candidates"][0]
    assert candidate["count"] == 6
    assert len(candidate["raw_variants"]) == 5
    assert candidate["raw_variants_total"] == 6
    assert candidate["raw_variants_truncated"] is True


def test_relation_inference_reserves_all_candidate_aliases_before_keys(
    tmp_path: Path,
) -> None:
    notes = tmp_path / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    for index, label in enumerate(("foo", "vault.foo")):
        (notes / f"{index}.md").write_text(
            "---\ntype: insight\n---\n# Source\n\n## Relations\n"
            f"- {label} [[Knowledge Base/Notes/target]]\n",
            encoding="utf-8",
        )

    inferred = memory_schema.infer_relation_registry(
        tmp_path, recurrence_threshold=1
    )

    candidates = inferred["promotion_candidates"]
    assert [
        (item["canonical"], item["aliases"]) for item in candidates
    ] == [
        ("vault.foo_2", ["foo"]),
        ("vault.vault_foo", ["vault.foo"]),
    ]
    identities = [item["canonical"] for item in candidates] + [
        alias for item in candidates for alias in item["aliases"]
    ]
    assert len(identities) == len(set(identities))


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
    for page in pages[:3]:
        page.write_text(
            page.read_text(encoding="utf-8")
            + "\n- science.replicates: [[Knowledge Base/Notes/future]]\n",
            encoding="utf-8",
        )
    result = commands.op_schema_memory(vault, operation="diff", subject="relations")
    assert result["comparison"] == "corpus"
    assert result["changed"] is False
    assert result["changes"]["added"] == []


def _seed_category_pages(vault: Path) -> list[Path]:
    schema_dir = vault / "Knowledge Base" / "_Schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "SKILL.md").write_text("# Test schema\n", encoding="utf-8")
    pages: list[Path] = []
    bodies = (
        "- [Äri Reegel] " + "A" * 190 + " ^long-example\n\n"
        + "".join(f"- [Äri Reegel] Extra example {index}\n" for index in range(5))
        + "\n"
        "## Decision\n- category: runtime_configuration\n- id: rich-config\n\nUse SQLite.\n",
        "- [äri-reegel] Keep evidence attached\n"
        "- [runtime_configuration] Session lifetime is 30 days\n",
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
            "runtime_setting": {
                "description": "Runtime setting facts",
                "aliases": ["runtime_configuration"],
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
        if item["category_key"] == "runtime_configuration"
    )
    assert aliased["resolved_category"] == "runtime_setting"
    assert aliased["registry_status"] == "alias"
    assert aliased["unit_count"] == 2
    assert aliased["forms"] == {"compact": 1, "rich": 1}
    assert all(
        item["category_key"] != "runtime_setting" for item in first["categories"]
    )
    assert first["normalization_candidates"][0]["basis"] == "shared_authored_normalization"


def test_category_validation_keeps_unknown_open_and_reports_deprecation_and_scope(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed_category_pages(vault)
    proposal = {
        "schema_version": 1,
        "categories": {
            "runtime_configuration": {
                "description": "Retired runtime setting facts",
                "status": "deprecated",
                "replaced_by": "runtime_setting",
            },
            "runtime_setting": {
                "description": "Runtime setting facts",
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
            "runtime_configuration": {
                "description": "Runtime setting facts",
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
            "runtime_setting": {"description": 42, "aliases": ["shared"]},
            "business_rule": {"description": "Rules", "aliases": ["shared"]},
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
        "categories": {
            "runtime_setting": {"description": "Runtime setting facts"}
        },
    }

    diff = commands.op_schema_memory(
        vault, operation="diff", subject="categories", proposal=reviewed
    )
    assert diff["comparison"] == "proposal"
    assert diff["changes"]["categories"]["added"] == ["runtime_setting"]
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
    assert "runtime_setting" in loaded.categories

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


def _contract_data(name: str = "contract") -> dict:
    return {
        "schema_version": 1,
        "name": name,
        "scope": {},
        "sample_size": 0,
        "fields": {},
        "blocks": {},
        "relations": {},
    }


def test_legacy_contract_loads_with_additive_defaults_and_deterministic_round_trip() -> None:
    contract = memory_schema.contract_from_dict(_contract_data("legacy"))

    assert contract.validation == "warn"
    assert contract.kinds == {}
    assert contract.categories == {}
    assert contract.unknown_kinds == "allow"
    assert contract.unknown_categories == "allow"
    serialized = contract.as_dict()
    assert list(serialized) == [
        "schema_version",
        "name",
        "scope",
        "validation",
        "sample_size",
        "fields",
        "blocks",
        "kinds",
        "categories",
        "relations",
        "unknown_fields",
        "unknown_blocks",
        "unknown_kinds",
        "unknown_categories",
        "unknown_relations",
    ]
    assert memory_schema.contract_from_dict(serialized) == contract


@pytest.mark.parametrize(
    ("updates", "match"),
    [
        ({"schema_version": True}, "schema_version"),
        ({"name": 7}, "name"),
        ({"sample_size": True}, "sample_size"),
        ({"sample_size": -1}, "sample_size"),
        ({"validation": "blocking"}, "validation"),
        ({"unknown_fields": "sometimes"}, "unknown_fields"),
        ({"unknown_blocks": False}, "unknown_blocks"),
        ({"unknown_kinds": 1}, "unknown_kinds"),
        ({"unknown_categories": "closed"}, "unknown_categories"),
        ({"unknown_relations": None}, "unknown_relations"),
        ({"scope": {"project": "atlas", "extra": "no"}}, "scope"),
        ({"scope": {"project": ""}}, "scope.project"),
        ({"fields": None}, "fields"),
        ({"fields": {1: {"required": True}}}, "fields"),
        ({"fields": {"status": {"other": True}}}, "fields.status"),
        ({"fields": {"status": {"required": 1}}}, "required"),
        ({"fields": {"status": {"types": []}}}, "types"),
        ({"fields": {"status": {"types": ["string", "string"]}}}, "types"),
        ({"fields": {"status": {"types": ["scalar"]}}}, "types"),
        ({"fields": {"status": {"enum": []}}}, "enum"),
        ({"fields": {"status": {"enum": [["nested"]]}}}, "enum"),
        (
            {"fields": {"status": {"types": ["string"], "enum": [1]}}},
            "enum",
        ),
        ({"kinds": {"decision": {"types": ["string"]}}}, "kinds.decision"),
        ({"categories": {"config": {"required": "yes"}}}, "required"),
        ({"unexpected": {}}, "root"),
    ],
)
def test_contract_shape_and_types_fail_closed(updates: dict, match: str) -> None:
    data = {**_contract_data(), **updates}

    with pytest.raises(ValueError, match=match):
        memory_schema.contract_from_dict(data)


def test_contract_enum_is_type_aware_deterministic_and_finite() -> None:
    data = {
        **_contract_data("typed-enum"),
        "fields": {
            "value": {
                "types": ["string", "number", "integer", "boolean"],
                "enum": ["1", 10.0, 2.0, 10, 2, True],
            }
        },
    }

    contract = memory_schema.contract_from_dict(data)

    assert contract.fields["value"]["types"] == [
        "boolean",
        "integer",
        "number",
        "string",
    ]
    assert contract.fields["value"]["enum"] == [True, 2, 10, 2.0, 10.0, "1"]
    duplicate = {
        **data,
        "fields": {"value": {"enum": [True, True]}},
    }
    with pytest.raises(ValueError, match="duplicate"):
        memory_schema.contract_from_dict(duplicate)
    nonfinite = {
        **data,
        "fields": {"value": {"enum": [float("inf")]}},
    }
    with pytest.raises(ValueError, match="finite"):
        memory_schema.contract_from_dict(nonfinite)


def _seed_semantic_contract_pages(vault: Path, count: int = 5) -> list[Path]:
    schema_dir = vault / "Knowledge Base/_Schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "SKILL.md").write_text("# Test schema\n", encoding="utf-8")
    semantic_language_registry.save_registry(
        vault,
        {
            "schema_version": 1,
            "categories": {
                "config": {
                    "description": "Configuration facts",
                    "aliases": ["configuration"],
                }
            },
            "kinds": {},
        },
    )
    pages: list[Path] = []
    for index in range(count):
        path = vault / f"Knowledge Base/Notes/semantic-{index}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "type: insight\n"
            "project: atlas\n"
            "status: active\n"
            "---\n\n"
            f"# Semantic {index}\n\n"
            "- [configuration] Session lifetime is bounded\n\n"
            "## Decision\n"
            "- category: config\n\n"
            "Use durable storage.\n",
            encoding="utf-8",
        )
        pages.append(path)
    return pages


def test_contract_inference_profiles_kinds_and_resolved_categories_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    pages = _seed_semantic_contract_pages(vault)
    calls = 0
    original = memory_schema.semantic_units.parse_semantic_units

    def counted_parse(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(memory_schema.semantic_units, "parse_semantic_units", counted_parse)

    result = memory_schema.infer_contract(
        vault, name="semantic", project="atlas", page_type="insight"
    )

    assert calls == len(pages)
    assert result["proposal"]["blocks"] == {"decision": {"required": True}}
    assert result["proposal"]["kinds"] == {
        "decision": {"required": True},
        "observation": {"required": True},
    }
    assert result["proposal"]["categories"] == {
        "config": {"required": True}
    }
    assert result["frequencies"]["categories"]["config"]["authored_keys"] == [
        "config",
        "configuration",
    ]


def test_contract_validation_covers_new_and_legacy_unknown_policies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    page = _seed_semantic_contract_pages(vault, count=1)[0]
    page.write_text(
        page.read_text(encoding="utf-8")
        + "\n## Observations\n\n- [mystery] Unknown category\n\n"
        "## Relations\n\n- supports [[Knowledge Base/Notes/target]]\n",
        encoding="utf-8",
    )
    contract = memory_schema.contract_from_dict(
        {
            **_contract_data("closed"),
            "fields": {"status": {"required": True}},
            "blocks": {},
            "kinds": {"decision": {"required": True}},
            "categories": {"config": {"required": True}},
            "relations": {},
            "unknown_fields": "forbid",
            "unknown_blocks": "forbid",
            "unknown_kinds": "forbid",
            "unknown_categories": "forbid",
            "unknown_relations": "forbid",
        }
    )
    calls = 0
    original = memory_schema.semantic_units.parse_semantic_units

    def counted_parse(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(memory_schema.semantic_units, "parse_semantic_units", counted_parse)

    result = memory_schema.validate_contract(vault, contract, strict=True)
    codes = {item["code"] for item in result["findings"]}

    assert calls == 1
    assert {
        "CONTRACT_UNKNOWN_FIELD",
        "CONTRACT_UNKNOWN_BLOCK",
        "CONTRACT_UNKNOWN_KIND",
        "CONTRACT_UNKNOWN_CATEGORY",
        "CONTRACT_UNKNOWN_RELATION",
    } <= codes
    unknown_category = next(
        item
        for item in result["findings"]
        if item["code"] == "CONTRACT_UNKNOWN_CATEGORY"
        and item["raw_element"] == "mystery"
    )
    assert unknown_category["governed_element_identity"] == [
        "categories",
        "mystery",
    ]
    assert unknown_category["resolved_rule"] == ["categories", "*", "allowed"]
    assert result["strict_failed"] is True

    open_contract = memory_schema.contract_from_dict(_contract_data("open"))
    assert memory_schema.validate_contract(vault, open_contract)["findings"] == []


def test_required_kind_and_category_findings_name_resolved_rules(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_semantic_contract_pages(vault, count=1)
    contract = memory_schema.contract_from_dict(
        {
            **_contract_data("required-units"),
            "kinds": {"protocol": {"required": True}},
            "categories": {"rule": {"required": True}},
        }
    )

    findings = memory_schema.validate_contract(vault, contract)["findings"]

    assert {
        (item["code"], tuple(item["resolved_rule"])) for item in findings
    } == {
        ("CONTRACT_REQUIRED_KIND", ("kinds", "protocol", "required")),
        ("CONTRACT_REQUIRED_CATEGORY", ("categories", "rule", "required")),
    }


def test_category_findings_retain_authored_alias_keys(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    page = _seed_semantic_contract_pages(vault, count=1)[0]
    page.write_text(
        "---\ntype: insight\nproject: atlas\n---\n\n# Empty page\n",
        encoding="utf-8",
    )
    required = memory_schema.contract_from_dict(
        {
            **_contract_data("required-alias"),
            "categories": {
                "config": {"required": False},
                "configuration": {"required": True},
            },
        }
    )

    required_finding = memory_schema.validate_contract(vault, required)["findings"][0]

    assert required_finding["resolved_rule"] == [
        "categories",
        "config",
        "required",
    ]
    assert required_finding["raw_element"] == "configuration"
    assert required_finding["element_key"] == "configuration"

    unknown_vault = tmp_path / "unknown-vault"
    unknown_page = _seed_semantic_contract_pages(unknown_vault, count=1)[0]
    unknown_page.write_text(
        "---\ntype: insight\nproject: atlas\n---\n\n"
        "# Alias page\n\n- [Configuration] Authored alias\n",
        encoding="utf-8",
    )
    closed = memory_schema.contract_from_dict(
        {**_contract_data("closed-alias"), "unknown_categories": "forbid"}
    )

    unknown_finding = next(
        item
        for item in memory_schema.validate_contract(unknown_vault, closed)["findings"]
        if item["code"] == "CONTRACT_UNKNOWN_CATEGORY"
    )
    assert unknown_finding["resolved_rule"] == ["categories", "*", "allowed"]
    assert unknown_finding["raw_element"] == "Configuration"
    assert unknown_finding["element_key"] == "configuration"
    assert "unknown category" in unknown_finding["detail"]


def test_empty_category_rule_registry_conflict_uses_a_real_rule_identity(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    schema_dir = vault / "Knowledge Base/_Schema"
    schema_dir.mkdir(parents=True)
    (schema_dir / "SKILL.md").write_text("# Test schema\n", encoding="utf-8")
    semantic_language_registry.save_registry(
        vault,
        {
            "schema_version": 1,
            "categories": {
                "deployment_setting": {
                    "description": "Deployment setting",
                    "scope": {"projects": ["other"]},
                }
            },
            "kinds": {},
        },
    )
    note = vault / "Knowledge Base/Notes/page.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: insight\nproject: atlas\n---\n\n# Page\n", encoding="utf-8"
    )
    contract = memory_schema.contract_from_dict(
        {
            **_contract_data("empty-rule"),
            "validation": "off",
            "categories": {"deployment_setting": {}},
        }
    )

    findings = memory_schema.validate_contract(vault, contract)["findings"]

    assert len(findings) == 1
    assert findings[0]["resolved_rule"] == [
        "categories",
        "deployment_setting",
        "declaration",
    ]


@pytest.mark.parametrize(
    ("mode", "finding_count", "strict_failed"),
    [
        ("off", 0, False),
        ("warn", 1, True),
        ("strict", 1, True),
    ],
)
def test_stored_validation_mode_is_independent_from_command_strict(
    tmp_path: Path, mode: str, finding_count: int, strict_failed: bool
) -> None:
    vault = tmp_path / mode
    _seed_semantic_contract_pages(vault, count=1)
    contract = memory_schema.contract_from_dict(
        {
            **_contract_data(mode),
            "validation": mode,
            "kinds": {"protocol": {"required": True}},
        }
    )

    result = memory_schema.validate_contract(vault, contract, strict=True)

    assert result["validation"] == mode
    assert len(result["findings"]) == finding_count
    assert result["strict_failed"] is strict_failed


def test_contract_diff_includes_every_additive_field_and_unknown_policy() -> None:
    before = memory_schema.contract_from_dict(_contract_data("before"))
    after = memory_schema.contract_from_dict(
        {
            **_contract_data("after"),
            "validation": "strict",
            "kinds": {"decision": {"required": True}},
            "categories": {"config": {"required": True}},
            "unknown_fields": "forbid",
            "unknown_blocks": "forbid",
            "unknown_kinds": "forbid",
            "unknown_categories": "forbid",
            "unknown_relations": "forbid",
        }
    )

    diff = memory_schema.diff_contracts(before, after)

    assert diff["changed"] is True
    assert diff["changes"]["validation"] == {"before": "warn", "after": "strict"}
    assert diff["changes"]["kinds"]["added"] == ["decision"]
    assert diff["changes"]["categories"]["added"] == ["config"]
    for key in (
        "unknown_fields",
        "unknown_blocks",
        "unknown_kinds",
        "unknown_categories",
        "unknown_relations",
    ):
        assert diff["changes"][key] == {"before": "allow", "after": "forbid"}


def test_contract_diff_compares_enum_values_by_exact_yaml_type() -> None:
    before = memory_schema.contract_from_dict(
        {
            **_contract_data("before-typed"),
            "fields": {"value": {"enum": [True]}},
        }
    )
    after = memory_schema.contract_from_dict(
        {
            **_contract_data("after-typed"),
            "fields": {"value": {"enum": [1]}},
        }
    )

    diff = memory_schema.diff_contracts(before, after)

    assert diff["changes"]["fields"]["enum_changes"] == {
        "value": {"before": [True], "after": [1]}
    }
