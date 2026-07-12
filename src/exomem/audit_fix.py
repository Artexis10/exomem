"""audit_fix: run audit + auto-apply safe fixes.

The lint-finds-but-doesn't-fix model leaves the user (or an LLM) doing the same
mechanical work over and over — canonicalize wikilinks, backfill missing
required fields, rewrite singular project to plural projects. This op
closes that loop for SAFE categories. Risky categories (orphan deletion,
supersession choices, tag rename, source-type inference) stay propose-only
and surface in the report's `proposed` list.

Safe categories (auto-applied):

1. **Canonical wikilink form** — walk every compiled page (Notes/ + Entities/),
   run body + frontmatter wikilinks through `normalize_wikilink`. Drift gets
   rewritten in place. Skips Sources/ + Evidence/ (append-only).
2. **Frontmatter required-field backfill** with safe defaults:
   - `production-log` missing `created`: use `started`, else today
   - `production-log` missing `updated`: use `shipped`, else `created`, else today
   - `research-note`/`insight`/`failure`/`pattern` missing `status`: `active`
   - `research-note`/`insight`/`failure`/`pattern` missing `updated`: use
     `created`, else today
   - `experiment` missing `duration`: compute from `started` + `concluded` if
     both present, else skip
   - `source` missing `captured`: use `created`, else skip
3. **Pattern with singular `project:`** → convert to `projects: [<value>]`
   (the documented frontmatter_compliance finding for cross-project patterns).
4. **Sub-folder index refresh** — fold in `compute_subindex_writes` so counts
   stay current after backfills + canonicalization.

Risky categories (proposed only):

- `broken_wikilink` after canonicalization — residuals are forward refs,
  missing files, or audit limitations. No auto-fix without human intent.
- `orphan_entity` — deletion is too big.
- `unprocessed_source` — compilation is a thinking task.
- `tag_inconsistency` — renames can break user mental models.
- `frontmatter_compliance: tenant set without the expected project` — might be
  a deliberate edge case, so it stays propose-only.
- `source` missing `source_type` — folder→type inference is brittle.

The op is idempotent: running it twice on a clean vault produces no changes.
Atomic writes use the rollback-safe batch infrastructure; caught mid-flip
failures restore pre-write content before raising.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import audit as audit_module
from . import find as find_module
from . import indexes
from .vault import (
    PlannedWrite,
    WikilinkResolver,
    batch_atomic_write,
    kb_root,
    normalize_body_wikilinks,
    normalize_wikilink,
    parse_frontmatter,
    render_wikilink_target,
)


log = logging.getLogger(__name__)


# Sub-folders within Knowledge Base that are append-only or infra and should
# be skipped during the canonicalization sweep.
_SKIP_KB_SUBDIRS = frozenset({
    "Sources", "Evidence", "_Schema", "_trash", "_archive", "_attachments",
})


@dataclass
class FixedFinding:
    """One auto-applied fix. Captured for the report + log entry."""
    category: str
    path: str
    detail: str
    action: str  # human description of what was changed


@dataclass
class AuditFixReport:
    fixed: list[FixedFinding] = field(default_factory=list)
    proposed: list[audit_module.AuditFinding] = field(default_factory=list)
    files_rewritten: int = 0
    summary: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {
            "fixed": [
                {
                    "category": f.category,
                    "path": f.path,
                    "detail": f.detail,
                    "action": f.action,
                }
                for f in self.fixed
            ],
            "proposed": [p.as_dict() for p in self.proposed],
            "files_rewritten": self.files_rewritten,
            "summary": self.summary,
            "dry_run": self.dry_run,
        }


# Same code-block-aware regex as scripts/normalize_vault_wikilinks.py uses,
# but applied at op-time so the writer pulls double duty.
_YAML_WIKILINK = re.compile(r"\[\[([^\]\|\n]+?)(\|[^\]\n]*)?\]\]")


def _normalize_frontmatter_wikilinks(
    fm_text: str, vault_root: Path, resolver: WikilinkResolver
) -> tuple[str, list[str]]:
    """Rewrite every wikilink inside a YAML frontmatter block to canonical form."""
    warnings: list[str] = []
    new_text = fm_text
    matches = list(_YAML_WIKILINK.finditer(fm_text))
    for m in reversed(matches):
        target = m.group(1).strip()
        alias = (m.group(2) or "").strip()
        canonical, warning = normalize_wikilink(
            target, vault_root, resolver=resolver, strict=False
        )
        if warning:
            warnings.append(warning)
            continue
        rendered = render_wikilink_target(canonical, vault_root)
        if rendered == target and not alias:
            continue
        replacement = f"[[{rendered}{alias}]]" if alias else f"[[{rendered}]]"
        if replacement != m.group(0):
            new_text = new_text[: m.start()] + replacement + new_text[m.end():]
    return new_text, warnings


def _walk_compiled_pages(kb: Path):
    """Yield every .md under KB that's compiled material (not Sources/Evidence/infra)."""
    for child in sorted(kb.iterdir()):
        if child.is_dir():
            if child.name in _SKIP_KB_SUBDIRS:
                continue
            yield from _walk_compiled(child)
        elif child.is_file() and child.suffix.lower() == ".md":
            yield child


