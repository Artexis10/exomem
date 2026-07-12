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
- `stale_review`: active compiled conclusion that is old AND rarely surfaced in
  `find` AND low inbound-link degree — a measurement-only review candidate.
  Surfaces it for the reader to judge (keep / supersede / archive); never
  decays, down-ranks, or moves anything (`find` ordering is unchanged).
- `corpus_contradictions`: corpus-wide sweep for pairs of ACTIVE read-write
  COMPILED conclusions whose embeddings sit in the contradiction band
  `[floor, dup_threshold)` — close enough to plausibly restate/refine/contradict
  each other, but not near-duplicates. A PROXIMITY measurement, not a stance
  judgment (cosine can't tell agreement from contradiction); deduped pairs are
  surfaced for the reader to reconcile or supersede. The queue is ORDERED by a
  review priority (cosine + ACT-R dormancy of the pair's notes), same-family
  `Notes/Research/<X>/` architecture noise is demoted, and the surfaced set is
  capped at `EXOMEM_CONTRADICTION_TOP_N` with an explicit omitted count.
  Ordering/capping is measure-only — never auto-acts, never touches `find`.
  No-ops cleanly when embeddings are disabled.

Audit is the diagnostic counterpart to the writers. Output is a proposal
report; nothing is rewritten without explicit confirmation via the
existing write tools (no auto-fix).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import access, indexes, markdown_relations, semantic_blocks
from . import find as find_module
from .kbdir import kb_dirname, kb_prefix
from .vault import (
    _mask_code_spans,
    content_hash,
    find_body_wikilinks,
    in_append_only_tree,
    kb_root,
    parse_frontmatter,
)

log = logging.getLogger(__name__)

ALL_CATEGORIES: tuple[str, ...] = (
    "broken_wikilink", "orphan_entity", "unprocessed_source",
    "index_drift", "tag_inconsistency", "frontmatter_compliance",
    "unregistered_project_key", "embedding_drift", "graph_drift", "reference_identity",
    "relevance_pairs_pending", "stale_review", "corpus_contradictions",
    "relation_debt",
)
OPTIONAL_CATEGORIES: tuple[str, ...] = ("relation_registry",)

# Repo-global feedback-loop logs (written by the running service) + the golden
# query set, used by the relevance_pairs_pending check. Module-level so tests
# can monkeypatch them to point at an isolated fixture.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RELEVANCE_LOGS_DIR = _REPO_ROOT / "logs"
_RELEVANCE_GOLDEN = _REPO_ROOT / "tests" / "golden" / "queries.yaml"

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
    valid_categories = set(ALL_CATEGORIES) | set(OPTIONAL_CATEGORIES)
    invalid = selected - valid_categories
    if invalid:
        raise ValueError(
            f"unknown audit categories: {sorted(invalid)}. "
            f"Valid: {sorted(valid_categories)}"
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
    if "graph_drift" in selected:
        findings.extend(_check_graph_drift(vault_root))
    if "reference_identity" in selected:
        findings.extend(_check_reference_identity(vault_root))
    if "relevance_pairs_pending" in selected:
        findings.extend(_check_relevance_pairs_pending())
    if "stale_review" in selected:
        findings.extend(_check_stale_review(vault_root, pages, today=today))
    if "corpus_contradictions" in selected:
        findings.extend(_check_corpus_contradictions(vault_root, pages, today=today))
    if "relation_registry" in selected:
        findings.extend(_check_relation_registry(vault_root))
    if "relation_debt" in selected:
        findings.extend(_check_relation_debt(vault_root, pages))

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


def _page_signal_version(page: find_module.ParsedPage) -> str:
    """Stable content version for fingerprint-bound review decisions."""
    frontmatter = yaml.safe_dump(page.frontmatter, sort_keys=True, allow_unicode=True)
    return content_hash(frontmatter + "\n" + page.body)[:16]


# ---------------- check: broken_wikilink ----------------


def _check_broken_wikilinks(
    vault_root: Path, pages: list[find_module.ParsedPage]
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []

    # Wikilinks in compiled KB notes may legitimately target curated sibling
    # folders outside Knowledge Base/ (read-only material), plus `_Schema/`.
    # SKILL.md rule 1 explicitly calls these out as link targets. Build the
    # existence set from the full vault so those don't false-positive.
    full_paths: set[str] = set()          # vault-relative, no .md, e.g. "Reference/Strategy"
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
        kb_stripped_paths.add(no_ext.removeprefix(kb_prefix()))
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
            normalized = target_for_resolve.removeprefix(kb_prefix()).lstrip("/")
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
                # title matches stay flagged so the user can disambiguate.
                title_matches = titles_to_paths.get(target_for_resolve.lower())
                if title_matches and len(title_matches) == 1:
                    continue
            # Attachment links: Obsidian resolves a wikilink carrying an
            # explicit (non-.md) extension to the file on disk of any type
            # (e.g. [[.../scan.pdf]], [[Reference/diagram.png]]). The resolution
            # set above is .md-only and skips _attachments/, so such links
            # false-positived even when the file was present. Probe the
            # filesystem directly. Extension-less links stay note (.md) links,
            # matching Obsidian — a bare [[Foo]] never resolves to Foo.eml.
            suffix = Path(target_for_resolve).suffix.lower()
            if suffix and suffix != ".md":
                rel = target_for_resolve.lstrip("/")
                if (vault_root / rel).exists() or (
                    vault_root / kb_dirname() / normalized
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
        if page.rel_path.endswith("/Entities/index.md") or page.rel_path == f"{kb_prefix()}Entities/index.md":
            continue
        for match in WIKILINK_PATTERN.finditer(page.body):
            target = match.group(1).strip().removeprefix(kb_prefix()).lstrip("/")
            if target:
                referenced.add(target)
        # Frontmatter wikilinks (sources, related, supersedes, etc.) count too.
        for value in page.frontmatter.values():
            for link in _extract_wikilinks_from_value(value):
                referenced.add(link.removeprefix(kb_prefix()).lstrip("/"))

    for page in pages:
        if not page.rel_path.startswith(f"{kb_prefix()}Entities/"):
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
        meta: dict = {"signal_version": _page_signal_version(page)}
        age_days: int | None = None
        if captured is not None:
            age_days = max(0, (today - captured).days)
            bucket = (
                "fresh" if age_days < 30 else "aging" if age_days < 90 else "stale"
            )
            meta.update(
                {
                    "age_days": age_days,
                    "age_bucket": bucket,
                    "captured": captured.isoformat(),
                }
            )
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
                meta=meta,
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
    entities = indexes._count_entities(kb / "Entities")
    actual: dict[str, int] = {
        "sources": sum(sources.values()),
        "notes": sum(notes.values()),
        "entities": sum(entities.values()),
    }
    for type_key, n in notes.items():
        actual[f"notes:{type_key}"] = n
    for type_key, n in entities.items():
        actual[f"entities:{type_key}"] = n

    # Compare. Only flag drift for keys present in declared (the index defines what's tracked).
    for key, declared_count in declared.items():
        actual_count = actual.get(key)
        if actual_count is None:
            findings.append(AuditFinding(
                category="index_drift",
                severity="warn",
                path=f"{kb_prefix()}index.md",
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
                path=f"{kb_prefix()}index.md",
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
    near-duplicates like `workflow` vs `workflows` are NOT flagged — that
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
                        "Add the missing field(s) via `set_frontmatter_field` "
                        "or `edit`. See `_Schema/references/frontmatter.md` "
                        "for the per-type required set."
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


# ---------------- check: graph_drift ----------------


def _check_reference_identity(vault_root: Path) -> list[AuditFinding]:
    """Flag duplicate/malformed IDs and reference-sidecar drift."""
    from . import memory_refs

    findings: list[AuditFinding] = []
    for item in memory_refs.scan_issues(vault_root):
        kind = item["kind"]
        value = item["value"]
        findings.append(AuditFinding(
            category="reference_identity",
            severity="error",
            path=item["path"],
            detail=f"{kind} exomem_id: {value}",
            proposed_fix=(
                "Assign a unique UUID in `exomem_id` and run `maintain_memory` "
                "with mode `reconcile`."
            ),
            meta=item,
        ))
    for item in memory_refs.drift(vault_root):
        findings.append(AuditFinding(
            category="reference_identity",
            severity="info",
            path=item["path"],
            detail=item["reason"],
            proposed_fix="Run `maintain_memory` with mode `reconcile`.",
            meta=item,
        ))
    return findings


def _check_graph_drift(vault_root: Path) -> list[AuditFinding]:
    """Flag derived graph sidecar drift. Read-only and disabled-gate aware."""
    from . import epistemic_graph

    findings: list[AuditFinding] = []
    for item in epistemic_graph.graph_drift(vault_root):
        path = str(item.get("path") or kb_prefix())
        reason = str(item.get("reason") or "graph drift")
        findings.append(AuditFinding(
            category="graph_drift",
            severity="info",
            path=path,
            detail=reason,
            proposed_fix="Run `reconcile` to refresh the derived graph sidecar.",
            meta=item,
        ))
    return findings


def _check_relation_registry(vault_root: Path) -> list[AuditFinding]:
    """Opt-in ontology-governance findings; never feeds default attention."""
    from . import memory_schema

    report = memory_schema.validate_relation_registry(vault_root)
    findings: list[AuditFinding] = []
    for item in report["findings"]:
        findings.append(AuditFinding(
            category="relation_registry",
            severity=str(item.get("severity") or "warning"),
            path=str(item.get("path") or kb_prefix()),
            detail=str(item.get("detail") or item.get("code") or "relation registry finding"),
            proposed_fix=(
                "Review corpus evidence with schema_memory(subject='relations') and save "
                "a complete, hash-guarded proposal."
            ),
            meta=item,
        ))
    return findings

# ---------------- check: embedding_drift ----------------


def _check_embedding_drift(vault_root: Path) -> list[AuditFinding]:
    """Flag embedding drift in three forms: (1) sidecar rows whose on-disk file
    mtime is newer than the row (external edit), (2) rows whose file is gone from
    disk, and (3) on-disk embeddable files with NO sidecar row at all — never
    embedded, e.g. created out-of-band in Obsidian / mobile / a filesystem write,
    which bypass the writer's embed hook.

    External edits/creates don't trigger the writer hooks, so the vector sidecar
    drifts silently. `reconcile` heals all three incrementally (it re-embeds
    whatever this flags); `audit_fix(rebuild_embeddings=True)` resolves them in
    one wipe-and-rebuild.
    """
    findings: list[AuditFinding] = []
    sidecar = vault_root / kb_dirname() / ".embeddings.sqlite"
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
                    "Run `reconcile` (or `audit_fix(rebuild_embeddings=true)`) to refresh."
                ),
            ))

    # Files on disk that were NEVER embedded — no sidecar row at all. The scan
    # above only compares existing rows, so out-of-band *creates* (Obsidian /
    # mobile / filesystem writes that bypass the writer's embed hook) stay
    # vector-invisible until caught here. Mirror the embedder's selection
    # (_index_walk + _is_embeddable_path + non-empty chunks) so we never flag a
    # file the rebuild itself would skip — that would be perpetual drift.
    #
    # LOCKSTEP with the index scope (EXOMEM_INDEX_SCOPE): under "vault" the
    # embedder covers the whole vault, so the never-embedded scan must too —
    # else out-of-KB creates would never be flagged and never get reconciled.
    # The "kb" branch is byte-identical to the historical KB-only scan.
    from . import embeddings as embeddings_module
    from . import index_paths

    scope = index_paths.index_scope()
    never_walk = index_paths.iter_index_markdown(vault_root)
    for md in never_walk:
        if not index_paths.is_embeddable_path(md):
            continue
        try:
            rel = md.resolve().relative_to(vault_root.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        if rel in seen:
            continue
        # Vault scope walks trees the KB scan never reached; honor is_indexable
        # so an `excluded` out-of-KB subtree isn't flagged as perpetual drift
        # (the embedder skips it). KB scope keeps its historical behavior.
        if scope == "vault" and not access.is_indexable(vault_root, rel):
            continue
        page = find_module._CACHE.get(md, vault_root)
        if page is None or not embeddings_module.chunk_text(page.title, page.body):
            continue  # empty / no-chunk file — the embedder skips it too
        findings.append(AuditFinding(
            category="embedding_drift",
            severity="info",
            path=rel,
            detail="file has no sidecar row — never embedded (out-of-band create).",
            proposed_fix="Run `reconcile` to embed it incrementally.",
        ))
    return findings


# ---------------- check: relevance_pairs_pending ----------------


def _relevance_canon(path: str) -> str:
    """Delegates to the shared usage primitives (see `usage.py`)."""
    from . import usage
    return usage.canon(path)


def _relevance_read_jsonl(path: Path) -> list[dict]:
    """Delegates to the shared usage primitives (see `usage.py`)."""
    from . import usage
    return usage.read_jsonl(path)


def _relevance_golden_queries(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError:
        return set()
    return {
        e["query"].strip().lower()
        for e in raw
        if isinstance(e, dict) and e.get("query")
    }


def _check_relevance_pairs_pending(
    *,
    logs_dir: Path | None = None,
    golden_path: Path | None = None,
    window_seconds: float = 7200.0,
) -> list[AuditFinding]:
    """Surface real-usage (query -> cited_path) relevance signal not yet in the
    golden set — the retrieval feedback loop's unconfirmed backlog.

    A note/replace write that cites a path shortly after a find() which surfaced
    that path is a weak (query -> path) relevance label (see
    `scripts/derive_relevance_pairs.py`). When such a query isn't yet in
    `tests/golden/queries.yaml`, ranking has measurable signal nobody has
    confirmed. Pure log-join (model-free), so it's safe to run inside audit.

    Gated by `EXOMEM_DISABLE_RELEVANCE_CHECK` (set by the test suite) so the
    per-vault audit stays deterministic regardless of the repo-global logs.
    """
    if os.environ.get("EXOMEM_DISABLE_RELEVANCE_CHECK"):
        return []
    logs_dir = logs_dir or _RELEVANCE_LOGS_DIR
    golden_path = golden_path or _RELEVANCE_GOLDEN

    queries = _relevance_read_jsonl(logs_dir / "queries.jsonl")
    writes = _relevance_read_jsonl(logs_dir / "writes.jsonl")
    if not queries or not writes:
        return []

    existing = _relevance_golden_queries(golden_path)
    new_queries: set[str] = set()
    pairs_in_window = 0
    for w in writes:
        if w.get("tool") not in ("note", "replace"):
            continue
        try:
            w_ts = dt.datetime.fromisoformat(w.get("ts", ""))
        except (ValueError, TypeError):
            continue
        cited = {_relevance_canon(c) for c in (w.get("cited_sources") or []) if c}
        if not cited:
            continue
        for q in queries:
            try:
                q_ts = dt.datetime.fromisoformat(q.get("ts", ""))
            except (ValueError, TypeError):
                continue
            delta = (w_ts - q_ts).total_seconds()
            if not (0 <= delta <= window_seconds):
                continue
            ranked = {
                _relevance_canon(t.get("path", ""))
                for t in (q.get("top_k") or [])
                if t.get("path")
            }
            if cited & ranked:
                pairs_in_window += 1
                ql = (q.get("query") or "").strip()
                if ql and ql.lower() not in existing:
                    new_queries.add(ql)

    if not new_queries:
        return []
    return [AuditFinding(
        category="relevance_pairs_pending",
        severity="info",
        path="logs/queries.jsonl",
        detail=(
            f"{len(new_queries)} query/result pair(s) from real usage are not in "
            "the golden set yet — unconfirmed retrieval feedback signal."
        ),
        proposed_fix=(
            "Run `python scripts/derive_relevance_pairs.py` to review the "
            "proposed (query -> cited_path) labels, paste confirmed ones into "
            "tests/golden/queries.yaml, then re-run `scripts/eval_retrieval.py`."
        ),
        meta={"new_queries": len(new_queries), "pairs_in_window": pairs_in_window},
    )]


# ---------------- check: relation_debt ----------------

_RELATION_DEBT_TYPES = frozenset(
    {
        "research-note",
        "insight",
        "pattern",
        "failure",
        "experiment",
        "production-log",
        "entity",
    }
)


def _check_relation_debt(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
) -> list[AuditFinding]:
    """Surface active compiled pages with no explicit outbound Markdown edges."""
    findings: list[AuditFinding] = []
    for page in pages:
        if page.page_type not in _RELATION_DEBT_TYPES:
            continue
        if page.path.name in ("index.md", "log.md"):
            continue
        if page.status in ("superseded", "archived", "draft", "dropped"):
            continue
        if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
            continue
        stem = page.path.stem.lower()
        if any(stem.endswith(suffix) for suffix in _STALE_SKIP_SLUG_SUFFIXES):
            continue
        if _STALE_SKIP_TAGS & set(page.tags):
            continue

        note_relations = markdown_relations.parse_markdown_relations(page.body)
        block_relations = semantic_blocks.parse_semantic_blocks(
            page.body, validate=False
        )
        typed_count = len(note_relations.relations) + sum(
            len(block.relations) for block in block_relations.blocks
        )
        body_link_count = sum(1 for _ in find_body_wikilinks(page.body))
        if typed_count or body_link_count:
            continue

        findings.append(
            AuditFinding(
                category="relation_debt",
                severity="info",
                path=page.rel_path,
                detail=(
                    "Active compiled page has no outbound Markdown connections; "
                    "its semantic neighbours are not visible as durable graph edges."
                ),
                proposed_fix=(
                    "Review `connect_memory(operation='suggest-relations')` or "
                    "`suggest-links`; accept only meaningful edges, writing note-level "
                    "relations under `## Relations`. Nothing is auto-written."
                ),
                meta={
                    "signal_version": _page_signal_version(page),
                    "typed_relations": typed_count,
                    "body_wikilinks": body_link_count,
                },
            )
        )

    return sorted(findings, key=lambda finding: finding.path)


# ---------------- check: stale_review ----------------

# Staleness review targets living CONCLUSIONS only. Raw sources have their own
# `unprocessed_source` check; time-bounded records (production-log/experiment)
# have lifecycle checks — "is this still true?" doesn't apply to either.
_STALE_REVIEW_TYPES = frozenset(
    {"research-note", "insight", "pattern", "failure", "entity"}
)
# Convention-named hubs/snapshots are EXPECTED to drift (SKILL.md) — never flag.
_STALE_SKIP_SLUG_SUFFIXES = ("-architecture", "-snapshot", "-catalog-snapshot")
_STALE_SKIP_TAGS = frozenset({"hub", "snapshot"})


def _stale_thresholds() -> tuple[int, int, int]:
    """(min_age_days, max_inbound, max_access), env-overridable; bad values fall back.

    Tunable via EXOMEM_STALE_AGE_DAYS / _MAX_INBOUND / _MAX_ACCESS. These are
    gate edges, not weights — the check is a filter, never a score (no
    confidence concept; see SKILL.md rule 5).
    """
    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            log.warning("invalid %s=%r; using %s", name, raw, default)
            return default

    return (
        _int_env("EXOMEM_STALE_AGE_DAYS", 365),
        _int_env("EXOMEM_STALE_MAX_INBOUND", 1),
        _int_env("EXOMEM_STALE_MAX_ACCESS", 1),
    )


def _inbound_degree(pages: list[find_module.ParsedPage]) -> dict[str, int]:
    """Inbound wikilink count per KB-relative target (canonicalised, no .md).

    Mirrors `_check_orphan_entities`' referenced-set scan (body + frontmatter
    wikilinks) but COUNTS, over the already-parsed in-memory pages — no second
    disk walk. A page contributes at most 1 to each target it links (dedup per
    source) and never to itself; the `Entities/index.md` hub listing is skipped
    so catalogue rows don't inflate degree.
    """
    counts: dict[str, int] = {}
    for page in pages:
        if (
            page.rel_path.endswith("/Entities/index.md")
            or page.rel_path == f"{kb_prefix()}Entities/index.md"
        ):
            continue
        self_key = _relevance_canon(page.rel_path)
        targets: set[str] = set()
        for match in WIKILINK_PATTERN.finditer(page.body):
            target = match.group(1).strip().removeprefix(kb_prefix()).lstrip("/")
            if target:
                targets.add(_relevance_canon(target))
        for value in page.frontmatter.values():
            for link in _extract_wikilinks_from_value(value):
                targets.add(
                    _relevance_canon(link.removeprefix(kb_prefix()).lstrip("/"))
                )
        for target in targets:
            if target == self_key:
                continue
            counts[target] = counts.get(target, 0) + 1
    return counts


def _stale_access_counts(logs_dir: Path | None = None) -> dict[str, int] | None:
    """How often each KB path was surfaced by `find` (its appearances across
    `top_k` in logs/queries.jsonl), keyed by canonicalised path.

    Returns None when the access signal is UNAVAILABLE — gated for tests
    (`EXOMEM_DISABLE_RELEVANCE_CHECK`, set by the suite so the per-vault audit is
    deterministic regardless of the repo-global log), or no/empty log. None lets
    the caller DROP the access conjunct rather than fabricate "zero access" from
    missing telemetry. Reuses the relevance-log reader.
    """
    if os.environ.get("EXOMEM_DISABLE_RELEVANCE_CHECK"):
        return None
    logs_dir = logs_dir or _RELEVANCE_LOGS_DIR
    queries = _relevance_read_jsonl(logs_dir / "queries.jsonl")
    if not queries:
        return None
    counts: dict[str, int] = {}
    for q in queries:
        for t in q.get("top_k") or []:
            p = t.get("path")
            if p:
                key = _relevance_canon(p)
                counts[key] = counts.get(key, 0) + 1
    return counts


def _stale_activation_params() -> tuple[float, float, float, float]:
    """(decay d, w_surfaced, w_read, w_cited) for the ACT-R dormancy sort.

    Env-overridable via EXOMEM_STALE_DECAY / _W_SURFACED / _W_READ / _W_CITED;
    bad values fall back. ACT-R canonical decay d=0.5; access weights order
    citation > read > surfacing. These weight the review-queue SORT only — they
    never touch the stale_review gate, and they never touch DEFAULT `find`
    ranking. (The opt-in `find(prefer_used=true)` boost is the sole, explicit
    exception: it consumes the same shared primitives via `usage.py` with its
    own RankingConfig weights — notably w_surfaced=0 — never these.)
    """
    def _float_env(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            log.warning("invalid %s=%r; using %s", name, raw, default)
            return default

    return (
        _float_env("EXOMEM_STALE_DECAY", 0.5),
        _float_env("EXOMEM_STALE_W_SURFACED", 1.0),
        _float_env("EXOMEM_STALE_W_READ", 2.0),
        _float_env("EXOMEM_STALE_W_CITED", 3.0),
    )


def _stale_access_events(
    logs_dir: Path | None = None, today: dt.date | None = None
) -> dict[str, list[tuple[float, float]]] | None:
    """Per-path weighted access events `(delta_days, weight)` for the ACT-R sort.

    Parallel to `_stale_access_counts`, but instead of a surfacing COUNT it
    returns, per canonicalised KB path, the access events feeding the base-level
    activation B = ln(Σ wⱼ·Δtⱼ^(−d)): find-surfacings (queries.jsonl top_k,
    weight w_surfaced), get-reads (reads.jsonl, w_read), and citations
    (writes.jsonl cited_sources, w_cited). delta_days = max((today - ts).days, 1)
    (floored at 1 to dodge the t^−d singularity).

    Returns None when the signal is UNAVAILABLE — gated for tests
    (`EXOMEM_DISABLE_RELEVANCE_CHECK`, set by the suite) or all three logs
    empty — so the caller FALLS BACK to the age-based sort rather than fabricate
    activation. Delegates to the shared primitives in `usage.py` (extracted
    from here verbatim; the parity test guards the move) with audit's own
    env-tunable weights and NO horizon.
    """
    from . import usage
    _, w_surfaced, w_read, w_cited = _stale_activation_params()
    return usage.access_events(
        logs_dir or _RELEVANCE_LOGS_DIR, today,
        w_surfaced=w_surfaced, w_read=w_read, w_cited=w_cited,
    )


def _activation(events: list[tuple[float, float]] | None, d: float) -> float | None:
    """ACT-R base-level activation B = ln(Σ wⱼ·Δtⱼ^(−d)) over weighted access
    events. Higher B = more recently/often accessed = LESS dormant. Returns None
    when there are no events (never accessed) — the caller sorts those to the TOP
    (most dormant). Delegates to `usage.activation`."""
    from . import usage
    return usage.activation(events, d)


def _check_stale_review(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
    *,
    today: dt.date | None = None,
) -> list[AuditFinding]:
    """Surface review candidates: active compiled conclusions that are old AND
    rarely surfaced in `find` AND low inbound-link degree.

    A measurement-only REVIEW QUEUE — it never decays, down-ranks, moves, or
    hides anything (DEFAULT `find` ordering is unchanged; the opt-in
    `find(prefer_used=true)` boost is the sole explicit exception, and it
    lives in `usage.py`/`find.py`, not here); the reader judges keep /
    `replace` (supersede) / archive. All three signals are derived from what the
    KB already records (frontmatter dates, the wikilink graph, the query
    log) — no new sidecar. AND-gated as a filter, not a score (no confidence
    concept). When the access log is unavailable/gated, that conjunct is DROPPED
    (absence is "unknown", never a fabricated zero), so the gate is age AND
    low-inbound. Scope is governed by access tier (read-write) + a conclusion
    type, so it spans the whole writeable KB, not a fixed folder list, and auto-
    excludes the readonly curated trees, append-only Sources/Evidence, hubs/
    snapshots (expected to drift), superseded/archived, and index files.

    Ordering is most-dormant first via ACT-R base-level activation
    (B = ln(Σ wⱼ·Δtⱼ^(−d)) over weighted access events, ascending; never-accessed
    sorts to the top); falls back to oldest-first when the access signal is
    gated/absent. Activation is SORT-ONLY — it never changes who is flagged.
    """
    today = today or dt.date.today()
    min_age_days, max_inbound, max_access = _stale_thresholds()
    degree = _inbound_degree(pages)
    access_counts = _stale_access_counts()  # None when unavailable/gated
    events_map = _stale_access_events(today=today)  # None when unavailable/gated
    d, *_ = _stale_activation_params()

    rows: list[tuple[float, int, AuditFinding]] = []
    for page in pages:
        if page.page_type not in _STALE_REVIEW_TYPES:
            continue
        if page.path.name in ("index.md", "log.md"):
            continue
        if page.status in ("superseded", "archived", "draft"):
            continue
        if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
            continue
        stem = page.path.stem.lower()
        if any(stem.endswith(suffix) for suffix in _STALE_SKIP_SLUG_SUFFIXES):
            continue
        if _STALE_SKIP_TAGS & set(page.tags):
            continue

        updated = _parse_fm_date(
            page.frontmatter.get("updated") or page.frontmatter.get("created")
        )
        if updated is None:
            continue  # no date → can't judge age; don't fabricate one
        age_days = max(0, (today - updated).days)
        if age_days < min_age_days:
            continue

        page_key = _relevance_canon(page.rel_path)
        inbound = degree.get(page_key, 0)
        if inbound > max_inbound:
            continue

        access_count = None if access_counts is None else access_counts.get(page_key, 0)
        if access_count is not None and access_count > max_access:
            continue

        bucket = "aging" if age_days < 2 * min_age_days else "stale"
        access_phrase = (
            "" if access_count is None else f", surfaced {access_count}x in find"
        )
        acts = _activation(events_map.get(page_key) if events_map else None, d)
        finding = AuditFinding(
            category="stale_review",
            severity="info",
            path=page.rel_path,
            detail=(
                f"Possibly stale — {age_days}d since updated, "
                f"{inbound} inbound link(s){access_phrase}. Still true?"
            ),
            proposed_fix=(
                "Surfaced for REVIEW only — not auto-decayed or down-ranked; "
                "`find` ordering is unchanged. Confirm still true (keep), "
                "`replace` (supersede) if newer understanding replaces it, or "
                "archive into a `_archive/` subfolder."
            ),
            meta={
                "signal_version": _page_signal_version(page),
                "age_days": age_days,
                "age_bucket": bucket,
                "inbound_count": inbound,
                "access_count": access_count,  # null when the signal is gated/absent
                "activation": round(acts, 4) if acts is not None else None,
                "access_observations": (
                    len(events_map.get(page_key, [])) if events_map else None
                ),
            },
        )
        # Most-dormant first: activation ASCENDING (never-accessed → -inf at the
        # top), age DESCENDING on ties (older first). Sort-only — the gate above
        # already decided who is flagged.
        sort_act = acts if acts is not None else float("-inf")
        rows.append((sort_act, age_days, finding))

    rows.sort(key=lambda r: (r[0], -r[1]))
    return [f for _, _, f in rows]


# ---------------- check: corpus_contradictions ----------------


def _contradiction_top_n() -> int:
    """Default cap on surfaced contradiction pairs (env EXOMEM_CONTRADICTION_TOP_N).

    Default 40. `0` or a negative value disables the cap (surface every in-band
    pair, no omitted-count summary finding). Bad values log + fall back. This
    caps only the SURFACED review list — it never changes what is measured.
    """
    raw = os.environ.get("EXOMEM_CONTRADICTION_TOP_N")
    if raw is None:
        return 40
    try:
        return int(raw)
    except ValueError:
        log.warning("invalid EXOMEM_CONTRADICTION_TOP_N=%r; using 40", raw)
        return 40


def _contradiction_w_dormancy() -> float:
    """Weight on the pair's ACT-R dormancy in the review priority
    (env EXOMEM_CONTRADICTION_W_DORMANCY).

    priority = cosine + w · pair_dormancy, where pair_dormancy ∈ [0, 1]. Default
    0.5: cosine occupies a narrow band (~[0.5, 0.93)) so a dormant pair earns up
    to +0.5, enough to lift a forgotten close pair over a fresher equally-close
    one while cosine still anchors the base order. Bad values log + fall back.
    Sort-only — it never changes who is eligible or `find` ranking.
    """
    raw = os.environ.get("EXOMEM_CONTRADICTION_W_DORMANCY")
    if raw is None:
        return 0.5
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid EXOMEM_CONTRADICTION_W_DORMANCY=%r; using 0.5", raw)
        return 0.5


def _contradiction_family(rel_path: str) -> str | None:
    """Return the `Notes/Research/<X>` family segment of a KB path, else None.

    The architecture-cluster noise is same-family adjacency: many
    `Notes/Research/<X>/*-architecture` pairs that are expected to sit close. Two
    notes are 'same-family' when they share the `<X>` subfolder directly under
    `Notes/Research/`. Returns `"Notes/Research/<X>"` for such a path (after the
    leading `Knowledge Base/` is stripped) and None for anything outside that
    tree (or directly in it with no `<X>` subfolder).
    """
    stripped = rel_path.removeprefix(kb_prefix()).lstrip("/")
    parts = stripped.split("/")
    # parts = ["Notes", "Research", "<X>", ..., "file.md"] → need an <X> dir
    # before the filename, so at least 4 components.
    if len(parts) >= 4 and parts[0] == "Notes" and parts[1] == "Research":
        return "/".join(parts[:3])
    return None


def _pair_dormancy(
    rel_a: str,
    rel_b: str,
    events_map: dict[str, list[tuple[float, float]]] | None,
    d: float,
) -> float:
    """Most-forgotten endpoint's dormancy ∈ [0, 1] for a contradiction pair.

    Per note, reuse the `stale_review` ACT-R activation B = ln(Σ wⱼ·Δtⱼ^(−d)):
    never-accessed (no events) OR a gated/absent access signal → maximally
    dormant (1.0), never a fabricated "active"; otherwise squash via the logistic
    1/(1+e^B) so a highly-active note → ~0 and a barely-active note → ~1. The
    pair takes the MAX over its two notes — one forgotten endpoint is the review
    trigger ("did I forget I already concluded the opposite?").
    """
    def _one(rel: str) -> float:
        events = events_map.get(_relevance_canon(rel)) if events_map else None
        b = _activation(events, d)
        if b is None:
            return 1.0
        return 1.0 / (1.0 + math.exp(b))

    return max(_one(rel_a), _one(rel_b))


def _is_active_compiled_rw(vault_root: Path, page: find_module.ParsedPage) -> bool:
    """An active, read-write, COMPILED conclusion — the only pages a contradiction
    can actually be reconciled against (edit/replace/supersede). Mirrors the scope
    of `corpus_aware.detect_contradictions` + `_check_stale_review`: a compiled type
    (`find._COMPILED_TYPES`), not an index/log hub, not superseded/archived/draft,
    and in a writeable (read-write) tree (auto-excludes readonly curated trees,
    append-only Sources/Evidence, and excluded subtrees)."""
    if page.page_type not in find_module._COMPILED_TYPES:
        return False
    if page.path.name in ("index.md", "log.md"):
        return False
    if page.status in ("superseded", "archived", "draft"):
        return False
    if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
        return False
    return True


def _claim_level_enabled() -> bool:
    """EXOMEM_CLAIM_LEVEL gate for the claim-level polarity enrichment. Isolated
    so a claims-import problem can't disable the whole sweep."""
    try:
        from . import claims

        return claims.claim_level_enabled()
    except Exception:  # noqa: BLE001
        return False


def _pair_polarity(vault_root: Path, a: str, b: str) -> dict | None:
    """Claim-level polarity for one flagged pair, or None (best-effort).

    Pulls each page's stored/live claim (`claims.claim_text_for_page`) and runs
    `claims.classify_polarity`. Returns `{label, score, method}` or None when a
    claim is missing / the check fails — the audit finding then degrades to the
    proximity-only detail. Never raises into the sweep.
    """
    try:
        from . import claims

        claim_a = claims.claim_text_for_page(vault_root, a)
        claim_b = claims.claim_text_for_page(vault_root, b)
        if not claim_a or not claim_b:
            return None
        res = claims.classify_polarity(claim_a, claim_b)
        return {"label": res.label, "score": res.score, "method": res.method}
    except Exception as e:  # noqa: BLE001
        log.debug("audit pair polarity failed for (%s, %s): %s", a, b, e)
        return None


def _check_corpus_contradictions(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
    *,
    today: dt.date | None = None,
) -> list[AuditFinding]:
    """Corpus-wide contradiction sweep: surface PAIRS of active read-write compiled
    conclusions whose embeddings sit in the band `[floor, dup_threshold)`.

    The audit-time counterpart to `corpus_aware.detect_contradictions` (which fires
    on a single write): instead of one draft vs. the corpus, it sweeps every active
    read-write compiled conclusion against every other and reports the deduped,
    unordered file pairs whose max chunk-cosine lands just below the near-dup
    ceiling. That band is close enough to plausibly restate, refine, OR contradict —
    a PROXIMITY measurement, not a stance judgment (cosine can't separate "X works"
    from "X doesn't"), so each pair is surfaced for the reader to reconcile or
    supersede; nothing is ever auto-acted.

    Reuses the existing vector sidecar (`EmbeddingIndex.all_vectors()`, cached by
    the write generation) — it reads the chunk vectors already on disk and never re-encodes, so the
    sweep is O(eligible_files) matmuls over the sidecar matrix and needs no model
    (works on a CPU/offline box as long as a sidecar exists). The band edges are the
    same knobs the write-time check uses: floor = `corpus_aware._contradiction_floor`,
    ceiling = `corpus_aware._dup_threshold`. An inverted band (floor >= ceiling) is
    disabled. No-ops cleanly (returns []) when embeddings are disabled, the sidecar
    is empty, or numpy/embeddings are unimportable — the same gate the write-time
    check honors, so the fast test suite and torch-less deploys are unaffected.

    The surfaced pairs are ORDERED into a usable review queue (the raw sweep is
    flat cosine-descending and dominated by same-family architecture noise): each
    pair gets a review `priority = cosine + w · pair_dormancy`, where pair_dormancy
    is the most-forgotten endpoint's ACT-R dormancy (reusing the `stale_review`
    activation calc — a dormant note in a close pair is the "is this still true /
    did I forget I concluded the opposite" case). Same-family pairs (both notes in
    one `Notes/Research/<X>/` subfolder) are flagged and sorted last. The surfaced
    set is capped at `EXOMEM_CONTRADICTION_TOP_N` (default 40; `0` = uncapped) with
    an explicit omitted-count summary finding — never a silent truncation. This
    ORDERS/CAPS the review list only; it never mutates a note or touches `find`.
    """
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return []
    from . import corpus_aware

    floor = corpus_aware._contradiction_floor()
    ceiling = corpus_aware._dup_threshold()
    if floor >= ceiling:
        log.warning(
            "contradiction floor (%s) >= dup ceiling (%s); corpus_contradictions "
            "sweep disabled this run",
            floor, ceiling,
        )
        return []
    try:
        import numpy as np

        from . import embeddings as embeddings_module
    except ImportError as e:  # numpy is core, but stay defensive
        log.debug("corpus_contradictions sweep unavailable (%s)", e)
        return []

    idx = embeddings_module.get_embedding_index(vault_root)
    metadata, matrix = idx.all_vectors()  # cached by the sidecar write generation
    if not metadata or matrix.shape[0] == 0:
        return []

    # Both endpoints of a flagged pair must be active read-write compiled.
    eligible: dict[str, find_module.ParsedPage] = {
        page.rel_path: page
        for page in pages
        if _is_active_compiled_rw(vault_root, page)
    }
    if len(eligible) < 2:
        return []

    rows_by_file: dict[str, list[int]] = {}
    for i, (fp, _cidx) in enumerate(metadata):
        rows_by_file.setdefault(fp, []).append(i)

    # max chunk-cosine per deduped unordered file pair, both endpoints eligible.
    pair_cos: dict[tuple[str, str], float] = {}
    for fp, _page in eligible.items():
        rows = rows_by_file.get(fp)
        if not rows:
            continue  # eligible page with no vectors yet (e.g. never embedded)
        sub = matrix[rows]                       # (m, D) this file's chunk vectors
        col_max = (sub @ matrix.T).max(axis=0)   # (N,) best cosine file→each chunk
        in_band = np.nonzero((col_max >= floor) & (col_max < ceiling))[0]
        for j in in_band:
            other_fp = metadata[int(j)][0]
            if other_fp == fp or other_fp not in eligible:
                continue
            score = float(col_max[int(j)])
            a, b = sorted((fp, other_fp))
            key = (a, b)
            if key not in pair_cos or score > pair_cos[key]:
                pair_cos[key] = score

    # Order into a usable review queue: priority = cosine + w · pair_dormancy,
    # same-family pairs demoted, then capped at top-N with an explicit count.
    # Dormancy reuses the stale_review ACT-R calc; gated/absent access → 1.0
    # (maximally dormant) so ordering degrades to cosine, never crashes.
    events_map = _stale_access_events(today=today)  # None when gated/unavailable
    d, *_ = _stale_activation_params()
    w_dormancy = _contradiction_w_dormancy()

    scored: list[tuple[bool, float, str, str, float, float]] = []
    for (a, b), cos in pair_cos.items():
        same_family = (
            _contradiction_family(a) is not None
            and _contradiction_family(a) == _contradiction_family(b)
        )
        dormancy = _pair_dormancy(a, b, events_map, d)
        priority = cos + w_dormancy * dormancy
        scored.append((same_family, priority, a, b, cos, dormancy))

    # Cross-family first; within a bucket by priority desc; path tiebreak.
    scored.sort(key=lambda r: (r[0], -r[1], r[2], r[3]))

    top_n = _contradiction_top_n()
    capped = top_n > 0 and len(scored) > top_n
    shown = scored[:top_n] if capped else scored

    # Sharpen PROXIMITY → POLARITY on the surfaced (already-capped) pairs. Opt-in
    # via EXOMEM_CLAIM_LEVEL; off → `_pair_polarity` returns None so every finding
    # is byte-identical to baseline. Bounded by top_n (shown is already capped).
    claim_level = _claim_level_enabled()

    findings: list[AuditFinding] = []
    for same_family, priority, a, b, cos, dormancy in shown:
        family_note = (
            " Same-family adjacency (likely architecture-cluster noise) — demoted."
            if same_family else ""
        )
        polarity = _pair_polarity(vault_root, a, b) if claim_level else None
        polarity_note = (
            f" Claim-level check: likely {polarity['label'].upper()}"
            f" (via {polarity['method']})."
            if polarity else ""
        )
        meta = {
            "signal_version": content_hash(
                _page_signal_version(eligible[a])
                + "\n"
                + _page_signal_version(eligible[b])
            )[:16],
            "cosine": round(cos, 4),
            "priority": round(priority, 4),
            "dormancy": round(dormancy, 4),
            "same_family": same_family,
        }
        if polarity:
            meta["polarity"] = polarity["label"]
            meta["polarity_score"] = polarity["score"]
            meta["polarity_method"] = polarity["method"]
        findings.append(AuditFinding(
            category="corpus_contradictions",
            severity="info",
            path=a,
            detail=(
                f"Active conclusion overlaps active conclusion {b!r} "
                f"(cosine {round(cos, 4)}) — close enough to restate, refine, or "
                f"contradict. Do they conflict?{family_note}{polarity_note}"
            ),
            proposed_fix=(
                "Surfaced for REVIEW only — a proximity measurement, not an asserted "
                "contradiction. Read both: if they genuinely conflict, `replace` "
                "(supersede) the stale one or `reconcile` them; otherwise leave as-is. "
                "Never auto-acted."
            ),
            paths=[a, b],
            meta=meta,
        ))

    if capped:
        omitted = len(scored) - top_n
        findings.append(AuditFinding(
            category="corpus_contradictions",
            severity="info",
            path=kb_prefix(),
            detail=(
                f"{omitted} more lower-priority/same-family contradiction pair(s) "
                f"not shown (showing top {top_n} of {len(scored)}; raise "
                f"EXOMEM_CONTRADICTION_TOP_N or set it to 0 to see all)."
            ),
            proposed_fix=(
                "Work the surfaced pairs first; raise EXOMEM_CONTRADICTION_TOP_N "
                "(or set it to 0) to surface the remainder. Ordering/capping is "
                "measurement-only — nothing is mutated or auto-acted."
            ),
            meta={"truncated": omitted, "shown": top_n, "total": len(scored)},
        ))
    return findings


# ---------------- helpers ----------------


def _rel_kb_path_no_ext(absolute: Path, vault_root: Path) -> str:
    """Return KB-rooted path with .md stripped, e.g. 'Sources/Articles/foo'.

    Matches the form wikilinks use after the leading 'Knowledge Base/' is stripped.
    """
    rel = absolute.resolve().relative_to(vault_root.resolve())
    no_ext = rel.with_suffix("").as_posix()
    return no_ext.removeprefix(kb_prefix()).lstrip("/")
