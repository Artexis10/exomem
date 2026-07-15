from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAFFOLD_ROOT = REPO_ROOT / "src/exomem/_scaffold/_Schema"


def _section(relative: str, heading: str) -> str:
    text = (SCAFFOLD_ROOT / relative).read_text(encoding="utf-8")
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    return " ".join(section.lower().split())


def test_shipped_scaffold_teaches_human_readable_memory_citations() -> None:
    sections = (
        _section("SKILL.md", "## Durable references"),
        _section("references/operations.md", "## Stable identity and references"),
    )

    for guidance in sections:
        for required in (
            "show the note title by default",
            "normal user-facing prose",
            "do not expose the raw canonical ref by default",
            "current vault-relative path",
            "clarity or disambiguation",
            "path or file name as the visible fallback",
            "tool arguments",
            "durable machine state",
            "machine-readable automation",
            "user explicitly asks",
            "identifier itself is being inspected or debugged",
            "do not embed the canonical ref as a markdown link target",
            "plain title-first citation",
        ):
            assert required in guidance
        assert "exomem://memory/<uuid>" in guidance


def test_operations_warns_against_visible_custom_scheme_link_targets() -> None:
    guidance = _section(
        "references/operations.md", "## Stable identity and references"
    )

    assert "do not embed the canonical ref as a markdown link target" in guidance
    assert "plain title-first citation" in guidance
