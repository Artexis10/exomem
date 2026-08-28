from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import pickle
from pathlib import Path

import pytest

VAULT_BINDING = hashlib.sha256(b"vault-binding").hexdigest()
OTHER_VAULT_BINDING = hashlib.sha256(b"other-vault-binding").hexdigest()
RUN_ID = "00000000-0000-4000-8000-000000000001"
OPERATION_ID = "00000000-0000-4000-8000-000000000042"
JOURNAL_DIGEST = hashlib.sha256(b"apply-journal").hexdigest()
OTHER_JOURNAL_DIGEST = hashlib.sha256(b"other-journal").hexdigest()
CHECKPOINT_DIGEST = hashlib.sha256(b"deletion-checkpoint").hexdigest()
T0 = "2026-08-28T15:00:00.000Z"
T1 = "2026-08-28T15:00:01.000Z"
T2 = "2026-08-28T15:00:02.000Z"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _authority():
    from exomem.governance import consolidation_authority

    return consolidation_authority.issue_authority(
        vault_binding_digest=_digest("vault-binding"),
        run_id="00000000-0000-4000-8000-000000000001",
        operation_id="00000000-0000-4000-8000-000000000042",
        journal_digest=_digest("apply-journal"),
        phase="transport-verifying",
        action="probe",
    )


def test_consolidation_authority_is_exactly_vault_run_journal_phase_action_bound() -> None:
    from exomem.governance import consolidation_authority

    authority = _authority()
    consolidation_authority.require_authority(
        authority,
        vault_binding_digest=_digest("vault-binding"),
        run_id="00000000-0000-4000-8000-000000000001",
        operation_id="00000000-0000-4000-8000-000000000042",
        journal_digest=_digest("apply-journal"),
        phase="transport-verifying",
        action="probe",
    )
    assert repr(authority) == "<ConsolidationAuthority process-local>"

    substitutions = {
        "vault_binding_digest": _digest("other-vault"),
        "run_id": "00000000-0000-4000-8000-000000000002",
        "operation_id": "00000000-0000-4000-8000-000000000043",
        "journal_digest": _digest("other-journal"),
        "phase": "verifying",
        "action": "verify",
    }
    baseline = {
        "vault_binding_digest": _digest("vault-binding"),
        "run_id": "00000000-0000-4000-8000-000000000001",
        "operation_id": "00000000-0000-4000-8000-000000000042",
        "journal_digest": _digest("apply-journal"),
        "phase": "transport-verifying",
        "action": "probe",
    }
    for field, changed in substitutions.items():
        with pytest.raises(consolidation_authority.ConsolidationAuthorityUnavailable):
            consolidation_authority.require_authority(
                authority,
                **{**baseline, field: changed},
            )


def test_consolidation_authority_cannot_serialize_copy_or_round_trip_request_data() -> None:
    from exomem.governance import consolidation_authority

    authority = _authority()
    for serializer in (
        pickle.dumps,
        copy.copy,
        copy.deepcopy,
        json.dumps,
        vars,
        dataclasses.asdict,
    ):
        with pytest.raises(TypeError):
            serializer(authority)

    request_value = {
        "vault_binding_digest": _digest("vault-binding"),
        "run_id": "00000000-0000-4000-8000-000000000001",
        "operation_id": "00000000-0000-4000-8000-000000000042",
        "journal_digest": _digest("apply-journal"),
        "phase": "transport-verifying",
        "action": "probe",
    }
    with pytest.raises(consolidation_authority.ConsolidationAuthorityUnavailable):
        consolidation_authority.require_authority(authority=request_value, **request_value)


def test_probe_authority_exists_only_for_the_transport_verifying_phase() -> None:
    from exomem.governance import consolidation_authority

    with pytest.raises(consolidation_authority.ConsolidationAuthorityUnavailable):
        consolidation_authority.issue_authority(
            vault_binding_digest=_digest("vault-binding"),
            run_id="00000000-0000-4000-8000-000000000001",
            operation_id="00000000-0000-4000-8000-000000000042",
            journal_digest=_digest("apply-journal"),
            phase="verifying",
            action="probe",
        )
    with pytest.raises(consolidation_authority.ConsolidationAuthorityUnavailable):
        consolidation_authority.issue_authority(
            vault_binding_digest=_digest("vault-binding"),
            run_id="00000000-0000-4000-8000-000000000001",
            operation_id="00000000-0000-4000-8000-000000000042",
            journal_digest=_digest("apply-journal"),
            phase="transport-verifying",
            action="read",
        )


