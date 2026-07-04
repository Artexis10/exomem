"""propose_compilation: turn unprocessed source(s) into a draft note skeleton.

Pure retrieval. Reads the cited source(s), infers a likely note_type, finds the
adjacent compiled notes worth connecting to (reusing corpus_aware.suggest_related),
and returns a sectioned outline with `sources[]` and wikilink connections
pre-filled. It NEVER writes — the client (Claude) fills the prose and calls
`note()`. Generation stays client-side per the pure-substrate principle; the
server just hands over the scaffolding and the connections it can compute.

Grouping (which sources belong together) is deliberately NOT done here — that's a
judgment call the client is better at. The audit unprocessed_source check surfaces
the aged backlog; the client picks a coherent set and passes it here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import corpus_aware
from . import find as find_module
from .vault import normalize_wikilink

log = logging.getLogger(__name__)


@dataclass
class ProposeError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def propose_compilation(
    vault_root: Path,
    *,
    sources: list[str],
    suggested_title: str | None = None,
) -> dict:
    """Return a draft compilation scaffold for `sources`. Never writes."""
    if not sources:
        raise ProposeError("INVALID_PROPOSE", "provide at least one source path")

    resolver = find_module.shared_resolver(vault_root)
    resolved: list[str] = []          # canonical "Knowledge Base/..." (no .md)
    source_types: list[str] = []
    titles: list[str] = []
    body_parts: list[str] = []
    warnings: list[str] = []

    for s in sources:
        canonical, _w = normalize_wikilink(s, vault_root, resolver=resolver, strict=False)
        abs_path = vault_root / f"{canonical}.md"
        page = find_module._CACHE.get(abs_path, vault_root)
        if page is None:
            warnings.append(f"source not found, skipped: {s}")
            continue
        resolved.append(canonical)
        st = page.frontmatter.get("source_type")
        if isinstance(st, str):
            source_types.append(st)
        titles.append(page.title)
        body_parts.append(page.body)

    if not resolved:
        raise ProposeError(
            "SOURCES_NOT_FOUND", f"none of the provided sources resolved: {sources}"
        )

    combined_body = "\n\n".join(body_parts)
    title = suggested_title or _suggest_title(titles)
    note_type = _suggest_note_type(source_types)

    # Adjacent compiled notes to connect to — exclude the sources themselves.
    suggestions = corpus_aware.suggest_related(
        vault_root,
        title=title,
        body=combined_body,
        self_path=None,
        existing_links=set(resolved),
        limit=6,
        scope="kb",
    )
    connections = [s.path for s in suggestions if s.type != "source"][:5]

    return {
        "suggested_note_type": note_type,
        "suggested_title": title,
        "suggested_sources": resolved,
        "suggested_connections": connections,
        "outline_markdown": _render_outline(note_type, title, connections),
        "warnings": warnings,
    }


def _suggest_title(titles: list[str]) -> str:
    clean = [t.removeprefix("Source: ").strip() for t in titles if t]
    if not clean:
        return "Untitled compilation"
    if len(clean) == 1:
        return clean[0]
    head = "; ".join(clean[:2])
    return f"Synthesis: {head}" + (" + more" if len(clean) > 2 else "")


def _suggest_note_type(source_types: list[str]) -> str:
    """Heuristic only — the client picks the final type. Session-heavy captures
    tend to be project work (research-note); mixed/article captures tend to be
    cross-cutting (insight)."""
    if source_types and source_types.count("session") >= (len(source_types) + 1) // 2:
        return "research-note"
    return "insight"


_SECTIONS: dict[str, list[str]] = {
    "research-note": ["Question", "Findings", "Connections"],
    "insight": ["Claim", "Why it holds", "Connections"],
    "failure": ["What happened", "Mechanism", "Detection", "Mitigation", "Connections"],
    "pattern": ["Problem", "Solution", "When to use", "When NOT to use", "Connections"],
}


def _render_outline(note_type: str, title: str, connections: list[str]) -> str:
    sections = _SECTIONS.get(note_type, _SECTIONS["insight"])
    lines = [f"# {title}", ""]
    if note_type == "research-note":
        lines += [
            "> Draft scaffold from propose_compilation. research-note requires a "
            "`project:` — pass it to note(). Fill the prose, then call note() with "
            "the suggested_sources/connections.",
            "",
        ]
    else:
        lines += [
            "> Draft scaffold from propose_compilation. Fill the prose, then call "
            "note() with the suggested_sources + connections.",
            "",
        ]
    for sec in sections:
        lines.append(f"## {sec}")
        lines.append("")
        if sec == "Connections" and connections:
            for c in connections:
                lines.append(f"- [[{c}]]")
        else:
            lines.append(f"<!-- {sec.lower()}: distilled from the cited source(s) -->")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
