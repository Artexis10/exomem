"""The write-latency gate must not fail the build on one noisy sample set.

Real incident, 2026-07-27: a contended CI runner produced a 2k baseline that was
*faster* than normal and an 8k measurement that was roughly double normal. The
scaling bound is anchored to the same run's small-corpus median, so both ends
moved the wrong way at once and the gate failed a release PR that had touched
nothing but a version string. A re-run passed with ordinary numbers.

These tests pin that arithmetic (so the sensitivity stays visible rather than
being rediscovered under time pressure) and pin the confirmation behaviour.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "semantic_write_latency.py"


def load_module():
    spec = importlib.util.spec_from_file_location("semantic_write_latency_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def result(pages: int, *, commit: float, validate: float = 30.0) -> dict[str, float | int]:
    return {
        "pages": pages,
        "samples": 5,
        "cold_ms": 1000.0,
        "validate_median_ms": validate,
        "validate_p95_ms": validate * 1.4,
        "commit_median_ms": commit,
        "commit_p95_ms": commit * 1.15,
    }


HEALTHY = [result(2_000, commit=51.5), result(8_000, commit=191.7)]
# The numbers from the 2026-07-27 false failure.
NOISY = [result(2_000, commit=42.3), result(8_000, commit=365.6)]


def test_healthy_scaling_passes() -> None:
    load_module().check(HEALTHY)


def test_absolute_ceiling_breach_fails() -> None:
    module = load_module()
    with pytest.raises(SystemExit, match="commit_median_ms"):
        module.check([result(8_000, commit=module.COMMIT_MEDIAN_MS + 1)])


def test_superlinear_scaling_fails() -> None:
    with pytest.raises(SystemExit, match="commit scaling"):
        load_module().check(NOISY)


def test_a_faster_baseline_tightens_the_bound() -> None:
    """The documented sensitivity: a quick 2k run makes the gate stricter."""

    module = load_module()
    bound = lambda small: small * module.SCALING_RATIO + module.SCALING_SLACK_MS  # noqa: E731

    # The real incident: baseline 42.3ms yielded a 284.6ms bound, while the
    # ordinary 51.5ms baseline would have allowed 303.0ms.
    assert bound(42.3) == pytest.approx(284.6)
    assert bound(51.5) == pytest.approx(303.0)
    assert bound(42.3) < bound(51.5)


def test_check_failure_is_confirmed_by_a_second_measurement(monkeypatch) -> None:
    """A first-attempt failure re-measures; a clean second attempt passes."""

    module = load_module()
    attempts: list[int] = []

    def fake_measure_all(sizes, samples, root):
        attempts.append(len(attempts) + 1)
        return NOISY if len(attempts) == 1 else HEALTHY

    monkeypatch.setattr(module, "measure_all", fake_measure_all)
    assert module.main(["--check"]) == 0
    assert attempts == [1, 2], "a failing first attempt must be re-measured once"


def test_a_reproducible_failure_still_fails(monkeypatch) -> None:
    """Confirmation must not turn a real regression into a pass."""

    module = load_module()
    attempts: list[int] = []

    def fake_measure_all(sizes, samples, root):
        attempts.append(len(attempts) + 1)
        return NOISY

    monkeypatch.setattr(module, "measure_all", fake_measure_all)
    with pytest.raises(SystemExit, match="commit scaling"):
        module.main(["--check"])
    assert attempts == [1, 2], "exactly two attempts before failing the build"


def test_attempts_one_disables_confirmation(monkeypatch) -> None:
    module = load_module()
    attempts: list[int] = []

    def fake_measure_all(sizes, samples, root):
        attempts.append(len(attempts) + 1)
        return NOISY

    monkeypatch.setattr(module, "measure_all", fake_measure_all)
    with pytest.raises(SystemExit, match="commit scaling"):
        module.main(["--check", "--attempts", "1"])
    assert attempts == [1]


def test_no_check_flag_never_fails(monkeypatch) -> None:
    module = load_module()
    monkeypatch.setattr(module, "measure_all", lambda sizes, samples, root: NOISY)
    assert module.main([]) == 0
