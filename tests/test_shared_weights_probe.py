"""Pure accounting checks; never load a model or touch the live runtime."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "shared_weights_probe", Path(__file__).parents[1] / "scripts/shared_weights_probe.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_rollup_uses_kib_and_ignores_headers_and_unrequested_fields():
    values = probe.parse_smaps_rollup(
        "0000-ffff ---p 00000000 00:00 0 [rollup]\n"
        "Rss: 120 kB\nPss: 90 kB\nShared_Clean: 30 kB\n"
        "Shared_Dirty: 0 kB\nPrivate_Clean: 10 kB\nPrivate_Dirty: 80 kB\n"
        "Anonymous: 80 kB\nSwap: 999 kB\n"
    )
    assert values == {
        "Rss": 120, "Pss": 90, "Shared_Clean": 30, "Shared_Dirty": 0,
        "Private_Clean": 10, "Private_Dirty": 80, "Anonymous": 80,
    }


def test_missing_rollup_field_is_not_a_zero_measurement():
    with pytest.raises(ValueError, match="Anonymous"):
        probe.parse_smaps_rollup(
            "Rss: 1 kB\nPss: 1 kB\nShared_Clean: 0 kB\nShared_Dirty: 0 kB\n"
            "Private_Clean: 0 kB\nPrivate_Dirty: 1 kB\n"
        )


def test_marginal_remeasures_all_children_after_pages_become_shared():
    # The first process's PSS falls when the second maps the same weights.
    # Marginal cost is the difference of cohort totals, not the newest child's PSS.
    assert probe.pss_accounting([{"Pss": 650}], None) == {
        "node_total_pss_kib": 650, "marginal_pss_kib": 650,
    }
    assert probe.pss_accounting([{"Pss": 350}, {"Pss": 350}], 650) == {
        "node_total_pss_kib": 700, "marginal_pss_kib": 50,
    }
