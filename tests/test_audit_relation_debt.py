from __future__ import annotations

from pathlib import Path

from exomem import audit as audit_module


def _write(
    vault: Path,
    name: str,
    body: str,
    *,
    status: str = "active",
    tags: str = "[]",
) -> str:
    rel = f"Knowledge Base/Notes/Insights/{name}.md"
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
type: insight
status: {status}
created: 2026-07-10
updated: 2026-07-10
tags: {tags}
---
{body}
""",
        encoding="utf-8",
    )
    return rel


def _findings(vault: Path):
    return audit_module.audit(vault, categories=["relation_debt"]).findings


def test_isolated_compiled_note_is_relation_debt(tmp_path: Path) -> None:
    rel = _write(tmp_path, "isolated", "## Finding\n\nA durable conclusion.")

    findings = _findings(tmp_path)

    assert [finding.path for finding in findings] == [rel]
    assert findings[0].severity == "info"
    assert findings[0].meta["signal_version"]
    assert "suggest-relations" in findings[0].proposed_fix


def test_canonical_block_or_inline_relation_clears_debt(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "canonical",
        "## Relations\n- relates_to [[Knowledge Base/Notes/Insights/target]]\n",
    )
    _write(
        tmp_path,
        "block",
        "## Finding\n- relations: supports: [[Knowledge Base/Notes/Insights/target]]\n\nBody.",
    )
    _write(
        tmp_path,
        "inline",
        "## Finding\n\nThis follows [[Knowledge Base/Notes/Insights/target]].",
    )

    assert _findings(tmp_path) == []


def test_inactive_hub_and_readonly_pages_are_excluded(tmp_path: Path) -> None:
    _write(tmp_path, "archived", "## Finding\n\nOld.", status="archived")
    _write(tmp_path, "project-snapshot", "## Finding\n\nExpected snapshot.")
    _write(tmp_path, "tagged-hub", "## Finding\n\nExpected hub.", tags="[hub]")
    _write(tmp_path, "readonly", "## Finding\n\nCurated.")
    (tmp_path / "Knowledge Base/_access.yaml").write_text(
        "readonly:\n  - Notes/Insights/readonly.md\n",
        encoding="utf-8",
    )

    assert _findings(tmp_path) == []


def test_relation_debt_signal_version_changes_with_content(tmp_path: Path) -> None:
    rel = _write(tmp_path, "changing", "## Finding\n\nFirst version.")
    first = _findings(tmp_path)[0].meta["signal_version"]

    _write(tmp_path, "changing", "## Finding\n\nSecond version.")
    second_findings = _findings(tmp_path)

    assert second_findings[0].path == rel
    assert first != second_findings[0].meta["signal_version"]
