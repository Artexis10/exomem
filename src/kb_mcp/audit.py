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
    "broken_wikilink", "orphan_entity", "unprocessed_source",
    "index_drift", "tag_inconsistency",
)

# Matches [[Target]] or [[Target|Alias]]. Target may contain '/' for paths.
WIKILINK_PATTERN = re.compile(r"\[\[([^\]\|\n]+?)(?:\|[^\]\n]*)?\]\]")

# When walking the full vault to build the wikilink-resolution set, skip these.
VAULT_WALK_SKIP_DIRS = frozenset({
    ".obsidian", ".git", ".trash", "_attachments", "_archive", "_trash",
})

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
    pages = _parse_all(kb, vault_root)

    findings: list[AuditFinding] = []
    if "broken_wikilink" in selected:
        findings.extend(_check_broken_wikilinks(vault_root, pages))
    if "orphan_entity" in selected:
        findings.extend(_check_orphan_entities(vault_root, pages))
    if "unprocessed_source" in selected:
        findings.extend(_check_unprocessed_sources(vault_root, pages))
    if "index_drift" in selected:
        findings.extend(_check_index_drift(vault_root))
    if "tag_inconsistency" in selected:
        findings.extend(_check_tag_inconsistency(pages))

    summary: dict[str, int] = {}
    for f in findings:
        summary[f.category] = summary.get(f.category, 0) + 1

    log.info(
        "audit complete: categories=%s findings=%d summary=%s",
        sorted(selected), len(findings), summary,
    )
    return AuditReport(findings=findings, summary=summary)


# ---------------- vault walk ----------------


def _parse_all(kb: Path, vault_root: Path) -> list[find_module.ParsedPage]:
    """Walk the KB once, parse every .md, return ParsedPage objects."""
    pages: list[find_module.ParsedPage] = []
    for path in find_module._walk_md(kb):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        page = find_module._parse_page(path, mtime, vault_root)
        if page is not None:
            pages.append(page)
    return pages


# ---------------- check: broken_wikilink ----------------


def _check_broken_wikilinks(
    vault_root: Path, pages: list[find_module.ParsedPage]
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []

    # Wikilinks in compiled KB notes may legitimately target the curated parent
    # trees (`Cognitive Core/`, `Domains/`, `Products/`, etc., plus `_Schema/`).
    # SKILL.md rule 1 explicitly calls these out as link targets. Build the
    # existence set from the full vault so those don't false-positive.
    full_paths: set[str] = set()          # vault-relative, no .md, e.g. "Products/Q/Strategy"
    kb_stripped_paths: set[str] = set()   # KB-relative, no .md, e.g. "Notes/Insights/foo"
    names_to_paths: dict[str, str] = {}   # bare filename (no ext) → first vault-rel path
    for md_path in _walk_vault_md(vault_root):
        try:
            rel = md_path.resolve().relative_to(vault_root.resolve()).as_posix()
        except ValueError:
            continue
        no_ext = rel.removesuffix(".md")
        full_paths.add(no_ext)
        kb_stripped_paths.add(no_ext.removeprefix("Knowledge Base/"))
        names_to_paths.setdefault(md_path.stem, no_ext)

    for page in pages:
        for match in WIKILINK_PATTERN.finditer(page.body):
            target = match.group(1).strip()
            if target.endswith("/"):
                # Folder hub link, not a page link.
                continue
            normalized = target.removeprefix("Knowledge Base/").lstrip("/")
            if normalized in kb_stripped_paths:
                continue
            if target.lstrip("/") in full_paths:
                continue
            # Bare-name lookup: Obsidian resolves [[name]] by filename anywhere
            # in the vault. Only attempt if no path separator.
            if "/" not in target and target in names_to_paths:
                continue
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


def _walk_vault_md(vault_root: Path):
    """Yield every .md path under the full vault, skipping config/cruft dirs.

    Used for wikilink resolution — broader than find._walk_md which scopes to
    Knowledge Base/ only. Compiled notes can link to curated parent trees
    (per SKILL.md rule 1), so we need a full-vault existence set.
    """
    def walk(d: Path):
        try:
            children = list(d.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir():
                if child.name in VAULT_WALK_SKIP_DIRS:
                    continue
                yield from walk(child)
            elif child.is_file() and child.suffix.lower() == ".md":
                yield child
    yield from walk(vault_root)


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


# ---------------- check: tag_inconsistency ----------------


_TAG_NORMALIZE_PATTERN = re.compile(r"[\s_]+")


def _normalize_tag(tag: str) -> str:
    """Lowercase + collapse whitespace/underscores to dashes for cluster keying.

    `Warning-Letter-Incident`, `warning_letter_incident`, `warning  letter  incident`
    all normalize to `warning-letter-incident`.
    """
    return _TAG_NORMALIZE_PATTERN.sub("-", tag.strip().lower())


def _extract_tags(value) -> list[str]:
    """Pull tags out of a frontmatter `tags:` value (string, list, or nested)."""
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                out.append(item)
    return out


def _check_tag_inconsistency(
    pages: list[find_module.ParsedPage],
) -> list[AuditFinding]:
    """Detect variant clusters: distinct raw tags that normalize to the same
    key (e.g. `warning_letter_incident` vs `warning-letter-incident` vs
    `Warning-Letter-Incident`).

    Only mechanical drift (case + separator) is detected. Semantic
    near-duplicates like `metabolism` vs `metabolic` are NOT flagged — that
    needs human or LLM judgment.

    Singleton tags (used exactly once) are NOT flagged — too noisy in
    practice (a healthy KB has many genuinely-unique one-offs).

    Source pages are immutable per rule 2, so their tags can't be fixed in
    place. The finding's proposed_fix names the compiled-material rewrite path.
    """
    findings: list[AuditFinding] = []

    # raw_tag -> list of pages using it
    raw_to_pages: dict[str, list[str]] = {}
    for page in pages:
        for raw in _extract_tags(page.frontmatter.get("tags")):
            raw_to_pages.setdefault(raw, []).append(page.rel_path)

    # Group raw tags by normalized key.
    norm_to_raws: dict[str, list[str]] = {}
    for raw in raw_to_pages:
        norm_to_raws.setdefault(_normalize_tag(raw), []).append(raw)

    # Variant clusters: normalized keys with >1 raw variant.
    for raws in norm_to_raws.values():
        if len(raws) < 2:
            continue
        # Canonical = the most-used raw variant; ties broken by lex order.
        canonical = max(raws, key=lambda r: (len(raw_to_pages[r]), r))
        for raw in raws:
            if raw == canonical:
                continue
            using_pages = raw_to_pages[raw]
            findings.append(AuditFinding(
                category="tag_inconsistency",
                severity="info",
                path=using_pages[0],  # representative; full list in detail
                detail=(
                    f"Tag {raw!r} (used in {len(using_pages)} page(s)) is a variant "
                    f"of {canonical!r} (used in {len(raw_to_pages[canonical])} page(s)). "
                    f"Using pages: {using_pages}"
                ),
                proposed_fix=(
                    f"Normalize {raw!r} → {canonical!r}. For compiled material, "
                    "rewrite the tag via `replace`. Source pages are immutable per "
                    "SKILL.md rule 2; normalize forward via downstream compiled "
                    "pages that cite them."
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
