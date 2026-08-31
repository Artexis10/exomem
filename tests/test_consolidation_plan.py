from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from exomem.governance import consolidation_plan, consolidation_policy, consolidation_review

RUN_ID = "00000000-0000-4000-8000-000000000001"
OPERATION_ID = "00000000-0000-4000-8000-000000000002"
CREATED_AT = "2026-08-28T12:00:00.000Z"
VALID_UNTIL = "2026-08-28T13:00:00.000Z"
RECOVERY_DEADLINE = "2026-08-29T12:00:00.000Z"
NONCE = "plan-00000000000000000001"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _policy_document() -> dict[str, object]:
    content = (
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FA1\n"
        'name: Résumé "review"\n'
        'paths: ["Notes/Private/**"]\n'
    )
    raw = content.encode("utf-8")
    return {
        "path": "scopes/private.yaml",
        "content": content,
        "byte_size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _plan_input(
    policy_bundle: consolidation_policy.DestinationPolicyPlan | None = None,
    *,
    plan_kind: str = "cutover",
) -> dict[str, object]:
    value = {
        "schema": "exomem.consolidation-plan/v1",
        "protocol_version": 1,
        "plan_kind": plan_kind,
        "run_id": RUN_ID,
        "run_mode": "cloned-rehearsal",
        "source_snapshot_fingerprint": _digest("source-snapshot"),
        "destination_snapshot_fingerprint": _digest("destination-snapshot"),
        "expected_destination_preimage_census_digest": _digest("destination-census"),
        "source_inventory_digest": _digest("source-inventory"),
        "reconciliation_digest": _digest("reconciliation"),
        "conflict_decision_digest": _digest("conflict-decisions"),
        "identity_map_digest": _digest("identity-map"),
        "path_map_digest": _digest("path-map"),
        "dependency_map_digest": _digest("dependency-map"),
        "content_actions": [
            {
                "ordinal": 0,
                "batch_ordinal": 0,
                "action": "overwrite",
                "object_ref": "source-object-1",
                "source_path": "Knowledge Base/Notes/source.md",
                "destination_path": "Knowledge Base/Notes/destination.md",
                "expected_before_state": "present",
                "expected_before_sha256": _digest("before"),
                "planned_after_state": "present",
                "planned_after_sha256": _digest("after"),
            }
        ],
        "journal_batch_partition_digest": _digest("batch-partition"),
        "policy_documents": [_policy_document()],
        "policy_bundle_digest": _digest("policy-bundle"),
        "prospective_policy_fingerprint": _digest("prospective-policy"),
        "bridge_fingerprints": [_digest("bridge")],
        "exact_release_approval_fingerprints": [_digest("release")],
        "principal_attestation_set_digest": _digest("principal-attestations"),
        "disclosure_matrix_digest": _digest("disclosure-matrix"),
        "verification_plan": {
            "schema": "exomem.consolidation-verification-plan/v1",
            "positive_probe_digest": _digest("positive-probes"),
            "negative_probe_digest": _digest("negative-probes"),
        },
        "rollback_contingency": {
            "schema": "exomem.consolidation-rollback-contingency/v1",
            "contingency_digest": _digest("rollback-contingency"),
            "applies_to": "nonterminal-apply",
            "recovery_window_deadline": RECOVERY_DEADLINE,
            "future_terminal_plan_authorized": False,
        },
        "source_retention": {
            "schema": "exomem.consolidation-source-retention/v1",
            "state": "required-through-cutover",
            "recovery_window_deadline": RECOVERY_DEADLINE,
            "recovery_window_ttl_ms": 86_400_000,
            "surviving_copy_required": True,
        },
        "plan_successor_automaton_digest": (consolidation_plan.plan_successor_automaton().digest),
        "impact_summary": {
            "schema": "exomem.consolidation-impact-summary/v1",
            "create_count": 0,
            "overwrite_count": 1,
            "removal_count": 0,
            "relocation_count": 0,
            "deduplication_count": 0,
            "provenance_mapping_count": 0,
            "policy_change_count": 1,
            "principal_change_count": 1,
            "disclosure_change_count": 1,
            "batch_count": 1,
            "rollback_consequence_count": 1,
            "surviving_copy_obligation_count": 1,
            "unresolved_count": 0,
            "rollback_consequence": "The prior destination bytes remain recoverable.",
            "surviving_copy_obligation": "Keep one verified source copy through recovery.",
        },
        "created_at": CREATED_AT,
        "valid_until": VALID_UNTIL,
        "nonce": NONCE,
    }
    if policy_bundle is not None:
        value["policy_bundle_digest"] = policy_bundle.digest
        value["prospective_policy_fingerprint"] = policy_bundle.prospective.policy.fingerprint
        value["principal_attestation_set_digest"] = policy_bundle.principal_attestation_set_digest
        value["nonce"] = policy_bundle.nonce
    value["rendering_definition"] = consolidation_plan.derive_rendering_definition(value)
    return value


def _materialization(
    *,
    basis_run_revision: int = 7,
) -> consolidation_plan.PlanMaterializationContext:
    return consolidation_plan.PlanMaterializationContext(
        operation_id=OPERATION_ID,
        basis_run_revision=basis_run_revision,
        predecessor_event_id=_digest("reconcile-terminal"),
        predecessor_payload_digest=_digest("reconcile-payload"),
    )


def _plan(
    *,
    basis_run_revision: int = 7,
    policy_bundle: consolidation_policy.DestinationPolicyPlan | None = None,
    plan_kind: str = "cutover",
    verification_manifest: object | None = None,
) -> consolidation_plan.CanonicalConsolidationPlan:
    plan_input = _plan_input(policy_bundle, plan_kind=plan_kind)
    if verification_manifest is not None:
        verification_plan = verification_manifest.verification_plan
        plan_input["verification_plan"] = {
            "schema": "exomem.consolidation-verification-plan/v1",
            "positive_probe_digest": verification_plan.positive_probe_digest,
            "negative_probe_digest": verification_plan.negative_probe_digest,
        }
        plan_input["rendering_definition"] = consolidation_plan.derive_rendering_definition(
            plan_input
        )
    return consolidation_plan.materialize_plan(
        plan_input,
        materialization=_materialization(basis_run_revision=basis_run_revision),
    )


def _verification_manifest():
    from exomem.governance import consolidation_verification_manifest

    common = {
        "executor_id": "canonical-governance-surface-v1",
        "surface": "rest",
        "principal_kind": "owner",
        "principal_id": "owner",
        "purpose": "consolidation-verification",
        "command_name": "read_memory",
    }
    return consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(
            {
                **common,
                "probe_id": "owner-destination-present",
                "arguments": {"path": "Knowledge Base/Notes/destination.md"},
                "expected_result_digest": _digest("destination-present-wire"),
            },
        ),
        negative_contracts=(
            {
                **common,
                "probe_id": "owner-private-absent",
                "arguments": {"path": "Knowledge Base/Notes/private.md"},
                "expected_result_digest": _digest("private-absent-wire"),
            },
        ),
    )


EXPECTED_TOP_LEVEL_FIELDS = {
    "schema",
    "protocol_version",
    "plan_kind",
    "run_id",
    "run_mode",
    "source_snapshot_fingerprint",
    "destination_snapshot_fingerprint",
    "expected_destination_preimage_census_digest",
    "source_inventory_digest",
    "reconciliation_digest",
    "conflict_decision_digest",
    "identity_map_digest",
    "path_map_digest",
    "dependency_map_digest",
    "content_actions",
    "journal_batch_partition_digest",
    "policy_documents",
    "policy_bundle_digest",
    "prospective_policy_fingerprint",
    "bridge_fingerprints",
    "exact_release_approval_fingerprints",
    "principal_attestation_set_digest",
    "disclosure_matrix_digest",
    "verification_plan",
    "rollback_contingency",
    "source_retention",
    "control_basis_digest",
    "plan_successor_automaton_digest",
    "impact_summary",
    "rendering_definition",
    "created_at",
    "valid_until",
    "nonce",
}

