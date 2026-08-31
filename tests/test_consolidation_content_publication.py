from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem.governance import consolidation_receipts
from tests.test_consolidation_effect_coordinator import (
    OPERATION_ID,
    REQUEST_DIGEST,
    RUN_ID,
    _append_policy_active_parent,
)

VAULT_BINDING = hashlib.sha256(b"content-publication-vault").hexdigest()
JOURNAL_DIGEST = hashlib.sha256(b"content-publication-journal").hexdigest()
PLAN_DIGEST = hashlib.sha256(b"content-publication-plan").hexdigest()
POLICY_FINGERPRINT = hashlib.sha256(b"content-publication-policy").hexdigest()
T0 = "2026-08-30T12:00:00.000Z"
T1 = "2026-08-30T12:00:01.000Z"
T2 = "2026-08-30T12:00:02.000Z"
T3 = "2026-08-30T12:00:03.000Z"
T4 = "2026-08-30T12:00:04.000Z"
T5 = "2026-08-30T12:00:05.000Z"


class SimulatedPublicationCrash(BaseException):
    pass


@pytest.fixture(autouse=True)
def _private_writer_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        str(tmp_path / "writer-state"),
    )


def _action(
    ordinal: int,
    *,
    batch_ordinal: int,
    destination_path: str,
    before: bytes | None,
    after: bytes,
) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "batch_ordinal": batch_ordinal,
        "action": "add" if before is None else "overwrite",
        "object_ref": f"approved-object-{ordinal}",
        "source_path": f"Knowledge Base/Notes/source-{ordinal}.bin",
        "destination_path": destination_path,
        "expected_before_state": "absent" if before is None else "present",
        "expected_before_sha256": (
            "0" * 64 if before is None else hashlib.sha256(before).hexdigest()
        ),
        "planned_after_state": "present",
        "planned_after_sha256": hashlib.sha256(after).hexdigest(),
    }


def _policy_terminal(vault: Path):
    from exomem.governance import consolidation_saga

    _append_policy_active_parent(vault)
    records = consolidation_receipts._active_records(vault)  # noqa: SLF001
    prepare_terminal = consolidation_receipts.validate_nested(
        records[-3]["consolidation_event"],
        outer_phase="committed",
    )
    consolidation_receipts.validate_nested(
        records[-2]["consolidation_event"],
        outer_phase="intent",
    )
    active_terminal = consolidation_receipts.validate_nested(
        records[-1]["consolidation_event"],
        outer_phase="committed",
    )
    return consolidation_saga.PolicyActivationTerminal(
        schema=consolidation_saga.POLICY_ACTIVATION_TERMINAL_SCHEMA,
        policy_fingerprint=POLICY_FINGERPRINT,
        intent_event_id=str(records[-2]["event_id"]),
        prepared_fingerprint=str(prepare_terminal["observed_digest"]),
        active_fingerprint=str(active_terminal["observed_digest"]),
        terminal_event_id=str(records[-1]["event_id"]),
    )


def _policy_active_vault(vault: Path) -> None:
    from exomem.governance import consolidation_authority, consolidation_seal

    store = consolidation_seal.ConsolidationSealStore(vault)
    store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    current = store.begin_consolidation(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        sealed_at=T0,
        expected_revision=0,
    )
    for target, timestamp in (
        ("sealed", T1),
        ("preimage-ready", T2),
        ("policy-active", T3),
    ):
        assert current.phase is not None
        authority = consolidation_authority.issue_authority(
            vault_binding_digest=VAULT_BINDING,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            phase=current.phase,
            action="apply",
        )
        current = store.advance_consolidation(
            authority,
            vault_binding_digest=VAULT_BINDING,
            action="apply",
            target_phase=target,
            recorded_at=timestamp,
            expected_revision=current.revision,
        )
    assert current.phase == "policy-active"


