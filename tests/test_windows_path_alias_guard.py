"""Regression coverage for Windows aliases in graph and freshness paths."""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

from exomem import epistemic_graph, freshness, graph_sync, mutation_lock, writer_lease
from exomem import vault as vault_module
from exomem.vault import PlannedWrite


def test_epoch_writes_preserve_the_caller_vault_spelling(
    vault: Path,
) -> None:
    """Internal graph artifacts must share an alias caller's namespace spelling."""
    alias = vault / ".." / vault.name
    note = alias / "Knowledge Base" / "Notes" / "alias-planning.md"
    writes = graph_sync.epoch_writes(alias, (PlannedWrite(note, "# Alias\n"),))

    assert writes is not None
    floor, checkpoint = writes
    assert floor.path.parent == alias / "Knowledge Base"
    assert checkpoint.path.parent == alias / "Knowledge Base"


def test_census_accepts_epoch_artifacts_from_an_alias_root(vault: Path) -> None:
    """A guarded first graph-admitted create retains one namespace for every write."""
    alias = vault / ".." / vault.name
    notes = alias / "Knowledge Base" / "Notes"
    notes.mkdir(exist_ok=True)
    census = vault_module.DirectoryCensusGuard.capture(alias, "Knowledge Base", max_entries=16)
    first = notes / "first.md"

    vault_module.batch_atomic_write(
        (PlannedWrite(first, "# First Note\n"),),
        vault_root=alias,
        required_guards=(census,),
        post_commit_fanout=False,
    )

    assert first.read_text(encoding="utf-8") == "# First Note\n"
    assert graph_sync.floor_path(alias).is_file()
    assert graph_sync.checkpoint_path(alias).is_file()


def test_windows_digest_normalizes_resolved_alias_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest treats equivalent Windows aliases as one path spelling."""

    class WindowsPath:
        def __init__(self, value: str) -> None:
            self.value = value

        def resolve(self) -> WindowsPath:
            return WindowsPath(
                self.value.replace("EXAMPLE~1", "Example Vault").replace(
                    "c" + ":" + "\\" + "users\\example", "C" + ":" + "\\" + "Users\\Example"
                )
            )

        def __str__(self) -> str:
            return self.value

    monkeypatch.setattr(freshness.os, "name", "nt")
    monkeypatch.setattr(freshness, "Path", WindowsPath)
    signature = (1, 2, 3)
    long = "C" + ":" + "\\" + "Users\\Example\\Example Vault\\Notes\\alias.md"
    short = "c" + ":" + "\\" + "users\\example\\EXAMPLE~1\\Notes\\alias.md"

    assert freshness.triple_from_entries(((long, signature),)) == freshness.triple_from_entries(
        ((short, signature),)
    )


def _short_path_name(path: Path) -> Path | None:
    if sys.platform != "win32":
        return None
    get_short = ctypes.windll.kernel32.GetShortPathNameW  # type: ignore[attr-defined]
    get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    result = ctypes.create_unicode_buffer(1024)
    if get_short(str(path), result, len(result)) == 0:
        return None
    return Path(result.value)


@pytest.fixture
def isolated_windows_writer_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Prepare test-only runtime state with production's protected Windows DACL."""
    state_dir = tmp_path / "writer-state"
    owners_dir = state_dir / "idempotency-owners"
    mutation_lock.prepare_windows_idempotency_runtime_paths(state_dir, owners_dir)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state_dir))
    writer_lease.reset_managers_for_tests()
    try:
        yield state_dir
    finally:
        writer_lease.reset_managers_for_tests()


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows 8.3 aliases")
def test_directory_census_allows_a_long_form_write_from_a_short_root_alias(
    vault: Path,
) -> None:
    """The actual 8.3/case split must not reject a graph-admitted write."""
    root = vault.with_name("Exomem Vault Alias Regression")
    vault.rename(root)
    notes = root / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    short_root = _short_path_name(root)
    if short_root is None:
        pytest.skip("8.3 short-name generation is disabled for this volume")
    assert short_root is not None
    if os.path.normcase(str(short_root)) == os.path.normcase(str(root)):
        pytest.skip("8.3 short-name generation is disabled for this volume")

    census = vault_module.DirectoryCensusGuard.capture(short_root, "Knowledge Base", max_entries=16)
    first = short_root / "Knowledge Base" / "Notes" / "first.md"

    vault_module.batch_atomic_write(
        (PlannedWrite(first, "# First Note\n"),),
        vault_root=short_root,
        required_guards=(census,),
        post_commit_fanout=False,
    )

    assert (notes / "first.md").read_text(encoding="utf-8") == "# First Note\n"
    assert graph_sync.floor_path(short_root).is_file()
    assert graph_sync.checkpoint_path(short_root).is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows 8.3 aliases")
def test_recall_projection_identity_survives_short_root_restart(
    vault: Path, isolated_windows_writer_state: Path
) -> None:
    """A published long-root graph remains current after a short-root restart."""
    root = vault.with_name("Exomem Vault Freshness Alias Regression")
    vault.rename(root)
    note = root / "Knowledge Base" / "Notes" / "alias.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Alias\n", encoding="utf-8")
    short_root = _short_path_name(root)
    if short_root is None:
        pytest.skip("8.3 short-name generation is disabled for this volume")
    assert short_root is not None
    if os.path.normcase(str(short_root)) == os.path.normcase(str(root)):
        pytest.skip("8.3 short-name generation is disabled for this volume")

    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )
    long_index = epistemic_graph.EpistemicGraphIndex(root)
    long_index.rebuild_all()
    long_identity = epistemic_graph._incremental_projection_identity(root)
    assert long_index.available() is True

    freshness.invalidate(root)

    short_index = epistemic_graph.EpistemicGraphIndex(short_root)
    assert epistemic_graph._incremental_projection_identity(short_root) == long_identity
    assert short_index.available() is True
