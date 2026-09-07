from dataclasses import FrozenInstanceError, replace

import pytest

from exomem import vocabulary_workflow as workflow


def item(**overrides):
    values = {
        "family": "relation-type/v1",
        "signal": "generic-pair",
        "targets": {"entity:a": "a1", "entity:b": "b1"},
        "evidence": (workflow.Evidence("source:1", "content1", "origin:1"),),
        "registry_hashes": {"relations": "registry1"},
        "projection_status": "current",
    }
    return workflow.make_item(**(values | overrides))


def decision(current, outcome="generic", **overrides):
    choice = None
    if outcome in {"reuse", "enrich"}:
        choice = {"canonical": "part_of"}
    elif outcome == "propose-new":
        choice = {
            "canonical": "vault.supplies",
            "definition": {
                "parent": "relates_to",
                "description": "An organization supplies goods to another.",
                "direction": "directed",
            },
        }
    return {
        "item_ref": current.ref,
        "fingerprint": current.fingerprint,
        "family": current.family,
        "choice": choice,
        "registry_hashes": dict(current.registry_hashes),
        "target_versions": dict(current.target_versions),
        "outcome": outcome,
        "rationale": "The evidence supports only a general connection.",
    } | overrides


def test_product_families_are_distinct_and_immutable():
    assert set(workflow.FAMILIES) == {"entity-instance/v1", "entity-type/v1", "relation-type/v1"}
    relation = workflow.family_descriptor("relation-type/v1")
    assert {"generic", "no-edge", "defer"} <= relation.allowed_decisions
    assert "enrich" not in relation.allowed_decisions
    assert "enrich" in workflow.family_descriptor("entity-instance/v1").allowed_decisions
    assert relation.validator and relation.persistence_owner and relation.evidence_contract
    assert relation.resolution_contract and relation.compatibility
    with pytest.raises((FrozenInstanceError, AttributeError)):
        relation.identifier = "changed/v1"
    with pytest.raises(TypeError):
        workflow.FAMILIES["foreign/v1"] = relation


def test_initial_additive_actions_exclude_existing_entity_enrichment():
    assert set().union(*(family.authority_actions for family in workflow.FAMILIES.values())) == {
        "entity.create",
        "entity_type.add",
        "relation_type.add",
        "edge.add",
    }


def test_decision_persists_the_canonical_meaning_without_an_executable_payload():
    current = item()
    raw = decision(current, "propose-new")
    result = workflow.validate_decision(current, raw)
    assert result.to_dict()["family"] == current.family
    assert result.to_dict()["choice"]["canonical"] == "vault.supplies"
    raw["choice"]["definition"]["description"] = "changed after validation"
    assert result.to_dict()["choice"]["definition"]["description"] != "changed after validation"


def test_rejected_type_choice_reports_the_canonical_validator_findings():
    current = item(family="entity-type/v1")
    choice = {
        "canonical": "programme",
        "definition": {
            "folder": "Programmes",
            "label": "Programme",
            "aliases": ["programmes"],
            "capture_guidance": "Capture the recurring activity and its lifecycle.",
        },
    }
    with pytest.raises(ValueError) as rejected:
        workflow.validate_decision(current, decision(current, "propose-new", choice=choice))
    assert "entity_types.programme.aliases" in str(rejected.value)
    assert "alias 'programmes' collides" in str(rejected.value)


@pytest.mark.parametrize("reference", [{"bad": 1}, "not a canonical relation"])
def test_pure_choice_does_not_defer_malformed_references(reference):
    current = item()
    selected = decision(current, "propose-new")
    selected["choice"]["definition"]["inverse"] = reference
    with pytest.raises(ValueError, match="extensions.vault.supplies.inverse"):
        workflow.validate_decision(current, selected)


