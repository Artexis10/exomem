from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterator, Mapping

import pytest
from jsonschema import Draft202012Validator

from exomem.governance import consolidation_plan
from exomem.governance import consolidation_successor as successor
from exomem.governance import consolidation_terminal as terminal

RUN_ID = "00000000-0000-4000-8000-000000000011"
OPERATION_ID = "00000000-0000-4000-8000-000000000012"
OTHER_OPERATION_ID = "00000000-0000-4000-8000-000000000013"
ISSUED_AT = "2026-08-28T12:00:00.000Z"
EXPIRES_AT = "2026-08-28T12:15:00.000Z"
PLAN_ENTRY_EXPIRES_AT = "9999-12-31T23:59:59.999Z"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


ROWS = {
    "start": (("source_fingerprint", "destination_snapshot_fingerprint"), ("source_objects", "source_bytes", "destination_objects", "destination_bytes"), ("status", "reconcile"), "start"),
    "status": (("run_state_digest", "journal_digest"), ("completed_effects", "pending_effects", "blocked_effects", "warnings"), ("status",), "active"),
    "reconcile": (("inventory_digest", "reconciliation_digest", "mapping_set_digest"), ("c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8", "unresolved", "mappings"), ("status", "reconcile", "plan"), "reconcile"),
    "plan:materialize": (("plan_digest", "control_basis_digest", "plan_successor_automaton_digest"), ("content_actions", "policy_documents", "impact_rows", "render_pages"), ("status", "plan"), "materialize"),
    "plan:render-begin": (("plan_digest", "render_session_digest"), ("render_pages", "acknowledged_pages"), ("status", "plan"), "render-begin"),
    "plan:render-page": (("plan_digest", "render_page_digest"), ("page_ordinal", "page_rows", "render_pages"), ("status", "plan"), "render-page"),
    "plan:render-acknowledge": (("plan_digest", "render_ack_digest"), ("acknowledged_pages", "render_pages"), ("status", "plan"), "render-acknowledge"),
    "plan:render-complete": (("plan_digest", "rendering_completeness_digest"), ("acknowledged_pages", "render_pages"), ("status", "approve"), "render-complete"),
    "approve": (("plan_digest", "approval_token_digest"), ("acknowledged_pages", "render_pages"), ("status", "apply"), "approved"),
    "apply": (("cutover_terminal_digest", "post_cutover_census_digest", "apply_predecessor_digest"), ("policy_documents", "content_batches", "content_actions", "rebuild_kinds", "in_process_probes", "transport_probes"), ("status", "plan", "apply", "verify", "recover", "abort", "rollback", "retire-source"), "complete"),
    "verify": (("verification_basis_digest", "verification_terminal_digest"), ("positive_probes", "negative_probes", "passed_probes", "failed_probes"), ("status", "plan", "apply", "verify", "recover", "rollback", "retire-source"), "complete"),
    "recover": (("journal_digest", "recovery_terminal_digest"), ("classified_effects", "repaired_effects", "blocked_effects"), ("status", "plan", "apply", "verify", "recover", "abort", "rollback", "retire-source"), "repair-terminal"),
    "abort": (("prior_census_digest", "abort_terminal_digest"), ("restored_entries", "removed_candidates", "evidence_events"), ("status",), "aborted"),
    "rollback:nonterminal-contingency": (("cutover_plan_digest", "original_apply_journal_digest", "rollback_contingency_digest", "target_census_digest", "rollback_terminal_digest"), ("restored_entries", "retained_entries", "reapplied_entries", "discarded_entries", "rebuild_kinds", "verification_probes"), ("status", "plan", "recover", "verify", "rollback"), "rollback-complete"),
    "rollback:terminal-plan": (("rollback_plan_digest", "target_census_digest", "rollback_terminal_digest"), ("restored_entries", "retained_entries", "reapplied_entries", "discarded_entries", "rebuild_kinds", "verification_probes"), ("status", "plan", "recover", "verify", "rollback"), "rollback-complete"),
    "retire-source:clearance": (("retirement_plan_digest", "clearance_digest", "retirement_lifecycle_digest", "surviving_copy_ledger_digest"), ("survivor_rows", "verified_survivor_rows"), ("status", "retire-source"), "retirement-pending"),
    "retire-source:finalize": (("retirement_plan_digest", "retirement_lifecycle_digest", "completion_digest", "finalization_digest", "surviving_copy_ledger_digest"), ("completion_records", "survivor_rows"), ("status", "plan"), "retirement-finalize"),
}