def _install_object(artifact_store, tmp_path: Path, content: bytes) -> None:
    digest = hashlib.sha256(content).hexdigest()
    staged = tmp_path / f"staged-{digest}"
    staged.write_bytes(content)
    assert (
        artifact_store.install_object_file(
            staged,
            expected_digest=digest,
        )
        == f"exomem-consolidation-object://sha256/{digest}"
    )


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    actions: tuple[dict[str, object], ...],
    after_values: tuple[bytes, ...],
):
    from exomem.governance import (
        consolidation_admission,
        consolidation_content_publication,
        consolidation_intake,
        consolidation_plan,
        consolidation_plan_store,
        consolidation_saga,
    )

    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "Notes").mkdir(parents=True)
    _policy_active_vault(vault)
    terminal = _policy_terminal(vault)
    partition = consolidation_plan.derive_journal_batch_partition(actions)
    plan = SimpleNamespace(
        digest=PLAN_DIGEST,
        preimage={
            "run_id": RUN_ID,
            "plan_kind": "cutover",
            "content_actions": actions,
            "journal_batch_partition_digest": partition.digest,
            "prospective_policy_fingerprint": POLICY_FINGERPRINT,
        },
    )
    loads: list[tuple[object, object, object]] = []

    def load_plan(_store, run_id, *, plan_kind, plan_digest):
        loads.append((run_id, plan_kind, plan_digest))
        return plan

    monkeypatch.setattr(
        consolidation_plan_store.ConsolidationPlanStore,
        "load",
        load_plan,
    )

    def verify_terminal(**kwargs):
        assert kwargs["vault_root"] == vault
        assert kwargs["vault_binding_digest"] == VAULT_BINDING
        assert kwargs["allowed_seal_phases"] == frozenset(
            {"policy-active", "publishing", "rebuilding"}
        )
        return consolidation_saga._policy_terminal(  # noqa: SLF001
            kwargs["terminal"],
            expected_policy_fingerprint=kwargs["expected_policy_fingerprint"],
        )

    monkeypatch.setattr(
        consolidation_saga,
        "_verify_policy_terminal_receipt",
        verify_terminal,
    )
    artifact_store = consolidation_intake.PrivateConsolidationArtifactStore(
        tmp_path / "private-artifacts",
        active_vault_roots=(vault,),
    )
    for content in after_values:
        _install_object(artifact_store, tmp_path, content)
    admission = consolidation_admission.ConsolidationAdmission(
        vault,
        vault_binding_digest=VAULT_BINDING,
    )
    arguments = {
        "vault_root": vault,
        "admission": admission,
        "artifact_store": artifact_store,
        "policy_terminal": terminal,
        "vault_binding_digest": VAULT_BINDING,
        "run_id": RUN_ID,
        "operation_id": OPERATION_ID,
        "journal_digest": JOURNAL_DIGEST,
        "request_digest": REQUEST_DIGEST,
        "plan_digest": PLAN_DIGEST,
        "publishing_at": T4,
        "rebuilding_at": T5,
    }
    assert consolidation_content_publication is not None
    return vault, partition, loads, arguments


def _content_records(vault: Path) -> list[dict[str, object]]:
    return [
        record
        for record in consolidation_receipts._active_records(vault)  # noqa: SLF001
        if record.get("event_type") == "consolidation"
        and isinstance(record.get("consolidation_event"), dict)
        and record["consolidation_event"].get("kind") == "content-batch"
    ]


def test_policy_active_run_publishes_stored_batches_then_reaches_rebuilding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_batch_journal,
        consolidation_content_publication,
        consolidation_effect_coordinator,
        consolidation_seal,
    )

    before = b"destination before\n"
    first_after = b"destination after\n"
    second_after = b"\x00private attachment\xff"
    actions = (
        _action(
            0,
            batch_ordinal=0,
            destination_path="Knowledge Base/Notes/destination.md",
            before=before,
            after=first_after,
        ),
        _action(
            1,
            batch_ordinal=1,
            destination_path="Knowledge Base/Notes/attachment.bin",
            before=None,
            after=second_after,
        ),
    )
    vault, partition, loads, arguments = _setup(
        tmp_path,
        monkeypatch,
        actions,
        (first_after, second_after),
    )
    target = vault / "Knowledge Base" / "Notes" / "destination.md"
    target.write_bytes(before)

    result = consolidation_content_publication.publish_stored_content_batches(**arguments)

    assert target.read_bytes() == first_after
    assert (vault / "Knowledge Base" / "Notes" / "attachment.bin").read_bytes() == second_after
    assert result.partition_digest == partition.digest
    assert result.committed_batch_ordinals == (0, 1)
    assert result.publication_boundary_ordinal == 0
    assert result.seal_state.phase == "rebuilding"
    assert loads == [(RUN_ID, "cutover", PLAN_DIGEST)]
    state = consolidation_batch_journal.ConsolidationBatchJournalStore(
        vault,
        run_id=RUN_ID,
    ).load()
    assert tuple(item.status for item in state.batches) == ("final", "final")
    assert state.publication_boundary_committed is True
    assert (
        consolidation_seal.ConsolidationSealStore(vault)
        .load(vault_binding_digest=VAULT_BINDING)
        .phase
        == "rebuilding"
    )
    records = _content_records(vault)
    assert [record["phase"] for record in records] == [
        "intent",
        "committed",
        "intent",
        "committed",
    ]
    first_intent = consolidation_receipts.validate_nested(
        records[0]["consolidation_event"],
        outer_phase="intent",
    )
    second_intent = consolidation_receipts.validate_nested(
        records[2]["consolidation_event"],
        outer_phase="intent",
    )
    assert (
        first_intent["semantic_parent_event_id"] == arguments["policy_terminal"].terminal_event_id
    )
    assert second_intent["semantic_parent_event_id"] == records[1]["event_id"]
    for effect_ordinal in (17, 18):
        journal = consolidation_effect_coordinator.ConsolidationEffectJournalStore(
            vault,
            run_id=RUN_ID,
            effect_ordinal=effect_ordinal,
        ).load()
        assert journal.status == "final"
        assert journal.observed_state == "target"


