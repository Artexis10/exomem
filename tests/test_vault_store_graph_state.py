"""The graph state that a store-mode vault reports (OpenSpec make-vault-store-canonical, task 3.2).

Migration leaves file mode's graph epoch where it was, and nothing in store mode advances it. Each
case plants file mode's graph debt on a migrated vault, a checkpoint that its sidecar never
acknowledged and the repair that it queued, and then reads what a reader reports or gates on.
"""

from __future__ import annotations

import threading

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, vault  # noqa: F401
from test_vault_store_read_write import stored  # noqa: F401
from test_vault_store_served import call, service  # noqa: F401

from exomem import (
    deferred_index,
    doctor,
    epistemic_graph,
    graph_sync,
    readiness,
    reconcile,
    server_runtime,
    vocabulary_delivery,
)
from exomem.vault_store import read

CREATED = "Knowledge Base/Notes/Graph State.md"


def leave_file_modes_graph_debt(root):
    """The epoch a pre-migration write left: an unacknowledged checkpoint and its queued repair."""
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="a1b2c3d4e5f6a7b8c9d0e1f2",
        paths=((ALPHA, "0" * 64),),
        created_paths=(ALPHA,),
    )
    graph_sync._write_checkpoint(root, checkpoint)
    deferred_index.add_graph(root, [ALPHA], generation=1)
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


def test_a_store_mode_write_reports_no_graph_outcome_from_file_modes_checkpoint(stored, service):
    """Defect: a store commit's response claims `graph_sync: queued` for a checkpoint that file mode left."""
    leave_file_modes_graph_debt(stored)

    created = call(stored, "manage_memory_file", operation="create", path=CREATED, content="Graph state.\n")

    assert "graph_sync" not in created


def test_a_store_mode_reconcile_rebuilds_no_file_mode_graph(stored, service, monkeypatch):
    """Defect: the reconcile terminal reads file mode's epoch as `not current` and rebuilds the sidecar
    that no store-mode reader opens."""
    leave_file_modes_graph_debt(stored)
    rebuilds = []
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex, "rebuild_all", lambda self: rebuilds.append(self.vault_root) or {}
    )

    call(stored, "maintain_memory", mode="reconcile", dry_run=False)

    assert rebuilds == []


def test_a_store_mode_reconcile_plan_names_no_file_mode_graph_lineage(stored):
    """Defect: a preview with `rebuild_graph` names file mode's unreadable graph lineage for quarantine,
    and the run that follows skips the graph phase and never moves it."""
    before = reconcile.reconcile(stored, dry_run=True, rebuild_graph=True).plan_id
    floor = graph_sync.floor_path(stored)
    floor.parent.mkdir(parents=True, exist_ok=True)
    floor.write_text("not a floor")
    assert graph_sync.classify_epoch(stored).kind == "unavailable"

    assert reconcile.reconcile(stored, dry_run=True, rebuild_graph=True).plan_id == before
