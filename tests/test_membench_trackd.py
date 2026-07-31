"""Track D workflow-journey tests: J1 and J2 run green end-to-end against
fresh isolated vaults, and a deliberate wrong-order J1 variant (final replace
skipped) fails its chain checks — proving the checks bite.

Vaults are per-test ``tmp_path`` children; ``journey_env`` pins
EXOMEM_VAULT_PATH + the lexical/deterministic profile for every subprocess.
"""

from __future__ import annotations

from pathlib import Path

from membench.trackd.journeys import run_j1_longitudinal, run_j2_correction
from membench.trackd.runner import JOURNEYS


def test_j1_longitudinal_evolution_green(tmp_path: Path) -> None:
    result = run_j1_longitudinal(tmp_path / "j1")
    assert result.ok, f"failed checks: {result.failed}\n" + "\n".join(
        f"{c.name}: {c.detail}" for c in result.checks if not c.ok
    )
    assert result.manual_interventions == 0
    # Journey shape: init + 3 writes + evolution + ask + 2 reads = 8 CLI steps.
    assert result.steps_count == 8
    expected_checks = {
        "init vault",
        "remember v1 commits",
        "replace to v2 commits",
        "replace to v3 commits",
        "evolution shows 3-state chain in order",
        "evolution anchors: chain_id=newest, topic_anchor=oldest",
        "ask returns the current (v3-value) page first",
        "top hit carries the current value",
        "superseded v1 remains readable",
        "superseded v2 remains readable",
        "page-count discipline (no duplicate sprawl)",
    }
    assert expected_checks == {c.name for c in result.checks}
    report = result.as_dict()
    assert report["ok"] is True and report["checks_failed"] == []


def test_j2_correction_propagation_green(tmp_path: Path) -> None:
    result = run_j2_correction(tmp_path / "j2")
    assert result.ok, f"failed checks: {result.failed}\n" + "\n".join(
        f"{c.name}: {c.detail}" for c in result.checks if not c.ok
    )
    assert result.manual_interventions == 0
    # init + capture + remember + replace + 5 asks + audit + 2 reads = 12 steps.
    assert result.steps_count == 12
    paraphrase_checks = [c for c in result.checks if c.name.startswith("paraphrase ")]
    assert len(paraphrase_checks) == 5 and all(c.ok for c in paraphrase_checks)
    assert any(
        c.name == "corrected page retains source provenance in sources: frontmatter"
        for c in result.checks
    )


def test_j1_wrong_order_variant_fails_chain_check(tmp_path: Path) -> None:
    """Skipping the v2->v3 replace must break the declared 3-state chain."""
    result = run_j1_longitudinal(tmp_path / "j1-skip", skip_final_replace=True)
    assert not result.ok
    assert "evolution shows 3-state chain in order" in result.failed
    assert "ask returns the current (v3-value) page first" in result.failed
    # The vault still holds only the two written versions (discipline check
    # itself passes for 2 pages — the failure is the missing third state).
    page_check = next(
        c for c in result.checks if c.name == "page-count discipline (no duplicate sprawl)"
    )
    assert page_check.ok, page_check.detail


def test_registry_exposes_both_journeys() -> None:
    assert set(JOURNEYS) == {"j1_longitudinal", "j2_correction"}
