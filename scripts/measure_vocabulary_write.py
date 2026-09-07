"""Quiescent ordinary-write comparison for the vocabulary change.

This is an instrument, not a gate. It keeps fixture construction and warm-up
outside the timed samples, then invokes the same canonical write boundary a
normal command uses. The selected source tree comes solely from ``PYTHONPATH``.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import pstats
import resource
import statistics
import tempfile
import time
import tracemalloc
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from synth_vault import gen_dense_vault

from exomem import (
    epistemic_graph,
    find_corpus,
    memory_refs,
    semantic_contract,
    semantic_writes,
    writer_lease,
)
from exomem.writer_lease import LeaseConfig, LeaseManager


def _p90(values: list[float]) -> float:
    return sorted(values)[min(len(values) - 1, max(0, int(len(values) * 0.9 + 0.999) - 1))]


def _fixture(root: Path, pages: int) -> str:
    gen_dense_vault(root, pages, links_per_note=3)
    sources = root / "Knowledge Base/Sources/Articles"
    entities = root / "Knowledge Base/Entities/Organizations"
    sources.mkdir(parents=True, exist_ok=True)
    entities.mkdir(parents=True, exist_ok=True)
    for index in range(2):
        (sources / f"source-{index}.md").write_text(
            "---\n"
            "type: source\n"
            f"url: https://example.test/source-{index}\n"
            f"exomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, f'source-{index}')}\n"
            "---\n\nIndependent ordinary account.\n",
            encoding="utf-8",
        )
    target = entities / "first.md"
    target.write_text(
        "---\n"
        "type: organization\nstatus: active\n"
        f"exomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, 'first')}\n"
        'sources: ["[[Sources/Articles/source-0]]", "[[Sources/Articles/source-1]]"]\n'
        "---\n\nOrdinary sourced measurement context for [[Entities/Organizations/second]].\n"
        "- Ordinary measurement revision 0\n",
        encoding="utf-8",
    )
    (entities / "second.md").write_text(
        "---\ntype: organization\nstatus: active\n"
        f"exomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, 'second')}\n"
        "---\n\nOrdinary endpoint.\n",
        encoding="utf-8",
    )
    return "Knowledge Base/Entities/Organizations/first.md"


def _counter() -> tuple[dict[str, int], Any]:
    counts = {"find_corpus.walk_md": 0, "memory_refs._scan_pages": 0}
    original_walk = find_corpus.walk_md
    original_scan = memory_refs._scan_pages

    def walk(*args: Any, **kwargs: Any):
        counts["find_corpus.walk_md"] += 1
        return original_walk(*args, **kwargs)

    def scan(*args: Any, **kwargs: Any):
        counts["memory_refs._scan_pages"] += 1
        return original_scan(*args, **kwargs)

    find_corpus.walk_md = walk
    memory_refs._scan_pages = scan

    def restore() -> None:
        find_corpus.walk_md = original_walk
        memory_refs._scan_pages = original_scan

    return counts, restore


def _sample(root: Path, rel_path: str, version: int, manager: LeaseManager) -> float:
    path = root / rel_path
    before = path.read_text(encoding="utf-8")
    after = before.replace(f"revision {version - 1}", f"revision {version}")

    def leaf(vault_root: Path) -> dict[str, str]:
        preflight = semantic_writes.preflight_existing(
            vault_root, path=rel_path, after_source=after, operation="observe"
        )
        if preflight.contract_result.should_block:
            raise RuntimeError(
                "ordinary fixture was blocked: "
                + ",".join(item.code for item in preflight.contract_result.blocking_findings)
            )
        semantic_writes.commit_existing(vault_root, preflight=preflight)
        return {"path": rel_path}

    command = SimpleNamespace(name="observe_memory", leaf=leaf, read_only=False)
    started = time.perf_counter()
    manager_token = writer_lease._ACTIVE_LEASE_MANAGER.set(manager)
    try:
        manager.invoke(command, (root,), {}, mutation_request_id=str(uuid.uuid4()))
    finally:
        writer_lease._ACTIVE_LEASE_MANAGER.reset(manager_token)
    return (time.perf_counter() - started) * 1_000.0


def _profile_sample(root: Path, rel_path: str, manager: LeaseManager) -> list[dict[str, Any]]:
    profiler = cProfile.Profile()
    profiler.runcall(_sample, root, rel_path, 2, manager)
    rows = []
    names = ("vocabulary", "review_state", "deferred_index", "epistemic_graph")
    statistics_by_function = pstats.Stats(profiler).stats
    for (filename, line, name), (calls, _, own, cumulative, _) in statistics_by_function.items():
        if any(part in filename or part in name for part in names):
            rows.append(
                {
                    "function": f"{Path(filename).name}:{line}:{name}",
                    "calls": calls,
                    "own_ms": round(own * 1_000.0, 3),
                    "cumulative_ms": round(cumulative * 1_000.0, 3),
                }
            )
    return sorted(rows, key=lambda row: row["cumulative_ms"], reverse=True)[:40]


def measure(*, label: str, pages: int, samples: int, profile: bool = False) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="exomem-vocabulary-perf-") as temporary:
        root = Path(temporary) / "vault"
        rel_path = _fixture(root, pages)
        semantic_contract.build_corpus_context(root)
        manager = LeaseManager(LeaseConfig(state_dir=Path(temporary) / "lease"))
        manager_token = writer_lease._ACTIVE_LEASE_MANAGER.set(manager)
        try:
            epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
        finally:
            writer_lease._ACTIVE_LEASE_MANAGER.reset(manager_token)
        # Warm all lazy imports/registries and keep this call outside the sample set.
        _sample(root, rel_path, 1, manager)
        if profile:
            return {
                "label": label,
                "pages": pages,
                "profile": "one warmed LeaseManager.invoke sample; not an acceptance timing",
                "vocabulary_rows": _profile_sample(root, rel_path, manager),
            }
        counts, restore = _counter()
        tracemalloc.start()
        timings = []
        try:
            for version in range(2, samples + 2):
                timings.append(_sample(root, rel_path, version, manager))
            _, trace_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            restore()
        return {
            "label": label,
            "pages": pages,
            "samples": samples,
            "instrumentation": "tracemalloc enabled during timed samples",
            "latency_ms": [round(value, 3) for value in timings],
            "p50_ms": round(statistics.median(timings), 3),
            "p90_ms": round(_p90(timings), 3),
            "tracemalloc_peak_bytes": trace_peak,
            "ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "whole_vault_scan_calls": counts,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--pages", type=int, nargs="+", default=[10, 100])
    parser.add_argument("--samples", type=int, default=9)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    if (not args.profile and args.samples < 3) or any(size < 2 for size in args.pages):
        raise SystemExit("samples must be at least 3 and pages at least 2")
    isolated_keys = (
        "XDG_STATE_HOME", "XDG_CONFIG_HOME", "EXOMEM_STATE_ROOT",
        "EXOMEM_WRITER_LEASE_STATE_DIR",
    )
    previous = {key: os.environ.get(key) for key in isolated_keys}
    with tempfile.TemporaryDirectory(prefix="exomem-vocabulary-measure-state-") as state:
        for key in isolated_keys:
            os.environ[key] = str(Path(state) / key.lower())
        try:
            print(
                json.dumps(
                    [
                        measure(
                            label=args.label, pages=size, samples=args.samples,
                            profile=args.profile,
                        )
                        for size in args.pages
                    ],
                    indent=2,
                )
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    main()
