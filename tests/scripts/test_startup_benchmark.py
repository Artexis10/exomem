"""Unit tests for the startup benchmark's importtime parser."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "startup_benchmark.py"
SPEC = importlib.util.spec_from_file_location("startup_benchmark", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
startup_benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = startup_benchmark
SPEC.loader.exec_module(startup_benchmark)


def test_parse_importtime_and_aggregate_top_level_packages() -> None:
    timings = startup_benchmark.parse_importtime(
        """\
import time: self [us] | cumulative | imported package
import time:       120 |        120 | _io
import time:        80 |        200 | exomem.kbdir
import time:        30 |        230 | exomem.__main__
unrelated stderr message
"""
    )

    assert timings == [
        startup_benchmark.ImportTiming(module="_io", self_us=120, cumulative_us=120),
        startup_benchmark.ImportTiming(module="exomem.kbdir", self_us=80, cumulative_us=200),
        startup_benchmark.ImportTiming(module="exomem.__main__", self_us=30, cumulative_us=230),
    ]
    assert startup_benchmark.aggregate_by_top_level(timings) == [
        {"package": "exomem", "cumulative_ms": 0.43},
        {"package": "_io", "cumulative_ms": 0.12},
    ]
