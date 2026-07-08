from __future__ import annotations

from pathlib import Path

from exomem import claims, context_pack, find as find_module, semantic_blocks
from exomem.find import Hit


def _label(name: str) -> str:
    return name.replace("_", " ").title()


def test_parses_all_required_block_types_from_headings() -> None:
    markdown = "\n\n".join(
        f"## {_label(block_type)}\n\nBody for {block_type}."
        for block_type in sorted(semantic_blocks.BLOCK_TYPES)
    )

    document = semantic_blocks.parse_semantic_blocks(markdown)

    assert document.is_valid
    assert {block.type for block in document.blocks} == semantic_blocks.BLOCK_TYPES
    assert all(block.level == 2 for block in document.blocks)
    assert all(block.line > 0 for block in document.blocks)
    assert all(block.body.startswith("Body for ") for block in document.blocks)


def test_unknown_headings_are_not_semantic_blocks_or_errors() -> None:
    markdown = "# Title\n\n## Background\n\nOrdinary section.\n"

    document = semantic_blocks.parse_semantic_blocks(markdown)

    assert document.blocks == []
    assert document.errors == []
    assert document.warnings == []


def test_metadata_relations_and_body_are_parsed() -> None:
    markdown = """\
## Claim
- id: c1
- status: active
- relations: supports: [[A]], evidenced_by: [[Source]]

The claim remains Markdown.

- A normal body bullet.
- [[A wikilink]]
"""

    document = semantic_blocks.parse_semantic_blocks(markdown)
    block = document.blocks[0]

    assert document.is_valid
    assert block.id == "c1"
    assert block.metadata["status"] == "active"
    assert [relation.kind for relation in block.relations] == ["supports", "evidenced_by"]
    assert [relation.target for relation in block.relations] == ["[[A]]", "[[Source]]"]
    assert "- id:" not in block.body
    assert block.body.startswith("The claim remains Markdown.")
    assert "- [[A wikilink]]" in block.body


def test_parses_all_required_relation_types() -> None:
    relation_names = [
        "supports",
        "contradicts",
        "refines",
        "supersedes",
        "derived_from",
        "depends_on",
        "evidenced_by",
        "used_for",
        "mitigates",
        "causes",
        "blocks",
        "resolves",
        "cites",
        "implements",
        "tests",
        "owns",
    ]
    relation_text = ", ".join(f"{name}: [[Target {i}]]" for i, name in enumerate(relation_names))
    markdown = f"## Finding\n- relations: {relation_text}\n\nFinding body.\n"

    document = semantic_blocks.parse_semantic_blocks(markdown)

    assert document.is_valid
    assert [relation.kind for relation in document.blocks[0].relations] == relation_names
    assert {relation.kind for relation in document.blocks[0].relations} == semantic_blocks.RELATION_TYPES


def test_relation_split_ignores_commas_inside_wikilinks() -> None:
    markdown = "## Evidence\n- relations: cites: [[Source, With Comma]], supports: [[Claim]]\n\nBody.\n"

    document = semantic_blocks.parse_semantic_blocks(markdown)

    assert document.is_valid
    assert [relation.target for relation in document.blocks[0].relations] == [
        "[[Source, With Comma]]",
        "[[Claim]]",
    ]


def test_invalid_relation_name_reports_error() -> None:
    markdown = "## Claim\n- relations: agrees_with: [[A]]\n\nBody.\n"

    document = semantic_blocks.parse_semantic_blocks(markdown)

    assert not document.is_valid
    assert document.errors[0].code == "unsupported_relation"
    assert "agrees_with" in document.errors[0].message
    assert document.blocks[0].relations == []


def test_malformed_relation_reports_error() -> None:
    markdown = "## Claim\n- relations: supports [[A]]\n\nBody.\n"

    document = semantic_blocks.parse_semantic_blocks(markdown)

    assert not document.is_valid
    assert document.errors[0].code == "malformed_relation"
    assert "supports [[A]]" in document.errors[0].message


def test_duplicate_ids_warn_without_blocking_parse() -> None:
    markdown = """\
## Claim
- id: same

A.

## Decision
- id: same

B.
"""

    document = semantic_blocks.parse_semantic_blocks(markdown)

    assert document.is_valid
    assert len(document.blocks) == 2
    assert len(document.warnings) == 1
    assert document.warnings[0].code == "duplicate_id"
    assert document.warnings[0].block_id == "same"


def test_aliases_normalize_to_open_question() -> None:
    markdown = """\
## Open Question

A.

## open-question

B.

## open_question

C.
"""

    document = semantic_blocks.parse_semantic_blocks(markdown)

    assert [block.type for block in document.blocks] == [
        "open_question",
        "open_question",
        "open_question",
    ]


def test_fenced_code_is_ignored() -> None:
    markdown = """\
```markdown
## Claim
- relations: agrees_with: [[A]]
```

## Decision

Use the parser.
"""

    document = semantic_blocks.parse_semantic_blocks(markdown)

    assert document.is_valid
    assert [block.type for block in document.blocks] == ["decision"]
    assert document.blocks[0].body == "Use the parser."


def test_first_block_body_returns_metadata_stripped_body() -> None:
    markdown = "## Claim\n- id: c1\n\nClaim body.\n"

    assert semantic_blocks.first_block_body(markdown, "claim") == "Claim body."


def test_claim_extraction_prefers_semantic_claim_block() -> None:
    body = """\
# T

## Claim
- id: c1

Semantic claim body.

## Conclusion

Legacy conclusion body.
"""

    extracted = claims.extract_claim_text("T", body, page_type="research-note")

    assert extracted == "T\n\nSemantic claim body."
    assert "id: c1" not in extracted
    assert "Legacy" not in extracted


def test_context_pack_includes_semantic_blocks_additively(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    rel = "Knowledge Base/Notes/Semantic.md"
    path = vault / rel
    path.parent.mkdir(parents=True)
    path.write_text(
        """\
---
type: insight
---
# Semantic

## Claim
- id: c1
- relations: evidenced_by: [[Knowledge Base/Sources/Session]]

Semantic claim.

## Risk

The implementation could grow a DSL.
""",
        encoding="utf-8",
    )
    find_module.clear_cache()

    pack = context_pack.assemble_pack(
        vault, [Hit(path=rel, type=None, scope=None, title="", updated="", excerpt="")]
    )

    assert set(pack) >= {
        "packed_paths",
        "claims",
        "semantic_blocks",
        "neighborhood",
        "contradictions",
        "embeddings_available",
        "truncation",
    }
    blocks = pack["semantic_blocks"][rel]
    assert [block["type"] for block in blocks] == ["claim", "risk"]
    assert blocks[0]["id"] == "c1"
    assert blocks[0]["relations"][0]["kind"] == "evidenced_by"
    assert pack["claims"][rel]["title"] == "Semantic"