def test_forged_constructor_seal_never_authorizes() -> None:
    from exomem.governance import consolidation_authority

    forged = consolidation_authority.ConsolidationAuthority(
        _digest("vault-binding"),
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000042",
        _digest("apply-journal"),
        "transport-verifying",
        "probe",
        object(),
    )
    with pytest.raises(consolidation_authority.ConsolidationAuthorityUnavailable):
        consolidation_authority.require_authority(
            forged,
            vault_binding_digest=_digest("vault-binding"),
            run_id="00000000-0000-4000-8000-000000000001",
            operation_id="00000000-0000-4000-8000-000000000042",
            journal_digest=_digest("apply-journal"),
            phase="transport-verifying",
            action="probe",
        )


def _seal_store(vault: Path):
    from exomem.governance import consolidation_seal

    return consolidation_seal.ConsolidationSealStore(vault)


def _assert_seal_error(code: str):
    from exomem.governance import consolidation_seal

    return pytest.raises(
        consolidation_seal.ConsolidationSealUnavailable,
        match=f"^{code}$",
    )


def test_durable_seal_requires_explicit_open_and_survives_restart(tmp_path: Path) -> None:
    store = _seal_store(tmp_path)
    with _assert_seal_error("SEAL_NOT_INITIALIZED"):
        store.load(vault_binding_digest=VAULT_BINDING)

    opened = store.initialize_open(
        vault_binding_digest=VAULT_BINDING,
        recorded_at=T0,
    )
    assert opened.kind == "open"
    assert opened.revision == 0
    assert opened.vault_binding_digest == VAULT_BINDING
    assert opened.recorded_at == T0
    assert opened.run_id is None
    assert opened.checkpoint_digest is None

    assert _seal_store(tmp_path).load(vault_binding_digest=VAULT_BINDING) == opened
    assert store.initialize_open(
        vault_binding_digest=VAULT_BINDING,
        recorded_at=T0,
    ) == opened
    with _assert_seal_error("SEAL_STATE_CONFLICT"):
        store.initialize_open(
            vault_binding_digest=VAULT_BINDING,
            recorded_at=T1,
        )


def test_consolidation_seal_is_exactly_journal_bound_and_revisioned(tmp_path: Path) -> None:
    from exomem.governance import consolidation_authority

    store = _seal_store(tmp_path)
    store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    sealing = store.begin_consolidation(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        sealed_at=T1,
        expected_revision=0,
    )
    assert sealing.kind == "consolidation-sealed"
    assert sealing.revision == 1
    assert sealing.run_id == RUN_ID
    assert sealing.operation_id == OPERATION_ID
    assert sealing.journal_digest == JOURNAL_DIGEST
    assert sealing.phase == "sealing"

    authority = consolidation_authority.issue_authority(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        phase="sealing",
        action="apply",
    )
    sealed = store.advance_consolidation(
        authority,
        vault_binding_digest=VAULT_BINDING,
        action="apply",
        target_phase="sealed",
        recorded_at=T2,
        expected_revision=1,
    )
    assert sealed.revision == 2
    assert sealed.phase == "sealed"
    assert sealed.sealed_at == T1
    assert _seal_store(tmp_path).load(vault_binding_digest=VAULT_BINDING) == sealed

    wrong = consolidation_authority.issue_authority(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=OTHER_JOURNAL_DIGEST,
        phase="sealed",
        action="apply",
    )
    with _assert_seal_error("SEAL_AUTHORITY_MISMATCH"):
        store.advance_consolidation(
            wrong,
            vault_binding_digest=VAULT_BINDING,
            action="apply",
            target_phase="preimage-ready",
            recorded_at=T2,
            expected_revision=2,
        )


def test_deletion_seal_dominates_and_can_never_be_resumed_by_consolidation(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_authority

    store = _seal_store(tmp_path)
    store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    deletion = store.seal_for_deletion(
        vault_binding_digest=VAULT_BINDING,
        checkpoint_digest=CHECKPOINT_DIGEST,
        sealed_at=T1,
        expected_revision=0,
    )
    assert deletion.kind == "deletion-sealed"
    assert deletion.checkpoint_digest == CHECKPOINT_DIGEST
    assert store.seal_for_deletion(
        vault_binding_digest=VAULT_BINDING,
        checkpoint_digest=CHECKPOINT_DIGEST,
        sealed_at=T1,
        expected_revision=0,
    ) == deletion

    with _assert_seal_error("DELETION_SEAL_IRREVERSIBLE"):
        store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T2)
    with _assert_seal_error("DELETION_SEAL_IRREVERSIBLE"):
        store.begin_consolidation(
            vault_binding_digest=VAULT_BINDING,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T2,
            expected_revision=1,
        )

    authority = consolidation_authority.issue_authority(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        phase="complete",
        action="apply",
    )
    with _assert_seal_error("DELETION_SEAL_IRREVERSIBLE"):
        store.unseal_consolidation(
            authority,
            vault_binding_digest=VAULT_BINDING,
            action="apply",
            recorded_at=T2,
            expected_revision=1,
        )


