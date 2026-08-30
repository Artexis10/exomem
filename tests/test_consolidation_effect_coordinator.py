"""Receipt-first execution and recovery for one consolidation effect."""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from exomem.governance import consolidation_receipts

RUN_ID = "00000000-0000-4000-8000-0000000000b1"
OPERATION_ID = "00000000-0000-4000-8000-0000000000b2"
REQUEST_DIGEST = hashlib.sha256(b"effect-request").hexdigest()
PRIOR_DIGEST = hashlib.sha256(b"effect-prior").hexdigest()
TARGET_DIGEST = hashlib.sha256(b"effect-target").hexdigest()
MIXED_DIGEST = hashlib.sha256(b"effect-mixed").hexdigest()
T0 = "2026-08-30T12:00:00Z"


class SimulatedProcessCrash(BaseException):
    """Bypass ordinary exception recovery like abrupt process loss."""


@pytest.fixture(autouse=True)
def _private_writer_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        str(tmp_path / "writer-state"),
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    return root


def _start_event() -> consolidation_receipts.ConsolidationEvent:
    root_id, root_digest = consolidation_receipts.semantic_root()
    return consolidation_receipts.build_intent(
        kind="start",
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        phase="intake",
        effect_ordinal=0,
        request_digest=REQUEST_DIGEST,
        prior_digest=PRIOR_DIGEST,
        target_digest=TARGET_DIGEST,
        evidence=consolidation_receipts.build_evidence(
            kind="start",
            digests={
                "identity_binding_digest": hashlib.sha256(b"identity").hexdigest(),
                "run_request_digest": REQUEST_DIGEST,
            },
        ),
        semantic_parent_event_id=root_id,
        semantic_parent_payload_digest=root_digest,
    )


def _engine(vault: Path):
    from exomem.governance import consolidation_effect_coordinator

    return consolidation_effect_coordinator.ConsolidationEffectJournalStore(
        vault,
        run_id=RUN_ID,
        effect_ordinal=0,
    )


def _journal(vault: Path, effect_ordinal: int):
    from exomem.governance import consolidation_effect_coordinator

    return consolidation_effect_coordinator.ConsolidationEffectJournalStore(
        vault,
        run_id=RUN_ID,
        effect_ordinal=effect_ordinal,
    )


def _observation(state: str):
    from exomem.governance import consolidation_effect_coordinator

    digest = {
        "prior": PRIOR_DIGEST,
        "target": TARGET_DIGEST,
        "mixed": MIXED_DIGEST,
    }[state]
    return consolidation_effect_coordinator.EffectObservation(
        state=state,
        digest=digest,
    )


