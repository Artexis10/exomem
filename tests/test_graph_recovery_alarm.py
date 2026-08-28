"""Persistent graph recovery is an alarmed, bounded condition (tasks 1.3 / 2.2).

The 2026-08 incident ran for days with no alarm. `recovery_required` is
correct for minutes and pathological for hours: while it holds, every batch
write used to mint a durable full-index receipt, so the backlog fed itself.
Two conditions must stop being silent:

* recovery has been required continuously beyond a configured bound; and
* graph work is DISABLED while a durable recovery checkpoint exists -- the
  provably unrecoverable combination, including when the kill switch comes
  from an unowned site-packages `.pth` rather than the process environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import doctor, graph_sync


def _durable_checkpoint(vault: Path) -> graph_sync.GraphSyncCheckpoint:
    """Write a real durable checkpoint the graph has not acknowledged."""
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="a1b2c3d4e5f6a7b8c9d0e1f2",
        paths=(("Knowledge Base/Notes/recovery-alarm.md", "0" * 64),),
        created_paths=("Knowledge Base/Notes/recovery-alarm.md",),
    )
    graph_sync._write_checkpoint(vault, checkpoint)
    return checkpoint


def test_recovery_age_is_measured_and_persists(vault: Path) -> None:
    """The age is durable: a restart must not reset the alarm clock."""
    _durable_checkpoint(vault)
    assert graph_sync.status(vault)["state"] == "recovery_required"

    first = graph_sync.observe_recovery_state(vault)
    assert first is not None and first >= 0.0

    # A second observation must not restart the clock.
    graph_sync.observe_recovery_state(vault)
    assert graph_sync.recovery_age_seconds(vault) is not None


def test_recovery_age_clears_when_recovery_completes(vault: Path) -> None:
    """Returning to `current` clears the marker, so the alarm is not sticky."""
    _durable_checkpoint(vault)
    graph_sync.observe_recovery_state(vault)
    assert graph_sync.recovery_age_seconds(vault) is not None

    graph_sync.checkpoint_path(vault).unlink()
    assert graph_sync.status(vault)["state"] == "current"

    assert graph_sync.observe_recovery_state(vault) is None
    assert graph_sync.recovery_age_seconds(vault) is None


def test_recovery_beyond_the_bound_is_a_doctor_failure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario: recovery outlasting its bound raises a diagnostic FAILURE."""
    _durable_checkpoint(vault)
    graph_sync.observe_recovery_state(vault)
    monkeypatch.setattr(
        graph_sync,
        "recovery_age_seconds",
        lambda _root: doctor.RECOVERY_AGE_FAIL_SECONDS + 1.0,
    )

    check = doctor.check_graph_recovery_age(vault)

    assert check.status == "fail"
    assert "recovery" in check.message.lower()
    assert check.remediation


def test_recovery_within_the_bound_is_not_a_failure(vault: Path) -> None:
    """Counter-scenario: minutes of recovery is normal, not an alarm."""
    _durable_checkpoint(vault)
    graph_sync.observe_recovery_state(vault)

    check = doctor.check_graph_recovery_age(vault)

    assert check.status != "fail"


