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


@pytest.fixture(autouse=True)
def _allow_synthetic_policy_terminals_in_batch_unit_tests(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if request.node.name == (
        "test_forged_committed_policy_terminal_never_reaches_batch_journal"
    ):
        return
    from exomem.governance import consolidation_saga

    def accept_synthetic_terminal(
        *,
        vault_root: Path,
        terminal: consolidation_saga.PolicyActivationTerminal,
        expected_policy_fingerprint: str,
        vault_binding_digest: str,
    ) -> consolidation_saga.PolicyActivationTerminal:
        del vault_root
        del vault_binding_digest
        return consolidation_saga._policy_terminal(  # noqa: SLF001
            terminal,
            expected_policy_fingerprint=expected_policy_fingerprint,
        )

    monkeypatch.setattr(
        consolidation_saga,
        "_verify_policy_terminal_receipt",
        accept_synthetic_terminal,
        raising=False,
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
    for action in actions:
        target = tmp_path / str(action["destination_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"before-{action['ordinal']}".encode())

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
        def batch_status(self, batch: consolidation_saga.ContentBatch) -> str:
            return "prior"

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
        planned = tuple(writes)  # type: ignore[arg-type]
        assert len(planned) == 1
        assert vault_root == tmp_path
        assert post_commit_fanout is False
        batch_ordinal = sum(event.startswith("batch_atomic_write:") for event in events)
        events.append(f"batch_atomic_write:{batch_ordinal}")
        planned[0].path.write_bytes(f"after-{batch_ordinal}".encode())
        return [planned[0].path]

    monkeypatch.setattr(consolidation_saga.vault, "batch_atomic_write", batch_atomic_write)

    result = consolidation_saga.publish_policy_first(
        content_actions=actions,
        approved_partition_digest=partition.digest,
        expected_policy_fingerprint=POLICY_FINGERPRINT,
        vault_binding_digest=VAULT_BINDING,
        activate_policy=activate_policy,
        journal=Journal(),
        vault_root=tmp_path,
        materialize_batch=lambda batch: (
            consolidation_saga.vault.PlannedWrite(
                path=tmp_path
                / str(actions[batch.first_action_ordinal]["destination_path"]),
                content=f"after-{batch.first_action_ordinal}",
                expected_hash=str(
                    actions[batch.first_action_ordinal]["expected_before_sha256"]
                ),
            ),
        ),
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
        def batch_status(self, batch: consolidation_saga.ContentBatch) -> str:
            return "prior"

        def prepare_batch(self, batch: consolidation_saga.ContentBatch) -> None:
            events.append(f"journal-prepare:{batch.ordinal}")

        def commit_batch(self, batch: consolidation_saga.ContentBatch) -> None:
            events.append(f"journal-final:{batch.ordinal}")

    with pytest.raises(RuntimeError, match="critical terminal"):
        consolidation_saga.publish_policy_first(
            content_actions=actions,
            approved_partition_digest=partition.digest,
            expected_policy_fingerprint=POLICY_FINGERPRINT,
            vault_binding_digest=VAULT_BINDING,
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
            vault_binding_digest=VAULT_BINDING,
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
            vault_binding_digest=VAULT_BINDING,
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


def test_forged_committed_policy_terminal_never_reaches_batch_journal(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_plan, consolidation_saga

    actions = (_content_action(0, 0),)
    target = tmp_path / str(actions[0]["destination_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"before-0")
    partition = consolidation_plan.derive_journal_batch_partition(actions)
    effects: list[str] = []

    class Journal:
        def batch_status(self, batch: consolidation_saga.ContentBatch) -> str:
            effects.append(f"journal-status:{batch.ordinal}")
            return "prior"

        def prepare_batch(self, batch: consolidation_saga.ContentBatch) -> None:
            effects.append(f"journal-prepare:{batch.ordinal}")

        def commit_batch(self, batch: consolidation_saga.ContentBatch) -> None:
            effects.append(f"journal-final:{batch.ordinal}")

    with pytest.raises(consolidation_saga.PolicyFirstPublicationUnavailable):
        consolidation_saga.publish_policy_first(
            content_actions=actions,
            approved_partition_digest=partition.digest,
            expected_policy_fingerprint=POLICY_FINGERPRINT,
            vault_binding_digest=VAULT_BINDING,
            activate_policy=_publication_terminal,
            journal=Journal(),
            vault_root=tmp_path,
            materialize_batch=lambda batch: (),
        )

    assert effects == []
    assert target.read_bytes() == b"before-0"


def _publication_terminal():
    from exomem.governance import consolidation_saga

    intent_id = hashlib.sha256(b"policy-intent").hexdigest()
    return consolidation_saga.PolicyActivationTerminal(
        schema="exomem.consolidation-policy-activation-terminal/v1",
        policy_fingerprint=POLICY_FINGERPRINT,
        intent_event_id=intent_id,
        prepared_fingerprint=hashlib.sha256(b"policy-prepared").hexdigest(),
        active_fingerprint=hashlib.sha256(b"policy-active").hexdigest(),
        terminal_event_id=f"{intent_id}:committed",
    )


def _state_action(
    *,
    ordinal: int,
    batch_ordinal: int,
    destination_path: str,
    before: bytes | None,
    after: bytes | None,
) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "batch_ordinal": batch_ordinal,
        "action": "add" if before is None else "overwrite",
        "object_ref": f"source-object-{ordinal}",
        "source_path": f"Knowledge Base/Notes/source-{ordinal}.md",
        "destination_path": destination_path,
        "expected_before_state": "absent" if before is None else "present",
        "expected_before_sha256": (
            "0" * 64 if before is None else hashlib.sha256(before).hexdigest()
        ),
        "planned_after_state": "absent" if after is None else "present",
        "planned_after_sha256": (
            "0" * 64 if after is None else hashlib.sha256(after).hexdigest()
        ),
    }


@pytest.mark.parametrize(
    ("payloads", "expected"),
    [
        ((b"before-a", b"before-b"), "prior"),
        ((b"after-a", b"after-b"), "final"),
        ((b"before-a", b"after-b"), "mixed"),
        ((b"third", b"after-b"), "mixed"),
    ],
)
def test_content_batch_classifier_accepts_only_exact_prior_or_final(
    tmp_path: Path,
    payloads: tuple[bytes, bytes],
    expected: str,
) -> None:
    from exomem.governance import consolidation_plan, consolidation_saga

    root = tmp_path / "vault"
    targets = (
        "Knowledge Base/Notes/a.md",
        "Knowledge Base/Notes/b.md",
    )
    actions = tuple(
        _state_action(
            ordinal=ordinal,
            batch_ordinal=0,
            destination_path=target,
            before=f"before-{target[-4]}".encode(),
            after=f"after-{target[-4]}".encode(),
        )
        for ordinal, target in enumerate(targets)
    )
    for target, payload in zip(targets, payloads, strict=True):
        path = root / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    partition = consolidation_plan.derive_journal_batch_partition(actions)
    batch = consolidation_saga._content_batches(partition)[0]

    observation = consolidation_saga.classify_content_batch_state(
        vault_root=root,
        content_actions=actions,
        batch=batch,
    )

    assert observation.state == expected
    assert observation.batch_ordinal == 0


def test_content_batch_classifier_handles_absent_and_equivalent_rows(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_plan, consolidation_saga

    root = tmp_path / "vault"
    root.mkdir()
    absent_action = _state_action(
        ordinal=0,
        batch_ordinal=0,
        destination_path="Knowledge Base/Notes/new.md",
        before=None,
        after=b"new",
    )
    same_action = _state_action(
        ordinal=0,
        batch_ordinal=0,
        destination_path="Knowledge Base/Notes/same.md",
        before=b"same",
        after=b"same",
    )
    same_path = root / str(same_action["destination_path"])
    same_path.parent.mkdir(parents=True)
    same_path.write_bytes(b"same")

    absent_partition = consolidation_plan.derive_journal_batch_partition(
        (absent_action,)
    )
    same_partition = consolidation_plan.derive_journal_batch_partition((same_action,))

    assert consolidation_saga.classify_content_batch_state(
        vault_root=root,
        content_actions=(absent_action,),
        batch=consolidation_saga._content_batches(absent_partition)[0],
    ).state == "prior"
    assert consolidation_saga.classify_content_batch_state(
        vault_root=root,
        content_actions=(same_action,),
        batch=consolidation_saga._content_batches(same_partition)[0],
    ).state == "equivalent"


@pytest.mark.parametrize(
    ("journal_status", "live_payload", "expected_events"),
    [
        (
            "prior",
            b"before",
            ("status", "prepare", "materialize", "publish", "commit"),
        ),
        ("prepared", b"before", ("status", "materialize", "publish", "commit")),
        ("prepared", b"after", ("status", "commit")),
        ("final", b"after", ("status",)),
    ],
)
def test_policy_first_retry_performs_only_the_missing_batch_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal_status: str,
    live_payload: bytes,
    expected_events: tuple[str, ...],
) -> None:
    from exomem import vault, writer_lease
    from exomem.governance import consolidation_plan, consolidation_saga

    root = tmp_path / "vault"
    target = root / "Knowledge Base/Notes/target.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(live_payload)
    actions = (
        _state_action(
            ordinal=0,
            batch_ordinal=0,
            destination_path="Knowledge Base/Notes/target.md",
            before=b"before",
            after=b"after",
        ),
    )
    partition = consolidation_plan.derive_journal_batch_partition(actions)
    events: list[str] = []
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "writer-state")
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)

    class Journal:
        def batch_status(self, batch: consolidation_saga.ContentBatch) -> str:
            events.append("status")
            return journal_status

        def prepare_batch(self, batch: consolidation_saga.ContentBatch) -> object:
            events.append("prepare")
            return object()

        def commit_batch(self, batch: consolidation_saga.ContentBatch) -> object:
            events.append("commit")
            return object()

    real_batch_atomic_write = vault.batch_atomic_write

    def observed_batch_atomic_write(*args: object, **kwargs: object) -> list[Path]:
        events.append("publish")
        return real_batch_atomic_write(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        consolidation_saga.vault,
        "batch_atomic_write",
        observed_batch_atomic_write,
    )

    def materialize(_batch: consolidation_saga.ContentBatch):
        events.append("materialize")
        return (
            vault.PlannedWrite(
                path=target,
                content="after",
                expected_hash=hashlib.sha256(b"before").hexdigest(),
            ),
        )

    result = consolidation_saga.publish_policy_first(
        content_actions=actions,
        approved_partition_digest=partition.digest,
        expected_policy_fingerprint=POLICY_FINGERPRINT,
        vault_binding_digest=VAULT_BINDING,
        activate_policy=_publication_terminal,
        journal=Journal(),
        vault_root=root,
        materialize_batch=materialize,
    )

    assert tuple(events) == expected_events
    assert target.read_bytes() == b"after"
    assert result.committed_batch_ordinals == (0,)


@pytest.mark.parametrize(
    ("journal_status", "live_payload"),
    [("prior", b"after"), ("prepared", b"third"), ("final", b"before")],
)
def test_policy_first_retry_refuses_inconsistent_journal_and_live_bytes(
    tmp_path: Path,
    journal_status: str,
    live_payload: bytes,
) -> None:
    from exomem.governance import consolidation_plan, consolidation_saga

    root = tmp_path / "vault"
    target = root / "Knowledge Base/Notes/target.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(live_payload)
    actions = (
        _state_action(
            ordinal=0,
            batch_ordinal=0,
            destination_path="Knowledge Base/Notes/target.md",
            before=b"before",
            after=b"after",
        ),
    )
    partition = consolidation_plan.derive_journal_batch_partition(actions)
    effects: list[str] = []

    class Journal:
        def batch_status(self, batch: consolidation_saga.ContentBatch) -> str:
            return journal_status

        def prepare_batch(self, batch: consolidation_saga.ContentBatch) -> object:
            effects.append("prepare")
            return object()

        def commit_batch(self, batch: consolidation_saga.ContentBatch) -> object:
            effects.append("commit")
            return object()

    with pytest.raises(consolidation_saga.PolicyFirstPublicationUnavailable):
        consolidation_saga.publish_policy_first(
            content_actions=actions,
            approved_partition_digest=partition.digest,
            expected_policy_fingerprint=POLICY_FINGERPRINT,
            vault_binding_digest=VAULT_BINDING,
            activate_policy=_publication_terminal,
            journal=Journal(),
            vault_root=root,
            materialize_batch=lambda batch: effects.append("materialize") or (),
        )

    assert effects == []


def test_persisted_multi_batch_retry_skips_final_and_resumes_prepared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import vault, writer_lease
    from exomem.governance import (
        consolidation_batch_journal,
        consolidation_plan,
        consolidation_saga,
    )

    root = tmp_path / "vault"
    targets = (
        root / "Knowledge Base/Notes/first.md",
        root / "Knowledge Base/Notes/second.md",
    )
    for ordinal, target in enumerate(targets):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"before-{ordinal}".encode())
    actions = tuple(
        _state_action(
            ordinal=ordinal,
            batch_ordinal=ordinal,
            destination_path=target.relative_to(root).as_posix(),
            before=f"before-{ordinal}".encode(),
            after=f"after-{ordinal}".encode(),
        )
        for ordinal, target in enumerate(targets)
    )
    partition = consolidation_plan.derive_journal_batch_partition(actions)
    batches = consolidation_saga._content_batches(partition)
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "writer-state")
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)
    store = consolidation_batch_journal.ConsolidationBatchJournalStore(
        root,
        run_id="00000000-0000-4000-8000-000000000091",
    )
    store.create(
        operation_id="00000000-0000-4000-8000-000000000092",
        request_digest=hashlib.sha256(b"multi-batch-retry").hexdigest(),
        partition=partition,
    )
    store.prepare_batch(batches[0])
    targets[0].write_bytes(b"after-0")
    store.commit_batch(batches[0])
    store.prepare_batch(batches[1])
    materialized: list[int] = []

    def materialize(batch: consolidation_saga.ContentBatch):
        materialized.append(batch.ordinal)
        action = actions[batch.first_action_ordinal]
        return (
            vault.PlannedWrite(
                path=root / str(action["destination_path"]),
                content=f"after-{batch.ordinal}",
                expected_hash=str(action["expected_before_sha256"]),
            ),
        )

    result = consolidation_saga.publish_policy_first(
        content_actions=actions,
        approved_partition_digest=partition.digest,
        expected_policy_fingerprint=POLICY_FINGERPRINT,
        vault_binding_digest=VAULT_BINDING,
        activate_policy=_publication_terminal,
        journal=consolidation_batch_journal.ConsolidationBatchJournalStore(
            root,
            run_id="00000000-0000-4000-8000-000000000091",
        ),
        vault_root=root,
        materialize_batch=materialize,
    )

    assert materialized == [1]
    assert tuple(target.read_bytes() for target in targets) == (b"after-0", b"after-1")
    assert result.committed_batch_ordinals == (0, 1)
    assert tuple(entry.status for entry in store.load().batches) == ("final", "final")


def test_content_batch_classifier_refuses_an_unsafe_leaf(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_plan, consolidation_saga

    root = tmp_path / "vault"
    target = root / "Knowledge Base/Notes/target.md"
    outside = tmp_path / "outside.md"
    target.parent.mkdir(parents=True)
    outside.write_bytes(b"before")
    target.symlink_to(outside)
    actions = (
        _state_action(
            ordinal=0,
            batch_ordinal=0,
            destination_path="Knowledge Base/Notes/target.md",
            before=b"before",
            after=b"after",
        ),
    )
    partition = consolidation_plan.derive_journal_batch_partition(actions)

    with pytest.raises(consolidation_saga.PolicyFirstPublicationUnavailable):
        consolidation_saga.classify_content_batch_state(
            vault_root=root,
            content_actions=actions,
            batch=consolidation_saga._content_batches(partition)[0],
        )


@pytest.mark.parametrize("changed", ["path", "content", "cas"])
def test_policy_first_rejects_materialized_writes_outside_the_approved_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    from exomem import vault
    from exomem.governance import consolidation_plan, consolidation_saga

    root = tmp_path / "vault"
    target = root / "Knowledge Base/Notes/target.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before")
    action = _state_action(
        ordinal=0,
        batch_ordinal=0,
        destination_path="Knowledge Base/Notes/target.md",
        before=b"before",
        after=b"after",
    )
    partition = consolidation_plan.derive_journal_batch_partition((action,))
    effects: list[str] = []

    class Journal:
        def batch_status(self, batch: consolidation_saga.ContentBatch) -> str:
            return "prior"

        def prepare_batch(self, batch: consolidation_saga.ContentBatch) -> object:
            effects.append("prepare")
            return object()

        def commit_batch(self, batch: consolidation_saga.ContentBatch) -> object:
            effects.append("commit")
            return object()

    monkeypatch.setattr(
        consolidation_saga.vault,
        "batch_atomic_write",
        lambda *args, **kwargs: effects.append("publish"),
    )
    write = vault.PlannedWrite(
        path=(root / "Knowledge Base/Notes/extra.md") if changed == "path" else target,
        content="different" if changed == "content" else "after",
        expected_hash=(
            hashlib.sha256(b"different-before").hexdigest()
            if changed == "cas"
            else hashlib.sha256(b"before").hexdigest()
        ),
    )

    with pytest.raises(consolidation_saga.PolicyFirstPublicationUnavailable):
        consolidation_saga.publish_policy_first(
            content_actions=(action,),
            approved_partition_digest=partition.digest,
            expected_policy_fingerprint=POLICY_FINGERPRINT,
            vault_binding_digest=VAULT_BINDING,
            activate_policy=_publication_terminal,
            journal=Journal(),
            vault_root=root,
            materialize_batch=lambda batch: (write,),
        )

    assert effects == ["prepare"]