def _walk_compiled(d: Path):
    for child in sorted(d.iterdir()):
        if child.is_dir():
            if child.name in _SKIP_KB_SUBDIRS:
                continue
            yield from _walk_compiled(child)
        elif child.is_file() and child.suffix.lower() == ".md":
            yield child


# Backfill rules per page type. Each entry: (page_type, field) → callable that
# takes the current frontmatter dict + today's ISO date and returns the
# inferred value, or None to skip.
def _as_iso_date(value: object) -> str | None:
    """Coerce a frontmatter date-ish value to ISO string, or None.

    YAML loads `2026-05-15` as a `datetime.date`, not a string. Templates
    sometimes pass strings. Both should normalize the same.
    """
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def _backfill_value(
    page_type: str, field: str, fm: dict, today_iso: str
) -> tuple[object | None, str | None]:
    """Return (inferred_value, why) or (None, None) if not safely inferable.

    `why` is a one-liner describing the inference for the log entry.
    """
    if page_type == "production-log":
        if field == "created":
            started = _as_iso_date(fm.get("started"))
            if started:
                return started, f"copied from started:{started}"
            return today_iso, "fallback to today"
        if field == "updated":
            shipped = _as_iso_date(fm.get("shipped"))
            if shipped:
                return shipped, f"copied from shipped:{shipped}"
            created = _as_iso_date(fm.get("created"))
            if created:
                return created, f"copied from created:{created}"
            return today_iso, "fallback to today"
    if page_type in ("research-note", "insight", "failure", "pattern"):
        if field == "status":
            return "active", "default for compiled non-experiment/production"
        if field == "updated":
            created = _as_iso_date(fm.get("created"))
            if created:
                return created, f"copied from created:{created}"
            return today_iso, "fallback to today"
    if page_type == "experiment":
        if field == "duration":
            started = _as_iso_date(fm.get("started"))
            concluded = _as_iso_date(fm.get("concluded"))
            if started and concluded:
                try:
                    s = dt.date.fromisoformat(started)
                    c = dt.date.fromisoformat(concluded)
                    days = (c - s).days + 1
                    if days >= 1:
                        return (
                            f"{days} days" if days != 1 else "1 day",
                            f"computed from started:{started} to concluded:{concluded}",
                        )
                except ValueError:
                    return None, None
            return None, None
    if page_type == "source":
        if field == "captured":
            created = _as_iso_date(fm.get("created"))
            if created:
                return created, f"copied from created:{created}"
            return None, None
    return None, None


