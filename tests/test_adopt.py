"""Existing-vault adoption workflow."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from importlib import resources
from pathlib import Path

import pytest
import yaml

from exomem import adopt as adopt_module
from exomem import commands, knowledge_packs, semantic_census
from exomem.__main__ import main


def _snapshot(root: Path, *, exclude_kb: bool = False) -> dict[str, tuple[int, float]]:
    out: dict[str, tuple[int, float]] = {}
    kb = root / "Knowledge Base"
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if exclude_kb and p.is_relative_to(kb):
            continue
        out[p.relative_to(root).as_posix()] = (p.stat().st_size, p.stat().st_mtime)
    return out


def _legacy_vault(root: Path, *, kb: bool = False) -> Path:
    vault = root / "legacy-vault"
    (vault / "Warranty Case").mkdir(parents=True)
    (vault / "Warranty Case" / "laptop-receipt.md").write_text("# Laptop receipt\n\nreceipt\n", encoding="utf-8")
    (vault / "Creative Assets").mkdir()
    (vault / "Creative Assets" / "shoot-reference.md").write_text("photo ideas\n", encoding="utf-8")
    (vault / "Repos").mkdir()
    (vault / "Repos" / "api-incident.md").write_text("deploy failed\n", encoding="utf-8")
    if kb:
        kb_root = vault / "Knowledge Base"
        (kb_root / "Notes").mkdir(parents=True)
        (kb_root / "Sources").mkdir(parents=True)
        (kb_root / "Sources" / "index.md").write_text(
            "# Sources - Index\n\n## By type\n\n## Recent captures\n\n",
            encoding="utf-8",
        )
        (kb_root / "index.md").write_text(
            "# Knowledge Base\n\n## Counts\n\n- Sources: 0\n\n## Recent activity\n\n",
            encoding="utf-8",
        )
        (kb_root / "log.md").write_text("# Log\n\n---\n", encoding="utf-8")
    return vault


def test_adopt_scan_only_is_read_only_before_init(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    before = _snapshot(vault)

    report = adopt_module.adopt(vault)

    assert _snapshot(vault) == before
    assert report["mode"] == "scan-only"
    assert report["governance"]["kb_present"] is False
    assert report["summary"]["kb"] == {"present": False}
    assert {a["action"] for a in report["next_actions"]} == {"scan-only", "initialize-kb"}


def test_adopt_scan_only_reports_bounded_semantic_census_without_fabrication(
    tmp_path: Path,
) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    notes = vault / "Legacy Notes"
    notes.mkdir()
    (notes / "semantic.md").write_text(
        """# Semantic sample

- [Config] Compact observation. ^config-one

## Finding
- category: Rule

Rich semantic unit.

- [bad/category] malformed candidate
- [x] ordinary task box

