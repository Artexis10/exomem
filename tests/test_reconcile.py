"""reconcile: heal index-count + embedding drift from out-of-band edits.

reconcile is the focused "I edited around the system, fix it" command —
recompute index counts + incrementally refresh stale embeddings + report
remaining drift, without audit_fix's wikilink/frontmatter rewrites.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from exomem import activation_manifest, commands, index_sync
from exomem import audit as audit_module
from exomem import reconcile as reconcile_module


def test_reconcile_reports_embeddings_disabled_in_test_env(vault: Path) -> None:
    """The suite runs with EXOMEM_DISABLE_EMBEDDINGS=1, so reconcile reports the
    embedding pass as disabled (no sidecar touched) rather than failing."""
    rep = reconcile_module.reconcile(vault)
    assert rep.embeddings_status == "disabled"
    assert rep.embeddings_refreshed == 0
    assert rep.dry_run is False


def test_reconcile_heals_index_count_drift(vault: Path) -> None:
    """An out-of-band edit that desyncs a count row is detected and restored."""
    top = vault / "Knowledge Base" / "index.md"
    original = top.read_text(encoding="utf-8")
    drifted = original.replace("- Notes (insight): 4", "- Notes (insight): 9")
    assert drifted != original, "fixture index.md changed shape; update the test"
    top.write_text(drifted, encoding="utf-8")

    # Drift is now visible to audit.
    pre = audit_module.audit(vault, categories=["index_drift"])
    assert pre.findings, "expected index_drift after corrupting a count"

    rep = reconcile_module.reconcile(vault)

    assert "Knowledge Base/index.md" in rep.indexes_updated, rep.as_dict()
    assert "- Notes (insight): 4" in top.read_text(encoding="utf-8")
    assert not any(
        f["category"] == "index_drift" for f in rep.remaining_drift
    ), rep.as_dict()


def test_reconcile_via_maintain_memory_heals_out_of_band_count_drift(vault: Path) -> None:
    """`maintain_memory(mode="reconcile")` — the only MCP-exposed entry point for
    reconcile — must actually heal per-type count drift by default, not just
    report it in `remaining_drift` forever. Regression for audit finding 1B-14:
    `op_maintain_memory`'s blanket `dry_run=True` default (shared with the
    riskier `fix`/`backfill-ids` modes) was silently swallowing every write for
    `mode="reconcile"` even though `op_reconcile` itself defaults to writing.
    """
    top = vault / "Knowledge Base" / "index.md"
    notes_dir = vault / "Knowledge Base" / "Notes" / "Insights"
    out_of_band = notes_dir / "manual-oob-note.md"
    out_of_band.write_text(
        "---\ntype: note\npage_type: insight\n---\n\n# Manual OOB\n", encoding="utf-8"
    )

    # Call exactly as an MCP client would: no explicit dry_run.
    res = commands.op_maintain_memory(vault, mode="reconcile")

    assert res["dry_run"] is False, res
    assert "- Notes (insight): 5" in top.read_text(encoding="utf-8")
    assert not any(
        f["category"] == "index_drift" for f in res["remaining_drift"]
    ), res

    # Idempotent: a second call finds nothing left to heal for this drift.
    res2 = commands.op_maintain_memory(vault, mode="reconcile")
    assert not any(
        f["category"] == "index_drift" for f in res2["remaining_drift"]
    ), res2


def test_reconcile_refreshes_source_indexes_and_total_rows(vault: Path) -> None:
    kb = vault / "Knowledge Base"
    extra = kb / "Sources" / "Articles" / "manual-source.md"
    extra.write_text("---\ntype: source\nsource_type: article\n---\n\n# Manual\n", encoding="utf-8")
    top = kb / "index.md"
    top.write_text(
        top.read_text(encoding="utf-8").replace("- Sources: 4", "- Sources: 0"),
        encoding="utf-8",
    )

    rep = reconcile_module.reconcile(vault)

    top_text = top.read_text(encoding="utf-8")
    assert "- Sources: 5" in top_text
    assert "- Notes:" in top_text
    assert "- Entities:" in top_text
    source_index = (kb / "Sources" / "index.md").read_text(encoding="utf-8")
    assert "Articles]]" in source_index and "(3)" in source_index
    assert "Knowledge Base/Sources/index.md" in rep.indexes_updated


def test_reconcile_dry_run_reports_without_writing(vault: Path) -> None:
    """dry_run surfaces the would-be index fix but writes nothing to disk."""
    top = vault / "Knowledge Base" / "index.md"
    top.write_text(
        top.read_text(encoding="utf-8").replace(
            "- Notes (insight): 1", "- Notes (insight): 9"
        ),
        encoding="utf-8",
    )
    drifted = top.read_text(encoding="utf-8")

    rep = reconcile_module.reconcile(vault, dry_run=True)

    assert rep.dry_run is True
    assert "Knowledge Base/index.md" in rep.indexes_updated
    assert top.read_text(encoding="utf-8") == drifted, "dry_run must not write"


def test_reconcile_creates_baseline_only_when_not_dry_run_and_never_refreshes_it(
    vault: Path,
) -> None:
    path = activation_manifest.manifest_path(vault)
    assert not path.exists()
    page = vault / "Knowledge Base/Notes/Insights/legacy-reconcile.md"
    page.write_text("---\ntype: insight\nstatus: active\n---\n\n# Legacy\n", encoding="utf-8")
    before_page = page.read_bytes()

    reconcile_module.reconcile(vault, dry_run=True)
    assert not path.exists()

    reconcile_module.reconcile(vault)
    first_bytes = path.read_bytes()
    assert activation_manifest.is_grandfathered(vault, page)
    assert page.read_bytes() == before_page
    assert "exomem_id:" not in page.read_text(encoding="utf-8")
    assert not (vault / "Knowledge Base/.review-state.json").exists()

    later = vault / "Knowledge Base/Notes/Insights/later-reconcile.md"
    later.write_text("---\ntype: insight\nstatus: active\n---\n\n# Later\n", encoding="utf-8")
    reconcile_module.reconcile(vault)
    assert path.read_bytes() == first_bytes
    assert not activation_manifest.is_grandfathered(vault, later)


def test_reconcile_clears_deferred_semantic_work_after_embedding_refresh(
    vault: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    index_sync.clear_deferred_work(vault)

    from exomem import find, lexstore

    monkeypatch.setattr(lexstore, "upsert_after_write", lambda root, paths: None)
    monkeypatch.setattr(
        find,
        "on_resolver_files_changed",
        lambda root, changed, deleted: None,
    )
    target = vault / "Knowledge Base" / "Notes" / "reconcile-deferred.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# reconcile deferred\n", encoding="utf-8")
    index_sync.upsert_after_write(vault, [target])
    assert index_sync.deferred_work_status(vault)["semantic_upserts"]["count"] == 1

    calls: list[list[Path]] = []
    monkeypatch.setattr(
        "exomem.embeddings.upsert_after_write",
        lambda root, paths: calls.append(list(paths)),
    )
    monkeypatch.setattr(
        audit_module,
        "_check_embedding_drift",
        lambda root: [
            SimpleNamespace(path=Path("Knowledge Base/Notes/reconcile-deferred.md"))
        ],
    )
    monkeypatch.setattr(
        audit_module,
        "audit",
        lambda root, categories: SimpleNamespace(findings=[]),
    )
    monkeypatch.setattr(
        reconcile_module.indexes,
        "compute_subindex_writes",
        lambda root, top_index_text: ([], top_index_text),
    )
    monkeypatch.setattr("exomem.lexstore.ensure_fresh", lambda root: None)

    rep = reconcile_module.reconcile(vault)

    assert rep.embeddings_status == "refreshed"
    assert calls == [[target]]
    assert index_sync.deferred_work_status(vault)["semantic_upserts"]["count"] == 0


def test_reconcile_preserves_deferred_work_after_embedding_failure(
    vault: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    index_sync.clear_deferred_work(vault)

    from exomem import find, lexstore

    monkeypatch.setattr(lexstore, "upsert_after_write", lambda root, paths: None)
    monkeypatch.setattr(find, "on_resolver_files_changed", lambda root, changed, deleted: None)
    target = vault / "Knowledge Base" / "Notes" / "reconcile-retry.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# reconcile retry\n", encoding="utf-8")
    index_sync.upsert_after_write(vault, [target])

    monkeypatch.setattr("exomem.embeddings.upsert_after_write", lambda root, paths: False)
    monkeypatch.setattr(
        audit_module,
        "_check_embedding_drift",
        lambda root: [SimpleNamespace(path=Path("Knowledge Base/Notes/reconcile-retry.md"))],
    )
    monkeypatch.setattr(
        audit_module,
        "audit",
        lambda root, categories: SimpleNamespace(findings=[]),
    )
    monkeypatch.setattr(
        reconcile_module.indexes,
        "compute_subindex_writes",
        lambda root, top_index_text: ([], top_index_text),
    )
    monkeypatch.setattr("exomem.lexstore.ensure_fresh", lambda root: None)

    rep = reconcile_module.reconcile(vault)

    assert rep.embeddings_status == "deferred"
    assert rep.embeddings_refreshed == 0
    assert index_sync.deferred_work_status(vault)["semantic_upserts"]["count"] == 1