def _apply_frontmatter_fix(
    fm_text: str, field: str, value: object, today_iso: str
) -> tuple[str, bool]:
    """Insert or update a single frontmatter field, returning (new_text, changed)."""
    pattern = re.compile(rf"^{re.escape(field)}:.*$", re.MULTILINE)
    formatted = (
        f'{field}: "{value}"' if isinstance(value, str) and " " in value
        else f"{field}: {value}"
    )
    if pattern.search(fm_text):
        new_text = pattern.sub(formatted, fm_text, count=1)
        return new_text, new_text != fm_text
    # Append before the closing block — caller wraps with `---` fences again.
    new_text = fm_text.rstrip() + "\n" + formatted
    return new_text, True


def _convert_singular_project_to_plural(fm_text: str) -> tuple[str, str | None]:
    """Rewrite `project: <value>` → `projects: [<value>]` for pattern pages.

    Returns (new_text, old_value_or_none). old_value is None if no change.
    """
    m = re.search(r"^project:\s*(\S.*)$", fm_text, re.MULTILINE)
    if not m:
        return fm_text, None
    value = m.group(1).strip().strip('"').strip("'")
    # Remove the singular line + insert plural form.
    new_text = re.sub(
        r"^project:.*\n?", "", fm_text, count=1, flags=re.MULTILINE
    )
    # Check if `projects:` already exists; if so, merge.
    plural_m = re.search(r"^projects:\s*\[([^\]]*)\]\s*$", new_text, re.MULTILINE)
    if plural_m:
        existing = [s.strip() for s in plural_m.group(1).split(",") if s.strip()]
        if value not in existing:
            existing.append(value)
        new_line = f"projects: [{', '.join(existing)}]"
        new_text = re.sub(
            r"^projects:\s*\[[^\]]*\]\s*$", new_line, new_text,
            count=1, flags=re.MULTILINE,
        )
    else:
        new_text = new_text.rstrip() + f"\nprojects: [{value}]"
    return new_text, value


