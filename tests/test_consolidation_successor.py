from __future__ import annotations

import copy
import hashlib
from dataclasses import replace

import pytest

from exomem.governance import consolidation_successor as successor

RUN_ID = "00000000-0000-4000-8000-000000000011"
OPERATION_ID = "00000000-0000-4000-8000-000000000012"
ISSUED_AT = "2026-08-28T12:00:00.000Z"
EXPIRES_AT = "2026-08-28T12:15:00.000Z"
MAX_EXPIRES_AT = "9999-12-31T23:59:59.999Z"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _facts() -> dict[str, dict[str, object]]:
    return {
        "plan-materialize": {
            "eligible_plan_kinds": ["cutover", "rollback", "retirement"],
            "plan_input_basis_digest": _digest("plan-input-basis"),
        },
        "render-begin": {
            "plan_kind": "cutover",
            "plan_digest": _digest("plan"),
        },
        "render-page": {
            "plan_kind": "cutover",
            "plan_digest": _digest("plan"),
            "render_session_digest": _digest("render-session"),
            "page_ordinal": 3,
        },
        "render-acknowledge": {
            "plan_kind": "cutover",
            "plan_digest": _digest("plan"),
            "render_session_digest": _digest("render-session"),
            "page_ordinal": 3,
            "page_digest": _digest("page"),
        },
        "render-complete": {
            "plan_kind": "cutover",
            "plan_digest": _digest("plan"),
            "render_session_digest": _digest("render-session"),
        },
        "approve": {
            "plan_kind": "cutover",
            "plan_digest": _digest("plan"),
            "rendering_completeness_digest": _digest("completeness"),
        },
        "apply": {
            "cutover_plan_digest": _digest("plan"),
            "approval_token_digest": _digest("approval-token"),
        },
        "rollback-terminal-plan": {
            "rollback_plan_digest": _digest("rollback-plan"),
            "rollback_token_digest": _digest("rollback-token"),
        },
        "retire-source-clearance": {
            "retirement_plan_digest": _digest("retirement-plan"),
            "retirement_token_digest": _digest("retirement-token"),
        },
        "rollback-nonterminal-contingency": {
            "original_apply_operation_id": OPERATION_ID,
            "original_apply_journal_digest": _digest("apply-journal"),
            "cutover_plan_digest": _digest("plan"),
            "rollback_contingency_digest": _digest("contingency"),
            "publication_state_digest": _digest("publication-state"),
            "contingency_authority_ref": "exomem-consolidation-authority://opaque-001",
            "contingency_authority_digest": _digest("authority"),
            "recovery_window_deadline": EXPIRES_AT,
        },
    }


EXPECTED_SUCCESSORS = {
    "plan-materialize": ("plan", "materialize"),
    "render-begin": ("plan", "render-begin"),
    "render-page": ("plan", "render-page"),
    "render-acknowledge": ("plan", "render-acknowledge"),
    "render-complete": ("plan", "render-complete"),
    "approve": ("approve", "cutover"),
    "apply": ("apply", "cutover"),
    "rollback-terminal-plan": ("rollback", "terminal-plan"),
    "retire-source-clearance": ("retire-source", "clearance"),
    "rollback-nonterminal-contingency": ("rollback", "nonterminal-contingency"),
}


def _owner() -> successor.SuccessorOwnerIdentity:
    return successor.SuccessorOwnerIdentity(
        vault_id="vault-destination-01",
        installation_id="installation-destination-01",
        generation=7,
        active_fence_digest=_digest("active-fence"),
        principal_digest=_digest("owner-principal"),
    )


def _seed_input(kind: str) -> successor.SuccessorSeedInput:
    action, variant = EXPECTED_SUCCESSORS[kind]
    facts = _facts()[kind]
    basis = (
        facts["plan_input_basis_digest"]
        if kind == "plan-materialize"
        else (
            facts["original_apply_journal_digest"]
            if kind == "rollback-nonterminal-contingency"
            else _digest("plan-control-basis")
        )
    )
    return successor.SuccessorSeedInput(
        context_kind=kind,
        run_id=RUN_ID,
        run_revision=9,
        destination_binding_digest=_digest("destination-binding"),
        owner_binding_digest=successor.build_owner_binding(_owner()).digest,
        basis_digest=basis,
        successor_action=action,
        successor_variant=variant,
        issued_at=ISSUED_AT,
        expires_at=MAX_EXPIRES_AT if kind == "plan-materialize" else EXPIRES_AT,
        nonce="context-00000000000000000001",
        facts=facts,
    )