```markdown
- [hidden] fenced example
```
""",
        encoding="utf-8",
    )
    before = _snapshot(vault)

    report = adopt_module.adopt(vault, mode="scan-only")

    assert _snapshot(vault) == before
    census = report["semantic_census"]
    assert census["read_only"] is True
    assert census["coverage"]["markdown_files_scanned"] == 4
    assert census["coverage"]["parseable_pages"] == 3
    assert census["units"] == {"total": 2, "compact": 1, "rich": 1}
    assert census["categories"]["raw_frequencies"] == {"Config": 1, "Rule": 1}
    assert census["categories"]["canonical_frequencies"] == {"config": 1, "rule": 1}
    assert census["categories"]["resolved_frequencies"] == {"config": 1, "rule": 1}
    assert census["categories"]["open_categories_valid"] is True
    assert census["diagnostics"]["malformed_candidates"] == 1
    [example] = census["diagnostics"]["examples"]
    assert example["path"] == "Legacy Notes/semantic.md"
    assert example["code"] == "invalid_compact_category"
    assert example["span"]["start_line"] == 10
    encoded = json.dumps(census, sort_keys=True)
    assert "ordinary task box" not in encoded
    assert "fenced example" not in encoded
    assert census["governance"] == {
        "kb_present": False,
        "saved_contracts": {"status": "unavailable", "count": 0, "debt": {}},
        "relation_dispositions": {"status": "unavailable", "counts": {}},
    }
    assert {item["action"] for item in census["safe_next_actions"]} >= {
        "review-malformed-candidates",
        "initialize-kb",
    }


def test_adopt_scan_only_semantic_census_honors_subtree_hidden_and_resource_bounds(
    tmp_path: Path,
) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    focus = vault / "Focus"
    focus.mkdir()
    (focus / "a.md").write_text("- [alpha] First.\n", encoding="utf-8")
    (focus / "b.md").write_text("- [beta] Second.\n", encoding="utf-8")
    (focus / ".hidden.md").write_text("- [secret] Hidden.\n", encoding="utf-8")
    (vault / "outside.md").write_text("- [outside] Ignore.\n", encoding="utf-8")
    before = _snapshot(vault)

    report = adopt_module.adopt(
        vault,
        mode="scan-only",
        path="Focus",
        include_hidden=False,
        semantic_max_files=1,
        semantic_max_bytes=1024,
        semantic_example_limit=1,
    )

    assert _snapshot(vault) == before
    census = report["semantic_census"]
    assert census["scope"] == "Focus"
    assert census["limits"] == {
        "max_files": 1,
        "max_bytes": 1024,
        "max_directory_entries": 32,
        "example_limit": 1,
        "include_hidden": False,
    }
    assert census["coverage"]["markdown_files_scanned"] == 1
    assert census["coverage"]["markdown_files_omitted"] == 1
    assert census["coverage"]["truncated"] is True
    assert census["units"]["total"] == 1
    assert set(census["categories"]["raw_frequencies"]) <= {"alpha", "beta"}
    assert "secret" not in census["categories"]["raw_frequencies"]
    assert "outside" not in census["categories"]["raw_frequencies"]


def test_adopt_semantic_census_bounds_directory_entry_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    focus = vault / "Focus"
    focus.mkdir()
    (focus / "00-semantic.md").write_text("- [bounded] First.\n", encoding="utf-8")
    for index in range(80):
        (focus / f"directory-{index:03d}").mkdir()
        (focus / f"ordinary-{index:03d}.bin").write_bytes(b"not markdown")

    real_scandir = os.scandir
    enumerated = 0

    class CountingScandir:
        def __init__(self, target: object) -> None:
            self._inner = real_scandir(target)

        def __enter__(self) -> CountingScandir:
            self._inner.__enter__()
            return self

        def __exit__(self, *args: object) -> None:
            self._inner.__exit__(*args)

        def __iter__(self) -> CountingScandir:
            return self

        def __next__(self) -> os.DirEntry[str]:
            nonlocal enumerated
            entry = next(self._inner)
            enumerated += 1
            return entry

    monkeypatch.setattr(semantic_census.os, "scandir", CountingScandir)

    census = semantic_census.scan(vault, path="Focus", max_files=1)

    entry_budget = census["limits"].get("max_directory_entries")
    assert isinstance(entry_budget, int)
    assert enumerated == census["coverage"]["directory_entries_enumerated"]
    assert enumerated <= entry_budget
    assert census["coverage"]["omitted_is_lower_bound"] is True
    assert census["coverage"]["truncated"] is True


def test_adopt_semantic_census_rejects_symlink_and_identity_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    focus = vault / "Focus"
    focus.mkdir()
    candidate = focus / "candidate.md"
    candidate.write_text("- [safe] Original.\n", encoding="utf-8")
    outside = vault / "outside.md"
    outside.write_text("- [escaped] Outside.\n", encoding="utf-8")
    (focus / "link.md").symlink_to(outside)
    real_lstat = Path.lstat
    swapped = False

    def swapping_lstat(path: Path) -> os.stat_result:
        nonlocal swapped
        result = real_lstat(path)
        if path == candidate and not swapped:
            candidate.unlink()
            candidate.symlink_to(outside)
            swapped = True
        return result

    monkeypatch.setattr(Path, "lstat", swapping_lstat)

    census = semantic_census.scan(vault, path="Focus", max_files=4)

    assert census["categories"]["raw_frequencies"] == {}
    assert census["coverage"]["unreadable_files"] >= 1


def test_adopt_semantic_census_never_reads_past_remaining_byte_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    focus = vault / "Focus"
    focus.mkdir()
    candidate = focus / "candidate.md"
    candidate.write_text("- [safe] Original.\n", encoding="utf-8")
    real_read_bytes = Path.read_bytes

    def oversized_read(path: Path) -> bytes:
        if path == candidate:
            return b"x" * 4096
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", oversized_read)

    census = semantic_census.scan(
        vault,
        path="Focus",
        max_files=1,
        max_bytes=32,
    )

    assert census["coverage"]["bytes_scanned"] <= 32
    assert census["categories"]["raw_frequencies"] == {"safe": 1}


def test_adopt_semantic_census_rejects_file_growth_during_bounded_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    focus = vault / "Focus"
    focus.mkdir()
    candidate = focus / "candidate.md"
    candidate.write_text("- [safe] Original.\n", encoding="utf-8")
    real_read = os.read
    requested: list[int] = []
    grown = False

    def growing_read(descriptor: int, count: int) -> bytes:
        nonlocal grown
        requested.append(count)
        if not grown:
            with candidate.open("ab") as stream:
                stream.write(b"x" * 4096)
            grown = True
        return real_read(descriptor, count)

    monkeypatch.setattr(semantic_census.os, "read", growing_read)

    census = semantic_census.scan(
        vault,
        path="Focus",
        max_files=1,
        max_bytes=32,
    )

    assert requested
    assert max(requested) <= 33
    assert census["coverage"]["bytes_scanned"] == 0
    assert census["coverage"]["markdown_files_scanned"] == 0
    assert census["categories"]["raw_frequencies"] == {}


def test_adopt_scan_only_semantic_census_reports_saved_governance_read_only(
    tmp_path: Path,
) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    schema = vault / "Knowledge Base" / "_Schema"
    schema.mkdir(parents=True)
    (schema / "semantic-language-registry.yaml").write_text(
        yaml.safe_dump(
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
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    contracts = schema / "contracts"
    contracts.mkdir()
    (contracts / "census.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "census",
                "scope": {"page_type": "insight"},
                "validation": "strict",
                "sample_size": 0,
                "fields": {},
                "blocks": {},
                "kinds": {},
                "categories": {"must_have": {"required": True}},
                "relations": {},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    page = vault / "Knowledge Base" / "Notes" / "semantic.md"
    page.write_text(
        """---
