"""Aggregate-only semantic validate/commit latency gate at realistic scale."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from synth_vault import gen_dense_vault  # noqa: E402

from exomem import find, freshness, semantic_contract, semantic_writes  # noqa: E402
from exomem.vault import walk_vault_md  # noqa: E402

DEFAULT_SIZES = (2_000, 8_000)
VALIDATE_MEDIAN_MS = 500.0
VALIDATE_P95_MS = 1_000.0
COMMIT_MEDIAN_MS = 750.0
COMMIT_P95_MS = 1_500.0
SCALING_RATIO = 2.0
SCALING_SLACK_MS = 200.0


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.999) - 1))
    return ordered[index]


def _seed_freshness(vault_root: Path) -> None:
    freshness.seed(
        vault_root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in walk_vault_md(vault_root)),
    )
    kb = vault_root / "Knowledge Base"
    freshness.seed(
        vault_root,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find._walk_md(kb)),
    )


def _next_source(source: str, version: int) -> str:
    marker = "Synthetic write latency version "
    start = source.index(marker) + len(marker)
    end = source.index(" ", start)
    return source[:start] + str(version) + source[end:]


def _transition(vault_root: Path, rel_path: str, version: int) -> tuple[float, float]:
    path = vault_root / rel_path
    before = path.read_text(encoding="utf-8")
    after = _next_source(before, version)
    started = time.perf_counter()
    preflight = semantic_writes.preflight_existing(
        vault_root,
        path=rel_path,
        after_source=after,
        operation="observe",
    )
    validate_ms = (time.perf_counter() - started) * 1_000.0
    if preflight.contract_result.should_block:
        codes = [item.code for item in preflight.contract_result.blocking_findings]
        raise RuntimeError(f"synthetic transition was blocked: {codes}")
    started = time.perf_counter()
    semantic_writes.commit_existing(vault_root, preflight=preflight)
    commit_ms = (time.perf_counter() - started) * 1_000.0
    return validate_ms, commit_ms


def measure(vault_root: Path, size: int, samples: int) -> dict[str, float | int]:
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    gen_dense_vault(vault_root, size, links_per_note=3)
    target_rel = "Knowledge Base/Entities/Concepts/write-latency-target.md"
    target = vault_root / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n"
        "title: Write latency target\n"
        "type: entity\n"
        "status: active\n"
        "updated: 2026-07-22\n"
        "---\n\n"
        "# Write latency target\n\n"
        "## Observations\n\n"
        "- [config] Synthetic write latency version 0 #latency (benchmark) ^latency-gate\n",
        encoding="utf-8",
    )

    cold_started = time.perf_counter()
    semantic_contract.build_corpus_context(vault_root)
    cold_ms = (time.perf_counter() - cold_started) * 1_000.0
    _seed_freshness(vault_root)
    semantic_contract.build_corpus_context(vault_root)

    # Install the activation boundary and warm derived sidecars outside samples.
    _transition(vault_root, target_rel, 1)
    validates: list[float] = []
    commits: list[float] = []
    for version in range(2, samples + 2):
        validate_ms, commit_ms = _transition(vault_root, target_rel, version)
        validates.append(validate_ms)
        commits.append(commit_ms)
    return {
        "pages": size,
        "samples": samples,
        "cold_ms": round(cold_ms, 1),
        "validate_median_ms": round(statistics.median(validates), 1),
        "validate_p95_ms": round(_percentile(validates, 0.95), 1),
        "commit_median_ms": round(statistics.median(commits), 1),
        "commit_p95_ms": round(_percentile(commits, 0.95), 1),
    }


def check(results: list[dict[str, float | int]]) -> None:
    failures: list[str] = []
    for result in results:
        pages = int(result["pages"])
        for key, ceiling in (
            ("validate_median_ms", VALIDATE_MEDIAN_MS),
            ("validate_p95_ms", VALIDATE_P95_MS),
            ("commit_median_ms", COMMIT_MEDIAN_MS),
            ("commit_p95_ms", COMMIT_P95_MS),
        ):
            value = float(result[key])
            if value >= ceiling:
                failures.append(f"{pages} pages: {key}={value:.1f}ms >= {ceiling:.1f}ms")
    if len(results) >= 2:
        ordered = sorted(results, key=lambda item: int(item["pages"]))
        small, large = ordered[0], ordered[-1]
        for operation in ("validate", "commit"):
            key = f"{operation}_median_ms"
            bound = float(small[key]) * SCALING_RATIO + SCALING_SLACK_MS
            if float(large[key]) >= bound:
                failures.append(
                    f"{operation} scaling: {large[key]}ms >= {bound:.1f}ms "
                    f"({small['pages']} -> {large['pages']} pages)"
                )
    if failures:
        raise SystemExit("semantic write latency gate failed: " + "; ".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=list(DEFAULT_SIZES))
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name in (
        "EXOMEM_DISABLE_EMBEDDINGS",
        "EXOMEM_DISABLE_CLIP",
        "EXOMEM_DISABLE_MEDIA_EXTRACTION",
        "EXOMEM_DISABLE_RANKING",
    ):
        os.environ[name] = "1"

    if args.root is not None:
        args.root.mkdir(parents=True, exist_ok=False)
        runtime_temp = args.root / "runtime-temp"
        runtime_temp.mkdir()
        tempfile.tempdir = str(runtime_temp)
        roots = [args.root / f"vault-{size}" for size in args.sizes]
        for root in roots:
            root.mkdir(parents=True)
        results = [
            measure(root, size, args.samples) for root, size in zip(roots, args.sizes, strict=True)
        ]
    else:
        with tempfile.TemporaryDirectory(prefix="exomem-write-latency-") as temp:
            base = Path(temp)
            results = []
            for size in args.sizes:
                root = base / f"vault-{size}"
                root.mkdir()
                results.append(measure(root, size, args.samples))
    print(json.dumps({"results": results}, sort_keys=True))
    if args.check:
        check(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