def test_retry_after_batch_terminal_does_not_rewrite_approved_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import vault as vault_module
    from exomem.governance import (
        consolidation_batch_journal,
        consolidation_content_publication,
        consolidation_effect_coordinator,
        consolidation_seal,
    )

    before = b"\x00binary before\xfe"
    after = b"\x00binary after\xff"
    actions = (
        _action(
            0,
            batch_ordinal=0,
            destination_path="Knowledge Base/Notes/destination.bin",
            before=before,
            after=after,
        ),
    )
    vault, _partition, _loads, arguments = _setup(
        tmp_path,
        monkeypatch,
        actions,
        (after,),
    )
    target = vault / "Knowledge Base" / "Notes" / "destination.bin"
    target.write_bytes(before)
    writes = 0
    real_write = vault_module.batch_atomic_write

    def observed_write(*args: object, **kwargs: object):
        nonlocal writes
        writes += 1
        return real_write(*args, **kwargs)

    monkeypatch.setattr(vault_module, "batch_atomic_write", observed_write)
    crashed = False

    def crash_once(point: str) -> None:
        nonlocal crashed
        if point == "after-terminal" and not crashed:
            crashed = True
            raise SimulatedPublicationCrash

    monkeypatch.setattr(
        consolidation_effect_coordinator,
        "_crash_point",
        crash_once,
    )
    with pytest.raises(SimulatedPublicationCrash):
        consolidation_content_publication.publish_stored_content_batches(**arguments)

    assert target.read_bytes() == after
    assert writes == 1
    assert (
        consolidation_seal.ConsolidationSealStore(vault)
        .load(vault_binding_digest=VAULT_BINDING)
        .phase
        == "publishing"
    )
    batch_store = consolidation_batch_journal.ConsolidationBatchJournalStore(
        vault,
        run_id=RUN_ID,
    )
    assert batch_store.load().batches[0].status == "prepared"
    assert (
        consolidation_effect_coordinator.ConsolidationEffectJournalStore(
            vault,
            run_id=RUN_ID,
            effect_ordinal=17,
        )
        .load()
        .status
        == "prepared"
    )

    result = consolidation_content_publication.publish_stored_content_batches(**arguments)

    assert result.seal_state.phase == "rebuilding"
    assert target.read_bytes() == after
    assert writes == 1
    assert batch_store.load().batches[0].status == "final"
    assert batch_store.load().publication_boundary_committed is True
    assert [record["phase"] for record in _content_records(vault)] == [
        "intent",
        "committed",
    ]

    replayed = consolidation_content_publication.publish_stored_content_batches(
        **arguments
    )

    assert replayed.seal_state.phase == "rebuilding"
    assert replayed.committed_batch_ordinals == (0,)
    assert target.read_bytes() == after
    assert writes == 1
    assert [record["phase"] for record in _content_records(vault)] == [
        "intent",
        "committed",
    ]


def test_publishing_phase_refuses_a_missing_batch_journal_without_recreating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_authority,
        consolidation_batch_journal,
        consolidation_content_publication,
        consolidation_seal,
    )

    after = b"approved"
    actions = (
        _action(
            0,
            batch_ordinal=0,
            destination_path="Knowledge Base/Notes/new.bin",
            before=None,
            after=after,
        ),
    )
    vault, _partition, _loads, arguments = _setup(
        tmp_path,
        monkeypatch,
        actions,
        (after,),
    )
    store = consolidation_seal.ConsolidationSealStore(vault)
    current = store.load(vault_binding_digest=VAULT_BINDING)
    store.advance_consolidation(
        consolidation_authority.issue_authority(
            vault_binding_digest=VAULT_BINDING,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            phase="policy-active",
            action="apply",
        ),
        vault_binding_digest=VAULT_BINDING,
        action="apply",
        target_phase="publishing",
        recorded_at=T4,
        expected_revision=current.revision,
    )
    batch_store = consolidation_batch_journal.ConsolidationBatchJournalStore(
        vault,
        run_id=RUN_ID,
    )
    assert not batch_store.path.exists()

    with pytest.raises(
        consolidation_content_publication.ConsolidationContentPublicationUnavailable
    ):
        consolidation_content_publication.publish_stored_content_batches(**arguments)

    assert not batch_store.path.exists()
    assert not (vault / "Knowledge Base" / "Notes" / "new.bin").exists()