type: insight
exomem_id: 00000000-0000-4000-8000-000000000301
title: Semantic
status: active
created: 2026-07-16
updated: 2026-07-16
sources: []
tags: []
---

# Semantic

- [Config] Canonical spelling.
- [configuration] Alias spelling.
""",
        encoding="utf-8",
    )
    before = _snapshot(vault)

    report = adopt_module.adopt(vault, mode="scan-only")

    assert _snapshot(vault) == before
    census = report["semantic_census"]
    assert census["categories"]["raw_frequencies"] == {
        "Config": 1,
        "configuration": 1,
    }
    assert census["categories"]["canonical_frequencies"] == {
        "config": 1,
        "configuration": 1,
    }
    assert census["categories"]["resolved_frequencies"] == {"config": 2}
    assert census["categories"]["resolved_alias_collisions"] == [
        {
            "resolved": "config",
            "canonical_labels": ["config", "configuration"],
            "count": 2,
        }
    ]
    governance = census["governance"]
    assert governance["kb_present"] is True
    assert governance["saved_contracts"] == {
        "status": "current",
        "count": 1,
        "debt": {"CONTRACT_REQUIRED_CATEGORY": 1},
    }
    assert governance["relation_dispositions"] == {
        "status": "current",
        "counts": {"missing": 1},
    }
    assert not (schema / "semantic-contract-activation.json").exists()


def test_adopt_semantic_census_parses_each_markdown_page_once_and_reports_metadata_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    page = vault / "Knowledge Base" / "Notes" / "semantic.md"
    page.write_text(
        """---
type: insight
exomem_id: 00000000-0000-4000-8000-000000000311
title: Semantic
status: active
created: 2026-07-16
updated: 2026-07-16
sources: []
tags: []
---

# Semantic