EXPECTED_CONTROL_BASIS_FIELDS = {
    "schema",
    "run_id",
    "plan_kind",
    "plan_materialization_operation_id",
    "basis_run_revision",
    "source_snapshot_fingerprint",
    "destination_snapshot_fingerprint",
    "plan_input_set_digest",
    "plan_nonce",
    "predecessor_event_id",
    "predecessor_payload_digest",
    "plan_successor_automaton_digest",
}


def test_plan_has_one_closed_cross_runtime_canonical_vector() -> None:
    plan = _plan()

    assert set(plan.preimage) == EXPECTED_TOP_LEVEL_FIELDS
    assert set(plan.control_basis.preimage) == EXPECTED_CONTROL_BASIS_FIELDS
    assert plan.digest == "b6ae9ea26b3cd08ba4a96bee788bb7d9b73645aebe605db0d3d516a8d7d2602f"
    assert plan.plan_input_set_digest == (
        "6b4a6d9c7dd1e7517accc478d7da79e7783157b8b002bdeadc09af174d53a201"
    )
    assert plan.control_basis.digest == (
        "d26eaaeecbb6f8162cc72d8a483330c72144be8ca8ab5f8381e01f6bcd3c2726"
    )
    assert plan.impact_summary_digest == (
        "e6ec13c6d5a662678910a04d60d67cc095480b907cb8f30751399977ac6ec46f"
    )
    assert plan.rendering_definition_digest == (
        "aa0c600a14463a3a773ace9fa492cb067daa46ff091e75ea4f676ab435bee7eb"
    )
    assert len(plan.canonical_bytes) == 5268
    assert hashlib.sha256(plan.canonical_bytes).hexdigest() == (
        "6f685f0f0436fdbbc995ff6476c2b7e33b2af91734771e705682d4ca38683529"
    )
    assert len(plan.framed_bytes) == 5308
    assert plan.framed_bytes[:40].hex() == (
        "0000001c65786f6d656d2e636f6e736f6c69646174696f6e2d706c616e2f76310000000000001494"
    )
    assert b"plan_digest" not in plan.canonical_bytes
    assert b"control_basis_digest" in plan.canonical_bytes
    assert hashlib.sha256(plan.framed_bytes).hexdigest() == plan.digest
    assert (
        consolidation_plan.parse_canonical_plan(
            plan.canonical_bytes,
            control_basis=plan.control_basis,
        )
        == plan
    )


def test_plan_successor_automaton_has_one_closed_static_vector() -> None:
    automaton = consolidation_plan.plan_successor_automaton()

    assert set(automaton.preimage) == {
        "schema",
        "initial_state",
        "states",
        "terminal_state",
        "minimum_pages",
        "page_count_source",
        "transitions",
        "retry_rule",
        "unexpected_event_rule",
    }
    assert automaton.digest == ("42c58e050c8e353b35e5ce211d501ec9ae5c1f3ee58d94e00150a5b35125a704")
    assert len(automaton.canonical_bytes) == 1060
    assert hashlib.sha256(automaton.canonical_bytes).hexdigest() == (
        "504af1a82c1ef5c381df23894099aeafca51b55515344d7d431a671865c5cdb3"
    )
    assert [row["ordinal"] for row in automaton.preimage["transitions"]] == list(range(7))
    assert not {
        "run_id",
        "plan_digest",
        "page_digest",
        "control_basis_digest",
        "plan_successor_automaton_digest",
    } & set(automaton.preimage)


def test_closed_jcs_normalizes_unicode_and_uses_utf16_key_order() -> None:
    canonical = consolidation_plan.canonical_closed_jcs(
        {"\ue000": "e\u0301", "😀": 'line\n"quoted"'}
    )

    assert canonical == '{"😀":"line\\n\\"quoted\\"","\ue000":"é"}'.encode()
    with pytest.raises(consolidation_plan.ConsolidationPlanUnavailable):
        consolidation_plan.canonical_closed_jcs({"é": 1, "e\u0301": 2})


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        -1,
        -0.0,
        1.0,
        float("nan"),
        float("inf"),
        (1 << 53),
    ],
)
def test_closed_plan_jcs_rejects_values_outside_the_interoperable_subset(
    invalid: object,
) -> None:
    with pytest.raises(consolidation_plan.ConsolidationPlanUnavailable):
        consolidation_plan.canonical_closed_jcs({"value": invalid})


def test_closed_plan_jcs_accepts_both_integer_boundaries() -> None:
    assert (
        consolidation_plan.canonical_closed_jcs({"maximum": (1 << 53) - 1, "minimum": 0})
        == b'{"maximum":9007199254740991,"minimum":0}'
    )

    value = _plan_input()
    value["impact_summary"]["principal_change_count"] = (1 << 53) - 1
    value["source_retention"]["recovery_window_ttl_ms"] = (1 << 53) - 1
    value["rendering_definition"] = consolidation_plan.derive_rendering_definition(value)
    plan = consolidation_plan.materialize_plan(
        value,
        materialization=consolidation_plan.PlanMaterializationContext(
            operation_id=OPERATION_ID,
            basis_run_revision=(1 << 53) - 1,
            predecessor_event_id=_digest("reconcile-terminal"),
            predecessor_payload_digest=_digest("reconcile-payload"),
        ),
    )

    assert plan.preimage["impact_summary"]["principal_change_count"] == (1 << 53) - 1
    assert plan.control_basis.preimage["basis_run_revision"] == (1 << 53) - 1


@pytest.mark.parametrize(
    "created_at",
    [
        "2026-08-28T12:00:00Z",
        "2026-08-28T12:00:00.000+00:00",
        "2026-08-28T12:00:00.000000Z",
        "2026-02-30T12:00:00.000Z",
    ],
)
def test_plan_timestamps_are_exact_utc_rfc3339_milliseconds(created_at: str) -> None:
    value = _plan_input()
    value["created_at"] = created_at

    with pytest.raises(consolidation_plan.ConsolidationPlanUnavailable):
        consolidation_plan.materialize_plan(value, materialization=_materialization())


@pytest.mark.parametrize(
    "raw_fragment",
    [
        b'"protocol_version":-0',
        b'"protocol_version":-1',
        b'"protocol_version":1.0',
        b'"protocol_version":1e0',
        b'"protocol_version":NaN',
        b'"protocol_version":Infinity',
        b'"protocol_version":null',
    ],
)
def test_raw_plan_parser_rejects_noncanonical_number_and_null_forms(
    raw_fragment: bytes,
) -> None:
    plan = _plan()
    damaged = plan.canonical_bytes.replace(b'"protocol_version":1', raw_fragment)

    with pytest.raises(consolidation_plan.ConsolidationPlanUnavailable):
        consolidation_plan.parse_canonical_plan(
            damaged,
            control_basis=plan.control_basis,
        )


def test_raw_plan_parser_rejects_duplicate_and_post_nfc_duplicate_keys() -> None:
    plan = _plan()
    parsed = json.loads(plan.canonical_bytes)
    without_closing = plan.canonical_bytes[:-1]
    exact_duplicate = without_closing + b',"schema":"exomem.consolidation-plan/v1"}'
    post_nfc_duplicate = b'{"e\\u0301":1,"\\u00e9":2}'

    with pytest.raises(consolidation_plan.ConsolidationPlanUnavailable):
        consolidation_plan.parse_canonical_plan(
            exact_duplicate,
            control_basis=plan.control_basis,
        )
    assert parsed["schema"] == "exomem.consolidation-plan/v1"
    with pytest.raises(consolidation_plan.ConsolidationPlanUnavailable):
        consolidation_plan.parse_canonical_plan(
            post_nfc_duplicate,
            control_basis=plan.control_basis,
        )


def test_paths_must_arrive_in_normalized_portable_form() -> None:
    value = _plan_input()
    value["content_actions"][0]["destination_path"] = "Knowledge Base\\Notes\\x.md"

    with pytest.raises(consolidation_plan.ConsolidationPlanUnavailable):
        consolidation_plan.materialize_plan(value, materialization=_materialization())


Mutation = Callable[[dict[str, object]], None]


