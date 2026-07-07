"""Excluded scan dirs must stay excluded on the INCREMENTAL index paths.

Every full rebuild (walk_vault_md, find's walker, the inbound scan) skips
`VAULT_SCAN_SKIP_DIRS` (`_trash/`, `_archive/`, `_Schema/`, …). The
event-driven patch paths did not: `delete_file` moves a note into
`Knowledge Base/_trash/`, the watcher sees a fresh .md file there, and the
trashed content was re-embedded under its trash path — invisible to find()
but not to the corpus-aware near-dup sweep, which reads the raw sidecar
(observed live 2026-07-04, dup warnings pointing at `_trash/` entries).

The fix is two chokepoints sharing one predicate (`vault.in_excluded_scan_dir`):
the watcher drops excluded paths at its single event intake (`_record`), and
`index_sync.upsert_after_write` filters as the belt for direct writer calls.
Deletes stay UNfiltered so legacy pollution can still be purged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import index_sync
from exomem.vault import in_excluded_scan_dir


def test_excluded_scan_dir_predicate() -> None:
    kb = "Knowledge Base"
    assert in_excluded_scan_dir(f"{kb}/_trash/2026-07-04/foo.md")
    assert in_excluded_scan_dir(f"{kb}/_archive/old.md")
    assert in_excluded_scan_dir(f"{kb}/_Schema/SKILL.md")
    assert in_excluded_scan_dir(".obsidian/workspace.json")
    # Backslash tolerance (Windows callers).
    assert in_excluded_scan_dir(f"{kb}\\_trash\\2026-07-04\\foo.md")
    # Normal content is NOT excluded.
    assert not in_excluded_scan_dir(f"{kb}/Notes/Insights/foo.md")
    assert not in_excluded_scan_dir(f"{kb}/Sources/Articles/bar.md")
    # Only whole segments match — a note ABOUT trash isn't excluded.
    assert not in_excluded_scan_dir(f"{kb}/Notes/Insights/_trash-handling.md")


def test_index_sync_upsert_drops_excluded_paths(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, list[Path]] = {}

    def _rec(name):
        def hook(vault_root, paths):
            seen[name] = list(paths)
        return hook

    from exomem import embeddings, find, lexstore

    monkeypatch.setattr(lexstore, "upsert_after_write", _rec("lexstore"))
    monkeypatch.setattr(embeddings, "upsert_after_write", _rec("embeddings"))
    resolver_rels: list[str] = []
    monkeypatch.setattr(
        find, "on_resolver_files_changed",
        lambda vr, changed, deleted: resolver_rels.extend(changed),
    )

    good = vault / "Knowledge Base" / "Notes" / "Insights" / "keep-me.md"
    trashed = vault / "Knowledge Base" / "_trash" / "2026-07-04" / "gone.md"
    trashed.parent.mkdir(parents=True, exist_ok=True)
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("# keep\n", encoding="utf-8")
    trashed.write_text("# gone\n", encoding="utf-8")

    index_sync.upsert_after_write(vault, [good, trashed])

    assert seen["lexstore"] == [good]
    assert seen["embeddings"] == [good]
    assert resolver_rels == ["Knowledge Base/Notes/Insights/keep-me.md"]


def test_index_sync_upsert_noop_when_all_excluded(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embeddings, lexstore

    called: list[str] = []
    monkeypatch.setattr(
        lexstore, "upsert_after_write", lambda vr, p: called.append("lex")
    )
    monkeypatch.setattr(
        embeddings, "upsert_after_write", lambda vr, p: called.append("emb")
    )
    trashed = vault / "Knowledge Base" / "_trash" / "2026-07-04" / "gone.md"
    trashed.parent.mkdir(parents=True, exist_ok=True)
    trashed.write_text("# gone\n", encoding="utf-8")

    index_sync.upsert_after_write(vault, [trashed])

    assert called == []


def test_index_sync_quiet_defers_semantic_upserts(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    index_sync.clear_deferred_work(vault)
    from exomem import embeddings, find, lexstore

    seen: dict[str, list] = {"lexstore": [], "embeddings": [], "resolver": []}
    monkeypatch.setattr(
        lexstore,
        "upsert_after_write",
        lambda root, paths: seen["lexstore"].append(list(paths)),
    )
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write",
        lambda root, paths: seen["embeddings"].append(list(paths)),
    )
    monkeypatch.setattr(
        find,
        "on_resolver_files_changed",
        lambda root, changed, deleted: seen["resolver"].append(
            (list(changed), list(deleted))
        ),
    )

    good = vault / "Knowledge Base" / "Notes" / "Insights" / "quiet-defers.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("# quiet\n", encoding="utf-8")

    try:
        index_sync.upsert_after_write(vault, [good, good])

        assert seen["lexstore"] == [[good, good]]
        assert seen["embeddings"] == []
        assert seen["resolver"] == [
            (
                [
                    "Knowledge Base/Notes/Insights/quiet-defers.md",
                    "Knowledge Base/Notes/Insights/quiet-defers.md",
                ],
                [],
            )
        ]
        status = index_sync.deferred_work_status(vault)["semantic_upserts"]
        assert status["count"] == 1
        assert status["paths"] == ["Knowledge Base/Notes/Insights/quiet-defers.md"]
    finally:
        index_sync.clear_deferred_work(vault)


def test_index_sync_nonquiet_keeps_immediate_embedding_upsert(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    index_sync.clear_deferred_work(vault)
    from exomem import embeddings, find, lexstore

    seen: dict[str, list] = {"lexstore": [], "embeddings": [], "resolver": []}
    monkeypatch.setattr(
        lexstore,
        "upsert_after_write",
        lambda root, paths: seen["lexstore"].append(list(paths)),
    )
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write",
        lambda root, paths: seen["embeddings"].append(list(paths)),
    )
    monkeypatch.setattr(
        find,
        "on_resolver_files_changed",
        lambda root, changed, deleted: seen["resolver"].append(
            (list(changed), list(deleted))
        ),
    )

    good = vault / "Knowledge Base" / "Notes" / "Insights" / "normal-upserts.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("# normal\n", encoding="utf-8")

    index_sync.upsert_after_write(vault, [good])

    assert seen["lexstore"] == [[good]]
    assert seen["embeddings"] == [[good]]
    assert seen["resolver"] == [(["Knowledge Base/Notes/Insights/normal-upserts.md"], [])]
    assert index_sync.deferred_work_status(vault)["semantic_upserts"]["count"] == 0


def test_drain_deferred_work_processes_and_clears_semantic_upserts(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    index_sync.clear_deferred_work(vault)
    from exomem import embeddings, find, lexstore

    monkeypatch.setattr(lexstore, "upsert_after_write", lambda root, paths: None)
    monkeypatch.setattr(
        find,
        "on_resolver_files_changed",
        lambda root, changed, deleted: None,
    )
    calls: list[list[Path]] = []
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write",
        lambda root, paths: calls.append(list(paths)),
    )
    good = vault / "Knowledge Base" / "Notes" / "Insights" / "drain-me.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("# drain\n", encoding="utf-8")

    index_sync.upsert_after_write(vault, [good])
    assert index_sync.deferred_work_status(vault)["semantic_upserts"]["count"] == 1

    processed = index_sync.drain_deferred_work(vault)

    assert processed == 1
    assert calls == [[good]]
    assert index_sync.deferred_work_status(vault)["semantic_upserts"]["count"] == 0