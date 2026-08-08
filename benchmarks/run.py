#!/usr/bin/env python3
"""Desk-side launcher: `uv run python benchmarks/run.py <subcommand> ...`."""

from __future__ import annotations

import sys
from pathlib import Path

_BENCH = Path(__file__).resolve().parent
_REPO = _BENCH.parent

for entry in (str(_BENCH), str(_REPO / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from membench.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
