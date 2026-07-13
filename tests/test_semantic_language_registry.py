from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from exomem import semantic_language_registry as language_registry


def _proposal(*, categories=None, kinds=None):
    return {
        "schema_version": 1,
        "categories": categories or {},
        "kinds": kinds or {},
    }


def test_missing_file_and_empty_proposal_preserve_portable_defaults(tmp_path: Path) -> None:
    registry = language_registry.load_registry(tmp_path)

    assert registry == language_registry.core_registry()
    assert registry.findings == ()
    assert language_registry.empty_proposal() == _proposal()
    assert language_registry.registry_path(tmp_path) == (
        tmp_path / "Knowledge Base/_Schema/semantic-language-registry.yaml"
    )
    assert registry.resolve_kind("Findings").resolved == "finding"
    assert registry.resolve_kind("observation").resolved == "observation"
    assert registry.resolve_heading("observation").resolved is None


def test_unknown_categories_are_open_and_preserve_raw_key_and_resolution() -> None:
    resolution = language_registry.core_registry().resolve_category("Äri-Reegel")

    assert (resolution.raw, resolution.key, resolution.resolved) == (
        "Äri-Reegel",
        "äri_reegel",
        "äri_reegel",
    )
    assert resolution.status == "unregistered"
    assert resolution.findings == ()


def test_category_alias_deprecation_replacement_and_scope_are_explicit() -> None:
    registry = language_registry.load_registry(
        proposal=_proposal(
            categories={
                "config": {
                    "description": "Configuration facts",
                    "aliases": ["configuration"],
                    "scope": {"projects": ["alpha"], "page_types": ["note"]},
                },
                "legacy_config": {
                    "description": "Retired configuration facts",
                    "status": "deprecated",
                    "replaced_by": "config",
                },
            }
        )
    )

    alias = registry.resolve_category(
        "Configuration", project="alpha", page_type="note"
    )
    deprecated = registry.resolve_category("legacy-config")
    out_of_scope = registry.resolve_category(
        "configuration", project="beta", page_type="note"
    )

    assert (alias.key, alias.resolved, alias.status) == (
        "configuration",
        "config",
        "alias",
    )
    assert (deprecated.resolved, deprecated.status, deprecated.replacement) == (
        "legacy_config",
        "deprecated",
        "config",
    )
    assert (out_of_scope.resolved, out_of_scope.status) == (
        "configuration",
        "scope_violation",
    )
    assert [finding["code"] for finding in out_of_scope.findings] == [
        "scope_violation"
    ]


def test_custom_rich_kind_is_scoped_and_namespaces_are_distinct() -> None:
    registry = language_registry.load_registry(
        proposal=_proposal(
            categories={"decision": {"description": "A domain category"}},
            kinds={
                "protocol": {
                    "description": "A repeatable protocol",
                    "aliases": ["playbook"],
                    "heading_aliases": ["Protocols"],
                    "scope": {"projects": ["alpha"]},
                }
            },
        )
    )

    assert registry.findings == ()
    assert registry.resolve_category("decision").definition is not None
    assert registry.resolve_kind("decision").status == "core"
    assert registry.resolve_kind("playbook", project="alpha").resolved == "protocol"
    assert registry.resolve_heading("Protocols", project="alpha").resolved == "protocol"
    scoped = registry.resolve_heading("Protocol", project="beta")
    assert scoped.resolved is None
    assert scoped.status == "scope_violation"
    assert registry.resolve_heading("Background", project="alpha").resolved is None


def test_registry_rejects_invalid_root_definition_status_scope_and_replacement() -> None:
    proposal = {
        "schema_version": 2,
        "extra": True,
        "categories": {
            "Bad/Key": "not an object",
            "missing": {"description": ""},
            "bad_status": {"description": "x", "status": "retired"},
            "bad_scope": {
                "description": "x",
                "scope": {"projects": "alpha", "other": []},
            },
            "old": {
                "description": "old",
                "status": "deprecated",
                "replaced_by": "missing_target",
            },
        },
        "kinds": [],
    }

    first = language_registry.load_registry(proposal=proposal)
    second = language_registry.load_registry(proposal=proposal)

    codes = {finding["code"] for finding in first.findings}
    assert {
        "unknown_field",
        "invalid_version",
        "invalid_key",
        "invalid_definition",
        "missing_description",
        "invalid_status",
        "invalid_scope",
        "invalid_list",
        "invalid_replacement",
        "invalid_kinds",
    } <= codes
    assert first.findings == tuple(
        sorted(first.findings, key=lambda item: (item["path"], item["code"], item["detail"]))
    )
    assert first.as_dict() == second.as_dict()


