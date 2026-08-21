"""Process-cut acceptance coverage for the graph receipt handoff.

The child owns the first idempotency store and dies with ``os._exit`` at a
real protocol boundary.  The parent then opens an independent store object
against the same local runtime and performs the exact retry.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

_DIGEST = "d" * 64


def _run_cut(tmp_path: Path, cut: str) -> tuple[Path, Path, Path, subprocess.CompletedProcess[str]]:
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    marker = tmp_path / "leaf-calls"
    script = dedent(
        """
        import os
        import sqlite3
        import sys
        from pathlib import Path
        from types import SimpleNamespace

        from exomem import graph_sync
        from exomem import vault as vault_module
        from exomem import writer_lease as writer_lease_module
        from exomem.vault import BatchTargetSummary, BatchWriteError, PlannedWrite, batch_atomic_write
        from exomem.writer_lease import LeaseConfig, LeaseManager

        vault, state, marker = map(Path, sys.argv[1:4])
        cut = sys.argv[4]
        note = vault / "Knowledge Base/Notes/cut.md"
        if cut == "after_caller_files":
            original_published = vault_module._after_batch_destination_published
            def exit_after_caller(destination):
                original_published(destination)
                if Path(destination) == note:
                    os._exit(42)
            vault_module._after_batch_destination_published = exit_after_caller
        if cut == "after_checkpoint":
            original_phase = writer_lease_module.log_active_mutation_phase
            def exit_after_checkpoint(phase, **fields):
                original_phase(phase, **fields)
                if phase == "canonical_files_committed":
                    os._exit(43)
            writer_lease_module.log_active_mutation_phase = exit_after_checkpoint
        if cut == "after_receipt":
            original_receipt = graph_sync.write_graph_commit_receipt
            def exit_after_receipt(*args, **kwargs):
                original_receipt(*args, **kwargs)
                os._exit(44)
            graph_sync.write_graph_commit_receipt = exit_after_receipt

        manager = LeaseManager(LeaseConfig(state_dir=state))
        if cut == "committed_cleanup":
            def lose_trusted_state(*_args, **_kwargs):
                with sqlite3.connect(manager.idempotency.path) as connection:
                    connection.execute(
                        "UPDATE mutations SET result = NULL, commit_secret = NULL "
                        "WHERE state = 'canonically_committed'"
                    )
                os._exit(45)
            manager.idempotency._persist_committed_failure = lose_trusted_state

        def leaf(root):
            marker.parent.mkdir(parents=True, exist_ok=True)
            with marker.open("a", encoding="utf-8") as handle:
                handle.write("leaf\\n")
            batch_atomic_write([PlannedWrite(note, "# cut\\n")], vault_root=root)
            if cut == "committed_cleanup":
                raise BatchWriteError(
                    "BATCH_CLEANUP_INCOMPLETE",
                    BatchTargetSummary(1, ("Knowledge Base/Notes/cut.md",), 0),
                    committed=True,
                )
            return {"path": "Knowledge Base/Notes/cut.md", "warnings": []}

        command = SimpleNamespace(name="graph-crash-matrix", read_only=False, leaf=leaf)
        manager.invoke(command, (vault,), {}, idempotency_key="graph-crash-matrix")
        """
    )
    vault.joinpath("Knowledge Base").mkdir(parents=True)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(vault), str(state), str(marker), cut],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
        timeout=30,
    )
    return vault, state, marker, completed


def _retry(vault: Path, state: Path, marker: Path) -> object:
    from types import SimpleNamespace

    from exomem.vault import PlannedWrite, batch_atomic_write
    from exomem.writer_lease import LeaseConfig, LeaseManager

    def leaf(root: Path) -> dict[str, object]:
        with marker.open("a", encoding="utf-8") as handle:
            handle.write("replayed-leaf\n")
        batch_atomic_write(
            [PlannedWrite(root / "Knowledge Base/Notes/cut.md", "# replayed\n")],
            vault_root=root,
        )
        return {"path": "Knowledge Base/Notes/cut.md", "warnings": []}

    return LeaseManager(LeaseConfig(state_dir=state)).invoke(
        SimpleNamespace(name="graph-crash-matrix", read_only=False, leaf=leaf),
        (vault,),
        {},
        idempotency_key="graph-crash-matrix",
    )


def test_subprocess_crash_before_leaf_leaves_executing_claim_outcome_unknown(
    tmp_path: Path,
) -> None:
    """The child dies at the real guard boundary after claim, before leaf entry."""
    from exomem.writer_lease import IdempotencyStore, OpError

    state = tmp_path / "state"
    marker = tmp_path / "leaf-calls"
    script = dedent(
        """
        import os
        import sys
        from pathlib import Path

        from exomem.writer_lease import IdempotencyStore

        class CrashBeforeLeaf:
            def __enter__(self):
                os._exit(41)

            def __exit__(self, *_args):
                raise AssertionError("process should have exited before leaf")

        store = IdempotencyStore(Path(sys.argv[1]) / "idempotency.sqlite")
        store.run(
            "before-leaf",
            "d" * 64,
            lambda: Path(sys.argv[2]).write_text("leaf\\n", encoding="utf-8"),
            operation_guard=CrashBeforeLeaf,
        )
        """
    )
    child = subprocess.run(
        [sys.executable, "-c", script, str(state), str(marker)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
        timeout=30,
    )

    assert child.returncode == 41, child.stderr
    parent = IdempotencyStore(state / "idempotency.sqlite")

    def leaf() -> dict[str, bool]:
        with marker.open("a", encoding="utf-8") as handle:
            handle.write("replayed-leaf\\n")
        return {"executed": True}

    with pytest.raises(OpError) as retry:
        parent.run("before-leaf", _DIGEST, leaf)
    assert retry.value.code == "MUTATION_OUTCOME_UNKNOWN"
    assert not marker.exists()


@pytest.mark.parametrize(
    ("cut", "exit_code"),
    [("after_caller_files", 42), ("after_checkpoint", 43)],
)
def test_ambiguous_subprocess_cuts_are_outcome_unknown_without_leaf_replay(
    tmp_path: Path, cut: str, exit_code: int
) -> None:
    from exomem.writer_lease import OpError

    vault, state, marker, child = _run_cut(tmp_path, cut)

    assert child.returncode == exit_code, child.stderr
    with pytest.raises(OpError) as retry:
        _retry(vault, state, marker)
    assert retry.value.code == "MUTATION_OUTCOME_UNKNOWN"
    assert marker.read_text(encoding="utf-8").splitlines() == ["leaf"]


def test_subprocess_receipt_cut_heals_only_the_matching_local_executing_row(
    tmp_path: Path,
) -> None:
    from exomem import graph_sync

    vault, state, marker, child = _run_cut(tmp_path, "after_receipt")

    assert child.returncode == 44, child.stderr
    result = _retry(vault, state, marker)
    assert isinstance(result, dict)
    assert result["status"] == "committed"
    assert result["graph_sync"] == "completed"
    assert graph_sync.status(vault)["state"] == "current"
    assert marker.read_text(encoding="utf-8").splitlines() == ["leaf"]
    with sqlite3.connect(next(state.glob("idempotency-*.sqlite"))) as connection:
        assert connection.execute("SELECT state FROM mutations").fetchone() == ("completed",)


def test_committed_cleanup_cut_with_lost_trusted_state_is_never_success(
    tmp_path: Path,
) -> None:
    from exomem.writer_lease import OpError

    vault, state, marker, child = _run_cut(tmp_path, "committed_cleanup")

    assert child.returncode == 45, child.stderr
    with pytest.raises(OpError) as retry:
        _retry(vault, state, marker)
    assert retry.value.code == "MUTATION_OUTCOME_UNKNOWN"
    assert marker.read_text(encoding="utf-8").splitlines() == ["leaf"]