PLAN_ENTRY_BRANCHES = {
    "reconcile": ("cutover",),
    "apply": ("rollback", "retirement"),
    "verify": ("rollback", "retirement"),
    "recover": ("rollback",),
    "rollback:nonterminal-contingency": ("rollback",),
    "rollback:terminal-plan": ("rollback",),
    "retire-source:finalize": ("rollback",),
}


def _cutover_options() -> dict[str, object]:
    return {
        "expected_reconciliation_digest": _digest("reconciliation"),
        "expected_policy_bundle_digest": _digest("policy"),
        "expected_principal_attestation_set_digest": _digest("principal"),
        "expected_verification_plan_digest": _digest("verification"),
        "expected_rollback_contingency_digest": _digest("contingency"),
        "expected_source_retention_digest": _digest("retention"),
        "expected_control_basis_digest": _digest("control"),
        "expected_rehearsal_proof_digest": _digest("rehearsal"),
    }


def _request(action: str) -> tuple[dict[str, object], str | None]:
    base: dict[str, object] = {
        "schema": "exomem.consolidate-memory-request/v1",
        "action": action.split(":", 1)[0],
    }
    if action == "start":
        return base | {"operation_id": OPERATION_ID, "expected_run_revision": 0, "run_mode": "real-cutover", "source_artifact_ref": "source", "source_attestation_ref": "attestation"}, "real-cutover"
    if action == "status":
        return base | {"run_id": RUN_ID, "expected_run_revision": 1, "detail": "summary"}, None
    mutation = {"operation_id": OPERATION_ID, "run_id": RUN_ID, "expected_run_revision": 0}
    context = {"successor_context_ref": "input-context", "successor_context_digest": _digest("input-context")}
    if action == "reconcile":
        return base | mutation | {"expected_inventory_digest": _digest("inventory"), "decision_set_ref": "decisions", "decision_set_digest": _digest("decisions")}, None
    if action.startswith("plan:"):
        request = base | mutation | context | {"plan_kind": "cutover"}
        variant = action.removeprefix("plan:")
        if variant == "materialize":
            return request | {"operation": "materialize", "cutover_options": _cutover_options()}, "real-cutover"
        step = variant.removeprefix("render-")
        request |= {"operation": "render", "render_step": step, "plan_digest": _digest("plan_digest")}
        if step in {"page", "acknowledge", "complete"}:
            request["render_session_ref"] = "render-session"
        if step in {"page", "acknowledge"}:
            request["page_ordinal"] = 0
        if step == "acknowledge":
            request["acknowledged_page_digest"] = _digest("render_page_digest")
        return request, None
    if action == "approve":
        return base | mutation | context | {"plan_kind": "cutover", "plan_digest": _digest("plan_digest"), "rendering_completeness_ref": "completeness", "rendering_completeness_digest": _digest("rendering_completeness_digest")}, None
    if action == "apply":
        return base | mutation | context | {"cutover_plan_digest": _digest("plan_digest"), "approval_token_ref": "token", "approval_token_digest": _digest("approval_token_digest")}, "real-cutover"
    if action == "verify":
        return base | mutation | {"verification_kind": "transport", "expected_plan_digest": _digest("plan_digest"), "expected_verification_basis_digest": _digest("verification-basis")}, "real-cutover"
    if action == "recover":
        return base | mutation | {"expected_journal_digest": _digest("journal")}, None
    if action == "abort":
        return base | mutation | {"expected_journal_digest": _digest("journal"), "reason_code": "owner-cancelled"}, None
    if action.startswith("rollback:"):
        mode = action.removeprefix("rollback:")
        request = base | mutation | context | {"rollback_mode": mode}
        if mode == "terminal-plan":
            request |= {"rollback_plan_digest": _digest("rollback_plan_digest"), "rollback_token_ref": "rollback-token", "rollback_token_digest": _digest("rollback-token")}
        return request, None
    if action == "retire-source:clearance":
        return base | mutation | context | {"phase": "clearance", "retirement_plan_digest": _digest("retirement_plan_digest"), "retirement_token_ref": "retirement-token", "retirement_token_digest": _digest("retirement-token")}, None
    if action == "retire-source:finalize":
        return base | mutation | {"phase": "finalize", "retirement_plan_digest": _digest("retirement_plan_digest"), "retirement_lifecycle_ref": "lifecycle", "completion_attestation_ref": "completion", "completion_attestation_digest": _digest("completion")}, None
    raise AssertionError(action)