def _replace_digest(field: str) -> Mutation:
    def mutate(value: dict[str, object]) -> None:
        value[field] = _digest(f"changed:{field}")

    return mutate


def _mutate_policy(value: dict[str, object]) -> None:
    document = value["policy_documents"][0]
    document["content"] += "description: changed\n"
    raw = document["content"].encode()
    document["byte_size"] = len(raw)
    document["sha256"] = hashlib.sha256(raw).hexdigest()


def _mutate_action(value: dict[str, object]) -> None:
    value["content_actions"][0]["planned_after_sha256"] = _digest("changed-after")


def _mutate_verification(value: dict[str, object]) -> None:
    value["verification_plan"]["negative_probe_digest"] = _digest("changed-negative")


def _mutate_rollback(value: dict[str, object]) -> None:
    value["rollback_contingency"]["contingency_digest"] = _digest("changed-rollback")


def _mutate_retention(value: dict[str, object]) -> None:
    value["source_retention"]["recovery_window_ttl_ms"] = 86_399_999


def _mutate_impact(value: dict[str, object]) -> None:
    value["impact_summary"]["principal_change_count"] = 2


def _mutate_rendering(value: dict[str, object]) -> None:
    value["impact_summary"]["rollback_consequence"] = "Changed trusted rendering consequence."


@pytest.mark.parametrize(
    "mutation",
    [
        _replace_digest("source_snapshot_fingerprint"),
        _replace_digest("destination_snapshot_fingerprint"),
        _replace_digest("expected_destination_preimage_census_digest"),
        _replace_digest("source_inventory_digest"),
        _replace_digest("reconciliation_digest"),
        _replace_digest("conflict_decision_digest"),
        _replace_digest("identity_map_digest"),
        _replace_digest("path_map_digest"),
        _replace_digest("dependency_map_digest"),
        _mutate_action,
        _replace_digest("journal_batch_partition_digest"),
        _mutate_policy,
        _replace_digest("policy_bundle_digest"),
        _replace_digest("prospective_policy_fingerprint"),
        lambda value: value["bridge_fingerprints"].append(_digest("bridge-2")),
        lambda value: value["exact_release_approval_fingerprints"].append(_digest("release-2")),
        _replace_digest("principal_attestation_set_digest"),
        _replace_digest("disclosure_matrix_digest"),
        _mutate_verification,
        _mutate_rollback,
        _mutate_retention,
        _mutate_impact,
        _mutate_rendering,
        lambda value: value.__setitem__("run_mode", "real-cutover"),
        lambda value: value.__setitem__("valid_until", "2026-08-28T12:59:59.999Z"),
        lambda value: value.__setitem__("nonce", "plan-00000000000000000002"),
    ],
)
def test_every_mutable_plan_input_changes_the_plan_digest(mutation: Mutation) -> None:
    original = _plan()
    changed_input = copy.deepcopy(_plan_input())
    mutation(changed_input)
    changed_input["rendering_definition"] = consolidation_plan.derive_rendering_definition(
        changed_input
    )
    changed = consolidation_plan.materialize_plan(
        changed_input,
        materialization=_materialization(),
    )

    assert changed.digest != original.digest
    assert changed.plan_input_set_digest != original.plan_input_set_digest


def test_control_basis_predecessor_changes_the_plan_without_entering_input_set() -> None:
    original = _plan()
    changed_context = consolidation_plan.PlanMaterializationContext(
        operation_id=OPERATION_ID,
        basis_run_revision=7,
        predecessor_event_id=_digest("other-terminal"),
        predecessor_payload_digest=_digest("reconcile-payload"),
    )
    changed = consolidation_plan.materialize_plan(
        _plan_input(),
        materialization=changed_context,
    )

    assert changed.plan_input_set_digest == original.plan_input_set_digest
    assert changed.control_basis.digest != original.control_basis.digest
    assert changed.digest != original.digest


def test_rendering_definition_is_derived_from_the_exact_plan_rows() -> None:
    draft = _plan_input()
    draft.pop("rendering_definition")

    definition = consolidation_plan.derive_rendering_definition(draft)
    draft["rendering_definition"] = definition
    plan = consolidation_plan.materialize_plan(
        draft,
        materialization=_materialization(),
    )

    assert definition["page_size"] == 20
    assert definition["page_count"] == 6
    assert definition["total_rows"] == 7
    assert [section["section_id"] for section in definition["sections"]] == list(
        consolidation_plan.RENDER_SECTION_IDS
    )
    assert consolidation_plan.canonical_closed_jcs(
        plan.preimage["rendering_definition"]
    ) == consolidation_plan.canonical_closed_jcs(definition)
    assert consolidation_plan.render_plan_page(plan, page_ordinal=0).digest == (
        "8bf88aeab633924885b5a995f3d131f5cf15c3af80f6c03d9168a94f475b65cf"
    )

    injected = copy.deepcopy(draft)
    injected["rendering_definition"]["sections"][0]["content_digest"] = _digest("caller-defined")
    with pytest.raises(consolidation_plan.ConsolidationPlanUnavailable):
        consolidation_plan.materialize_plan(
            injected,
            materialization=_materialization(),
        )


def test_rendered_plan_pages_are_bounded_complete_and_digest_stable() -> None:
    draft = _plan_input()
    actions = []
    for ordinal in range(45):
        action = copy.deepcopy(draft["content_actions"][0])
        action["ordinal"] = ordinal
        action["batch_ordinal"] = ordinal // 10
        action["object_ref"] = f"source-object-{ordinal:03d}"
        action["source_path"] = f"Knowledge Base/Notes/source-{ordinal:03d}.md"
        action["destination_path"] = f"Knowledge Base/Notes/destination-{ordinal:03d}.md"
        actions.append(action)
    draft["content_actions"] = actions
    draft["impact_summary"]["overwrite_count"] = 45
    draft["impact_summary"]["batch_count"] = 5
    draft.pop("rendering_definition")
    draft["rendering_definition"] = consolidation_plan.derive_rendering_definition(draft)
    plan = consolidation_plan.materialize_plan(
        draft,
        materialization=_materialization(),
    )

    pages = tuple(
        consolidation_plan.render_plan_page(plan, page_ordinal=ordinal)
        for ordinal in range(plan.preimage["rendering_definition"]["page_count"])
    )
    assert len(pages) == 8
    assert sum(len(page.rows) for page in pages) == 51
    assert max(len(page.rows) for page in pages) == 20
    assert len({page.digest for page in pages}) == len(pages)
    assert all(page.plan_digest == plan.digest for page in pages)
    assert all(page.total_pages == 8 for page in pages)
    assert all(page.total_rows == 51 for page in pages)
    assert pages[1].section_id == "content-actions"
    assert pages[1].rows[0]["ordinal"] == 0
    assert pages[3].rows[-1]["ordinal"] == 44
    assert consolidation_plan.render_plan_page(plan, page_ordinal=3) == pages[3]
    with pytest.raises(consolidation_plan.ConsolidationPlanUnavailable):
        consolidation_plan.render_plan_page(plan, page_ordinal=8)


def _create_run(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import writer_lease
    from exomem.governance import consolidation_run_state
    from exomem.governance.consolidation_intake import ConsolidationInventoryItem

    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=vault.parent / "writer-state")
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)
    identity = consolidation_run_state.ConsolidationRunIdentity(
        run_id=RUN_ID,
        start_operation_id="00000000-0000-4000-8000-000000000003",
        run_mode="cloned-rehearsal",
        destination_vault_id="vault-destination-01",
        destination_installation_id="installation-destination-01",
        destination_generation=3,
        destination_fence_digest=_digest("destination-fence"),
        destination_identity_binding_digest=_digest("destination-identity"),
        destination_snapshot_fingerprint=_digest("destination-snapshot"),
        source_artifact_ref="exomem-export://sha256/" + _digest("archive"),
        source_attestation_ref="exomem-source-attestation://sha256/" + _digest("proof"),
        archive_sha256=_digest("archive"),
        manifest_sha256=_digest("manifest"),
        source_census_sha256=_digest("source-census"),
        source_proof_digest=_digest("proof"),
        source_fingerprint=_digest("source-snapshot"),
        created_at=CREATED_AT,
    )
    item = ConsolidationInventoryItem(
        path="Knowledge Base/Notes/source.md",
        size=10,
        sha256=_digest("source-item"),
        classification="canonical",
        artifact_ref="exomem-consolidation-object://sha256/" + _digest("source-item"),
    )
    consolidation_run_state.ConsolidationRunStore(vault).create(identity, (item,))


