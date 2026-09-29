"""The sensed status on the read path under a write burst: no read regression.

close-memory-loop design.md:212 handed this change one condition: sensing may
not regress reads under the write-burst probes. With sensing on, `read_memory`
and `activate_context` compute the point-of-use status on every call, so this
probe measures both doors under the same write burst with sensing off and on.

A generated vault with semantic units and linked entity pages is sensed to
completion with a stub instrument, so the status path does real work: live
signature checks, released-edge reads and chains. The graph drain runs, three
writers commit every second, and a reader alternates the two doors. The dreamer
itself stays off in both arms: its gate is closed while writes land and the
sensor child is killed with it (test_dreamer_write_coexistence covers the tick
side), so the status is the only sensing work a burst can put on a read.

Arms run interleaved (off, on, off, on) so machine drift lands on both. The
guard is the dreamer write-coexistence probe's: p95 with sensing on stays within
twice the control's plus a fixed contention allowance. The measured numbers are
recorded in the change's design.md.
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
import sensing_fixture as sf

from exomem import (
    commands,
    dreamer,
    epistemic_graph,
    foreground_activity,
    freshness,
    graph_drain,
    sensed_model,
    sensor_worker,
)
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.kbdir import kb_dirname

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from synth_vault import gen_dense_vault, gen_entity_overlay  # noqa: E402

PAGES = 200
ENTITIES = 20
WRITERS = 3
WRITE_GAP = 1.0
BURST_SECONDS = 6.0
CONTENTION_ALLOWANCE_SECONDS = 0.25


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    freshness.clear()
    dreamer.reset_for_tests()
    sensor_worker.reset_for_tests()
    sensed_model._VECTORS.clear()
    monkeypatch.delenv("EXOMEM_DISABLE_GRAPH_DRAIN", raising=False)
    yield
    graph_drain.stop(timeout=5.0)
    dreamer.reset_for_tests()
    sensor_worker.reset_for_tests()
    freshness.clear()


class _Refines(sf.StubInstrument):
    """Every pair refines: the first input (in hash order) refines the second."""

    def judge(self, pairs):
        self.calls.extend(pairs)
        return [(sf.ENTAIL, sf.WEAK, None) for _pair in pairs]


def _vault(tmp_path: Path) -> tuple[Path, list[str], list[str]]:
    root = tmp_path / "vault"
    root.mkdir()
    notes = gen_dense_vault(root, PAGES, links_per_note=3)
    entities = gen_entity_overlay(root, ENTITIES)
    for index, rel in enumerate(entities):
        path = root / rel
        path.write_text(
            path.read_text(encoding="utf-8")
            + f"\n## Observations\n\n- [finding] Person {index} keeps the rig running.\n",
            encoding="utf-8",
        )
    for index, rel in enumerate(notes):
        entity = entities[index % ENTITIES][len(f"{kb_dirname()}/"):-3]
        path = root / rel
        path.write_text(
            path.read_text(encoding="utf-8")
            + f"\nSee [[{entity}]].\n\n## Observations\n\n"
            f"- [finding] Measurement {index} of subject {index % 7} holds steady.\n",
            encoding="utf-8",
        )
    find_module.clear_cache()
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
    return root, notes, entities


class _Burst:
    """Three writers and one reader alternating the two doors, for one arm."""

    def __init__(self, root: Path, notes: list[str], entities: list[str], seed: int) -> None:
        self.root = root
        self.notes = notes
        self.entities = entities
        self.seed = seed
        self.latency: dict[str, list[float]] = {"read_memory": [], "activate_context": []}
        self.statuses = 0
        self.errors: collections.Counter[str] = collections.Counter()
        self.writes = 0
        self.lock = threading.Lock()

    def _write(self, index: int, stop: threading.Event) -> None:
        from exomem.writer_lease import get_manager, mark_active_mutation_committed

        rng = random.Random(self.seed * 10 + index)
        revision = 0
        while not stop.is_set():
            page = self.root / self.notes[rng.randrange(len(self.notes))]
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
            with self.lock:
                self.writes += 1
            stop.wait(WRITE_GAP)

    def _read(self, stop: threading.Event) -> None:
        rng = random.Random(self.seed)
        turn = 0
        while not stop.is_set():
            turn += 1
            started = time.perf_counter()
            with foreground_activity.foreground_scope(self.root):
                if turn % 2:
                    door = "read_memory"
                    pool = self.entities if turn % 3 else self.notes
                    out = commands.op_read_memory(self.root, pool[rng.randrange(len(pool))])
                    found = "epistemic_status" in out
                else:
                    door = "activate_context"
                    person = rng.randrange(ENTITIES)
                    out = commands.op_activate_context(
                        self.root, turn=f"how is Synthetic Person {person:05d} doing"
                    )
                    found = any("epistemic_status" in a for a in out.get("anchors") or [])
            elapsed = time.perf_counter() - started
            with self.lock:
                self.latency[door].append(elapsed)
                self.statuses += int(found)
            stop.wait(0.05)

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


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def _settle(root: Path, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not graph_drain.debt_pending(root):
            return True
        time.sleep(0.1)
    return False


@pytest.mark.timeout(600)
def test_sensing_does_not_regress_reads_under_a_write_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sf.enable(monkeypatch)
    root, notes, entities = _vault(tmp_path)
    for _ in range(10):
        sf.converge(root, _Refines())
        if not sensed_model.queue_depth(root):
            break
    assert sensed_model.queue_depth(root) == 0
    conn = sensed_model.open_readonly(root)
    assert conn is not None
    pairs, current = conn.execute("SELECT count(*), sum(state='current') FROM pairs").fetchone()
    conn.close()
    assert current > 100, (pairs, current)

    graph_drain.start(root)
    assert _settle(root)
    arms: dict[str, list[_Burst]] = {"off": [], "on": []}
    for round_, setting in enumerate(("off", "on", "off", "on")):
        monkeypatch.setenv("EXOMEM_SENSING", setting)
        burst = _Burst(root, notes, entities, seed=round_)
        burst.run()
        assert _settle(root)
        arms[setting].append(burst)

    report = []
    for door in ("read_memory", "activate_context"):
        values = {
            setting: [v for burst in bursts for v in burst.latency[door]]
            for setting, bursts in arms.items()
        }
        row = {
            setting: (len(v), _quantile(v, 0.5), _quantile(v, 0.95))
            for setting, v in values.items()
        }
        report.append((door, row))
        off_p95, on_p95 = row["off"][2], row["on"][2]
        assert on_p95 <= off_p95 * 2.0 + CONTENTION_ALLOWANCE_SECONDS, (door, row)
    for door, row in report:
        print(
            f"{door}: off n={row['off'][0]} p50={row['off'][1] * 1000:.1f}ms "
            f"p95={row['off'][2] * 1000:.1f}ms | on n={row['on'][0]} "
            f"p50={row['on'][1] * 1000:.1f}ms p95={row['on'][2] * 1000:.1f}ms"
        )
    statuses = sum(burst.statuses for burst in arms["on"])
    writes = sum(burst.writes for bursts in arms.values() for burst in bursts)
    print(f"statuses attached with sensing on: {statuses}; writes: {writes}")
    assert sum(burst.statuses for burst in arms["off"]) == 0
    assert statuses > 0, "the on arms never exercised the status path"
    assert all(not burst.errors for bursts in arms.values() for burst in bursts)
