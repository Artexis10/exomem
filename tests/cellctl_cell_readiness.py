"""cellctl's `cell_readiness` module, loaded by path for the contract tests.

cellctl starts a cell's health-only listener and reads its readiness answer by
the names in that module, and judges the answer with its verdict. The module
imports only the standard library, so this suite loads it without cellctl's
dependencies, and a rename on either side fails a test here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "infra" / "cellctl" / "src" / "cellctl" / "cell_readiness.py"

_spec = importlib.util.spec_from_file_location("cellctl_cell_readiness", PATH)
assert _spec is not None and _spec.loader is not None
cell_readiness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cell_readiness)
