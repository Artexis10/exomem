"""Closed request contract for governed vault consolidation."""

from __future__ import annotations

import copy
import os
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from exomem.governance import consolidation_request

UUID = "123e4567-e89b-42d3-a456-426614174000"
DIGEST = "a" * 64


def _request(action: str) -> dict[str, object]:
    base: dict[str, object] = {
        "schema": "exomem.consolidate-memory-request/v1",
        "action": action,
    }
    if action == "start":
        return base | {
            "operation_id": UUID,
            "expected_run_revision": 0,
            "run_mode": "cloned-rehearsal",
            "source_artifact_ref": "source-artifact",
            "source_attestation_ref": "source-attestation",
            "clone_binding_ref": "clone-binding",
        }
    if action == "status":
        return base | {"run_id": UUID, "detail": "owner-detail", "cursor": "next", "limit": 1}
    if action == "reconcile":
        return base | {
            "operation_id": UUID,
            "run_id": UUID,
            "expected_run_revision": 0,
            "expected_inventory_digest": DIGEST,
            "decision_set_ref": "decisions",
            "decision_set_digest": DIGEST,
        }
    if action == "plan":
        return base | {
            "operation_id": UUID,
            "run_id": UUID,
            "expected_run_revision": 0,
            "plan_kind": "cutover",
            "operation": "materialize",
            "successor_context_ref": "context",
            "successor_context_digest": DIGEST,
            "cutover_options": {
                "expected_reconciliation_digest": DIGEST,
                "expected_policy_bundle_digest": DIGEST,
                "expected_principal_attestation_set_digest": DIGEST,
                "expected_verification_plan_digest": DIGEST,
                "expected_rollback_contingency_digest": DIGEST,
                "expected_source_retention_digest": DIGEST,
                "expected_control_basis_digest": DIGEST,
            },
        }
    if action == "approve":
        return base | {
            "operation_id": UUID,
            "run_id": UUID,
            "expected_run_revision": 0,
            "plan_kind": "cutover",
            "plan_digest": DIGEST,
            "rendering_completeness_ref": "completeness",
            "rendering_completeness_digest": DIGEST,
            "successor_context_ref": "context",
            "successor_context_digest": DIGEST,
        }
    if action == "apply":
        return base | {
            "operation_id": UUID,
            "run_id": UUID,
            "expected_run_revision": 0,
            "cutover_plan_digest": DIGEST,
            "approval_token_ref": "token",
            "approval_token_digest": DIGEST,
            "successor_context_ref": "context",
            "successor_context_digest": DIGEST,
        }
    if action == "verify":
        return base | {
            "operation_id": UUID,
            "run_id": UUID,
            "expected_run_revision": 0,
            "verification_kind": "in-process",
            "expected_plan_digest": DIGEST,
            "expected_verification_basis_digest": DIGEST,
        }
    if action == "recover":
        return base | {
            "operation_id": UUID,
            "run_id": UUID,
            "expected_run_revision": 0,
            "expected_journal_digest": DIGEST,
            "expected_intent_event_id": DIGEST,
        }
    if action == "abort":
        return base | {
            "operation_id": UUID,
            "run_id": UUID,
            "expected_run_revision": 0,
            "expected_journal_digest": DIGEST,
            "reason_code": "owner-cancelled",
            "reason": "stop",
        }
    if action == "rollback":
        return base | {
            "operation_id": UUID,
            "run_id": UUID,
            "expected_run_revision": 0,
            "rollback_mode": "terminal-plan",
            "successor_context_ref": "context",
            "successor_context_digest": DIGEST,
            "rollback_plan_digest": DIGEST,
            "rollback_token_ref": "token",
            "rollback_token_digest": DIGEST,
        }
    if action == "retire-source":
        return base | {
            "operation_id": UUID,
            "run_id": UUID,
            "expected_run_revision": 0,
            "phase": "clearance",
            "retirement_plan_digest": DIGEST,
            "retirement_token_ref": "token",
            "retirement_token_digest": DIGEST,
            "successor_context_ref": "context",
            "successor_context_digest": DIGEST,
        }
    raise AssertionError(action)