def _append_policy_active_parent(vault: Path) -> dict[str, object]:
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
        ("seal-intent", "sealing", None),
        ("seal-drained", "sealed", None),
        ("preimage", "preimage", None),
        ("policy-prepare", "policy", None),
        ("policy-active", "policy", None),
    )
    root_id, root_digest = consolidation_receipts.semantic_root()
    parent_id = root_id
    parent_digest = root_digest
    terminal: dict[str, object] | None = None
    for ordinal, (kind, phase, page_ordinal) in enumerate(chain):
        fields = consolidation_receipts._EVIDENCE_FIELDS[kind]  # noqa: SLF001
        event = consolidation_receipts.build_intent(
            kind=kind,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            phase=phase,
            effect_ordinal=ordinal,
            request_digest=REQUEST_DIGEST,
            prior_digest=hashlib.sha256(f"{kind}:prior".encode()).hexdigest(),
            target_digest=hashlib.sha256(f"{kind}:target".encode()).hexdigest(),
            prepared_digest=(
                hashlib.sha256(b"preimage:prepared").hexdigest()
                if kind == "preimage"
                else None
            ),
            evidence=consolidation_receipts.build_evidence(
                kind=kind,
                digests={
                    field: hashlib.sha256(f"{kind}:{field}".encode()).hexdigest()
                    for field in fields
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


def _content_batch_event(
    vault: Path,
    actions: tuple[dict[str, object], ...],
    *,
    effect_ordinal: int = 17,
):
    from exomem.governance import (
        consolidation_effect_coordinator,
        consolidation_plan,
        consolidation_saga,
    )

    parent = _append_policy_active_parent(vault)
    partition = consolidation_plan.derive_journal_batch_partition(actions)
    batch = consolidation_saga._content_batches(partition)[0]  # noqa: SLF001
    event = consolidation_receipts.build_intent(
        kind="content-batch",
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        phase="publishing",
        effect_ordinal=effect_ordinal,
        batch_ordinal=0,
        request_digest=REQUEST_DIGEST,
        prior_digest=batch.prior_fingerprint,
        prepared_digest=batch.prepared_fingerprint,
        target_digest=batch.final_fingerprint,
        evidence=consolidation_receipts.build_evidence(
            kind="content-batch",
            digests={
                "batch_manifest_digest": batch.action_set_digest,
                "classification_digest": (
                    consolidation_saga._content_batch_classification_digest(  # noqa: SLF001
                        batch
                    )
                ),
            },
        ),
        semantic_parent_event_id=str(parent["event_id"]),
        semantic_parent_payload_digest=str(
            parent["consolidation_event"]["payload_digest"]
        ),
    )
    journal = consolidation_effect_coordinator.ConsolidationEffectJournalStore(
        vault,
        run_id=RUN_ID,
        effect_ordinal=effect_ordinal,
    )
    return batch, event, journal


def test_effect_uses_exact_receipt_first_order_and_persists_references(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_effect_coordinator, receipts

    live = {"state": "prior"}
    order: list[str] = []
    monkeypatch.setattr(
        consolidation_effect_coordinator,
        "_crash_point",
        order.append,
    )

    result = consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
        vault_root=vault,
        event=_start_event(),
        journal=_engine(vault),
        classify=lambda: _observation(live["state"]),
        apply_effect=lambda: live.__setitem__("state", "target"),
        timestamp=T0,
    )

    assert order == [
        "after-intent",
        "after-prepared",
        "after-effect",
        "after-classification",
        "after-terminal",
        "after-final",
    ]
    assert result.role == "committed"
    assert result.observed_digest == TARGET_DIGEST
    state = _engine(vault).load()
    assert state.status == "final"
    assert state.intent.event_id == _start_event().event_id
    assert state.intent.record_hash == state.intent.receipt_head_digest
    assert state.terminal is not None
    assert state.terminal.event_id == f"{_start_event().event_id}:committed"
    assert state.terminal.record_hash == state.terminal.receipt_head_digest
    assert state.observed_state == "target"
    assert state.observed_digest == TARGET_DIGEST
    assert [record["phase"] for record in receipts.event_records(vault)] == [
        "intent",
        "committed",
    ]


@pytest.mark.parametrize(
    "crash_seam",
    (
        "after-prepared",
        "after-effect",
        "after-classification",
        "after-terminal",
        "after-final",
    ),
)
def test_restart_repairs_each_cross_store_gap_without_repeating_effect(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_seam: str,
) -> None:
    from exomem.governance import consolidation_effect_coordinator, receipts

    live = {"state": "prior"}
    effects = 0

    def apply() -> None:
        nonlocal effects
        effects += 1
        live["state"] = "target"

    def crash(point: str) -> None:
        if point == crash_seam:
            raise SimulatedProcessCrash

    monkeypatch.setattr(consolidation_effect_coordinator, "_crash_point", crash)
    with pytest.raises(SimulatedProcessCrash):
        consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=vault,
            event=_start_event(),
            journal=_engine(vault),
            classify=lambda: _observation(live["state"]),
            apply_effect=apply,
            timestamp=T0,
        )

    monkeypatch.setattr(
        consolidation_effect_coordinator,
        "_crash_point",
        lambda _point: None,
    )
    result = consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
        vault_root=vault,
        event=_start_event(),
        journal=_engine(vault),
        classify=lambda: _observation(live["state"]),
        apply_effect=apply,
        timestamp=T0,
    )

    assert effects == 1
    assert result.role == "committed"
    assert _engine(vault).load().status == "final"
    assert len(receipts.event_records(vault)) == 2


def test_intent_without_prepared_journal_aborts_on_prior_without_effect(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_effect_coordinator, receipts

    live = {"state": "prior"}
    effects = 0

    def apply() -> None:
        nonlocal effects
        effects += 1
        live["state"] = "target"

    def crash(point: str) -> None:
        if point == "after-intent":
            raise SimulatedProcessCrash

    monkeypatch.setattr(consolidation_effect_coordinator, "_crash_point", crash)
    with pytest.raises(SimulatedProcessCrash):
        consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=vault,
            event=_start_event(),
            journal=_engine(vault),
            classify=lambda: _observation(live["state"]),
            apply_effect=apply,
            timestamp=T0,
        )

    monkeypatch.setattr(
        consolidation_effect_coordinator,
        "_crash_point",
        lambda _point: None,
    )
    with pytest.raises(
        consolidation_effect_coordinator.ConsolidationEffectUnavailable,
        match="^CONSOLIDATION_EFFECT_UNAVAILABLE$",
    ):
        consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=vault,
            event=_start_event(),
            journal=_engine(vault),
            classify=lambda: _observation(live["state"]),
            apply_effect=apply,
            timestamp=T0,
        )

    assert effects == 0
    final = _engine(vault).load()
    assert final.status == "final"
    assert final.revision == 1
    assert final.terminal is not None
    assert final.terminal.event_id.endswith(":aborted")
    assert final.observed_state == "prior"
    assert [record["phase"] for record in receipts.event_records(vault)] == [
        "intent",
        "aborted",
    ]