def _request_digest(request: Mapping[str, object]) -> str:
    return hashlib.sha256(consolidation_plan.canonical_closed_jcs(request)).hexdigest()


def _successor(
    kind: str,
    facts: dict[str, object],
    *,
    run_revision: int = 1,
) -> tuple[successor.CanonicalSuccessorContext, successor.ExpectedSuccessorContext]:
    expected_successors = {
        "plan-materialize": ("plan", "materialize"),
        "render-begin": ("plan", "render-begin"),
        "render-page": ("plan", "render-page"),
        "render-acknowledge": ("plan", "render-acknowledge"),
        "render-complete": ("plan", "render-complete"),
        "approve": ("approve", "plan-kind"),
        "apply": ("apply", "cutover"),
        "rollback-terminal-plan": ("rollback", "terminal-plan"),
        "retire-source-clearance": ("retire-source", "clearance"),
        "rollback-nonterminal-contingency": ("rollback", "nonterminal-contingency"),
    }
    action, variant = expected_successors[kind]
    if kind == "approve":
        variant = str(facts["plan_kind"])
    basis = str(facts.get("plan_input_basis_digest", facts.get("original_apply_journal_digest", _digest("basis"))))
    expiry = PLAN_ENTRY_EXPIRES_AT if kind == "plan-materialize" else EXPIRES_AT
    owner = successor.SuccessorOwnerIdentity("vault", "installation", 1, _digest("fence"), _digest("principal"))
    owner_digest = successor.build_owner_binding(owner).digest
    seed = successor.build_seed(successor.SuccessorSeedInput(kind, RUN_ID, run_revision, _digest("destination"), owner_digest, basis, action, variant, ISSUED_AT, expiry, "nonce", facts))
    context = successor.derive_context(seed, predecessor_event_id=f"{_digest('event')}:committed", predecessor_payload_digest=_digest("payload"))
    expected = successor.ExpectedSuccessorContext(kind, RUN_ID, run_revision, _digest("destination"), owner_digest, basis, context.preimage["predecessor_event_id"], _digest("payload"), action, variant, facts, ISSUED_AT)
    return context, expected