def _validate(request: dict[str, object]) -> dict[str, object]:
    trusted_run_mode = (
        "cloned-rehearsal"
        if request.get("action") == "plan"
        and request.get("operation") == "materialize"
        and request.get("plan_kind") == "cutover"
        else None
    )
    return consolidation_request.validate_request(request, trusted_run_mode=trusted_run_mode)


@pytest.mark.parametrize("action", consolidation_request.ACTIONS)
def test_accepts_each_closed_request_variant(action: str) -> None:
    request = _request(action)

    validated = _validate(request)

    assert validated == request
    assert validated is not request


def test_schema_and_runtime_accept_the_same_canonical_examples() -> None:
    schema = consolidation_request.request_schema()
    validator = Draft202012Validator(schema)

    for action in consolidation_request.ACTIONS:
        request = _request(action)
        assert validator.is_valid(request)
        assert _validate(request) == request


@pytest.mark.parametrize(
    ("mutate"),
    [
        lambda request: request.__setitem__("unknown", "no"),
        lambda request: request.__setitem__("operation_id", UUID.upper()),
        lambda request: request.__setitem__("expected_run_revision", True),
        lambda request: request.__setitem__("source_artifact_ref", "\x00"),
        lambda request: request.__setitem__("source_attestation_ref", "x" * 513),
        lambda request: request.__setitem__("schema", None),
    ],
)
def test_refuses_invalid_common_field_mutations(mutate) -> None:
    request = _request("start")
    mutate(request)

    with pytest.raises(consolidation_request.ConsolidationRequestUnavailable) as raised:
        _validate(request)

    assert raised.value.code == "CONSOLIDATION_REQUEST_INVALID"
    assert str(raised.value) == "consolidation request is unavailable"


@pytest.mark.parametrize(
    ("action", "mutate"),
    [
        ("start", lambda request: request.__setitem__("run_mode", "real-cutover")),
        ("status", lambda request: request.__setitem__("operation_id", UUID)),
        ("reconcile", lambda request: request.__setitem__("decision_set_digest", None)),
        ("plan", lambda request: request.__setitem__("render_step", "begin")),
        ("approve", lambda request: request.__setitem__("approval", True)),
        ("apply", lambda request: request.__setitem__("resume", True)),
        ("verify", lambda request: request.__setitem__("verification_kind", "other")),
        ("recover", lambda request: request.__setitem__("expected_intent_event_id", DIGEST + "0")),
        ("abort", lambda request: request.__setitem__("reason", "x" * 501)),
        (
            "rollback",
            lambda request: request.__setitem__("rollback_mode", "nonterminal-contingency"),
        ),
        ("retire-source", lambda request: request.__setitem__("phase", "finalize")),
    ],
)
def test_refuses_cross_action_and_conditional_mutations(action: str, mutate) -> None:
    request = _request(action)
    mutate(request)

    with pytest.raises(consolidation_request.ConsolidationRequestUnavailable):
        _validate(request)


def test_plan_render_steps_and_materialize_options_are_closed() -> None:
    request = _request("plan")
    request.update(
        {
            "operation": "render",
            "render_step": "acknowledge",
            "plan_digest": DIGEST,
            "render_session_ref": "session",
            "page_ordinal": 0,
            "acknowledged_page_digest": DIGEST,
        }
    )
    request.pop("cutover_options")

    assert _validate(request) == request

    request["rollback_options"] = {}
    with pytest.raises(consolidation_request.ConsolidationRequestUnavailable):
        _validate(request)


