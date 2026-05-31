"""Read-only audit of the Knowledge Base. Returns structured findings.

Checks (all read-only; no writes ever):
- `broken_wikilink`: `[[...]]` whose resolved target file doesn't exist.
  Skips wikilinks inside fenced code blocks and inline code spans (so
  `[[:space:]]` regex literals don't false-positive). Bare names resolve
  against filename stems AND frontmatter `title:` (so date-prefixed
  sources with a title match are not flagged).
- `orphan_entity`: file under `Entities/` with no inbound wikilinks from
  anywhere in `Knowledge Base/`
- `unprocessed_source`: `type: source` page whose `ingested_into:` is empty
- `index_drift`: top-level `index.md` Counts disagree with on-disk counts
- `tag_inconsistency`: case/separator variants of the same tag
- `frontmatter_compliance`: per-page-type required-field gaps,
  `tenant:` set without `project: q`, patterns using `project:` (singular)
  instead of `projects:` (plural list)

Audit is the diagnostic counterpart to the writers. Output is a proposal
report; nothing is rewritten without explicit confirmation via the
existing write tools (no auto-fix).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import find as find_module
from . import indexes
from .vault import (
    _mask_code_spans,
    in_append_only_tree,
    kb_root,
    parse_frontmatter,
)


log = logging.getLogger(__name__)

ALL_CATEGORIES: tuple[str, ...] = (
    "broken_wikilink", "orphan_entity", "unprocessed_source",
    "index_drift", "tag_inconsistency", "frontmatter_compliance",
    "unregistered_project_key", "embedding_drift",
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
    # Optional cluster/aging context, surfaced only when set (mirrors
    # find.Hit.signals' omit-when-empty convention so existing findings and
    # the test suite see no shape change). `paths` carries a multi-file group;
    # `meta` carries structured extras like age_days / age_bucket.
    paths: list[str] | None = None
    meta: dict | None = None

    def as_dict(self) -> dict:
        out: dict = {
            "category": self.category,
            "severity": self.severity,
            "path": self.path,
            "detail": self.detail,
            "proposed_fix": self.proposed_fix,
        }
        if self.paths:
            out["paths"] = self.paths
        if self.meta:
            out["meta"] = self.meta
        return out


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
    vault_root: Path,
    *,
    categories: list[str] | None = None,
    today: dt.date | None = None,
) -> AuditReport:
    """Scan the KB and return a structured findings report.

    `categories` filters which checks to run (default: all). Read-only.
    `today` is dependency-injectable for tests (used by unprocessed-source aging).
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
        findings.extend(_check_unprocessed_sources(vault_root, pages, today=today))
    if "index_drift" in selected:
        findings.extend(_check_index_drift(vault_root))
    if "tag_inconsistency" in selected:
        findings.extend(_check_tag_inconsistency(pages))
    if "frontmatter_compliance" in selected:
        findings.extend(_check_frontmatter_compliance(pages))
    if "unregistered_project_key" in selected:
        findings.extend(_check_unregistered_project_keys(vault_root, pages))
    if "embedding_drift" in selected:
        findings.extend(_check_embedding_drift(vault_root))

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
    kb_stripped_paths: set[str] = set()   # KB-relative, no .md
    names_to_paths: dict[str, str] = {}   # bare filename (no ext) → first vault-rel path
    titles_to_paths: dict[str, list[str]] = {}  # lower(frontmatter title) → paths
    for md_path in _walk_vault_md(vault_root):
        try:
            rel = md_path.resolve().relative_to(vault_root.resolve()).as_posix()
        except ValueError:
            continue
        no_ext = rel.removesuffix(".md")
        full_paths.add(no_ext)
        kb_stripped_paths.add(no_ext.removeprefix("Knowledge Base/"))
        names_to_paths.setdefault(md_path.stem, no_ext)
        # Title fallback: lets `[[North-Led Content Manual]]` resolve to a
        # date-prefixed source whose frontmatter `title:` matches.
        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fm, _, _ = parse_frontmatter(text)
        title = fm.get("title") if isinstance(fm, dict) else None
        if isinstance(title, str) and title.strip():
            titles_to_paths.setdefault(title.strip().lower(), []).append(no_ext)

    for page in pages:
        # Skip wikilinks inside fenced code blocks and inline code spans —
        # `[[:space:]]` and similar regex/bash snippets aren't real links.
        body_masked = _mask_code_spans(page.body)
        for match in WIKILINK_PATTERN.finditer(body_masked):
            target = match.group(1).strip()
            if target.endswith("/"):
                # Folder hub link, not a page link.
                continue
            # Strip `#anchor` for resolution — anchors are intra-page jumps,
            # not file paths.
            target_for_resolve = target.split("#", 1)[0].strip()
            if not target_for_resolve:
                continue
            normalized = target_for_resolve.removeprefix("Knowledge Base/").lstrip("/")
            if normalized in kb_stripped_paths:
                continue
            if target_for_resolve.lstrip("/") in full_paths:
                continue
            # Bare-name lookup: Obsidian resolves [[name]] by filename anywhere
            # in the vault. Only attempt if no path separator.
            if "/" not in target_for_resolve:
                if target_for_resolve in names_to_paths:
                    continue
                # Title fallback. Only resolves when unambiguous; ambiguous
                # title matches stay flagged so Hugo can disambiguate.
                title_matches = titles_to_paths.get(target_for_resolve.lower())
                if title_matches and len(title_matches) == 1:
                    continue
            # Attachment links: Obsidian resolves a wikilink carrying an
            # explicit (non-.md) extension to the file on disk of any type
            # (e.g. [[.../scan.pdf]], [[Domains/diagram.png]]). The resolution
            # set above is .md-only and skips _attachments/, so such links
            # false-positived even when the file was present. Probe the
            # filesystem directly. Extension-less links stay note (.md) links,
            # matching Obsidian — a bare [[Foo]] never resolves to Foo.eml.
            suffix = Path(target_for_resolve).suffix.lower()
            if suffix and suffix != ".md":
                rel = target_for_resolve.lstrip("/")
                if (vault_root / rel).exists() or (
                    vault_root / "Knowledge Base" / normalized
                ).exists():
                    continue
            # A broken link inside an append-only tree (Sources/, Evidence/)
            # can't be repaired in place — the containing file is immutable.
            # Surface it at `info` + meta.immutable so it stays out of the
            # actionable `warn` set (you'd fix it in the source body desk-side
            # or accept it as a stray reference in captured material).
            immutable = in_append_only_tree(str(page.rel_path)) is not None
            findings.append(AuditFinding(
                category="broken_wikilink",
                severity="info" if immutable else "warn",
                path=str(page.rel_path),
                detail=(
                    f"Wikilink [[{target}]] points to a file that doesn't exist"
                    + (" (append-only file — not repairable in place)" if immutable else "")
                ),
                proposed_fix=(
                    "Append-only file (Sources/Evidence): the link can't be edited "
                    "in place. Correct it in the source body desk-side, or accept it "
                    "as a stray reference in captured material."
                    if immutable else
                    "Update the link to the correct target, or remove if obsolete. "
                    "Common cause: target was renamed or moved without supersession."
                ),
                meta={"immutable": True} if immutable else None,
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
    vault_root: Path,
    pages: list[find_module.ParsedPage],
    *,
    today: dt.date | None = None,
) -> list[AuditFinding]:
    """Flag sources with empty ingested_into, aged + triaged oldest-first.

    Adds age signal so the backlog can be drained by priority rather than
    treated as an undifferentiated pile: bucket fresh (<30d) / aging (30-90d) /
    stale (>90d), bump severity to `warn` once stale, sort oldest-first, and
    surface age in `meta` so a client can `propose_compilation` the worst rot
    first.
    """
    today = today or dt.date.today()
    rows: list[tuple[int, AuditFinding]] = []  # (age_days for sort, finding)
    for page in pages:
        if page.frontmatter.get("type") != "source":
            continue
        ingested = page.frontmatter.get("ingested_into")
        if not (ingested is None or (isinstance(ingested, list) and len(ingested) == 0)):
            continue

        captured = _parse_fm_date(
            page.frontmatter.get("captured") or page.frontmatter.get("created")
        )
        meta: dict = {}
        age_days: int | None = None
        if captured is not None:
            age_days = max(0, (today - captured).days)
            bucket = (
                "fresh" if age_days < 30 else "aging" if age_days < 90 else "stale"
            )
            meta = {"age_days": age_days, "age_bucket": bucket, "captured": captured.isoformat()}
            severity = "warn" if bucket == "stale" else "info"
            age_phrase = f" ({age_days}d old, {bucket})"
        else:
            bucket = "unknown"
            severity = "info"
            age_phrase = " (capture date unknown)"

        rows.append((
            age_days if age_days is not None else -1,
            AuditFinding(
                category="unprocessed_source",
                severity=severity,
                path=page.rel_path,
                detail=(
                    f"Source has no ingested_into entries — nothing compiled "
                    f"from it yet{age_phrase}"
                ),
                proposed_fix=(
                    "Call `propose_compilation(sources=[this])` for a draft note "
                    "skeleton, then compile via `note` (the back-ref updates "
                    "automatically). Otherwise mark archived or delete."
                ),
                meta=meta or None,
            ),
        ))

    # Oldest first — drain the worst rot first. Capture-unknown (-1) sinks last.
    rows.sort(key=lambda t: t[0], reverse=True)
    return [f for _, f in rows]


def _parse_fm_date(value) -> dt.date | None:
    """Coerce a frontmatter date value (yaml date, datetime, or ISO str) to date."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return dt.date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


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


# ---------------- check: frontmatter_compliance ----------------


# Required fields per page-type (per `_Schema/references/frontmatter.md`).
# Optional / per-type-conditional fields aren't enforced — those depend on
# subtype and can't be validated without parser-level type intent.
_REQUIRED_FIELDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "source": ("type", "source_type", "captured"),
    "research-note": ("type", "project", "status", "created", "updated"),
    "insight": ("type", "status", "created", "updated"),
    "failure": ("type", "status", "created", "updated"),
    "pattern": ("type", "status", "created", "updated"),
    "experiment": ("type", "domain", "status", "created", "updated", "started", "duration"),
    "production-log": ("type", "medium", "status", "created", "updated"),
    "entity": ("type", "entity_type", "status", "created", "updated"),
}


def _check_frontmatter_compliance(
    pages: list[find_module.ParsedPage],
) -> list[AuditFinding]:
    """Surface per-page-type frontmatter problems.

    Three classes of finding:
    - Missing required field for the declared `type:`.
    - `tenant:` set on a non-Q page (the `tenant` field is Q-only).
    - Pattern page with singular `project:` instead of plural `projects:`
      (the convention for cross-project patterns).
    """
    findings: list[AuditFinding] = []
    for page in pages:
        fm = page.frontmatter
        page_type = fm.get("type")
        if not isinstance(page_type, str):
            continue
        required = _REQUIRED_FIELDS_BY_TYPE.get(page_type)
        if required:
            missing = [k for k in required if not fm.get(k)]
            if missing:
                findings.append(AuditFinding(
                    category="frontmatter_compliance",
                    severity="warn",
                    path=page.rel_path,
                    detail=(
                        f"{page_type!r} page missing required frontmatter "
                        f"field(s): {missing}"
                    ),
                    proposed_fix=(
                        f"Add the missing field(s) via `set_frontmatter_field` "
                        f"or `edit`. See `_Schema/references/frontmatter.md` "
                        f"for the per-type required set."
                    ),
                ))
        # tenant: is Q-only.
        if fm.get("tenant") and fm.get("project") != "q":
            findings.append(AuditFinding(
                category="frontmatter_compliance",
                severity="warn",
                path=page.rel_path,
                detail=(
                    f"`tenant: {fm['tenant']!r}` set but `project` is "
                    f"{fm.get('project')!r}, not 'q'. The tenant field is "
                    f"Q-only."
                ),
                proposed_fix=(
                    "Either set `project: q` (if this is a Q-tenant note) "
                    "or remove the `tenant:` field."
                ),
            ))
        # Patterns should use plural `projects:`, not singular `project:`.
        if page_type == "pattern" and fm.get("project") and not fm.get("projects"):
            findings.append(AuditFinding(
                category="frontmatter_compliance",
                severity="info",
                path=page.rel_path,
                detail=(
                    "pattern page uses singular `project:` instead of plural "
                    "`projects:` (the convention for cross-project patterns)."
                ),
                proposed_fix=(
                    "Rename to `projects: [<key>]` (plural list form) via "
                    "`set_frontmatter_field`."
                ),
            ))
    return findings


# ---------------- check: unregistered_project_key ----------------


def _check_unregistered_project_keys(
    vault_root: Path, pages: list[find_module.ParsedPage]
) -> list[AuditFinding]:
    """Flag frontmatter `project:` / `projects:` values not in the registry.

    Catches drift that bypasses `note`/`replace`/`set_frontmatter_field`'s
    auto-register (e.g. pre-typo-guard history, or values landed via the
    Tier 2 `create_file` escape hatch).
    """
    from . import project_keys as project_keys_module

    registry = project_keys_module.load_project_registry(vault_root)
    valid = set(registry.project_to_folder.keys())
    findings: list[AuditFinding] = []
    for page in pages:
        fm = page.frontmatter
        if not isinstance(fm, dict):
            continue
        seen: list[tuple[str, str]] = []  # (field, key)
        single = fm.get("project")
        if isinstance(single, str) and single:
            seen.append(("project", single))
        plural = fm.get("projects")
        if isinstance(plural, list):
            for v in plural:
                if isinstance(v, str) and v:
                    seen.append(("projects", v))
        for field, key in seen:
            if key in valid:
                continue
            findings.append(AuditFinding(
                category="unregistered_project_key",
                severity="warn",
                path=page.rel_path,
                detail=(
                    f"`{field}: {key!r}` not in _Schema/project-keys.yaml "
                    f"registry. Drift from a pre-guard write or a Tier 2 "
                    f"escape-hatch path."
                ),
                proposed_fix=(
                    f"If {key!r} is a typo, fix the frontmatter via "
                    f"`set_frontmatter_field` (the typo guard will surface "
                    f"the intended key). If it's a real new key, hand-add "
                    f"it to _Schema/project-keys.yaml."
                ),
            ))
    return findings


# ---------------- check: embedding_drift ----------------


def _check_embedding_drift(vault_root: Path) -> list[AuditFinding]:
    """Flag sidecar rows whose on-disk file mtime is newer than the row's mtime.

    External Obsidian edits don't trigger the writer hooks, so the vector
    sidecar drifts silently. `audit_fix(rebuild_embeddings=True)` resolves
    all of them in one rebuild — but you want to know it's needed.
    """
    findings: list[AuditFinding] = []
    sidecar = vault_root / "Knowledge Base" / ".embeddings.sqlite"
    if not sidecar.exists():
        return findings
    import sqlite3
    try:
        conn = sqlite3.connect(sidecar)
    except sqlite3.Error:
        return findings
    try:
        try:
            rows = conn.execute(
                "SELECT file_path, MAX(file_mtime) FROM chunks GROUP BY file_path"
            ).fetchall()
        except sqlite3.Error:
            return findings
    finally:
        conn.close()
    seen: set[str] = set()
    for rel_path, row_mtime in rows:
        if not isinstance(rel_path, str) or rel_path in seen:
            continue
        seen.add(rel_path)
        abs_path = vault_root / rel_path
        try:
            disk_mtime = abs_path.stat().st_mtime
        except OSError:
            # File removed in vault but still in sidecar: surface that too.
            findings.append(AuditFinding(
                category="embedding_drift",
                severity="info",
                path=rel_path,
                detail="sidecar row for file no longer on disk",
                proposed_fix=(
                    "Run `audit_fix(rebuild_embeddings=true)` to drop stale rows."
                ),
            ))
            continue
        if disk_mtime > (row_mtime or 0) + 1.0:  # 1s slack for FS jitter
            findings.append(AuditFinding(
                category="embedding_drift",
                severity="info",
                path=rel_path,
                detail=(
                    f"file mtime ({disk_mtime:.0f}) newer than sidecar "
                    f"({(row_mtime or 0):.0f}) — likely external edit."
                ),
                proposed_fix=(
                    "Run `audit_fix(rebuild_embeddings=true)` to refresh."
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
