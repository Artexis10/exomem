"""The note-type registry, end to end (`add-note-type-registry`, S4a).

An owner registers a vault-defined compiled type with its own folder. Ranking,
the semantic write gate and activation then treat its page as compiled. A
restore removes the type again; the page keeps its bytes and becomes
unregistered note-type debt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import activation, commands
from exomem import find as find_module

MEETING_PAGE = "Knowledge Base/Notes/Meetings/2026-10-quillwort-sync.md"
MEETING_SOURCE = (
    "---\ntype: meeting-note\nstatus: active\ncreated: 2026-10-01\nupdated: 2026-10-01\n---\n\n"
    "# Quillwort sync\n\nThe quillwort crew agreed the tide schedule.\n"
)
MEETING_TYPE = {
    "label": "Meeting note",
    "description": "The agreed outcome of one meeting.",
    "attributes": {"role": "compiled", "folder": "Notes/Meetings"},
}


def _type_factor(vault: Path) -> float:
    explained = commands.op_find(
        vault,
        query="quillwort",
        mode="hybrid",
        graph=False,
        rerank=False,
        scope="kb-only",
        detail="compact",
        explain=True,
    )
    [hit] = [hit for hit in explained["hits"] if hit["path"] == MEETING_PAGE]
    [step] = [
        step for step in hit["ranking_explanation"]["multipliers"] if step["name"] == "type"
    ]
    return step["factor"]


def _gate_codes(vault: Path) -> set[str]:
    validation = commands.op_manage_memory_file(
        vault,
        operation="create",
        path="Knowledge Base/Notes/Meetings/2026-10-second-sync.md",
        content="# Second sync\n\nThe crew met again.\n",
        frontmatter={"type": "meeting-note", "status": "active"},
        validate_only=True,
    )
    return {finding["code"] for finding in validation["contract_result"]["blocking_findings"]}


def _eligible(vault: Path) -> bool:
    page = find_module._parse_page(vault / MEETING_PAGE, 0.0, vault)
    return activation.is_eligible_compiled_page(vault, page)


def _debt(vault: Path) -> set[str]:
    audited = commands.op_maintain_memory(
        vault, mode="audit", categories=["frontmatter_compliance"]
    )
    return {
        finding["path"]
        for finding in audited["findings"]
        if finding.get("meta", {}).get("code") == "unregistered_note_type"
    }


def test_a_vault_compiled_type_takes_compiled_behaviour_until_restored(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance.principal import library_scope

    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    page = vault / MEETING_PAGE
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(MEETING_SOURCE, encoding="utf-8")
    original = page.read_bytes()

    with library_scope():
        assert _type_factor(vault) == 1.0
        assert "missing_semantic_unit" not in _gate_codes(vault)
        assert not _eligible(vault)
        assert MEETING_PAGE in _debt(vault)

        inspected = commands.op_schema_memory(vault, subject="note-types", operation="inspect")
        saved = commands.op_schema_memory(
            vault,
            subject="note-types",
            operation="save",
            proposal={"upsert": {"meeting-note": MEETING_TYPE}},
            expected_hash=inspected["content_hash"],
            why="meeting outcomes are compiled conclusions",
        )
        assert saved["valid"] and saved["saved"]

        assert _type_factor(vault) == pytest.approx(1.15)
        assert "missing_semantic_unit" in _gate_codes(vault)
        assert _eligible(vault)
        assert MEETING_PAGE not in _debt(vault)

        history = commands.op_schema_memory(vault, subject="note-types", operation="history")
        restored = commands.op_schema_memory(
            vault,
            subject="note-types",
            operation="restore",
            version=history["versions"][0]["version"],
            expected_hash=history["content_hash"],
            why="the owner keeps meetings as plain pages",
        )
        assert restored["removed_keys"] == ["meeting-note"]

        assert _type_factor(vault) == 1.0
        assert "missing_semantic_unit" not in _gate_codes(vault)
        assert not _eligible(vault)
        assert MEETING_PAGE in _debt(vault)
    assert page.read_bytes() == original