def _policy_bundle(
    vault: Path,
    *,
    name: str = 'Résumé "review"',
) -> consolidation_policy.DestinationPolicyPlan:
    document = _policy_document()
    content = str(document["content"]).replace('Résumé "review"', name)
    return consolidation_policy.compile_destination_policy(
        vault,
        documents={str(document["path"]): content},
        source_authority=(),
        attestations=(),
        principal_contexts=(),
        destination_vault_id="vault-destination-01",
        expected_nonce=NONCE,
        verified_at=CREATED_AT,
    )


def test_owner_only_plan_store_reloads_exact_bytes_and_replays_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_plan_store,
        consolidation_verification_manifest,
    )

    vault = tmp_path / "vault"
    _create_run(vault, monkeypatch)
    policy_bundle = _policy_bundle(vault)
    manifest = _verification_manifest()
    plan = _plan(
        basis_run_revision=1,
        policy_bundle=policy_bundle,
        verification_manifest=manifest,
    )
    store = consolidation_plan_store.ConsolidationPlanStore(vault)

    with pytest.raises(consolidation_plan_store.ConsolidationPlanStoreUnavailable):
        store.persist(
            plan,
            policy_bundle=_policy_bundle(vault, name="Changed review"),
            verification_manifest=manifest,
            expected_run_revision=1,
        )

    first = store.persist(
        plan,
        policy_bundle=policy_bundle,
        verification_manifest=manifest,
        expected_run_revision=1,
    )
    assert (
        store.persist(
            plan,
            policy_bundle=policy_bundle,
            verification_manifest=manifest,
            expected_run_revision=1,
        )
        == first
    )
    assert store.load(RUN_ID, plan_kind="cutover", plan_digest=plan.digest) == plan
    assert (
        store.load_policy_bundle(RUN_ID, plan_kind="cutover", plan_digest=plan.digest)
        == policy_bundle
    )
    plan_dir = (
        vault
        / "Knowledge Base"
        / "_Consolidation"
        / "runs"
        / RUN_ID
        / "plans"
        / "cutover"
        / plan.digest
    )
    assert sorted(path.name for path in plan_dir.iterdir()) == [
        "control-basis.json",
        "plan.json",
        "policy-bundle.json",
        "verification-manifest.json",
    ]
    if os.name != "nt":
        assert plan_dir.stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in plan_dir.iterdir())

    restarted = consolidation_plan_store.ConsolidationPlanStore(vault)
    assert restarted.load(RUN_ID, plan_kind="cutover", plan_digest=plan.digest) == plan
    assert (
        restarted.load_policy_bundle(RUN_ID, plan_kind="cutover", plan_digest=plan.digest)
        == policy_bundle
    )
    assert (
        consolidation_verification_manifest.ConsolidationVerificationManifestStore(vault).load(
            RUN_ID, plan.digest
        )
        == manifest
    )


def test_plan_store_refuses_wrong_run_revision_or_partial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_plan_store

    vault = tmp_path / "vault"
    _create_run(vault, monkeypatch)
    policy_bundle = _policy_bundle(vault)
    manifest = _verification_manifest()
    plan = _plan(
        basis_run_revision=1,
        policy_bundle=policy_bundle,
        verification_manifest=manifest,
    )
    store = consolidation_plan_store.ConsolidationPlanStore(vault)

    with pytest.raises(consolidation_plan_store.ConsolidationPlanStoreUnavailable):
        store.persist(
            plan,
            policy_bundle=policy_bundle,
            verification_manifest=manifest,
            expected_run_revision=2,
        )
    with pytest.raises(consolidation_plan_store.ConsolidationPlanStoreUnavailable):
        store.persist(plan, expected_run_revision=1)
    with pytest.raises(
        consolidation_plan_store.ConsolidationPlanStoreUnavailable,
        match="^PLAN_VERIFICATION_MANIFEST_REQUIRED$",
    ):
        store.persist(
            plan,
            policy_bundle=policy_bundle,
            expected_run_revision=1,
        )
    store.persist(
        plan,
        policy_bundle=policy_bundle,
        verification_manifest=manifest,
        expected_run_revision=1,
    )
    control_path = (
        vault
        / "Knowledge Base"
        / "_Consolidation"
        / "runs"
        / RUN_ID
        / "plans"
        / "cutover"
        / plan.digest
        / "control-basis.json"
    )
    control_path.unlink()
    with pytest.raises(consolidation_plan_store.ConsolidationPlanStoreUnavailable):
        store.load(RUN_ID, plan_kind="cutover", plan_digest=plan.digest)


def test_plan_store_recovers_control_first_crash_without_changing_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_plan_store

    vault = tmp_path / "vault"
    _create_run(vault, monkeypatch)
    policy_bundle = _policy_bundle(vault)
    manifest = _verification_manifest()
    plan = _plan(
        basis_run_revision=1,
        policy_bundle=policy_bundle,
        verification_manifest=manifest,
    )
    store = consolidation_plan_store.ConsolidationPlanStore(vault)
    original_publish = store._publish_missing

    def crash_before_plan(path: Path, value: bytes) -> None:
        if path.name == "plan.json":
            raise OSError("injected crash gap")
        original_publish(path, value)

    monkeypatch.setattr(store, "_publish_missing", crash_before_plan)
    with pytest.raises(consolidation_plan_store.ConsolidationPlanStoreUnavailable):
        store.persist(
            plan,
            policy_bundle=policy_bundle,
            verification_manifest=manifest,
            expected_run_revision=1,
        )

    monkeypatch.setattr(store, "_publish_missing", original_publish)
    assert (
        store.persist(
            plan,
            policy_bundle=policy_bundle,
            verification_manifest=manifest,
            expected_run_revision=1,
        )
        == plan
    )
    assert store.load(RUN_ID, plan_kind="cutover", plan_digest=plan.digest) == plan


@pytest.mark.parametrize("replacement", [None, b"{}"])
def test_plan_store_refuses_missing_or_changed_policy_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: bytes | None,
) -> None:
    from exomem.governance import consolidation_plan_store

    vault = tmp_path / "vault"
    _create_run(vault, monkeypatch)
    policy_bundle = _policy_bundle(vault)
    manifest = _verification_manifest()
    plan = _plan(
        basis_run_revision=1,
        policy_bundle=policy_bundle,
        verification_manifest=manifest,
    )
    store = consolidation_plan_store.ConsolidationPlanStore(vault)
    store.persist(
        plan,
        policy_bundle=policy_bundle,
        verification_manifest=manifest,
        expected_run_revision=1,
    )
    path = (
        vault
        / "Knowledge Base"
        / "_Consolidation"
        / "runs"
        / RUN_ID
        / "plans"
        / "cutover"
        / plan.digest
        / "policy-bundle.json"
    )
    if replacement is None:
        path.unlink()
    else:
        path.write_bytes(replacement)

    with pytest.raises(consolidation_plan_store.ConsolidationPlanStoreUnavailable):
        store.load(RUN_ID, plan_kind="cutover", plan_digest=plan.digest)