def test_rebuilding_phase_refuses_an_incomplete_batch_journal_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_authority,
        consolidation_batch_journal,
        consolidation_content_publication,
        consolidation_seal,
    )

    after = b"approved"
    actions = (
        _action(
            0,
            batch_ordinal=0,
            destination_path="Knowledge Base/Notes/new.bin",
            before=None,
            after=after,
        ),
    )
    vault, partition, _loads, arguments = _setup(
        tmp_path,
        monkeypatch,
        actions,
        (after,),
    )
    batch_store = consolidation_batch_journal.ConsolidationBatchJournalStore(
        vault,
        run_id=RUN_ID,
    )
    assert (
        batch_store.create(
            operation_id=OPERATION_ID,
            request_digest=REQUEST_DIGEST,
            partition=partition,
        )
        .batches[0]
        .status
        == "prior"
    )
    seal_store = consolidation_seal.ConsolidationSealStore(vault)
    current = seal_store.load(vault_binding_digest=VAULT_BINDING)
    for source, target, recorded_at in (
        ("policy-active", "publishing", T4),
        ("publishing", "rebuilding", T5),
    ):
        current = seal_store.advance_consolidation(
            consolidation_authority.issue_authority(
                vault_binding_digest=VAULT_BINDING,
                run_id=RUN_ID,
                operation_id=OPERATION_ID,
                journal_digest=JOURNAL_DIGEST,
                phase=source,
                action="apply",
            ),
            vault_binding_digest=VAULT_BINDING,
            action="apply",
            target_phase=target,
            recorded_at=recorded_at,
            expected_revision=current.revision,
        )

    with pytest.raises(
        consolidation_content_publication.ConsolidationContentPublicationUnavailable
    ):
        consolidation_content_publication.publish_stored_content_batches(**arguments)

    assert batch_store.load().batches[0].status == "prior"
    assert not (vault / "Knowledge Base" / "Notes" / "new.bin").exists()
    assert _content_records(vault) == []


def test_publication_refuses_an_artifact_store_inside_the_destination_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_content_publication,
        consolidation_intake,
    )

    after = b"approved"
    actions = (
        _action(
            0,
            batch_ordinal=0,
            destination_path="Knowledge Base/Notes/new.bin",
            before=None,
            after=after,
        ),
    )
    vault, _partition, _loads, arguments = _setup(
        tmp_path,
        monkeypatch,
        actions,
        (after,),
    )
    nested_store = consolidation_intake.PrivateConsolidationArtifactStore(
        vault / "private-artifacts",
        active_vault_roots=(),
    )
    _install_object(nested_store, tmp_path, after)
    arguments["artifact_store"] = nested_store

    with pytest.raises(
        consolidation_content_publication.ConsolidationContentPublicationUnavailable
    ):
        consolidation_content_publication.publish_stored_content_batches(**arguments)

    assert not (vault / "Knowledge Base" / "Notes" / "new.bin").exists()


def test_removal_identity_race_is_normalized_to_content_free_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import reserved_paths
    from exomem.governance import consolidation_content_publication

    before = b"reviewed removal"
    actions = (
        {
            "ordinal": 0,
            "batch_ordinal": 0,
            "action": "remove",
            "object_ref": "approved-removal",
            "source_path": "Knowledge Base/Notes/source.md",
            "destination_path": "Knowledge Base/Notes/remove.md",
            "expected_before_state": "present",
            "expected_before_sha256": hashlib.sha256(before).hexdigest(),
            "planned_after_state": "absent",
            "planned_after_sha256": "0" * 64,
        },
    )
    vault, _partition, _loads, arguments = _setup(
        tmp_path,
        monkeypatch,
        actions,
        (),
    )
    target = vault / "Knowledge Base" / "Notes" / "remove.md"
    target.write_bytes(before)

    def changed_identity(*_args: object, **_kwargs: object) -> None:
        raise reserved_paths.ReservedPathLeafError("IDENTITY_CHANGED")

    monkeypatch.setattr(
        reserved_paths,
        "unlink_generic_file",
        changed_identity,
    )

    with pytest.raises(
        consolidation_content_publication.ConsolidationContentPublicationUnavailable
    ):
        consolidation_content_publication.publish_stored_content_batches(**arguments)

    assert target.read_bytes() == before
