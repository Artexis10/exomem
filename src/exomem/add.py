"""The `add` MCP tool: capture a raw source into the KB with full rule-7 writes.

Implements the workflow from the architecture plan:

1. Validate the proposed source via schema.validate_source()
2. Build the frontmatter + body markdown for the source file
3. Compute today's filename (date + slug, collision-safe)
4. Auto-create Sources/<Type>/ if missing
5. Compute updated contents of Sources/index.md, top-level index.md, log.md
6. Batch-atomic-write all four files

On schema-rejection: return a structured error, do not touch disk.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from . import corpus_aware, indexes, schema
from .vault import (
    PlannedWrite,
    batch_atomic_write,
    kb_root,
    slugify_with_truncation_check,
    unique_path,
)


log = logging.getLogger(__name__)

# source_type → on-disk folder name (title-cased)
SOURCE_TYPE_TO_FOLDER: dict[str, str] = {
    "article": "Articles",
    "session": "Sessions",
    "book": "Books",
    "paper": "Papers",
    "video": "Videos",
    "other": "Other",
}

# folder description used by indexes.py when auto-creating the By-type row
FOLDER_DESCRIPTIONS: dict[str, str] = {
    "Articles": "captured web/PDF content",
    "Sessions": "pasted Claude/conversation transcripts",
    "Books": "book notes/excerpts",
    "Papers": "academic papers",
    "Videos": "captured video transcripts/notes",
    "Other": "miscellaneous captures",
}


@dataclass
class AddResult:
    path: str  # vault-relative
    warnings: list[str]

    def as_dict(self) -> dict:
        return {"path": self.path, "warnings": self.warnings}


@dataclass
class AddError(Exception):
    code: str
    missing: list[str]
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "missing": self.missing, "reason": self.reason}


def add(
    vault_root: Path,
    source_schema: schema.SourceSchema,
    *,
    content: str,
    source_type: str,
    title: str,
    url: str | None = None,
    tags: list[str] | None = None,
    why_captured: str | None = None,
    today: dt.date | None = None,
) -> AddResult:
    """Capture a raw source into the KB and update indexes/log atomically.

    `today` is dependency-injectable for tests; defaults to dt.date.today().
    """
    err = schema.validate_source(
        source_schema,
        content=content,
        source_type=source_type,
        title=title,
        url=url,
    )
    if err is not None:
        raise AddError(code=err.code, missing=list(err.missing), reason=err.reason)

    # Corpus-aware near-duplicate check (best-effort; warns, never blocks — the
    # 57% unprocessed-source backlog implies real dupes). Skipped when embeddings
    # are disabled so the fast suite and existing add() tests are unaffected.
    dup_warnings: list[str] = []
    contradiction_warnings: list[str] = []
    if not os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        try:
            # One embedding pass: dups (vs other sources) + contradictions (vs
            # active compiled conclusions). Restricting contradiction candidates
            # to conclusions is what makes an `add`-time flag meaningful ("this
            # capture challenges conclusion [[Y]]") rather than source-vs-source.
            cosines = corpus_aware._best_cosine_per_file(
                vault_root, title=title, body=content
            )
            dup_warnings = [
                corpus_aware.dup_warning(c)
                for c in corpus_aware.detect_duplicates(
                    vault_root, title=title, body=content,
                    self_path=None, types_filter=["source"],
                    precomputed=cosines,
                )
            ]
            contradiction_warnings = [
                corpus_aware.overlap_warning(c)
                for c in corpus_aware.detect_contradictions(
                    vault_root, title=title, body=content,
                    self_path=None, precomputed=cosines,
                )
            ]
        except Exception as e:  # noqa: BLE001 — never break a capture
            log.debug("corpus-aware dup check failed (non-fatal): %s", e)

    today = today or dt.date.today()
    date_iso = today.isoformat()
    folder_name = SOURCE_TYPE_TO_FOLDER[source_type]
    folder_path = kb_root(vault_root) / "Sources" / folder_name

    slug, slug_warning = slugify_with_truncation_check(title)
    stem = f"{date_iso}-{slug}"
    source_path = unique_path(folder_path, stem)

    tags_clean = _clean_tags(tags)

    source_md = _render_source(
        title=title,
        source_type=source_type,
        date_iso=date_iso,
        url=url,
        tags=tags_clean,
        why_captured=why_captured,
        content=content,
    )

    # Plan the source file write so the counts in compute_updates() are
    # *post*-creation. We do this by passing a "+1" hint via writing the file
    # to a tmp first? Simpler: pre-create the folder and let compute_updates
    # re-scan, then add the new file as part of the batch. We need the new
    # count to reflect the file we're about to write, so we explicitly bump
    # the in-memory counts.
    folder_path.mkdir(parents=True, exist_ok=True)

    rel_source_no_ext = (
        source_path.relative_to(vault_root).with_suffix("").as_posix()
    )

    # Pre-compute counts and bump the relevant folder by 1 for the new file.
    pre_counts = indexes._count_sources(kb_root(vault_root) / "Sources")
    post_counts = dict(pre_counts)
    post_counts[folder_name] = post_counts.get(folder_name, 0) + 1

    activity_summary = _activity_summary(
        rel_source_no_ext=rel_source_no_ext,
        title=title,
        source_type=source_type,
        tags=tags_clean,
    )
    log_entry_body = _log_entry_body(
        title=title,
        source_type=source_type,
        url=url,
        tags=tags_clean,
        why_captured=why_captured,
    )

    update = _compute_updates_with_counts(
        vault_root=vault_root,
        folder_name=folder_name,
        rel_source_no_ext=rel_source_no_ext,
        date_iso=date_iso,
        activity_summary=activity_summary,
        log_entry_body=log_entry_body,
        forced_counts=post_counts,
    )

    kb = kb_root(vault_root)
    # Refresh the Notes/Entities counts in the top index alongside the
    # Sources counts that compute_updates() already handled. `add` doesn't
    # change Notes/Entities counts, so no override needed.
    sub_writes, top_with_counts = indexes.compute_subindex_writes(
        vault_root, top_index_text=update.top_index_content
    )
    top_index_final = (
        top_with_counts if top_with_counts is not None
        else update.top_index_content
    )
    writes = [
        PlannedWrite(path=source_path, content=source_md),
        PlannedWrite(path=kb / "Sources" / "index.md", content=update.sources_index_content),
        PlannedWrite(path=kb / "index.md", content=top_index_final),
        PlannedWrite(path=kb / "log.md", content=update.log_content),
    ]
    writes.extend(sub_writes)

    warnings: list[str] = list(dup_warnings) + list(contradiction_warnings)
    if slug_warning:
        warnings.append(slug_warning)
    # Cap-50 trim is recorded in log.md per SKILL.md trim discipline; no need
    # to also surface it as a per-write warning.

    try:
        batch_atomic_write(writes, vault_root=vault_root)
    except Exception as e:
        log.exception("partial write during add(); some files may be updated")
        warnings.append(f"partial write — reconcile on desktop: {e}")
        raise

    return AddResult(
        path=source_path.relative_to(vault_root).as_posix(),
        warnings=warnings,
    )


def _compute_updates_with_counts(
    *,
    vault_root: Path,
    folder_name: str,
    rel_source_no_ext: str,
    date_iso: str,
    activity_summary: str,
    log_entry_body: str,
    forced_counts: dict[str, int],
) -> indexes.IndexUpdate:
    """Wrapper that overrides the disk-scan with forced counts.

    indexes.compute_updates() reads from disk; for `add` we need the count to
    reflect the source file we're *about* to write. We monkey-patch the count
    function for this call.
    """
    original = indexes._count_sources
    indexes._count_sources = lambda _sources_dir: dict(forced_counts)  # type: ignore[assignment]
    try:
        return indexes.compute_updates(
            vault_root,
            source_type=folder_name.lower(),
            folder_title=folder_name,
            folder_description=FOLDER_DESCRIPTIONS.get(folder_name, "captured material"),
            rel_source_path=f"Knowledge Base/Sources/{folder_name}/{rel_source_no_ext.rsplit('/', 1)[-1]}",
            date_iso=date_iso,
            activity_summary=activity_summary,
            log_entry_body=log_entry_body,
        )
    finally:
        indexes._count_sources = original  # type: ignore[assignment]


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


def _render_source(
    *,
    title: str,
    source_type: str,
    date_iso: str,
    url: str | None,
    tags: list[str],
    why_captured: str | None,
    content: str,
) -> str:
    """Emit the source page markdown matching frontmatter.md's example shape."""
    lines = ["---"]
    lines.append("type: source")
    lines.append(f"source_type: {source_type}")
    lines.append(f"captured: {date_iso}")
    if url:
        lines.append(f"url: {url}")
    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    else:
        lines.append("tags: []")
    lines.append("ingested_into: []")
    lines.append("---")
    lines.append("")
    lines.append(f"# Source: {title.strip()}")
    lines.append("")
    if why_captured and why_captured.strip():
        # Single-line blockquote at top, per page-types.md shape.
        for paragraph in why_captured.strip().splitlines():
            lines.append(f"> {paragraph}")
        lines.append("")
    lines.append("## Capture")
    lines.append("")
    lines.append(content.strip())
    lines.append("")
    return "\n".join(lines)


def _activity_summary(
    *,
    rel_source_no_ext: str,
    title: str,
    source_type: str,
    tags: list[str],
) -> str:
    """One-liner for the top index's Recent activity bullet."""
    base = f"`{rel_source_no_ext.replace('Knowledge Base/', '')}` (source, {source_type}, mobile capture via exomem)"
    excerpt = f"\"{title.strip()}\""
    tags_part = f"; tags: {tags}" if tags else ""
    return f"{base} — {excerpt}{tags_part}"


def _log_entry_body(
    *,
    title: str,
    source_type: str,
    url: str | None,
    tags: list[str],
    why_captured: str | None,
) -> str:
    """Multi-line description body for log.md."""
    parts: list[str] = []
    parts.append(
        f"Mobile capture via exomem. source_type={source_type}. \"{title.strip()}\"."
    )
    if url:
        parts.append(f"url: {url}.")
    if tags:
        parts.append(f"tags: {tags}.")
    if why_captured and why_captured.strip():
        wc = why_captured.strip().replace("\n", " ")
        if len(wc) > 280:
            wc = wc[:277] + "…"
        parts.append(f"Why captured: {wc}")
    return " ".join(parts)