@pytest.mark.parametrize("replacement", [None, b"{}"])
def test_plan_store_refuses_missing_or_changed_verification_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: bytes | None,
) -> None:
    from exomem.governance import consolidation_plan_store

    vault = tmp_path / "vault"
    _create_run(vault, monkeypatch)
    policy_bundle = _policy_bundle(vault)
    manifest = _verification_manifest()
    plan = _plan(
        basis_run_revision=1,
        policy_bundle=policy_bundle,
        verification_manifest=manifest,
    )
    store = consolidation_plan_store.ConsolidationPlanStore(vault)
    store.persist(
        plan,
        policy_bundle=policy_bundle,
        verification_manifest=manifest,
        expected_run_revision=1,
    )
    path = store._plan_dir(RUN_ID, "cutover", plan.digest) / "verification-manifest.json"  # noqa: SLF001
    if replacement is None:
        path.unlink()
    else:
        path.write_bytes(replacement)

    with pytest.raises(consolidation_plan_store.ConsolidationPlanStoreUnavailable):
        store.load(RUN_ID, plan_kind="cutover", plan_digest=plan.digest)


def test_plan_store_binds_only_executable_policy_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_plan_store

    vault = tmp_path / "vault"
    _create_run(vault, monkeypatch)
    governance = vault / "Knowledge Base" / "_Governance"
    governance.mkdir(parents=True)
    (governance / "README.md").write_text("Authoring guidance only.\n", encoding="utf-8")
    policy_bundle = _policy_bundle(vault)
    manifest = _verification_manifest()
    plan = _plan(
        basis_run_revision=1,
        policy_bundle=policy_bundle,
        verification_manifest=manifest,
    )
    store = consolidation_plan_store.ConsolidationPlanStore(vault)

    assert (
        store.persist(
            plan,
            policy_bundle=policy_bundle,
            verification_manifest=manifest,
            expected_run_revision=1,
        )
        == plan
    )
    assert store.load(RUN_ID, plan_kind="cutover", plan_digest=plan.digest) == plan


@pytest.mark.parametrize("plan_kind", ["rollback", "retirement"])
def test_non_cutover_plan_store_replay_does_not_require_policy_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan_kind: str,
) -> None:
    from exomem.governance import consolidation_plan_store

    vault = tmp_path / "vault"
    _create_run(vault, monkeypatch)
    plan = _plan(basis_run_revision=1, plan_kind=plan_kind)
    store = consolidation_plan_store.ConsolidationPlanStore(vault)

    assert store.persist(plan, expected_run_revision=1) == plan
    assert store.persist(plan, expected_run_revision=1) == plan
    assert store.load(RUN_ID, plan_kind=plan_kind, plan_digest=plan.digest) == plan


def _trusted_renderer() -> consolidation_review.TrustedRenderIdentity:
    return consolidation_review.TrustedRenderIdentity(
        owner_binding_digest=_digest("owner-binding"),
        owner_principal_digest=_digest("owner-principal"),
        authorization_session_digest=_digest("authorization-session"),
        issuer="trusted-cli-host",
        surface="cli",
    )


def _review() -> consolidation_review.CanonicalRenderReview:
    return consolidation_review.begin_review(
        _plan(),
        identity=_trusted_renderer(),
        issued_at=CREATED_AT,
        expires_at=VALID_UNTIL,
        nonce="render-session-000000000001",
    )


def test_render_session_is_bound_to_stored_plan_and_trusted_surface() -> None:
    review = _review()

    assert set(review.session.preimage) == {
        "schema",
        "run_id",
        "plan_kind",
        "plan_digest",
        "control_basis_digest",
        "rendering_definition_digest",
        "impact_summary_digest",
        "owner_binding_digest",
        "owner_principal_digest",
        "authorization_session_digest",
        "issuer",
        "surface",
        "page_count",
        "total_rows",
        "issued_at",
        "expires_at",
        "nonce",
    }
    assert review.session.digest == (
        "d5b0891f67fe6db97c60f5fc0cbc650aa281a0c921abac2574e14b7226cb103c"
    )
    assert review.state.preimage["next_page_ordinal"] == 0
    assert b"governance_version" not in review.state.canonical_bytes
    assert b"Notes/source.md" not in review.state.canonical_bytes


def test_review_serves_and_acknowledges_only_the_exact_next_stored_page() -> None:
    plan = _plan()
    review = _review()

    review, page = consolidation_review.serve_page(
        review,
        plan=plan,
        identity=_trusted_renderer(),
        page_ordinal=0,
        served_at="2026-08-28T12:00:00.500Z",
    )
    assert page == consolidation_plan.render_plan_page(plan, page_ordinal=0)
    assert review.state.preimage["pending_page_digest"] == page.digest
    replayed, replayed_page = consolidation_review.serve_page(
        review,
        plan=plan,
        identity=_trusted_renderer(),
        page_ordinal=0,
        served_at="2026-08-28T12:00:00.500Z",
    )
    assert replayed == review
    assert replayed_page == page
    for ordinal in (1, 5):
        with pytest.raises(consolidation_review.ConsolidationReviewUnavailable):
            consolidation_review.serve_page(
                review,
                plan=plan,
                identity=_trusted_renderer(),
                page_ordinal=ordinal,
                served_at="2026-08-28T12:00:00.500Z",
            )

    acknowledgement = consolidation_review.build_acknowledgement(
        review,
        page=page,
        identity=_trusted_renderer(),
        issued_at="2026-08-28T12:00:01.000Z",
        nonce="render-ack-000000000000001",
    )
    acknowledged = consolidation_review.acknowledge_page(
        review,
        plan=plan,
        acknowledgement=acknowledgement,
    )
    assert acknowledged.state.preimage["next_page_ordinal"] == 1
    assert acknowledged.state.preimage["pending_page_digest"] == ""
    assert (
        consolidation_review.acknowledge_page(
            acknowledged,
            plan=plan,
            acknowledgement=acknowledgement,
        )
        == acknowledged
    )


def test_body_digest_or_cross_session_acknowledgement_cannot_create_coverage() -> None:
    plan = _plan()
    with pytest.raises(consolidation_review.ConsolidationReviewUnavailable):
        consolidation_review.serve_page(
            _review(),
            plan=plan,
            identity=replace(_trusted_renderer(), surface="hosted"),
            page_ordinal=0,
            served_at="2026-08-28T12:00:00.500Z",
        )
    review, page = consolidation_review.serve_page(
        _review(),
        plan=plan,
        identity=_trusted_renderer(),
        page_ordinal=0,
        served_at="2026-08-28T12:00:00.500Z",
    )

    with pytest.raises(consolidation_review.ConsolidationReviewUnavailable):
        consolidation_review.acknowledge_page(
            review,
            plan=plan,
            acknowledgement={"page_digest": page.digest},
        )
    valid = consolidation_review.build_acknowledgement(
        review,
        page=page,
        identity=_trusted_renderer(),
        issued_at="2026-08-28T12:00:01.000Z",
        nonce="render-ack-000000000000001",
    )
    for changed_identity in (
        replace(_trusted_renderer(), owner_principal_digest=_digest("other-owner")),
        replace(
            _trusted_renderer(),
            authorization_session_digest=_digest("other-session"),
        ),
        replace(_trusted_renderer(), surface="hosted"),
    ):
        forged = consolidation_review.build_acknowledgement(
            review,
            page=page,
            identity=changed_identity,
            issued_at="2026-08-28T12:00:01.000Z",
            nonce="render-ack-000000000000001",
        )
        with pytest.raises(consolidation_review.ConsolidationReviewUnavailable):
            consolidation_review.acknowledge_page(
                review,
                plan=plan,
                acknowledgement=forged,
            )
    assert consolidation_review.acknowledge_page(
        review,
        plan=plan,
        acknowledgement=valid,
    )


