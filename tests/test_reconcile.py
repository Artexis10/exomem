"""reconcile: heal index-count + embedding drift from out-of-band edits.

reconcile is the focused "I edited around the system, fix it" command —
recompute index counts + incrementally refresh stale embeddings + report
remaining drift, without audit_fix's wikilink/frontmatter rewrites.
"""

from __future__ import annotations

from pathlib import Path

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
    drifted = original.replace("- Notes (insight): 1", "- Notes (insight): 9")
    assert drifted != original, "fixture index.md changed shape; update the test"
    top.write_text(drifted, encoding="utf-8")

    # Drift is now visible to audit.
    pre = audit_module.audit(vault, categories=["index_drift"])
    assert pre.findings, "expected index_drift after corrupting a count"

    rep = reconcile_module.reconcile(vault)

    assert "Knowledge Base/index.md" in rep.indexes_updated, rep.as_dict()
    assert "- Notes (insight): 1" in top.read_text(encoding="utf-8")
    assert not any(
        f["category"] == "index_drift" for f in rep.remaining_drift
    ), rep.as_dict()


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
