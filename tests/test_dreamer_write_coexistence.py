"""D1-T13: the dreamer beside a write burst and the real graph drain.

A generated vault, the real graph-drain daemon, three writer threads writing
every second, and a reader measuring latency, run twice: once with the dreamer
off (the control) and once with it on and mid-reseed when the burst starts.
The idle and settle gates are shortened to two seconds through the policy seam
(never the environment) so the dreamer is actually eligible around the burst.

It must start no tick while writes land, tick again once the vault settles,
add no graph-drain work the control did not do, and leave read latency where
the control left it.

Scale. On this write path a concurrent write can fall back to a whole-vault
graph pass whose cost grows with the vault, and the test waits for two bursts
to settle, so it runs at 200 pages (30 to 45 s here) under its own timeout.
The paired and interleaved runs of the U5 write-burst probes carry the
at-scale evidence.
"""

from __future__ import annotations

import collections
import random
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    commands,
    deferred_index,
    dreamer,
    dreamer_policy,
    dreamer_store,
    epistemic_graph,
    foreground_activity,
    freshness,
    graph_drain,
)
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.kbdir import kb_dirname

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from synth_vault import gen_dense_vault  # noqa: E402

PAGES = 200
WRITERS = 3
WRITE_GAP = 1.0
BURST_SECONDS = 6.0
GATE_SECONDS = 2.0

#: A request that lands mid-tick waits for at most the page in flight; the read
#: tail may move by that much plus ten percent of the control.
PAGE_ALLOWANCE_SECONDS = 0.025


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    monkeypatch.delenv("EXOMEM_DISABLE_GRAPH_DRAIN", raising=False)
    # The test seam: short gates so the dreamer is eligible around the burst.
    monkeypatch.setattr(dreamer_policy, "IDLE_SECONDS", GATE_SECONDS)
    monkeypatch.setattr(dreamer_policy, "SETTLE_FLOOR_SECONDS", GATE_SECONDS)
    monkeypatch.setattr(dreamer_policy, "settle_seconds", lambda _last: GATE_SECONDS)
    monkeypatch.setattr(dreamer_policy, "POLL_SECONDS", 0.5)
    monkeypatch.setattr(dreamer_policy, "MIN_SLEEP_SECONDS", 0.2)
    yield
    dreamer.reset_for_tests()
    graph_drain.stop(timeout=5.0)
    freshness.clear()
    dreamer_store.clear_reader_memo()