def test_completeness_exists_only_after_every_ordered_page_acknowledgement() -> None:
    plan = _plan()
    review = _review()
    with pytest.raises(consolidation_review.ConsolidationReviewUnavailable):
        consolidation_review.complete_review(
            review,
            plan=plan,
            identity=_trusted_renderer(),
            issued_at="2026-08-28T12:10:00.000Z",
            expires_at=VALID_UNTIL,
            nonce="completeness-000000000001",
        )

    page_digests = []
    for ordinal in range(6):
        review, page = consolidation_review.serve_page(
            review,
            plan=plan,
            identity=_trusted_renderer(),
            page_ordinal=ordinal,
            served_at=f"2026-08-28T12:00:{ordinal:02d}.500Z",
        )
        page_digests.append(page.digest)
        acknowledgement = consolidation_review.build_acknowledgement(
            review,
            page=page,
            identity=_trusted_renderer(),
            issued_at=f"2026-08-28T12:00:{ordinal + 1:02d}.000Z",
            nonce=f"render-ack-{ordinal:020d}",
        )
        review = consolidation_review.acknowledge_page(
            review,
            plan=plan,
            acknowledgement=acknowledgement,
        )

    completed, completeness = consolidation_review.complete_review(
        review,
        plan=plan,
        identity=_trusted_renderer(),
        issued_at="2026-08-28T12:10:00.000Z",
        expires_at=VALID_UNTIL,
        nonce="completeness-000000000001",
    )
    assert tuple(completeness.preimage["page_digests"]) == tuple(page_digests)
    assert tuple(completeness.preimage["section_digests"]) == tuple(
        section["content_digest"] for section in plan.preimage["rendering_definition"]["sections"]
    )
    assert completeness.preimage["total_pages"] == 6
    assert completeness.preimage["total_rows"] == 7
    assert completeness.preimage["impact_summary_digest"] == plan.impact_summary_digest
    assert completed.state.preimage["completeness_digest"] == completeness.digest
    replayed_review, replayed = consolidation_review.complete_review(
        completed,
        plan=plan,
        identity=_trusted_renderer(),
        issued_at="2026-08-28T12:10:00.000Z",
        expires_at=VALID_UNTIL,
        nonce="completeness-000000000001",
    )
    assert replayed_review == completed
    assert replayed == completeness


def test_review_refuses_plan_drift_expiry_and_changed_completion_payload() -> None:
    plan = _plan()
    review = _review()
    changed_plan = consolidation_plan.materialize_plan(
        {**_plan_input(), "nonce": "plan-00000000000000000002"},
        materialization=_materialization(),
    )
    with pytest.raises(consolidation_review.ConsolidationReviewUnavailable):
        consolidation_review.serve_page(
            review,
            plan=changed_plan,
            identity=_trusted_renderer(),
            page_ordinal=0,
            served_at="2026-08-28T12:00:00.500Z",
        )

    review, page = consolidation_review.serve_page(
        review,
        plan=plan,
        identity=_trusted_renderer(),
        page_ordinal=0,
        served_at="2026-08-28T12:00:00.500Z",
    )
    with pytest.raises(consolidation_review.ConsolidationReviewUnavailable):
        consolidation_review.build_acknowledgement(
            review,
            page=page,
            identity=_trusted_renderer(),
            issued_at=VALID_UNTIL,
            nonce="render-ack-000000000000001",
        )


def _stored_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    Path,
    consolidation_plan.CanonicalConsolidationPlan,
    consolidation_review.CanonicalRenderReview,
]:
    from exomem.governance import consolidation_plan_store

    vault = tmp_path / "vault"
    _create_run(vault, monkeypatch)
    policy_bundle = _policy_bundle(vault)
    manifest = _verification_manifest()
    plan = _plan(
        basis_run_revision=1,
        policy_bundle=policy_bundle,
        verification_manifest=manifest,
    )
    consolidation_plan_store.ConsolidationPlanStore(vault).persist(
        plan,
        policy_bundle=policy_bundle,
        verification_manifest=manifest,
        expected_run_revision=1,
    )
    review = consolidation_review.begin_review(
        plan,
        identity=_trusted_renderer(),
        issued_at=CREATED_AT,
        expires_at=VALID_UNTIL,
        nonce="render-session-000000000001",
    )
    return vault, plan, review


def test_review_store_persists_and_reloads_exact_ordered_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_review_store

    vault, plan, review = _stored_review(tmp_path, monkeypatch)
    store = consolidation_review_store.ConsolidationReviewStore(vault)
    assert store.persist(review, plan=plan, expected_state_digest=None) == review
    assert (
        store.load(
            RUN_ID,
            plan_kind="cutover",
            plan_digest=plan.digest,
            render_session_digest=review.session.digest,
        )
        == review
    )

    pending, page = consolidation_review.serve_page(
        review,
        plan=plan,
        identity=_trusted_renderer(),
        page_ordinal=0,
        served_at="2026-08-28T12:00:00.500Z",
    )
    store.persist(
        pending,
        plan=plan,
        expected_state_digest=review.state.digest,
    )
    acknowledgement = consolidation_review.build_acknowledgement(
        pending,
        page=page,
        identity=_trusted_renderer(),
        issued_at="2026-08-28T12:00:01.000Z",
        nonce="render-ack-000000000000001",
    )
    acknowledged = consolidation_review.acknowledge_page(
        pending,
        plan=plan,
        acknowledgement=acknowledgement,
    )
    store.persist(
        acknowledged,
        plan=plan,
        expected_state_digest=pending.state.digest,
    )

    restarted = consolidation_review_store.ConsolidationReviewStore(vault)
    loaded = restarted.load(
        RUN_ID,
        plan_kind="cutover",
        plan_digest=plan.digest,
        render_session_digest=review.session.digest,
    )
    assert loaded == acknowledged

    current = loaded
    for ordinal in range(1, 6):
        pending, page = consolidation_review.serve_page(
            current,
            plan=plan,
            identity=_trusted_renderer(),
            page_ordinal=ordinal,
            served_at=f"2026-08-28T12:00:{ordinal:02d}.500Z",
        )
        restarted.persist(
            pending,
            plan=plan,
            expected_state_digest=current.state.digest,
        )
        acknowledgement = consolidation_review.build_acknowledgement(
            pending,
            page=page,
            identity=_trusted_renderer(),
            issued_at=f"2026-08-28T12:00:{ordinal + 1:02d}.000Z",
            nonce=f"render-ack-{ordinal:020d}",
        )
        acknowledged = consolidation_review.acknowledge_page(
            pending,
            plan=plan,
            acknowledgement=acknowledgement,
        )
        restarted.persist(
            acknowledged,
            plan=plan,
            expected_state_digest=pending.state.digest,
        )
        current = acknowledged
    completed, _completeness = consolidation_review.complete_review(
        current,
        plan=plan,
        identity=_trusted_renderer(),
        issued_at="2026-08-28T12:10:00.000Z",
        expires_at=VALID_UNTIL,
        nonce="completeness-000000000001",
    )
    restarted.persist(
        completed,
        plan=plan,
        expected_state_digest=current.state.digest,
    )
    assert (
        consolidation_review_store.ConsolidationReviewStore(vault).load(
            RUN_ID,
            plan_kind="cutover",
            plan_digest=plan.digest,
            render_session_digest=review.session.digest,
        )
        == completed
    )
    stored = b"".join(
        path.read_bytes()
        for path in sorted(
            (
                vault
                / "Knowledge Base"
                / "_Consolidation"
                / "runs"
                / RUN_ID
                / "plans"
                / "cutover"
                / plan.digest
                / "reviews"
            ).rglob("*.json")
        )
    )
    assert b"Knowledge Base/Notes/source.md" not in stored
    assert b"source body" not in stored


def test_review_store_rejects_stale_concurrent_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_review_store

    vault, plan, review = _stored_review(tmp_path, monkeypatch)
    store = consolidation_review_store.ConsolidationReviewStore(vault)
    store.persist(review, plan=plan, expected_state_digest=None)
    pending, page = consolidation_review.serve_page(
        review,
        plan=plan,
        identity=_trusted_renderer(),
        page_ordinal=0,
        served_at="2026-08-28T12:00:00.500Z",
    )
    store.persist(pending, plan=plan, expected_state_digest=review.state.digest)
    first_ack = consolidation_review.build_acknowledgement(
        pending,
        page=page,
        identity=_trusted_renderer(),
        issued_at="2026-08-28T12:00:01.000Z",
        nonce="render-ack-000000000000001",
    )
    second_ack = consolidation_review.build_acknowledgement(
        pending,
        page=page,
        identity=_trusted_renderer(),
        issued_at="2026-08-28T12:00:01.000Z",
        nonce="render-ack-000000000000002",
    )
    first = consolidation_review.acknowledge_page(
        pending,
        plan=plan,
        acknowledgement=first_ack,
    )
    second = consolidation_review.acknowledge_page(
        pending,
        plan=plan,
        acknowledgement=second_ack,
    )
    store.persist(first, plan=plan, expected_state_digest=pending.state.digest)
    with pytest.raises(consolidation_review_store.ConsolidationReviewStoreUnavailable):
        store.persist(second, plan=plan, expected_state_digest=pending.state.digest)

    assert store.persist(first, plan=plan, expected_state_digest=pending.state.digest) == first


