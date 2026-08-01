"""Cell-budget text rules for a grid that cannot reflow.

A terminal has no soft wrap worth trusting: a folded vault path is harder to
read than a truncated one, and a wrapped list row destroys the alignment that
makes a queue scannable. Every region therefore computes a cell budget and
fits its strings to it here, with one rule per shape:

* generic text truncates at the tail (`fit`);
* paths truncate from the LEFT, so the filename — the part that identifies the
  page — always survives (`truncate_path`);
* prose wraps on word boundaries inside its own column (`wrap`).

Receipt lines share a fixed label field so `✓ vault`, `✓ packs`, and
`✓ capture` line their detail up in a column you can read down.
"""

from __future__ import annotations

import textwrap

#: Width of the receipt label field: glyph + word, padded, then the detail.
LABEL_FIELD = 12


def fit(text: str, budget: int, ellipsis: str = "…") -> str:
    """Hard-truncate to a cell budget, marking the cut with an ellipsis."""
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    if budget <= len(ellipsis):
        return text[:budget]
    return text[: budget - len(ellipsis)].rstrip() + ellipsis


def truncate_path(path: str, budget: int, ellipsis: str = "…") -> str:
    """Shorten a vault path from the LEFT so the filename stays readable.

    `Knowledge Base/Notes/Insights/queue-backpressure-needs-explicit-limits.md`
    becomes `…/Insights/queue-backpressure-needs-explicit-limits.md` and, if
    that still overflows, `…limits.md` — never a truncated directory with the
    filename gone.
    """
    if budget <= 0:
        return ""
    if len(path) <= budget:
        return path
    parts = [part for part in path.split("/") if part]
    for start in range(1, len(parts)):
        candidate = ellipsis + "/" + "/".join(parts[start:])
        if len(candidate) <= budget:
            return candidate
    tail = parts[-1] if parts else path
    if len(tail) + len(ellipsis) <= budget:
        return ellipsis + tail
    return ellipsis + tail[-(budget - len(ellipsis)) :]


def wrap(text: str, budget: int, limit: int | None = None) -> list[str]:
    """Word-wrap prose inside its column; never break a word mid-way."""
    if budget <= 0:
        return []
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(textwrap.wrap(paragraph, width=budget) or [""])
    if limit is not None and len(lines) > limit:
        lines = lines[:limit]
    return lines


def label_field(glyph: str, word: str, width: int = LABEL_FIELD) -> str:
    """`✓ vault     ` — the glyph + word pair padded to the receipt column."""
    return f"{glyph} {word}".ljust(width)


def first_words(text: str, budget: int = 34) -> str:
    """The opening of a capture, for its receipt line."""
    flat = " ".join(text.split())
    if len(flat) <= budget:
        return flat
    return flat[:budget].rstrip() + "…"