def _terminal(action: str) -> tuple[dict[str, object], terminal.TerminalProjectionState]:
    digests, counts, next_actions, phase = ROWS[action]
    request, mode = _request(action)
    value: dict[str, object] = {
        "schema": terminal.TERMINAL_SCHEMA_NAME,
        "action": action,
        "outcome": "observed" if action == "status" else "committed",
        "run_id": RUN_ID,
        "run_revision": 1,
        "phase": phase,
        "artifact_digests": {key: _digest(key) for key in digests},
        "counts": {key: 0 for key in counts},
        "next_actions": list(next_actions),
        "trusted_outputs": {},
    }
    if action != "status":
        value.update({"operation_id": OPERATION_ID, "request_digest": _request_digest(request), "prior_state_digest": _digest("prior"), "final_state_digest": _digest("final")})

    context = None
    expected = None
    eligible = PLAN_ENTRY_BRANCHES.get(action)
    facts: dict[str, object] | None = None
    if eligible is not None:
        facts = {"eligible_plan_kinds": list(eligible), "plan_input_basis_digest": _digest(f"{action}-basis")}
        context, expected = _successor("plan-materialize", facts)
        value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest, "eligible_plan_kinds": list(eligible)}
    elif action == "plan:materialize":
        facts = {"plan_kind": "cutover", "plan_digest": value["artifact_digests"]["plan_digest"]}
        context, expected = _successor("render-begin", facts)
        value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest}
    elif action == "plan:render-begin":
        value["counts"].update({"render_pages": 2, "acknowledged_pages": 0})
        facts = {"plan_kind": "cutover", "plan_digest": value["artifact_digests"]["plan_digest"], "render_session_digest": value["artifact_digests"]["render_session_digest"], "page_ordinal": 0}
        context, expected = _successor("render-page", facts)
        value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest, "render_session_ref": "render-session"}
    elif action == "plan:render-page":
        value["counts"].update({"page_ordinal": 0, "render_pages": 2})
        facts = {"plan_kind": "cutover", "plan_digest": value["artifact_digests"]["plan_digest"], "render_session_digest": _digest("render-session"), "page_ordinal": 0, "page_digest": value["artifact_digests"]["render_page_digest"]}
        context, expected = _successor("render-acknowledge", facts)
        value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest, "render_session_ref": "render-session", "render_delivery_ref": "render-delivery"}
    elif action == "plan:render-acknowledge":
        value["counts"].update({"acknowledged_pages": 1, "render_pages": 2})
        facts = {"plan_kind": "cutover", "plan_digest": value["artifact_digests"]["plan_digest"], "render_session_digest": _digest("render-session"), "page_ordinal": 1}
        context, expected = _successor("render-page", facts)
        value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest, "render_session_ref": "render-session"}
    elif action == "plan:render-complete":
        value["counts"].update({"acknowledged_pages": 2, "render_pages": 2})
        facts = {"plan_kind": "cutover", "plan_digest": value["artifact_digests"]["plan_digest"], "rendering_completeness_digest": value["artifact_digests"]["rendering_completeness_digest"]}
        context, expected = _successor("approve", facts)
        value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest, "rendering_completeness_ref": "completeness"}
    elif action == "approve":
        value["counts"].update({"acknowledged_pages": 2, "render_pages": 2})
        facts = {"cutover_plan_digest": value["artifact_digests"]["plan_digest"], "approval_token_digest": value["artifact_digests"]["approval_token_digest"]}
        context, expected = _successor("apply", facts)
        value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest, "approval_token_ref": "approval-token"}
    elif action == "retire-source:clearance":
        value["trusted_outputs"] = {"retirement_clearance_ref": "clearance", "retirement_lifecycle_ref": "lifecycle"}

    eligibility = {}
    if "eligible_plan_kinds" in terminal.TerminalProjectionState.__dataclass_fields__:
        eligibility["eligible_plan_kinds"] = eligible
    state = terminal.TerminalProjectionState(
        request,
        copy.deepcopy(value),
        mode,
        "output-context" if context is not None else None,
        context,
        expected,
        **eligibility,
    )
    return value, state


@pytest.mark.parametrize("action", tuple(ROWS))
def test_all_closed_rows_bind_the_exact_coordinator_projection(action: str) -> None:
    value, state = _terminal(action)
    checked = terminal.validate_terminal(value, request=state)

    assert checked["action"] == action
    assert tuple(checked["next_actions"]) == tuple(value["next_actions"])
    assert not list(Draft202012Validator(terminal.terminal_schema()).iter_errors(value))