- [config] Parse once.
""",
        encoding="utf-8",
    )
    real_build_page_state = semantic_census.semantic_contract.build_page_state
    parser_calls: list[str] = []
    markdown_path_reads: list[str] = []
    real_read_text = Path.read_text

    def counting_build_page_state(*args: object, **kwargs: object) -> object:
        parser_calls.append(str(args[1]))
        return real_build_page_state(*args, **kwargs)

    def counting_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path.suffix.casefold() == ".md":
            markdown_path_reads.append(path.as_posix())
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(
        semantic_census.semantic_contract,
        "build_page_state",
        counting_build_page_state,
    )
    monkeypatch.setattr(Path, "read_text", counting_read_text)

    census = semantic_census.scan(vault)

    assert len(parser_calls) == census["coverage"]["markdown_files_scanned"]
    assert len(parser_calls) == len(set(parser_calls))
    assert markdown_path_reads == []
    assert census["coverage"]["metadata_work"] == {
        "status": "unbounded",
        "bounded": False,
        "counted_as_markdown_bytes": False,
        "sources": [
            "activation_manifest",
            "relation_registry",
            "relation_review_state",
            "saved_contracts",
            "semantic_language_registry",
        ],
    }
    assert census["governance"]["resource_status"] == "unbounded_metadata"


def test_adopt_semantic_census_governance_uses_exact_scanned_page_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    page = vault / "Knowledge Base" / "Notes" / "semantic.md"
    original = """---
type: insight
exomem_id: 00000000-0000-4000-8000-000000000312
title: Semantic
status: active
created: 2026-07-16
updated: 2026-07-16
sources: []
tags: []
---

# Semantic

- [before] Exact scanned bytes.
"""
    replacement = original.replace("[before] Exact scanned bytes", "[after] Replaced")
    page.write_text(original, encoding="utf-8")
    relative = page.relative_to(vault).as_posix()
    real_build_page_state = semantic_census.semantic_contract.build_page_state
    observed_texts: list[str] = []

    def mutate_after_first_parse(*args: object, **kwargs: object) -> object:
        state = real_build_page_state(*args, **kwargs)
        if str(args[1]) == relative:
            observed_texts.append(str(args[2]))
            if len(observed_texts) == 1:
                page.write_text(replacement, encoding="utf-8")
        return state

    monkeypatch.setattr(
        semantic_census.semantic_contract,
        "build_page_state",
        mutate_after_first_parse,
    )

    census = semantic_census.scan(vault)

    assert observed_texts == [original]
    assert census["categories"]["raw_frequencies"] == {"before": 1}
    assert census["governance"]["relation_dispositions"] == {
        "status": "current",
        "counts": {"missing": 1},
    }


def test_adopt_semantic_census_separates_general_registry_findings_from_category_aliases(
    tmp_path: Path,
) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    schema = vault / "Knowledge Base" / "_Schema"
    schema.mkdir(parents=True)
    (schema / "semantic-language-registry.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "categories": {
                    "config": {"aliases": ["shared-category"]},
                    "policy": {"aliases": ["shared-category"]},
                    "malformed": {"aliases": [123]},
                },
                "kinds": {
                    "finding": {"aliases": ["shared-kind"]},
                    "claim": {"aliases": ["shared-kind"]},
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    census = semantic_census.scan(vault)

    findings = census["categories"]["registry_findings"]
    aliases = census["categories"]["alias_conflicts"]
    assert len(findings) > len(aliases)
    assert {finding["namespace"] for finding in findings} >= {"categories", "kinds"}
    assert aliases
    assert all(finding["code"] == "alias_conflict" for finding in aliases)
    assert all(finding["namespace"] == "categories" for finding in aliases)
    assert all(
        str(finding.get("path", "")).startswith("categories.")
        for finding in aliases
    )


def test_adopt_semantic_census_hidden_markdown_makes_governance_partial(
    tmp_path: Path,
) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    notes = vault / "Knowledge Base" / "Notes"
    page_id = "00000000-0000-4000-8000-000000000313"
    visible = notes / "visible.md"
    visible.write_text(
        f"---\ntype: insight\nexomem_id: {page_id}\ntitle: Visible\n---\n\n# Visible\n",
        encoding="utf-8",
    )
    (notes / ".duplicate.md").write_text(
        f"---\ntype: insight\nexomem_id: {page_id}\ntitle: Hidden\n---\n\n# Hidden\n",
        encoding="utf-8",
    )

    census = semantic_census.scan(vault)

    assert census["coverage"]["governance_complete"] is False
    assert census["governance"]["saved_contracts"]["status"] == "partial"
    assert census["governance"]["relation_dispositions"]["status"] == "partial"
    assert census["governance"]["resource_status"] == "partial_corpus"


def test_adopt_semantic_census_separates_identity_ownership_from_corpus_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    page_id = "00000000-0000-4000-8000-000000000314"
    paths = (
        "Knowledge Base/Notes/ordinary.md",
        "Knowledge Base/_Schema/ownership.md",
        "Knowledge Base/_trash/ownership.md",
        "Knowledge Base/Notes/ordinary.sync-conflict-20260716.md",
    )
    for relative in paths:
        page = vault / relative
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            f"---\ntype: insight\nexomem_id: {page_id}\ntitle: Owner\n---\n\n# Owner\n",
            encoding="utf-8",
        )
    real_evaluate = semantic_census.semantic_writes.evaluate_posthoc_batch
    captured: list[object] = []

    def capture_corpus(*args: object, **kwargs: object) -> object:
        captured.append(kwargs["corpus"])
        return real_evaluate(*args, **kwargs)

    monkeypatch.setattr(
        semantic_census.semantic_writes,
        "evaluate_posthoc_batch",
        capture_corpus,
    )

    census = semantic_census.scan(vault, include_hidden=True)

    assert census["governance"]["saved_contracts"]["status"] == "current"
    [corpus] = captured
    assert corpus.identity_census.paths_by_identity[page_id] == tuple(sorted(paths))
    assert "Knowledge Base/Notes/ordinary.md" in corpus.pages
    assert set(paths[1:]).isdisjoint(corpus.pages)


def test_adopt_semantic_census_malformed_raw_id_fails_governance_identity_census(
    tmp_path: Path,
) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    page = vault / "Knowledge Base" / "Notes" / "malformed-id.md"
    page.write_text(
        """---
