#!/usr/bin/env python3
"""Synthetic, bounded relation-review scale and concurrency gate.

The harness creates a disposable Markdown corpus, uses the real graph-native
relation queue, and mutates two eligible canonical pages through the normal
batch writer.  Its output intentionally contains aggregate measurements only.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import tempfile
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from exomem import (
    corpus_aware,
    epistemic_graph,
    find,
    freshness,
    graph_sync,
    relation_queue,
    semantic_contract,
    vault,
    writer_lease,
)

_KB = "Knowledge Base/Notes/Insights"
_MAX_INDEXED_QUERIES = 12
_POST_RECOVERY_READS = 1


class ScaleGateError(AssertionError):
    """A scale run did not supply the evidence required by this gate."""


@dataclass(frozen=True)
class ScaleConfig:
    pages: int = 3_600
    streams: int = 20
    groups: int = 20

    def validate(self) -> None:
        if self.pages < 3_600:
            raise ValueError("pages must be at least 3600")
        if self.streams < 20:
            raise ValueError("streams must be at least 20")
        if not 1 <= self.groups <= 20:
            raise ValueError("groups must be between 1 and 20")


class _CountingConnection:
    def __init__(self, connection: Any, sample: dict[str, int]) -> None:
        self._connection = connection
        self._sample = sample

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self._sample["queries"] += 1
        return self._connection.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil((len(ordered) * percentile) / 100) - 1)
    return round(ordered[index], 3)


def _distribution(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "p50_ms": _percentile(values, 50),
        "p95_ms": _percentile(values, 95),
        "max_ms": round(max(values), 3) if values else 0.0,
    }


def _write_corpus(root: Path, pages: int) -> tuple[Path, tuple[Path, Path]]:
    target = root / _KB / "target.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: insight\nstatus: active\n---\n# Target\n\nSynthetic target.\n",
        encoding="utf-8",
    )
    sources: list[Path] = []
    target_link = f"{_KB}/target"
    for number in range(pages):
        page = root / _KB / f"source-{number:04d}.md"
        page.write_text(
            "---\ntype: insight\nstatus: active\n---\n"
            f"# Source {number:04d}\n\nSee [[{target_link}]].\n",
            encoding="utf-8",
        )
        if len(sources) < 2:
            sources.append(page)
    return target, (sources[0], sources[1])


def _prebuild_current_graph(root: Path) -> None:
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    freshness.rebaseline(root)
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()


def _timed_queue(
    root: Path,
    *,
    groups: int,
    available: list[float],
    unavailable: list[float],
    statuses: list[str],
    lock: threading.Lock,
    measurement: threading.local,
    samples: list[dict[str, int]],
    current_available_completions: list[int],
    final_commit_ns: list[int],
) -> None:
    sample = {"snapshots": 0, "queries": 0}
    measurement.sample = sample
    started = time.perf_counter_ns()
    try:
        result = relation_queue.build_queue(root, limit_pages=groups, limit_per_page=1)
        completed = time.perf_counter_ns()
        elapsed = (completed - started) / 1_000_000
        with lock:
            statuses.append(str(result.get("status") or "unavailable"))
            samples.append(sample)
            if result.get("status") == "available":
                available.append(elapsed)
                if final_commit_ns:
                    current_available_completions.append(completed)
            else:
                unavailable.append(elapsed)
    finally:
        del measurement.sample


def _monitor_queue_cost(measurement: threading.local) -> ExitStack:
    stack = ExitStack()
    original_open = epistemic_graph.EpistemicGraphIndex._open_read_snapshot
    original_walk = find._walk_md
    original_parse = find._parse_page
    original_cosine = corpus_aware._best_cosine_per_file

    def open_counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        connection = original_open(self, *args, **kwargs)
        if connection is None:
            return None
        sample = getattr(measurement, "sample", None)
        if sample is None:
            return connection
        sample["snapshots"] += 1
        return _CountingConnection(connection, sample)

    def walked(*args: Any, **kwargs: Any) -> Any:
        if getattr(measurement, "sample", None) is not None:
            raise ScaleGateError("structural queue violation: Markdown walk")
        return original_walk(*args, **kwargs)

    def parsed(*args: Any, **kwargs: Any) -> Any:
        if getattr(measurement, "sample", None) is not None:
            raise ScaleGateError("structural queue violation: Markdown parse")
        return original_parse(*args, **kwargs)

    def cosine(*args: Any, **kwargs: Any) -> Any:
        if getattr(measurement, "sample", None) is not None:
            raise ScaleGateError("structural queue violation: embedding call")
        return original_cosine(*args, **kwargs)

    stack.enter_context(
        patch.object(epistemic_graph.EpistemicGraphIndex, "_open_read_snapshot", open_counted)
    )
    stack.enter_context(patch.object(find, "_walk_md", walked))
    stack.enter_context(patch.object(find, "_parse_page", parsed))
    stack.enter_context(patch.object(corpus_aware, "_best_cosine_per_file", cosine))
    return stack


def _mutate_eligible_page(root: Path, page: Path, sequence: int) -> int:
    """Use the same request/fanout boundary an interactive writer uses."""
    before = graph_sync.read_checkpoint(root)
    original = page.read_text(encoding="utf-8")
    replacement = f"\nSynthetic canonical mutation {sequence}.\n"
    if replacement in original:
        raise ScaleGateError("invalid substitute: synthetic mutation became a no-op")
    def leaf(vault_root: Path, **_kwargs: Any) -> dict[str, Any]:
        vault.batch_atomic_write(
            [vault.PlannedWrite(page, original + replacement)],
            vault_root=vault_root,
            post_commit_fanout=True,
        )
        writer_lease.mark_active_mutation_committed()
        return {"status": "committed", "mutated": True}

    command = SimpleNamespace(name="remember", read_only=False, leaf=leaf)
    terminal = writer_lease.get_manager().invoke(command, (root,), {})
    if not isinstance(terminal, dict) or terminal.get("status") != "committed":
        raise ScaleGateError("mutation did not reach a committed production terminal")
    after = graph_sync.read_checkpoint(root)
    if after is None or (before is not None and after.generation <= before.generation):
        raise ScaleGateError("unchanged checkpoint generation after canonical mutation")
    return after.generation


def run_calibrated(
    *, root: Path, config: ScaleConfig | None = None
) -> dict[str, Any]:
    """Run the real, disposable calibrated workload and return aggregate data."""
    config = config or ScaleConfig()
    config.validate()
    root = Path(root)
    if root.exists():
        raise ValueError("synthetic root must not already exist")
    root.mkdir(parents=True)
    _, mutation_pages = _write_corpus(root, config.pages)
    _prebuild_current_graph(root)
    setup_queue = relation_queue.build_queue(root, limit_pages=config.groups, limit_per_page=1)
    coverage = dict(setup_queue.get("coverage") or {})
    eligible_pages = int(coverage.get("eligible_pages", 0))
    if setup_queue.get("status") != "available" or eligible_pages < config.pages:
        raise ScaleGateError("synthetic corpus did not publish eligible graph pages")

    start = threading.Barrier(config.streams)
    first_commit = threading.Event()
    overlap_reads_done = threading.Event()
    first_recovered = threading.Event()
    final_recovered = threading.Event()
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    queue_lock = threading.Lock()
    mutation_lock = threading.Lock()
    measurement = threading.local()
    available: list[float] = []
    unavailable: list[float] = []
    statuses: list[str] = []
    samples: list[dict[str, int]] = []
    current_available_completions: list[int] = []
    committed_generations: list[int] = []
    final_commit_ns: list[int] = []
    overlap = {
        "commit_boundary_entered": False,
        "queue_reads_completed_while_commit_boundary_held": 0,
        "queue_read_during_graph_mutation": False,
    }
    hold_first_commit = threading.Event()
    boundary_held = threading.Event()
    original_locked_write = vault._batch_atomic_write_locked

    def held_first_commit(*args: Any, **kwargs: Any) -> Any:
        writes = list(args[0]) if args else list(kwargs.get("writes", ()))
        writes_mutation_page = any(
            Path(write.path) == mutation_pages[0] for write in writes
        )
        if hold_first_commit.is_set() and writes_mutation_page:
            with mutation_lock:
                overlap["commit_boundary_entered"] = True
            boundary_held.set()
            first_commit.set()
            try:
                if not overlap_reads_done.wait(30):
                    raise ScaleGateError("non-overlapping mixed phase")
            finally:
                boundary_held.clear()
        return original_locked_write(*args, **kwargs)

    def record_error(error: BaseException) -> None:
        with errors_lock:
            errors.append(error)

    def queue_read() -> None:
        _timed_queue(
            root,
            groups=config.groups,
            available=available,
            unavailable=unavailable,
            statuses=statuses,
            lock=queue_lock,
            measurement=measurement,
            samples=samples,
            current_available_completions=current_available_completions,
            final_commit_ns=final_commit_ns,
        )

    def reader(stream: int) -> None:
        try:
            start.wait()
            if stream < 2:
                if not first_commit.wait(30):
                    raise ScaleGateError("missing graph-relevant commit boundary")
                queue_read()
                with mutation_lock:
                    if boundary_held.is_set():
                        overlap["queue_reads_completed_while_commit_boundary_held"] += 1
                    overlap["queue_read_during_graph_mutation"] = (
                        overlap["queue_reads_completed_while_commit_boundary_held"] >= 2
                    )
                    if overlap["queue_read_during_graph_mutation"]:
                        overlap_reads_done.set()
            if not final_recovered.wait(30):
                raise ScaleGateError("missing recovery after final graph-relevant commit")
            for _ in range(_POST_RECOVERY_READS):
                queue_read()
        except BaseException as error:  # noqa: BLE001 - transport thread errors
            record_error(error)

    def first_mutator() -> None:
        try:
            start.wait()
            hold_first_commit.set()
            generation = _mutate_eligible_page(root, mutation_pages[0], 1)
            with mutation_lock:
                committed_generations.append(generation)
            first_recovered.set()
        except BaseException as error:  # noqa: BLE001 - transport thread errors
            record_error(error)
            first_commit.set()
            first_recovered.set()

    def second_mutator() -> None:
        try:
            start.wait()
            if not first_recovered.wait(30):
                raise ScaleGateError("first mutation did not recover")
            generation = _mutate_eligible_page(root, mutation_pages[1], 2)
            with mutation_lock:
                committed_generations.append(generation)
                final_commit_ns.append(time.perf_counter_ns())
            final_recovered.set()
            for _ in range(_POST_RECOVERY_READS):
                queue_read()
        except BaseException as error:  # noqa: BLE001 - transport thread errors
            record_error(error)
            final_recovered.set()

    threads = [threading.Thread(target=first_mutator), threading.Thread(target=second_mutator)]
    threads.extend(
        threading.Thread(target=reader, args=(stream,))
        for stream in range(config.streams - 2)
    )
    with _monitor_queue_cost(measurement), patch.object(
        vault, "_batch_atomic_write_locked", held_first_commit
    ):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(90)

    if any(thread.is_alive() for thread in threads):
        raise ScaleGateError("controlled request stream did not finish")
    if errors:
        raise errors[0]
    if not final_commit_ns:
        raise ScaleGateError("missing final graph-relevant commit")

    recovery_ms = (
        (min(current_available_completions) - final_commit_ns[0]) / 1_000_000
        if current_available_completions
        else None
    )
    total = len(statuses)
    report: dict[str, Any] = {
        "schema": "relation-review-scale/v1",
        "corpus": {
            "eligible_pages": eligible_pages,
            "candidate_denominator": int(coverage.get("eligible_pages", 0)),
        },
        "workload": {"streams": config.streams, "requested_groups": config.groups},
        "overlap": overlap,
        "substitutes": {"validation_only": 0, "no_op": 0, "graph_excluded": 0},
        "mutations": {"committed": len(committed_generations), "busy": 0, "failed": 0},
        "checkpoints": {"committed_generations": sorted(committed_generations)},
        "queue": {
            "available": _distribution(available),
            "unavailable": _distribution(unavailable),
            "availability_ratio": round((len(available) / total) if total else 0.0, 6),
            "typed_statuses": sorted(set(status for status in statuses if status != "available")),
        },
        "recovery": {
            "current_available_after_final_commit_ms": round(recovery_ms, 3)
            if recovery_ms is not None
            else None,
        },
        "structural": {
            "requests_measured": len(samples),
            "snapshot_count": sum(sample["snapshots"] for sample in samples),
            "indexed_query_count": sum(sample["queries"] for sample in samples),
            "max_snapshots_per_request": max(
                (sample["snapshots"] for sample in samples), default=0
            ),
            "max_indexed_queries_per_request": max(
                (sample["queries"] for sample in samples), default=0
            ),
            "markdown_parses": 0,
            "embedding_calls": 0,
        },
    }
    validate_report(report)
    assert_privacy_safe(report)
    return report


def reference_report() -> dict[str, Any]:
    """A deterministic valid aggregate used to test each fail-closed classifier."""
    return {
        "schema": "relation-review-scale/v1",
        "corpus": {"eligible_pages": 3_600, "candidate_denominator": 3_599},
        "workload": {"streams": 20, "requested_groups": 20},
        "overlap": {
            "commit_boundary_entered": True,
            "queue_reads_completed_while_commit_boundary_held": 2,
            "queue_read_during_graph_mutation": True,
        },
        "substitutes": {"validation_only": 0, "no_op": 0, "graph_excluded": 0},
        "mutations": {"committed": 2, "busy": 0, "failed": 0},
        "checkpoints": {"committed_generations": [41, 42]},
        "queue": {
            "available": {"count": 20, "p50_ms": 10.0, "p95_ms": 15.0, "max_ms": 20.0},
            "unavailable": {"count": 1, "p50_ms": 2.0, "p95_ms": 3.0, "max_ms": 3.0},
            "availability_ratio": 0.95,
            "typed_statuses": ["warming"],
        },
        "recovery": {"current_available_after_final_commit_ms": 100.0},
        "structural": {
            "requests_measured": 21,
            "snapshot_count": 21,
            "indexed_query_count": 168,
            "max_snapshots_per_request": 1,
            "max_indexed_queries_per_request": 8,
            "markdown_parses": 0,
            "embedding_calls": 0,
        },
    }


def validate_report(report: dict[str, Any]) -> None:
    """Fail closed if a report cannot prove every calibrated gate."""
    corpus = report.get("corpus", {})
    workload = report.get("workload", {})
    mutations = report.get("mutations", {})
    queue = report.get("queue", {})
    checkpoints = report.get("checkpoints", {})
    structural = report.get("structural", {})
    substitutes = report.get("substitutes", {})
    if int(corpus.get("eligible_pages", 0)) < 3_600 or int(workload.get("streams", 0)) < 20:
        raise ScaleGateError("structural corpus or stream threshold failed")
    if int(workload.get("requested_groups", 0)) > 20:
        raise ScaleGateError("structural requested group threshold failed")
    if any(int(substitutes.get(name, 0)) for name in ("validation_only", "no_op", "graph_excluded")):
        raise ScaleGateError("invalid substitute detected")
    if int(mutations.get("committed", 0)) == 0 and int(mutations.get("busy", 0)):
        raise ScaleGateError("all-busy mutation run")
    if int(mutations.get("committed", 0)) < 2:
        raise ScaleGateError("fewer than two committed graph-relevant mutations")
    generations = list(checkpoints.get("committed_generations", []))
    if len(generations) < 2 or generations != sorted(set(generations)):
        raise ScaleGateError("unchanged checkpoint generations")
    overlap = report.get("overlap", {})
    if (
        not overlap.get("commit_boundary_entered")
        or int(overlap.get("queue_reads_completed_while_commit_boundary_held", 0)) < 2
        or not overlap.get("queue_read_during_graph_mutation")
    ):
        raise ScaleGateError("non-overlapping mixed phase")
    available = queue.get("available", {})
    unavailable = queue.get("unavailable", {})
    if int(available.get("count", 0)) == 0:
        raise ScaleGateError("all-warming reader run")
    if float(queue.get("availability_ratio", 0.0)) < 0.90:
        raise ScaleGateError("timing availability ratio threshold failed")
    recovery = report.get("recovery", {}).get("current_available_after_final_commit_ms")
    if recovery is None:
        raise ScaleGateError("missing recovery")
    if float(recovery) > 5_000:
        raise ScaleGateError("timing recovery threshold failed")
    if float(available.get("p95_ms", float("inf"))) >= 1_000 or float(available.get("max_ms", float("inf"))) >= 2_000:
        raise ScaleGateError("timing available latency threshold failed")
    if int(unavailable.get("count", 0)) and float(unavailable.get("p95_ms", float("inf"))) >= 250:
        raise ScaleGateError("timing unavailable latency threshold failed")
    if (
        int(structural.get("requests_measured", 0)) < 20
        or int(structural.get("snapshot_count", -1))
        != int(structural.get("requests_measured", 0))
        or int(structural.get("max_snapshots_per_request", -1)) != 1
        or int(structural.get("max_indexed_queries_per_request", _MAX_INDEXED_QUERIES + 1))
        > _MAX_INDEXED_QUERIES
        or int(structural.get("markdown_parses", -1)) != 0
        or int(structural.get("embedding_calls", -1)) != 0
    ):
        raise ScaleGateError("structural bounded-work threshold failed")


def assert_privacy_safe(report: dict[str, Any]) -> None:
    """Reject report shapes likely to carry corpus content or local identity."""
    forbidden_keys = {"path", "source_path", "content", "snippet", "hostname", "vault_key", "root"}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in forbidden_keys:
                    raise ScaleGateError("privacy-safe report contains a forbidden field")
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and (
            value.startswith(("/", "~"))
            or "\\" in value
            or "\n" in value
            or (len(value) >= 3 and value[1:3] in {":/", ":\\"})
        ):
            raise ScaleGateError("privacy-safe report contains path-like or source text")

    visit(report)


def render_report(report: dict[str, Any]) -> str:
    """Render the already privacy-checked aggregate report for the CLI."""
    assert_privacy_safe(report)
    return json.dumps(report, sort_keys=True, separators=(",", ":"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run the synthetic relation-review scale gate")
    parser.add_argument("--pages", type=int, default=3_600)
    parser.add_argument("--streams", type=int, default=20)
    parser.add_argument("--groups", type=int, default=20)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    config = ScaleConfig(pages=args.pages, streams=args.streams, groups=args.groups)
    logging.disable(logging.CRITICAL)
    try:
        if args.root is None:
            with tempfile.TemporaryDirectory(prefix="exomem-relation-scale-") as temporary:
                report = run_calibrated(root=Path(temporary) / "synthetic", config=config)
        else:
            report = run_calibrated(root=args.root, config=config)
    finally:
        logging.disable(logging.NOTSET)
    print(render_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())
