"""The `note` MCP tool: create a compiled note with rule-7 writes.

Handles all six compiled page types: research-note, insight, failure, pattern,
experiment, production-log. Discriminated by the `note_type` arg.

Path conventions:
- research-note → Notes/Research/<Project>/<slug>.md (no date prefix; evolves)
- insight       → Notes/Insights/<slug>.md
- failure       → Notes/Failures/<slug>.md
- pattern       → Notes/Patterns/<slug>.md
- experiment    → Notes/Experiments/<domain>/YYYY-MM-<slug>.md (date from `started`)
- production-log → Notes/Productions/<medium>/YYYY-MM-<slug>.md (date from `created`)

Workflow per call:
1. Validate inputs against the note_type's per-type rules.
2. Resolve target path; auto-create domain/medium subfolder if needed.
3. Render frontmatter + body markdown per type.
4. For each source in `sources:`, compute the updated source file with the new
   note's wikilink appended to its ingested_into list.
5. Compute updated top-level index.md (prepend Recent activity bullet, cap-50)
   and log.md (prepend `## [<date>] note | <path>` entry).
6. Batch-atomic-write everything.

Sources/Notes/Entities navigation counts are recomputed with each governed
write. The caller's Unicode title is stored in frontmatter and normalized into
the canonical H1; the optional ASCII slug controls only the new filename.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import corpus_aware, indexes, markdown_relations, memory_refs, semantic_blocks
from . import project_keys as project_keys_module
from .kbdir import kb_prefix
from .vault import (
    InvalidSlugError,
    PlannedWrite,
    WikilinkResolver,
    batch_atomic_write,
    ensure_canonical_h1,
    escape_wikilinks_for_log,
    find_body_wikilinks,
    kb_root,
    normalize_body_wikilinks,
    normalize_wikilink,
    render_wikilink_target,
    resolve_filename_slug,
    rotate_log_if_needed,
    unique_path,
    yaml_scalar,
)

log = logging.getLogger(__name__)


NOTE_TYPES = (
    "research-note", "insight", "failure", "pattern",
    "experiment", "production-log",
)


def _load_keys(vault_root: Path) -> project_keys_module.ProjectRegistry:
    """Load the live project registry (from `_Schema/project-keys.yaml`)."""
    return project_keys_module.load_project_registry(vault_root)

SEVERITY_VALUES = ("minor", "moderate", "serious", "critical")

PATTERN_TYPE_VALUES = (
    "architectural", "workflow", "prompting", "governance", "pedagogical",
)

# Lifecycle status enums per type. research-note/insight/failure/pattern share
# the basic {active, draft} pair; experiment + production-log have richer
# lifecycles per page-types.md.
STATUS_BASIC = ("active", "draft")
STATUS_EXPERIMENT = ("active", "draft", "archived")
STATUS_PRODUCTION = (
    "planned", "recorded", "edited", "published", "reflected", "dropped", "archived",
)


@dataclass
class NoteResult:
    path: str  # vault-relative
    ref: str
    warnings: list[str]
    # Corpus-aware "you might want to link these" hints. Non-binding; the
    # client decides. Omitted from as_dict() when empty so existing callers
    # see no shape change unless a suggestion fires.
    suggestions: list[dict] = field(default_factory=list)
    # Deterministic structural feedback for agents. This is a write-quality
    # checklist over the Markdown shape, not a semantic truth judgment.
    write_feedback: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out: dict = {"path": self.path, "ref": self.ref, "warnings": self.warnings}
        if self.suggestions:
            out["suggestions"] = self.suggestions
        if self.write_feedback:
            out["write_feedback"] = self.write_feedback
        return out


@dataclass
class NoteError(Exception):
    code: str
    missing: list[str]
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "missing": self.missing, "reason": self.reason}


def _build_write_feedback(
    *,
    note_type: str,
    sources_norm: list[str],
    body_clean: str,
    body_warnings: list[str],
    source_warnings: list[str],
    backrefs_planned: int,
    suggestions_count: int,
) -> dict:
    document = semantic_blocks.parse_semantic_blocks(body_clean)
    relation_document = markdown_relations.parse_markdown_relations(body_clean)
    by_kind: dict[str, int] = {}
    for block in document.blocks:
        by_kind[block.type] = by_kind.get(block.type, 0) + 1

    note_relation_lines = {relation.line for relation in relation_document.relations}
    block_relation_lines = {
        relation.line
        for block in document.blocks
        for relation in block.relations
    }
    typed_lines = note_relation_lines | block_relation_lines

    body_targets: list[str] = []
    generic_targets: list[str] = []
    seen_targets: set[str] = set()
    seen_generic_targets: set[str] = set()
    for match in find_body_wikilinks(body_clean):
        target = match.group(0)[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
        if target and target not in seen_targets:
            seen_targets.add(target)
            body_targets.append(target)
        line = body_clean.count("\n", 0, match.start()) + 1
        if target and line not in typed_lines and target not in seen_generic_targets:
            seen_generic_targets.add(target)
            generic_targets.append(target)

    typed_note_relations = len(relation_document.relations)
    typed_block_relations = sum(len(block.relations) for block in document.blocks)
    relation_debt = not (
        typed_note_relations
        or typed_block_relations
        or generic_targets
        or sources_norm
    )

    unresolved = list(source_warnings) + list(body_warnings)
    next_actions: list[str] = []
    if unresolved:
        next_actions.append("resolve or intentionally leave unresolved wikilinks")
    if suggestions_count:
        next_actions.append(
            "review related-page suggestions and add accepted note edges under `## Relations`"
        )
    if not sources_norm:
        next_actions.append("add a source link later if this conclusion came from raw material")
    if relation_debt:
        next_actions.append(
            "review relation debt with `connect_memory(operation='suggest-relations')`"
        )
    if not next_actions:
        next_actions.append("no immediate structural follow-up")

    return {
        "contract": "compiled-note",
        "note_type": note_type,
        "semantic_blocks": {
            "total": len(document.blocks),
            "by_kind": by_kind,
            "errors": [error.to_dict() for error in document.errors],
            "warnings": [warning.to_dict() for warning in document.warnings],
        },
        "sources": {
            "cited": len(sources_norm),
            "backrefs_planned": backrefs_planned,
        },
        "links": {
            "body_wikilinks": len(body_targets),
            "generic_wikilinks": len(generic_targets),
            "source_wikilinks": len(sources_norm),
            "unresolved_count": len(unresolved),
            "unresolved": unresolved,
        },
        "relations": {
            "typed_note": typed_note_relations,
            "typed_block": typed_block_relations,
            "errors": [error.as_dict() for error in relation_document.errors],
            "relation_debt": relation_debt,
        },
        "suggestions": {
            "related_pages": suggestions_count,
        },
        "next_actions": next_actions,
    }


def note(
    vault_root: Path,
    *,
    content: str,
    note_type: str,
    title: str,
    slug: str | None = None,
    project: str | None = None,
    projects: list[str] | None = None,
    sources: list[str] | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    # failure-specific
    severity: str | None = None,
    # pattern-specific
    pattern_type: str | None = None,
    # experiment-specific
    domain: str | None = None,
    started: str | None = None,
    duration: str | None = None,
    hypothesis: str | None = None,
    n: int | None = None,
    concluded: str | None = None,
    # production-log-specific
    medium: str | None = None,
    recorded: str | None = None,
    published: str | None = None,
    host: str | None = None,
    editor: str | None = None,
    suggestions: bool = True,
    today: dt.date | None = None,
    project_category: str | None = None,
    _planned_writes: list[PlannedWrite] | None = None,
) -> NoteResult:
    """Create a compiled note + update indexes/log + back-ref cited sources atomically.

    `today` is dependency-injectable for tests; defaults to dt.date.today().
    """
    try:
        filename_slug, slug_warnings = resolve_filename_slug(title, slug)
    except InvalidSlugError as e:
        raise NoteError(code="INVALID_SLUG", missing=["slug"], reason=str(e)) from e

    # Apply per-type default status if caller didn't specify.
    if status is None:
        if note_type == "production-log":
            status = "planned"
        else:
            status = "active"

    # Auto-register unknown project keys BEFORE validation. This is usually
    # driven through an LLM, so the writer shouldn't force a hand-edit of
    # `_Schema/project-keys.yaml`. If `project` (or any item in
    # `projects`) is a valid slug but not yet registered, we add it to the
    # registry + create the matching folder, then surface a warning so the
    # registration is visible. Invalid slugs fall through to validation
    # which rejects them with a typed error.
    autoregister_warnings: list[str] = []
    candidates: list[str] = []
    if project:
        candidates.append(project)
    if projects:
        candidates.extend(p for p in projects if p)
    if candidates:
        registry = _load_keys(vault_root)
        for cand in candidates:
            if cand in registry.project_to_folder:
                continue
            try:
                _, new_folder, was_new = project_keys_module.register_project_key(
                    vault_root, cand,
                    category=project_category or "uncategorized",
                )
                if was_new:
                    cat_note = (
                        f", category: {project_category!r}"
                        if project_category else ""
                    )
                    autoregister_warnings.append(
                        f"Auto-registered project key {cand!r} (folder: "
                        f"{new_folder!r}{cat_note})."
                    )
            except project_keys_module.ProjectKeyTypoError as e:
                # Surface as a typed validation error — the agent's natural
                # recovery is to re-call with the suggested key.
                raise NoteError(
                    code="PROJECT_KEY_TYPO",
                    missing=["project" if cand == project else "projects"],
                    reason=str(e),
                ) from e
            except ValueError:
                # Invalid slug — fall through to validation which will reject.
                pass

    err = _validate(
        note_type=note_type,
        content=content,
        title=title,
        project=project,
        projects=projects,
        status=status,
        severity=severity,
        pattern_type=pattern_type,
        domain=domain,
        started=started,
        duration=duration,
        medium=medium,
        vault_root=vault_root,
    )
    if err is not None:
        raise NoteError(code=err.code, missing=err.missing, reason=err.reason)

    today = today or dt.date.today()
    date_iso = today.isoformat()
    tags_clean = _clean_tags(tags)
    exomem_id = memory_refs.new_id()

    note_path = _resolve_path(
        vault_root=vault_root,
        note_type=note_type,
        project=project,
        slug=filename_slug,
        domain=domain,
        medium=medium,
        started=started,
        date_iso=date_iso,
    )
    rel_note_no_ext = note_path.relative_to(vault_root).with_suffix("").as_posix()
    new_note_wikilink = f"[[{render_wikilink_target(rel_note_no_ext, vault_root)}]]"

    # Resolver: the process-shared, freshness-checked instance (find's graph-
    # lane cache) — a fresh build reads + YAML-parses the whole vault per
    # write. We register the new note's own path so any body reference back to
    # itself resolves cleanly; index_sync re-syncs the entry from disk after
    # the batch write, and the except-path below purges it on failure.
    from . import find as find_module
    resolver = find_module.shared_resolver(vault_root)
    resolver.add_pending(rel_note_no_ext, title=title)

    sources_norm, source_warnings = _normalize_sources(
        sources, vault_root=vault_root, resolver=resolver
    )

    # Normalize wikilinks inside the body to canonical full form, skipping
    # code blocks. Unresolvable links pass through with a warning so forward
    # refs are still permitted.
    body_clean, body_warnings = normalize_body_wikilinks(
        content, vault_root, resolver=resolver
    )

    # Corpus-aware nudges — best-effort, must NEVER block or roll back the write.
    # Computed PRE-write so the new note isn't in the sidecar yet (no self-match,
    # no 70MB matrix reload). Skipped entirely when embeddings are disabled, so
    # the fast test suite and existing note() tests see no behaviour change.
    corpus_suggestions: list[dict] = []
    dup_warnings: list[str] = []
    contradiction_warnings: list[str] = []
    if not os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        try:
            existing_links: set[str] = set(sources_norm)
            for m in find_body_wikilinks(body_clean):
                inner = m.group(0)[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
                if inner:
                    existing_links.add(inner)
            def _cosines() -> dict[str, float]:
                # One embedding pass, partitioned into the dup band and the
                # contradiction band — the draft is encoded once per write.
                return corpus_aware._best_cosine_per_file(
                    vault_root, title=title, body=body_clean
                )

            if suggestions:
                # The suggestion query (find-class) and the near-dup sweep
                # (draft embed + sidecar KNN) are independent read-only
                # passes — overlap them. The server already encodes from
                # several threads (worker pool, watcher), so this introduces
                # no new concurrency class.
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fut_sugg = pool.submit(
                        corpus_aware.suggest_related,
                        vault_root, title=title, body=body_clean,
                        self_path=rel_note_no_ext, existing_links=existing_links,
                        limit=6,
                    )
                    fut_cos = pool.submit(_cosines)
                    cosines = fut_cos.result()
                    corpus_suggestions = [s.as_dict() for s in fut_sugg.result()]
            else:
                # suggestions=False skips ONLY the related-links pass. The
                # near-dup/contradiction sweep below is a dedupe GUARDRAIL
                # (SKILL rule: prefer edit/replace over a parallel page) and
                # stays on in every mode.
                cosines = _cosines()
            dup_warnings = [
                corpus_aware.dup_warning(c)
                for c in corpus_aware.detect_duplicates(
                    vault_root, title=title, body=body_clean,
                    self_path=rel_note_no_ext, types_filter=[note_type],
                    precomputed=cosines,
                )
            ]
            contradiction_warnings = [
                corpus_aware.overlap_warning(c)
                for c in corpus_aware.detect_contradictions(
                    vault_root, title=title, body=body_clean,
                    self_path=rel_note_no_ext, precomputed=cosines,
                )
            ]
        except Exception as e:  # noqa: BLE001 — nudges never break a write
            log.debug("corpus-aware nudges failed (non-fatal): %s", e)

    note_md = _render_note(
        note_type=note_type,
        title=title,
        project=project,
        projects=projects,
        status=status,
        date_iso=date_iso,
        sources=[render_wikilink_target(source, vault_root) for source in sources_norm],
        tags=tags_clean,
        content=body_clean,
        severity=severity,
        pattern_type=pattern_type,
        domain=domain,
        started=started,
        duration=duration,
        hypothesis=hypothesis,
        n=n,
        concluded=concluded,
        medium=medium,
        recorded=recorded,
        published=published,
        host=host,
        editor=editor,
        exomem_id=exomem_id,
    )

    kb = kb_root(vault_root)
    write_feedback = _build_write_feedback(
        note_type=note_type,
        sources_norm=sources_norm,
        body_clean=body_clean,
        body_warnings=body_warnings,
        source_warnings=source_warnings,
        backrefs_planned=0,
        suggestions_count=len(corpus_suggestions),
    )
    writes: list[PlannedWrite] = [PlannedWrite(path=note_path, content=note_md)]
    warnings: list[str] = (
        list(autoregister_warnings)
        + list(slug_warnings)
        + list(source_warnings)
        + list(body_warnings)
        + list(dup_warnings)
        + list(contradiction_warnings)
    )
    # Back-refs: append the new note's wikilink to each cited source's ingested_into.
    backrefs_planned = 0
    for src in sources_norm:
        src_path = _resolve_source_path(vault_root, src)
        if src_path is None or not src_path.exists():
            warnings.append(
                f"source not found, ingested_into back-ref skipped: {src}"
            )
            continue
        original = src_path.read_text(encoding="utf-8")
        updated = _append_to_ingested_into(original, new_note_wikilink)
        if updated != original:
            writes.append(PlannedWrite(path=src_path, content=updated))
            backrefs_planned += 1
        else:
            warnings.append(
                f"could not locate ingested_into: field in {src}, back-ref skipped"
            )

    # Top index.md Recent activity + log.md entry.
    top_index = kb / "index.md"
    log_file = kb / "log.md"
    activity_summary = _activity_summary(
        rel_note_no_ext=rel_note_no_ext,
        title=title,
        note_type=note_type,
        project=project,
        projects=projects,
        severity=severity,
        pattern_type=pattern_type,
        domain=domain,
        medium=medium,
        status=status,
    )
    log_body = _log_entry_body(
        note_type=note_type,
        title=title,
        project=project,
        projects=projects,
        tags=tags_clean,
        sources=sources_norm,
        severity=severity,
        pattern_type=pattern_type,
        domain=domain,
        medium=medium,
        status=status,
        started=started,
        duration=duration,
    )

    if top_index.exists():
        new_top, trim_note = indexes._prepend_recent_activity(
            top_index.read_text(encoding="utf-8"),
            date_iso=date_iso,
            summary=activity_summary,
        )
        # Refresh sub-folder indexes + the top-index Counts rows for Notes/
        # Entities. Pass the new note's path so counts reflect post-write
        # state without a second disk scan.
        sub_writes, new_top_with_counts = indexes.compute_subindex_writes(
            vault_root,
            top_index_text=new_top,
            pending_paths=[rel_note_no_ext],
        )
        if new_top_with_counts is not None:
            new_top = new_top_with_counts
        # trim_note still flows into the log entry body below — log.md is the
        # paper trail for cap-50 displacement (SKILL.md trim discipline).
        writes.append(PlannedWrite(path=top_index, content=new_top))
        writes.extend(sub_writes)
    else:
        warnings.append(f"{kb_prefix()}index.md missing; skipped Recent activity bump")

    if log_file.exists():
        full_body = log_body + (
            f"\n\n{trim_note}" if (top_index.exists() and trim_note) else ""
        )
        new_log = _prepend_log_entry(
            log_file.read_text(encoding="utf-8"),
            date_iso=date_iso,
            verb="note",
            rel_path=rel_note_no_ext,
            body=full_body,
        )
        writes.append(PlannedWrite(path=log_file, content=new_log))
    else:
        warnings.append(f"{kb_prefix()}log.md missing; skipped log entry")

    if _planned_writes is None:
        try:
            batch_atomic_write(writes, vault_root=vault_root)
        except Exception as e:
            log.exception("partial write during note(); some files may be updated")
            warnings.append(f"partial write — reconcile on desktop: {e}")
            # Purge this write's add_pending registration from the SHARED resolver
            # — the note never landed, and a phantom entry would resolve wikilinks
            # to a nonexistent page until the next full rebuild.
            try:
                find_module.on_resolver_files_changed(
                    vault_root, [rel_note_no_ext + ".md"], []
                )
            except Exception:  # noqa: BLE001 — purge is best-effort cleanup
                log.debug("resolver pending-purge failed", exc_info=True)
            raise

        rotate_note = rotate_log_if_needed(vault_root)
        if rotate_note:
            warnings.append(rotate_note)
    else:
        # Internal composition seam for replace(): preserve note()'s exact
        # write plan while letting the supersession pointers join the same batch.
        _planned_writes.extend(writes)

    write_feedback["sources"]["backrefs_planned"] = backrefs_planned

    return NoteResult(
        path=note_path.relative_to(vault_root).as_posix(),
        ref=memory_refs.memory_ref(exomem_id),
        warnings=warnings,
        suggestions=corpus_suggestions,
        write_feedback=write_feedback,
    )


# ---------------- validation ----------------


@dataclass
class _Err:
    code: str
    missing: list[str]
    reason: str


_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate(
    *,
    note_type: str,
    content: str,
    title: str,
    project: str | None,
    projects: list[str] | None,
    status: str,
    severity: str | None,
    pattern_type: str | None,
    domain: str | None,
    started: str | None,
    duration: str | None,
    medium: str | None,
    vault_root: Path,
) -> _Err | None:
    missing: list[str] = []
    reasons: list[str] = []

    if note_type not in NOTE_TYPES:
        return _Err(
            code="INVALID_NOTE",
            missing=["note_type"],
            reason=(
                f"note_type {note_type!r} is not supported. "
                f"Valid: {list(NOTE_TYPES)}."
            ),
        )
    if not content or not content.strip():
        missing.append("content")
        reasons.append("content is empty")
    if not title or not title.strip():
        missing.append("title")
        reasons.append("title is empty")

    # Per-type status enum.
    if note_type == "experiment":
        if status not in STATUS_EXPERIMENT:
            return _Err(
                code="INVALID_NOTE",
                missing=["status"],
                reason=(
                    f"experiment status must be one of {list(STATUS_EXPERIMENT)}, "
                    f"got {status!r}"
                ),
            )
    elif note_type == "production-log":
        if status not in STATUS_PRODUCTION:
            return _Err(
                code="INVALID_NOTE",
                missing=["status"],
                reason=(
                    f"production-log status must be one of {list(STATUS_PRODUCTION)}, "
                    f"got {status!r}"
                ),
            )
    else:
        if status not in STATUS_BASIC:
            missing.append("status")
            reasons.append(f"status must be 'active' or 'draft', got {status!r}")

    registry = _load_keys(vault_root)
    valid_keys = registry.project_to_folder
    if note_type == "research-note":
        if not project:
            missing.append("project")
            reasons.append("project is required for research-note")
        elif project not in valid_keys:
            # Reaching here means auto-register failed (invalid slug shape).
            return _Err(
                code="INVALID_NOTE",
                missing=["project"],
                reason=(
                    f"project {project!r} is not a valid slug "
                    f"(must be lowercase letters/digits/dashes). "
                    f"Existing keys: {sorted(valid_keys)}"
                ),
            )
        if projects:
            reasons.append(
                "research-note uses singular `project`, not `projects`; "
                "the `projects` arg was ignored"
            )
    elif note_type in ("insight", "failure", "pattern"):
        if project:
            reasons.append(
                f"{note_type} uses plural `projects`, not `project`; "
                "the `project` arg was ignored"
            )
        if projects:
            invalid = [p for p in projects if p not in valid_keys]
            if invalid:
                return _Err(
                    code="INVALID_NOTE",
                    missing=["projects"],
                    reason=(
                        f"projects contains keys that aren't valid slugs "
                        f"and couldn't be auto-registered: {invalid}. "
                        f"Existing keys: {sorted(valid_keys)}"
                    ),
                )
        if note_type == "failure" and severity is not None and severity not in SEVERITY_VALUES:
            return _Err(
                code="INVALID_NOTE",
                missing=["severity"],
                reason=(
                    f"severity {severity!r} not valid. Valid: {list(SEVERITY_VALUES)}"
                ),
            )
        if note_type == "pattern" and pattern_type is not None and pattern_type not in PATTERN_TYPE_VALUES:
            return _Err(
                code="INVALID_NOTE",
                missing=["pattern_type"],
                reason=(
                    f"pattern_type {pattern_type!r} not valid. "
                    f"Valid: {list(PATTERN_TYPE_VALUES)}"
                ),
            )
    elif note_type == "experiment":
        if not domain:
            missing.append("domain")
            reasons.append("domain is required for experiment (becomes the subfolder)")
        if not started:
            missing.append("started")
            reasons.append("started (YYYY-MM-DD) is required for experiment")
        elif not _ISO_DATE_PATTERN.match(started):
            return _Err(
                code="INVALID_NOTE",
                missing=["started"],
                reason=f"started must be YYYY-MM-DD, got {started!r}",
            )
        if not duration:
            missing.append("duration")
            reasons.append(
                "duration is required for experiment (e.g. '30 days', '2 weeks', 'ongoing')"
            )
    elif note_type == "production-log":
        if not medium:
            missing.append("medium")
            reasons.append("medium is required for production-log (becomes the subfolder)")

    if missing:
        return _Err(
            code="INVALID_NOTE",
            missing=missing,
            reason="; ".join(reasons),
        )
    return None


# ---------------- path / slug ----------------


def _resolve_path(
    *,
    vault_root: Path,
    note_type: str,
    project: str | None,
    slug: str,
    domain: str | None,
    medium: str | None,
    started: str | None,
    date_iso: str,
) -> Path:
    kb = kb_root(vault_root)
    if note_type == "research-note":
        assert project is not None  # validated above
        # Use live registry so auto-registered keys resolve to their folder.
        registry = _load_keys(vault_root)
        folder_name = registry.folder_for(project) or project.capitalize()
        folder = kb / "Notes" / "Research" / folder_name
        stem = slug
    elif note_type == "insight":
        folder = kb / "Notes" / "Insights"
        stem = slug
    elif note_type == "failure":
        folder = kb / "Notes" / "Failures"
        stem = slug
    elif note_type == "pattern":
        folder = kb / "Notes" / "Patterns"
        stem = slug
    elif note_type == "experiment":
        assert domain and started  # validated above
        folder = kb / "Notes" / "Experiments" / _domain_folder(domain)
        stem = f"{started[:7]}-{slug}"  # YYYY-MM-<slug>
    elif note_type == "production-log":
        assert medium  # validated above
        folder = kb / "Notes" / "Productions" / _medium_folder(medium)
        stem = f"{date_iso[:7]}-{slug}"  # YYYY-MM-<slug>
    else:  # pragma: no cover — validation guards this
        raise ValueError(f"unhandled note_type: {note_type}")
    folder.mkdir(parents=True, exist_ok=True)
    return unique_path(folder, stem)


def _domain_folder(domain: str) -> str:
    """Lowercase domain to subfolder name. Sanitize to avoid path traversal."""
    safe = re.sub(r"[^a-z0-9-]", "", domain.strip().lower())
    return safe or "misc"


def _medium_folder(medium: str) -> str:
    """Title-case medium per fixture convention (Posts, Articles, etc.)."""
    safe = re.sub(r"[^a-zA-Z0-9-]", "", medium.strip())
    return safe.title() if safe else "Misc"


# ---------------- render ----------------


def _render_note(
    *,
    note_type: str,
    title: str,
    project: str | None,
    projects: list[str] | None,
    status: str,
    date_iso: str,
    sources: list[str],
    tags: list[str],
    content: str,
    severity: str | None = None,
    pattern_type: str | None = None,
    domain: str | None = None,
    started: str | None = None,
    duration: str | None = None,
    hypothesis: str | None = None,
    n: int | None = None,
    concluded: str | None = None,
    medium: str | None = None,
    recorded: str | None = None,
    published: str | None = None,
    host: str | None = None,
    editor: str | None = None,
    exomem_id: str,
) -> str:
    lines = ["---"]
    lines.append(f"type: {note_type}")
    lines.append(f"exomem_id: {exomem_id}")
    lines.append(f"title: {yaml_scalar(title.strip())}")

    # Type-specific required fields, ordered per fixture convention.
    if note_type == "research-note":
        lines.append(f"project: {project}")
    elif note_type == "experiment":
        lines.append(f"domain: {domain}")
    elif note_type == "production-log":
        lines.append(f"medium: {medium}")

    lines.append(f"status: {status}")
    lines.append(f"created: {date_iso}")
    lines.append(f"updated: {date_iso}")

    # Experiment-specific dates + numerics, placed near the top so the
    # temporal scaffolding reads naturally before the body content.
    if note_type == "experiment":
        lines.append(f"started: {started}")
        lines.append(f"duration: \"{duration}\"")
        if concluded:
            lines.append(f"concluded: {concluded}")
        lines.append(f"n: {n if n is not None else 1}")
        if hypothesis:
            lines.append(f"hypothesis: \"{hypothesis}\"")
    elif note_type == "production-log":
        if recorded:
            lines.append(f"recorded: {recorded}")
        lines.append(f"published: {published if published else 'null'}")
        if host:
            lines.append(f"host: \"{host}\"")
        if editor:
            lines.append(f"editor: \"{editor}\"")

    # Sources block (shared by all types).
    if sources:
        lines.append("sources:")
        for s in sources:
            lines.append(f"  - \"[[{s}]]\"")
    else:
        lines.append("sources: []")

    # Plural projects: insight, failure, pattern, production-log.
    if note_type in ("insight", "failure", "pattern", "production-log") and projects:
        lines.append("projects: [" + ", ".join(projects) + "]")

    # Optional categorical fields.
    if note_type == "failure" and severity:
        lines.append(f"severity: {severity}")
    if note_type == "pattern" and pattern_type:
        lines.append(f"pattern_type: {pattern_type}")

    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    else:
        lines.append("tags: []")
    lines.append("---")
    lines.append("")
    lines.append(ensure_canonical_h1(content, title))
    lines.append("")
    return "\n".join(lines)


# ---------------- ingested_into back-ref ----------------


_INGESTED_FLOW_PATTERN = re.compile(
    r"^(ingested_into:\s*)(\[\s*\]|\[[^\]\n]*\])\s*$", re.MULTILINE
)
_INGESTED_BLOCK_HEADER_PATTERN = re.compile(
    r"^(ingested_into:)\s*$", re.MULTILINE
)


def _append_to_ingested_into(text: str, new_wikilink: str) -> str:
    """Append `new_wikilink` (e.g. "[[Knowledge Base/Notes/...]]") to the
    ingested_into: list in a source file's frontmatter. Idempotent.

    Handles two YAML shapes:
    - Flow:  `ingested_into: []`  or  `ingested_into: ["[[A]]"]`
    - Block: `ingested_into:\n  - "[[A]]"\n  - "[[B]]"`

    Empty flow `[]` is converted to block form on first append. Returns the
    text unchanged if no match is found (caller surfaces this as a warning).
    """
    if new_wikilink in text:
        return text  # already linked; idempotent

    flow_match = _INGESTED_FLOW_PATTERN.search(text)
    if flow_match:
        prefix, current = flow_match.group(1), flow_match.group(2).strip()
        inner = current.strip("[]").strip()
        items: list[str]
        if not inner:
            items = []
        else:
            items = [s.strip().strip('"').strip("'") for s in inner.split(",")]
        items.append(new_wikilink)
        # Convert to block form for readability (and to keep wikilink quoting clean).
        block_lines = [prefix.rstrip().rstrip(":") + ":"] + [
            f'  - "{item}"' for item in items
        ]
        replacement = "\n".join(block_lines)
        return text[: flow_match.start()] + replacement + text[flow_match.end():]

    block_match = _INGESTED_BLOCK_HEADER_PATTERN.search(text)
    if block_match:
        # Find the end of the block list (first non-`  - ...` line or blank).
        body_start = block_match.end()
        # Walk forward line-by-line to find where the block ends.
        cursor = body_start
        while cursor < len(text):
            line_end = text.find("\n", cursor + 1)
            if line_end == -1:
                line_end = len(text)
            line = text[cursor + 1 : line_end] if text[cursor] == "\n" else text[cursor:line_end]
            stripped = line.lstrip()
            if stripped.startswith("- "):
                cursor = line_end
            else:
                break
        insertion = f'\n  - "{new_wikilink}"'
        return text[:cursor] + insertion + text[cursor:]

    return text  # no match, signal caller


# ---------------- sources normalization & resolution ----------------


def _normalize_sources(
    sources: list[str] | None,
    *,
    vault_root: Path,
    resolver: WikilinkResolver,
) -> tuple[list[str], list[str]]:
    """Canonicalize each source wikilink to full vault-rooted form.

    Returns (canonical_sources, warnings). Resolvable inputs become
    `Knowledge Base/<path>` (no `.md`). Unresolvable inputs are kept in the
    caller-supplied form (with `Knowledge Base/` prepended if missing) and
    surfaced as a warning — sources are sometimes added before the source
    file lands (e.g. compile-then-capture order), so we don't refuse.
    """
    if not sources:
        return [], []
    out: list[str] = []
    seen: set[str] = set()
    warnings: list[str] = []
    for s in sources:
        s = (s or "").strip()
        if not s:
            continue
        canonical, warning = normalize_wikilink(
            s, vault_root, resolver=resolver, strict=False
        )
        if warning:
            warnings.append(warning)
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out, warnings


def _resolve_source_path(vault_root: Path, kb_relative: str) -> Path | None:
    """Resolve a 'Knowledge Base/Sources/Articles/<slug>' wikilink to an on-disk
    .md path, or None if the path escapes the vault."""
    rel = kb_relative.removeprefix(kb_prefix())
    candidate = (kb_root(vault_root) / rel).with_suffix(".md")
    try:
        candidate.resolve().relative_to(vault_root.resolve())
    except ValueError:
        return None
    return candidate


# ---------------- log + activity helpers ----------------


def _prepend_log_entry(
    text: str, *, date_iso: str, verb: str, rel_path: str, body: str
) -> str:
    """Insert `## [<date>] <verb> | <kb-relative-path>` entry just after the
    log's `---` separator (newest entries at top)."""
    title = rel_path.replace(kb_prefix(), "", 1)
    new_entry = f"## [{date_iso}] {verb} | {title}\n\n{escape_wikilinks_for_log(body)}\n"
    sep_idx = text.find(indexes.LOG_SEPARATOR)
    if sep_idx == -1:
        return text.rstrip() + "\n\n" + new_entry + "\n"
    insertion_point = sep_idx + len(indexes.LOG_SEPARATOR)
    return text[:insertion_point] + "\n" + new_entry + "\n" + text[insertion_point:]