@pytest.mark.parametrize(
    "family,entry_path",
    [
        ("entity-type/v1", "entity_types[choice.canonical]"),
        ("relation-type/v1", "extensions[choice.canonical]"),
    ],
)
def test_type_choice_contract_names_the_selected_entry_in_the_proposal(family, entry_path):
    definition = workflow.choice_contract(family)["propose-new"]["definition"]
    assert definition["proposal_entry"] == entry_path
    assert definition["include_proposal_wrapper"] is False


@pytest.mark.parametrize(
    "family,canonical,definition",
    [
        (
            "relation-type/v1",
            "vault.supplies",
            {
                "parent": "relates_to",
                "description": "Supplies goods to another organization.",
                "direction": "directed",
            },
        ),
        (
            "entity-type/v1",
            "school",
            {
                "folder": "Schools",
                "label": "School",
                "aliases": ["academy"],
                "capture_guidance": "Capture the institution and its educational purpose.",
                "status": "active",
            },
        ),
        (
            "entity-instance/v1",
            "Example institution",
            {
                "entity_type": "organization",
                "name": "Example institution",
                "summary": "An institution providing technical education.",
            },
        ),
    ],
)
def test_each_family_accepts_its_valid_proposal(family, canonical, definition):
    current = item(family=family)
    choice = {"canonical": canonical, "definition": definition}
    validated = workflow.validate_decision(current, decision(current, "propose-new", choice=choice))
    assert validated.to_dict()["choice"] == choice


@pytest.mark.parametrize(
    ("definition", "message"),
    [
        (
            {
                "entity_type": "organization",
                "name": "Example institution",
                "summary": "An institution providing technical education.",
                "extra": "not admitted",
            },
            "unsupported fields",
        ),
        (
            {
                "entity_type": "organization",
                "name": "Example institution",
            },
            "missing required fields",
        ),
        (
            {
                "entity_type": "organization",
                "name": "Different institution",
                "summary": "An institution providing technical education.",
            },
            "canonical choice must equal entity proposal name",
        ),
        (
            {
                "entity_type": "Organization",
                "name": "Example institution",
                "summary": "An institution providing technical education.",
            },
            "entity_type must be a lowercase canonical type",
        ),
    ],
)
def test_entity_instance_proposal_errors_name_the_invalid_contract(definition, message):
    current = item(family="entity-instance/v1")
    choice = {"canonical": "Example institution", "definition": definition}

    with pytest.raises(ValueError, match=message):
        workflow.validate_decision(current, decision(current, "propose-new", choice=choice))


@pytest.mark.parametrize(
    "choice",
    [
        None,
        {"canonical": "vault.supplies"},
        {"canonical": "vault.supplies", "definition": {"validator": "os.system"}},
        {
            "canonical": "vault.supplies",
            "definition": {
                "parent": "unknown",
                "description": "a distinction",
                "direction": "directed",
            },
        },
    ],
)
def test_new_meaning_requires_its_existing_family_validator(choice):
    current = item()
    with pytest.raises(ValueError, match="VOCABULARY_DECISION_INVALID"):
        workflow.validate_decision(current, decision(current, "propose-new", choice=choice))


def test_unknown_family_cannot_install_a_validator_or_permission():
    with pytest.raises(ValueError, match="VOCABULARY_FAMILY_UNSUPPORTED"):
        item(family="vault.module.execute/v1")
    with pytest.raises(ValueError, match="VOCABULARY_DECISION_INVALID"):
        workflow.validate_decision(item(), decision(item(), validator="os.system"))


def test_identity_and_fingerprint_ignore_order_and_unrelated_registry_currency():
    original = item()
    reordered = item(targets={"entity:b": "b1", "entity:a": "a1"})
    refreshed = item(registry_hashes={"relations": "unrelated-addition"})
    assert original.ref == reordered.ref == refreshed.ref
    assert original.fingerprint == reordered.fingerprint == refreshed.fingerprint
    changed = item(evidence=(workflow.Evidence("source:1", "content2", "origin:1"),))
    assert changed.ref == original.ref
    assert changed.fingerprint != original.fingerprint
    assert item(targets={"entity:a": "a2", "entity:b": "b1"}).fingerprint != original.fingerprint


