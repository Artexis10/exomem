from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from exomem import semantic_labels
from exomem import semantic_language_registry as language_registry
from exomem import vault as vault_module
from exomem.semantic_units import parse_semantic_units


def _proposal(*, categories=None, kinds=None, verdicts=None):
    proposal = {
        "schema_version": 1,
        "categories": categories or {},
        "kinds": kinds or {},
    }
    if verdicts is not None:
        proposal["verdicts"] = verdicts
    return proposal


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
                "runtime_config": {
                    "description": "Configuration facts",
                    "aliases": ["runtime_configuration"],
                    "scope": {"projects": ["alpha"], "page_types": ["note"]},
                },
                "legacy_config": {
                    "description": "Retired configuration facts",
                    "status": "deprecated",
                    "replaced_by": "runtime_config",
                },
            }
        )
    )

    alias = registry.resolve_category(
        "Runtime Configuration", project="alpha", page_type="note"
    )
    deprecated = registry.resolve_category("legacy-config")
    out_of_scope = registry.resolve_category(
        "runtime_configuration", project="beta", page_type="note"
    )

    assert (alias.key, alias.resolved, alias.status) == (
        "runtime_configuration",
        "runtime_config",
        "alias",
    )
    assert (deprecated.resolved, deprecated.status, deprecated.replacement) == (
        "legacy_config",
        "deprecated",
        "runtime_config",
    )
    assert (out_of_scope.resolved, out_of_scope.status) == (
        "runtime_configuration",
        "scope_violation",
    )
    assert [finding["code"] for finding in out_of_scope.findings] == [
        "scope_violation"
    ]