def _context(kind: str = "render-page") -> successor.CanonicalSuccessorContext:
    seed = successor.build_seed(_seed_input(kind))
    return successor.derive_context(
        seed,
        predecessor_event_id=f"{_digest('predecessor')}:committed",
        predecessor_payload_digest=_digest("predecessor-payload"),
    )


def test_owner_binding_has_one_stable_cross_adapter_vector() -> None:
    binding = successor.build_owner_binding(_owner())

    assert set(binding.preimage) == {
        "schema",
        "vault_id",
        "installation_id",
        "generation",
        "active_fence_digest",
        "principal_digest",
        "purpose",
    }
    assert "session" not in binding.canonical_bytes.decode()
    assert "surface" not in binding.canonical_bytes.decode()
    assert binding.digest == "26c95d1058acc5f5e1d2173c81c137926aac649fb5a3912c9e2e25c43ffc9f07"
    assert successor.parse_owner_binding(binding.canonical_bytes) == binding


@pytest.mark.parametrize("kind", tuple(EXPECTED_SUCCESSORS))
def test_all_ten_seed_and_context_branches_are_closed_and_acyclic(kind: str) -> None:
    seed = successor.build_seed(_seed_input(kind))
    context = successor.derive_context(
        seed,
        predecessor_event_id=f"{_digest('predecessor')}:committed",
        predecessor_payload_digest=_digest("predecessor-payload"),
    )

    assert set(seed.preimage) == {
        "schema",
        "context_schema",
        "context_kind",
        "run_id",
        "run_revision",
        "destination_binding_digest",
        "owner_binding_digest",
        "basis_digest",
        "successor_action",
        "successor_variant",
        "issued_at",
        "expires_at",
        "nonce",
        "facts",
    }
    assert set(context.preimage) == {
        "schema",
        "context_kind",
        "run_id",
        "run_revision",
        "destination_binding_digest",
        "owner_binding_digest",
        "basis_digest",
        "context_seed_digest",
        "predecessor_event_id",
        "predecessor_payload_digest",
        "successor_action",
        "successor_variant",
        "issued_at",
        "expires_at",
        "nonce",
        "facts",
    }
    assert context.preimage["context_seed_digest"] == seed.digest
    assert context.preimage["facts"] == seed.preimage["facts"]
    for forbidden in (
        b"predecessor_event_id",
        b"predecessor_payload_digest",
        b"context_seed_digest",
        b"successor_context_digest",
        b"receipt",
    ):
        assert forbidden not in seed.canonical_bytes
    assert successor.parse_seed(seed.canonical_bytes) == seed
    assert successor.parse_context(context.canonical_bytes) == context


def test_seed_and_full_context_fixed_vectors_bind_only_s_then_p_then_context() -> None:
    seed = successor.build_seed(_seed_input("render-acknowledge"))
    context = successor.derive_context(
        seed,
        predecessor_event_id=f"{_digest('predecessor')}:committed",
        predecessor_payload_digest=_digest("predecessor-payload"),
    )
    changed = successor.derive_context(
        seed,
        predecessor_event_id=f"{_digest('predecessor')}:committed",
        predecessor_payload_digest=_digest("changed-payload"),
    )

    assert seed.digest == "0a615d242d9576f9af671cce044de065849ca93a12947e15ade78a52cc92ef7b"
    assert context.digest == "4dec5d89418dd027897758a9a941d155c3d05cd5f4ecf65157964ad033a90903"
    assert changed.digest != context.digest
    assert changed.seed == seed
    assert seed.digest in context.canonical_bytes.decode()
    assert context.digest not in context.canonical_bytes.decode()


@pytest.mark.parametrize("kind", tuple(EXPECTED_SUCCESSORS))
def test_context_verification_binds_current_owner_destination_predecessor_and_facts(
    kind: str,
) -> None:
    context = _context(kind)
    expected = successor.ExpectedSuccessorContext(
        context_kind=kind,
        run_id=RUN_ID,
        run_revision=9,
        destination_binding_digest=_digest("destination-binding"),
        owner_binding_digest=successor.build_owner_binding(_owner()).digest,
        basis_digest=context.preimage["basis_digest"],
        predecessor_event_id=context.preimage["predecessor_event_id"],
        predecessor_payload_digest=context.preimage["predecessor_payload_digest"],
        successor_action=EXPECTED_SUCCESSORS[kind][0],
        successor_variant=EXPECTED_SUCCESSORS[kind][1],
        facts=_facts()[kind],
        verified_at=ISSUED_AT,
    )

    assert successor.verify_context(context, expected=expected) == context
    for field, value in (
        ("run_revision", 10),
        ("destination_binding_digest", _digest("other-destination")),
        ("owner_binding_digest", _digest("other-owner")),
        ("basis_digest", _digest("other-basis")),
        ("predecessor_event_id", f"{_digest('other-event')}:committed"),
        ("successor_variant", "wrong-variant"),
        ("facts", {"unexpected": _digest("fact")}),
    ):
        with pytest.raises(successor.SuccessorContextUnavailable):
            successor.verify_context(context, expected=replace(expected, **{field: value}))


