from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable

import pytest

from exomem.governance import consolidation_plan

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


def _rendering_definition() -> dict[str, object]:
    section_ids = (
        "impact-summary",
        "content-actions",
        "policy",
        "principals-disclosure",
        "verification",
        "rollback-retention",
    )
    return {
        "schema": "exomem.consolidation-rendering-definition/v1",
        "page_size": 20,
        "page_count": len(section_ids),
        "total_rows": len(section_ids),
        "sections": [
            {
                "ordinal": ordinal,
                "section_id": section_id,
                "row_count": 1,
                "first_page_ordinal": ordinal,
                "page_count": 1,
                "content_digest": _digest(f"section:{section_id}"),
            }
            for ordinal, section_id in enumerate(section_ids)
        ],
    }


def _plan_input() -> dict[str, object]:
    return {
        "schema": "exomem.consolidation-plan/v1",
        "protocol_version": 1,
        "plan_kind": "cutover",
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
        "rendering_definition": _rendering_definition(),
        "created_at": CREATED_AT,
        "valid_until": VALID_UNTIL,
        "nonce": NONCE,
    }


def _materialization() -> consolidation_plan.PlanMaterializationContext:
    return consolidation_plan.PlanMaterializationContext(
        operation_id=OPERATION_ID,
        basis_run_revision=7,
        predecessor_event_id=_digest("reconcile-terminal"),
        predecessor_payload_digest=_digest("reconcile-payload"),
    )


def _plan() -> consolidation_plan.CanonicalConsolidationPlan:
    return consolidation_plan.materialize_plan(
        _plan_input(),
        materialization=_materialization(),
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
    assert plan.digest == "062739152e58992e29a8a224f01f7aee45f6941a6622aeb82ed76f374c4d1f76"
    assert plan.plan_input_set_digest == (
        "14313cde01a2586e455ae0235ad5971451d5c7b7d34aaec34772338f7c90ae09"
    )
    assert plan.control_basis.digest == (
        "4adcd528d34f4784ceaef8b67c33998a34af8fff0c3d418ba97b44efc34d9a04"
    )
    assert plan.impact_summary_digest == (
        "e6ec13c6d5a662678910a04d60d67cc095480b907cb8f30751399977ac6ec46f"
    )
    assert plan.rendering_definition_digest == (
        "feafeaf811ca3c02b13afaacd0e731846b812e753fc519e7dd9a9d15422b0172"
    )
    assert len(plan.canonical_bytes) == 5178
    assert hashlib.sha256(plan.canonical_bytes).hexdigest() == (
        "e03ad068536f73875d679c39f0298463d94d32ced350cf28c794a9f5c8c8b816"
    )
    assert len(plan.framed_bytes) == 5218
    assert plan.framed_bytes[:40].hex() == (
        "0000001c65786f6d656d2e636f6e736f6c69646174696f6e2d706c616e2f7631000000000000143a"
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
    value["rendering_definition"]["sections"][0]["content_digest"] = _digest("changed-section")


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
