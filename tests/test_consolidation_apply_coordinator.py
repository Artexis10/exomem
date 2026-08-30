from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem.governance import consolidation_fingerprints, consolidation_receipts
from exomem.governance.consolidation_identity import ConsolidationCellIdentity

RUN_ID = "00000000-0000-4000-8000-0000000000d1"
OPERATION_ID = "00000000-0000-4000-8000-0000000000d2"
PRIOR_RUN_ID = "00000000-0000-4000-8000-0000000000e1"
PRIOR_OPERATION_ID = "00000000-0000-4000-8000-0000000000e2"
VAULT_BINDING = hashlib.sha256(b"apply-preparation-vault").hexdigest()
JOURNAL_DIGEST = hashlib.sha256(b"apply-preparation-journal").hexdigest()
PRIOR_JOURNAL_DIGEST = hashlib.sha256(b"prior-apply-journal").hexdigest()
REQUEST_DIGEST = hashlib.sha256(b"apply-preparation-request").hexdigest()
PLAN_DIGEST = hashlib.sha256(b"apply-preparation-plan").hexdigest()
CONTROL_BASIS_DIGEST = hashlib.sha256(b"apply-preparation-control").hexdigest()
T0 = "2026-08-30T12:00:00.000Z"
T1 = "2026-08-30T12:00:01.000Z"
T2 = "2026-08-30T12:00:02.000Z"
T3 = "2026-08-30T12:00:03.000Z"


class SimulatedProcessCrash(BaseException):
    pass


def _identity(tmp_path: Path) -> ConsolidationCellIdentity:
    def digest(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    return ConsolidationCellIdentity(
        schema="exomem.consolidation-cell-identity/v1",
        cell_id="cell-apply-preparation",
        vault_id="vault-apply-preparation",
        installation_id="installation-apply-preparation",
        installation_generation=2,
        active_fence_digest=digest(b"fence"),
        root_binding_id="attachment-apply-preparation",
        root_binding_digest=digest(b"root"),
        machine_key_id="key-apply-preparation",
        adoption_census_digest=digest(b"adoption"),
        clone_of_vault_id=None,
        clone_of_installation_id=None,
        clone_of_snapshot_digest=None,
        created_at=1_777_777_777,
        authentication_algorithm="HMAC-SHA256",
        record_digest=digest(b"identity"),
        identity_path=tmp_path / "identity.json",
    )


def _destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, object]:
    from exomem.governance import consolidation_intake

    vault = tmp_path / "vault"
    files = {
        "Knowledge Base/Notes/destination.md": b"destination before apply\n",
        "Knowledge Base/Sources/source.md": b"preserved source\n",
        "Knowledge Base/Evidence/proof.bin": b"proof\x00bytes",
        "Knowledge Base/_access.yaml": b"readonly: []\n",
        "Knowledge Base/.review-state.json": b'{"version":2,"records":{}}',
    }
    for relative, content in files.items():
        path = vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    monkeypatch.setattr(
        consolidation_fingerprints,
        "load_local_identity",
        lambda *_args, **_kwargs: _identity(tmp_path),
    )
    monkeypatch.setattr(
        consolidation_fingerprints,
        "_load_active_policy_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(
            active=SimpleNamespace(
                logical_vault_id="vault-apply-preparation",
                policy_fingerprint=hashlib.sha256(b"policy").hexdigest(),
            ),
            source_documents=(("rules/default.yaml", b"governance_version: 1\n"),),
        ),
    )
    return vault, consolidation_intake.PrivateConsolidationArtifactStore(
        tmp_path / "private-artifacts",
        active_vault_roots=(vault,),
    )


def _append_token_reservation(vault: Path) -> dict[str, object]:
    chain = (
        ("start", "intake", None),
        ("intake", "intake", None),
        ("snapshot-source", "snapshot", None),
        ("snapshot-destination", "snapshot", None),
        ("reconcile", "reconciliation", None),
        ("plan-cutover", "plan", None),
        ("render-begin", "rendering", None),
        ("render-page", "rendering", 0),
        ("render-ack", "rendering", 0),
        ("render-complete", "rendering", None),
        ("approval", "approval", None),
        ("token-reservation", "authorization", None),
    )
    parent_id, parent_digest = consolidation_receipts.semantic_root()
    terminal: dict[str, object] | None = None
    for ordinal, (kind, phase, page_ordinal) in enumerate(chain):
        event = consolidation_receipts.build_intent(
            kind=kind,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            phase=phase,
            effect_ordinal=ordinal,
            request_digest=REQUEST_DIGEST,
            prior_digest=hashlib.sha256(f"{kind}:prior".encode()).hexdigest(),
            target_digest=hashlib.sha256(f"{kind}:target".encode()).hexdigest(),
            evidence=consolidation_receipts.build_evidence(
                kind=kind,
                digests={
                    field: (
                        PLAN_DIGEST
                        if kind == "token-reservation" and field == "plan_digest"
                        else hashlib.sha256(f"{kind}:{field}".encode()).hexdigest()
                    )
                    for field in consolidation_receipts._EVIDENCE_FIELDS[kind]  # noqa: SLF001
                },
            ),
            semantic_parent_event_id=parent_id,
            semantic_parent_payload_digest=parent_digest,
            page_ordinal=page_ordinal,
        )
        intent = consolidation_receipts.append_intent(vault, event, timestamp=T0)
        terminal = consolidation_receipts.append_terminal(
            vault,
            intent_event_id=str(intent["event_id"]),
            role="committed",
            observed_digest=str(event.payload["target_digest"]),
            timestamp=T0,
        )
        parent_id = str(terminal["event_id"])
        parent_digest = str(terminal["consolidation_event"]["payload_digest"])
    assert terminal is not None
    return terminal


