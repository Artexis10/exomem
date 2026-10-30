"""A write under derived batch custody (fast durable ack) is not an outside edit.

The live-index-freshness contract suppresses the watcher's echo of a write
Exomem made itself. Fast ack moves the index and registry publication behind
the reply, and the echo suppression has to stay with the write: otherwise every
filesystem event of a Planning add or a Records append reaches the watcher as
somebody else's edit, marks the vault external-pending and repeats the drain's
fan-out on the watcher thread.

These tests drive a real watchdog observer over a real vault and the real
lease, receipt store and drain.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import lifecycle_fixtures as fixtures
import pytest

from exomem import (
    derived_drain,
    derived_receipts,
    file_watcher,
    freshness,
    lexstore,
    pending_recall,
    planning,
    records,
    writer_lease,
)
from exomem import find as find_module
from exomem import vault as vault_module

pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="SQLite build lacks FTS5"
)

_WAIT_SECONDS = 20.0


class _Observed:
    """What the watcher did, read from the three places a foreign edit surfaces."""

    def __init__(self) -> None:
        self.events: list[Path] = []
        self.marks: list[tuple[Path, ...] | None] = []
        self.batches: list[list[Path]] = []
        self.arrived = threading.Condition()

    def wait_for(self, predicate, what: str) -> None:  # noqa: ANN001
        deadline = time.monotonic() + _WAIT_SECONDS
        with self.arrived:
            while not predicate():
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"timed out waiting for {what}"
                self.arrived.wait(min(remaining, 0.25))

    def notify(self) -> None:
        with self.arrived:
            self.arrived.notify_all()


@pytest.fixture
def watched(vault: Path, monkeypatch: pytest.MonkeyPatch):
    """A seeded Planning and Records vault under a started watcher, fast ack on."""
    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    pending_recall.reset()
    find_module.clear_cache()
    file_watcher.clear_self_write_registry()
    fixtures.seed_vault(vault)
    assert lexstore.get_store(vault).rebuild_atomic() is True

    observed = _Observed()
    watcher = file_watcher.FileWatcher(vault, debounce_seconds=0.05)
    real_record = watcher._record
    real_dispatch = watcher._dispatch_batch
    real_mark = freshness.mark_external_pending

    def record(path: Path, **kwargs) -> None:  # noqa: ANN003
        real_record(path, **kwargs)
        observed.events.append(Path(path))
        observed.notify()

    def dispatch(ups, up_rels, del_rels, **kwargs):  # noqa: ANN001, ANN003
        observed.batches.append(list(ups))
        observed.notify()
        return real_dispatch(ups, up_rels, del_rels, **kwargs)

    def mark(vault_root, *args, paths=None, **kwargs):  # noqa: ANN001, ANN002, ANN003
        observed.marks.append(None if paths is None else tuple(Path(item) for item in paths))
        return real_mark(vault_root, *args, paths=paths, **kwargs)

    monkeypatch.setattr(watcher, "_record", record)
    monkeypatch.setattr(watcher, "_dispatch_batch", dispatch)
    monkeypatch.setattr(freshness, "mark_external_pending", mark)
    assert watcher.start() is True
    assert watcher.wait_until_seeded(_WAIT_SECONDS) is True
    try:
        yield vault, observed
    finally:
        watcher.stop()
        file_watcher.clear_self_write_registry()


def _governed(tmp_path: Path, root: Path, command_name: str, work) -> dict:  # noqa: ANN001
    """Run one mutation through the real lease, as an MCP call routes it."""

    def leaf(vault_root: Path, **_surface_kwargs):
        work(vault_root)
        return {"warnings": []}

    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "lease-state")
    )
    terminal = manager.invoke(
        SimpleNamespace(name=command_name, leaf=leaf, read_only=False),
        (root,),
        {"response_detail": "compact"},
        idempotency_key=None,
        mutation_request_id=str(uuid.uuid4()),
    )
    assert terminal["status"] == "committed", terminal
    assert terminal["derived_sync"] == "pending", "the write must run under derived batch custody"
    return terminal


def _drain(root: Path) -> None:
    now = time.time()
    for _attempt in range(24):
        derived_drain.drain_once(
            root,
            dispatch=derived_drain.component_dispatcher(),
            observe_current_generation=derived_drain.canonical_generation_observer(),
            visibility_publisher=pending_recall.publish,
            retire_visibility=derived_drain.pending_visibility_retirer(),
            limit=32,
            now=now,
        )
        now += derived_drain.MAX_RETRY_SECONDS + 1.0
        if not derived_receipts.due_component_count(
            root, now=now
        ) and not derived_receipts.recoverable_batch_count(root):
            return


def _let_events_arrive(root: Path, observed: _Observed) -> Path:
    """Wait until the watcher has handled every event the writes caused.

    Events reach the watcher in order, and it flushes what it holds as one
    batch, so the control file's batch closes after theirs. The control file is
    the only edit in the run that is somebody else's.
    """
    control = root / "Knowledge Base" / "Notes" / "control-outside-edit.md"
    control.write_text("# Control\n", encoding="utf-8")
    observed.wait_for(
        lambda: any(control in batch for batch in observed.batches), "the control edit's batch"
    )
    return control


def test_planning_add_and_records_append_under_fast_ack_are_not_outside_edits(
    watched, tmp_path: Path
) -> None:
    vault, observed = watched
    initiative = fixtures.initiative_ref(vault)
    # A live inbound-link index, as a running server has: the write must patch it.
    link_target = "Knowledge Base/log.md"
    vault_module.find_inbound_wikilinks(vault, link_target)
    assert vault_module.cached_inbound_wikilinks(vault, link_target) is not None

    _governed(
        tmp_path,
        vault,
        "plan_memory",
        lambda root: planning.add(
            root,
            fixtures.PLANNING_PATH,
            item={
                "title": "Ship the quarterly report",
                "status": "planned",
                "commitment": "committed",
                "horizon": "week",
                "parent": initiative,
            },
            why="queue the report",
        ),
    )
    _governed(
        tmp_path,
        vault,
        "record_memory",
        lambda root: records.append_record(
            root,
            fixtures.RECORDS_PATH,
            item={"occurred_on": "2026-10-01", "title": "Report shipped", "event_type": "delivered"},
            why="log the delivery",
        ),
    )
    control = _let_events_arrive(vault, observed)

    # Only the control edit is external: no mark, and no watcher batch, names a
    # path the writes touched. A save can arrive as a create and a modify.
    assert set(observed.marks) == {(control,)}
    assert {path for batch in observed.batches for path in batch} == {control}

    _drain(vault)
    # The drain published both writes, which the watcher no longer does: the
    # recall registries and the lexical catalogue name one live state, and the
    # inbound-link index took the same delta.
    assert lexstore.runtime_retrieval_catalog_proof(vault, schedule_repair=False) is not None
    assert vault_module.cached_inbound_wikilinks(vault, link_target) is not None


def test_an_outside_edit_to_a_self_written_file_is_still_dispatched(
    watched, tmp_path: Path
) -> None:
    vault, observed = watched
    _governed(
        tmp_path,
        vault,
        "plan_memory",
        lambda root: planning.add(
            root,
            fixtures.PLANNING_PATH,
            item={
                "title": "Draft the roadmap",
                "status": "planned",
                "commitment": "committed",
                "horizon": "week",
                "parent": fixtures.initiative_ref(root),
            },
            why="queue the roadmap",
        ),
    )
    _let_events_arrive(vault, observed)
    observed.batches.clear()
    observed.marks.clear()
    item = next(
        path
        for path in (vault / "Knowledge Base" / "Planning" / "Delivery").rglob("*.md")
        if "Draft the roadmap" in path.read_text(encoding="utf-8")
    )

    item.write_text(item.read_text(encoding="utf-8") + "\nAn outside editor wrote this.\n")

    observed.wait_for(lambda: any(item in batch for batch in observed.batches), "the edit's batch")
    assert (item,) in observed.marks
