"""Shape of scripts/activation_lexical_latency.py at a toy scale.

The harness's numbers are workstation evidence, not a CI gate; this only
proves it runs end to end, reports both shapes, and that the bounded shape
keeps the budget and still puts the named page first.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "activation_lexical_latency.py"
SPEC = importlib.util.spec_from_file_location("activation_lexical_latency", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
harness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = harness
SPEC.loader.exec_module(harness)

from exomem import working_set_runtime  # noqa: E402


def test_percentile_is_nearest_rank() -> None:
    samples = [float(value) for value in range(1, 21)]
    assert harness.percentile(samples, 0.50) == 10.0
    assert harness.percentile(samples, 0.95) == 19.0
    assert harness.percentile([], 0.95) == 0.0


def test_the_harness_reports_both_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("EXOMEM_STATE_ROOT", "EXOMEM_WRITER_LEASE_STATE_DIR", "EXOMEM_DISABLE_EMBEDDINGS"):
        monkeypatch.setenv(key, "")
    report = harness.run(pages=160, turns=3, repeat=1, turn_words=150, seed=4)

    assert report["pages"] == 160
    assert set(report) >= {"unbounded", "bounded"}
    bounded = report["bounded"]
    assert bounded["samples"] == 3
    assert bounded["named_page_first"] == 1.0
    assert 0 < bounded["mean_units_kept"] <= working_set_runtime.ACTIVATION_LEXICAL_MAX_TERMS
    assert bounded["mean_units_dropped"] > 0
    assert report["unbounded"]["mean_units_kept"] is None
