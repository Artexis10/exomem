"""Attribution + triage hardening for the real-state-root guard (packet G).

`_isolate_state_root` in conftest.py already asserts, correctly, that nothing
in a test run touched the machine's real platform state root. What it did not
do -- until this lane -- is say WHAT changed, or note that the failure is at
least as often cross-process interference (another worktree's pytest, a stray
``-x`` run, a foreign tmpdir touching this same machine-global root) as it is
the diff under test. Four incidents on 2026-08-29 alone hit exactly this: an
unrelated test ERRORs with "a test touched the real user state root" on files
the diff never touched, and the message printed raw snapshot tuples a triager
could not act on.

These tests drive the actual fixture generator directly through its
documented seam (``XDG_STATE_HOME``, mirroring how conftest.py itself derives
the real root and how ``tests/test_state_root_placement.py`` already
monkeypatches it) rather than reimplementing its comparison logic, so a red
run here is truthful about what the shipped fixture actually says -- and a
green run proves the enrichment without moving WHEN the assert fires. The
assert condition itself (``after == before``) must never change; every test
below aims at the *message*, never at weakening the comparison.

Correction round (independent review, same day): the first pass leaked raw
argv (including, live, an ssh private-key path) into the failure message, and
its "concurrent candidates" scan matched the run's own ancestor chain
unconditionally (CI always runs pytest under a wrapper, so that chain always
matches "pytest"), which would have misattributed genuine diff failures to
interference on every single run. Two mutation-proven gaps were also found: a
`tmp*`-prefix allowlist and a lying drift summary (naming a file as both
added and removed) both passed the original six tests. The tests below add
coverage for all four; see `test_concurrent_scan_excludes_its_own_ancestor_chain_but_not_a_genuine_third_party`
and the parametrization / negative asserts on the added- and removed-entry
tests.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import (
    _candidate_exclusion_pids,
    _concurrent_process_candidates,
    _isolate_state_root,
    _matching_processes,
    _process_ancestor_pids,
)

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason=(
        "drives the POSIX XDG_STATE_HOME branch of "
        "state_paths.platform_default_state_root(), and the /proc-based "
        "concurrent-process scan; Windows has neither"
    ),
)


def _wrapped_fixture_function(fixture):
    """The raw function a `@pytest.fixture` wraps.

    `_get_wrapped_function()` is pytest 9.0.3's accessor for this (verified
    against `_pytest.fixtures.FixtureFunctionDefinition`); `__wrapped__` is
    kept as a fallback for other pytest versions this suite might run under.
    """
    getter = getattr(fixture, "_get_wrapped_function", None)
    if getter is not None:
        return getter()
    return fixture.__wrapped__


def _drive_guard(inner_tmp_path: Path):
    """Start one raw instance of the real fixture body outside pytest's own
    fixture machinery, wrapped in `contextlib.closing`.

    Fixture-teardown assertions are otherwise invisible to the test they
    wrap -- `_isolate_state_root` is autouse, so the only way to inspect its
    own failure is to run its generator by hand. `inner_tmp_path` stands in
    for the `tmp_path` pytest would normally inject; it must be a distinct
    directory from the *outer* test's own `tmp_path` (used to control
    `XDG_STATE_HOME` below) so this inner run's injected `EXOMEM_STATE_ROOT`
    never collides with the outer autouse guard already wrapping this very
    test.

    `contextlib.closing` matters here: if a test raised between getting this
    generator and calling `_finish` on it (its own manipulation of the fake
    real root failing, say), the generator would otherwise sit suspended,
    holding the fixture's private `EXOMEM_STATE_ROOT` monkeypatch open until
    the GC eventually collects it. `with _drive_guard(...) as gen:` calls
    `gen.close()` deterministically at block exit instead, which resumes the
    fixture at its `yield` with `GeneratorExit` and runs its own
    `finally: private.undo()` immediately. Calling `.close()` on an already
    -exhausted generator (the normal path, via `_finish`) is a safe no-op.
    """
    inner_tmp_path.mkdir(parents=True, exist_ok=True)
    raw = _wrapped_fixture_function(_isolate_state_root)
    gen = raw(inner_tmp_path)
    next(gen)  # run setup through `yield`: captures the "before" snapshot
    return contextlib.closing(gen)


def _finish(gen) -> str | None:
    """Drive the generator past its `yield`.

    Returns the guard's AssertionError message if it fired, or None if the
    generator completed cleanly (no drift detected).
    """
    try:
        next(gen)
    except StopIteration:
        return None
    except AssertionError as exc:
        return str(exc)
    raise AssertionError(
        "guard generator yielded a second time; _isolate_state_root's shape "
        "changed and this harness no longer matches it"
    )


def _point_real_root_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect `platform_default_state_root()` at a private tmp directory
    for this test only, via the same `XDG_STATE_HOME` seam
    `test_state_root_placement.py` already uses. Returns the resulting real
    root (not yet created)."""
    xdg_base = tmp_path / "xdg-home"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_base))
    return xdg_base / "exomem" / "state"


