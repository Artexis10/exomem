"""Markdown source artifact: title, fact paragraphs, visible sentinel."""

from __future__ import annotations

from membench.templates.base import SourceContent


def _table(rows: list[list[str]]) -> str:
    header, *body = rows
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def render(content: SourceContent, sentinel: str, *, banner: str | None = None) -> str:
    parts = [f"# {content.title}", ""]
    if banner:
        parts.extend([banner, ""])
    for line in content.lines:
        parts.extend([line, ""])
    if content.table:
        parts.extend([_table(content.table), ""])
    parts.append(sentinel)
    return "\n".join(parts) + "\n"
