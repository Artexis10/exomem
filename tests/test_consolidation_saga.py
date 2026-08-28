from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

VAULT_BINDING = hashlib.sha256(b"saga-vault-binding").hexdigest()
RUN_ID = "00000000-0000-4000-8000-000000000071"
OPERATION_ID = "00000000-0000-4000-8000-000000000072"
JOURNAL_DIGEST = hashlib.sha256(b"saga-journal").hexdigest()
T0 = "2026-08-28T17:00:00.000Z"
T1 = "2026-08-28T17:00:01.000Z"
T2 = "2026-08-28T17:00:02.000Z"
T3 = "2026-08-28T17:00:03.000Z"

APPLY_PHASES = (
    "sealing",
    "sealed",
    "preimage-ready",
    "policy-active",
    "publishing",
    "rebuilding",
    "verifying",
    "verified",
    "transport-stopping",
    "transport-verifying",
    "transport-verified",
    "routing-opening",
    "complete",
)


def _authority(phase: str, *, action: str = "apply"):
    from exomem.governance import consolidation_authority

    return consolidation_authority.issue_authority(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        phase=phase,
        action=action,
    )


def _started_store(vault: Path):
    from exomem.governance import consolidation_seal

    store = consolidation_seal.ConsolidationSealStore(vault)
    store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    sealing = store.begin_consolidation(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        sealed_at=T1,
        expected_revision=0,
    )
    return store, sealing


def _assert_phase_conflict():
    from exomem.governance import consolidation_seal

    return pytest.raises(
        consolidation_seal.ConsolidationSealUnavailable,
        match="^SEAL_PHASE_CONFLICT$",
    )


def test_apply_state_machine_allows_only_next_phase_or_exact_replay(
    tmp_path: Path,
) -> None:
    store, current = _started_store(tmp_path)

    for next_phase in APPLY_PHASES[1:]:
        current_phase = current.phase
        assert current_phase is not None
        for illegal_target in (
            phase for phase in APPLY_PHASES if phase not in {current_phase, next_phase}
        ):
            with _assert_phase_conflict():
                store.advance_consolidation(
                    _authority(current_phase),
                    vault_binding_digest=VAULT_BINDING,
                    action="apply",
                    target_phase=illegal_target,
                    recorded_at=T2,
                    expected_revision=current.revision,
                )
        prior_revision = current.revision
        current = store.advance_consolidation(
            _authority(current_phase),
            vault_binding_digest=VAULT_BINDING,
            action="apply",
            target_phase=next_phase,
            recorded_at=T2,
            expected_revision=prior_revision,
        )
        assert current.phase == next_phase
        assert current.revision == prior_revision + 1
        assert (
            store.advance_consolidation(
                _authority(next_phase),
                vault_binding_digest=VAULT_BINDING,
                action="apply",
                target_phase=next_phase,
                recorded_at=T2,
                expected_revision=prior_revision,
            )
            == current
        )

        for changed_time, changed_revision in (
            (T3, prior_revision),
            (T2, current.revision),
        ):
            with _assert_phase_conflict():
                store.advance_consolidation(
                    _authority(next_phase),
                    vault_binding_digest=VAULT_BINDING,
                    action="apply",
                    target_phase=next_phase,
                    recorded_at=changed_time,
                    expected_revision=changed_revision,
                )

    assert current.phase == "complete"
    for target_phase in APPLY_PHASES[:-1]:
        with _assert_phase_conflict():
            store.advance_consolidation(
                _authority("complete"),
                vault_binding_digest=VAULT_BINDING,
                action="apply",
                target_phase=target_phase,
                recorded_at=T2,
                expected_revision=current.revision,
            )


def test_seal_intent_replay_requires_the_original_expected_revision(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_seal

    store, sealing = _started_store(tmp_path)

    assert (
        store.begin_consolidation(
            vault_binding_digest=VAULT_BINDING,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            expected_revision=0,
        )
        == sealing
    )
    with pytest.raises(
        consolidation_seal.ConsolidationSealUnavailable,
        match="^SEAL_STATE_CONFLICT$",
    ):
        store.begin_consolidation(
            vault_binding_digest=VAULT_BINDING,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            expected_revision=1,
        )


@pytest.mark.parametrize(
    ("source_phase", "target_phase", "changed_action"),
    (
        ("sealed", "preimage-ready", "verify"),
        ("sealed", "preimage-ready", "recover"),
        ("sealed", "preimage-ready", "abort"),
        ("sealed", "preimage-ready", "rollback"),
        ("transport-verifying", "transport-verified", "probe"),
    ),
)
def test_apply_transition_rejects_every_changed_action(
    tmp_path: Path,
    source_phase: str,
    target_phase: str,
    changed_action: str,
) -> None:
    store, current = _started_store(tmp_path)
    source_ordinal = APPLY_PHASES.index(source_phase)
    for phase in APPLY_PHASES[1 : source_ordinal + 1]:
        assert current.phase is not None
        current = store.advance_consolidation(
            _authority(current.phase),
            vault_binding_digest=VAULT_BINDING,
            action="apply",
            target_phase=phase,
            recorded_at=T2,
            expected_revision=current.revision,
        )

    with _assert_phase_conflict():
        store.advance_consolidation(
            _authority(source_phase, action=changed_action),
            vault_binding_digest=VAULT_BINDING,
            action=changed_action,
            target_phase=target_phase,
            recorded_at=T2,
            expected_revision=current.revision,
        )
    assert store.load(vault_binding_digest=VAULT_BINDING) == current


def test_apply_transition_replay_rejects_a_changed_action(tmp_path: Path) -> None:
    store, sealing = _started_store(tmp_path)
    sealed = store.advance_consolidation(
        _authority("sealing"),
        vault_binding_digest=VAULT_BINDING,
        action="apply",
        target_phase="sealed",
        recorded_at=T2,
        expected_revision=sealing.revision,
    )

    with _assert_phase_conflict():
        store.advance_consolidation(
            _authority("sealed", action="rollback"),
            vault_binding_digest=VAULT_BINDING,
            action="rollback",
            target_phase="sealed",
            recorded_at=T2,
            expected_revision=sealing.revision,
        )
    assert store.load(vault_binding_digest=VAULT_BINDING) == sealed