def _drift_clause(message: str) -> str:
    """The `(...)` drift-summary clause right after "real user state root
    <path> " in the guard message.

    The raw snapshot tuples at the end of the message legitimately mention
    every present filename, both unchanged and changed -- an untouched
    sibling file's name is expected to appear there. Negative asserts about
    "this name must not be listed as added/removed" must scope to the
    drift-summary clause only, or they would false-fail against a correct
    implementation.
    """
    return message.split("(", 1)[1].split(")", 1)[0]


def test_guard_passes_when_nothing_changed(tmp_path, monkeypatch):
    real_root = _point_real_root_at(monkeypatch, tmp_path)
    real_root.mkdir(parents=True)
    (real_root / "steady.txt").write_text("baseline")

    with _drive_guard(tmp_path / "inner-untouched") as gen:
        message = _finish(gen)

    assert message is None


@pytest.mark.parametrize(
    "intruder_name",
    [
        "intruder-471fb58e.txt",
        # The real 2026-08-29 incidents were named exactly like this
        # (pytest's own tmp_path_factory prefix). A mutation that ignored
        # `tmp*`-prefixed intruders (plausible-looking as "probably a
        # pytest tmpdir, not a real change") would still pass every other
        # test here while silently blinding the guard to the actual
        # incident shape -- this parametrization closes that gap.
        "tmp0cx8c0i9-abc",
    ],
)
def test_guard_names_added_entry_and_carries_triage_hint(tmp_path, monkeypatch, intruder_name):
    real_root = _point_real_root_at(monkeypatch, tmp_path)
    real_root.mkdir(parents=True)
    (real_root / "steady.txt").write_text("baseline")

    with _drive_guard(tmp_path / "inner-added") as gen:
        # Simulate exactly the cross-process interference the packet
        # describes: something else writes into the real root mid-test.
        (real_root / intruder_name).write_text("from another process")
        message = _finish(gen)

    assert message is not None, "guard did not fire on an added entry"
    drift_clause = _drift_clause(message)
    assert f"added: {intruder_name}" in drift_clause, message
    # A lying drift summary that names every child as both added and removed
    # would still satisfy the assertion above -- pin that the untouched
    # sibling is not swept into the drift clause at all.
    assert "steady.txt" not in drift_clause, message
    assert "concurrent" in message.lower(), message
    assert "rerun" in message.lower(), message


def test_guard_names_removed_entry(tmp_path, monkeypatch):
    real_root = _point_real_root_at(monkeypatch, tmp_path)
    real_root.mkdir(parents=True)
    (real_root / "keep.txt").write_text("baseline")
    (real_root / "gone.txt").write_text("baseline")

    with _drive_guard(tmp_path / "inner-removed") as gen:
        (real_root / "gone.txt").unlink()
        message = _finish(gen)

    assert message is not None, "guard did not fire on a removed entry"
    drift_clause = _drift_clause(message)
    assert "removed: gone.txt" in drift_clause, message
    assert "keep.txt" not in drift_clause, message


def test_guard_names_mtime_shifted_entry(tmp_path, monkeypatch):
    real_root = _point_real_root_at(monkeypatch, tmp_path)
    real_root.mkdir(parents=True)
    (real_root / "sibling.txt").write_text("baseline")
    target = real_root / "vault-shifted.txt"
    target.write_text("baseline")

    with _drive_guard(tmp_path / "inner-mtime") as gen:
        shifted = target.stat().st_mtime + 120
        os.utime(target, (shifted, shifted))
        message = _finish(gen)

    assert message is not None, "guard did not fire on an mtime-only shift"
    drift_clause = _drift_clause(message)
    assert "changed: vault-shifted.txt" in drift_clause, message
    assert "sibling.txt" not in drift_clause, message


def test_guard_never_omits_the_concurrent_candidates_line(tmp_path, monkeypatch):
    """The 'concurrent candidates' line must appear even when the best-effort
    /proc scan finds nothing to report -- never silently omitted. This does
    not assert the scan is empty: other processes are routinely running
    concurrently on this box (that is the whole premise of packet G), so the
    only safe assertion is that the label itself is always present.
    """
    real_root = _point_real_root_at(monkeypatch, tmp_path)
    real_root.mkdir(parents=True)

    with _drive_guard(tmp_path / "inner-candidates") as gen:
        (real_root / "intruder.txt").write_text("x")
        message = _finish(gen)

    assert message is not None
    assert "concurrent candidates:" in message, message