def _reopen_after_prior_apply(seal_store) -> None:
    from exomem.governance import consolidation_authority, consolidation_seal

    current = seal_store.begin_consolidation(
        vault_binding_digest=VAULT_BINDING,
        run_id=PRIOR_RUN_ID,
        operation_id=PRIOR_OPERATION_ID,
        journal_digest=PRIOR_JOURNAL_DIGEST,
        sealed_at=T0,
        expected_revision=0,
    )
    while current.phase != "complete":
        assert current.phase is not None
        authority = consolidation_authority.issue_authority(
            vault_binding_digest=VAULT_BINDING,
            run_id=PRIOR_RUN_ID,
            operation_id=PRIOR_OPERATION_ID,
            journal_digest=PRIOR_JOURNAL_DIGEST,
            phase=current.phase,
            action="apply",
        )
        current = seal_store.advance_consolidation(
            authority,
            vault_binding_digest=VAULT_BINDING,
            action="apply",
            target_phase=consolidation_seal._PHASE_SUCCESSORS[current.phase],  # noqa: SLF001
            recorded_at=T0,
            expected_revision=current.revision,
        )
    opened = seal_store.unseal_consolidation(
        consolidation_authority.issue_authority(
            vault_binding_digest=VAULT_BINDING,
            run_id=PRIOR_RUN_ID,
            operation_id=PRIOR_OPERATION_ID,
            journal_digest=PRIOR_JOURNAL_DIGEST,
            phase="complete",
            action="apply",
        ),
        vault_binding_digest=VAULT_BINDING,
        action="apply",
        recorded_at=T0,
        expected_revision=current.revision,
    )
    assert opened.kind == "open"
    assert opened.revision == 14


