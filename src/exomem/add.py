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

from . import (
    corpus_aware,
    indexes,
    memory_refs,
    project_keys,
    schema,
    source_taxonomy,
    temporal,
)
from .kbdir import kb_prefix
from .vault import (
    InvalidSlugError,
    PlannedWrite,
    batch_atomic_write,
    kb_root,
    resolve_filename_slug,
    unique_path,
    yaml_scalar,
)

log = logging.getLogger(__name__)

# Legacy kind → folder. Retained only so callers that still import it keep
# working; the live routing decision is `source_taxonomy.source_segments`, which
# reproduces every one of these mappings from the registry. Do not add to it.
SOURCE_TYPE_TO_FOLDER: dict[str, str] = {
    "article": "Articles",
    "session": "Sessions",
    "book": "Books",
    "paper": "Papers",
    "video": "Videos",
    "other": "Other",
}


def folder_descriptions(vault_root: Path) -> dict[str, str]:
    """`{folder: description}` for the source index, derived from the registry.

    Replaces the hard-coded table this module used to own, and the drifted
    duplicate in `indexes`, so a kind the product never shipped still gets a
    description without a code change.
    """
    return source_taxonomy.load_taxonomy(vault_root).category_descriptions()


@dataclass
class AddResult:
    path: str  # vault-relative
    ref: str
    warnings: list[str]
    # The filename slug actually written, after truncation/normalisation.
    # See NoteResult.slug — callers must link by this, not by re-slugging.
    slug: str = ""
    # Optional advisory classification signal. Emitted only when a condition is
    # detected, following the compiled-write `structure_suggestion` convention.
    structure_suggestion: dict | None = None

    def as_dict(self) -> dict:
        out = {"path": self.path, "ref": self.ref, "warnings": self.warnings}
        if self.slug:
            out["slug"] = self.slug
        if self.structure_suggestion:
            out["structure_suggestion"] = self.structure_suggestion
        return out


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
    title: str,
    source_type: str | None = None,
    slug: str | None = None,
    url: str | None = None,
    tags: list[str] | None = None,
    why_captured: str | None = None,
    domain: str | None = None,
    projects: list[str] | None = None,
    today: dt.date | None = None,
) -> AddResult:
    """Capture a raw source into the KB and update indexes/log atomically.

    `source_type` is the open source-kind axis, `domain` the independent subject
    axis, and `projects` an association that never affects where the source is
    stored. All three resolve through `source_taxonomy`/`project_keys`, so a
    meaningful value this code has never seen is accepted and registers itself as
    part of this capture's atomic batch.

    `today` is dependency-injectable for tests; defaults to dt.date.today().
    """
    taxonomy = source_taxonomy.load_taxonomy(vault_root)
    try:
        # No kind supplied means unclassified, not invalid: capture is never
        # gated on classification.
        kind = taxonomy.resolve_kind(
            source_type if source_type else source_taxonomy.FALLBACK_KIND
        )
        domain_resolution = (
            taxonomy.resolve_domain(domain) if domain is not None else None
        )
    except source_taxonomy.TaxonomyError as e:
        axis = getattr(e, "axis", "source_kind")
        raise AddError(
            code="INVALID_SOURCE",
            missing=["source_type" if axis == "source_kind" else "domain"],
            reason=str(e),
        ) from e

    err = schema.validate_source(
        source_schema,
        content=content,
        source_type=kind.key,
        title=title,
        url=url,
        requires_url=kind.requires_url,
    )
    if err is not None:
        raise AddError(code=err.code, missing=list(err.missing), reason=err.reason)
    try:
        filename_slug, slug_warnings = resolve_filename_slug(
            title, slug, vault_root=vault_root
        )
    except InvalidSlugError as e:
        raise AddError(code="INVALID_SLUG", missing=["slug"], reason=str(e)) from e

    # Corpus-aware near-duplicate check (best-effort; warns, never blocks — the
    # 57% unprocessed-source backlog implies real dupes). Skipped when embeddings
    # are disabled so the fast suite and existing add() tests are unaffected.
    duplicate_candidates: list[corpus_aware.DupCandidate] = []
    contradiction_candidates: list[corpus_aware.DupCandidate] = []
    if not os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        try:
            # One embedding pass: dups (vs other sources) + contradictions (vs
            # active compiled conclusions). Restricting contradiction candidates
            # to conclusions is what makes an `add`-time flag meaningful ("this
            # capture challenges conclusion [[Y]]") rather than source-vs-source.
            cosines = corpus_aware._best_cosine_per_file(
                vault_root, title=title, body=content
            )
            duplicate_candidates = corpus_aware.detect_duplicates(
                vault_root, title=title, body=content,
                self_path=None, types_filter=["source"], precomputed=cosines,
            )
            contradiction_candidates = corpus_aware.detect_contradictions(
                vault_root, title=title, body=content,
                self_path=None, precomputed=cosines,
            )
        except Exception as e:  # noqa: BLE001 — never break a capture
            log.debug("corpus-aware dup check failed (non-fatal): %s", e)

    now = today or temporal.now()
    date_iso = temporal.render_date(now)
    stamp_iso = temporal.stamp(now)

    # The location is a projection of the canonical semantic keys, not the
    # ontology. `folder_name` stays the *top-level* segment because that is what
    # the source index counts and labels by; a domain adds one level below it.
    segments = source_taxonomy.source_segments(kind, domain_resolution)
    folder_name = segments[1]
    folder_path = kb_root(vault_root).joinpath(*segments)

    # Vocabulary and project keys register in this capture's own batch, so a
    # source and the labels it introduced land together or not at all.
    taxonomy_plan = source_taxonomy.plan_registrations(
        vault_root, kind=kind, domain=domain_resolution
    )
    project_keys_clean = list(dict.fromkeys(projects or ()))
    project_plan = project_keys.plan_project_keys(vault_root, project_keys_clean)

    stem = f"{date_iso}-{filename_slug}"
    source_path = unique_path(folder_path, stem)

    tags_clean = _clean_tags(tags)
    exomem_id = memory_refs.new_id()

    source_md = _render_source(
        title=title,
        source_type=kind.key,
        date_iso=stamp_iso,
        url=url,
        tags=tags_clean,
        why_captured=why_captured,
        content=content,
        exomem_id=exomem_id,
        domain=domain_resolution.key if domain_resolution else None,
        projects=project_keys_clean,
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
        source_type=kind.key,
        tags=tags_clean,
    )
    log_entry_body = _log_entry_body(
        title=title,
        source_type=kind.key,
        url=url,
        tags=tags_clean,
        why_captured=why_captured,
    )

    update = _compute_updates_with_counts(
        vault_root=vault_root,
        folder_name=folder_name,
        folder_description=taxonomy.category_description(folder_name),
        rel_source_no_ext=rel_source_no_ext,
        rel_index_path=(
            f"{kb_prefix()}{'/'.join(segments)}/{rel_source_no_ext.rsplit('/', 1)[-1]}"
        ),
        date_iso=date_iso,
        stamp_iso=stamp_iso,
        activity_summary=activity_summary,
        log_entry_body=log_entry_body,
        forced_counts=post_counts,
    )

    kb = kb_root(vault_root)
    # Refresh the Notes/Entities counts in the top index alongside the
    # Sources counts that compute_updates() already handled. `add` doesn't
    # change Notes/Entities counts, so no override needed.
    sub_writes, top_with_counts = indexes.compute_subindex_writes(
        vault_root,
        top_index_text=update.top_index_content,
        pending_paths=[rel_source_no_ext],
    )
    sub_writes = [
        write for write in sub_writes
        if write.path != kb / "Sources" / "index.md"
    ]
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
    writes.extend(taxonomy_plan.writes)
    writes.extend(project_plan.writes)

    warnings: list[str] = list(slug_warnings)
    # Vocabulary notices are plain per-write warnings, not dismissible advisories:
    # "registered 'field-notebook'" reports what the write did, so routing it
    # through the suppression channel would let a dismissal hide a fact.
    warnings.extend(
        _vocabulary_warnings(kind, domain_resolution, taxonomy_plan, project_plan)
    )
    # Cap-50 trim is recorded in log.md per SKILL.md trim discipline; no need
    # to also surface it as a per-write warning.

    try:
        batch_atomic_write(writes, vault_root=vault_root)
    except Exception as e:
        log.exception("partial write during add(); some files may be updated")
        warnings.append(f"partial write — reconcile on desktop: {e}")
        raise

    try:
        self_path = source_path.relative_to(vault_root).as_posix()
        warnings.extend(
            corpus_aware.emit_write_advisory_groups(
                vault_root,
                self_path=self_path,
                groups=[
                    ("near-duplicate", duplicate_candidates),
                    *corpus_aware.detected_overlap_advisory_groups(
                        contradiction_candidates
                    ),
                ],
                # add() detects before its new source path exists, so only this
                # path needs the post-commit competing-pair composition.
                apply_declared_pair_filter=True,
            )
        )
    except Exception as error:  # noqa: BLE001 — advisories never break a capture
        log.debug("write advisory emission failed (non-fatal): %s", error)

    return AddResult(
        path=source_path.relative_to(vault_root).as_posix(),
        ref=memory_refs.memory_ref(exomem_id),
        warnings=warnings,
        slug=filename_slug,
        structure_suggestion=_classification_suggestion(
            folder_path, kind, domain_resolution
        ),
    )