@pytest.mark.parametrize("action", tuple(action for action in ROWS if action != "status"))
@pytest.mark.parametrize("field", ("operation_id", "request_digest", "run_revision"))
def test_every_mutation_binds_identity_request_and_resulting_revision(action: str, field: str) -> None:
    value, state = _terminal(action)
    value[field] = OTHER_OPERATION_ID if field == "operation_id" else _digest("foreign-request") if field == "request_digest" else 2
    state = terminal.TerminalProjectionState(state.validated_request, value, state.trusted_run_mode, state.successor_reference, state.successor_context, state.expected_successor, eligible_plan_kinds=state.eligible_plan_kinds)

    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=state)


def test_status_binds_an_optional_expected_revision() -> None:
    value, state = _terminal("status")
    value["run_revision"] = 2
    state = terminal.TerminalProjectionState(state.validated_request, value)

    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=state)


@pytest.mark.parametrize(
    ("action", "bad_phase"),
    (("reconcile", "complete"), ("apply", "seal-intent"), ("verify", "probe"), ("recover", "classify"), ("rollback:nonterminal-contingency", "rolling-back"), ("rollback:terminal-plan", "rolling-back"), ("retire-source:finalize", "finalizing")),
)
def test_plan_entry_contexts_only_appear_at_exact_product_terminal_phases(action: str, bad_phase: str) -> None:
    value, state = _terminal(action)
    value["phase"] = bad_phase
    state = terminal.TerminalProjectionState(state.validated_request, value, state.trusted_run_mode, state.successor_reference, state.successor_context, state.expected_successor, eligible_plan_kinds=state.eligible_plan_kinds)

    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=state)


def test_apply_complete_rejects_a_valid_but_wrong_render_successor() -> None:
    value, state = _terminal("apply")
    context, expected = _successor("render-begin", {"plan_kind": "cutover", "plan_digest": _digest("plan_digest")})
    value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest}
    state = terminal.TerminalProjectionState(state.validated_request, value, "real-cutover", "output-context", context, expected)

    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=state)


def _contingency() -> tuple[successor.CanonicalSuccessorContext, successor.ExpectedSuccessorContext]:
    facts = {
        "original_apply_operation_id": OPERATION_ID,
        "original_apply_journal_digest": _digest("apply-journal"),
        "cutover_plan_digest": _digest("plan_digest"),
        "rollback_contingency_digest": _digest("contingency"),
        "publication_state_digest": _digest("publication"),
        "contingency_authority_ref": "contingency-authority",
        "contingency_authority_digest": _digest("authority"),
        "recovery_window_deadline": "2026-08-28T12:30:00.000Z",
    }
    return _successor("rollback-nonterminal-contingency", facts)


def test_apply_before_complete_only_accepts_an_explicitly_eligible_contingency() -> None:
    value, state = _terminal("apply")
    value["phase"] = "seal-intent"
    context, expected = _contingency()
    value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest}
    value["next_actions"] = [action for action in value["next_actions"] if action != "plan"]

    ineligible = terminal.TerminalProjectionState(state.validated_request, value, "real-cutover", "output-context", context, expected)
    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=ineligible)

    eligible = terminal.TerminalProjectionState(state.validated_request, value, "real-cutover", "output-context", context, expected, nonterminal_contingency_eligible=True)
    assert terminal.validate_terminal(value, request=eligible)["phase"] == "seal-intent"


def test_contingency_context_never_advertises_plan() -> None:
    value, state = _terminal("apply")
    value["phase"] = "seal-intent"
    context, expected = _contingency()
    value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest}
    state = terminal.TerminalProjectionState(state.validated_request, value, "real-cutover", "output-context", context, expected, nonterminal_contingency_eligible=True)

    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=state)