def test_mixed_state_after_preparation_stays_blocked_without_terminal(
    vault: Path,
) -> None:
    from exomem.governance import consolidation_effect_coordinator, receipts

    states = iter((_observation("prior"), _observation("mixed")))
    with pytest.raises(
        consolidation_effect_coordinator.ConsolidationEffectUnavailable,
        match="^CONSOLIDATION_EFFECT_UNAVAILABLE$",
    ):
        consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=vault,
            event=_start_event(),
            journal=_engine(vault),
            classify=lambda: next(states),
            apply_effect=lambda: None,
            timestamp=T0,
        )

    assert _engine(vault).load().status == "prepared"
    assert [record["phase"] for record in receipts.event_records(vault)] == ["intent"]


def test_intent_without_prepared_journal_blocks_if_target_already_exists(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_effect_coordinator, receipts

    def crash(point: str) -> None:
        if point == "after-intent":
            raise SimulatedProcessCrash

    monkeypatch.setattr(consolidation_effect_coordinator, "_crash_point", crash)
    with pytest.raises(SimulatedProcessCrash):
        consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=vault,
            event=_start_event(),
            journal=_engine(vault),
            classify=lambda: _observation("prior"),
            apply_effect=lambda: None,
            timestamp=T0,
        )

    monkeypatch.setattr(
        consolidation_effect_coordinator,
        "_crash_point",
        lambda _point: None,
    )
    with pytest.raises(
        consolidation_effect_coordinator.ConsolidationEffectUnavailable,
        match="^CONSOLIDATION_EFFECT_UNAVAILABLE$",
    ):
        consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=vault,
            event=_start_event(),
            journal=_engine(vault),
            classify=lambda: _observation("target"),
            apply_effect=lambda: pytest.fail("target without journal must not replay"),
            timestamp=T0,
        )

    assert not _engine(vault).path.exists()
    assert [record["phase"] for record in receipts.event_records(vault)] == ["intent"]


def test_new_intent_does_not_legalize_a_preexisting_target(vault: Path) -> None:
    from exomem.governance import consolidation_effect_coordinator, receipts

    effects = 0

    def apply() -> None:
        nonlocal effects
        effects += 1

    with pytest.raises(
        consolidation_effect_coordinator.ConsolidationEffectUnavailable,
        match="^CONSOLIDATION_EFFECT_UNAVAILABLE$",
    ):
        consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=vault,
            event=_start_event(),
            journal=_engine(vault),
            classify=lambda: _observation("target"),
            apply_effect=apply,
            timestamp=T0,
        )

    assert effects == 0
    assert not _engine(vault).path.exists()
    assert [record["phase"] for record in receipts.event_records(vault)] == ["intent"]