type: insight
exomem_id: definitely-not-a-uuid
title: Malformed identity
---

# Malformed identity
""",
        encoding="utf-8",
    )

    census = semantic_census.scan(vault)

    assert census["governance"]["saved_contracts"]["status"] == "error"
    assert census["governance"]["saved_contracts"]["error"] == "ValueError"
    assert census["governance"]["relation_dispositions"]["status"] == "error"
    assert census["governance"]["resource_status"] == "partial_identity_error"


def test_adopt_vault_surface_exposes_semantic_census_bounds(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=False)

    report = commands.op_adopt_vault(
        vault,
        semantic_max_files=1,
        semantic_max_bytes=1024,
        semantic_example_limit=1,
    )

    assert report["semantic_census"]["limits"] == {
        "max_files": 1,
        "max_bytes": 1024,
        "max_directory_entries": 32,
        "example_limit": 1,
        "include_hidden": False,
    }


def test_adopt_suggests_builtin_packs_from_structure(tmp_path: Path) -> None:
    report = adopt_module.adopt(_legacy_vault(tmp_path, kb=True))
    by_id = {p["id"]: p for p in report["pack_suggestions"]}

    assert {"creative", "legal-warranty", "technical"} <= set(by_id)
    assert by_id["legal-warranty"]["score"] >= 3
    assert by_id["technical"]["score"] >= 3
    assert "creative" in by_id
    assert {p["id"] for p in report["available_packs"]} >= {
        "legal-warranty",
        "creative",
        "technical",
        "health-athletic",
        "business",
        "personal-records",
    }
    assert "required_fields" in report["pack_schema"]
    assert "purpose" in report["pack_schema"]["required_fields"]
    assert report["pack_schema"]["selection_manifest"] == "Knowledge Base/_Packs/selected-packs.json"
    assert by_id["technical"]["beginner_description"]
    assert by_id["legal-warranty"]["suggested_workflows"][0]["route"]
    assert report["governance"]["kb_present"] is True
    assert {a["action"] for a in report["next_actions"]} >= {"save-manifest", "copy-as-sources"}


def test_adopt_save_manifest_writes_only_under_kb(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    before_legacy = _snapshot(vault, exclude_kb=True)

    report = adopt_module.adopt(
        vault,
        mode="save-manifest",
        today=dt.date(2026, 7, 7),
    )

    assert _snapshot(vault, exclude_kb=True) == before_legacy
    manifest = report["manifest"]
    assert manifest["path"].startswith("Knowledge Base/_Adoption/")
    manifest_path = vault / manifest["path"]
    assert manifest_path.exists()
    text = manifest_path.read_text(encoding="utf-8")
    assert "# Adoption Manifest" in text
    assert "Originals stay where they are" in text


def test_adopt_copy_as_sources_preserves_original_and_records_provenance(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    original = vault / "Warranty Case" / "laptop-receipt.md"
    before = original.read_bytes()
    expected_hash = hashlib.sha256(before).hexdigest()

    report = adopt_module.adopt(
        vault,
        mode="copy-as-sources",
        selected_paths=["Warranty Case/laptop-receipt.md"],
        today=dt.date(2026, 7, 7),
    )

    assert original.read_bytes() == before
    copied = report["copy"]["copied_sources"]
    assert len(copied) == 1
    assert copied[0]["original_path"] == "Warranty Case/laptop-receipt.md"
    assert copied[0]["original_sha256"] == expected_hash
    source_path = vault / copied[0]["source_path"]
    assert source_path.exists()
    assert source_path.as_posix().endswith("Knowledge Base/Sources/Imported/2026-07-07-laptop-receipt.md")
    source_text = source_path.read_text(encoding="utf-8")
    assert "imported_from: Warranty Case/laptop-receipt.md" in source_text
    assert f"original_sha256: {expected_hash}" in source_text
    assert "# Laptop receipt" in source_text


def test_adopt_quotes_yaml_significant_imported_path(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    legacy = vault / "Legacy" / "Step2: Paste your conversation.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# 会話\n\nOriginal.\n", encoding="utf-8")

    report = adopt_module.adopt(
        vault,
        mode="copy-as-sources",
        selected_paths=["Legacy/Step2: Paste your conversation.md"],
        today=dt.date(2026, 7, 12),
    )

    copied = report["copy"]["copied_sources"]
    imported = vault / copied[0]["source_path"]
    raw = imported.read_text(encoding="utf-8")
    frontmatter = raw.removeprefix("---\n").split("\n---\n", 1)[0]
    assert yaml.safe_load(frontmatter)["imported_from"] == "Legacy/Step2: Paste your conversation.md"


def test_adopt_copy_as_sources_disambiguates_same_basename_batch(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    for folder, body in (("Mercor A", "alpha answer"), ("Mercor B", "beta answer")):
        target = vault / folder / "Task1.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# Task1\n\n{body}\n", encoding="utf-8")

    report = adopt_module.adopt(
        vault,
        mode="copy-as-sources",
        selected_paths=["Mercor A/Task1.md", "Mercor B/Task1.md"],
        today=dt.date(2026, 7, 7),
    )

    copied = report["copy"]["copied_sources"]
    source_paths = [item["source_path"] for item in copied]
    assert len(copied) == 2
    assert len(set(source_paths)) == 2
    assert source_paths == [
        "Knowledge Base/Sources/Imported/2026-07-07-task1.md",
        "Knowledge Base/Sources/Imported/2026-07-07-task1-2.md",
    ]
    source_texts = {
        item["original_path"]: (vault / item["source_path"]).read_text(encoding="utf-8")
        for item in copied
    }
    assert "alpha answer" in source_texts["Mercor A/Task1.md"]
    assert "beta answer" in source_texts["Mercor B/Task1.md"]



def test_adopt_compile_selected_copies_and_returns_reviewable_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    original = vault / "Warranty Case" / "laptop-receipt.md"
    before_legacy = _snapshot(vault, exclude_kb=True)

    def fake_propose(root: Path, *, sources: list[str], suggested_title: str | None = None) -> dict:
        assert root == vault
        assert sources == ["Knowledge Base/Sources/Imported/2026-07-07-laptop-receipt"]
        return {
            "suggested_note_type": "insight",
            "suggested_title": "Laptop receipt",
            "suggested_sources": list(sources),
            "suggested_connections": [],
            "outline_markdown": "# Laptop receipt\n\n## Claim\n",
            "warnings": [],
        }

    monkeypatch.setattr(adopt_module.compile_proposal_module, "propose_compilation", fake_propose)

    report = adopt_module.adopt(
        vault,
        mode="compile-selected",
        selected_paths=["Warranty Case/laptop-receipt.md"],
        today=dt.date(2026, 7, 7),
    )

    assert original.read_text(encoding="utf-8") == "# Laptop receipt\n\nreceipt\n"
    assert _snapshot(vault, exclude_kb=True) == before_legacy
    assert list((vault / "Knowledge Base" / "Notes").rglob("*.md")) == []

    plan = report["compile_plan"]
    assert plan["status"] == "ready"
    assert plan["proposal"]["suggested_sources"] == [
        "Knowledge Base/Sources/Imported/2026-07-07-laptop-receipt"
    ]
    assert plan["proposal"]["proposal_ref"].startswith("exomem://proposal/")
    assert plan["next_step"].startswith("Review outline_markdown")

    [source] = plan["sources"]
    assert source["original_path"] == "Warranty Case/laptop-receipt.md"
    assert source["original_ref"] == "exomem://vault/Warranty%20Case/laptop-receipt.md"
    assert source["source_path"] == "Knowledge Base/Sources/Imported/2026-07-07-laptop-receipt.md"
    assert source["source_ref"] == (
        "exomem://source/Knowledge%20Base/Sources/Imported/2026-07-07-laptop-receipt"
    )
    assert source["already_governed"] is False


def test_adopt_compile_selected_requires_explicit_selection(tmp_path: Path) -> None:
    with pytest.raises(adopt_module.AdoptError) as ei:
        adopt_module.adopt(_legacy_vault(tmp_path, kb=True), mode="compile-selected")
    assert ei.value.code == "MISSING_SELECTION"


def test_adopt_compile_selected_skips_unsupported_without_writing(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    (vault / "scan.jpg").write_bytes(b"not really an image")
    before = _snapshot(vault)

    report = adopt_module.adopt(
        vault,
        mode="compile-selected",
        selected_paths=["scan.jpg"],
        today=dt.date(2026, 7, 7),
    )

    assert _snapshot(vault) == before
    plan = report["compile_plan"]
    assert plan["status"] == "empty"
    assert plan["proposal"] is None
    assert plan["skipped"] == [
        {
            "path": "scan.jpg",
            "code": "UNSUPPORTED_IMPORT_TYPE",
            "reason": "compile-selected currently imports text/markdown-like files only",
            "ref": "exomem://vault/scan.jpg",
        }
    ]

def test_adopt_copy_as_sources_requires_explicit_selection(tmp_path: Path) -> None:
    with pytest.raises(adopt_module.AdoptError) as ei:
        adopt_module.adopt(_legacy_vault(tmp_path, kb=True), mode="copy-as-sources")
    assert ei.value.code == "MISSING_SELECTION"


def test_adopt_unsupported_mode_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(adopt_module.AdoptError) as ei:
        adopt_module.adopt(_legacy_vault(tmp_path), mode="teleport")
    assert ei.value.code == "UNSUPPORTED_MODE"
    assert "supported modes" in ei.value.reason
    assert "compile-selected" in ei.value.reason


def test_pack_suggestions_default_to_personal_records() -> None:
    out = knowledge_packs.suggest_packs({"tree": []})
    assert out[0]["id"] == "personal-records"
    assert out[0]["score"] == 0


def test_pack_validation_rejects_unknown_fields() -> None:
    raw = knowledge_packs.list_builtin_packs()[0]
    raw["surprise"] = True
    with pytest.raises(knowledge_packs.PackValidationError) as ei:
        knowledge_packs.validate_pack_dict(raw)
    assert ei.value.code == "UNKNOWN_FIELD"
def test_pack_validation_rejects_invalid_workflows() -> None:
    raw = knowledge_packs.list_builtin_packs()[0]
    raw["suggested_workflows"] = [{"title": "Missing route", "intent": "x", "example": "x"}]
    with pytest.raises(knowledge_packs.PackValidationError) as ei:
        knowledge_packs.validate_pack_dict(raw)
    assert ei.value.code == "MISSING_WORKFLOW_FIELD"

    raw = knowledge_packs.list_builtin_packs()[0]
    raw["default_note_types"] = []
    with pytest.raises(knowledge_packs.PackValidationError) as ei:
        knowledge_packs.validate_pack_dict(raw)
    assert ei.value.code == "INVALID_FIELD"


def test_selected_pack_manifest_roundtrip(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=True)

    written = knowledge_packs.write_selected_packs(
        vault,
        ["technical", "creative", "technical"],
        source="test",
        today=dt.date(2026, 7, 7),
    )
    state = knowledge_packs.selected_pack_state(vault)

    assert written["path"] == "Knowledge Base/_Packs/selected-packs.json"
    assert written["selected_pack_ids"] == ["technical", "creative"]
    assert state["manifest_present"] is True
    assert state["selected_pack_ids"] == ["technical", "creative"]
    assert state["packs"][0]["agent_instructions"]


def test_builtin_packs_are_declarative_files() -> None:
    base = resources.files("exomem").joinpath("packs")
    names = sorted(entry.name for entry in base.iterdir() if entry.name.endswith(".json"))

    assert names == [
        "business.json",
        "creative.json",
        "health-athletic.json",
        "legal-warranty.json",
        "personal-records.json",
        "technical.json",
    ]
    raw = json.loads(base.joinpath("legal-warranty.json").read_text(encoding="utf-8"))
    assert knowledge_packs.validate_pack_dict(raw).id == "legal-warranty"
    assert knowledge_packs.pack_schema()["directory"] == "src/exomem/packs/"


def test_pack_validation_rejects_invalid_primitives_and_actions() -> None:
    raw = knowledge_packs.list_builtin_packs()[0]
    raw["primitives"] = ["source", "mind-palace"]
    with pytest.raises(knowledge_packs.PackValidationError) as ei:
        knowledge_packs.validate_pack_dict(raw)
    assert ei.value.code == "INVALID_PRIMITIVE"

    raw = knowledge_packs.list_builtin_packs()[0]
    raw["actions"] = ["save", "teleport"]
    with pytest.raises(knowledge_packs.PackValidationError) as ei:
        knowledge_packs.validate_pack_dict(raw)
    assert ei.value.code == "INVALID_ACTION"


def test_adopt_registry_exposure_survives_tier2_optout() -> None:
    from exomem.commands import product_commands_for

    for surface in ("mcp", "cli", "rest"):
        commands = {c.name: c for c in product_commands_for(surface, expose_tier2=False)}
        assert "adopt_vault" in commands, f"adopt_vault missing from {surface} with Tier 2 off"
        assert commands["adopt_vault"].product_surface == "primary"
        assert "adopt" in commands["adopt_vault"].product_actions


def test_adopt_cli_door(vault: Path, capsys) -> None:
    code = main(["adopt", "--json"])
    out = capsys.readouterr().out

    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert payload["data"]["mode"] == "scan-only"
    assert payload["data"]["summary"]["kb"]["present"] is True


def test_product_cli_scan_only_adoption_allows_pre_init_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))

    code = main(["adopt_vault", ".", "--mode", "scan-only", "--json"])
    out = capsys.readouterr().out

    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert payload["data"]["mode"] == "scan-only"
    assert payload["data"]["summary"]["kb"]["present"] is False


def test_product_cli_browse_memory_allows_pre_init_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))

    code = main(["browse_memory", ".", "--mode", "overview", "--json"])
    out = capsys.readouterr().out

    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert payload["data"]["kb"]["present"] is False
    assert payload["data"]["totals"]["markdown"] >= 3


def test_product_cli_write_adoption_still_requires_initialized_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))

    code = main(["adopt_vault", ".", "--mode", "copy-as-sources", "--json"])
    out = capsys.readouterr().out

    assert code == 1
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is False
    assert "does not look like a vault" in payload["error"]["message"]


def test_adopt_cli_human_output_is_product_shaped(vault: Path, capsys) -> None:
    code = main(["adopt"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Adoption report" in out
    assert "Likely packs" in out
    assert "Safe next actions" in out
    assert "Originals: untouched" in out
    assert '"mode"' not in out
