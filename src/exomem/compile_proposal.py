"""propose_compilation: offer one optional note scaffold from captured sources.

Pure retrieval. Reads the cited source(s), infers a likely note_type, finds the
adjacent compiled notes worth connecting to (reusing corpus_aware.suggest_related),
and returns a sectioned outline with citation candidates. Adjacent pages are
unreviewed candidates, never authored edges. The outline is not a source-wide
semantic compilation plan. The client reads the full material, judges useful
destinations and relations, and writes through the governed authoring routes.

Grouping (which sources belong together) is deliberately NOT done here — that's a
judgment call the client is better at. The audit unprocessed_source check surfaces
the aged backlog; the client picks a coherent set and passes it here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import corpus_aware, source_coverage
from . import find as find_module
from .governance import egress
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
    pages: list[find_module.ParsedPage] = []
    source_types: list[str] = []
    titles: list[str] = []
    body_parts: list[str] = []
    warnings: list[str] = []
    keep = egress.restricted_release_filter(vault_root)

    for s in sources:
        canonical, _w = normalize_wikilink(s, vault_root, resolver=resolver, strict=False)
        abs_path = vault_root / f"{canonical}.md"
        page = find_module._CACHE.get(abs_path, vault_root)
        if page is None or (keep is not None and not keep(page.rel_path)):
            warnings.append(f"source not found, skipped: {s}")
            continue
        resolved.append(canonical)
        pages.append(page)
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
        "outline_scope": "optional_single_destination",
        "candidate_status": "unreviewed",
        "outline_markdown": _render_outline(note_type, title),
        "semantic_compilation": source_coverage.compilation_guidance(vault_root, resolved),
        "source_excerpts": _source_excerpts(vault_root, resolved, pages, warnings),
        "warnings": warnings,
    }


def _source_excerpts(
    vault_root: Path,
    resolved: list[str],
    pages: list[find_module.ParsedPage],
    warnings: list[str],
) -> list[dict]:
    """Opening-only text and recorded metadata; full-source reading remains necessary.

    The excerpts share `SCAFFOLD_EXCERPT_CHARS`. Room for `SCAFFOLD_EXCERPT_CHARS //
    MIN_EXCERPT_CHARS` excerpts is the most the bound can fund, so the Sources after
    them keep their metadata without an excerpt. Each funded Source gets an equal
    share of what is left, capped at one finding's excerpt. A page the caller may
    not release is left out, as it is from every other derived structure.
    """
    keep = egress.restricted_release_filter(vault_root)
    released = [
        (canonical, page)
        for canonical, page in zip(resolved, pages, strict=True)
        if keep is None or keep(page.rel_path)
    ]
    bound = source_coverage.SCAFFOLD_EXCERPT_CHARS
    funded = min(len(released), bound // source_coverage.MIN_EXCERPT_CHARS)
    remaining = bound
    out: list[dict] = []
    for index, (canonical, page) in enumerate(released):
        share = None
        if index < funded:
            share = min(source_coverage.EXCERPT_CHARS, remaining // (funded - index))
        described = source_coverage.describe(vault_root, page, excerpt_chars=share)
        remaining -= len(described.get("excerpt", ""))
        out.append({"path": canonical, **described})
    if funded < len(released):
        warnings.append(
            f"excerpt omitted for {len(released) - funded} source(s): the scaffold's total "
            f"excerpt bound is {bound} characters; compile fewer sources at once"
        )
    return out


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
    "research-note": ["Question", "Findings", "Relations"],
    "insight": ["Claim", "Why it holds", "Relations"],
    "failure": ["What happened", "Mechanism", "Detection", "Mitigation", "Relations"],
    "pattern": ["Problem", "Solution", "When to use", "When NOT to use", "Relations"],
}


def _render_outline(note_type: str, title: str) -> str:
    sections = _SECTIONS.get(note_type, _SECTIONS["insight"])
    lines = [f"# {title}", ""]
    if note_type == "research-note":
        lines += [
            "> Draft scaffold from propose_compilation. research-note requires a "
            "`project:` — pass it to note(). Replace every unresolved placeholder "
            "before calling note(); untouched placeholders cannot be committed.",
            "> Satisfy semantic authoring by replacing the observation fill-in row, "
            "or by filling a governed rich section with a valid non-empty rich unit.",
            "",
        ]
    else:
        lines += [
            "> Draft scaffold from propose_compilation. Replace every unresolved "
            "placeholder before calling note(); untouched placeholders cannot be committed.",
            "> Satisfy semantic authoring by replacing the observation fill-in row, "
            "or by filling a governed rich section with a valid non-empty rich unit.",
            "",
        ]
    lines += [
        "> Optional scaffold for one note, not a source-wide compilation plan. "
        "Read the full sources and review adjacent candidates before choosing outputs or relations.",
        "",
        "## Observations",
        "",
        "- [ ] Replace this row with `- [category] content #tags (context) ^anchor`.",
        "",
    ]
    for sec in sections:
        lines.append(f"## {sec}")
        lines.append("")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