def test_review_store_recovers_snapshot_before_active_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_review_store

    vault, plan, review = _stored_review(tmp_path, monkeypatch)
    store = consolidation_review_store.ConsolidationReviewStore(vault)
    store.persist(review, plan=plan, expected_state_digest=None)
    pending, _page = consolidation_review.serve_page(
        review,
        plan=plan,
        identity=_trusted_renderer(),
        page_ordinal=0,
        served_at="2026-08-28T12:00:00.500Z",
    )
    original_publish_active = store._publish_active

    def crash_before_active(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected active-pointer gap")

    monkeypatch.setattr(store, "_publish_active", crash_before_active)
    with pytest.raises(consolidation_review_store.ConsolidationReviewStoreUnavailable):
        store.persist(
            pending,
            plan=plan,
            expected_state_digest=review.state.digest,
        )
    monkeypatch.setattr(store, "_publish_active", original_publish_active)

    assert (
        store.load(
            RUN_ID,
            plan_kind="cutover",
            plan_digest=plan.digest,
            render_session_digest=review.session.digest,
        )
        == review
    )
    assert (
        store.persist(
            pending,
            plan=plan,
            expected_state_digest=review.state.digest,
        )
        == pending
    )


def _completed_review() -> tuple[
    consolidation_plan.CanonicalConsolidationPlan,
    consolidation_review.CanonicalRenderReview,
]:
    plan = _plan()
    review = _review()
    for ordinal in range(6):
        review, page = consolidation_review.serve_page(
            review,
            plan=plan,
            identity=_trusted_renderer(),
            page_ordinal=ordinal,
            served_at=f"2026-08-28T12:00:{ordinal:02d}.500Z",
        )
        acknowledgement = consolidation_review.build_acknowledgement(
            review,
            page=page,
            identity=_trusted_renderer(),
            issued_at=f"2026-08-28T12:00:{ordinal + 1:02d}.000Z",
            nonce=f"render-ack-{ordinal:020d}",
        )
        review = consolidation_review.acknowledge_page(
            review,
            plan=plan,
            acknowledgement=acknowledgement,
        )
    completed, _completeness = consolidation_review.complete_review(
        review,
        plan=plan,
        identity=_trusted_renderer(),
        issued_at="2026-08-28T12:10:00.000Z",
        expires_at=VALID_UNTIL,
        nonce="completeness-000000000001",
    )
    return plan, completed


def test_approval_token_binds_exact_plan_completeness_and_confirmation() -> None:
    from exomem.governance import consolidation_approval

    plan, review = _completed_review()
    confirmation = consolidation_approval.TrustedOwnerConfirmation(
        owner_binding_digest=_trusted_renderer().owner_binding_digest,
        owner_principal_digest=_trusted_renderer().owner_principal_digest,
        authorization_session_digest=_trusted_renderer().authorization_session_digest,
        issuer=_trusted_renderer().issuer,
        surface=_trusted_renderer().surface,
        action="approve",
        run_id=RUN_ID,
        plan_kind="cutover",
        plan_digest=plan.digest,
        rendering_completeness_digest=review.completeness.digest,
        confirmed_at="2026-08-28T12:11:00.000Z",
        nonce="owner-confirmation-00000001",
    )
    token = consolidation_approval.mint_approval(
        plan=plan,
        review=review,
        identity=_trusted_renderer(),
        confirmation=confirmation,
        jti="0123456789abcdef0123456789abcdef",
        expires_at="2026-08-28T12:30:00.000Z",
        signing_key_id="approval-key-01",
        signing_key=b"k" * 32,
    )

    assert token.claim.preimage == {
        "schema": "exomem.consolidation-approval-token/v1",
        "plan_kind": "cutover",
        "run_id": RUN_ID,
        "plan_digest": plan.digest,
        "rendering_completeness_digest": review.completeness.digest,
        "jti": "0123456789abcdef0123456789abcdef",
        "expires_at": "2026-08-28T12:30:00.000Z",
        "signing_key_id": "approval-key-01",
    }
    assert token.digest == "acf440d4b21fbe930d2e782d6c1b9b305a2ce36a4fb176278a402848eae668a0"
    assert (
        consolidation_approval.verify_approval(
            token.wire,
            plan=plan,
            review=review,
            now="2026-08-28T12:12:00.000Z",
            verifier_keys={"approval-key-01": b"k" * 32},
        )
        == token
    )
    assert token.wire.encode() not in token.claim.canonical_bytes
    assert b"kkkkkkkk" not in token.claim.canonical_bytes


def test_approval_refuses_incomplete_cross_session_or_body_confirmation() -> None:
    from exomem.governance import consolidation_approval

    plan, completed = _completed_review()
    confirmation = consolidation_approval.TrustedOwnerConfirmation(
        owner_binding_digest=_trusted_renderer().owner_binding_digest,
        owner_principal_digest=_trusted_renderer().owner_principal_digest,
        authorization_session_digest=_trusted_renderer().authorization_session_digest,
        issuer=_trusted_renderer().issuer,
        surface=_trusted_renderer().surface,
        action="approve",
        run_id=RUN_ID,
        plan_kind="cutover",
        plan_digest=plan.digest,
        rendering_completeness_digest=completed.completeness.digest,
        confirmed_at="2026-08-28T12:11:00.000Z",
        nonce="owner-confirmation-00000001",
    )
    arguments = {
        "plan": plan,
        "review": completed,
        "identity": _trusted_renderer(),
        "confirmation": confirmation,
        "jti": "0123456789abcdef0123456789abcdef",
        "expires_at": "2026-08-28T12:30:00.000Z",
        "signing_key_id": "approval-key-01",
        "signing_key": b"k" * 32,
    }
    for changed in (
        {**arguments, "review": _review()},
        {
            **arguments,
            "identity": replace(_trusted_renderer(), surface="hosted"),
        },
        {**arguments, "confirmation": {"approved": True}},
        {
            **arguments,
            "confirmation": replace(confirmation, action="apply"),
        },
        {
            **arguments,
            "confirmation": replace(
                confirmation,
                confirmed_at="2026-08-28T12:09:59.999Z",
            ),
        },
    ):
        with pytest.raises(consolidation_approval.ConsolidationApprovalUnavailable):
            consolidation_approval.mint_approval(**changed)


def test_approval_refuses_expiry_tamper_and_changed_plan() -> None:
    from exomem.governance import consolidation_approval

    plan, review = _completed_review()
    confirmation = consolidation_approval.TrustedOwnerConfirmation(
        owner_binding_digest=_trusted_renderer().owner_binding_digest,
        owner_principal_digest=_trusted_renderer().owner_principal_digest,
        authorization_session_digest=_trusted_renderer().authorization_session_digest,
        issuer=_trusted_renderer().issuer,
        surface=_trusted_renderer().surface,
        action="approve",
        run_id=RUN_ID,
        plan_kind="cutover",
        plan_digest=plan.digest,
        rendering_completeness_digest=review.completeness.digest,
        confirmed_at="2026-08-28T12:11:00.000Z",
        nonce="owner-confirmation-00000001",
    )
    token = consolidation_approval.mint_approval(
        plan=plan,
        review=review,
        identity=_trusted_renderer(),
        confirmation=confirmation,
        jti="0123456789abcdef0123456789abcdef",
        expires_at="2026-08-28T12:30:00.000Z",
        signing_key_id="approval-key-01",
        signing_key=b"k" * 32,
    )
    changed_plan = consolidation_plan.materialize_plan(
        {**_plan_input(), "nonce": "plan-00000000000000000002"},
        materialization=_materialization(),
    )
    for wire, candidate_plan, now in (
        (
            token.wire[:-1] + ("A" if token.wire[-1] != "A" else "B"),
            plan,
            "2026-08-28T12:12:00.000Z",
        ),
        (token.wire, changed_plan, "2026-08-28T12:12:00.000Z"),
        (token.wire, plan, "2026-08-28T12:30:00.000Z"),
    ):
        with pytest.raises(consolidation_approval.ConsolidationApprovalUnavailable):
            consolidation_approval.verify_approval(
                wire,
                plan=candidate_plan,
                review=review,
                now=now,
                verifier_keys={"approval-key-01": b"k" * 32},
            )


def _stored_completed_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    Path,
    consolidation_plan.CanonicalConsolidationPlan,
    consolidation_review.CanonicalRenderReview,
]:
    from exomem.governance import consolidation_review_store

    vault, plan, review = _stored_review(tmp_path, monkeypatch)
    store = consolidation_review_store.ConsolidationReviewStore(vault)
    store.persist(review, plan=plan, expected_state_digest=None)
    for ordinal in range(6):
        pending, page = consolidation_review.serve_page(
            review,
            plan=plan,
            identity=_trusted_renderer(),
            page_ordinal=ordinal,
            served_at=f"2026-08-28T12:00:{ordinal:02d}.500Z",
        )
        store.persist(
            pending,
            plan=plan,
            expected_state_digest=review.state.digest,
        )
        acknowledgement = consolidation_review.build_acknowledgement(
            pending,
            page=page,
            identity=_trusted_renderer(),
            issued_at=f"2026-08-28T12:00:{ordinal + 1:02d}.000Z",
            nonce=f"render-ack-{ordinal:020d}",
        )
        review = consolidation_review.acknowledge_page(
            pending,
            plan=plan,
            acknowledgement=acknowledgement,
        )
        store.persist(
            review,
            plan=plan,
            expected_state_digest=pending.state.digest,
        )
    completed, _completeness = consolidation_review.complete_review(
        review,
        plan=plan,
        identity=_trusted_renderer(),
        issued_at="2026-08-28T12:10:00.000Z",
        expires_at=VALID_UNTIL,
        nonce="completeness-000000000001",
    )
    store.persist(
        completed,
        plan=plan,
        expected_state_digest=review.state.digest,
    )
    return vault, plan, completed