def test_status_context_requires_owner_detail_and_plan_requires_plan_entry_context() -> None:
    value, state = _terminal("status")
    facts = {"eligible_plan_kinds": ["rollback"], "plan_input_basis_digest": _digest("status-basis")}
    context, expected = _successor("plan-materialize", facts)
    value["phase"] = "complete"
    value["next_actions"] = ["status", "plan"]
    value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest, "eligible_plan_kinds": ["rollback"]}
    summary = terminal.TerminalProjectionState(state.validated_request, value, "real-cutover", "output-context", context, expected, eligible_plan_kinds=("rollback",))
    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=summary)

    owner_request = dict(state.validated_request) | {"detail": "owner-detail"}
    owner_state = terminal.TerminalProjectionState(owner_request, value, "real-cutover", "output-context", context, expected, eligible_plan_kinds=("rollback",))
    assert terminal.validate_terminal(value, request=owner_state)["phase"] == "complete"

    value["trusted_outputs"] = {}
    no_context = terminal.TerminalProjectionState(owner_request, value, "real-cutover")
    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=no_context)


def test_owner_detail_status_may_return_another_current_context_with_a_cursor() -> None:
    value, state = _terminal("status")
    facts = {"cutover_plan_digest": _digest("plan_digest"), "approval_token_digest": _digest("approval")}
    context, expected = _successor("apply", facts)
    request = dict(state.validated_request) | {"detail": "owner-detail"}
    value["next_actions"] = ["status", "apply"]
    value["trusted_outputs"] = {"next_cursor": "cursor", "successor_context_ref": "output-context", "successor_context_digest": context.digest}
    state = terminal.TerminalProjectionState(request, value, successor_reference="output-context", successor_context=context, expected_successor=expected)

    checked = terminal.validate_terminal(value, request=state)

    assert checked["trusted_outputs"]["next_cursor"] == "cursor"


@pytest.mark.parametrize(
    ("kind", "facts", "next_action", "contingency"),
    (
        ("render-begin", {"plan_kind": "cutover", "plan_digest": _digest("plan")}, "plan", False),
        ("render-page", {"plan_kind": "cutover", "plan_digest": _digest("plan"), "render_session_digest": _digest("session"), "page_ordinal": 0}, "plan", False),
        ("render-acknowledge", {"plan_kind": "cutover", "plan_digest": _digest("plan"), "render_session_digest": _digest("session"), "page_ordinal": 0, "page_digest": _digest("page")}, "plan", False),
        ("render-complete", {"plan_kind": "cutover", "plan_digest": _digest("plan"), "render_session_digest": _digest("session")}, "plan", False),
        ("approve", {"plan_kind": "cutover", "plan_digest": _digest("plan"), "rendering_completeness_digest": _digest("completeness")}, "approve", False),
        ("apply", {"cutover_plan_digest": _digest("plan"), "approval_token_digest": _digest("approval")}, "apply", False),
        ("rollback-terminal-plan", {"rollback_plan_digest": _digest("plan"), "rollback_token_digest": _digest("rollback-token")}, "rollback", False),
        ("rollback-nonterminal-contingency", {"original_apply_operation_id": OPERATION_ID, "original_apply_journal_digest": _digest("journal"), "cutover_plan_digest": _digest("plan"), "rollback_contingency_digest": _digest("contingency"), "publication_state_digest": _digest("publication"), "contingency_authority_ref": "authority", "contingency_authority_digest": _digest("authority"), "recovery_window_deadline": "2026-08-28T12:30:00.000Z"}, "rollback", True),
        ("retire-source-clearance", {"retirement_plan_digest": _digest("plan"), "retirement_token_digest": _digest("retirement-token")}, "retire-source", False),
    ),
)
def test_owner_detail_status_reexposes_each_current_successor_context(kind: str, facts: dict[str, object], next_action: str, contingency: bool) -> None:
    value, state = _terminal("status")
    context, expected = _successor(kind, facts)
    request = dict(state.validated_request) | {"detail": "owner-detail"}
    value["next_actions"] = ["status", next_action]
    value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest}
    state = terminal.TerminalProjectionState(request, value, successor_reference="output-context", successor_context=context, expected_successor=expected, nonterminal_contingency_eligible=contingency)

    assert terminal.validate_terminal(value, request=state)["trusted_outputs"]["successor_context_digest"] == context.digest