def test_graph_disabled_with_a_recovery_checkpoint_fails_immediately(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario: the provably unrecoverable combination FAILs at once."""
    _durable_checkpoint(vault)
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")

    check = doctor.check_graph_recovery_age(vault)

    assert check.status == "fail"
    assert "EXOMEM_DISABLE_GRAPH_INDEX" in str(check.details) + check.message


def test_graph_disabled_without_a_checkpoint_is_not_a_failure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter-scenario: the kill switch alone is a deliberate choice, not a fault."""
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    assert graph_sync.read_checkpoint(vault) is None

    check = doctor.check_graph_recovery_age(vault)

    assert check.status != "fail"


def test_pth_injected_kill_switch_is_identified(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disabling SOURCE must be named, including a site-packages `.pth`.

    A stale `.pth` force-disabling the graph is what made recovery impossible
    for days while the environment itself looked clean.
    """
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    (site_packages / "zz-stale-exomem.pth").write_text(
        "import os; os.environ.setdefault('EXOMEM_DISABLE_GRAPH_SCHEDULING', '1')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "_site_package_dirs", lambda: [site_packages])

    sources = doctor.graph_kill_switch_sources()

    assert any("zz-stale-exomem.pth" in str(source) for source in sources)


def test_pth_without_a_kill_switch_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter-scenario: an ordinary `.pth` is not a finding."""
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    (site_packages / "ordinary.pth").write_text("some/other/path\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "_site_package_dirs", lambda: [site_packages])

    assert doctor.graph_kill_switch_sources() == []


def test_readiness_payload_exposes_recovery_age(vault: Path) -> None:
    """Scenario: the readiness payload exposes the recovery age."""
    from exomem import readiness

    _durable_checkpoint(vault)
    graph_sync.observe_recovery_state(vault)

    payload = readiness.graph_recovery_payload(vault)

    assert payload["recovery_required"] is True
    assert isinstance(payload["recovery_age_s"], float)
    assert payload["recovery_age_s"] >= 0.0


def test_the_recovery_check_actually_runs_in_the_doctor(vault: Path) -> None:
    """A check nobody runs is not an alarm. Pin its presence in the real run."""
    report = doctor.doctor(vault=str(vault))
    ids = {check.id for check in report.checks}

    assert "write_path.graph_recovery" in ids


# --- The alarm clock itself must not be silenceable (HIGH) ------------------
#
# A corrupt marker used to read as age 0.0. That is not merely wrong, it is
# self-silencing: `observe_recovery_state` saw 0.0 (not None) and never
# restamped, and doctor's `age > bound` could never fire, so the FAIL was
# permanently downgraded to a WARN. The marker is written during exactly the
# crashes this subsystem exists to monitor, so a torn write is reachable.


def _corrupt_marker(vault: Path) -> Path:
    marker = graph_sync.recovery_marker_path(vault)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"since": ', encoding="utf-8")  # torn write
    return marker


def test_a_corrupt_clock_is_restamped_not_treated_as_zero(vault: Path) -> None:
    """(a) Observation must repair a corrupt clock, not accept it as 'just now'."""
    _durable_checkpoint(vault)
    marker = _corrupt_marker(vault)
    assert graph_sync.recovery_clock_state(vault) == "corrupt"

    graph_sync.observe_recovery_state(vault)

    assert graph_sync.recovery_clock_state(vault) == "valid", (
        "a corrupt recovery clock was left corrupt, so the alarm can never fire"
    )
    assert marker.exists()


def test_a_corrupt_clock_during_recovery_is_a_doctor_failure(vault: Path) -> None:
    """(b) An unreadable clock is its own FAIL, never a 0.0 warn."""
    _durable_checkpoint(vault)
    _corrupt_marker(vault)

    check = doctor.check_graph_recovery_age(vault)

    assert check.status == "fail", "a corrupt recovery clock downgraded FAIL to WARN"
    assert check.remediation


def test_recovery_age_is_none_for_a_corrupt_clock(vault: Path) -> None:
    """`recovery_age_seconds` reports an age only when it has a real one."""
    _durable_checkpoint(vault)
    _corrupt_marker(vault)

    assert graph_sync.recovery_age_seconds(vault) is None
    assert graph_sync.recovery_clock_state(vault) == "corrupt"


def test_observation_preserves_the_age_across_a_restart(vault: Path) -> None:
    """The docstring's central property: a restart must not reset the clock.

    Back-date the marker, then observe again as a restarted process would.
    An implementation that restamps unconditionally zeroes the age here, which
    is precisely how a restart loop keeps an alarm permanently silent.
    """
    import json
    import time

    _durable_checkpoint(vault)
    graph_sync.observe_recovery_state(vault)
    marker = graph_sync.recovery_marker_path(vault)
    backdated = time.time() - 9000.0
    marker.write_text(json.dumps({"since": backdated}), encoding="utf-8")

    age = graph_sync.observe_recovery_state(vault)

    assert age is not None
    assert age > 8000.0, f"observation reset the durable recovery clock (age={age})"


def test_the_recovery_marker_is_reserved_owner_bound_state(vault: Path) -> None:
    """The durable clock is graph_sync state, like its checkpoint siblings."""
    from exomem import reserved_paths
    from exomem.kbdir import kb_dirname

    marker = graph_sync.recovery_marker_path(vault)
    result = reserved_paths.classify_logical(f"{kb_dirname()}/{marker.name}")

    assert result.disposition is reserved_paths.PathDisposition.RESERVED
    assert result.descriptor_id == "graph-handoff"
