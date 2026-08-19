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


def _fail_read_of(monkeypatch, name: str, error: OSError) -> list[str]:
    """Make `read_text` raise for one page, after its stat has already passed."""
    reads: list[str] = []
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):  # noqa: ANN001, ANN202
        if self.name == name:
            reads.append(self.name)
            raise error
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    return reads


def test_a_markdown_page_deleted_between_the_stat_and_the_read_is_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    """The residual half of the same race, one call later.

    The stat window has tolerated this since #528. Leaving the read window
    fail-closed made the census's verdict depend on which microsecond the
    deletion landed in -- absent from the snapshot if it beat the stat, a hard
    `IDENTITY_CENSUS_PAGE_UNREADABLE` if it lost by a hair. That distinction
    describes the timing, not the vault.
    """
    _seed(tmp_path, GHOST_REL)
    reads = _fail_read_of(
        monkeypatch,
        "ghost.md",
        FileNotFoundError(2, "No such file or directory", "ghost.md"),
    )

    census = semantic_contract.build_stable_identity_census(tmp_path)

    # The page survived the stat, so the read was genuinely attempted.
    assert reads == ["ghost.md"]
    paths = {entry.path for entry in census.entries}
    assert PAGE_REL in paths
    assert GHOST_REL not in paths


def test_an_unreadable_page_still_fails_closed_at_the_read(
    tmp_path: Path, monkeypatch
) -> None:
    """Only absence is tolerated, at the read exactly as at the stat.

    A page that is present and cannot be read is a different fact from one that
    is gone: it says the tree holds something this census cannot vouch for, and
    refusing is the whole point of a fail-closed census.
    """
    _seed(tmp_path, GHOST_REL)
    _fail_read_of(
        monkeypatch,
        "ghost.md",
        PermissionError(13, "Permission denied", "ghost.md"),
    )

    with pytest.raises(activation_manifest.ActivationManifestError) as raised:
        semantic_contract.build_stable_identity_census(tmp_path)

    assert raised.value.code in {
        "IDENTITY_CENSUS_PAGE_UNREADABLE",
        "ACTIVATION_MANIFEST_PAGE_UNREADABLE",
    }


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


# ---------------------------------------------------------------------------
# Issue #545. `_build_identity_census` walked `Knowledge Base/_trash/` like
# any canonical directory: `_IDENTITY_CENSUS_RESERVED_KB_DIRS` reserved only
# `.graph-commit-receipts`, so a malformed page trashed by `delete_file`
# escalated `vault.parse_frontmatter(strict=True)`'s failure into a
# census-fatal `ActivationManifestError` — blocking every governed write on
# the vault, not just recall of the deleted page. Trash is exempt,
# non-canonical content per the semantic-authoring contract (mirrors
# `_SEMANTIC_UNIT_EXEMPT_PARTS`'s "_trash"/"trash" pair): a deleted page's
# frontmatter must never be able to block a live write. Fixed by pruning
# `_trash`/`trash` at the KB root — deliberately NOT a general
# tolerate-unparseable-pages path, which would also mask a real defect in a
# canonical directory.
# ---------------------------------------------------------------------------

TRASH_BAD_REL = f"{KB}/_trash/2026-08-15/bad.md"
CANONICAL_BAD_REL = f"{KB}/Notes/Insights/bad.md"
TRASH_GOOD_REL = f"{KB}/_trash/2026-08-15/good.md"
# The exact defect class from the incident: an unquoted scalar value that
# itself contains a "key: value"-shaped substring collides with YAML's block
# mapping indicator and raises `ScannerError`, which `parse_frontmatter`
# reports as `INVALID_FRONTMATTER` — verbatim the code the live incident
# raised (`could not safely inspect stable identity at Knowledge
# Base/_trash/2026-08-15/145201-...md`).
_MALFORMED_FRONTMATTER = (
    "---\n"
    "title: bad\n"
    "type: insight\n"
    "status: active\n"
    "label: PR 512 - readytags: [a, b]\n"
    "---\n\n"
    "Prose.\n"
)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_malformed_trash_page_no_longer_blocks_the_identity_census(tmp_path: Path) -> None:
    """The reported incident. A page trashed by `delete_file` with frontmatter
    that fails strict YAML must not be able to block every governed write."""
    _seed(tmp_path)
    _write(tmp_path, TRASH_BAD_REL, _MALFORMED_FRONTMATTER)

    census = semantic_contract.build_stable_identity_census(tmp_path)

    paths = {entry.path for entry in census.entries}
    assert PAGE_REL in paths
    assert TRASH_BAD_REL not in paths


def test_a_malformed_canonical_page_still_fails_closed(tmp_path: Path) -> None:
    """Companion guard: the fix is a KB-root prune of trash, not a general
    tolerate-unparseable-pages path. A malformed page in a CANONICAL
    directory must still refuse to vouch for the tree."""
    _seed(tmp_path)
    _write(tmp_path, CANONICAL_BAD_REL, _MALFORMED_FRONTMATTER)

    with pytest.raises(activation_manifest.ActivationManifestError) as raised:
        semantic_contract.build_stable_identity_census(tmp_path)

    assert raised.value.code == "INVALID_FRONTMATTER"


def test_a_well_formed_trash_page_is_absent_from_the_census_either_way(tmp_path: Path) -> None:
    """Companion guard: pruning the whole `_trash` directory means even a
    well-formed trashed page never enters the identity census — proving the
    fix is a full directory prune, not selective error-tolerance that would
    still surface valid trash entries either way (malformed or well-formed)."""
    _seed(tmp_path)
    _write(tmp_path, TRASH_GOOD_REL, _page("Good but trashed"))

    census = semantic_contract.build_stable_identity_census(tmp_path)

    paths = {entry.path for entry in census.entries}
    assert PAGE_REL in paths
    assert TRASH_GOOD_REL not in paths


def test_reserved_kb_dirs_prune_trash_at_the_kb_root_only(tmp_path: Path) -> None:
    """Unit-level pin on `_IDENTITY_CENSUS_RESERVED_KB_DIRS`: both spellings
    from the semantic-authoring contract's exempt-content list are reserved,
    matching `.graph-commit-receipts`'s KB-root-only, case-sensitive scope."""
    kb = tmp_path / KB
    assert semantic_contract._prune_identity_census_directory(kb, kb, "_trash")
    assert semantic_contract._prune_identity_census_directory(kb, kb, "trash")
    assert not semantic_contract._prune_identity_census_directory(kb, kb, "_Trash")
    assert not semantic_contract._prune_identity_census_directory(kb, kb, "TRASH")
    assert not semantic_contract._prune_identity_census_directory(
        kb, kb / "Notes", "_trash"
    )