def test_owner_detail_status_rejects_plan_for_an_unrelated_current_context() -> None:
    value, state = _terminal("status")
    facts = {"cutover_plan_digest": _digest("plan"), "approval_token_digest": _digest("approval")}
    context, expected = _successor("apply", facts)
    request = dict(state.validated_request) | {"detail": "owner-detail"}
    value["next_actions"] = ["status", "plan", "apply"]
    value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest}
    state = terminal.TerminalProjectionState(request, value, successor_reference="output-context", successor_context=context, expected_successor=expected)

    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=state)


def test_terminal_eligible_kinds_match_canonical_context_and_process_state() -> None:
    value, state = _terminal("apply")
    value["trusted_outputs"]["eligible_plan_kinds"] = ["rollback"]
    changed = terminal.TerminalProjectionState(state.validated_request, value, state.trusted_run_mode, state.successor_reference, state.successor_context, state.expected_successor, eligible_plan_kinds=("rollback",))

    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=changed)


def test_retirement_plan_entry_requires_real_cutover() -> None:
    value, state = _terminal("apply")
    changed = terminal.TerminalProjectionState(state.validated_request, value, "cloned-rehearsal", state.successor_reference, state.successor_context, state.expected_successor, eligible_plan_kinds=state.eligible_plan_kinds)

    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=changed)


def test_verify_plan_entry_requires_transport_verification() -> None:
    value, state = _terminal("verify")
    request = dict(state.validated_request)
    request["verification_kind"] = "in-process"
    changed = terminal.TerminalProjectionState(request, value, state.trusted_run_mode, state.successor_reference, state.successor_context, state.expected_successor, eligible_plan_kinds=state.eligible_plan_kinds)

    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=changed)


@pytest.mark.parametrize(
    ("plan_kind", "successor_kind", "next_actions"),
    (("cutover", "apply", ["status", "apply"]), ("rollback", "rollback-terminal-plan", ["status", "rollback"]), ("retirement", "retire-source-clearance", ["status", "retire-source"])),
)
def test_approve_exactly_binds_plan_kind_successor_and_next_actions(plan_kind: str, successor_kind: str, next_actions: list[str]) -> None:
    value, state = _terminal("approve")
    request = dict(state.validated_request)
    request["plan_kind"] = plan_kind
    value["request_digest"] = _request_digest(request)
    value["next_actions"] = next_actions
    if successor_kind == "apply":
        facts = {"cutover_plan_digest": request["plan_digest"], "approval_token_digest": value["artifact_digests"]["approval_token_digest"]}
    elif successor_kind == "rollback-terminal-plan":
        facts = {"rollback_plan_digest": request["plan_digest"], "rollback_token_digest": value["artifact_digests"]["approval_token_digest"]}
    else:
        facts = {"retirement_plan_digest": request["plan_digest"], "retirement_token_digest": value["artifact_digests"]["approval_token_digest"]}
    context, expected = _successor(successor_kind, facts)
    value["trusted_outputs"] = {"successor_context_ref": "output-context", "successor_context_digest": context.digest, "approval_token_ref": "approval-token"}
    state = terminal.TerminalProjectionState(request, value, successor_reference="output-context", successor_context=context, expected_successor=expected)

    assert terminal.validate_terminal(value, request=state)["next_actions"] == tuple(next_actions)

    value["next_actions"] = ["status"]
    bad_state = terminal.TerminalProjectionState(request, value, successor_reference="output-context", successor_context=context, expected_successor=expected)
    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=bad_state)


def test_context_run_revision_is_the_resulting_terminal_revision() -> None:
    value, state = _terminal("plan:materialize")
    facts = {"plan_kind": "cutover", "plan_digest": value["artifact_digests"]["plan_digest"]}
    context, expected = _successor("render-begin", facts, run_revision=2)
    value["trusted_outputs"]["successor_context_digest"] = context.digest
    changed = terminal.TerminalProjectionState(state.validated_request, value, state.trusted_run_mode, "output-context", context, expected)

    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=changed)


