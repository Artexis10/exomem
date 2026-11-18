"""The graph state that a store-mode vault reports (OpenSpec make-vault-store-canonical, task 3.2).

Migration leaves file mode's graph epoch where it was, and nothing in store mode advances it. Each
case plants file mode's graph debt on a migrated vault, a checkpoint that its sidecar never
acknowledged and the repair that it queued, and then reads what a reader reports or gates on.
"""

from __future__ import annotations

import threading

import test_vault_store_read_write as read_write
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, vault  # noqa: F401

from exomem import (
    deferred_index,
    doctor,
    graph_sync,
    readiness,
    reconcile,
    server_runtime,
    vocabulary_delivery,
)
from exomem.vault_store import read

stored = read_write.stored


def leave_file_modes_graph_debt(root):
    """The epoch a pre-migration write left: an unacknowledged checkpoint and its queued repair.

    It issues the generation above the floor and the acknowledgement that the file-mode phase left.
    """
    _state, acknowledged = graph_sync.acknowledgement_state(root)
    floor = graph_sync.read_floor(root)
    generation = 1 + max(floor.generation if floor else 0, acknowledged.generation if acknowledged else 0)
    graph_sync._write_floor(root, graph_sync.GraphSyncGenerationFloor.create(generation))
    graph_sync._write_checkpoint(
        root,
        graph_sync.GraphSyncCheckpoint.create(
            generation=generation,
            mutation_id="a1b2c3d4e5f6a7b8c9d0e1f2",
            paths=((ALPHA, "0" * 64),),
            created_paths=(ALPHA,),
        ),
    )
    deferred_index.add_graph(root, [ALPHA], generation=generation)
    assert graph_sync.status(root)["state"] == "recovery_required"


def test_doctor_reports_the_stores_graph_revision_not_file_modes_generation(stored):
    """Defect: a store-mode vault reads `recovery_required at generation 1` from the frozen epoch."""
    leave_file_modes_graph_debt(stored)

    check = doctor._check_graph_sync_state(stored)

    assert check.status == "pass"
    assert f"store revision {read.current_revision(stored)}" in check.message
    assert "generation" not in check.message


def test_doctor_names_no_graph_recovery_for_a_store_mode_vault(stored):
    """Defect: a store-mode vault warns that graph recovery is pending, and then fails after the bound."""
    leave_file_modes_graph_debt(stored)

    check = doctor.check_graph_recovery_age(stored)

    assert check.status == "pass"
    assert "store mode" in check.message


def test_the_readiness_payload_reports_no_graph_recovery_clock_in_store_mode(stored):
    """Defect: serving readiness starts a recovery clock for a graph that has nothing to recover."""
    leave_file_modes_graph_debt(stored)

    payload = readiness.graph_recovery_payload(stored)

    assert payload == {"recovery_required": False, "recovery_age_s": None}
    assert graph_sync.recovery_clock_state(stored) == "absent"


def test_the_vocabulary_drain_runs_in_store_mode_over_file_modes_graph_debt(stored, monkeypatch):
    """Defect: the drain waits for file mode's graph to become current, which never happens in store
    mode, and gives up without recovering any queued vocabulary job."""
    leave_file_modes_graph_debt(stored)
    passes = iter([{"processed": 1}, {"processed": 0}])
    monkeypatch.setattr(vocabulary_delivery, "recover", lambda root: next(passes))

    drained = server_runtime.drain_vocabulary_recovery(stored, threading.Event(), wait_seconds=0.3)

    assert drained == 1


def test_a_store_mode_reconcile_plan_names_no_file_mode_graph_lineage(stored):
    """Defect: a preview with `rebuild_graph` names file mode's unreadable graph lineage for quarantine,
    and the run that follows skips the graph phase and never moves it."""
    before = reconcile.reconcile(stored, dry_run=True, rebuild_graph=True).plan_id
    floor = graph_sync.floor_path(stored)
    floor.parent.mkdir(parents=True, exist_ok=True)
    floor.write_text("not a floor")
    assert graph_sync.classify_epoch(stored).kind == "unavailable"

    assert reconcile.reconcile(stored, dry_run=True, rebuild_graph=True).plan_id == before
