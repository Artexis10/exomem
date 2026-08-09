"""Plain-text transcript artifact with alternating speakers."""

from __future__ import annotations

from membench.templates.base import SourceContent


def render(content: SourceContent, sentinel: str) -> str:
    lines = [f"TRANSCRIPT: {content.title}", ""]
    speakers = ("A", "B")
    for index, line in enumerate(content.lines):
        lines.append(f"SPEAKER {speakers[index % 2]}: {line}")
    lines.extend(["", sentinel])
    return "\n".join(lines) + "\n"
