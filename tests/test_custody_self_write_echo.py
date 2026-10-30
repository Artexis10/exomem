"""A write under derived batch custody (fast durable ack) is not an outside edit.

The live-index-freshness contract suppresses the watcher's echo of a write
Exomem made itself. Fast ack moves the index publication behind the reply, and
the echo suppression has to stay with the write: otherwise every filesystem
event of a Planning add or a Records append reaches the watcher as somebody
else's edit, marks the vault external-pending and repeats the drain's fan-out
on the watcher thread. The suppressed echo also stops fencing readers, so the
write has to publish its registry delta before the reply.

These tests drive a real watchdog observer over a real vault and the real
lease and receipt store.
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
    file_watcher,
    freshness,
    graph_sync,
    pending_recall,
    planning,
    records,
    writer_lease,
)
from exomem import find as find_module
from exomem import move_file as move_file_module
from exomem import vault as vault_module

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
    # Neither test reads the lexical catalogue. A live one starts a background
    # repair per seeding write, and that repair races the watcher's own
    # publication into a lag no test here can wait out.
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    pending_recall.reset()
    find_module.clear_cache()
    file_watcher.clear_self_write_registry()
    fixtures.seed_vault(vault)

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
    try:
        assert watcher.wait_until_seeded(_WAIT_SECONDS) is True
        # A graph rebuild that the seeding started reconciles the freshness registry
        # from its own walk and holds the inbound index while it reads. Begin the
        # test only once it has landed, so no write below meets that walk.
        assert graph_sync.drain_active_rebuilds(_WAIT_SECONDS), (
            "the seeding's graph rebuild did not settle"
        )
        yield vault, observed
    finally:
        watcher.stop()
        # stop() waits two seconds for a thread. The control edit's dispatch can run
        # longer on a busy host, and it resolves the state root when it finishes. After
        # a worker's last test that is the real platform root, which trips the guard of
        # the other workers' tests.
        for thread in (watcher._thread, watcher._reconcile_thread):
            if thread is not None:
                thread.join(_WAIT_SECONDS)
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


def test_a_page_written_under_fast_ack_is_linked_before_the_drain_runs(
    watched, tmp_path: Path
) -> None:
    """A move right after the reply must rewrite the links the write just made."""
    vault, observed = watched
    target = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
    renamed = "Knowledge Base/Notes/Insights/renamed-disclosure.md"
    referrer = vault / "Knowledge Base" / "Notes" / "Insights" / "custody-referrer.md"
    # A live inbound-link index, as a running server has.
    assert vault_module.find_inbound_wikilinks(vault, target) == []

    def write_referrer(root: Path) -> None:
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(
                    referrer,
                    "---\ntype: insight\nstatus: draft\ncreated: 2026-10-01\n"
                    "updated: 2026-10-01\ntags: []\n---\n# Referrer\n\n"
                    f"See [[{target.removesuffix('.md')}]].\n",
                    create_only=True,
                )
            ],
            vault_root=root,
        )

    _governed(tmp_path, vault, "remember", write_referrer)

    # No drain has run. The index is current only if it holds the new link.
    inbound = vault_module.cached_inbound_wikilinks(vault, target)
    assert inbound is not None
    assert [link.path for link in inbound] == ["Knowledge Base/Notes/Insights/custody-referrer.md"]

    moved: list = []
    _governed(
        tmp_path,
        vault,
        "move_memory",
        lambda root: moved.append(
            move_file_module.move_file(root, old_path=target, new_path=renamed)
        ),
    )

    assert moved[0].wikilinks_updated == 1
    text = referrer.read_text(encoding="utf-8")
    assert "renamed-disclosure" in text
    assert "progressive-disclosure-without-mode-fragmentation" not in text