#: Fallback captures sharing one domain before the pattern reads as a real,
#: nameable kind rather than one unusual artifact. Counted post-write, so the
#: capture that triggers the suggestion is included.
_FALLBACK_RECURRENCE_THRESHOLD = 3

_SUGGESTION_KIND = "source_classification_debt"


def _vocabulary_warnings(
    kind: source_taxonomy.Resolution,
    domain: source_taxonomy.Resolution | None,
    taxonomy_plan: source_taxonomy.TaxonomyPlan,
    project_plan: project_keys.ProjectKeyPlan,
) -> list[str]:
    """Surface every vocabulary the capture introduced or nearly mistyped.

    Registration is deliberately silent-but-visible: it never blocks a capture,
    and it always says so, so an unnoticed typo cannot quietly become a category.
    """
    warnings: list[str] = []
    for introduction in taxonomy_plan.introductions:
        warnings.append(
            f"NEW_{introduction.axis.upper()}: registered {introduction.key!r} "
            f"(files under Sources/{introduction.path_label}/). Edit "
            f"_Schema/source-taxonomy.yaml to rename or relabel it."
        )
    for key in project_plan.introduced_keys:
        warnings.append(f"NEW_PROJECT_KEY: registered {key!r}")
    if domain is not None and domain.close_match:
        warnings.append(
            f"DOMAIN_NEAR_MISS: {domain.key!r} closely resembles existing domain "
            f"{domain.close_match!r}. Kept as supplied — re-capture with the "
            f"existing domain if this was a typo."
        )
    for resolution in (kind, domain):
        if resolution is not None and resolution.status == "deprecated":
            replacement = resolution.replaced_by or "an active key"
            warnings.append(
                f"DEPRECATED_{resolution.axis.upper()}: {resolution.key!r} is "
                f"deprecated; prefer {replacement}."
            )
    return warnings


