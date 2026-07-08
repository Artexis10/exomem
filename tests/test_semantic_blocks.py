"""Semantic block extraction: deterministic Markdown structure, no model load."""

from __future__ import annotations

import builtins
from pathlib import Path

from exomem import semantic_blocks


def _write(vault: Path, rel: str, body: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _by_kind(result: semantic_blocks.SemanticBlockExtraction, kind: str):
    return [b for b in result.blocks if b.kind == kind]


def test_extracts_recognized_sections_without_mutating(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    body = """\
---
type: insight
status: active
---
# Rich Note

## Findings

- The graph can be derived from files.

## Decision

Build a sidecar, not a canonical graph store.

## Risks

- Graph neighborhoods can become noisy.

## Actions

- Add deterministic extraction first.
"""
    path = _write(vault, "Knowledge Base/Notes/Insights/rich-note.md", body)
    before = path.read_text(encoding="utf-8")

    result = semantic_blocks.extract_semantic_blocks(path, vault_root=vault)

    assert path.read_text(encoding="utf-8") == before
    assert _by_kind(result, "finding")[0].text.startswith(
        "The graph can be derived"
    )
    assert _by_kind(result, "decision")[0].text.startswith("Build a sidecar")
    assert _by_kind(result, "risk")[0].text.startswith(
        "Graph neighborhoods can become noisy"
    )
    assert _by_kind(result, "action")[0].text.startswith(
        "Add deterministic extraction"
    )


def test_page_type_contributes_page_level_block(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = _write(
        vault,
        "Knowledge Base/Notes/Patterns/derived-graph.md",
        """\
---
type: pattern
status: active
---
# Derived Graph

## Problem

Graph state should not replace files.
""",
    )

    result = semantic_blocks.extract_semantic_blocks(path, vault_root=vault)
    pattern = _by_kind(result, "pattern")[0]

    assert pattern.path == "Knowledge Base/Notes/Patterns/derived-graph.md"
    assert pattern.anchor == "page"
    assert pattern.heading is None
    assert "Derived Graph" in pattern.text


def test_block_identity_is_stable_and_source_spanned(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = _write(
        vault,
        "Knowledge Base/Notes/Insights/stable.md",
        """\
---
type: insight
---
# Stable

## Findings

The first finding is stable.
""",
    )

    first = semantic_blocks.extract_semantic_blocks(path, vault_root=vault)
    second = semantic_blocks.extract_semantic_blocks(path, vault_root=vault)
    first_finding = _by_kind(first, "finding")[0]
    second_finding = _by_kind(second, "finding")[0]

    assert first_finding.key == second_finding.key
    assert first_finding.source_hash == second_finding.source_hash
    assert first_finding.path == "Knowledge Base/Notes/Insights/stable.md"
    assert first_finding.anchor == "findings"
    assert first_finding.line_start is not None
    assert first_finding.line_end is not None

    path.write_text(
        path.read_text(encoding="utf-8").replace("stable.", "changed."),
        encoding="utf-8",
    )
    changed = semantic_blocks.extract_semantic_blocks(path, vault_root=vault)
    changed_finding = _by_kind(changed, "finding")[0]

    assert changed_finding.key != first_finding.key
    assert changed_finding.source_hash != first_finding.source_hash


def test_unknown_markdown_and_malformed_relation_degrade(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = _write(
        vault,
        "Knowledge Base/Notes/Insights/loose.md",
        """\
---
type: insight
---
# Loose

## Strange Local Heading

This prose should remain ordinary content.

- supports [[Knowledge Base/Notes/Other]]
- [not-a-real-kind] malformed optional block
""",
    )

    result = semantic_blocks.extract_semantic_blocks(path, vault_root=vault)

    assert result.warnings == ()
    assert all(b.kind != "strange-local-heading" for b in result.blocks)
    assert all("supports [[" not in b.text for b in result.blocks)


def test_model_suggestion_path_is_default_off_and_soft_fails(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    path = _write(
        vault,
        "Knowledge Base/Notes/Insights/no-model.md",
        """\
---
type: insight
---
# No Model

## Claim

Extraction should be deterministic by default.
""",
    )

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith(("torch", "sentence_transformers"))
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    default_result = semantic_blocks.extract_semantic_blocks(path, vault_root=vault)
    assert default_result.model_suggestions_available is False
    assert default_result.suggestions == ()

    suggested = semantic_blocks.extract_semantic_blocks(
        path, vault_root=vault, include_model_suggestions=True
    )
    assert suggested.model_suggestions_available is False
    assert suggested.suggestions == ()
    assert any("unavailable" in w for w in suggested.warnings)