def _owner_confirmation(
    plan: consolidation_plan.CanonicalConsolidationPlan,
    review: consolidation_review.CanonicalRenderReview,
):
    from exomem.governance import consolidation_approval

    return consolidation_approval.TrustedOwnerConfirmation(
        owner_binding_digest=_trusted_renderer().owner_binding_digest,
        owner_principal_digest=_trusted_renderer().owner_principal_digest,
        authorization_session_digest=_trusted_renderer().authorization_session_digest,
        issuer=_trusted_renderer().issuer,
        surface=_trusted_renderer().surface,
        action="approve",
        run_id=RUN_ID,
        plan_kind="cutover",
        plan_digest=plan.digest,
        rendering_completeness_digest=review.completeness.digest,
        confirmed_at="2026-08-28T12:11:00.000Z",
        nonce="owner-confirmation-00000001",
    )


def test_approval_store_replays_lost_issuance_without_persisting_token_or_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_approval_store

    vault, plan, review = _stored_completed_review(tmp_path, monkeypatch)
    store = consolidation_approval_store.ConsolidationApprovalStore(vault)
    generated = iter(
        (
            "0123456789abcdef0123456789abcdef",
            "fedcba9876543210fedcba9876543210",
        )
    )
    monkeypatch.setattr(store, "_new_jti", lambda: next(generated))
    arguments = {
        "plan": plan,
        "render_session_digest": review.session.digest,
        "identity": _trusted_renderer(),
        "confirmation": _owner_confirmation(plan, review),
        "operation_id": "00000000-0000-4000-8000-000000000041",
        "expires_at": "2026-08-28T12:30:00.000Z",
        "signing_key_id": "approval-key-01",
        "signing_key": b"k" * 32,
    }
    issued = store.issue(**arguments)
    assert store.issue(**arguments) == issued
    assert next(generated) == "fedcba9876543210fedcba9876543210"
    with pytest.raises(consolidation_approval_store.ConsolidationApprovalStoreUnavailable):
        store.issue(
            **{
                **arguments,
                "confirmation": replace(
                    arguments["confirmation"],
                    nonce="owner-confirmation-00000002",
                ),
            }
        )

    approval_dir = (
        vault
        / "Knowledge Base"
        / "_Consolidation"
        / "runs"
        / RUN_ID
        / "plans"
        / "cutover"
        / plan.digest
        / "approvals"
    )
    persisted = b"".join(path.read_bytes() for path in sorted(approval_dir.rglob("*.json")))
    assert issued.wire.encode() not in persisted
    assert b"kkkkkkkk" not in persisted
    assert b"approved" not in persisted


def test_approval_store_repairs_operation_before_jti_index_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_approval_store

    vault, plan, review = _stored_completed_review(tmp_path, monkeypatch)
    store = consolidation_approval_store.ConsolidationApprovalStore(vault)
    monkeypatch.setattr(
        store,
        "_new_jti",
        lambda: "0123456789abcdef0123456789abcdef",
    )
    original_publish_jti = store._publish_jti

    def crash_before_jti(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected JTI-index gap")

    monkeypatch.setattr(store, "_publish_jti", crash_before_jti)
    arguments = {
        "plan": plan,
        "render_session_digest": review.session.digest,
        "identity": _trusted_renderer(),
        "confirmation": _owner_confirmation(plan, review),
        "operation_id": "00000000-0000-4000-8000-000000000041",
        "expires_at": "2026-08-28T12:30:00.000Z",
        "signing_key_id": "approval-key-01",
        "signing_key": b"k" * 32,
    }
    with pytest.raises(consolidation_approval_store.ConsolidationApprovalStoreUnavailable):
        store.issue(**arguments)
    monkeypatch.setattr(store, "_publish_jti", original_publish_jti)
    assert store.issue(**arguments).claim.preimage["jti"] == ("0123456789abcdef0123456789abcdef")


def test_approval_jti_reserves_exactly_one_execution_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_approval_store

    vault, plan, review = _stored_completed_review(tmp_path, monkeypatch)
    store = consolidation_approval_store.ConsolidationApprovalStore(vault)
    monkeypatch.setattr(
        store,
        "_new_jti",
        lambda: "0123456789abcdef0123456789abcdef",
    )
    issued = store.issue(
        plan=plan,
        render_session_digest=review.session.digest,
        identity=_trusted_renderer(),
        confirmation=_owner_confirmation(plan, review),
        operation_id="00000000-0000-4000-8000-000000000041",
        expires_at="2026-08-28T12:30:00.000Z",
        signing_key_id="approval-key-01",
        signing_key=b"k" * 32,
    )
    arguments = {
        "wire": issued.wire,
        "plan": plan,
        "render_session_digest": review.session.digest,
        "execution_operation_id": "00000000-0000-4000-8000-000000000042",
        "request_digest": _digest("apply-request"),
        "reserved_at": "2026-08-28T12:12:00.000Z",
        "verifier_keys": {"approval-key-01": b"k" * 32},
    }
    reservation = store.reserve(**arguments)
    assert store.reserve(**arguments) == reservation
    assert store.reserve(**{**arguments, "reserved_at": "2026-08-28T12:31:00.000Z"}) == reservation
    assert reservation.jti == "0123456789abcdef0123456789abcdef"
    for changed in (
        {
            **arguments,
            "execution_operation_id": "00000000-0000-4000-8000-000000000043",
        },
        {**arguments, "request_digest": _digest("changed-request")},
    ):
        with pytest.raises(consolidation_approval_store.ConsolidationApprovalStoreUnavailable):
            store.reserve(**changed)

    restarted = consolidation_approval_store.ConsolidationApprovalStore(vault)
    assert restarted.reserve(**arguments) == reservation