@pytest.mark.parametrize(
    ("payload", "trusted_run_mode"),
    [
        (
            _request("start") | {"run_mode": "real-cutover"},
            None,
        ),
        (
            _request("plan")
            | {
                "plan_kind": "rollback",
                "rollback_options": {
                    "target_kind": "pre-cutover",
                    "target_snapshot_ref": "snapshot",
                    "target_snapshot_digest": DIGEST,
                    "expected_current_census_digest": DIGEST,
                    "treatment_set_ref": "treatment-set",
                    "treatment_set_digest": DIGEST,
                    "surviving_copy_ledger_digest": DIGEST,
                    "expected_retirement_state_digest": DIGEST,
                },
            },
            None,
        ),
        (
            _request("plan")
            | {
                "operation": "render",
                "render_step": "begin",
                "plan_digest": DIGEST,
            },
            None,
        ),
        (
            _request("plan")
            | {
                "operation": "render",
                "render_step": "page",
                "plan_digest": DIGEST,
                "render_session_ref": "session",
                "page_ordinal": 0,
            },
            None,
        ),
        (
            _request("plan")
            | {
                "operation": "render",
                "render_step": "complete",
                "plan_digest": DIGEST,
                "render_session_ref": "session",
            },
            None,
        ),
        (
            _request("rollback") | {"rollback_mode": "nonterminal-contingency"},
            None,
        ),
        (
            _request("retire-source")
            | {
                "phase": "finalize",
                "retirement_lifecycle_ref": "lifecycle",
                "completion_attestation_ref": "attestation",
                "completion_attestation_digest": DIGEST,
            },
            None,
        ),
    ],
)
def test_accepts_every_non_nested_conditional_branch(
    payload: dict[str, object], trusted_run_mode: str | None
) -> None:
    request = copy.deepcopy(payload)
    request.pop("clone_binding_ref", None)
    request.pop("cutover_options", None)
    request.pop("rollback_plan_digest", None)
    request.pop("rollback_token_ref", None)
    request.pop("rollback_token_digest", None)
    request.pop("retirement_token_ref", None)
    request.pop("retirement_token_digest", None)
    if request["action"] == "retire-source" and request["phase"] == "finalize":
        request.pop("successor_context_ref")
        request.pop("successor_context_digest")
    schema = Draft202012Validator(consolidation_request.request_schema())

    assert schema.is_valid(request)
    assert (
        consolidation_request.validate_request(request, trusted_run_mode=trusted_run_mode)
        == request
    )


@pytest.mark.parametrize(
    ("disposition", "rollback_mode"),
    [
        ("retain", "pre-cutover-reversible"),
        ("retain", "forward-only"),
        ("transfer", "pre-cutover-reversible"),
        ("transfer", "forward-only"),
        ("external-destruction", "pre-cutover-reversible"),
        ("external-destruction", "forward-only"),
    ],
)
def test_retirement_options_accept_each_exact_conditional_branch(
    disposition: str, rollback_mode: str
) -> None:
    request = _request("plan")
    request["plan_kind"] = "retirement"
    request.pop("cutover_options")
    options = {
        "archive_disposition": disposition,
        "archive_artifact_ref": "archive",
        "archive_artifact_digest": DIGEST,
        "archive_terms_digest": DIGEST,
        "post_retirement_rollback_mode": rollback_mode,
        "source_retention_proof_ref": "retention-proof",
        "source_retention_proof_digest": DIGEST,
        "surviving_copy_ledger_digest": DIGEST,
        "retained_irrecoverable_statement_digest": DIGEST,
    }
    if disposition == "transfer":
        options |= {"custodian_receipt_ref": "custody", "custodian_receipt_digest": DIGEST}
    if rollback_mode == "forward-only":
        options |= {"forward_snapshot_ref": "forward", "forward_snapshot_digest": DIGEST}
    request["retirement_options"] = options

    assert _validate(request) == request


