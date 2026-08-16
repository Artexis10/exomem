"""A vault reached through a symlink must still index its own files.

`Path.relative_to` is purely lexical — it never consults the filesystem. Code
that resolves the vault root but compares it against unresolved file paths
therefore decides that every file in the vault is *outside* the vault, and the
sites below all react by silently skipping the file rather than raising.

The visible symptom is total, quiet semantic loss: nothing is ever embedded, the
vector lane finds nothing, every search falls back to keyword ranking, and the
write path reports the benign `no_eligible_paths` while doing so. Symlinked
vault roots are ordinary — macOS `/tmp` is a symlink to `/private/tmp`, and
synced or NAS-mounted vaults routinely sit behind one.

What must NOT change is which files are eligible. Location and eligibility are
separate questions — `recall_policy` already declines symlinked files on their
own merits — and the fix must not quietly answer the second one while repairing
the first, so that boundary is pinned here too.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem import embedding_index, embeddings, index_paths, recall_policy
from exomem.kbdir import kb_dirname


@pytest.fixture
def symlinked_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A real vault plus an equivalent path that traverses a symlink.

    Returns `(real_root, linked_root)`. Both name the same directory; only the
    spelling differs, which is exactly what a lexical comparison gets wrong.
    """
    real = tmp_path / "real" / "vault"
    (real / kb_dirname() / "Notes").mkdir(parents=True)
    link_parent = tmp_path / "link"
    link_parent.mkdir()
    (link_parent / "alias").symlink_to(tmp_path / "real")
    linked = link_parent / "alias" / "vault"
    assert linked.resolve() == real.resolve()
    assert str(linked) != str(real)
    return real, linked


def _write_note(root: Path, name: str = "note.md") -> Path:
    path = root / kb_dirname() / "Notes" / name
    path.write_text("---\ntype: note\n---\n\nA sentence worth embedding.\n", encoding="utf-8")
    return path


def test_a_written_file_is_eligible_through_a_symlinked_root(
    symlinked_vault: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug in one assertion: admission counted zero files that exist.

    `eligible_count` is the observable that survives `EXOMEM_DISABLE_EMBEDDINGS`,
    so this pins the path-admission defect without loading a model.
    """
    _real, linked = symlinked_vault
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    written = _write_note(linked)

    status = embeddings.upsert_after_write_status(linked, [written])

    assert status.eligible_count == 1


def test_admission_is_identical_through_either_spelling(
    symlinked_vault: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two names for one directory must not produce two different verdicts."""
    real, linked = symlinked_vault
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _write_note(linked)

    through_real = embeddings.upsert_after_write_status(
        real, [real / kb_dirname() / "Notes" / "note.md"]
    )
    through_link = embeddings.upsert_after_write_status(
        linked, [linked / kb_dirname() / "Notes" / "note.md"]
    )

    assert through_link.eligible_count == through_real.eligible_count == 1


def test_a_path_genuinely_outside_the_vault_is_still_rejected(
    symlinked_vault: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving both sides must not turn containment into a rubber stamp."""
    _real, linked = symlinked_vault
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    outsider = tmp_path / "elsewhere.md"
    outsider.write_text("not in the vault\n", encoding="utf-8")

    status = embeddings.upsert_after_write_status(linked, [outsider])

    assert status.eligible_count == 0


def test_a_symlinked_file_is_still_refused_by_recall_policy_not_by_geometry(
    symlinked_vault: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different questions that a lexical bug had been conflating.

    `rel_to_vault` answers *where* a file is; `recall_policy` answers *whether*
    it may be indexed, and it already declines symlinked files on their own
    merits. Keeping those separate is the point: the fix must not start
    admitting files policy rejects, nor keep rejecting files only because the
    root was spelled through a link.
    """
    _real, linked = symlinked_vault
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    outsider = tmp_path / "outside.md"
    outsider.write_text("shared notes\n", encoding="utf-8")
    linked_in = linked / kb_dirname() / "Notes" / "shared.md"
    linked_in.symlink_to(outsider)

    # Located inside the vault ...
    assert index_paths.rel_to_vault(linked, linked_in) == f"{kb_dirname()}/Notes/shared.md"
    # ... and declined anyway, by the gate that owns that decision.
    assert recall_policy.is_recall_candidate(linked, linked_in) is False
    assert embeddings.upsert_after_write_status(linked, [linked_in]).eligible_count == 0


def test_the_corpus_signature_sees_the_corpus_through_a_symlinked_root(
    symlinked_vault: tuple[Path, Path],
) -> None:
    """An empty signature would let a stale sidecar look current."""
    _real, linked = symlinked_vault
    _write_note(linked)

    _identity, rows = embedding_index.EmbeddingIndex(linked)._projected_source_snapshot()

    assert [rel for rel, _signature in rows] == [f"{kb_dirname()}/Notes/note.md"]


def test_rel_to_vault_agrees_across_spellings(symlinked_vault: tuple[Path, Path]) -> None:
    """The shared helper is the single place this comparison is made."""
    real, linked = symlinked_vault
    _write_note(linked)
    expected = f"{kb_dirname()}/Notes/note.md"

    assert index_paths.rel_to_vault(linked, linked / expected) == expected
    assert index_paths.rel_to_vault(linked, real / expected) == expected
    assert index_paths.rel_to_vault(real, linked / expected) == expected


def test_rel_to_vault_reports_absence_rather_than_raising(
    symlinked_vault: tuple[Path, Path], tmp_path: Path
) -> None:
    """Callers skip non-members; a raised error would abort the whole batch."""
    _real, linked = symlinked_vault
    outsider = tmp_path / "elsewhere.md"
    outsider.write_text("x\n", encoding="utf-8")

    assert index_paths.rel_to_vault(linked, outsider) is None
    assert index_paths.rel_to_vault(linked, Path(os.devnull)) is None


def test_rel_to_vault_tolerates_a_path_that_does_not_exist(
    symlinked_vault: tuple[Path, Path],
) -> None:
    """Deletion sync asks about paths that are already gone."""
    _real, linked = symlinked_vault
    expected = f"{kb_dirname()}/Notes/deleted.md"

    assert index_paths.rel_to_vault(linked, linked / expected) == expected
