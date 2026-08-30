from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

VAULT_BINDING = hashlib.sha256(b"saga-vault-binding").hexdigest()
RUN_ID = "00000000-0000-4000-8000-000000000071"
OPERATION_ID = "00000000-0000-4000-8000-000000000072"
JOURNAL_DIGEST = hashlib.sha256(b"saga-journal").hexdigest()
POLICY_FINGERPRINT = hashlib.sha256(b"saga-policy").hexdigest()
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


def _content_action(
    ordinal: int,
    batch_ordinal: int,
    *,
    before: str = "present",
    after: str = "present",
) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "batch_ordinal": batch_ordinal,
        "action": "overwrite",
        "object_ref": f"source-object-{ordinal}",
        "source_path": f"Knowledge Base/Notes/source-{ordinal}.md",
        "destination_path": f"Knowledge Base/Notes/destination-{ordinal}.md",
        "expected_before_state": before,
        "expected_before_sha256": (
            hashlib.sha256(f"before-{ordinal}".encode()).hexdigest()
            if before == "present"
            else "0" * 64
        ),
        "planned_after_state": after,
        "planned_after_sha256": (
            hashlib.sha256(f"after-{ordinal}".encode()).hexdigest()
            if after == "present"
            else "0" * 64
        ),
    }


def test_journal_batch_partition_is_deterministic_and_binds_each_state() -> None:
    from exomem.governance import consolidation_plan

    actions = (
        _content_action(0, 0),
        _content_action(1, 0, before="absent"),
        _content_action(2, 1, after="absent"),
    )

    first = consolidation_plan.derive_journal_batch_partition(actions)
    second = consolidation_plan.derive_journal_batch_partition(
        tuple(dict(row) for row in actions)
    )

    assert first == second
    assert first.digest == "0159532ea78c3e08be2803c955b2f78d03092a049371a1e4611a2078c2db20d2"
    assert consolidation_plan.parse_journal_batch_partition(first.canonical_bytes) == first
    assert first.preimage["schema"] == "exomem.consolidation-journal-batch-partition/v1"
    assert first.preimage["action_count"] == 3
    assert first.preimage["batch_count"] == 2
    batches = first.preimage["batches"]
    assert isinstance(batches, tuple)
    assert [batch["batch_ordinal"] for batch in batches] == [0, 1]
    assert [batch["publication_boundary"] for batch in batches] == [True, False]
    for batch in batches:
        assert batch["action_set_digest"] not in {
            batch["prior_fingerprint"],
            batch["prepared_fingerprint"],
            batch["final_fingerprint"],
        }
        assert len(
            {
                batch["prior_fingerprint"],
                batch["prepared_fingerprint"],
                batch["final_fingerprint"],
            }
        ) == 3


@pytest.mark.parametrize(
    "actions",
    [
        (_content_action(0, 1),),
        (_content_action(0, 0), _content_action(1, 2)),
        (_content_action(0, 0), _content_action(1, 1), _content_action(2, 0)),
    ],
)
def test_journal_batch_partition_rejects_noncanonical_batch_ordinals(
    actions: tuple[dict[str, object], ...],
) -> None:
    from exomem.governance import consolidation_plan

    with pytest.raises(consolidation_plan.ConsolidationPlanUnavailable):
        consolidation_plan.derive_journal_batch_partition(actions)


def test_journal_batch_partition_has_a_fixed_action_bound() -> None:
    from exomem.governance import consolidation_plan

    actions = tuple(
        _content_action(ordinal, 0)
        for ordinal in range(consolidation_plan.MAX_CONTENT_BATCH_ACTIONS + 1)
    )

    with pytest.raises(consolidation_plan.ConsolidationPlanUnavailable):
        consolidation_plan.derive_journal_batch_partition(actions)