def _classification_suggestion(
    folder_path: Path,
    kind: source_taxonomy.Resolution,
    domain: source_taxonomy.Resolution | None,
) -> dict | None:
    """Advisory: this capture used the fallback where a real kind likely exists.

    Deterministic and local — one bounded directory listing that stops at the
    threshold. No model call, no corpus scan, no persistent state. Wrapped so a
    fault here can never turn a committed capture into a failure.
    """
    if kind.key != source_taxonomy.FALLBACK_KIND or domain is None:
        return None
    try:
        reasons = ["fallback_kind_with_declared_domain"]
        strength = "moderate"
        if _count_capped(folder_path, _FALLBACK_RECURRENCE_THRESHOLD) >= (
            _FALLBACK_RECURRENCE_THRESHOLD
        ):
            reasons.append("fallback_captures_recur_in_domain")
            strength = "strong"
        return {
            "kind": _SUGGESTION_KIND,
            "strength": strength,
            "reasons": sorted(reasons),
            "domain": domain.key,
            "fallback_captures": _count_capped(
                folder_path, _FALLBACK_RECURRENCE_THRESHOLD
            ),
        }
    except Exception:  # noqa: BLE001 — advisory only; never fail a capture
        log.debug("source-classification advisory failed (non-fatal)", exc_info=True)
        return None


def _count_capped(folder_path: Path, cap: int) -> int:
    """Count `.md` files in one directory, stopping once `cap` is reached."""
    total = 0
    for entry in folder_path.iterdir():
        if entry.name == "index.md" or entry.suffix != ".md" or not entry.is_file():
            continue
        total += 1
        if total >= cap:
            break
    return total


def _compute_updates_with_counts(
    *,
    vault_root: Path,
    folder_name: str,
    folder_description: str,
    rel_source_no_ext: str,
    rel_index_path: str,
    date_iso: str,
    stamp_iso: str,
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
            folder_description=folder_description,
            rel_source_path=rel_index_path,
            date_iso=date_iso,
            stamp_iso=stamp_iso,
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
    exomem_id: str,
    domain: str | None = None,
    projects: list[str] | None = None,
) -> str:
    """Emit the source page markdown matching frontmatter.md's example shape."""
    lines = ["---"]
    lines.append("type: source")
    lines.append(f"exomem_id: {exomem_id}")
    lines.append(f"title: {yaml_scalar(title.strip())}")
    lines.append(f"source_type: {source_type}")
    if domain:
        lines.append(f"domain: {domain}")
    if projects:
        lines.append("projects: [" + ", ".join(projects) + "]")
    lines.append(f"captured: {date_iso}")
    if url:
        lines.append(f"url: {yaml_scalar(url)}")
    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    else:
        lines.append("tags: []")
    lines.append("ingested_into: []")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title.strip()}")
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
    base = f"`{rel_source_no_ext.replace(kb_prefix(), '')}` (source, {source_type}, mobile capture via exomem)"
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
