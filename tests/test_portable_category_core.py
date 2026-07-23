"""RED tests for the portable, code-owned core category vocabulary.

These pin decision 1 of ``teach-portable-category-core`` and the first two
requirements of the ``portable-category-authoring`` spec: an immutable
``core_categories`` ring beside ``core_kinds``, the exact built-in alias table,
open (never fuzzily coerced) unknown labels, and non-fatal legacy collision
shadowing. They exercise the public ``core_registry`` / ``load_registry`` /
``resolve_category`` / ``registry_proposal`` surfaces only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import semantic_language_registry as language_registry

# Decision 1: the sixteen immutable core category keys.
CORE_CATEGORY_KEYS = frozenset(
    {
        "decision",
        "fact",
        "finding",
        "insight",
        "constraint",
        "requirement",
        "assumption",
        "risk",
        "problem",
        "question",
        "action",
        "technique",
        "preference",
        "code",
        "design",
        "config",
    }
)

# Decision 1: the exact built-in alias table (normalized alias -> canonical key).
BUILTIN_CATEGORY_ALIASES = {
    "decisions": "decision",
    "facts": "fact",
    "findings": "finding",
    "insights": "insight",
    "constraints": "constraint",
    "requirements": "requirement",
    "assumptions": "assumption",
    "risks": "risk",
    "problems": "problem",
    "questions": "question",
    "open_question": "question",
    "open_questions": "question",
    "actions": "action",
    "techniques": "technique",
    "preferences": "preference",
    "designs": "design",
    "configs": "config",
    "configuration": "config",
    "configurations": "config",
}

# Raw spellings that must funnel through existing normalization to an alias.
NORMALIZED_ALIAS_SPELLINGS = {
    "Decisions": "decision",
    "CONSTRAINTS": "constraint",
    "  requirements  ": "requirement",
    "Open Question": "question",
    "open-questions": "question",
    "Configuration": "config",
    "CONFIGURATIONS": "config",
}

# Well-formed labels that are deliberately outside the exact table. ``codes`` and
# ``observation`` are the sharp cases: the table gives ``code`` no plural alias,
# and ``observation`` is a core *kind*, never a core *category*.
UNREGISTERED_LABELS = (
    "desicion",
    "requirment",
    "guideline",
    "rationale",
    "observation",
    "codes",
)


def _proposal(*, categories=None, kinds=None):
    return {
        "schema_version": 1,
        "categories": categories or {},
        "kinds": kinds or {},
    }


def test_core_categories_expose_the_exact_immutable_ring(tmp_path: Path) -> None:
    registry = language_registry.load_registry(tmp_path)

    assert set(registry.core_categories) == set(CORE_CATEGORY_KEYS)
    assert len(registry.core_categories) == 16
    # The vocabulary is available before any extension registry file exists.
    assert not language_registry.registry_path(tmp_path).exists()

    for key in sorted(CORE_CATEGORY_KEYS):
        resolution = registry.resolve_category(key)
        assert resolution.key == key
        assert resolution.resolved == key
        assert resolution.status == "core"
        assert resolution.definition is not None
        assert resolution.findings == ()

    with pytest.raises(TypeError):
        registry.core_categories["new"] = registry.core_categories["config"]  # type: ignore[index]


def test_builtin_category_aliases_resolve_to_canonical_core_keys() -> None:
    registry = language_registry.core_registry()

    for alias, canonical in BUILTIN_CATEGORY_ALIASES.items():
        assert language_registry.normalize_label(alias) == alias
        resolution = registry.resolve_category(alias)
        assert resolution.key == alias
        assert resolution.resolved == canonical
        assert resolution.status == "alias"
        assert resolution.definition is not None
        assert resolution.findings == ()


def test_builtin_category_aliases_apply_existing_normalization() -> None:
    registry = language_registry.core_registry()

    for raw, canonical in NORMALIZED_ALIAS_SPELLINGS.items():
        resolution = registry.resolve_category(raw)
        assert resolution.key == language_registry.normalize_label(raw)
        assert resolution.resolved == canonical
        assert resolution.status == "alias"


def test_unknown_valid_labels_stay_unregistered_and_are_never_fuzzy_coerced() -> None:
    registry = language_registry.core_registry()

    for raw in UNREGISTERED_LABELS:
        normalized = language_registry.normalize_label(raw)
        assert normalized not in CORE_CATEGORY_KEYS
        assert normalized not in BUILTIN_CATEGORY_ALIASES
        resolution = registry.resolve_category(raw)
        assert resolution.key == normalized
        assert resolution.resolved == normalized
        assert resolution.status == "unregistered"
        assert resolution.definition is None
        assert resolution.findings == ()


def test_legacy_core_collision_is_preserved_shadowed_and_non_fatal() -> None:
    proposal = _proposal(
        categories={
            # Collides with a reserved core key; its alias collides with a core alias.
            "config": {
                "description": "Vault settings vocabulary",
                "aliases": ["configuration"],
            },
            # Unrelated extension category that must keep resolving normally.
            "domain_glossary": {"description": "A vault-specific domain glossary"},
        }
    )
    registry = language_registry.load_registry(proposal=proposal)

    # Core resolution wins for both the shadowed key and its shadowed alias.
    shadowed = registry.resolve_category("config")
    assert shadowed.resolved == "config"
    assert shadowed.status == "core"
    aliased = registry.resolve_category("configuration")
    assert aliased.resolved == "config"
    assert aliased.status == "alias"

    # The collision is advisory: a non-fatal warning, never an error-severity finding.
    shadow_findings = [
        finding
        for finding in registry.findings
        if finding["code"] == "core_category_shadowed"
    ]
    assert shadow_findings
    assert all(finding["severity"] == "warning" for finding in shadow_findings)
    assert all(finding["severity"] != "error" for finding in registry.findings)

    # The shadowed extension entry survives in extension-only serialization.
    serialized = language_registry.registry_proposal(registry)["categories"]
    assert serialized["config"] == proposal["categories"]["config"]

    # An unrelated extension still resolves; a warning must not force registry_invalid.
    unrelated = registry.resolve_category("domain_glossary")
    assert unrelated.resolved == "domain_glossary"
    assert unrelated.status == "extension"
    assert unrelated.definition is not None


def test_legacy_deprecation_may_replace_with_a_new_core_category() -> None:
    registry = language_registry.load_registry(
        proposal=_proposal(
            categories={
                "legacy_configuration": {
                    "description": "Retired extension label",
                    "status": "deprecated",
                    "replaced_by": "config",
                }
            }
        )
    )

    assert not [finding for finding in registry.findings if finding["severity"] == "error"]
    resolution = registry.resolve_category("legacy_configuration")
    assert resolution.status == "deprecated"
    assert resolution.replacement == "config"
    assert registry.resolve_category(resolution.replacement).status == "core"