@pytest.mark.parametrize("crash_before_preimage_ready", [False, True])
@pytest.mark.parametrize("reopened_after_prior_apply", [False, True])
def test_apply_preparation_receipt_chains_seal_drain_and_exact_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_before_preimage_ready: bool,
    reopened_after_prior_apply: bool,
) -> None:
    from exomem.governance import (
        consolidation_admission,
        consolidation_apply_coordinator,
        consolidation_authority,
        consolidation_plan_store,
        consolidation_seal,
        receipts,
    )

    vault, artifact_store = _destination(tmp_path, monkeypatch)
    predecessor = _append_token_reservation(vault)
    snapshot = consolidation_fingerprints.load_local_destination_snapshot(vault, now=123)
    loaded_plans: list[tuple[str, str, str]] = []

    def load_plan(_store, run_id, *, plan_kind, plan_digest):
        loaded_plans.append((run_id, plan_kind, plan_digest))
        return SimpleNamespace(
            digest=PLAN_DIGEST,
            control_basis=SimpleNamespace(digest=CONTROL_BASIS_DIGEST),
            preimage={
                "run_id": RUN_ID,
                "plan_kind": "cutover",
                "destination_snapshot_fingerprint": snapshot.digest,
                "expected_destination_preimage_census_digest": (
                    snapshot.canonical_census_digest
                ),
            },
        )

    monkeypatch.setattr(
        consolidation_plan_store.ConsolidationPlanStore,
        "load",
        load_plan,
    )
    seal_store = consolidation_seal.ConsolidationSealStore(vault)
    seal_store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    if reopened_after_prior_apply:
        _reopen_after_prior_apply(seal_store)
    admission = consolidation_admission.ConsolidationAdmission(
        vault,
        vault_binding_digest=VAULT_BINDING,
    )
    authority = consolidation_authority.issue_authority(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        phase="sealing",
        action="apply",
    )
    before = (vault / "Knowledge Base/Notes/destination.md").read_bytes()
    arguments = {
        "vault_root": vault,
        "admission": admission,
        "artifact_store": artifact_store,
        "token_reservation_event_id": str(predecessor["event_id"]),
        "token_reservation_payload_digest": str(
            predecessor["consolidation_event"]["payload_digest"]
        ),
        "vault_binding_digest": VAULT_BINDING,
        "run_id": RUN_ID,
        "operation_id": OPERATION_ID,
        "journal_digest": JOURNAL_DIGEST,
        "request_digest": REQUEST_DIGEST,
        "plan_digest": PLAN_DIGEST,
        "sealed_at": T1,
        "drained_at": T2,
        "preimage_ready_at": T3,
        "now": 123,
        "timeout": 2.0,
    }
    original_advance = consolidation_seal.ConsolidationSealStore.advance_consolidation
    crashed = False

    def crash_once(self, *args, **kwargs):
        nonlocal crashed
        if (
            crash_before_preimage_ready
            and not crashed
            and kwargs.get("target_phase") == "preimage-ready"
        ):
            crashed = True
            raise SimulatedProcessCrash
        return original_advance(self, *args, **kwargs)

    monkeypatch.setattr(
        consolidation_seal.ConsolidationSealStore,
        "advance_consolidation",
        crash_once,
    )

    result = None
    with admission.admit_mutation() as mutation:
        control = admission.convert_control_mutation(
            mutation,
            authority=authority,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            request_digest=REQUEST_DIGEST,
            phase="sealing",
            action="apply",
        )
        if crash_before_preimage_ready:
            with pytest.raises(SimulatedProcessCrash):
                consolidation_apply_coordinator.prepare_apply_through_preimage(
                    control=control,
                    **arguments,
                )
        else:
            result = consolidation_apply_coordinator.prepare_apply_through_preimage(
                control=control,
                **arguments,
            )

    if crash_before_preimage_ready:
        assert seal_store.load(vault_binding_digest=VAULT_BINDING).phase == "sealed"
        assert list((artifact_store.root / "preimages").glob("*.json"))
        assert receipts.event_records(vault)[-1]["phase"] == "intent"
        with admission.resume_control_mutation(
            authority=authority,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            request_digest=REQUEST_DIGEST,
            phase="sealing",
            action="apply",
        ) as recovered:
            result = consolidation_apply_coordinator.prepare_apply_through_preimage(
                control=recovered,
                **arguments,
            )

    assert result is not None
    assert loaded_plans
    assert set(loaded_plans) == {(RUN_ID, "cutover", PLAN_DIGEST)}
    assert result.seal_state.phase == "preimage-ready"
    assert result.preimage.manifest_digest == result.preimage_plan.manifest_digest
    assert result.preimage.binding.semantic_predecessor_event_id == (
        result.seal_drained.terminal.event_id
    )
    assert (vault / "Knowledge Base/Notes/destination.md").read_bytes() == before
    assert [
        record["consolidation_event"]["kind"]
        for record in receipts.event_records(vault)[-6:]
    ] == [
        "seal-intent",
        "seal-intent",
        "seal-drained",
        "seal-drained",
        "preimage",
        "preimage",
    ]
    assert [record["phase"] for record in receipts.event_records(vault)[-6:]] == [
        "intent",
        "committed",
        "intent",
        "committed",
        "intent",
        "committed",
    ]
    record_count = len(receipts.event_records(vault))
    with admission.resume_control_mutation(
        authority=authority,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        request_digest=REQUEST_DIGEST,
        phase="sealing",
        action="apply",
    ) as recovered:
        replay = consolidation_apply_coordinator.prepare_apply_through_preimage(
            control=recovered,
            **arguments,
        )
    assert replay == result
    assert len(receipts.event_records(vault)) == record_count
    for changed in (
        {"request_digest": hashlib.sha256(b"changed-request").hexdigest()},
        {"plan_digest": hashlib.sha256(b"changed-plan").hexdigest()},
        {"drained_at": T0},
    ):
        with admission.resume_control_mutation(
            authority=authority,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            request_digest=REQUEST_DIGEST,
            phase="sealing",
            action="apply",
        ) as recovered:
            with pytest.raises(
                consolidation_apply_coordinator.ConsolidationApplyPreparationUnavailable,
                match="^CONSOLIDATION_APPLY_PREPARATION_UNAVAILABLE$",
            ):
                consolidation_apply_coordinator.prepare_apply_through_preimage(
                    control=recovered,
                    **{**arguments, **changed},
                )
    assert len(receipts.event_records(vault)) == record_count

    if not crash_before_preimage_ready:
        artifact_store.resolve_preimage(result.preimage.manifest_ref).write_bytes(b"{}")
        with admission.resume_control_mutation(
            authority=authority,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            request_digest=REQUEST_DIGEST,
            phase="sealing",
            action="apply",
        ) as recovered:
            with pytest.raises(
                consolidation_apply_coordinator.ConsolidationApplyPreparationUnavailable,
                match="^CONSOLIDATION_APPLY_PREPARATION_UNAVAILABLE$",
            ):
                consolidation_apply_coordinator.prepare_apply_through_preimage(
                    control=recovered,
                    **arguments,
                )
        assert len(receipts.event_records(vault)) == record_count
