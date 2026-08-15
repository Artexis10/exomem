"""The stable-identity census walk must survive a directory that is changing.

Issue #528. `_build_identity_census` stat'd EVERY entry `os.scandir` returned
and only applied the `.md` filter afterwards. A vault's Knowledge Base root
also holds SQLite's transient sidecars — `.embeddings.sqlite-shm` and friends,
created and deleted as embedding connections open and close — so the walk was
routinely statting files it had no interest in. When one of them vanished in
the window between the listing and the stat, the `FileNotFoundError` was
escalated into `IDENTITY_CENSUS_ENTRY_UNREADABLE` and killed whatever was
building the corpus context. It failed CI's py3.11 shard 3/4 twice in a row
through `reconcile -> evaluate_posthoc_batch -> build_corpus_context -> walk`
while py3.13's identical shard passed — the signature of a race, not a defect
in the thing being measured.

The census is a snapshot, not a lock. A file that no longer exists is simply
not in it; change detection belongs to the freshness machinery. What must NOT
be relaxed is refusal on real unreadability, or on filesystem aliases.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem import activation_manifest, semantic_contract

KB = "Knowledge Base"
PAGE_REL = f"{KB}/Notes/Insights/page.md"
GHOST_REL = f"{KB}/Notes/Insights/ghost.md"
# The exact sidecar from the incident: SQLite drops it when the last
# connection to the embeddings database closes.
SIDECAR = ".embeddings.sqlite-shm"


def _page(title: str) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: insight\n"
        "status: active\n"
        "---\n\n"
        f"# {title}\n\nProse.\n"
    )


def _seed(root: Path, *extra_pages: str) -> Path:
    path = root / PAGE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_page("Page"), encoding="utf-8")
    for rel in extra_pages:
        extra = root / rel
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text(_page(Path(rel).stem), encoding="utf-8")
    return path


class _RacingEntry:
    """A directory entry whose target changes between listing and stat.

    Stands in for the real race: `os.scandir` already returned the name, and
    by the time anyone asks the filesystem about it the answer has changed.
    Records every `stat()` it receives so a test can assert the walk did not
    ask at all.
    """

    def __init__(self, entry: os.DirEntry, stat_calls: list[str], error: OSError) -> None:
        self.name = entry.name
        self.path = entry.path
        self._stat_calls = stat_calls
        self._error = error

    def is_symlink(self) -> bool:
        return False

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return False

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return False

    def stat(self, *, follow_symlinks: bool = True):
        self._stat_calls.append(self.name)
        raise self._error


class _AliasEntry:
    """An entry that reports itself as a filesystem alias."""

    def __init__(self, entry: os.DirEntry, stat_calls: list[str]) -> None:
        self.name = entry.name
        self.path = entry.path
        self._stat_calls = stat_calls

    def is_symlink(self) -> bool:
        return True

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return False

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return False

    def stat(self, *, follow_symlinks: bool = True):
        self._stat_calls.append(self.name)
        return os.stat(self.path)


def _substitute(monkeypatch, name: str, factory) -> list[str]:
    """Swap the scandir entry called `name` for one built by `factory`."""
    stat_calls: list[str] = []
    real_scandir = os.scandir

    def fake_scandir(directory):
        entries = list(real_scandir(directory))
        return [
            factory(entry, stat_calls) if entry.name == name else entry for entry in entries
        ]

    monkeypatch.setattr(semantic_contract.os, "scandir", fake_scandir)
    return stat_calls


def test_a_transient_sidecar_is_never_stat_ed(tmp_path: Path, monkeypatch) -> None:
    """The reported bug. A non-Markdown runtime sidecar is not census input,
    so the walk must filter it out BEFORE asking the filesystem about it —
    otherwise the race window exists at all."""
    _seed(tmp_path)
    (tmp_path / KB / SIDECAR).write_bytes(b"")
    stat_calls = _substitute(
        monkeypatch,
        SIDECAR,
        lambda entry, calls: _RacingEntry(
            entry, calls, FileNotFoundError(2, "No such file or directory", entry.path)
        ),
    )

    census = semantic_contract.build_stable_identity_census(tmp_path)

    assert stat_calls == [], f"the walk stat'd a non-Markdown sidecar: {stat_calls}"
    assert PAGE_REL in {entry.path for entry in census.entries}


def test_a_markdown_page_deleted_mid_walk_is_skipped(tmp_path: Path, monkeypatch) -> None:
    """The residual race on files the census DOES care about. A page deleted
    between the listing and the stat is absent from the snapshot, not fatal to
    it."""
    _seed(tmp_path, GHOST_REL)
    stat_calls = _substitute(
        monkeypatch,
        "ghost.md",
        lambda entry, calls: _RacingEntry(
            entry, calls, FileNotFoundError(2, "No such file or directory", entry.path)
        ),
    )

    census = semantic_contract.build_stable_identity_census(tmp_path)

    # It was a `.md`, so the walk legitimately asked — and tolerated the answer.
    assert stat_calls == ["ghost.md"]
    paths = {entry.path for entry in census.entries}
    assert PAGE_REL in paths
    assert GHOST_REL not in paths


def test_an_unreadable_markdown_page_still_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """Only deletion is tolerated. Real unreadability must still refuse to
    vouch for the tree."""
    _seed(tmp_path, GHOST_REL)
    _substitute(
        monkeypatch,
        "ghost.md",
        lambda entry, calls: _RacingEntry(
            entry, calls, PermissionError(13, "Permission denied", entry.path)
        ),
    )

    with pytest.raises(activation_manifest.ActivationManifestError) as raised:
        semantic_contract.build_stable_identity_census(tmp_path)

    assert raised.value.code == "IDENTITY_CENSUS_ENTRY_UNREADABLE"


def test_a_non_markdown_alias_still_refuses_the_census(tmp_path: Path, monkeypatch) -> None:
    """Filtering before the stat must not open an alias-refusal hole.

    An aliased entry whose name is not `.md` — a symlinked subdirectory, say —
    can hide or duplicate a whole subtree. It is exactly the case the census
    refuses to vouch for, and skipping it silently would be worse than the
    race this change fixes.
    """
    _seed(tmp_path)
    (tmp_path / KB / "aliased-subtree").mkdir()
    _substitute(monkeypatch, "aliased-subtree", _AliasEntry)

    with pytest.raises(activation_manifest.ActivationManifestError) as raised:
        semantic_contract.build_stable_identity_census(tmp_path)

    assert raised.value.code == "IDENTITY_CENSUS_UNSAFE_ENTRY"