def test_final_journal_with_missing_or_changed_terminal_never_replays_success(
    vault: Path,
) -> None:
    from exomem.governance import consolidation_effect_coordinator

    live = {"state": "prior"}
    consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
        vault_root=vault,
        event=_start_event(),
        journal=_engine(vault),
        classify=lambda: _observation(live["state"]),
        apply_effect=lambda: live.__setitem__("state", "target"),
        timestamp=T0,
    )
    path = _engine(vault).path
    raw = path.read_bytes()
    terminal_id = f"{_start_event().event_id}:committed".encode()
    path.write_bytes(raw.replace(terminal_id, b"f" * len(terminal_id)))

    with pytest.raises(
        consolidation_effect_coordinator.ConsolidationEffectUnavailable,
        match="^CONSOLIDATION_EFFECT_UNAVAILABLE$",
    ):
        _engine(vault).load()


def test_real_content_batch_uses_receipt_first_engine_and_held_batch_writer(
    vault: Path,
) -> None:
    from exomem import vault as exomem_vault
    from exomem.governance import (
        consolidation_effect_coordinator,
        consolidation_plan,
        consolidation_saga,
        receipts,
    )

    parent = _append_policy_active_parent(vault)
    target = vault / "Knowledge Base" / "Notes" / "target.md"
    target.parent.mkdir(parents=True)
    target.write_text("before", encoding="utf-8")
    before_digest = hashlib.sha256(b"before").hexdigest()
    after_digest = hashlib.sha256(b"after").hexdigest()
    actions = (
        {
            "ordinal": 0,
            "batch_ordinal": 0,
            "action": "overwrite",
            "object_ref": "source-object-0",
            "source_path": "Knowledge Base/Notes/source.md",
            "destination_path": "Knowledge Base/Notes/target.md",
            "expected_before_state": "present",
            "expected_before_sha256": before_digest,
            "planned_after_state": "present",
            "planned_after_sha256": after_digest,
        },
    )
    partition = consolidation_plan.derive_journal_batch_partition(actions)
    batch = consolidation_saga._content_batches(partition)[0]  # noqa: SLF001
    classification_digest = consolidation_saga._content_batch_classification_digest(  # noqa: SLF001
        batch
    )
    event = consolidation_receipts.build_intent(
        kind="content-batch",
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        phase="publishing",
        effect_ordinal=17,
        batch_ordinal=0,
        request_digest=REQUEST_DIGEST,
        prior_digest=batch.prior_fingerprint,
        prepared_digest=batch.prepared_fingerprint,
        target_digest=batch.final_fingerprint,
        evidence=consolidation_receipts.build_evidence(
            kind="content-batch",
            digests={
                "batch_manifest_digest": batch.action_set_digest,
                "classification_digest": classification_digest,
            },
        ),
        semantic_parent_event_id=str(parent["event_id"]),
        semantic_parent_payload_digest=str(
            parent["consolidation_event"]["payload_digest"]
        ),
    )
    journal = _journal(vault, 17)

    result = consolidation_saga.publish_content_batch_receipt_first(
        content_actions=actions,
        batch=batch,
        event=event,
        journal=journal,
        vault_root=vault,
        materialize_batch=lambda _batch: (
            exomem_vault.PlannedWrite(
                path=target,
                content="after",
                expected_hash=before_digest,
            ),
        ),
        timestamp=T0,
    )

    assert target.read_text(encoding="utf-8") == "after"
    assert result.role == "committed"
    assert result.observed_digest == batch.final_fingerprint
    final = journal.load()
    assert final.status == "final"
    assert final.kind == "content-batch"
    assert final.observed_state == "target"
    assert [record["phase"] for record in receipts.event_records(vault)[-2:]] == [
        "intent",
        "committed",
    ]
    assert isinstance(
        journal,
        consolidation_effect_coordinator.ConsolidationEffectJournalStore,
    )


