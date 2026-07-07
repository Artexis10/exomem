"""Compare Exomem's Python keyword path with a Rust keyword-slice prototype.

This is a diagnostic harness, not a production backend. It isolates the
language-sensitive part of the keyword path: recursively read markdown under
`Knowledge Base/`, parse the same minimal page fields, require every query
token as a literal title/body substring, then sort by `updated` desc and path
desc.

Rows:
- `python_tool_keyword`: current `find(mode="keyword", scope="kb-only")` path
- `python_lane_scan`: reference `_keyword_match_paths` scan (`EXOMEM_LEXICAL_BACKEND=python`)
- `python_lane_current`: current hybrid keyword lane policy (`auto`, normally FTS5)
- `rust_lite`: the Rust implementation in `experiments/rust_find_keyword_lite`
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CRATE_ROOT = REPO_ROOT / "experiments" / "rust_find_keyword_lite"
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from synth_vault import gen_dense_vault  # noqa: E402

from exomem import find as find_module  # noqa: E402

DEFAULT_QUERIES = (
    "topic prose paragraph",
    "synthetic dense graph",
    "related links context",
    "body text rank",
    "note topic",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, help="Use an existing vault instead of a synthetic temp vault.")
    parser.add_argument("--size", type=int, default=2000, help="Synthetic note count when --vault is omitted.")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1, help="Warmup passes per implementation before sampling.")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--query", action="append", dest="queries", help="Query to measure; repeatable.")
    parser.add_argument("--skip-build", action="store_true", help="Assume the Rust binary already exists.")
    args = parser.parse_args(argv)

    cargo = shutil.which("cargo")
    if cargo is None:
        raise SystemExit("cargo not found on PATH")
    rust_bin = build_rust(cargo, skip_build=args.skip_build)

    queries = tuple(args.queries or DEFAULT_QUERIES)
    with maybe_synthetic_vault(args.vault, args.size) as vault:
        rows = [
            measure_python_tool_keyword(vault, queries, args.repeat, args.warmup, args.limit),
            measure_python_lane("python_lane_scan", vault, queries, args.repeat, args.warmup, args.limit, backend="python"),
            measure_python_lane("python_lane_current", vault, queries, args.repeat, args.warmup, args.limit, backend="auto"),
            measure_rust(rust_bin, vault, queries, args.repeat, args.warmup, args.limit),
        ]
        print_table(rows)
        verify_same_paths(rust_bin, vault, queries, args.limit)
    return 0


def build_rust(cargo: str, *, skip_build: bool = False) -> Path:
    manifest = CRATE_ROOT / "Cargo.toml"
    target_dir = Path(os.environ.get("CARGO_TARGET_DIR", CRATE_ROOT / "target"))
    exe = "rust_find_keyword_lite.exe" if os.name == "nt" else "rust_find_keyword_lite"
    rust_bin = target_dir / "release" / exe
    if not skip_build:
        env = os.environ.copy()
        env["CARGO_TARGET_DIR"] = str(target_dir)
        subprocess.run(
            [cargo, "build", "--release", "--manifest-path", str(manifest)],
            check=True,
            cwd=REPO_ROOT,
            env=env,
        )
    if not rust_bin.exists():
        raise SystemExit(f"Rust binary not found: {rust_bin}")
    return rust_bin


class maybe_synthetic_vault:
    def __init__(self, vault: Path | None, size: int) -> None:
        self.vault = vault
        self.size = size

    def __enter__(self) -> Path:
        if self.vault is not None:
            return self.vault
        scratch = CRATE_ROOT / "target" / "pytest-vaults"
        root = scratch / f"bench-{self.size}"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        gen_dense_vault(root, self.size)
        return root

    def __exit__(self, *_exc: object) -> None:
        return None


def measure_python_tool_keyword(
    vault: Path,
    queries: tuple[str, ...],
    repeat: int,
    warmup: int,
    limit: int,
) -> dict[str, Any]:
    def run(query: str) -> int:
        old_cache = os.environ.get("EXOMEM_FIND_CACHE_SIZE")
        os.environ["EXOMEM_FIND_CACHE_SIZE"] = "0"
        try:
            hits = find_module.find(
                vault,
                query=query,
                mode="keyword",
                scope="kb-only",
                graph=False,
                limit=limit,
            )
            return len(hits)
        finally:
            restore_env("EXOMEM_FIND_CACHE_SIZE", old_cache)

    return measure("python_tool_keyword", queries, repeat, warmup, run)


def measure_python_lane(
    name: str,
    vault: Path,
    queries: tuple[str, ...],
    repeat: int,
    warmup: int,
    limit: int,
    *,
    backend: str,
) -> dict[str, Any]:
    def run(query: str) -> int:
        old_backend = os.environ.get("EXOMEM_LEXICAL_BACKEND")
        os.environ["EXOMEM_LEXICAL_BACKEND"] = backend
        try:
            paths = find_module._keyword_match_paths(vault, query.lower().strip(), "kb")[:limit]
            return len(paths)
        finally:
            restore_env("EXOMEM_LEXICAL_BACKEND", old_backend)

    return measure(name, queries, repeat, warmup, run)


def measure_rust(
    rust_bin: Path,
    vault: Path,
    queries: tuple[str, ...],
    repeat: int,
    warmup: int,
    limit: int,
) -> dict[str, Any]:
    def run(query: str) -> int:
        out = subprocess.run(
            [str(rust_bin), "--vault", str(vault), "--query", query, "--limit", str(limit)],
            check=True,
            text=True,
            capture_output=True,
        )
        return len(json.loads(out.stdout)["hits"])

    return measure("rust_lite", queries, repeat, warmup, run)


def measure(
    name: str,
    queries: tuple[str, ...],
    repeat: int,
    warmup: int,
    run: Callable[[str], int],
) -> dict[str, Any]:
    for _ in range(warmup):
        for query in queries:
            run(query)
    samples = []
    hits = 0
    for _ in range(repeat):
        for query in queries:
            t0 = time.perf_counter()
            hits += run(query)
            samples.append((time.perf_counter() - t0) * 1000.0)
    return {
        "name": name,
        "n": len(samples),
        "median": statistics.median(samples),
        "p90": percentile(samples, 90),
        "min": min(samples),
        "max": max(samples),
        "hits": hits,
    }


def verify_same_paths(rust_bin: Path, vault: Path, queries: tuple[str, ...], limit: int) -> None:
    old_backend = os.environ.get("EXOMEM_LEXICAL_BACKEND")
    os.environ["EXOMEM_LEXICAL_BACKEND"] = "python"
    try:
        for query in queries:
            query_norm = query.lower().strip()
            expected = find_module._keyword_match_paths(vault, query_norm, "kb")[:limit]
            out = subprocess.run(
                [str(rust_bin), "--vault", str(vault), "--query", query, "--limit", str(limit)],
                check=True,
                text=True,
                capture_output=True,
            )
            got = [hit["path"] for hit in json.loads(out.stdout)["hits"]]
            if got != expected:
                raise SystemExit(
                    f"Rust/Python path mismatch for {query!r}\nexpected={expected}\ngot={got}"
                )
    finally:
        restore_env("EXOMEM_LEXICAL_BACKEND", old_backend)


def restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def percentile(samples: list[float], pct: float) -> float:
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, int(len(ordered) * pct / 100 + 0.999999) - 1))
    return ordered[idx]


def print_table(rows: list[dict[str, Any]]) -> None:
    print("| implementation | n | median ms | p90 ms | min ms | max ms | total hits |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for row in rows:
        print(
            f"| {row['name']} | {row['n']} | {row['median']:.2f} | {row['p90']:.2f} | "
            f"{row['min']:.2f} | {row['max']:.2f} | {row['hits']} |"
        )


if __name__ == "__main__":
    raise SystemExit(main())
