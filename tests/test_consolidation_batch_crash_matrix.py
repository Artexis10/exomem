"""Deterministic crash/restart matrix for consolidation content batches."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from exomem import vault, writer_lease
from exomem.governance import (
    consolidation_batch_journal,
    consolidation_plan,
    consolidation_saga,
)

RUN_ID = "00000000-0000-4000-8000-0000000000a1"
OPERATION_ID = "00000000-0000-4000-8000-0000000000a2"
REQUEST_DIGEST = hashlib.sha256(b"batch-crash-matrix").hexdigest()
POLICY_FINGERPRINT = hashlib.sha256(b"batch-crash-policy").hexdigest()
VAULT_BINDING = hashlib.sha256(b"batch-crash-vault-binding").hexdigest()


class SimulatedProcessCrash(BaseException):
    """Bypass ordinary Exception rollback exactly like abrupt process loss."""


@pytest.fixture(autouse=True)
def _private_writer_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "writer-state")
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)

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


def _action(ordinal: int, *, batch_ordinal: int = 0) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "batch_ordinal": batch_ordinal,
        "action": "overwrite",
        "object_ref": f"source-object-{ordinal}",
        "source_path": f"Knowledge Base/Notes/source-{ordinal}.md",
        "destination_path": f"Knowledge Base/Notes/target-{ordinal}.md",
        "expected_before_state": "present",
        "expected_before_sha256": hashlib.sha256(
            f"before-{ordinal}".encode()
        ).hexdigest(),
        "planned_after_state": "present",
        "planned_after_sha256": hashlib.sha256(
            f"after-{ordinal}".encode()
        ).hexdigest(),
    }


def _terminal() -> consolidation_saga.PolicyActivationTerminal:
    intent_id = hashlib.sha256(b"batch-crash-policy-intent").hexdigest()
    return consolidation_saga.PolicyActivationTerminal(
        schema=consolidation_saga.POLICY_ACTIVATION_TERMINAL_SCHEMA,
        policy_fingerprint=POLICY_FINGERPRINT,
        intent_event_id=intent_id,
        prepared_fingerprint=hashlib.sha256(b"policy-prepared").hexdigest(),
        active_fingerprint=hashlib.sha256(b"policy-active").hexdigest(),
        terminal_event_id=f"{intent_id}:committed",
    )


def _store(root: Path) -> consolidation_batch_journal.ConsolidationBatchJournalStore:
    return consolidation_batch_journal.ConsolidationBatchJournalStore(
        root,
        run_id=RUN_ID,
    )


def _initialize(
    root: Path,
    actions: tuple[dict[str, object], ...],
) -> tuple[
    consolidation_plan.CanonicalObject,
    consolidation_batch_journal.ConsolidationBatchJournalStore,
]:
    for action in actions:
        target = root / str(action["destination_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"before-{action['ordinal']}".encode())
    partition = consolidation_plan.derive_journal_batch_partition(actions)
    store = _store(root)
    store.create(
        operation_id=OPERATION_ID,
        request_digest=REQUEST_DIGEST,
        partition=partition,
    )
    return partition, store


def _materializer(
    root: Path,
    actions: tuple[dict[str, object], ...],
    calls: list[int],
):
    def materialize(batch: consolidation_saga.ContentBatch):
        calls.append(batch.ordinal)
        writes: list[vault.PlannedWrite] = []
        for action in actions:
            if action["batch_ordinal"] != batch.ordinal:
                continue
            writes.append(
                vault.PlannedWrite(
                    path=root / str(action["destination_path"]),
                    content=f"after-{action['ordinal']}",
                    expected_hash=str(action["expected_before_sha256"]),
                )
            )
        return tuple(writes)

    return materialize


class FaultingJournal:
    def __init__(
        self,
        inner: consolidation_batch_journal.ConsolidationBatchJournalStore,
        seam: str,
    ) -> None:
        self.inner = inner
        self.seam = seam

    def batch_status(self, batch: consolidation_saga.ContentBatch) -> str:
        return self.inner.batch_status(batch)

    def prepare_batch(self, batch: consolidation_saga.ContentBatch) -> object:
        if self.seam == "before-prepare":
            raise SimulatedProcessCrash
        result = self.inner.prepare_batch(batch)
        if self.seam == "after-prepare":
            raise SimulatedProcessCrash
        return result

    def commit_batch(self, batch: consolidation_saga.ContentBatch) -> object:
        if self.seam == "before-final":
            raise SimulatedProcessCrash
        result = self.inner.commit_batch(batch)
        if self.seam == "after-final-ack-lost":
            raise SimulatedProcessCrash
        return result


@pytest.mark.parametrize(
    ("seam", "expected_status", "expected_payload", "restart_materializations"),
    [
        ("before-prepare", "prior", b"before-0", [0]),
        ("after-prepare", "prepared", b"before-0", [0]),
        ("before-publish", "prepared", b"before-0", [0]),
        ("after-publish", "prepared", b"after-0", []),
        ("before-final", "prepared", b"after-0", []),
        ("after-final-ack-lost", "final", b"after-0", []),
    ],
)
def test_restart_repairs_only_the_missing_effect_or_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
    expected_status: str,
    expected_payload: bytes,
    restart_materializations: list[int],
) -> None:
    root = tmp_path / "vault"
    actions = (_action(0),)
    partition, store = _initialize(root, actions)
    real_publish = vault.batch_atomic_write

    def publish(*args: object, **kwargs: object):
        if seam == "before-publish":
            raise SimulatedProcessCrash
        result = real_publish(*args, **kwargs)  # type: ignore[arg-type]
        if seam == "after-publish":
            raise SimulatedProcessCrash
        return result

    monkeypatch.setattr(consolidation_saga.vault, "batch_atomic_write", publish)
    first_calls: list[int] = []
    with pytest.raises(SimulatedProcessCrash):
        consolidation_saga.publish_policy_first(
            content_actions=actions,
            approved_partition_digest=partition.digest,
            expected_policy_fingerprint=POLICY_FINGERPRINT,
            vault_binding_digest=VAULT_BINDING,
            activate_policy=_terminal,
            journal=FaultingJournal(store, seam),
            vault_root=root,
            materialize_batch=_materializer(root, actions, first_calls),
        )

    target = root / str(actions[0]["destination_path"])
    assert store.batch_status(consolidation_saga._content_batches(partition)[0]) == (
        expected_status
    )
    assert target.read_bytes() == expected_payload

    monkeypatch.setattr(consolidation_saga.vault, "batch_atomic_write", real_publish)
    restart_calls: list[int] = []
    result = consolidation_saga.publish_policy_first(
        content_actions=actions,
        approved_partition_digest=partition.digest,
        expected_policy_fingerprint=POLICY_FINGERPRINT,
        vault_binding_digest=VAULT_BINDING,
        activate_policy=_terminal,
        journal=_store(root),
        vault_root=root,
        materialize_batch=_materializer(root, actions, restart_calls),
    )

    assert restart_calls == restart_materializations
    assert target.read_bytes() == b"after-0"
    assert result.committed_batch_ordinals == (0,)
    assert _store(root).batch_status(consolidation_saga._content_batches(partition)[0]) == (
        "final"
    )


def test_crash_after_one_of_two_destination_renames_stays_mixed_and_cannot_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    actions = (_action(0), _action(1))
    partition, store = _initialize(root, actions)
    first_target = root / str(actions[0]["destination_path"])
    real_hook = vault._after_batch_destination_published

    def crash_after_first(path: Path) -> None:
        real_hook(path)
        if path == first_target:
            raise SimulatedProcessCrash

    monkeypatch.setattr(vault, "_after_batch_destination_published", crash_after_first)
    with pytest.raises(SimulatedProcessCrash):
        consolidation_saga.publish_policy_first(
            content_actions=actions,
            approved_partition_digest=partition.digest,
            expected_policy_fingerprint=POLICY_FINGERPRINT,
            vault_binding_digest=VAULT_BINDING,
            activate_policy=_terminal,
            journal=store,
            vault_root=root,
            materialize_batch=_materializer(root, actions, []),
        )

    batch = consolidation_saga._content_batches(partition)[0]
    assert store.batch_status(batch) == "prepared"
    assert consolidation_saga.classify_content_batch_state(
        vault_root=root,
        content_actions=actions,
        batch=batch,
    ).state == "mixed"
    monkeypatch.setattr(vault, "_after_batch_destination_published", real_hook)
    effects: list[int] = []
    with pytest.raises(consolidation_saga.PolicyFirstPublicationUnavailable):
        consolidation_saga.publish_policy_first(
            content_actions=actions,
            approved_partition_digest=partition.digest,
            expected_policy_fingerprint=POLICY_FINGERPRINT,
            vault_binding_digest=VAULT_BINDING,
            activate_policy=_terminal,
            journal=_store(root),
            vault_root=root,
            materialize_batch=_materializer(root, actions, effects),
        )
    assert effects == []
    assert _store(root).batch_status(batch) == "prepared"


def test_crash_after_last_destination_rename_repairs_only_the_final_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    actions = (_action(0), _action(1))
    partition, store = _initialize(root, actions)
    last_target = root / str(actions[-1]["destination_path"])
    real_hook = vault._after_batch_destination_published

    def crash_after_last(path: Path) -> None:
        real_hook(path)
        if path == last_target:
            raise SimulatedProcessCrash

    monkeypatch.setattr(vault, "_after_batch_destination_published", crash_after_last)
    with pytest.raises(SimulatedProcessCrash):
        consolidation_saga.publish_policy_first(
            content_actions=actions,
            approved_partition_digest=partition.digest,
            expected_policy_fingerprint=POLICY_FINGERPRINT,
            vault_binding_digest=VAULT_BINDING,
            activate_policy=_terminal,
            journal=store,
            vault_root=root,
            materialize_batch=_materializer(root, actions, []),
        )

    batch = consolidation_saga._content_batches(partition)[0]
    assert store.batch_status(batch) == "prepared"
    assert consolidation_saga.classify_content_batch_state(
        vault_root=root,
        content_actions=actions,
        batch=batch,
    ).state == "final"
    monkeypatch.setattr(vault, "_after_batch_destination_published", real_hook)
    effects: list[int] = []
    consolidation_saga.publish_policy_first(
        content_actions=actions,
        approved_partition_digest=partition.digest,
        expected_policy_fingerprint=POLICY_FINGERPRINT,
        vault_binding_digest=VAULT_BINDING,
        activate_policy=_terminal,
        journal=_store(root),
        vault_root=root,
        materialize_batch=_materializer(root, actions, effects),
    )
    assert effects == []
    assert _store(root).batch_status(batch) == "final"
