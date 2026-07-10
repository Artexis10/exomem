#!/usr/bin/env python3
"""Measure one-shot startup costs for the Exomem CLI.

The import profile deliberately uses the module entry point rather than a
console-script wrapper so it is portable across development environments.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

IMPORTTIME_LINE = re.compile(
    r"^import time:\s*(?P<self_us>\d+)\s*\|\s*"
    r"(?P<cumulative_us>\d+)\s*\|\s*(?P<module>.+?)\s*$"
)
SAMPLES = 3
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


@dataclass(frozen=True)
class ImportTiming:
    """One row from Python's ``-X importtime`` output."""

    module: str
    self_us: int
    cumulative_us: int


def parse_importtime(stderr: str) -> list[ImportTiming]:
    """Parse the import-time rows from stderr, ignoring non-profile output."""
    timings = []
    for line in stderr.splitlines():
        match = IMPORTTIME_LINE.match(line)
        if match:
            timings.append(
                ImportTiming(
                    module=match.group("module"),
                    self_us=int(match.group("self_us")),
                    cumulative_us=int(match.group("cumulative_us")),
                )
            )
    return timings


def aggregate_by_top_level(timings: list[ImportTiming]) -> list[dict[str, float | str]]:
    """Sum cumulative import time for each top-level import package."""
    totals: defaultdict[str, int] = defaultdict(int)
    for timing in timings:
        top_level = timing.module.split(".", 1)[0]
        totals[top_level] += timing.cumulative_us
    return [
        {"package": package, "cumulative_ms": round(cumulative_us / 1000, 3)}
        for package, cumulative_us in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    ]


def measure_import_time() -> tuple[float, list[dict[str, float | str]]]:
    """Return total module import time and cumulative package totals."""
    completed = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", "import exomem.__main__"],
        check=True,
        capture_output=True,
        text=True,
    )
    timings = parse_importtime(completed.stderr)
    if not timings:
        raise RuntimeError("Python produced no importtime rows")
    return max(timing.cumulative_us for timing in timings) / 1000, aggregate_by_top_level(timings)


def command_environment() -> dict[str, str]:
    """Build the deterministic environment for model-free product probes."""
    environment = os.environ.copy()
    environment["EXOMEM_DISABLE_EMBEDDINGS"] = "1"
    environment["EXOMEM_VAULT_PATH"] = str(FIXTURES_DIR)
    return environment


def median_command_time(command: list[str], environment: dict[str, str]) -> float:
    """Measure a command end-to-end three times and return its median in milliseconds."""
    samples = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 3)


def benchmark(top: int) -> dict[str, object]:
    """Run the import profile and both one-shot CLI probes."""
    total_import_ms, package_totals = measure_import_time()
    environment = command_environment()
    commands = {
        "exomem --help": [sys.executable, "-m", "exomem", "--help"],
        "exomem browse_memory --samples 1": [
            sys.executable,
            "-m",
            "exomem",
            "browse_memory",
            "--samples",
            "1",
        ],
    }
    return {
        "import_time": {
            "total_ms": round(total_import_ms, 3),
            "top_packages": package_totals[:top],
        },
        "wall_time": {
            label: median_command_time(command, environment) for label, command in commands.items()
        },
    }


def print_markdown(result: dict[str, object]) -> None:
    """Render a compact, copyable before/after comparison table."""
    import_time = result["import_time"]
    wall_time = result["wall_time"]
    assert isinstance(import_time, dict)
    assert isinstance(wall_time, dict)

    print("# CLI startup benchmark")
    print(f"\nTotal import time: {import_time['total_ms']:.3f} ms")
    print("\n| Package | Cumulative import time (ms) |")
    print("| --- | ---: |")
    for item in import_time["top_packages"]:
        assert isinstance(item, dict)
        print(f"| {item['package']} | {item['cumulative_ms']:.3f} |")
    print("\n| Command (median of 3) | Wall time (ms) |")
    print("| --- | ---: |")
    for label, milliseconds in wall_time.items():
        print(f"| {label} | {milliseconds:.3f} |")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=15, help="number of import packages to show")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    if args.top < 1:
        parser.error("--top must be at least 1")

    result = benchmark(args.top)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_markdown(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
