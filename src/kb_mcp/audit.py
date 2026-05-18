"""Read-only audit of the Knowledge Base. Returns structured findings.

v1 checks (all read-only; no writes ever):
- `broken_wikilink`: `[[...]]` whose resolved target file doesn't exist
- `orphan_entity`: file under `Entities/` with no inbound wikilinks from
  anywhere in `Knowledge Base/`
- `unprocessed_source`: `type: source` page whose `ingested_into:` is empty
- `index_drift`: top-level `index.md` Counts disagree with on-disk counts

Audit is the diagnostic counterpart to `add` and `note`. The intent is for
Claude (or Hugo) to call it, read the findings, and follow up with targeted
fixes via the existing write tools — not auto-fix.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import find as find_module
from . import indexes
from .vault import kb_root


log = logging.getLogger(__name__)

ALL_CATEGORIES: tuple[str, ...] = (
    "broken_wikilink", "orphan_entity", "unprocessed_source", "index_drift",
)

# Matches [[Target]] or [[Target|Alias]]. Target may contain '/' for paths.
WIKILINK_PATTERN = re.compile(r"\[\[([^\]\|\n]+?)(?:\|[^\]\n]*)?\]\]")

# Counts row in index.md. Captures (label, optional-subcategory, count).
# Matches lines like:
#   - Sources: 3 (articles: 1, ...)
#   - Notes (research): 2
#   - Entities (person): 1
_COUNTS_ROW_PATTERN = re.compile(
    r"^- (Sources|Notes|Entities)(?:\s*\(([^)]+)\))?:\s*(\d+)\b",
    re.MULTILINE,
)


@dataclass
class AuditFinding:
    category: str       # one of ALL_CATEGORIES
    severity: str       # "info" | "warn" | "error"
    path: str           # vault-relative path of the affected page (or "index.md")
    detail: str         # one-line human description
    proposed_fix: str | None = None

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "path": self.path,
            "detail": self.detail,
            "proposed_fix": self.proposed_fix,
        }


@dataclass
class AuditReport:
    findings: list[AuditFinding]
    summary: dict[str, int]  # category → count

    def as_dict(self) -> dict:
        return {
            "findings": [f.as_dict() for f in self.findings],
            "summary": self.summary,
        }


def audit(
    vault_root: Path, *, categories: list[str] | None = None
) -> AuditReport:
    """Scan the KB and return a structured findings report.

    `categories` filters which checks to run (default: all). Read-only.
    """
    selected = set(categories) if categories else set(ALL_CATEGORIES)
    invalid = selected - set(ALL_CATEGORIES)
    if invalid:
        raise ValueError(
            f"unknown audit categories: {sorted(invalid)}. "
            f"Valid: {list(ALL_CATEGORIES)}"
        )

    kb = kb_root(vault_root)
    pages = _parse_all(kb)

    findings: list[AuditFinding] = []
    if "broken_wikilink" in selected:
        findings.extend(_check_broken_wikilinks(vault_root, pages))
    if "orphan_entity" in selected:
        findings.extend(_check_orphan_entities(vault_root, pages))
    if "unprocessed_source" in selected:
        findings.extend(_check_unprocessed_sources(vault_root, pages))
    if "index_drift" in selected:
        findings.extend(_check_index_drift(vault_root))

    summary: dict[str, int] = {}
    for f in findings:
        summary[f.category] = summary.get(f.category, 0) + 1

    log.info(
        "audit complete: categories=%s findings=%d summary=%s",
        sorted(selected), len(findings), summary,
    )
    return AuditReport(findings=findings, summary=summary)


# ---------------- vault walk ----------------


def _parse_all(kb: Path) -> list[find_module.ParsedPage]:
    """Walk the KB once, parse every .md, return ParsedPage objects."""
    pages: list[find_module.ParsedPage] = []
    for path in find_module._walk_md(kb):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        page = find_module._parse_page(path, mtime)
        if page is not None:
            pages.append(page)
    return pages


# ---------------- check: broken_wikilink ----------------


def _check_broken_wikilinks(
    vault_root: Path, pages: list[find_module.ParsedPage]
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    kb = kb_root(vault_root)
    # Pre-compute existing page paths (without .md suffix, KB-relative) for fast lookup.
    existing: set[str] = set()
    for page in pages:
        existing.add(_rel_kb_path_no_ext(page.path, vault_root))

    for page in pages:
        for match in WIKILINK_PATTERN.finditer(page.body):
            target = match.group(1).strip()
            # Strip leading "Knowledge Base/" if present; some wikilinks are KB-rooted.
            normalized = target.removeprefix("Knowledge Base/").lstrip("/")
            # Some wikilinks point at folders via trailing /, e.g. [[Sources/Articles/]];
            # skip those — they're folder hubs, not page links.
            if normalized.endswith("/"):
                continue
            if normalized in existing:
                continue
            # Also accept the absolute kb-rooted form on lookup, e.g. callers might
            # write [[Sources/foo]] when target is filed as Sources/foo; our existing
            # set stores the kb-rooted form, so a match would have already hit.
            # If we get here, it's genuinely broken.
            findings.append(AuditFinding(
                category="broken_wikilink",
                severity="warn",
                path=str(page.rel_path),
                detail=f"Wikilink [[{target}]] points to a file that doesn't exist",
                proposed_fix=(
                    "Update the link to the correct target, or remove if obsolete. "
                    "Common cause: target was renamed or moved without supersession."
                ),
            ))
    return findings


# ---------------- check: orphan_entity ----------------


def _check_orphan_entities(
    vault_root: Path, pages: list[find_module.ParsedPage]
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []

    # Collect every wikilink target referenced anywhere in the KB.
    referenced: set[str] = set()
    for page in pages:
        # Don't count self-references and don't count from inside Entities/index.md
        # (those are hub listings, not real "uses").
        if page.rel_path.endswith("/Entities/index.md") or page.rel_path == "Knowledge Base/Entities/index.md":
            continue
        for match in WIKILINK_PATTERN.finditer(page.body):
            target = match.group(1).strip().removeprefix("Knowledge Base/").lstrip("/")
            if target:
                referenced.add(target)
        # Frontmatter wikilinks (sources, related, supersedes, etc.) count too.
        for value in page.frontmatter.values():
            for link in _extract_wikilinks_from_value(value):
                referenced.add(link.removeprefix("Knowledge Base/").lstrip("/"))

    for page in pages:
        if not page.rel_path.startswith("Knowledge Base/Entities/"):
            continue
        if page.path.name == "index.md":
            continue
        self_key = _rel_kb_path_no_ext(page.path, vault_root)
        if self_key in referenced:
            continue
        findings.append(AuditFinding(
            category="orphan_entity",
            severity="info",
            path=page.rel_path,
            detail=f"Entity {self_key!r} has no inbound wikilinks in the KB",
            proposed_fix=(
                "Either link to it from a relevant page (research-note, insight, etc.) "
                "or archive it if no longer relevant."
            ),
        ))
    return findings


def _extract_wikilinks_from_value(value) -> list[str]:
    """Pull `[[...]]` strings out of a frontmatter value (string / list / nested)."""
    out: list[str] = []
    if isinstance(value, str):
        for m in WIKILINK_PATTERN.finditer(value):
            out.append(m.group(1).strip())
    elif isinstance(value, list):
        for item in value:
            out.extend(_extract_wikilinks_from_value(item))
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_extract_wikilinks_from_value(v))
    return out


# ---------------- check: unprocessed_source ----------------


def _check_unprocessed_sources(
    vault_root: Path, pages: list[find_module.ParsedPage]
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for page in pages:
        if page.frontmatter.get("type") != "source":
            continue
        ingested = page.frontmatter.get("ingested_into")
        if ingested is None or (isinstance(ingested, list) and len(ingested) == 0):
            findings.append(AuditFinding(
                category="unprocessed_source",
                severity="info",
                path=page.rel_path,
                detail="Source has no ingested_into entries — nothing compiled from it yet",
                proposed_fix=(
                    "If still relevant, compile a research-note/insight that cites this "
                    "source (the back-ref will update automatically). Otherwise mark as "
                    "archived or delete."
                ),
            ))
    return findings


# ---------------- check: index_drift ----------------


def _check_index_drift(vault_root: Path) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    kb = kb_root(vault_root)
    top_index = kb / "index.md"
    if not top_index.exists():
        return findings

    text = top_index.read_text(encoding="utf-8")
    declared: dict[str, int] = {}
    for m in _COUNTS_ROW_PATTERN.finditer(text):
        label, subcat, count = m.group(1), (m.group(2) or "").strip().lower(), int(m.group(3))
        key = f"{label.lower()}:{subcat}" if subcat else label.lower()
        declared[key] = count

    # Actual counts.
    sources = indexes._count_sources(kb / "Sources")
    notes = indexes._count_notes(kb / "Notes")
    actual: dict[str, int] = {"sources": sum(sources.values())}
    for type_key, n in notes.items():
        actual[f"notes:{type_key}"] = n

    # Compare. Only flag drift for keys present in declared (the index defines what's tracked).
    for key, declared_count in declared.items():
        # The Entities count is harder to verify without per-type folder structure
        # introspection; skip entity drift in v1.
        if key.startswith("entities"):
            continue
        actual_count = actual.get(key)
        if actual_count is None:
            findings.append(AuditFinding(
                category="index_drift",
                severity="warn",
                path="Knowledge Base/index.md",
                detail=(
                    f"Counts row {key!r} declared {declared_count} but the on-disk "
                    "folder doesn't exist"
                ),
                proposed_fix="Remove the row or create the missing folder.",
            ))
            continue
        if actual_count != declared_count:
            findings.append(AuditFinding(
                category="index_drift",
                severity="warn",
                path="Knowledge Base/index.md",
                detail=(
                    f"Counts row {key!r} declared {declared_count}, actual is {actual_count}"
                ),
                proposed_fix=(
                    "Update the Counts line manually (or run an `audit --fix` once "
                    "auto-fix is supported)."
                ),
            ))
    return findings


# ---------------- helpers ----------------


def _rel_kb_path_no_ext(absolute: Path, vault_root: Path) -> str:
    """Return KB-rooted path with .md stripped, e.g. 'Sources/Articles/foo'.

    Matches the form wikilinks use after the leading 'Knowledge Base/' is stripped.
    """
    rel = absolute.resolve().relative_to(vault_root.resolve())
    no_ext = rel.with_suffix("").as_posix()
    return no_ext.removeprefix("Knowledge Base/").lstrip("/")