def _activity_summary(
    *,
    rel_note_no_ext: str,
    title: str,
    note_type: str,
    project: str | None,
    projects: list[str] | None,
    severity: str | None = None,
    pattern_type: str | None = None,
    domain: str | None = None,
    medium: str | None = None,
    status: str | None = None,
) -> str:
    path_part = rel_note_no_ext.replace(kb_prefix(), "")
    modifier_parts: list[str] = []
    if note_type == "research-note" and project:
        modifier_parts.append(project)
    elif note_type in ("insight", "failure", "pattern") and projects:
        modifier_parts.append("+".join(projects))
    if note_type == "failure" and severity:
        modifier_parts.append(severity)
    if note_type == "pattern" and pattern_type:
        modifier_parts.append(pattern_type)
    if note_type == "experiment" and domain:
        modifier_parts.append(domain)
    if note_type == "production-log":
        if medium:
            modifier_parts.append(medium)
        if status:
            modifier_parts.append(status)
    modifier = (", " + ", ".join(modifier_parts)) if modifier_parts else ""
    return (
        f"`{path_part}` ({note_type}{modifier}, mobile via exomem) "
        f"— \"{title.strip()}\""
    )


def _log_entry_body(
    *,
    note_type: str,
    title: str,
    project: str | None,
    projects: list[str] | None,
    tags: list[str],
    sources: list[str],
    severity: str | None = None,
    pattern_type: str | None = None,
    domain: str | None = None,
    medium: str | None = None,
    status: str | None = None,
    started: str | None = None,
    duration: str | None = None,
) -> str:
    parts: list[str] = []
    if note_type == "research-note":
        scope = project or "unknown"
    elif note_type == "experiment":
        scope = f"domain={domain}"
    elif note_type == "production-log":
        scope = f"medium={medium}"
    elif projects:
        scope = "+".join(projects)
    else:
        scope = "cross-cutting"
    parts.append(
        f"Mobile compile via exomem. note_type={note_type}. "
        f"scope={scope}. \"{title.strip()}\"."
    )
    if note_type == "failure" and severity:
        parts.append(f"severity={severity}.")
    if note_type == "pattern" and pattern_type:
        parts.append(f"pattern_type={pattern_type}.")
    if note_type == "experiment":
        parts.append(f"started={started}, duration={duration}.")
    if note_type == "production-log" and status:
        parts.append(f"status={status}.")
    if tags:
        parts.append(f"tags: {tags}.")
    if sources:
        parts.append(f"sources: {len(sources)} cited.")
    return " ".join(parts)


# ---------------- tag normalization (matches add._clean_tags) ----------------


def _clean_tags(tags: list[str] | None) -> list[str]:
    if not tags:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        norm = str(t).strip().lower().replace(" ", "-").replace("_", "-")
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out
