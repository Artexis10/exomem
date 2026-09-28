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


def test_shared_rare_words_sit_on_other_anchors_never_together(tmp_path: Path) -> None:
    """`shared` spreads each rare word to other anchors, but only the named
    page holds both of its words, so the case still has one right answer."""
    paths, named = harness.build_vault(tmp_path, 80, seed=4, shared=2)
    words = {rel: set((tmp_path / rel).read_text(encoding="utf-8").split()) for rel in paths}

    def holders(word: str) -> set[str]:
        return {rel for rel, held in words.items() if word in held}

    for rel, (first, second) in named.items():
        assert holders(first) & holders(second) == {rel}
        assert len(holders(first)) <= 3 and len(holders(second)) <= 3
    assert max(len(holders(first)) for first, _second in named.values()) == 3


@pytest.mark.parametrize(("script", "shared"), [("latin", 3), ("japanese", 0)])
def test_the_harness_reports_its_harder_cases(
    monkeypatch: pytest.MonkeyPatch, script: str, shared: int
) -> None:
    """Rare words shared with other anchors, and a turn of long unspaced runs:
    both run end to end and the bounded shape keeps the unit budget. How often
    the named page comes first is what the case measures, so only the
    Japanese case, whose names are unique, pins it."""
    for key in ("EXOMEM_STATE_ROOT", "EXOMEM_WRITER_LEASE_STATE_DIR", "EXOMEM_DISABLE_EMBEDDINGS"):
        monkeypatch.setenv(key, "")
    report = harness.run(
        pages=160, turns=3, repeat=1, turn_words=150, seed=4, script=script, shared=shared
    )

    assert (report["script"], report["shared"]) == (script, shared)
    bounded = report["bounded"]
    assert bounded["samples"] == 3
    assert 0.0 <= bounded["named_page_first"] <= 1.0
    assert 0 < bounded["mean_units_kept"] <= working_set_runtime.ACTIVATION_LEXICAL_MAX_TERMS
    if script == "japanese":
        assert bounded["named_page_first"] == 1.0


def test_the_harness_times_a_short_common_turn_warm_and_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twelve head words and no name: the turn the budget keeps whole. Both
    shapes run, nothing is named, and the bounded shape also reports a p95
    with its term-frequency cache emptied before every turn."""
    for key in ("EXOMEM_STATE_ROOT", "EXOMEM_WRITER_LEASE_STATE_DIR", "EXOMEM_DISABLE_EMBEDDINGS"):
        monkeypatch.setenv(key, "")
    report = harness.run(pages=160, turns=3, repeat=1, turn_words=12, seed=4, common=True)

    assert report["common_turn"] is True
    bounded = report["bounded"]
    assert bounded["samples"] == 3
    assert bounded["named_page_first"] is None
    assert bounded["cold_p95_ms"] is not None and bounded["cold_p95_ms"] > 0
    assert bounded["mean_units_kept"] == working_set_runtime.ACTIVATION_LEXICAL_MAX_TERMS
    assert report["unbounded"]["cold_p95_ms"] is None
