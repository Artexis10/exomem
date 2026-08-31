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
5. **`duplicated_sidecar`** — collapse a media sidecar that accumulated nested
   copies of itself. Keeps the longest surviving `## Extracted text` (for some
   sidecars a re-render blanked the top-level block and the only copy is a nested
   one), leaves frontmatter alone so a still-`pending` sidecar is re-extracted
   normally, and refuses any rewrite that would shorten a transcript. See
   `sidecar_repair`.

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
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import access, indexes, temporal
from . import audit as audit_module
from . import find as find_module
from .vault import (
    PathGuardError,
    PlannedWrite,
    WikilinkResolver,
    batch_atomic_write,
    document_newline,
    kb_root,
    normalize_body_wikilinks,
    normalize_wikilink,
    parse_frontmatter,
    read_guarded_text,
    render_frontmatter_document,
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
    # Pages passed over because they sit in a `readonly` subtree. Reported
    # rather than silently dropped: a caller comparing a dry run against the
    # audit's finding count must be able to account for the difference.
    skipped_readonly: int = 0

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
            "skipped_readonly": self.skipped_readonly,
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
    """Coerce a frontmatter date-ish value to a canonical ISO string, or None.

    YAML loads `2026-05-15` as a `datetime.date` and `2026-05-15T09:12:33Z` as
    a `datetime`; templates sometimes pass either as a string. All normalize to
    the same spelling here.

    Precision is preserved rather than flattened, because every caller is
    copying one recorded field onto another (`created`→`updated`,
    `created`→`captured`). Dropping the time would restate a known instant as a
    vaguer one; inventing a time where there was none would be worse. What this
    must not do is emit `str(datetime)`, whose space separator is not valid
    round-trippable ISO.
    """
    if isinstance(value, dt.date):
        return temporal.stamp(value)
    if isinstance(value, str) and value:
        moment = temporal.parse(value)
        if moment is None:
            return value
        return temporal.stamp(moment.instant or moment.day)
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
                # Whole calendar days, so a timestamped `started` collapses to
                # its day rather than raising out of the computation — plain
                # `date.fromisoformat` rejects any value carrying a time, which
                # would silently abandon the backfill.
                s = temporal.parse(started)
                c = temporal.parse(concluded)
                if s is not None and c is not None:
                    days = (c.day - s.day).days + 1
                    if days >= 1:
                        return (
                            f"{days} days" if days != 1 else "1 day",
                            f"computed from started:{started} to concluded:{concluded}",
                        )
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


def _plan_sidecar_repairs(
    vault_root: Path, report: AuditFixReport
) -> list[PlannedWrite]:
    """Plan one canonical rewrite per media sidecar that nested copies of itself.

    Safe to auto-apply because the rewrite is a pure function of the sidecar and
    is refused unless it keeps at least as much transcript as it found — content
    loss is impossible rather than merely unlikely. Frontmatter is untouched, so a
    sidecar still marked `pending` stays queued for a real re-extraction and the
    recovered text is only the fallback if that fails.
    """
    from . import sidecar_repair

    writes: list[PlannedWrite] = []
    for sidecar in sidecar_repair.iter_media_sidecars(vault_root):
        rel = sidecar.relative_to(vault_root).as_posix()
        # Evidence/ is `append-only`, not `read-write`, and that is the tier these
        # sidecars are supposed to have: the extraction worker rewrites them on
        # every pass through the same write layer. Refuse exactly what the write
        # layer refuses (excluded/readonly) rather than the stricter read-write
        # test the compiled-page passes use, or this would skip every sidecar. The
        # binary itself — the actual evidence — is never touched.
        if access.access_tier(vault_root, rel) in (
            access.TIER_EXCLUDED,
            access.TIER_READONLY,
        ):
            report.skipped_readonly += 1
            continue
        try:
            original, _guard = read_guarded_text(vault_root, sidecar)
        except (OSError, UnicodeDecodeError, PathGuardError):
            continue
        damage = sidecar_repair.analyze(original, sidecar)
        if damage is None:
            continue
        if damage.source_reextraction_required:
            report.proposed.append(
                audit_module.AuditFinding(
                    category="duplicated_sidecar",
                    severity="error",
                    path=rel,
                    detail=(
                        f"{damage.depth} nested copies; source re-extraction required "
                        "(no surviving extracted text)"
                    ),
                    proposed_fix=(
                        "retry media processing to produce a source-derived extraction; "
                        "automatic sidecar repair cannot determine a safe unit"
                    ),
                )
            )
            continue
        repaired = sidecar_repair.repair(original)
        if repaired == original:
            continue
        if not sidecar_repair.repair_is_safe(original, repaired):
            report.proposed.append(
                audit_module.AuditFinding(
                    category="duplicated_sidecar",
                    severity="error",
                    path=rel,
                    detail=(
                        f"{damage.depth} nested copies, but the repair would drop "
                        "transcript — refused, needs a human"
                    ),
                )
            )
            continue
        writes.append(PlannedWrite(path=sidecar, content=repaired))
        report.files_rewritten += 1
        report.fixed.append(
            FixedFinding(
                category="duplicated_sidecar",
                path=rel,
                detail=(
                    f"{damage.depth} nested copies, "
                    f"{damage.distinct_extractions} distinct extraction(s)"
                ),
                action=(
                    f"kept the longest extraction ({damage.recovered_chars:,} chars), "
                    f"dropped the nesting, reclaimed {damage.duplicate_chars:,} chars"
                ),
            )
        )
    return writes


def _compose_writes(*groups: list[PlannedWrite]) -> list[PlannedWrite]:
    """Keep the final planned write for each exact destination.

    Audit passes may independently transform the same index. Later passes are
    explicitly composed over earlier text, so retaining their guarded final
    write is safe. Distinct spellings that collide on a portable filesystem are
    refused here rather than escaping separate ``<=100`` write batches.
    """
    ordered: list[PlannedWrite] = []
    positions: dict[str, int] = {}
    portable_destinations: dict[str, str] = {}
    for write in (write for group in groups for write in group):
        destination = os.path.abspath(write.path)
        portable = "/".join(
            unicodedata.normalize("NFC", part).casefold()
            for part in Path(destination).parts
        )
        existing = portable_destinations.get(portable)
        if existing is not None and existing != destination:
            raise PathGuardError("PATH_GUARD_TARGET", "batch destinations collide")
        portable_destinations[portable] = destination
        if destination in positions:
            ordered[positions[destination]] = write
        else:
            positions[destination] = len(ordered)
            ordered.append(write)
    return ordered


def audit_fix(
    vault_root: Path,
    *,
    dry_run: bool = False,
    today: dt.date | None = None,
    rebuild_embeddings: bool = False,
) -> AuditFixReport:
    """Run audit + auto-apply safe fixes. Read-only if dry_run=True.

    When `rebuild_embeddings=True`, wipes and rebuilds the vector sidecar
    (`.embeddings.sqlite` in the machine-local state root) from the current
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
        # A `readonly` subtree refuses every write with no override, so a
        # canonicalisation planned for one can never be applied. Planning it
        # anyway broke this pass twice: the dry run reported the page as
        # fixed (disagreeing with what a real run could do), and the real run
        # hit WRITE_REFUSED at the write layer and aborted the entire batch —
        # so a handful of readonly pages blocked every writeable page beside
        # them, leaving the fixer permanently unusable on any vault that
        # marks a subtree readonly.
        if access.access_tier(vault_root, md.relative_to(vault_root).as_posix()) != (
            access.TIER_READ_WRITE
        ):
            report.skipped_readonly += 1
            continue
        try:
            original, original_guard = read_guarded_text(vault_root, md)
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
            had_blank_after_fm = bool(
                re.match(r"^---\r?\n.*?\r?\n---\r?\n\r?\n", original, re.DOTALL)
            )
            new_text = render_frontmatter_document(
                new_fm_text,
                new_body,
                newline=document_newline(original),
                blank_line=had_blank_after_fm,
            )
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
            writes.append(PlannedWrite(path=md, content=new_text, guard=original_guard))
            pending_paths.append(rel)
            report.files_rewritten += 1

    # ---- Pass 2b: collapse media sidecars that nested copies of themselves ----
    # Evidence/ is skipped by the compiled-page passes above (append-only), so
    # this walks the media sidecars directly.
    writes.extend(_plan_sidecar_repairs(vault_root, report))

    # ---- Pass 3: sub-folder index refresh + top-index counts ----
    top_index_path = kb / "index.md"
    planned_index_writes = {
        write.path.resolve(): write
        for write in writes
        if write.path.resolve()
        in {
            top_index_path.resolve(),
            (kb / "Sources" / "index.md").resolve(),
            (kb / "Notes" / "index.md").resolve(),
            (kb / "Entities" / "index.md").resolve(),
        }
    }
    top_base_write = planned_index_writes.get(top_index_path.resolve())
    top_guard = top_base_write.guard if top_base_write is not None else None
    if top_base_write is not None:
        top_base = top_base_write.content
    elif top_index_path.exists():
        top_base, top_guard = read_guarded_text(vault_root, top_index_path)
    else:
        top_base = None
    sub_writes, new_top = indexes.compute_subindex_writes(
        vault_root,
        top_index_text=top_base,
        base_writes=planned_index_writes,
    )
    refresh_writes: list[PlannedWrite] = list(sub_writes)
    if new_top is not None and top_base is not None and new_top != top_base:
        refresh_writes.append(
            PlannedWrite(path=top_index_path, content=new_top, guard=top_guard)
        )
    writes = _compose_writes(writes, refresh_writes)

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
        if f not in report.proposed:
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
