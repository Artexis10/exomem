"""Closed, deterministic receipt evidence for consolidation effects."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

RUN_ID = "00000000-0000-4000-8000-000000000091"
OPERATION_ID = "00000000-0000-4000-8000-000000000092"
REQUEST_DIGEST = hashlib.sha256(b"request").hexdigest()
PRIOR_DIGEST = hashlib.sha256(b"prior").hexdigest()
PREPARED_DIGEST = hashlib.sha256(b"prepared").hexdigest()
TARGET_DIGEST = hashlib.sha256(b"target").hexdigest()
OBSERVED_DIGEST = hashlib.sha256(b"observed").hexdigest()
PARENT_EVENT_ID = f"{'d' * 64}:committed"
PARENT_PAYLOAD_DIGEST = "e" * 64
T0 = "2026-08-30T10:00:00Z"
T1 = "2026-08-30T10:00:01Z"


@pytest.fixture(autouse=True)
def receipt_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        str(tmp_path / "writer-state"),
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    return root


_EVIDENCE_FIELDS = {
    "start": ("identity_binding_digest", "run_request_digest"),
    "intake": ("archive_attestation_digest", "intake_manifest_digest"),
    "content-batch": ("batch_manifest_digest", "classification_digest"),
    "complete": ("completion_digest", "verification_basis_digest"),
    "render-page": (
        "impact_summary_digest",
        "page_digest",
        "plan_digest",
        "render_session_digest",
    ),
    "render-ack": (
        "acknowledgement_digest",
        "impact_summary_digest",
        "page_digest",
        "plan_digest",
        "render_session_digest",
    ),
    "retirement-consume": (
        "clearance_jti_digest",
        "destination_proof_digest",
        "disposition_digest",
        "source_checkpoint_digest",
        "source_fence_digest",
        "verifier_decision_digest",
    ),
    "retirement-completion": (
        "authentication_proof_digest",
        "completion_attestation_digest",
        "disposition_digest",
        "source_consume_event_digest",
        "source_receipt_head_digest",
    ),
    "rebuild-kind": ("rebuild_basis_digest", "rebuild_result_digest"),
    "in-process-probe": (
        "probe_digest",
        "probe_result_digest",
        "verification_basis_digest",
    ),
}


def _evidence(kind: str):
    from exomem.governance import consolidation_receipts

    return consolidation_receipts.build_evidence(
        kind=kind,
        digests={
            field: hashlib.sha256(f"{kind}:{field}".encode()).hexdigest()
            for field in _EVIDENCE_FIELDS[kind]
        },
    )


def _intent(**overrides: object):
    from exomem.governance import consolidation_receipts

    values: dict[str, object] = {
        "kind": "content-batch",
        "run_id": RUN_ID,
        "operation_id": OPERATION_ID,
        "phase": "publishing",
        "effect_ordinal": 7,
        "batch_ordinal": 2,
        "request_digest": REQUEST_DIGEST,
        "prior_digest": PRIOR_DIGEST,
        "prepared_digest": PREPARED_DIGEST,
        "target_digest": TARGET_DIGEST,
        "semantic_parent_event_id": PARENT_EVENT_ID,
        "semantic_parent_payload_digest": PARENT_PAYLOAD_DIGEST,
    }
    values.update(overrides)
    values.setdefault("evidence", _evidence(str(values["kind"])))
    return consolidation_receipts.build_intent(**values)


def _append_start(vault: Path):
    from exomem.governance import consolidation_receipts

    root_id, root_digest = consolidation_receipts.semantic_root()
    start = consolidation_receipts.build_intent(
        kind="start",
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        phase="intake",
        effect_ordinal=0,
        request_digest=REQUEST_DIGEST,
        prior_digest=PRIOR_DIGEST,
        target_digest=TARGET_DIGEST,
        evidence=_evidence("start"),
        semantic_parent_event_id=root_id,
        semantic_parent_payload_digest=root_digest,
    )
    start_record = consolidation_receipts.append_intent(vault, start, timestamp=T0)
    return consolidation_receipts.append_terminal(
        vault,
        intent_event_id=start_record["event_id"],
        role="committed",
        observed_digest=TARGET_DIGEST,
        timestamp=T0,
    )


def _appendable_intent(vault: Path):
    from exomem.governance import consolidation_receipts

    parent = _append_start(vault)
    return consolidation_receipts.build_intent(
        kind="intake",
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        phase="intake",
        effect_ordinal=1,
        request_digest=REQUEST_DIGEST,
        prior_digest=PRIOR_DIGEST,
        target_digest=TARGET_DIGEST,
        evidence=_evidence("intake"),
        semantic_parent_event_id=parent["event_id"],
        semantic_parent_payload_digest=parent["consolidation_event"][
            "payload_digest"
        ],
    )


def test_intent_identity_and_payload_digest_are_exact_framed_jcs_vectors() -> None:
    from exomem.governance import consolidation_plan, consolidation_receipts

    event = _intent()
    payload_without_digest = dict(event.payload)
    payload_without_digest.pop("payload_digest")
    payload_bytes = consolidation_plan.canonical_closed_jcs(payload_without_digest)
    payload_domain = b"exomem.consolidation-event-payload/content-batch/intent/v1"
    expected_payload_digest = hashlib.sha256(
        len(payload_domain).to_bytes(4, "big")
        + payload_domain
        + len(payload_bytes).to_bytes(8, "big")
        + payload_bytes
    ).hexdigest()
    identity_preimage = dict(payload_without_digest)
    identity_preimage.pop("evidence")
    identity_bytes = consolidation_plan.canonical_closed_jcs(identity_preimage)
    identity_domain = b"exomem.consolidation-event-id/content-batch/v1"
    expected_event_id = hashlib.sha256(
        len(identity_domain).to_bytes(4, "big")
        + identity_domain
        + len(identity_bytes).to_bytes(8, "big")
        + identity_bytes
    ).hexdigest()

    assert event.payload_digest == expected_payload_digest
    assert event.event_id == expected_event_id
    assert event.payload_digest == (
        "3aad76e3f306762c21781dc5d8f4d56e7265f3676d8d76dd80cf0557c7360138"
    )
    assert event.event_id == (
        "23bfe075deeb12e20d8db33b9fcbcfd7ff2c817dd081048308da30e7db48ecf3"
    )
    assert consolidation_receipts.semantic_root() == (
        "28db6cc424d3332bdfff97ecf3238eda5982c82c92a9d0d11bca1ec0e92fdd3d",
        "28db6cc424d3332bdfff97ecf3238eda5982c82c92a9d0d11bca1ec0e92fdd3d",
    )
    assert event.phase == "intent"
    assert _intent() == event


def test_every_kind_binds_its_owner_protected_evidence_bundle() -> None:
    from exomem.governance import consolidation_plan

    event = _intent()
    evidence = dict(_evidence("content-batch"))
    domain = b"exomem.consolidation-event-evidence/content-batch/v1"
    encoded = consolidation_plan.canonical_closed_jcs(evidence)
    expected = hashlib.sha256(
        len(domain).to_bytes(4, "big")
        + domain
        + len(encoded).to_bytes(8, "big")
        + encoded
    ).hexdigest()
    assert event.payload["evidence"] == evidence
    assert event.payload["evidence_digest"] == expected


def test_terminal_suffix_ids_and_role_payload_digests_are_fixed_vectors() -> None:
    from exomem.governance import consolidation_receipts

    intent = _intent()
    committed = consolidation_receipts.build_terminal(
        intent,
        role="committed",
        observed_digest=OBSERVED_DIGEST,
    )
    aborted = consolidation_receipts.build_terminal(
        intent,
        role="aborted",
        observed_digest=PRIOR_DIGEST,
    )

    assert committed.event_id == f"{intent.event_id}:committed"
    assert committed.payload_digest == (
        "18402f4c67a9a94278af2b05a7255033ae96628f1d02308e5c1224610da32e19"
    )
    assert aborted.event_id == f"{intent.event_id}:aborted"
    assert aborted.payload_digest == (
        "62649628fcea42657d6814fba771baaff0c1299a2fcb181d5f3793fee4b34eb8"
    )


def test_receipt_envelope_keeps_physical_prev_separate_from_semantic_parent(
    vault: Path,
) -> None:
    from exomem.governance import consolidation_receipts, receipts

    intent = consolidation_receipts.append_intent(
        vault,
        _appendable_intent(vault),
        timestamp=T0,
    )
    unrelated = receipts.append_event(
        vault,
        event_type="disclosure",
        payload={"outcomes": []},
        timestamp=T0,
    )
    terminal = consolidation_receipts.append_terminal(
        vault,
        intent_event_id=intent["event_id"],
        role="committed",
        observed_digest=OBSERVED_DIGEST,
        timestamp=T1,
    )

    nested = terminal["consolidation_event"]
    assert terminal["schema"] == "receipt/v1"
    assert terminal["event_type"] == "consolidation"
    assert terminal["event_id"] == f"{intent['event_id']}:committed"
    assert terminal["prev"] == unrelated["hash"]
    assert nested["semantic_parent_event_id"] == intent["event_id"]
    assert nested["semantic_parent_payload_digest"] == intent["consolidation_event"][
        "payload_digest"
    ]
    assert nested["observed_digest"] == OBSERVED_DIGEST
    assert receipts.verify_chain(vault)["valid"] is True


def test_identical_intent_and_terminal_retries_adopt_the_same_records(
    vault: Path,
) -> None:
    from exomem.governance import consolidation_receipts, receipts

    event = _appendable_intent(vault)
    first_intent = consolidation_receipts.append_intent(vault, event, timestamp=T0)
    replayed_intent = consolidation_receipts.append_intent(vault, event, timestamp=T0)
    first_terminal = consolidation_receipts.append_terminal(
        vault,
        intent_event_id=first_intent["event_id"],
        role="committed",
        observed_digest=OBSERVED_DIGEST,
        timestamp=T1,
    )
    replayed_terminal = consolidation_receipts.append_terminal(
        vault,
        intent_event_id=first_intent["event_id"],
        role="committed",
        observed_digest=OBSERVED_DIGEST,
        timestamp=T1,
    )

    assert replayed_intent == first_intent
    assert replayed_terminal == first_terminal
    assert len(receipts.event_records(vault)) == 4


def test_terminal_requires_its_exact_intent_and_refuses_competing_outcome(
    vault: Path,
) -> None:
    from exomem.governance import consolidation_receipts

    with pytest.raises(
        consolidation_receipts.ConsolidationReceiptUnavailable,
        match="^CONSOLIDATION_RECEIPT_UNAVAILABLE$",
    ):
        consolidation_receipts.append_terminal(
            vault,
            intent_event_id="a" * 64,
            role="committed",
            observed_digest=OBSERVED_DIGEST,
        )

    intent = consolidation_receipts.append_intent(
        vault,
        _appendable_intent(vault),
        timestamp=T0,
    )
    consolidation_receipts.append_terminal(
        vault,
        intent_event_id=intent["event_id"],
        role="committed",
        observed_digest=OBSERVED_DIGEST,
        timestamp=T1,
    )
    with pytest.raises(
        consolidation_receipts.ConsolidationReceiptUnavailable,
        match="^CONSOLIDATION_RECEIPT_UNAVAILABLE$",
    ):
        consolidation_receipts.append_terminal(
            vault,
            intent_event_id=intent["event_id"],
            role="aborted",
            observed_digest=PRIOR_DIGEST,
        )


def test_low_level_writer_cannot_bypass_terminal_intent_causality(vault: Path) -> None:
    from exomem.governance import consolidation_receipts, receipts

    intent = _appendable_intent(vault)
    terminal = consolidation_receipts.build_terminal(
        intent,
        role="committed",
        observed_digest=OBSERVED_DIGEST,
    )
    with pytest.raises(receipts.ReceiptError):
        receipts.append_event(
            vault,
            event_type="consolidation",
            phase="committed",
            event_id=terminal.event_id,
            payload={"consolidation_event": dict(terminal.payload)},
            critical=True,
        )

    consolidation_receipts.append_intent(vault, intent, timestamp=T0)
    written = receipts.append_event(
        vault,
        event_type="consolidation",
        phase="committed",
        event_id=terminal.event_id,
        payload={"consolidation_event": dict(terminal.payload)},
        timestamp=T1,
        critical=True,
    )
    assert written["event_id"] == terminal.event_id


def test_low_level_writer_refuses_a_nondurable_consolidation_record(
    vault: Path,
) -> None:
    from exomem.governance import receipts

    intent = _intent()
    with pytest.raises(receipts.ReceiptError):
        receipts.append_event(
            vault,
            event_type="consolidation",
            phase="intent",
            event_id=intent.event_id,
            payload={"consolidation_event": dict(intent.payload)},
            critical=False,
        )


def test_nonstart_intent_requires_its_exact_local_semantic_parent(
    vault: Path,
) -> None:
    from exomem.governance import consolidation_receipts

    with pytest.raises(
        consolidation_receipts.ConsolidationReceiptUnavailable,
        match="^CONSOLIDATION_RECEIPT_UNAVAILABLE$",
    ):
        consolidation_receipts.append_intent(vault, _intent())


def test_intent_append_recomputes_the_deterministic_outer_identity(vault: Path) -> None:
    from exomem.governance import consolidation_receipts

    event = _intent()
    forged = consolidation_receipts.ConsolidationEvent(
        event_id="a" * 64,
        phase=event.phase,
        payload=event.payload,
        payload_digest=event.payload_digest,
    )
    with pytest.raises(
        consolidation_receipts.ConsolidationReceiptUnavailable,
        match="^CONSOLIDATION_RECEIPT_UNAVAILABLE$",
    ):
        consolidation_receipts.append_intent(vault, forged)


def test_successor_seed_is_allowed_only_on_registered_producer_kinds() -> None:
    from exomem.governance import consolidation_receipts

    seed = hashlib.sha256(b"successor-seed").hexdigest()
    producer = _intent(
        kind="complete",
        phase="complete",
        batch_ordinal=None,
        prepared_digest=None,
        successor_context_seed_digest=seed,
    )
    terminal = consolidation_receipts.build_terminal(
        producer,
        role="committed",
        observed_digest=OBSERVED_DIGEST,
    )
    assert producer.payload["successor_context_seed_digest"] == seed
    assert terminal.payload["successor_context_seed_digest"] == seed

    with pytest.raises(
        consolidation_receipts.ConsolidationReceiptUnavailable,
        match="^CONSOLIDATION_RECEIPT_UNAVAILABLE$",
    ):
        _intent(successor_context_seed_digest=seed)


def test_complete_requires_a_successor_context_seed() -> None:
    from exomem.governance import consolidation_receipts

    with pytest.raises(
        consolidation_receipts.ConsolidationReceiptUnavailable,
        match="^CONSOLIDATION_RECEIPT_UNAVAILABLE$",
    ):
        _intent(
            kind="complete",
            phase="complete",
            batch_ordinal=None,
            prepared_digest=None,
        )


def test_render_page_after_matching_ack_advances_to_the_next_page() -> None:
    from exomem.governance import consolidation_receipts

    ack_intent = _intent(
        kind="render-ack",
        phase="rendering",
        effect_ordinal=7,
        batch_ordinal=None,
        prepared_digest=None,
        page_ordinal=0,
    )
    ack = consolidation_receipts.build_terminal(
        ack_intent,
        role="committed",
        observed_digest=OBSERVED_DIGEST,
    )
    page = _intent(
        kind="render-page",
        phase="rendering",
        effect_ordinal=8,
        batch_ordinal=None,
        prepared_digest=None,
        page_ordinal=1,
        semantic_parent_event_id=ack.event_id,
        semantic_parent_payload_digest=ack.payload_digest,
    )

    consolidation_receipts.validate_outer_append(
        [
            {
                "event_type": "consolidation",
                "phase": "committed",
                "event_id": ack.event_id,
                "consolidation_event": dict(ack.payload),
            }
        ],
        event_id=page.event_id,
        outer_phase="intent",
        nested=page.payload,
    )


def test_kind_specific_evidence_is_closed_and_hashed_by_the_builder() -> None:
    from exomem.governance import consolidation_receipts

    evidence = consolidation_receipts.build_evidence(
        kind="content-batch",
        digests={
            "batch_manifest_digest": hashlib.sha256(b"batch-manifest").hexdigest(),
            "classification_digest": hashlib.sha256(b"classification").hexdigest(),
        },
    )
    assert evidence == {
        "schema": "exomem.consolidation-event-evidence/content-batch/v1",
        "kind": "content-batch",
        "batch_manifest_digest": hashlib.sha256(b"batch-manifest").hexdigest(),
        "classification_digest": hashlib.sha256(b"classification").hexdigest(),
    }

    with pytest.raises(
        consolidation_receipts.ConsolidationReceiptUnavailable,
        match="^CONSOLIDATION_RECEIPT_UNAVAILABLE$",
    ):
        consolidation_receipts.build_evidence(
            kind="content-batch",
            digests={"batch_manifest_digest": "a" * 64},
        )


def test_only_start_can_name_the_fixed_semantic_root() -> None:
    from exomem.governance import consolidation_receipts

    root_id, root_digest = consolidation_receipts.semantic_root()
    start = consolidation_receipts.build_intent(
        kind="start",
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        phase="intake",
        effect_ordinal=0,
        request_digest=REQUEST_DIGEST,
        prior_digest=PRIOR_DIGEST,
        target_digest=TARGET_DIGEST,
        evidence=_evidence("start"),
        semantic_parent_event_id=root_id,
        semantic_parent_payload_digest=root_digest,
    )
    assert start.payload["semantic_parent_event_id"] == root_id

    with pytest.raises(
        consolidation_receipts.ConsolidationReceiptUnavailable,
        match="^CONSOLIDATION_RECEIPT_UNAVAILABLE$",
    ):
        consolidation_receipts.build_intent(
            kind="content-batch",
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            phase="publishing",
            effect_ordinal=1,
            batch_ordinal=0,
            prepared_digest=PREPARED_DIGEST,
            request_digest=REQUEST_DIGEST,
            prior_digest=PRIOR_DIGEST,
            target_digest=TARGET_DIGEST,
            evidence=_evidence("content-batch"),
            semantic_parent_event_id=root_id,
            semantic_parent_payload_digest=root_digest,
        )


def test_content_batch_cannot_skip_policy_and_name_start_as_its_parent(
    vault: Path,
) -> None:
    from exomem.governance import consolidation_receipts

    parent = _append_start(vault)
    event = _intent(
        batch_ordinal=0,
        effect_ordinal=1,
        semantic_parent_event_id=parent["event_id"],
        semantic_parent_payload_digest=parent["consolidation_event"][
            "payload_digest"
        ],
    )
    with pytest.raises(
        consolidation_receipts.ConsolidationReceiptUnavailable,
        match="^CONSOLIDATION_RECEIPT_UNAVAILABLE$",
    ):
        consolidation_receipts.append_intent(vault, event)


@pytest.mark.parametrize("kind", ("retirement-consume", "retirement-completion"))
def test_cross_chain_retirement_parent_needs_authenticated_intake_authority(
    vault: Path,
    kind: str,
) -> None:
    from exomem.governance import consolidation_receipts

    event = consolidation_receipts.build_intent(
        kind=kind,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        phase="retirement",
        effect_ordinal=1,
        request_digest=REQUEST_DIGEST,
        prior_digest=PRIOR_DIGEST,
        target_digest=TARGET_DIGEST,
        evidence=_evidence(kind),
        semantic_parent_event_id=PARENT_EVENT_ID,
        semantic_parent_payload_digest=PARENT_PAYLOAD_DIGEST,
    )
    with pytest.raises(
        consolidation_receipts.ConsolidationReceiptUnavailable,
        match="^CONSOLIDATION_RECEIPT_UNAVAILABLE$",
    ):
        consolidation_receipts.append_intent(vault, event)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.update(path="Knowledge Base/Notes/private.md"),
        lambda value: value.update(record_role="aborted"),
        lambda value: value.pop("observed_digest", None),
        lambda value: value.update(page_ordinal=0),
        lambda value: value.update(batch_ordinal=-1),
        lambda value: value.update(payload_digest="f" * 64),
    ),
)
def test_nested_terminal_schema_rejects_unknown_role_ordinal_and_digest_changes(
    mutation,
) -> None:
    from exomem.governance import consolidation_receipts

    intent = _intent()
    terminal = consolidation_receipts.build_terminal(
        intent,
        role="committed",
        observed_digest=OBSERVED_DIGEST,
    )
    changed = copy.deepcopy(dict(terminal.payload))
    mutation(changed)

    with pytest.raises(
        consolidation_receipts.ConsolidationReceiptUnavailable,
        match="^CONSOLIDATION_RECEIPT_UNAVAILABLE$",
    ):
        consolidation_receipts.validate_nested(changed, outer_phase="committed")


def test_receipt_writer_rejects_a_nested_schema_masquerading_as_the_envelope(
    vault: Path,
) -> None:
    from exomem.governance import receipts

    with pytest.raises(receipts.ReceiptError):
        receipts.append_event(
            vault,
            event_type="consolidation",
            phase="intent",
            event_id="a" * 64,
            payload={
                "schema": "exomem.consolidation-event/start/v1",
                "kind": "start",
            },
            critical=True,
        )


@pytest.mark.parametrize(
    ("kind", "ordinal_name"),
    (
        ("content-batch", "batch_ordinal"),
        ("rebuild-kind", "rebuild_ordinal"),
        ("in-process-probe", "probe_ordinal"),
        ("render-page", "page_ordinal"),
    ),
)
def test_each_specialized_effect_requires_only_its_registered_ordinal(
    kind: str,
    ordinal_name: str,
) -> None:
    from exomem.governance import consolidation_receipts

    values: dict[str, object] = {
        "kind": kind,
        "run_id": RUN_ID,
        "operation_id": OPERATION_ID,
        "phase": "publishing",
        "effect_ordinal": 1,
        ordinal_name: 0,
        "request_digest": REQUEST_DIGEST,
        "prior_digest": PRIOR_DIGEST,
        "target_digest": TARGET_DIGEST,
        "semantic_parent_event_id": PARENT_EVENT_ID,
        "semantic_parent_payload_digest": PARENT_PAYLOAD_DIGEST,
    }
    values["evidence"] = _evidence(kind)
    if kind == "content-batch":
        values["prepared_digest"] = PREPARED_DIGEST
    event = consolidation_receipts.build_intent(**values)
    assert event.payload[ordinal_name] == 0
    assert not (
        {"batch_ordinal", "rebuild_ordinal", "probe_ordinal", "page_ordinal"}
        - {ordinal_name}
    ).intersection(event.payload)