def test_materialize_options_enforce_trusted_real_cutover_rehearsal_proof() -> None:
    request = _request("plan")

    with pytest.raises(consolidation_request.ConsolidationRequestUnavailable):
        consolidation_request.validate_request(request)
    with pytest.raises(consolidation_request.ConsolidationRequestUnavailable):
        consolidation_request.validate_request(request, trusted_run_mode="real-cutover")

    request["cutover_options"] = copy.deepcopy(request["cutover_options"])
    request["cutover_options"]["expected_rehearsal_proof_digest"] = DIGEST  # type: ignore[index]
    assert (
        consolidation_request.validate_request(request, trusted_run_mode="real-cutover") == request
    )
    with pytest.raises(consolidation_request.ConsolidationRequestUnavailable):
        consolidation_request.validate_request(request, trusted_run_mode="cloned-rehearsal")


def test_refuses_non_nfc_and_non_dict_decoded_values() -> None:
    request = _request("start")
    request["source_artifact_ref"] = "cafe\u0301"

    with pytest.raises(consolidation_request.ConsolidationRequestUnavailable):
        _validate(request)
    with pytest.raises(consolidation_request.ConsolidationRequestUnavailable):
        consolidation_request.validate_request(tuple(request.items()), trusted_run_mode=None)


@pytest.mark.parametrize(
    ("action", "field", "invalid"),
    [
        ("start", "operation_id", UUID + "\n"),
        ("plan", "successor_context_digest", DIGEST + "\n"),
    ],
)
def test_schema_and_runtime_refuse_exact_type_and_end_boundary_mutations(
    action: str, field: str, invalid: object
) -> None:
    request = _request(action)
    request[field] = invalid
    validator = Draft202012Validator(consolidation_request.request_schema())

    assert not validator.is_valid(request)
    with pytest.raises(consolidation_request.ConsolidationRequestUnavailable):
        _validate(request)


def test_runtime_refuses_float_for_exact_integer_field() -> None:
    request = _request("start")
    request["expected_run_revision"] = 0.0

    with pytest.raises(consolidation_request.ConsolidationRequestUnavailable):
        _validate(request)


def test_stock_json_schema_cannot_distinguish_a_decoded_integral_float() -> None:
    request = _request("start")
    request["expected_run_revision"] = 0.0
    validator = Draft202012Validator(consolidation_request.request_schema())

    assert validator.is_valid(request)


def test_stock_json_schema_cannot_express_the_unicode_ref_contract() -> None:
    request = _request("start")
    request["source_artifact_ref"] = "é" * 512
    validator = Draft202012Validator(consolidation_request.request_schema())

    assert validator.is_valid(request)
    with pytest.raises(consolidation_request.ConsolidationRequestUnavailable):
        _validate(request)


def test_generated_schema_has_stable_serialized_order_across_hash_seeds() -> None:
    script = (
        "import hashlib, json; "
        "from exomem.governance.consolidation_request import REQUEST_SCHEMA; "
        "print(hashlib.sha256("
        "json.dumps(REQUEST_SCHEMA, separators=(',', ':')).encode()).hexdigest())"
    )
    environment = os.environ.copy()
    hashes = set()
    for seed in ("1", "2", "3"):
        environment["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            env=environment,
            text=True,
        )
        hashes.add(completed.stdout.strip())

    assert len(hashes) == 1


def test_schema_is_fresh_and_closed() -> None:
    first = consolidation_request.request_schema()
    second = consolidation_request.request_schema()

    assert first == consolidation_request.REQUEST_SCHEMA
    assert first is not second
    first["$defs"]["digest"]["pattern"] = "broken"
    assert second["$defs"]["digest"]["pattern"] == "^[0-9a-f]{64}(?![\\s\\S])"


@pytest.mark.parametrize("action", ["start", "abort"])
def test_optional_rehearsal_binding_and_empty_abort_reason_are_valid(action):
    request = _request(action)
    if action == "start":
        request.pop("clone_binding_ref")
    else:
        request["reason"] = ""
    assert _validate(request) == request
    assert Draft202012Validator(consolidation_request.request_schema()).is_valid(request)