def test_evidence_order_and_duplicate_copies_do_not_manufacture_new_signals():
    first = workflow.Evidence("source:1", "v1", "origin:1")
    second = workflow.Evidence("source:2", "v2", "origin:2")
    assert (
        item(evidence=(first, second)).fingerprint
        == item(evidence=(second, first, first)).fingerprint
    )


@pytest.mark.parametrize(
    "outcome,state",
    [
        ("generic", "resolved_without_mutation"),
        ("no-edge", "resolved_without_mutation"),
        ("defer", "deferred"),
        ("propose-new", "proposed"),
        ("reuse", "proposed"),
    ],
)
def test_decision_is_consideration_not_semantic_application(outcome, state):
    current = item()
    result = workflow.validate_decision(current, decision(current, outcome))
    assert result.initial_state == state
    assert result.initial_state != "applied"
    assert result.rationale


@pytest.mark.parametrize(
    "field,value",
    [
        ("fingerprint", "stale"),
        ("registry_hashes", {"relations": "stale"}),
        ("target_versions", {"entity:a": "stale", "entity:b": "b1"}),
        ("item_ref", "exomem://review/vocabulary/other"),
    ],
)
def test_stale_decision_refuses_before_any_state_change(field, value):
    current = item()
    with pytest.raises(ValueError, match="VOCABULARY_DECISION_STALE"):
        workflow.validate_decision(current, decision(current, **{field: value}))


@pytest.mark.parametrize(
    "override",
    [
        {"outcome": "enrich"},
        {"outcome": "apply"},
        {"rationale": " "},
        {"execute": {"path": "note.md", "content": "new"}},
        {"confirmed": True},
        {"grant": "all"},
        {"registry_hashes": []},
    ],
)
def test_decision_schema_is_closed_and_cannot_carry_execution(override):
    current = item()
    with pytest.raises(ValueError, match="VOCABULARY_DECISION_INVALID"):
        workflow.validate_decision(current, decision(current, **override))


def test_unavailable_evidence_remains_visible_but_cannot_be_resolved_as_absent():
    current = item(projection_status="unavailable")
    assert current.to_dict()["projection"]["status"] == "unavailable"
    assert (
        workflow.validate_decision(current, decision(current, "defer")).initial_state == "deferred"
    )
    with pytest.raises(ValueError, match="VOCABULARY_EVIDENCE_UNAVAILABLE"):
        workflow.validate_decision(current, decision(current, "no-edge"))


def test_future_product_family_never_inherits_authority():
    future = replace(
        workflow.family_descriptor("relation-type/v1"),
        identifier="future-label/v1",
        authority_actions=frozenset({"future_label.add"}),
    )
    catalog = workflow.FamilyCatalog((*workflow.FAMILIES.values(), future))
    assert catalog.get("future-label/v1") == future
    assert not catalog.covered_actions("future-label/v1", {"relation_type.add"})
    assert catalog.covered_actions("future-label/v1", {"future_label.add"}) == {"future_label.add"}
    with pytest.raises(ValueError, match="VOCABULARY_ACTION_INVALID"):
        catalog.covered_actions("future-label/v1", {"*"})
    # A product catalog is data, not a grant store or dynamic module loader.
    assert not hasattr(catalog, "approve")
    assert not hasattr(catalog, "load_from_vault")


def test_item_snapshots_caller_owned_mappings():
    targets = {"entity:a": "a1"}
    hashes = {"relations": "r1"}
    current = item(targets=targets, registry_hashes=hashes)
    targets["entity:a"] = "changed"
    hashes["relations"] = "changed"
    assert dict(current.target_versions) == {"entity:a": "a1"}
    assert dict(current.registry_hashes) == {"relations": "r1"}
