"""Prove the graph converges under sustained concurrent writes.

Phase 2's exit criterion, and the claim the whole change rests on. Three
properties, none of which the older design could hold at once on a large vault:

1. **No write blocks on graph repair.** A write returns on its own commit, never
   on a whole-vault rebuild it happened to trigger.
2. **The graph ends current, not pending.** Repair queued off the write path is
   still repair: once the drains catch up, the epoch must reach `current`.
3. **Zero drift.** The converged graph must agree with the Markdown it derives
   from, audited rather than asserted by construction.

The concurrency is the point. A vault-global optimistic proof gets *less* likely
to succeed as the vault and the write rate grow, so a single-writer run proves
nothing about the failure this change addresses. Run it with a writer active and
a large page count or do not bother.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_PAGES = 1_200
DEFAULT_SECONDS = 30.0
DEFAULT_DRAIN_INTERVAL = 0.5
#: A write that waited this long plainly parked on something. The commit budget
#: itself is 750 ms (`scripts/semantic_write_latency.py`); this is deliberately
#: looser, because the property under test is "did not join a rebuild" (tens of
#: seconds on a vault this size), not "was fast".
BLOCKING_WRITE_SECONDS = 5.0


def _imports(repo_root: Path) -> dict[str, Any]:
    for name in (
        "EXOMEM_DISABLE_EMBEDDINGS",
        "EXOMEM_DISABLE_CLIP",
        "EXOMEM_DISABLE_MEDIA_EXTRACTION",
        "EXOMEM_DISABLE_RANKING",
    ):
        os.environ[name] = "1"
    sys.path.insert(0, str(repo_root / "src"))
    sys.path.insert(0, str(repo_root / "scripts"))
    from synth_vault import gen_dense_vault

    from exomem import (
        audit,
        deferred_index,
        epistemic_graph,
        find,
        freshness,
        graph_sync,
        index_sync,
        vault,
    )
    from exomem.kbdir import kb_dirname
    from exomem.vault import walk_vault_md

    return {
        "audit": audit,
        "deferred_index": deferred_index,
        "epistemic_graph": epistemic_graph,
        "find": find,
        "freshness": freshness,
        "gen_dense_vault": gen_dense_vault,
        "graph_sync": graph_sync,
        "index_sync": index_sync,
        "kb_dirname": kb_dirname,
        "vault": vault,
        "walk_vault_md": walk_vault_md,
    }


def _seed_live_freshness(vault_root: Path, imports: dict[str, Any]) -> None:
    freshness = imports["freshness"]
    freshness.seed(
        vault_root,
        "vault",
        (
            (str(path), freshness.stat_signature(path))
            for path in imports["walk_vault_md"](vault_root)
        ),
    )
    freshness.seed(
        vault_root,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in imports["find"]._walk_md(vault_root / imports["kb_dirname"]())
        ),
    )


class _Writer(threading.Thread):
    """One writer committing real batches and dispatching real graph work."""

    def __init__(
        self, vault_root: Path, pages: list[Path], stop: threading.Event, imports: dict[str, Any]
    ) -> None:
        super().__init__(daemon=True, name="convergence-writer")
        self.vault_root = vault_root
        self.pages = pages
        self.stop = stop
        self.imports = imports
        self.latencies: list[float] = []
        self.outcomes: dict[str, int] = {}
        self.errors: list[str] = []

    def run(self) -> None:
        vault = self.imports["vault"]
        epistemic_graph = self.imports["epistemic_graph"]
        revision = 0
        while not self.stop.is_set():
            page = self.pages[revision % len(self.pages)]
            revision += 1
            started = time.perf_counter()
            try:
                vault.batch_atomic_write(
                    [
                        vault.PlannedWrite(
                            page,
                            page.read_text(encoding="utf-8") + f"\nRevision {revision}.\n",
                        )
                    ],
                    vault_root=self.vault_root,
                    post_commit_fanout=False,
                )
                _seed_live_freshness(self.vault_root, self.imports)
                result = epistemic_graph.upsert_after_write(self.vault_root, [page])
                outcome = f"{result.outcome}:{result.code}"
            except Exception as error:  # noqa: BLE001 - a writer failure is a finding
                outcome = f"error:{type(error).__name__}"
                self.errors.append(f"{type(error).__name__}: {error}")
            self.latencies.append((time.perf_counter() - started) * 1_000.0)
            self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1


class _Drainer(threading.Thread):
    """The queue consumer the watcher and CLI already call in production."""

    def __init__(
        self, vault_root: Path, stop: threading.Event, interval: float, imports: dict[str, Any]
    ) -> None:
        super().__init__(daemon=True, name="convergence-drainer")
        self.vault_root = vault_root
        self.stop = stop
        self.interval = interval
        self.imports = imports
        self.drains = 0
        self.errors: list[str] = []

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.imports["index_sync"].drain_deferred_work(self.vault_root)
                self.drains += 1
            except Exception as error:  # noqa: BLE001 - a drain failure is a finding
                self.errors.append(f"{type(error).__name__}: {error}")
            self.stop.wait(self.interval)


def _percentile(samples: list[float], fraction: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.999) - 1))
    return ordered[index]


def _converge(vault_root: Path, imports: dict[str, Any], *, deadline_seconds: float) -> bool:
    """Drain to quiescence, the way an idle system would."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        imports["index_sync"].drain_deferred_work(vault_root)
        imports["graph_sync"].drain_active_rebuilds()
        if not imports["deferred_index"].list_graph_paths(vault_root):
            return True
        time.sleep(0.2)
    return not imports["deferred_index"].list_graph_paths(vault_root)