def audit_fix(
    vault_root: Path,
    *,
    dry_run: bool = False,
    today: dt.date | None = None,
    rebuild_embeddings: bool = False,
) -> AuditFixReport:
    """Run audit + auto-apply safe fixes. Read-only if dry_run=True.

    When `rebuild_embeddings=True`, wipes and rebuilds the vector sidecar
    at `<vault>/Knowledge Base/.embeddings.sqlite` from the current
    markdown state of every compiled page. Use on first run, after a
    machine swap, or whenever the sidecar drifts from disk.
    """
    today = today or dt.date.today()
    today_iso = today.isoformat()
    kb = kb_root(vault_root)

    report = AuditFixReport(dry_run=dry_run)

    # ---- Pass 1: canonicalize wikilinks across all compiled material ----
    resolver = find_module.shared_resolver(vault_root)
    writes: list[PlannedWrite] = []
    pending_paths: list[str] = []

    for md in _walk_compiled_pages(kb):
        try:
            original = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fm, body, fm_text = parse_frontmatter(original)
        new_fm_text = fm_text
        fm_warnings: list[str] = []
        if fm_text is not None:
            new_fm_text, fm_warnings = _normalize_frontmatter_wikilinks(
                fm_text, vault_root, resolver
            )
        new_body, _body_warnings = normalize_body_wikilinks(
            body, vault_root, resolver=resolver
        )

        # ---- Pass 2: frontmatter backfill (only on this file's parsed fm) ----
        page_type = fm.get("type") if isinstance(fm, dict) else None
        if page_type and new_fm_text is not None:
            required = audit_module._REQUIRED_FIELDS_BY_TYPE.get(page_type, ())
            for req_field in required:
                if fm.get(req_field):
                    continue
                inferred, why = _backfill_value(page_type, req_field, fm, today_iso)
                if inferred is None:
                    continue
                new_fm_text, changed = _apply_frontmatter_fix(
                    new_fm_text, req_field, inferred, today_iso
                )
                if changed:
                    rel = md.resolve().relative_to(vault_root.resolve()).as_posix()
                    report.fixed.append(FixedFinding(
                        category="frontmatter_compliance",
                        path=rel,
                        detail=f"{page_type!r} missing required field {req_field!r}",
                        action=f"set {req_field}={inferred!r} ({why})",
                    ))

            # Pattern with singular project → plural projects.
            if page_type == "pattern" and fm.get("project") and not fm.get("projects"):
                new_fm_text2, old_value = _convert_singular_project_to_plural(new_fm_text)
                if old_value is not None and new_fm_text2 != new_fm_text:
                    new_fm_text = new_fm_text2
                    rel = md.resolve().relative_to(vault_root.resolve()).as_posix()
                    report.fixed.append(FixedFinding(
                        category="frontmatter_compliance",
                        path=rel,
                        detail="pattern uses singular `project:` instead of plural `projects:`",
                        action=f"converted to `projects: [{old_value}]`",
                    ))

        # Reconstruct file text.
        if fm_text is not None:
            had_blank_after_fm = bool(re.match(r"^---\n.*?\n---\n\n", original, re.DOTALL))
            body_prefix = "\n" if had_blank_after_fm else ""
            new_text = f"---\n{new_fm_text}\n---\n{body_prefix}{new_body}"
        else:
            new_text = new_body

        if new_text != original:
            try:
                rel = md.resolve().relative_to(vault_root.resolve()).as_posix()
            except ValueError:
                rel = md.as_posix()
            # Wikilink-only canonicalizations weren't logged as fixes above;
            # surface them so the report shows the file got touched.
            if not any(f.path == rel for f in report.fixed):
                report.fixed.append(FixedFinding(
                    category="broken_wikilink",
                    path=rel,
                    detail="non-canonical wikilink(s) in body or frontmatter",
                    action="rewrote to full vault-rooted canonical form",
                ))
            writes.append(PlannedWrite(path=md, content=new_text))
            pending_paths.append(rel)
            report.files_rewritten += 1

    # ---- Pass 3: sub-folder index refresh + top-index counts ----
    top_index_path = kb / "index.md"
    top_text = top_index_path.read_text(encoding="utf-8") if top_index_path.exists() else None
    sub_writes, new_top = indexes.compute_subindex_writes(
        vault_root, top_index_text=top_text
    )
    refresh_writes: list[PlannedWrite] = list(sub_writes)
    if new_top is not None and top_text is not None and new_top != top_text:
        refresh_writes.append(PlannedWrite(path=top_index_path, content=new_top))
    writes.extend(refresh_writes)

    # ---- Apply ----
    if writes and not dry_run:
        BATCH = 100
        for i in range(0, len(writes), BATCH):
            batch_atomic_write(writes[i : i + BATCH], vault_root=vault_root)
        log.info(
            "audit_fix: applied %d file writes (%d compiled, %d index refresh)",
            len(writes), report.files_rewritten, len(refresh_writes),
        )

    # ---- Re-audit (post-fix) to capture remaining proposed-only findings ----
    post_report = audit_module.audit(vault_root)
    for f in post_report.findings:
        # broken_wikilink residuals after canonicalization are forward refs
        # or audit limitations — propose only.
        # Other categories are propose-only by category-level policy.
        report.proposed.append(f)

    # ---- Optional full rebuild of the embedding sidecar ----
    if rebuild_embeddings and not dry_run:
        try:
            from . import embeddings
            count = embeddings.get_embedding_index(vault_root).rebuild_all()
            report.summary["embeddings_chunks"] = count
            log.info("audit_fix: rebuilt embedding sidecar (%d chunks)", count)
        except ImportError as e:
            log.warning(
                "rebuild_embeddings requested but embeddings unavailable: %s", e
            )
        except Exception as e:
            log.exception("rebuild_embeddings failed: %s", e)

    # ---- Summary ----
    report.summary["fixed"] = len(report.fixed)
    report.summary["proposed"] = len(report.proposed)
    by_cat: dict[str, int] = {}
    for f in report.fixed:
        by_cat[f.category] = by_cat.get(f.category, 0) + 1
    for cat, n in by_cat.items():
        report.summary[f"fixed_{cat}"] = n

    return report