def test_render_acknowledge_last_page_advances_to_render_complete() -> None:
    value, state = _terminal("plan:render-acknowledge")
    value["counts"]["render_pages"] = 1
    facts = {"plan_kind": "cutover", "plan_digest": value["artifact_digests"]["plan_digest"], "render_session_digest": _digest("render-session")}
    context, expected = _successor("render-complete", facts)
    value["trusted_outputs"]["successor_context_digest"] = context.digest
    state = terminal.TerminalProjectionState(state.validated_request, value, successor_reference="output-context", successor_context=context, expected_successor=expected)

    assert terminal.validate_terminal(value, request=state)["action"] == "plan:render-acknowledge"


def test_render_request_plan_digest_must_match_the_terminal_artifact() -> None:
    value, state = _terminal("plan:render-begin")
    request = dict(state.validated_request)
    request["plan_digest"] = _digest("foreign-plan")
    value["request_digest"] = _request_digest(request)
    changed = terminal.TerminalProjectionState(request, value, successor_reference=state.successor_reference, successor_context=state.successor_context, expected_successor=state.expected_successor)

    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=changed)


def test_schema_accepts_a_semantic_negative_but_projection_refuses_it() -> None:
    value, state = _terminal("start")
    changed = copy.deepcopy(value)
    changed["request_digest"] = _digest("foreign")

    assert not list(Draft202012Validator(terminal.terminal_schema()).iter_errors(changed))
    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(changed, request=state)


@pytest.mark.parametrize("action", ("start", "abort", "retire-source:clearance"))
def test_noncontext_rows_never_accept_a_forged_successor_pair(action: str) -> None:
    value, state = _terminal(action)
    value["trusted_outputs"].update({"successor_context_ref": "ref", "successor_context_digest": _digest("context")})
    changed = terminal.TerminalProjectionState(state.validated_request, value, state.trusted_run_mode)

    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.validate_terminal(value, request=changed)


class _UserMapping(Mapping[str, object]):
    def __init__(self, value: Mapping[str, object]) -> None:
        self._value = dict(value)

    def __getitem__(self, key: str) -> object:
        return self._value[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._value)

    def __len__(self) -> int:
        return len(self._value)


class _ForgedTerminal(terminal._ValidatedTerminal):
    def __getitem__(self, key: str) -> object:
        return {"action": "start", "secret": "owner-control-ref"}[key]

    def __iter__(self) -> Iterator[str]:
        return iter(("action", "secret"))

    def __len__(self) -> int:
        return 2


def test_success_envelope_rejects_an_unsealed_validated_terminal_subclass() -> None:
    forged = object.__new__(_ForgedTerminal)

    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.success_envelope(forged)


def test_success_envelope_is_exact_json_and_detached_from_custom_mappings() -> None:
    value, state = _terminal("start")
    artifacts = _UserMapping(value["artifact_digests"])
    value["artifact_digests"] = artifacts
    value["counts"] = _UserMapping(value["counts"])
    state = terminal.TerminalProjectionState(state.validated_request, value, state.trusted_run_mode)
    checked = terminal.validate_terminal(value, request=state)
    artifacts._value["source_fingerprint"] = _digest("changed-after-validation")

    envelope = terminal.success_envelope(checked)
    replayed = terminal.success_envelope(checked, delivery="replayed")
    encoded = json.dumps(envelope, sort_keys=True)

    assert json.loads(encoded) == envelope
    assert envelope["data"]["terminal"] == replayed["data"]["terminal"]
    assert replayed["data"]["delivery"] == "replayed"
    assert envelope["data"]["terminal"]["artifact_digests"]["source_fingerprint"] != artifacts["source_fingerprint"]
    assert set(envelope) == {"success", "data"}
    assert set(envelope["data"]) == {"delivery", "terminal"}
    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.success_envelope({"action": "apply", "outcome": "committed", "secret": "owner-control-ref"})
    status, status_state = _terminal("status")
    observed = terminal.validate_terminal(status, request=status_state)
    with pytest.raises(terminal.ConsolidationTerminalUnavailable):
        terminal.success_envelope(observed, delivery="replayed")