def run(
    vault_root: Path,
    *,
    pages: int,
    seconds: float,
    writers: int,
    drain_interval: float,
    imports: dict[str, Any],
) -> dict[str, Any]:
    imports["gen_dense_vault"](vault_root, pages, links_per_note=3)
    _seed_live_freshness(vault_root, imports)
    epistemic_graph = imports["epistemic_graph"]
    index = epistemic_graph.EpistemicGraphIndex(vault_root)
    index.rebuild_all()
    if not index.available():
        raise RuntimeError("setup did not publish a current graph")

    kb_pages = sorted((vault_root / imports["kb_dirname"]()).rglob("*.md"))
    if not kb_pages:
        raise RuntimeError("synthetic vault produced no pages")

    stop = threading.Event()
    writer_threads = [
        _Writer(vault_root, kb_pages[index_ :: max(writers, 1)], stop, imports)
        for index_ in range(max(writers, 1))
    ]
    drainer = _Drainer(vault_root, stop, drain_interval, imports)
    started = time.perf_counter()
    for thread in writer_threads:
        thread.start()
    drainer.start()
    stop.wait(seconds)
    stop.set()
    for thread in writer_threads:
        thread.join(timeout=120)
    drainer.join(timeout=120)
    elapsed = time.perf_counter() - started

    quiesced = _converge(vault_root, imports, deadline_seconds=300.0)
    state = imports["graph_sync"].status(vault_root)["state"]
    drift = imports["audit"].audit(vault_root, categories=["graph_drift"]).findings

    latencies = [value for thread in writer_threads for value in thread.latencies]
    outcomes: dict[str, int] = {}
    errors: list[str] = list(drainer.errors)
    for thread in writer_threads:
        for key, count in thread.outcomes.items():
            outcomes[key] = outcomes.get(key, 0) + count
        errors.extend(thread.errors)

    return {
        "pages": pages,
        "writers": max(writers, 1),
        "elapsed_seconds": round(elapsed, 1),
        "writes": len(latencies),
        "drains": drainer.drains,
        "outcomes": outcomes,
        "write_ms": {
            "median": round(statistics.median(latencies), 1) if latencies else 0.0,
            "p95": round(_percentile(latencies, 0.95), 1),
            "max": round(max(latencies), 1) if latencies else 0.0,
        },
        "blocking_writes": sum(
            1 for value in latencies if value > BLOCKING_WRITE_SECONDS * 1_000.0
        ),
        "quiesced": quiesced,
        "queue_remaining": len(imports["deferred_index"].list_graph_paths(vault_root)),
        "graph_state": state,
        "drift_findings": len(drift),
        "errors": errors[:10],
    }


def verdict(report: dict[str, Any]) -> list[str]:
    """Every way this run failed its exit criteria, named."""
    failures: list[str] = []
    if report["writes"] == 0:
        failures.append("no writes were attempted")
    if report["blocking_writes"]:
        failures.append(
            f"{report['blocking_writes']} write(s) exceeded {BLOCKING_WRITE_SECONDS:.0f}s "
            "-- a write parked on graph repair"
        )
    if not report["quiesced"] or report["queue_remaining"]:
        failures.append(f"queue did not drain ({report['queue_remaining']} path(s) left)")
    if report["graph_state"] != "current":
        failures.append(f"graph settled at {report['graph_state']!r}, not 'current'")
    if report["drift_findings"]:
        failures.append(f"{report['drift_findings']} graph drift finding(s) after convergence")
    if report["errors"]:
        failures.append(f"{len(report['errors'])} error(s), first: {report['errors'][0]}")
    return failures


def format_report(report: dict[str, Any]) -> str:
    outcomes = ", ".join(f"{key}={count}" for key, count in sorted(report["outcomes"].items()))
    write = report["write_ms"]
    return (
        f"pages={report['pages']} writers={report['writers']} "
        f"elapsed={report['elapsed_seconds']}s writes={report['writes']} "
        f"drains={report['drains']}\n"
        f"  write ms median/p95/max={write['median']}/{write['p95']}/{write['max']} "
        f"blocking={report['blocking_writes']}\n"
        f"  outcomes: {outcomes or '(none)'}\n"
        f"  after convergence: graph_state={report['graph_state']} "
        f"queue_remaining={report['queue_remaining']} drift={report['drift_findings']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", type=Path)
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES)
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    parser.add_argument("--writers", type=int, default=1)
    parser.add_argument("--drain-interval", type=float, default=DEFAULT_DRAIN_INTERVAL)
    parser.add_argument("--root", type=Path, help="reuse this directory instead of a temp one")
    parser.add_argument(
        "--check", action="store_true", help="exit non-zero on any failed criterion"
    )
    args = parser.parse_args(argv)

    imports = _imports(args.repo_root.resolve())
    if args.root is not None:
        root = args.root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        report = run(
            root,
            pages=args.pages,
            seconds=args.seconds,
            writers=args.writers,
            drain_interval=args.drain_interval,
            imports=imports,
        )
    else:
        import tempfile

        with tempfile.TemporaryDirectory(
            prefix="graph-convergence-", ignore_cleanup_errors=True
        ) as temp:
            root = Path(temp) / "vault"
            root.mkdir()
            report = run(
                root,
                pages=args.pages,
                seconds=args.seconds,
                writers=args.writers,
                drain_interval=args.drain_interval,
                imports=imports,
            )
            imports["graph_sync"].drain_active_rebuilds()

    print(format_report(report), flush=True)
    failures = verdict(report)
    for failure in failures:
        print(f"FAIL: {failure}", flush=True)
    if not failures:
        print("PASS: no write blocked, the graph is current, and drift is zero", flush=True)
    return 1 if (failures and args.check) else 0


if __name__ == "__main__":
    raise SystemExit(main())
