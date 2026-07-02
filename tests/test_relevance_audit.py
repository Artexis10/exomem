"""relevance_pairs_pending audit check: surface unconfirmed retrieval feedback.

A note/replace write that cites a path shortly after a find() which surfaced
that path is a weak (query -> path) relevance label. When such a query isn't
yet in the golden set, ranking has measurable signal nobody has confirmed.
The check is model-free (pure log-join) and gated by an env flag so the
per-vault suite stays deterministic against repo-global logs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import audit as audit_module


def _write_logs(tmp: Path) -> tuple[Path, Path]:
    logs = tmp / "logs"
    logs.mkdir()
    (logs / "queries.jsonl").write_text(
        json.dumps({
            "ts": "2026-05-31T10:00:00",
            "query": "glucose regulation and brain function",
            "top_k": [{"path": "Knowledge Base/Notes/Insights/foo"}],
        }) + "\n",
        encoding="utf-8",
    )
    (logs / "writes.jsonl").write_text(
        json.dumps({
            "ts": "2026-05-31T10:05:00",
            "tool": "note",
            "written_path": "Knowledge Base/Notes/Insights/bar",
            "cited_sources": ["Knowledge Base/Notes/Insights/foo"],
        }) + "\n",
        encoding="utf-8",
    )
    golden = tmp / "queries.yaml"
    golden.write_text("[]\n", encoding="utf-8")
    return logs, golden


def test_relevance_check_surfaces_new_pairs(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs, golden = _write_logs(tmp_path)
    monkeypatch.delenv("EXOMEM_DISABLE_RELEVANCE_CHECK", raising=False)
    monkeypatch.setattr(audit_module, "_RELEVANCE_LOGS_DIR", logs)
    monkeypatch.setattr(audit_module, "_RELEVANCE_GOLDEN", golden)

    report = audit_module.audit(vault, categories=["relevance_pairs_pending"])
    rel = [f for f in report.findings if f.category == "relevance_pairs_pending"]
    assert len(rel) == 1, [f.as_dict() for f in report.findings]
    assert rel[0].severity == "info"
    assert rel[0].meta["new_queries"] == 1, rel[0].as_dict()


def test_relevance_check_disabled_by_default_in_tests(vault: Path) -> None:
    """conftest sets EXOMEM_DISABLE_RELEVANCE_CHECK, so default audits don't read
    repo-global logs (keeps the per-vault suite deterministic)."""
    report = audit_module.audit(vault, categories=["relevance_pairs_pending"])
    assert not [
        f for f in report.findings if f.category == "relevance_pairs_pending"
    ]


def test_relevance_query_already_in_golden_not_flagged(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs, golden = _write_logs(tmp_path)
    golden.write_text(
        "- query: glucose regulation and brain function\n"
        "  expect_any_of: [Knowledge Base/Notes/Insights/foo]\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("EXOMEM_DISABLE_RELEVANCE_CHECK", raising=False)
    monkeypatch.setattr(audit_module, "_RELEVANCE_LOGS_DIR", logs)
    monkeypatch.setattr(audit_module, "_RELEVANCE_GOLDEN", golden)

    report = audit_module.audit(vault, categories=["relevance_pairs_pending"])
    assert not [
        f for f in report.findings if f.category == "relevance_pairs_pending"
    ]