def test_policy_terminal_precedes_every_content_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_plan, consolidation_saga

    actions = (_content_action(0, 0), _content_action(1, 1))
    partition = consolidation_plan.derive_journal_batch_partition(actions)
    events: list[str] = []

    def activate_policy() -> consolidation_saga.PolicyActivationTerminal:
        events.extend(("policy-intent", "policy-prepare", "policy-activate", "policy-terminal"))
        intent_id = hashlib.sha256(b"policy-intent").hexdigest()
        return consolidation_saga.PolicyActivationTerminal(
            schema="exomem.consolidation-policy-activation-terminal/v1",
            policy_fingerprint=POLICY_FINGERPRINT,
            intent_event_id=intent_id,
            prepared_fingerprint=hashlib.sha256(b"policy-prepared").hexdigest(),
            active_fingerprint=hashlib.sha256(b"policy-active").hexdigest(),
            terminal_event_id=f"{intent_id}:committed",
        )

    class Journal:
        def prepare_batch(self, batch: consolidation_saga.ContentBatch) -> None:
            expected = partition.preimage["batches"][batch.ordinal]
            assert batch.action_set_digest == expected["action_set_digest"]
            assert batch.prior_fingerprint == expected["prior_fingerprint"]
            assert batch.prepared_fingerprint == expected["prepared_fingerprint"]
            assert batch.final_fingerprint == expected["final_fingerprint"]
            assert batch.publication_boundary is (batch.ordinal == 0)
            events.append(f"journal-prepare:{batch.ordinal}")

        def commit_batch(self, batch: consolidation_saga.ContentBatch) -> None:
            events.append(f"journal-final:{batch.ordinal}")

    def batch_atomic_write(
        writes: object,
        *,
        vault_root: Path,
        post_commit_fanout: bool,
    ) -> list[Path]:
        assert tuple(writes) == ()  # type: ignore[arg-type]
        assert vault_root == tmp_path
        assert post_commit_fanout is False
        batch_ordinal = sum(event.startswith("batch_atomic_write:") for event in events)
        events.append(f"batch_atomic_write:{batch_ordinal}")
        return []

    monkeypatch.setattr(consolidation_saga.vault, "batch_atomic_write", batch_atomic_write)

    result = consolidation_saga.publish_policy_first(
        content_actions=actions,
        approved_partition_digest=partition.digest,
        expected_policy_fingerprint=POLICY_FINGERPRINT,
        activate_policy=activate_policy,
        journal=Journal(),
        vault_root=tmp_path,
        materialize_batch=lambda batch: (),
    )

    assert events == [
        "policy-intent",
        "policy-prepare",
        "policy-activate",
        "policy-terminal",
        "journal-prepare:0",
        "batch_atomic_write:0",
        "journal-final:0",
        "journal-prepare:1",
        "batch_atomic_write:1",
        "journal-final:1",
    ]
    assert result.policy_terminal.policy_fingerprint == POLICY_FINGERPRINT
    assert result.publication_boundary_ordinal == 0
    assert result.committed_batch_ordinals == (0, 1)


def test_policy_activation_failure_never_reaches_content_publication() -> None:
    from exomem.governance import consolidation_plan, consolidation_saga

    actions = (_content_action(0, 0),)
    partition = consolidation_plan.derive_journal_batch_partition(actions)
    events: list[str] = []

    def activate_policy() -> consolidation_saga.PolicyActivationTerminal:
        events.append("policy-refused")
        raise RuntimeError("activation did not reach its critical terminal")

    class Journal:
        def prepare_batch(self, batch: consolidation_saga.ContentBatch) -> None:
            events.append(f"journal-prepare:{batch.ordinal}")

        def commit_batch(self, batch: consolidation_saga.ContentBatch) -> None:
            events.append(f"journal-final:{batch.ordinal}")

    with pytest.raises(RuntimeError, match="critical terminal"):
        consolidation_saga.publish_policy_first(
            content_actions=actions,
            approved_partition_digest=partition.digest,
            expected_policy_fingerprint=POLICY_FINGERPRINT,
            activate_policy=activate_policy,
            journal=Journal(),
            vault_root=Path("unused"),
            materialize_batch=lambda batch: (),
        )

    assert events == ["policy-refused"]


def test_changed_approved_batch_partition_refuses_before_policy_activation() -> None:
    from exomem.governance import consolidation_saga

    actions = (_content_action(0, 0),)
    effects: list[str] = []

    with pytest.raises(consolidation_saga.PolicyFirstPublicationUnavailable):
        consolidation_saga.publish_policy_first(
            content_actions=actions,
            approved_partition_digest=hashlib.sha256(b"different-partition").hexdigest(),
            expected_policy_fingerprint=POLICY_FINGERPRINT,
            activate_policy=lambda: effects.append("policy") or None,  # type: ignore[arg-type,return-value]
            journal=None,  # type: ignore[arg-type]
            vault_root=Path("unused"),
            materialize_batch=lambda batch: effects.append(f"batch:{batch.ordinal}") or (),
        )

    assert effects == []


def test_noncommitted_policy_terminal_never_reaches_batch_journal() -> None:
    from exomem.governance import consolidation_plan, consolidation_saga

    actions = (_content_action(0, 0),)
    partition = consolidation_plan.derive_journal_batch_partition(actions)
    effects: list[str] = []
    intent_id = hashlib.sha256(b"policy-intent").hexdigest()

    with pytest.raises(consolidation_saga.PolicyFirstPublicationUnavailable):
        consolidation_saga.publish_policy_first(
            content_actions=actions,
            approved_partition_digest=partition.digest,
            expected_policy_fingerprint=POLICY_FINGERPRINT,
            activate_policy=lambda: consolidation_saga.PolicyActivationTerminal(
                schema="exomem.consolidation-policy-activation-terminal/v1",
                policy_fingerprint=POLICY_FINGERPRINT,
                intent_event_id=intent_id,
                prepared_fingerprint=hashlib.sha256(b"policy-prepared").hexdigest(),
                active_fingerprint=hashlib.sha256(b"policy-active").hexdigest(),
                terminal_event_id=f"{intent_id}:aborted",
            ),
            journal=None,  # type: ignore[arg-type]
            vault_root=Path("unused"),
            materialize_batch=lambda batch: effects.append(f"batch:{batch.ordinal}") or (),
        )

    assert effects == []
