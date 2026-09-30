"""The byte budgets fail at the ceiling and warn in the band below it (`shrink-bootstrap`).

The spec: "the budget test warns before the ceiling and fails at it". A test that
fails inside the warning band is the ceiling moved down without the argument the
ceiling's own docstring demands, so the gate under test here is one function.
"""

from __future__ import annotations

import warnings

import pytest
from budget_gate import check_budget


def test_a_size_over_the_ceiling_fails():
    with pytest.raises(AssertionError, match="over the 1,000 ceiling by 1"):
        check_budget(1_001, ceiling=1_000, band=100, label="x")


def test_a_size_in_the_band_warns_and_does_not_fail():
    with pytest.warns(UserWarning, match="only 50 bytes under the 1,000 ceiling"):
        check_budget(950, ceiling=1_000, band=100, label="x")


def test_a_size_at_the_ceiling_is_the_last_that_passes_and_it_warns():
    with pytest.warns(UserWarning):
        check_budget(1_000, ceiling=1_000, band=100, label="x")


def test_a_size_clear_of_the_band_is_silent():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        check_budget(900, ceiling=1_000, band=100, label="x")