def test_guard_keeps_raw_snapshot_tuples_available(tmp_path, monkeypatch):
    """Raw tuples stay in the message -- secondary to the legible attribution,
    never removed (packet requirement: 'Keep raw tuples available but
    secondary')."""
    real_root = _point_real_root_at(monkeypatch, tmp_path)
    real_root.mkdir(parents=True)

    with _drive_guard(tmp_path / "inner-raw") as gen:
        (real_root / "intruder.txt").write_text("x")
        message = _finish(gen)

    assert message is not None
    assert "before=" in message, message
    assert "after=" in message, message


def test_guard_never_prints_raw_argv_in_candidates(tmp_path, monkeypatch):
    """Redaction: a candidate line names the pid, the basename of argv[0], and
    a best-effort cwd -- never the raw argv, which can carry secrets (an ssh
    private-key path was captured live in review off an earlier, unredacted
    render). This is a real-box smoke check, not synthetic: it runs the
    actual scan and pins the *shape* of whatever it finds rather than
    asserting specific content, since this dev box always has several real
    concurrent exomem/pytest processes to find.
    """
    candidates = _concurrent_process_candidates()
    for line in candidates:
        if line.startswith("(+") and line.endswith(" more)"):
            continue  # the capped-overflow summary line, not a process line
        assert line.startswith("pid "), line
        assert "matched: pytest|exomem" in line, line
        assert "cwd: " in line, line
        # A raw cmdline would routinely contain "/", multiple words, and
        # flags; the basename slot must not carry a full path.
        basename_slot = line.split(": ", 1)[1].split(" (matched:", 1)[0]
        assert "/" not in basename_slot, (
            f"candidate line leaks a path instead of a basename: {line!r}"
        )


def test_concurrent_scan_excludes_its_own_ancestor_chain_but_not_a_genuine_third_party():
    """CI always runs pytest under a wrapper (`uv run ... python -m
    pytest`), so the scan's own ancestor chain would otherwise match
    "pytest"/"exomem" on every single run, making "concurrent candidates"
    permanently non-empty and actively misleading a triager into blaming
    interference for a genuine diff failure -- reproduced live in review.

    Proven two ways:

    1. none of this process's real ancestor pids (nor itself) survive the
       exclusion in the UNCAPPED `_matching_processes` scan. Checked against
       the uncapped scan deliberately: on a busy box the cap can hide an
       ancestor that sits late in `/proc` readdir order, so absence from the
       capped render proves nothing. A precondition assert (this very pytest
       process is visible to an unexcluded scan) keeps the check from going
       vacuous on a quiet host.
    2. a genuine third party -- this test's own CHILD process, never an
       ancestor, with "pytest" in its argv[0] via the `sleep` argv-override
       trick -- is still found by the underlying scan. Checked against the
       uncapped `_matching_processes` rather than the public capped
       function: this box routinely runs several concurrent exomem/pytest
       sessions and `/proc` readdir order is not guaranteed, so the cap
       could cut the marker before it through no fault of the exclusion
       logic under test.

    The marker is always reaped in `finally`, bounded to a 300s `sleep` that
    is killed almost immediately.
    """
    own_pid = os.getpid()
    ancestors = _process_ancestor_pids(own_pid)
    assert ancestors, "expected at least one live ancestor pid for this test process"

    excluded = ancestors | {own_pid}
    assert _candidate_exclusion_pids() >= excluded, (
        "the scan's own exclusion set must contain this process and its full "
        "ancestor chain; a weaker set re-reports the run's own wrapper as "
        "concurrent interference"
    )
    unexcluded_pids = {pid for pid, _cmdline in _matching_processes(frozenset())}
    assert own_pid in unexcluded_pids, (
        "precondition: an unexcluded scan must see this very pytest process, "
        "or the exclusion assertion below has no content on this host"
    )
    excluded_scan_pids = {pid for pid, _cmdline in _matching_processes(excluded)}
    leaked = sorted(excluded_scan_pids & excluded)
    assert not leaked, f"self/ancestor pids leaked past the exclusion: {leaked}"

    marker = subprocess.Popen(
        ["fake-pytest-marker-should-be-reported", "300"],
        executable="/bin/sleep",
    )
    try:
        matches = _matching_processes(ancestors | {own_pid})
        matched_pids = {pid for pid, _cmdline in matches}
        assert marker.pid in matched_pids, (
            f"genuine third-party marker process (pid {marker.pid}) was not "
            f"reported; matched pids only (argv withheld): {sorted(matched_pids)!r}"
        )
    finally:
        marker.kill()
        marker.wait(timeout=5)
