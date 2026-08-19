"""The tool scratch root is reclaimed, and says so when it cannot be.

These tools each build a hundreds-of-megabytes tree under the system temp
directory and used to remove it with cleanup errors suppressed. Suppression
never retries and never reports, so a transient Windows handle and a real leak
looked identical -- and both simply stayed. A laptop reached 58 GB of temp made
entirely of these (#579).

What follows pins the three behaviours that replace suppression: a removal that
outlasts a transient failure, a removal that takes everything it can when one
entry is held for good, and a sweep that reclaims what a killed run could never
have removed itself.
"""

from __future__ import annotations

import importlib.util
import os
import time
import uuid
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "scratch_root.py"
SPEC = importlib.util.spec_from_file_location("scratch_root_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
scratch_root = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scratch_root)


def _always_busy(path, *args, **kwargs):
    raise PermissionError(32, "The process cannot access the file")


def _tree(path: Path) -> Path:
    (path / "nested").mkdir(parents=True)
    (path / "nested" / "page.md").write_text("body", encoding="utf-8")
    return path


def test_a_transient_failure_is_retried_rather_than_suppressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The case suppression was chosen for, now actually handled.

    A SQLite or child-process handle released moments ago loses to a short
    retry. `ignore_errors=True` never gave it that chance -- it returned on
    the first error, leaving the tree.
    """
    target = _tree(tmp_path / "scratch")
    real_rmtree = scratch_root.shutil.rmtree
    attempts: list[int] = []

    def flaky(path, *args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(32, "The process cannot access the file")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(scratch_root.shutil, "rmtree", flaky)

    assert scratch_root.remove_scratch_tree(target) is True
    assert not target.exists()
    assert len(attempts) == 3
    assert capsys.readouterr().out == ""


def test_an_rmtree_that_never_succeeds_still_loses_to_the_per_entry_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`rmtree` failing is not the same as the tree being unremovable.

    It abandons its whole walk at the first refusal, so a single awkward entry
    -- or a race it lost -- makes it report failure for a tree that would come
    apart entry by entry. Falling back to that pass is what turns most
    "permanent" failures into no failure at all, and it is why nothing is
    printed here: there is nothing left to name.
    """
    target = _tree(tmp_path / "scratch")

    monkeypatch.setattr(scratch_root.shutil, "rmtree", _always_busy)

    assert scratch_root.remove_scratch_tree(target, deadline=0.05) is True
    assert not target.exists()
    assert capsys.readouterr().out == ""


def test_a_single_held_entry_does_not_strand_the_tree_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The megabytes go even when the lock file cannot.

    `shutil.rmtree` abandons the whole walk at its first refusal, so one held
    handle used to strand everything beside it -- and the ratio is brutal:
    `writer_lease`'s owner lock is a few hundred bytes and is held for the
    life of the process by design, while the vault next to it is hundreds of
    megabytes. Remove what will go, and name only what stayed.
    """
    target = _tree(tmp_path / "scratch")
    held = target / "writer-lease" / "owner.lock"
    held.parent.mkdir(parents=True)
    held.write_bytes(b"locked")
    real_unlink = Path.unlink

    def refuse_the_lock(self, *args, **kwargs):
        if self.name == "owner.lock":
            raise PermissionError(32, "The process cannot access the file")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(scratch_root.shutil, "rmtree", _always_busy)
    monkeypatch.setattr(Path, "unlink", refuse_the_lock)

    assert scratch_root.remove_scratch_tree(target, deadline=0.01) is False

    assert held.exists()
    assert not (target / "nested").exists()
    out = capsys.readouterr().out
    assert str(held) in out


def test_the_root_is_removed_even_when_the_run_raises(tmp_path: Path) -> None:
    """Cleanup is not conditional on success; a failed run leaks the most."""
    monkeypatched_prefix = f"exomem-scratch-test-{uuid.uuid4().hex}-"
    seen: list[Path] = []

    with pytest.raises(RuntimeError, match="measurement failed"):
        with scratch_root.scratch_root(monkeypatched_prefix) as path:
            seen.append(path)
            _tree(path / "vault")
            raise RuntimeError("measurement failed")

    assert seen and not seen[0].exists()


def test_keep_retains_the_root_and_says_where_it_is(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A retained tree must be a visible decision, not an invisible leak."""
    prefix = f"exomem-scratch-test-{uuid.uuid4().hex}-"
    with scratch_root.scratch_root(prefix, keep=True) as path:
        _tree(path / "vault")
    try:
        assert path.exists()
        assert str(path) in capsys.readouterr().out
    finally:
        scratch_root.remove_scratch_tree(path)


def test_the_sweep_reclaims_an_abandoned_root_and_spares_a_live_one() -> None:
    """The half a retry cannot cover: a run that was killed.

    Ctrl-C and a cancelled CI job leave a root no code of that run will ever
    remove, and that is exactly the case that accumulates. The next run
    reclaims it -- but only when age proves it cannot belong to a run still in
    progress, because deleting a concurrent run's tree would be far worse than
    leaving bytes on disk.

    The prefix is unique to this test, so the sweep it exercises can only ever
    see directories this test made.
    """
    prefix = f"exomem-scratch-test-{uuid.uuid4().hex}-"
    temp = Path(scratch_root.tempfile.gettempdir())
    abandoned = _tree(temp / f"{prefix}abandoned")
    live = _tree(temp / f"{prefix}live")
    stale = time.time() - (scratch_root.STALE_AGE_SECONDS + 60)
    os.utime(abandoned, (stale, stale))

    try:
        swept = scratch_root.sweep_stale_scratch_roots(prefix)

        assert swept == [abandoned]
        assert not abandoned.exists()
        assert live.exists()
    finally:
        scratch_root.remove_scratch_tree(abandoned)
        scratch_root.remove_scratch_tree(live)


def test_a_fresh_root_is_not_swept_by_its_own_context_manager() -> None:
    """The sweep runs on the way in, so it must never see the root it precedes."""
    prefix = f"exomem-scratch-test-{uuid.uuid4().hex}-"
    with scratch_root.scratch_root(prefix) as path:
        assert path.exists()
        assert scratch_root.sweep_stale_scratch_roots(prefix) == []
        assert path.exists()