def test_custom_rich_kind_is_scoped_and_namespaces_are_distinct() -> None:
    registry = language_registry.load_registry(
        proposal=_proposal(
            categories={"protocol": {"description": "A domain category"}},
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
    assert registry.resolve_category("protocol").definition is not None
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


def test_reviewed_registry_save_round_trips_complete_document_and_hash_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal(
        categories={
            "config": {
                "description": "Sätted",
                "aliases": ["configuration"],
                "scope": {"projects": ["alpha"], "page_types": ["insight"]},
            },
            "legacy_config": {
                "description": "Retired settings",
                "status": "deprecated",
                "replaced_by": "config",
            },
        },
        kinds={
            "protocol": {
                "description": "A repeatable protocol",
                "aliases": ["playbook"],
                "heading_aliases": ["protocols"],
                "scope": {"projects": ["alpha"]},
            }
        },
    )

    created = language_registry.save_registry(tmp_path, proposal)
    path = language_registry.registry_path(tmp_path)
    original = path.read_text(encoding="utf-8")
    loaded = language_registry.load_registry(tmp_path)

    assert created["created"] is True
    assert "Sätted" in original
    assert language_registry.registry_proposal(loaded) == proposal
    assert language_registry.load_registry(
        proposal=language_registry.registry_proposal(loaded)
    ).as_dict() == loaded.as_dict()

    with pytest.raises(ValueError, match="SEMANTIC_LANGUAGE_REGISTRY_EXISTS"):
        language_registry.save_registry(tmp_path, proposal)
    with pytest.raises(ValueError, match="STALE_SEMANTIC_LANGUAGE_REGISTRY"):
        language_registry.save_registry(tmp_path, proposal, expected_hash="stale")
    with pytest.raises(ValueError, match="INVALID_SEMANTIC_LANGUAGE_REGISTRY"):
        language_registry.save_registry(
            tmp_path,
            {**proposal, "schema_version": 2},
            expected_hash=created["content_hash"],
        )
    assert path.read_text(encoding="utf-8") == original

    updated = _proposal(
        categories={
            **proposal["categories"],
            "rule": {"description": "A durable rule"},
        },
        kinds=proposal["kinds"],
    )
    atomic_writer = vault_module.batch_atomic_write

    def fail_atomic_write(*args, **kwargs):
        raise OSError("simulated atomic write failure")

    monkeypatch.setattr(vault_module, "batch_atomic_write", fail_atomic_write)
    with pytest.raises(OSError, match="simulated atomic write failure"):
        language_registry.save_registry(
            tmp_path, updated, expected_hash=created["content_hash"]
        )
    assert path.read_text(encoding="utf-8") == original
    monkeypatch.setattr(vault_module, "batch_atomic_write", atomic_writer)

    overwritten = language_registry.save_registry(
        tmp_path, updated, expected_hash=created["content_hash"]
    )
    assert overwritten["created"] is False
    assert overwritten["previous_hash"] == created["content_hash"]
    assert language_registry.registry_proposal(
        language_registry.load_registry(tmp_path)
    ) == updated


def test_registry_alias_changes_resolution_without_changing_authored_identity() -> None:
    markdown = "- [configuration] Session lifetime is 30 days\n"
    parent_ref = "exomem://memory/00000000-0000-4000-8000-000000000001"
    before = parse_semantic_units(markdown, parent_ref=parent_ref).units[0]
    registry = language_registry.load_registry(
        proposal=_proposal(
            categories={
                "config": {
                    "description": "Configuration facts",
                    "aliases": ["configuration"],
                }
            }
        )
    )
    after = parse_semantic_units(
        markdown,
        parent_ref=parent_ref,
        language_registry=registry,
    ).units[0]

    assert after.category == "config"
    assert (after.category_raw, after.category_key) == (
        before.category_raw,
        before.category_key,
    )
    assert (after.fingerprint, after.unit_ref) == (before.fingerprint, before.unit_ref)


def test_expected_hash_refuses_recreating_a_missing_registry(tmp_path: Path) -> None:
    path = language_registry.registry_path(tmp_path)

    with pytest.raises(ValueError, match="SEMANTIC_LANGUAGE_REGISTRY_MISSING"):
        language_registry.save_registry(
            tmp_path,
            _proposal(categories={"config": {"description": "Configuration"}}),
            expected_hash="deleted-registry-hash",
        )

    assert not path.exists()


def test_the_shipped_traits_sit_on_the_kinds_the_replaced_kind_sets_named() -> None:
    registry = language_registry.core_registry()

    assert {
        trait: registry.kinds_with(trait)
        for trait in ("assertion", "page_claim", "procedure", "outcome", "question")
    } == {
        "assertion": {"claim", "finding", "inference", "hypothesis", "result"},
        "page_claim": {"claim"},
        "procedure": {"procedure"},
        "outcome": {"result"},
        "question": {"open_question"},
    }
    assert registry.traits_of("result") == {"assertion", "outcome"}
    assert registry.traits_of("decision") == frozenset()
    assert registry.traits_of("not_a_kind") == frozenset()


def test_a_trait_outside_the_closed_set_raises_instead_of_reading_false() -> None:
    registry = language_registry.core_registry()

    with pytest.raises(ValueError, match="not a kind trait"):
        registry.kinds_with("assertive")
    with pytest.raises(ValueError, match="not a kind trait"):
        registry.has("claim", "assertive")


def test_a_vault_kind_declares_traits_and_a_vault_cannot_change_a_core_kind() -> None:
    registry = language_registry.load_registry(
        proposal=_proposal(
            kinds={
                "protocol": {"description": "A repeatable protocol", "traits": ["procedure"]},
                "claim": {"description": "Shadows the built-in", "traits": []},
            }
        )
    )

    assert registry.has("protocol", "procedure")
    assert registry.kinds_with("procedure") == {"procedure", "protocol"}
    assert registry.kinds["protocol"].as_dict()["traits"] == ["procedure"]
    assert registry.traits_of("claim") == {"assertion", "page_claim"}
    assert [item["code"] for item in registry.findings] == ["canonical_collision"]


def test_an_unknown_trait_is_refused_and_names_the_accepted_traits() -> None:
    findings = language_registry.validate_proposal(
        _proposal(kinds={"protocol": {"description": "A protocol", "traits": ["procedural"]}})
    )

    assert [item["code"] for item in findings] == ["unknown_trait"]
    assert findings[0]["path"] == "kinds.protocol.traits"
    for accepted in ("assertion", "page_claim", "procedure", "outcome", "question"):
        assert accepted in findings[0]["detail"]


def test_a_saved_vault_kind_keeps_its_traits(tmp_path: Path) -> None:
    proposal = _proposal(
        kinds={"protocol": {"description": "A repeatable protocol", "traits": ["procedure"]}}
    )

    language_registry.save_registry(tmp_path, proposal)

    reloaded = language_registry.load_registry(tmp_path)
    assert reloaded.findings == ()
    assert reloaded.traits_of("protocol") == {"procedure"}
    assert language_registry.registry_proposal(reloaded) == proposal


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def test_the_shipped_verdicts_resolve_by_key_and_label_without_a_vault_entry() -> None:
    registry = language_registry.core_registry()

    assert registry.label_keys(semantic_labels.VERDICTS) == (
        "abandoned", "confirmed", "inconclusive", "qualified", "refuted",
    )
    assert registry.resolve_label(semantic_labels.VERDICTS, "  Refuted ") == "refuted"
    assert registry.resolve_label(semantic_labels.VERDICTS, "probably-wrong") is None
    assert registry.resolve_label(semantic_labels.VERDICTS, "0.7") is None


def test_a_vault_adds_a_verdict_and_labels_a_shipped_one_in_another_language() -> None:
    registry = language_registry.load_registry(
        proposal=_proposal(
            verdicts={
                "retracted": {
                    "labels": {"en": "Retracted by authors", "pt-br": "Retirado pelos autores"},
                    "description": "Withdrawn",
                },
                "refuted": {"labels": {"fi": "Kumottu"}},
            }
        )
    )

    assert registry.findings == ()
    assert "retracted" in registry.label_keys(semantic_labels.VERDICTS)
    assert registry.resolve_label(semantic_labels.VERDICTS, "retracted by AUTHORS") == "retracted"
    assert registry.resolve_label(semantic_labels.VERDICTS, "Retirado pelos autores") == "retracted"
    assert registry.resolve_label(semantic_labels.VERDICTS, "kumottu") == "refuted"
    # The vault's Finnish label sits beside the shipped English one.
    assert registry.label_definitions(semantic_labels.VERDICTS)["refuted"].labels == (
        ("en", "Refuted"), ("fi", "Kumottu"),
    )
    assert registry.label_definitions(semantic_labels.VERDICTS)["refuted"].description == (
        language_registry.core_registry().label_definitions(semantic_labels.VERDICTS)["refuted"].description
    )


@pytest.mark.parametrize(
    ("verdicts", "code"),
    [
        (
            {"retracted": {"labels": {"fi": "Confirmed"}, "description": "Withdrawn"}},
            "verdict_label_collision",
        ),
        (
            {
                "retracted": {"labels": {"en": "Pulled"}, "description": "Withdrawn"},
                "withdrawn": {"labels": {"fi": "PULLED"}, "description": "Gone"},
            },
            "verdict_label_collision",
        ),
        (
            {"retracted": {"labels": {"English": "Retracted"}, "description": "Withdrawn"}},
            "invalid_label",
        ),
        ({"retracted": {"labels": "Retracted", "description": "Withdrawn"}}, "invalid_labels"),
        ({"retracted": {"labels": {"en": "Retracted"}}}, "missing_description"),
        ({"retracted": {"description": "Withdrawn", "behaviour": "closes"}}, "unknown_field"),
        ({"Not A Key": {"description": "Withdrawn"}}, "invalid_key"),
    ],
    ids=[
        "label-is-another-key", "label-is-another-label", "bad-language-tag", "labels-not-a-map",
        "no-description",
        "unknown-field", "bad-key",
    ],
)
def test_a_malformed_verdict_is_refused_and_hides_every_vault_verdict(verdicts, code: str) -> None:
    registry = language_registry.load_registry(proposal=_proposal(verdicts=verdicts))

    assert code in [item["code"] for item in registry.findings]
    # The shipped verdicts always resolve; the vault's own wait for a clean registry.
    assert registry.label_keys(semantic_labels.VERDICTS) == language_registry.core_registry().label_keys(semantic_labels.VERDICTS)


def test_a_vault_entry_that_redescribes_a_shipped_verdict_is_ignored_with_a_warning() -> None:
    proposal = _proposal(
        verdicts={
            "refuted": {"labels": {"fi": "Kumottu"}, "description": "Rewritten"},
            "retracted": {"labels": {"en": "Retracted by authors"}, "description": "Withdrawn"},
        }
    )
    registry = language_registry.load_registry(proposal=proposal)

    assert [(item["code"], item["severity"]) for item in registry.findings] == [
        ("core_verdict_shadowed", "warning")
    ]
    # The rest of the vault's verdicts still resolve; the shadowing entry adds nothing.
    assert registry.resolve_label(semantic_labels.VERDICTS, "Retracted by authors") == "retracted"
    assert registry.resolve_label(semantic_labels.VERDICTS, "Kumottu") is None
    assert registry.label_definitions(semantic_labels.VERDICTS)["refuted"] == (
        language_registry.core_registry().label_definitions(semantic_labels.VERDICTS)["refuted"]
    )
    # A save keeps the entry as the vault wrote it.
    assert language_registry.registry_proposal(registry) == proposal


def test_a_saved_vault_verdict_keeps_its_labels_and_description(tmp_path: Path) -> None:
    proposal = _proposal(
        verdicts={
            "retracted": {"labels": {"en": "Retracted by authors"}, "description": "Withdrawn"},
            "refuted": {"labels": {"fi": "Kumottu"}},
        }
    )

    language_registry.save_registry(tmp_path, proposal)

    reloaded = language_registry.load_registry(tmp_path)
    assert reloaded.findings == ()
    assert reloaded.resolve_label(semantic_labels.VERDICTS, "Retracted by authors") == "retracted"
    assert language_registry.registry_proposal(reloaded) == proposal


# ---------------------------------------------------------------------------
# Belief levels, modalities and inference modes share the verdicts' mechanism
# ---------------------------------------------------------------------------


def test_the_shipped_levels_modalities_and_modes_resolve_by_key_and_label() -> None:
    """Catches a registry whose pack is missing or unread: a unit row could never validate."""
    registry = language_registry.core_registry()

    assert registry.label_keys(semantic_labels.BELIEF_LEVELS) == (
        "likely", "near_certain", "plausible", "speculative",
    )
    assert registry.resolve_label(semantic_labels.BELIEF_LEVELS, "Near-certain") == "near_certain"
    assert registry.resolve_label(semantic_labels.MODALITIES, " SHOULD ") == "should"
    assert registry.resolve_label(semantic_labels.MODALITIES, "likely") is None
    assert (
        registry.resolve_label(semantic_labels.INFERENCE_MODES, "statistical inference")
        == "statistical_inference"
    )


def test_a_vault_extends_each_new_registry_and_a_collision_stays_inside_one_registry() -> None:
    """Catches a vault section that is ignored, or a label check that leaks across registries."""
    registry = language_registry.load_registry(
        proposal={
            **_proposal(),
            "belief_levels": {"remote": {"labels": {"en": "Remote"}, "description": "Barely"}},
            "modalities": {"should": {"labels": {"fi": "Pitäisi"}}},
            # `Likely` is a belief level, not an inference mode, so it collides with nothing here.
            "inference_modes": {"guess": {"labels": {"en": "Likely"}, "description": "A guess"}},
        }
    )

    assert registry.findings == ()
    assert registry.resolve_label(semantic_labels.BELIEF_LEVELS, "remote") == "remote"
    assert registry.resolve_label(semantic_labels.MODALITIES, "pitäisi") == "should"
    assert registry.resolve_label(semantic_labels.INFERENCE_MODES, "Likely") == "guess"

    colliding = language_registry.load_registry(
        proposal={
            **_proposal(),
            "inference_modes": {"proof": {"labels": {"en": "Deduction"}, "description": "Proof"}},
        }
    )
    assert [(item["code"], item["path"]) for item in colliding.findings] == [
        ("inference_mode_label_collision", "inference_modes.proof.labels.en"),
    ]


def test_removing_an_entry_that_only_labels_a_shipped_key_is_no_drop() -> None:
    """Catches a save refused for dropping a Finnish label, when the shipped key still resolves."""
    current = language_registry.load_registry(
        proposal=_proposal(
            verdicts={
                "refuted": {"labels": {"fi": "Kumottu"}},
                "retracted": {"labels": {"en": "Retracted"}, "description": "Withdrawn"},
            }
        )
    )
    candidate = language_registry.load_registry(proposal=_proposal())

    dropped = language_registry.dropped_label_findings(current, candidate)

    assert [finding["code"] for _labels, finding in dropped] == ["verdicts_dropped"]
    assert "retracted" in dropped[0][1]["detail"]
    assert "refuted" not in dropped[0][1]["detail"]


def test_a_language_tag_in_any_case_is_one_language() -> None:
    """Catches `pt-BR` or `zh-Hant` refused as no language tag, as the shape check once did."""
    registry = language_registry.load_registry(
        proposal=_proposal(
            verdicts={"refuted": {"labels": {"pt-BR": "Refutado", "zh-Hant": "已駁斥"}}}
        )
    )

    assert registry.findings == ()
    assert dict(registry.label_definitions(semantic_labels.VERDICTS)["refuted"].labels) == {
        "en": "Refuted", "pt-br": "Refutado", "zh-hant": "已駁斥",
    }
    assert registry.resolve_label(semantic_labels.VERDICTS, "refutado") == "refuted"


def test_an_unquoted_norwegian_tag_is_refused_with_the_quoting_fix() -> None:
    """Catches the YAML trap: `no:` loads as False and once reported a baffling bad tag."""
    proposal = yaml.safe_load(
        "schema_version: 1\ncategories: {}\nkinds: {}\n"
        "verdicts:\n  refuted:\n    labels:\n      no: Tilbakevist\n"
    )

    registry = language_registry.load_registry(proposal=proposal)

    [finding] = registry.findings
    assert finding["code"] == "invalid_label"
    assert "quote the tag" in finding["detail"]
