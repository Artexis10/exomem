"""Epistemic graph freshness, audit, and reconcile integration."""

from __future__ import annotations

from pathlib import Path

from exomem import audit, epistemic_graph, reconcile

A = "Knowledge Base/Notes/Insights/a.md"
B = "Knowledge Base/Notes/Insights/b.md"


def _write(vault: Path, rel: str, body: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _seed(vault: Path) -> tuple[Path, Path]:
    a = _write(
        vault,
        A,
        """\
---
type: insight
status: active
---
# A

## Claim

A claim links to [[Knowledge Base/Notes/Insights/b]].
""",
    )
    b = _write(
        vault,
        B,
        """\
---
type: insight
status: active
---
# B

## Claim

B claim.
""",
    )
    return a, b


def test_single_file_edit_refreshes_affected_graph_rows(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    a, b = _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    b_before = next(n for n in idx.nodes(path=B) if n["kind"] == "file")["source_hash"]

    a.write_text(a.read_text(encoding="utf-8").replace("A claim", "A changed claim"), encoding="utf-8")
    report = idx.refresh_paths([a])

    assert report["indexed_files"] == 1
    a_after = next(n for n in idx.nodes(path=A) if n["kind"] == "file")
    b_after = next(n for n in idx.nodes(path=B) if n["kind"] == "file")
    assert a_after["source_hash"] == epistemic_graph.vault_module.content_hash(a.read_text(encoding="utf-8"))
    assert b_after["source_hash"] == b_before


def test_incremental_graph_update_matches_full_rebuild(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    a, _b = _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()

    a.write_text(a.read_text(encoding="utf-8") + "\n## Decision\n\nKeep it derived.\n", encoding="utf-8")
    idx.refresh_paths([a])
    incremental = epistemic_graph.graph_context(vault, path=A, depth=1)

    epistemic_graph.sidecar_path(vault).unlink()
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    rebuilt = epistemic_graph.graph_context(vault, path=A, depth=1)

    assert incremental == rebuilt


def test_graph_drift_is_audited_and_reconciled_without_markdown_mutation(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    a, _b = _seed(vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    changed = a.read_text(encoding="utf-8").replace("A claim", "Externally edited claim")
    a.write_text(changed, encoding="utf-8")

    report = audit.audit(vault, categories=["graph_drift"])
    assert report.findings
    assert report.findings[0].category == "graph_drift"

    reconciled = reconcile.reconcile(vault)

    assert a.read_text(encoding="utf-8") == changed
    assert reconciled.graph_status == "refreshed"
    assert all(f["category"] != "graph_drift" for f in reconciled.remaining_drift)


def test_disabled_graph_indexing_makes_drift_check_noop(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")

    report = audit.audit(vault, categories=["graph_drift"])

    assert report.findings == []