def test_snapshot_before_pointer_crash_keeps_prior_state_and_retries_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_seal

    store = _seal_store(tmp_path)
    opened = store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    publish_active = store._publish_active

    def fail_after_snapshot(*_args, **_kwargs) -> None:
        raise consolidation_seal.ConsolidationSealUnavailable("SEAL_STORE_UNAVAILABLE")

    monkeypatch.setattr(store, "_publish_active", fail_after_snapshot)
    with _assert_seal_error("SEAL_STORE_UNAVAILABLE"):
        store.begin_consolidation(
            vault_binding_digest=VAULT_BINDING,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            expected_revision=0,
        )
    assert _seal_store(tmp_path).load(vault_binding_digest=VAULT_BINDING) == opened

    monkeypatch.setattr(store, "_publish_active", publish_active)
    sealed = store.begin_consolidation(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        sealed_at=T1,
        expected_revision=0,
    )
    assert sealed.kind == "consolidation-sealed"
    assert sealed.revision == 1

    other_snapshot = store.base / "snapshots" / "2.json"
    assert not other_snapshot.exists()


def test_missing_active_after_initialization_is_corrupt_not_open(tmp_path: Path) -> None:
    store = _seal_store(tmp_path)
    store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    (store.base / "active.json").unlink()

    with _assert_seal_error("SEAL_STORE_CORRUPT"):
        store.load(vault_binding_digest=VAULT_BINDING)
    with _assert_seal_error("SEAL_STORE_CORRUPT"):
        store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T1)


def test_missing_active_after_later_revision_cannot_republish_initial_open(
    tmp_path: Path,
) -> None:
    store = _seal_store(tmp_path)
    store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    store.seal_for_deletion(
        vault_binding_digest=VAULT_BINDING,
        checkpoint_digest=CHECKPOINT_DIGEST,
        sealed_at=T1,
        expected_revision=0,
    )
    (store.base / "active.json").unlink()

    with _assert_seal_error("SEAL_STORE_CORRUPT"):
        store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)


def test_malformed_active_pointer_is_store_corruption(tmp_path: Path) -> None:
    store = _seal_store(tmp_path)
    store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    active = store.base / "active.json"
    active.write_text(
        active.read_text(encoding="utf-8").replace('"snapshot_digest":"', '"snapshot_digest":"x'),
        encoding="utf-8",
    )

    with _assert_seal_error("SEAL_STORE_CORRUPT"):
        store.load(vault_binding_digest=VAULT_BINDING)


def test_exact_terminal_consolidation_journal_can_unseal_only_its_own_kind(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_authority

    store = _seal_store(tmp_path)
    store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    store.begin_consolidation(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        sealed_at=T1,
        expected_revision=0,
    )
    sealing_authority = consolidation_authority.issue_authority(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        phase="sealing",
        action="apply",
    )
    complete = store.advance_consolidation(
        sealing_authority,
        vault_binding_digest=VAULT_BINDING,
        action="apply",
        target_phase="complete",
        recorded_at=T2,
        expected_revision=1,
    )
    terminal_authority = consolidation_authority.issue_authority(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        phase="complete",
        action="apply",
    )
    with _assert_seal_error("SEAL_PHASE_CONFLICT"):
        store.advance_consolidation(
            terminal_authority,
            vault_binding_digest=VAULT_BINDING,
            action="apply",
            target_phase="publishing",
            recorded_at=T2,
            expected_revision=complete.revision,
        )
    opened = store.unseal_consolidation(
        terminal_authority,
        vault_binding_digest=VAULT_BINDING,
        action="apply",
        recorded_at=T2,
        expected_revision=complete.revision,
    )
    assert opened.kind == "open"
    assert opened.revision == 3


def test_other_vault_identity_remains_independent(tmp_path: Path) -> None:
    first = _seal_store(tmp_path / "a")
    second = _seal_store(tmp_path / "b")
    first.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    second.initialize_open(vault_binding_digest=OTHER_VAULT_BINDING, recorded_at=T0)
    first.begin_consolidation(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        sealed_at=T1,
        expected_revision=0,
    )

    assert first.load(vault_binding_digest=VAULT_BINDING).kind == "consolidation-sealed"
    assert second.load(vault_binding_digest=OTHER_VAULT_BINDING).kind == "open"
    with _assert_seal_error("SEAL_VAULT_MISMATCH"):
        second.load(vault_binding_digest=VAULT_BINDING)
