"""Kind labels in any language, as slice 6 of `make-epistemic-language-dynamic` adds them.

This module began as the before-picture, in the style of `test_epistemic_kind_parity`: every
`# CORE` value never changes, and a `# FLIPS slice6` value is the one that the slice edited on
purpose. The comment on each flipped line names the value it held before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, semantic_language_registry, semantic_units
from exomem.governance.principal import library_scope

FINNISH = {
    "schema_version": 1,
    "categories": {},
    "kinds": {"protocol": {"description": "A repeatable protocol", "labels": {"fi": "Pöytäkirja"}}},
}


def _codes(registry) -> list[str]:
    return sorted(finding["code"] for finding in registry.findings)


def _parse(markdown: str, registry):
    return semantic_units.parse_semantic_units(markdown, language_registry=registry)


def test_a_labels_field_on_a_kind_is_an_unknown_field_today() -> None:
    registry = semantic_language_registry.load_registry(proposal=FINNISH)

    assert _codes(registry) == ["unknown_field"]  # FLIPS slice6: was an unknown `labels` field; now []


def test_a_non_ascii_heading_parses_as_no_kind_today() -> None:
    registry = semantic_language_registry.load_registry(proposal={
        "schema_version": 1, "categories": {},
        "kinds": {"protocol": {"description": "A repeatable protocol"}},
    })

    document = _parse("## Pöytäkirja\n\nFollow the steps.\n", registry)

    assert [unit.kind for unit in document.units] == []  # FLIPS slice6: was [] (no label); now ["protocol"]
    assert [unit.kind for unit in _parse("## Protocol\n\nFollow the steps.\n", registry).units] == [
        "protocol"
    ]  # CORE


def test_an_ascii_heading_resolves_to_its_shipped_kind() -> None:
    document = _parse(
        "## Open Question\n\nWhat ships?\n\n## claims\n\nIt ships.\n",
        semantic_language_registry.core_registry(),
    )

    assert [unit.kind for unit in document.units] == ["open_question", "claim"]  # CORE


def test_a_categories_save_refuses_a_kind_label_today(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "_Schema").mkdir(parents=True)
    current = {
        "schema_version": 1, "categories": {},
        "kinds": {"protocol": {"description": "A repeatable protocol"}},
    }
    created = semantic_language_registry.save_registry(vault, current)

    with library_scope(), pytest.raises(ValueError) as refused:  # FLIPS slice6: was refused; now saves
        commands.op_schema_memory(
            vault, operation="infer", subject="categories", save=True, proposal=FINNISH,
            expected_hash=created["content_hash"],
        )

    assert str(refused.value).startswith("INVALID_SEMANTIC_LANGUAGE_REGISTRY")  # FLIPS slice6: see above