def test_replacement_cycles_deprecated_targets_and_cross_namespace_are_rejected() -> None:
    registry = language_registry.load_registry(
        proposal=_proposal(
            categories={
                "a": {
                    "description": "a",
                    "status": "deprecated",
                    "replaced_by": "b",
                },
                "b": {
                    "description": "b",
                    "status": "deprecated",
                    "replaced_by": "a",
                },
                "cross": {
                    "description": "cross",
                    "status": "deprecated",
                    "replaced_by": "protocol",
                },
            },
            kinds={"protocol": {"description": "protocol"}},
        )
    )

    codes = [finding["code"] for finding in registry.findings]
    assert "replacement_cycle" in codes
    assert codes.count("invalid_replacement") >= 3


def test_alias_and_builtin_collisions_fail_closed_without_choosing_a_target() -> None:
    registry = language_registry.load_registry(
        proposal=_proposal(
            categories={
                "config": {"description": "config", "aliases": ["shared", "rule"]},
                "rule": {"description": "rule", "aliases": ["shared"]},
            },
            kinds={
                "claim": {"description": "collides with built-in"},
                "protocol": {
                    "description": "protocol",
                    "aliases": ["findings"],
                    "heading_aliases": ["claims"],
                },
            },
        )
    )

    codes = [finding["code"] for finding in registry.findings]
    assert "canonical_collision" in codes
    assert "alias_collision" in codes
    assert "alias_conflict" in codes
    resolution = registry.resolve_category("shared")
    assert (resolution.resolved, resolution.status) == ("shared", "registry_invalid")
    assert resolution.findings == registry.findings


def test_replacements_must_use_canonical_targets_not_aliases() -> None:
    registry = language_registry.load_registry(
        proposal=_proposal(
            categories={
                "current": {"description": "current", "aliases": ["newer"]},
                "old": {
                    "description": "old",
                    "status": "deprecated",
                    "replaced_by": "newer",
                },
            }
        )
    )

    assert any(finding["code"] == "invalid_replacement" for finding in registry.findings)


def test_registry_values_and_serialization_are_frozen_and_deterministic(tmp_path: Path) -> None:
    proposal = _proposal(
        categories={
            "config": {
                "description": "Configuration",
                "aliases": ["settings", "configuration"],
            }
        },
        kinds={"protocol": {"description": "Protocol", "heading_aliases": ["Protocols"]}},
    )
    path = language_registry.registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(proposal, sort_keys=False), encoding="utf-8")

    first = language_registry.load_registry(tmp_path)
    second = language_registry.load_registry(tmp_path)
    proposed = language_registry.load_registry(proposal=proposal)

    assert first is second
    assert json.dumps(first.as_dict(), ensure_ascii=False, sort_keys=True) == json.dumps(
        proposed.as_dict(), ensure_ascii=False, sort_keys=True
    )
    with pytest.raises(FrozenInstanceError):
        first.schema_version = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.categories["new"] = first.categories["config"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        first.categories["config"].description = "changed"  # type: ignore[misc]


def test_invalid_registry_findings_are_deeply_immutable() -> None:
    registry = language_registry.load_registry(
        proposal={"schema_version": 2, "categories": {}, "kinds": {}}
    )
    finding = registry.findings[0]

    with pytest.raises(TypeError):
        finding["severity"] = "warning"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        finding.severity = "warning"  # type: ignore[misc]
    assert registry.findings[0]["severity"] == "error"
    assert registry.resolve_category("config").findings[0]["severity"] == "error"


def test_non_string_definition_scalars_fail_closed_with_stable_type_findings() -> None:
    registry = language_registry.load_registry(
        proposal=_proposal(
            kinds={
                "protocol": {
                    "description": 42,
                    "status": ["active"],
                    "replaced_by": {"kind": "workflow"},
                }
            }
        )
    )

    assert [
        (finding["code"], finding["path"]) for finding in registry.findings
    ] == [
        ("invalid_type", "kinds.protocol.description"),
        ("invalid_type", "kinds.protocol.replaced_by"),
        ("invalid_type", "kinds.protocol.status"),
    ]
    resolution = registry.resolve_heading("Protocol")
    assert resolution.resolved is None
    assert resolution.status == "registry_invalid"


@pytest.mark.parametrize(
    "proposal",
    [
        None,
        [],
        {"schema_version": True, "categories": {}, "kinds": {}},
        {
            1: "bad",
            "extra": "bad",
            "schema_version": 1,
            "categories": {},
            "kinds": {},
        },
        {"schema_version": 1, "categories": [], "kinds": {}},
        {"schema_version": 1, "categories": {}, "kinds": "bad"},
    ],
)
def test_validate_proposal_returns_stable_findings_for_invalid_shapes(proposal) -> None:
    findings = language_registry.validate_proposal(proposal)

    assert findings
    assert findings == sorted(
        findings, key=lambda item: (item["path"], item["code"], item["detail"])
    )
