"""Durable exact-state journal for consolidation content batches."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import writer_lease
from exomem.governance import consolidation_plan, consolidation_saga

RUN_ID = "00000000-0000-4000-8000-000000000081"
OPERATION_ID = "00000000-0000-4000-8000-000000000082"
REQUEST_DIGEST = hashlib.sha256(b"batch-request").hexdigest()


@pytest.fixture(autouse=True)
def _private_writer_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "writer-state")
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _action(ordinal: int, batch_ordinal: int) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "batch_ordinal": batch_ordinal,
        "action": "overwrite",
        "object_ref": f"source-object-{ordinal}",
        "source_path": f"Knowledge Base/Notes/source-{ordinal}.md",
        "destination_path": f"Knowledge Base/Notes/destination-{ordinal}.md",
        "expected_before_state": "present",
        "expected_before_sha256": _digest(f"before-{ordinal}"),
        "planned_after_state": "present",
        "planned_after_sha256": _digest(f"after-{ordinal}"),
    }


def _partition() -> consolidation_plan.CanonicalObject:
    return consolidation_plan.derive_journal_batch_partition(
        (_action(0, 0), _action(1, 0), _action(2, 1))
    )


def _batch(ordinal: int) -> consolidation_saga.ContentBatch:
    row = _partition().preimage["batches"][ordinal]
    return consolidation_saga.ContentBatch(
        ordinal=ordinal,
        first_action_ordinal=row["first_action_ordinal"],
        last_action_ordinal=row["last_action_ordinal"],
        action_count=row["action_count"],
        publication_boundary=row["publication_boundary"],
        action_set_digest=row["action_set_digest"],
        prior_fingerprint=row["prior_fingerprint"],
        prepared_fingerprint=row["prepared_fingerprint"],
        final_fingerprint=row["final_fingerprint"],
    )


def _store(vault: Path):
    from exomem.governance import consolidation_batch_journal

    return consolidation_batch_journal.ConsolidationBatchJournalStore(
        vault,
        run_id=RUN_ID,
    )


def test_create_persists_exact_prior_state_and_restarts(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = _store(vault)

    created = store.create(
        operation_id=OPERATION_ID,
        request_digest=REQUEST_DIGEST,
        partition=_partition(),
    )

    assert created.schema == "exomem.consolidation-content-batch-journal/v1"
    assert created.revision == 1
    assert created.partition_digest == _partition().digest
    assert created.binding_digest == (
        "3744d28927cdbe78a647a55dbf124b5377da557cc3e83961720105fcf4760870"
    )
    assert created.state_digest == (
        "bd8e47e2ffdd3f3363cc590ec8358caa76f3f83461e2bd58c7832439762bf0d1"
    )
    assert created.publication_boundary_ordinal == 0
    assert created.publication_boundary_committed is False
    assert tuple(item.status for item in created.batches) == ("prior", "prior")
    assert store.batch_status(_batch(0)) == "prior"
    assert _store(vault).load() == created


def test_prepare_and_commit_are_cas_revisioned_and_exactly_replayable(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    store = _store(vault)
    initial = store.create(
        operation_id=OPERATION_ID,
        request_digest=REQUEST_DIGEST,
        partition=_partition(),
    )

    prepared = store.prepare_batch(_batch(0))
    assert prepared.revision == initial.revision + 1
    assert tuple(item.status for item in prepared.batches) == ("prepared", "prior")
    assert store.prepare_batch(_batch(0)) == prepared
    assert _store(vault).batch_status(_batch(0)) == "prepared"

    final = store.commit_batch(_batch(0))
    assert final.revision == prepared.revision + 1
    assert tuple(item.status for item in final.batches) == ("final", "prior")
    assert final.publication_boundary_committed is True
    assert store.batch_status(_batch(0)) == "final"
    assert _store(vault).commit_batch(_batch(0)) == final

    second_prepared = _store(vault).prepare_batch(_batch(1))
    assert tuple(item.status for item in second_prepared.batches) == (
        "final",
        "prepared",
    )


def test_out_of_order_or_changed_batch_never_advances_the_journal(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_batch_journal

    vault = tmp_path / "vault"
    store = _store(vault)
    initial = store.create(
        operation_id=OPERATION_ID,
        request_digest=REQUEST_DIGEST,
        partition=_partition(),
    )

    for operation in (
        lambda: store.prepare_batch(_batch(1)),
        lambda: store.commit_batch(_batch(0)),
        lambda: store.prepare_batch(
            replace(_batch(0), final_fingerprint=_digest("changed-final"))
        ),
    ):
        with pytest.raises(
            consolidation_batch_journal.ConsolidationBatchJournalUnavailable
        ):
            operation()
        assert store.load() == initial


def test_create_replay_rejects_changed_operation_request_or_partition(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_batch_journal

    vault = tmp_path / "vault"
    store = _store(vault)
    created = store.create(
        operation_id=OPERATION_ID,
        request_digest=REQUEST_DIGEST,
        partition=_partition(),
    )
    assert store.create(
        operation_id=OPERATION_ID,
        request_digest=REQUEST_DIGEST,
        partition=_partition(),
    ) == created

    changed = (
        {"operation_id": "00000000-0000-4000-8000-000000000083"},
        {"request_digest": _digest("changed-request")},
        {
            "partition": consolidation_plan.derive_journal_batch_partition(
                (_action(0, 0),)
            )
        },
    )
    for override in changed:
        kwargs = {
            "operation_id": OPERATION_ID,
            "request_digest": REQUEST_DIGEST,
            "partition": _partition(),
            **override,
        }
        with pytest.raises(
            consolidation_batch_journal.ConsolidationBatchJournalUnavailable
        ):
            store.create(**kwargs)
        assert store.load() == created


def test_corrupt_or_revision_inconsistent_journal_is_never_recreated(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_batch_journal

    vault = tmp_path / "vault"
    store = _store(vault)
    created = store.create(
        operation_id=OPERATION_ID,
        request_digest=REQUEST_DIGEST,
        partition=_partition(),
    )
    path = (
        vault
        / "Knowledge Base"
        / "_Consolidation"
        / "runs"
        / RUN_ID
        / "content-batches.json"
    )
    value = json.loads(path.read_bytes())
    value["revision"] = created.revision + 1
    changed = consolidation_plan.canonical_closed_jcs(value)
    path.write_bytes(changed)

    with pytest.raises(
        consolidation_batch_journal.ConsolidationBatchJournalUnavailable
    ):
        store.create(
            operation_id=OPERATION_ID,
            request_digest=REQUEST_DIGEST,
            partition=_partition(),
        )

    assert path.read_bytes() == changed