def test_equivalent_content_batch_commits_without_materialization(vault: Path) -> None:
    from exomem.governance import consolidation_saga

    target = vault / "Knowledge Base" / "Notes" / "same.md"
    target.parent.mkdir(parents=True)
    target.write_text("same", encoding="utf-8")
    digest = hashlib.sha256(b"same").hexdigest()
    actions = (
        {
            "ordinal": 0,
            "batch_ordinal": 0,
            "action": "reuse_destination",
            "object_ref": "source-object-same",
            "source_path": "Knowledge Base/Notes/source-same.md",
            "destination_path": "Knowledge Base/Notes/same.md",
            "expected_before_state": "present",
            "expected_before_sha256": digest,
            "planned_after_state": "present",
            "planned_after_sha256": digest,
        },
    )
    batch, event, journal = _content_batch_event(vault, actions)

    result = consolidation_saga.publish_content_batch_receipt_first(
        content_actions=actions,
        batch=batch,
        event=event,
        journal=journal,
        vault_root=vault,
        materialize_batch=lambda _batch: pytest.fail(
            "equivalent batches must not materialize"
        ),
        timestamp=T0,
    )

    assert result.role == "committed"
    assert result.observed_state == "target"
    assert target.read_text(encoding="utf-8") == "same"


def test_equivalent_batch_crash_after_intent_closes_aborted_without_effect(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_effect_coordinator, consolidation_saga

    target = vault / "Knowledge Base" / "Notes" / "same.md"
    target.parent.mkdir(parents=True)
    target.write_text("same", encoding="utf-8")
    digest = hashlib.sha256(b"same").hexdigest()
    actions = (
        {
            "ordinal": 0,
            "batch_ordinal": 0,
            "action": "reuse_destination",
            "object_ref": "source-object-same",
            "source_path": "Knowledge Base/Notes/source-same.md",
            "destination_path": "Knowledge Base/Notes/same.md",
            "expected_before_state": "present",
            "expected_before_sha256": digest,
            "planned_after_state": "present",
            "planned_after_sha256": digest,
        },
    )
    batch, event, journal = _content_batch_event(vault, actions)

    monkeypatch.setattr(
        consolidation_effect_coordinator,
        "_crash_point",
        lambda point: (_ for _ in ()).throw(SimulatedProcessCrash)
        if point == "after-intent"
        else None,
    )
    with pytest.raises(SimulatedProcessCrash):
        consolidation_saga.publish_content_batch_receipt_first(
            content_actions=actions,
            batch=batch,
            event=event,
            journal=journal,
            vault_root=vault,
            materialize_batch=lambda _batch: pytest.fail(
                "equivalent batches must not materialize"
            ),
            timestamp=T0,
        )

    monkeypatch.setattr(
        consolidation_effect_coordinator,
        "_crash_point",
        lambda _point: None,
    )
    with pytest.raises(
        consolidation_effect_coordinator.ConsolidationEffectUnavailable,
        match="^CONSOLIDATION_EFFECT_UNAVAILABLE$",
    ):
        consolidation_saga.publish_content_batch_receipt_first(
            content_actions=actions,
            batch=batch,
            event=event,
            journal=journal,
            vault_root=vault,
            materialize_batch=lambda _batch: pytest.fail(
                "unprepared equivalent recovery must not materialize"
            ),
            timestamp=T0,
        )

    final = journal.load()
    assert final.status == "final"
    assert final.observed_state == "prior"
    assert final.terminal is not None
    assert final.terminal.event_id.endswith(":aborted")


def test_remove_content_action_is_applied_before_commit(vault: Path) -> None:
    from exomem import held_fs
    from exomem.governance import consolidation_saga

    target = vault / "Knowledge Base" / "Notes" / "remove.md"
    target.parent.mkdir(parents=True)
    target.write_text("remove me", encoding="utf-8")
    digest = hashlib.sha256(b"remove me").hexdigest()
    actions = (
        {
            "ordinal": 0,
            "batch_ordinal": 0,
            "action": "remove",
            "object_ref": "destination-object-remove",
            "source_path": "Knowledge Base/Notes/source-remove.md",
            "destination_path": "Knowledge Base/Notes/remove.md",
            "expected_before_state": "present",
            "expected_before_sha256": digest,
            "planned_after_state": "absent",
            "planned_after_sha256": "0" * 64,
        },
    )
    batch, event, journal = _content_batch_event(vault, actions)
    flush_observations: list[bool] = []
    with held_fs.acquire(vault).require() as filesystem:
        filesystem_type = type(filesystem)
    original_flush = filesystem_type.flush_directory

    def observed_flush(filesystem, parent):
        flush_observations.append(not target.exists())
        return original_flush(filesystem, parent)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(filesystem_type, "flush_directory", observed_flush)

        result = consolidation_saga.publish_content_batch_receipt_first(
            content_actions=actions,
            batch=batch,
            event=event,
            journal=journal,
            vault_root=vault,
            materialize_batch=lambda _batch: (),
            timestamp=T0,
        )

    assert not target.exists()
    assert flush_observations == [True]
    assert result.role == "committed"
    assert result.observed_state == "target"


def test_remove_refuses_same_identity_with_changed_bytes(vault: Path) -> None:
    from exomem import reserved_paths

    target = vault / "Knowledge Base" / "Notes" / "changed.md"
    target.parent.mkdir(parents=True)
    target.write_text("reviewed", encoding="utf-8")
    identity = reserved_paths.inspect_generic_file(
        vault,
        "Knowledge Base/Notes/changed.md",
    )
    target.write_text("changed after review", encoding="utf-8")

    with pytest.raises(reserved_paths.ReservedPathLeafError) as error:
        reserved_paths.unlink_generic_file(
            vault,
            "Knowledge Base/Notes/changed.md",
            expected_identity=identity,
            expected_sha256=hashlib.sha256(b"reviewed").hexdigest(),
        )

    assert error.value.code == "CONTENT_CHANGED"
    assert target.read_text(encoding="utf-8") == "changed after review"


def test_mutation_lock_error_is_normalized_before_any_receipt(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import mutation_lock
    from exomem.cli_ops import OpError
    from exomem.governance import consolidation_effect_coordinator, receipts

    class RefusingHold:
        def __enter__(self):
            raise OpError("MUTATION_BUSY", "busy")

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        mutation_lock.VaultMutationCoordinator,
        "hold",
        lambda *_args, **_kwargs: RefusingHold(),
    )

    with pytest.raises(
        consolidation_effect_coordinator.ConsolidationEffectUnavailable,
        match="^CONSOLIDATION_EFFECT_UNAVAILABLE$",
    ):
        consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=vault,
            event=_start_event(),
            journal=_engine(vault),
            classify=lambda: _observation("prior"),
            apply_effect=lambda: pytest.fail("lock refusal must precede the effect"),
            timestamp=T0,
        )

    assert receipts.event_records(vault) == []
    assert not _engine(vault).path.exists()


def test_two_same_effect_executors_cannot_both_enter_the_effect(
    vault: Path,
) -> None:
    from exomem.governance import consolidation_effect_coordinator

    live = {"state": "prior"}
    entered = threading.Event()
    release = threading.Event()
    apply_count = 0
    apply_guard = threading.Lock()

    def apply() -> None:
        nonlocal apply_count
        with apply_guard:
            apply_count += 1
        entered.set()
        assert release.wait(timeout=2.0)
        live["state"] = "target"

    def execute():
        return consolidation_effect_coordinator._execute_effect(  # noqa: SLF001
            vault_root=vault,
            event=_start_event(),
            journal=_engine(vault),
            classify=lambda: _observation(live["state"]),
            apply_effect=apply,
            timestamp=T0,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute)
        assert entered.wait(timeout=2.0)
        second = pool.submit(execute)
        time.sleep(0.1)
        with apply_guard:
            assert apply_count == 1
        release.set()
        assert first.result(timeout=2.0).role == "committed"
        assert second.result(timeout=2.0).role == "committed"
    assert apply_count == 1
