"""Dimensions that slice 5a of `make-epistemic-language-dynamic` adds: the registry, membership and
the `unit.dimension` retrieval filter.

This module began as the before-picture, in the style of `test_epistemic_belief_keys`: every
`# CORE` value never changes, and a `# FLIPS slice5a` value is one that the slice edited on
purpose. The comment on each flipped line names the value it held before.
"""

from __future__ import annotations

import pytest

from exomem import semantic_language_registry as language_registry
from exomem import structured_filters


def _proposal(**sections) -> dict:
    return {"schema_version": 1, "categories": {}, "kinds": {}, **sections}


def test_a_dimensions_section_in_the_vault_registry() -> None:
    registry = language_registry.load_registry(
        proposal=_proposal(
            dimensions={
                "regulation": {
                    "labels": {"en": "Regulation"},
                    "description": "What the rules require",
                    "members": {"kinds": ["constraint", "requirement"]},
                },
            }
        )
    )

    codes = [(item["code"], item["path"]) for item in registry.findings]
    assert codes == [("unknown_field", "dimensions")]  # FLIPS slice5a: was [("unknown_field", "dimensions")]
    assert [item["severity"] for item in registry.findings] == ["error"]  # FLIPS slice5a: was ["error"]


def test_the_filter_field_unit_dimension() -> None:
    with pytest.raises(structured_filters.FilterError) as raised:
        structured_filters.compile_filter({"unit.dimension": {"$eq": "currency"}})

    assert raised.value.code == "INVALID_FILTER_FIELD"  # FLIPS slice5a: was "INVALID_FILTER_FIELD"