def _vault(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = tmp_path / "vault"
    root.mkdir()
    gen_dense_vault(root, PAGES, links_per_note=3)
    freshness.seed(
        root,
        "vault",
        [(str(p), freshness.stat_signature(p)) for p in vault_module.walk_vault_md(root)],
    )
    freshness.seed(
        root,
        "kb",
        [(str(p), freshness.stat_signature(p)) for p in find_module._walk_md(root / kb_dirname())],
    )
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    return root, sorted((root / kb_dirname()).rglob("*.md"))


class _Burst:
    """Writers and a reader for one window, with counts and latencies."""

    def __init__(self, root: Path, pages: list[Path], seed: int) -> None:
        self.root = root
        self.pages = pages
        self.seed = seed
        self.reads: list[float] = []
        self.writes = 0
        self.errors: collections.Counter[str] = collections.Counter()
        self.first_write_at: float | None = None
        self.last_write_at: float | None = None
        self.lock = threading.Lock()

    def _write(self, index: int, stop: threading.Event) -> None:
        from exomem.writer_lease import get_manager, mark_active_mutation_committed

        rng = random.Random(self.seed * 10 + index)
        revision = 0
        while not stop.is_set():
            page = self.pages[rng.randrange(len(self.pages))]
            revision += 1
            body = page.read_text(encoding="utf-8") + f"\nRevision {index}-{revision}.\n"

            def leaf(root, _page=page, _body=body, **_kw):  # noqa: ANN001, ANN003
                vault_module.batch_atomic_write(
                    [vault_module.PlannedWrite(_page, _body)],
                    vault_root=root,
                    post_commit_fanout=True,
                )
                mark_active_mutation_committed()
                return {"status": "committed", "mutated": True}

            command = SimpleNamespace(name="remember", read_only=False, leaf=leaf)
            try:
                with foreground_activity.foreground_scope(self.root):
                    get_manager().invoke(command, (self.root,), {})
            except Exception as error:  # noqa: BLE001 - counted, never raised
                with self.lock:
                    self.errors[type(error).__name__] += 1
            now = time.monotonic()
            with self.lock:
                self.writes += 1
                self.first_write_at = self.first_write_at or now
                self.last_write_at = now
            stop.wait(WRITE_GAP)

    def _read(self, stop: threading.Event) -> None:
        rng = random.Random(self.seed)
        while not stop.is_set():
            page = self.pages[rng.randrange(len(self.pages))]
            rel = page.relative_to(self.root).as_posix()
            started = time.perf_counter()
            with foreground_activity.foreground_scope(self.root):
                commands.op_read_memory(self.root, rel, links=True)
            with self.lock:
                self.reads.append(time.perf_counter() - started)
            stop.wait(0.1)

    def run(self) -> None:
        stop = threading.Event()
        threads = [
            threading.Thread(target=self._write, args=(i, stop), daemon=True)
            for i in range(WRITERS)
        ]
        threads.append(threading.Thread(target=self._read, args=(stop,), daemon=True))
        for thread in threads:
            thread.start()
        stop.wait(BURST_SECONDS)
        stop.set()
        for thread in threads:
            thread.join(timeout=30)

    def p95(self) -> float:
        ordered = sorted(self.reads)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def _settle(root: Path, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not graph_drain.debt_pending(root):
            return True
        time.sleep(0.1)
    return False


def _instrument(monkeypatch: pytest.MonkeyPatch) -> dict[str, collections.Counter]:
    counts: dict[str, collections.Counter] = {"drain": collections.Counter()}
    original_pass = epistemic_graph.EpistemicGraphIndex._rebuild_all_pass

    def counted_pass(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        counts["drain"]["whole_vault_passes"] += 1
        return original_pass(self, *args, **kwargs)

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "_rebuild_all_pass", counted_pass)
    for module, name, label in (
        (deferred_index, "mark_graph_full_rebuild", "markers_raised"),
        (epistemic_graph, "recover_suspended_graph", "barrier_recoveries"),
    ):
        original = getattr(module, name)

        def wrapped(*args, _original=original, _label=label, **kwargs):  # noqa: ANN002, ANN003
            counts["drain"][_label] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapped)
    return counts


@pytest.mark.timeout(180)
def test_the_dreamer_yields_to_a_write_burst_and_changes_nothing_for_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, pages = _vault(tmp_path)
    counts = _instrument(monkeypatch)
    ticks: list[float] = []
    real_run_once = dreamer.run_once
    monkeypatch.setattr(
        dreamer,
        "run_once",
        lambda *a, **k: (ticks.append(time.monotonic()), real_run_once(*a, **k))[1],
    )
    graph_drain.start(root)
    assert _settle(root)
    for page in pages[:5]:
        commands.op_read_memory(root, page.relative_to(root).as_posix(), links=True)

    # Control: the dreamer is off.
    control = _Burst(root, pages, seed=1)
    control.run()
    assert _settle(root)
    control_drain = dict(counts["drain"])
    counts["drain"].clear()

    # The dreamer on, and mid-reseed when the burst starts.
    monkeypatch.setenv("EXOMEM_DREAMER", "on")
    assert dreamer.start(root) is not None
    deadline = time.monotonic() + 8.0
    while not ticks and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ticks, "the dreamer never became eligible before the burst"
    burst = _Burst(root, pages, seed=1)
    burst.run()
    first_write, last_write = burst.first_write_at, burst.last_write_at
    assert first_write is not None and last_write is not None
    during = [t for t in ticks if first_write <= t <= last_write]
    assert during == [], f"{len(during)} dreamer tick(s) started while writes were landing"
    assert _settle(root)
    deadline = time.monotonic() + 8.0
    while not any(t > last_write for t in ticks) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert any(t > last_write for t in ticks), "the dreamer did not resume after the burst"

    assert control.errors == burst.errors == collections.Counter()
    # No drain outcome the dreamer could cause moves: no marker raised, no
    # barrier recovered, and no whole-vault pass beyond the writes' own.
    for key in ("markers_raised", "barrier_recoveries"):
        assert abs(counts["drain"].get(key, 0) - control_drain.get(key, 0)) <= 1, key
    extra = counts["drain"].get("whole_vault_passes", 0) - burst.writes
    control_extra = control_drain.get("whole_vault_passes", 0) - control.writes
    assert extra <= max(control_extra, 0) + 1, (counts["drain"], control_drain)
    assert burst.p95() <= control.p95() * 1.10 + PAGE_ALLOWANCE_SECONDS, (
        burst.p95(),
        control.p95(),
    )