def test_context_expiry_is_fixed_and_capped_without_status_extension() -> None:
    assert (
        successor.context_expiry(
            context_kind="plan-materialize",
            issued_at=ISSUED_AT,
            ttl_ms=1,
            deadline=None,
        )
        == MAX_EXPIRES_AT
    )
    assert (
        successor.context_expiry(
            context_kind="render-page",
            issued_at=ISSUED_AT,
            ttl_ms=900_000,
            deadline="2026-08-28T12:05:00.000Z",
        )
        == "2026-08-28T12:05:00.000Z"
    )
    assert (
        successor.context_expiry(
            context_kind="render-page",
            issued_at=ISSUED_AT,
            ttl_ms=60_000,
            deadline=EXPIRES_AT,
        )
        == "2026-08-28T12:01:00.000Z"
    )
    for ttl in (0, 86_400_001):
        with pytest.raises(successor.SuccessorContextUnavailable):
            successor.context_expiry(
                context_kind="render-page",
                issued_at=ISSUED_AT,
                ttl_ms=ttl,
                deadline=EXPIRES_AT,
            )
    with pytest.raises(successor.SuccessorContextUnavailable):
        successor.context_expiry(
            context_kind="render-page",
            issued_at="9999-12-31T23:59:59.999Z",
            ttl_ms=1,
            deadline="9999-12-31T23:59:59.999Z",
        )


def test_expired_or_noncanonical_contexts_fail_content_free() -> None:
    context = _context()
    expected = successor.ExpectedSuccessorContext(
        context_kind="render-page",
        run_id=RUN_ID,
        run_revision=9,
        destination_binding_digest=_digest("destination-binding"),
        owner_binding_digest=successor.build_owner_binding(_owner()).digest,
        basis_digest=_digest("plan-control-basis"),
        predecessor_event_id=context.preimage["predecessor_event_id"],
        predecessor_payload_digest=context.preimage["predecessor_payload_digest"],
        successor_action="plan",
        successor_variant="render-page",
        facts=_facts()["render-page"],
        verified_at=EXPIRES_AT,
    )
    with pytest.raises(successor.SuccessorContextUnavailable) as caught:
        successor.verify_context(context, expected=expected)
    assert str(caught.value) == "successor context is unavailable"

    raw = context.canonical_bytes[:-1] + b',"schema":"exomem.consolidation-successor-context/v1"}'
    with pytest.raises(successor.SuccessorContextUnavailable):
        successor.parse_context(raw)


def test_branch_facts_are_exact_and_semantic_basis_is_enforced() -> None:
    for kind in EXPECTED_SUCCESSORS:
        value = _seed_input(kind)
        extra = copy.deepcopy(value.facts)
        extra["unexpected"] = _digest("unexpected")
        with pytest.raises(successor.SuccessorContextUnavailable):
            successor.build_seed(replace(value, facts=extra))

    plan_entry = _seed_input("plan-materialize")
    with pytest.raises(successor.SuccessorContextUnavailable):
        successor.build_seed(replace(plan_entry, basis_digest=_digest("wrong-basis")))
    contingency = _seed_input("rollback-nonterminal-contingency")
    with pytest.raises(successor.SuccessorContextUnavailable):
        successor.build_seed(replace(contingency, basis_digest=_digest("wrong-basis")))


@pytest.mark.parametrize(
    "eligible",
    [[], ["rollback", "cutover"], ["cutover", "cutover"], ["unknown"]],
)
def test_plan_entry_eligible_kinds_are_one_fixed_nonempty_subset(
    eligible: list[str],
) -> None:
    value = _seed_input("plan-materialize")
    facts = copy.deepcopy(value.facts)
    facts["eligible_plan_kinds"] = eligible
    with pytest.raises(successor.SuccessorContextUnavailable):
        successor.build_seed(replace(value, facts=facts))
