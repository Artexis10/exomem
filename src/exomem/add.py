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
import hashlib
import logging
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from . import (
    cli_ops,
    corpus_aware,
    indexes,
    memory_refs,
    project_keys,
    schema,
    source_taxonomy,
    tag_variants,
    temporal,
    vocabulary_resolution,
)
from .kbdir import kb_prefix
from .vault import (
    MISSING_CONTENT_HASH,
    ContentHashMismatchError,
    InvalidSlugError,
    PlannedWrite,
    PreparedBinaryContent,
    batch_atomic_write,
    content_hash,
    kb_root,
    parse_frontmatter,
    render_wikilink_target,
    resolve_filename_slug,
    unique_path,
    yaml_scalar,
)

log = logging.getLogger(__name__)

# Legacy kind → folder. Retained only so callers that still import it keep
# working; the live routing decision is `source_taxonomy.source_segments`, which
# reproduces every one of these mappings from the registry. Do not add to it.
# `other` names a legacy folder that is read and never written.
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


#: The closed frontmatter an `episode` Source carries beyond every Source's
#: own, in render order: (field, required). Nothing else may be added through
#: `extra_frontmatter`, and no other kind may carry any of it, so a recap is
#: always a bounded, attributable record and never a free-form frontmatter
#: channel. Semantic bounds (lengths, credential refusal) are the recording
#: operation's (`episode_capture`); this module only keeps the shape closed.
#: The refs a recap concerns are deliberately not here: every audience that may
#: read the page would learn them, so they live in the recorder's own ledger.
EPISODE_FRONTMATTER_FIELDS: tuple[tuple[str, bool], ...] = (
    ("summary", True),
    ("episode", True),
    ("episode_digest", True),
    ("client", False),
)


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
    artifact_path: str | None = None
    artifact_hash: str | None = None
    artifact_size: int | None = None
    adoption: dict[str, object] | None = None
    # The shared domain identity record, present only when a domain was given.
    vocabulary_resolution: dict[str, str] | None = None

    def as_dict(self) -> dict:
        out = {"path": self.path, "ref": self.ref, "warnings": self.warnings}
        if self.slug:
            out["slug"] = self.slug
        if self.structure_suggestion:
            out["structure_suggestion"] = self.structure_suggestion
        if self.artifact_path:
            out["artifact_path"] = self.artifact_path
            out["hash"] = self.artifact_hash
            out["hash_algorithm"] = "sha256"
            out["size"] = self.artifact_size
        if self.adoption is not None:
            out["adoption"] = self.adoption
        if self.vocabulary_resolution is not None:
            out["vocabulary_resolution"] = self.vocabulary_resolution
        return out


@dataclass
class AddError(Exception):
    code: str
    missing: list[str]
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "missing": self.missing, "reason": self.reason}


@dataclass(frozen=True)
class SourceArtifact:
    """Bytes being captured as a Source, already staged on readable disk.

    The caller stages — `client_artifacts` for a client file handle, the upload
    route for an out-of-band POST — and this module copies. Keeping the staging
    out of here is what lets one safe-fetch implementation serve both lanes
    instead of two that drift.
    """

    staged_path: Path
    filename: str
    content_type: str | None = None


def _artifact_pair(folder: Path, stem: str, suffix: str, *, vault_root: Path) -> tuple[Path, Path]:
    """A free `<stem><suffix>` / `<stem><suffix>.md` pair under `folder`.

    `unique_path` guarantees one free name; a captured artifact needs two, and
    an orphaned binary with no page would otherwise be silently overwritten.
    Bumping the seed and asking again keeps one implementation of the
    case-insensitive collision test rather than a second copy of it here, and
    keeps `add`'s uniquify semantics rather than introducing a refusal.
    """
    for attempt in range(1, 51):
        seed = stem if attempt == 1 else f"{stem}-{attempt}"
        page = unique_path(folder, seed, suffix=f"{suffix}.md", vault_root=vault_root)
        artifact = page.with_name(page.name[:-3])
        if not artifact.exists():
            return artifact, page
    raise AddError(
        code="INVALID_SOURCE",
        missing=["slug"],
        reason=f"could not find a free filename for {stem!r} in {folder.name!r}",
    )


def _describe_artifact(filename: str, digest: str, size: int) -> str:
    """The body of a source page for bytes it deliberately does not inline."""
    return "\n".join(
        [
            f"- Original filename: `{filename}`",
            f"- SHA-256: `{digest}`",
            f"- Bytes: {size}",
        ]
    )


def _artifact_identity(path: Path) -> tuple[str, int]:
    """SHA-256 and byte count of a staged file, read in chunks."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


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
    artifact: SourceArtifact | None = None,
    adoption_seed: Mapping[str, object] | None = None,
    extra_frontmatter: Mapping[str, object] | None = None,
    supersede: Sequence[tuple[str, str]] = (),
    defer_fanout_to_terminal: bool = False,
    raw_protection: bool = False,
) -> AddResult:
    """Capture a raw source into the KB and update indexes/log atomically.

    `source_type` is the open source-kind axis, `domain` the independent subject
    axis, and `projects` an association that never affects where the source is
    stored. All three resolve through `source_taxonomy`/`project_keys`, so a
    meaningful value this code has never seen is accepted and registers itself as
    part of this capture's atomic batch.

    `extra_frontmatter` and `supersede` exist for the `episode` kind only, and
    that kind requires the first (`EPISODE_FRONTMATTER_FIELDS`). `supersede`
    names earlier revisions of the same recap as `(vault-relative path, content
    hash the caller read)`, inside the episode folder: each gets `status:
    superseded` and a `superseded_by` link in THIS batch, frontmatter only, so
    the new revision and the retirement of the old one land together or not at
    all and a Source body is never rewritten. A revision whose text no longer
    matches the caller's hash fails the whole call with
    `ContentHashMismatchError`; one that is already superseded is left alone.

    `defer_fanout_to_terminal` is the episode recorder's opt-in: its caller
    keeps planning and canonical writes under the wide guard, while derived
    refresh waits for the committed mutation's terminal.

    `today` is dependency-injectable for tests; defaults to dt.date.today().
    """
    from .governance import raw_protection as raw_guard
    from .governance.principal import effective_principal

    if raw_protection and not raw_guard.applies_to(effective_principal()):
        raise AddError(
            code="RAW_PROTECTION_UNAVAILABLE", missing=["raw_protection"], reason=raw_guard.UNAVAILABLE_REASON
        )
    if raw_protection and artifact is None:
        with tempfile.TemporaryDirectory(prefix="exomem-raw-source-") as staging:
            original = Path(staging) / "source.txt"
            original.write_bytes(content.encode("utf-8"))
            return add(
                vault_root, source_schema, content="", title=title, source_type=source_type,
                slug=slug, url=url, tags=tags, why_captured=why_captured, domain=domain,
                projects=projects, today=today, artifact=SourceArtifact(original, "source.txt", "text/plain"),
                adoption_seed=adoption_seed, extra_frontmatter=extra_frontmatter,
                supersede=supersede, defer_fanout_to_terminal=defer_fanout_to_terminal,
                raw_protection=True,
            )
    # An artifact's identity is read before validation, because the page body
    # is synthesized from it when the caller supplied no text — and
    # `schema.validate_source` refuses an empty body.
    artifact_digest: str | None = None
    artifact_size: int | None = None
    artifact_suffix = ""
    if artifact is not None:
        from .preserve import _sanitize_filename

        safe_name = _sanitize_filename(artifact.filename)
        if not safe_name:
            raise AddError(
                code="INVALID_SOURCE",
                missing=["artifact"],
                reason="artifact filename is empty or only invalid characters",
            )
        artifact_suffix = Path(safe_name).suffix
        artifact_digest, artifact_size = _artifact_identity(artifact.staged_path)
        if not content or not content.strip():
            content = _describe_artifact(safe_name, artifact_digest, artifact_size)

    # The caller decides what a missing kind means: an agent-facing surface
    # refuses it (`capture_kind`), and a surface with no agent in the loop
    # passes `unclassified`. This layer never invents a kind.
    if not source_type or not source_type.strip():
        raise AddError(
            code="SOURCE_KIND_REQUIRED",
            missing=["source_type"],
            reason="a source needs a kind; name what the material is",
        )
    requested_kind = source_type
    domain_binding: vocabulary_resolution.SourceDomainBinding | None = None
    try:
        if domain is not None:
            # A domain projects a destination, so it resolves through the same
            # strict snapshot and existing-spelling rule as Notes experiments.
            domain_binding = vocabulary_resolution.resolve_source_domain(
                vault_root, kind=requested_kind, domain=domain
            )
            taxonomy = domain_binding.taxonomy
            kind = domain_binding.kind
            domain_resolution: source_taxonomy.Resolution | None = domain_binding.domain
        else:
            taxonomy = source_taxonomy.load_taxonomy(vault_root)
            kind = taxonomy.resolve_kind(requested_kind)
            domain_resolution = None
    except source_taxonomy.TaxonomyError as e:
        axis = getattr(e, "axis", "source_kind")
        raise AddError(
            code="INVALID_SOURCE",
            missing=["source_type" if axis == "source_kind" else "domain"],
            reason=str(e),
        ) from e
    except vocabulary_resolution.VocabularyResolutionError as e:
        raise AddError(
            code=e.code,
            missing=["domain"],
            reason=vocabulary_resolution.capture_refusal_reason(vault_root, e),
        ) from e

    if kind.key == source_taxonomy.LEGACY_OTHER_KIND:
        raise AddError(
            code="SOURCE_KIND_REQUIRED",
            missing=["source_type"],
            reason=f"{kind.key!r} is retired and no new source is filed under it",
        )

    episode_lines = _episode_frontmatter_lines(kind.key, extra_frontmatter, supersede)

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
    # A recap revision near-duplicates the revision it supersedes by
    # construction, so the embedding pass could only ever report that.
    if not os.environ.get("EXOMEM_DISABLE_EMBEDDINGS") and episode_lines is None:
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
    segments = (
        domain_binding.segments
        if domain_binding is not None
        else source_taxonomy.source_segments(kind, domain_resolution)
    )
    folder_name = segments[1]
    folder_path = kb_root(vault_root).joinpath(*segments)
    from .governance import connector_boundary

    try:
        connector_boundary.require_create(vault_root, (folder_path / f"{date_iso}-{filename_slug}.md").relative_to(vault_root).as_posix())
    except ValueError as error:
        raise AddError(code="WRITE_REFUSED", missing=[], reason="target is unavailable") from error
    if adoption_seed is not None:
        actual_destination = folder_path.relative_to(vault_root).as_posix()
        if adoption_seed.get("destination") != actual_destination:
            raise AddError(
                code="ADOPTION_DESTINATION_CHANGED",
                missing=["destination"],
                reason="source taxonomy changed the adoption destination before commit",
            )

    # Vocabulary and project keys register in this capture's own batch, so a
    # source and the labels it introduced land together or not at all.
    taxonomy_plan = source_taxonomy.plan_registrations(
        vault_root, kind=kind, domain=domain_resolution
    )
    if (
        domain_binding is not None
        and taxonomy_plan.writes
        and vocabulary_resolution.registry_text_snapshot(taxonomy_plan.source_text)
        != domain_binding.snapshot
    ):
        # The registration re-read the registry and guards only that read. A
        # registry edited since the domain resolved could make this append a
        # second owner, so the capture is refused as stale instead.
        raise AddError(
            code="STALE_VOCABULARY_BINDING",
            missing=["domain"],
            reason="domain vocabulary changed while the capture resolved it; retry the capture",
        )
    project_keys_clean = list(dict.fromkeys(projects or ()))
    project_plan = project_keys.plan_project_keys(vault_root, project_keys_clean)
    if raw_protection:
        # Private capture metadata does not enroll shared vocabulary.
        taxonomy_plan = replace(taxonomy_plan, writes=(), introductions=())
        project_plan = replace(project_plan, writes=(), introductions=())

    supersede_targets = _supersede_targets(vault_root, folder_path, supersede)
    stem = f"{date_iso}-{filename_slug}"
    if raw_protection:
        stem = raw_guard.PREFIX + stem
    # No directory is created here: the batch creates the folder with the page
    # and removes it again if the commit is refused.
    if artifact is None:
        artifact_path: Path | None = None
        source_path = unique_path(folder_path, stem, vault_root=vault_root)
    else:
        # The page is `<stem><ext>.md` beside `<stem><ext>`, the same convention
        # Evidence uses, so the media pipeline addresses both lanes with no
        # change and the citation resolver is fixed once rather than per layout.
        artifact_path, source_path = _artifact_pair(folder_path, stem, artifact_suffix, vault_root=vault_root)

    adoption_receipt: dict[str, object] | None = None
    if adoption_seed is not None:
        if artifact is None or artifact_path is None or artifact_digest is None or artifact_size is None:
            raise AddError(
                code="INVALID_SOURCE",
                missing=["artifact"],
                reason="artifact adoption requires exact staged bytes",
            )
        from .preserve import _complete_adoption_receipt

        adoption_receipt = _complete_adoption_receipt(
            adoption_seed,
            stored_path=artifact_path.relative_to(vault_root).as_posix(),
            page_path=source_path.relative_to(vault_root).as_posix(),
            digest=artifact_digest,
            size=artifact_size,
            content_type=artifact.content_type,
        )

    tags_clean = _clean_tags(tags)
    tag_warnings = tag_variants.advise_authored(vault_root, tags_clean)
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
        artifact_name=(
            _sanitize_filename(artifact.filename) if artifact is not None else None
        ),
        artifact_rel=(
            artifact_path.relative_to(vault_root).as_posix()
            if artifact_path is not None
            else None
        ),
        artifact_digest=artifact_digest,
        artifact_size=artifact_size,
        adoption_receipt=adoption_receipt,
        extra_lines=episode_lines or (),
    )
    supersede_writes = _supersede_writes(
        vault_root, supersede_targets, new_path=source_path, stamp_iso=stamp_iso
    )
    if raw_protection:
        source_md = raw_guard.protect(
            source_md, artifact_path=artifact_path.relative_to(vault_root).as_posix(),
            digest=artifact_digest,
        )

    # Plan the source file write so the counts in compute_updates() are
    # *post*-creation: compute_updates re-scans, the folder may not exist
    # until the batch creates it, and the new file joins the batch — so the
    # in-memory counts are bumped explicitly to include it.
    rel_source_no_ext = (
        source_path.relative_to(vault_root).with_suffix("").as_posix()
    )

    # Pre-compute counts and bump the relevant folder by 1 for the new file.
    count_errors: list[OSError] = []
    pre_counts = indexes._count_sources(
        kb_root(vault_root) / "Sources", on_error=count_errors.append
    )
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
    writes.extend(supersede_writes)
    if raw_protection:
        # Global index/log prose cannot carry the protected title or byte identity.
        writes = [writes[0], *taxonomy_plan.writes, *project_plan.writes]

    warnings: list[str] = list(slug_warnings) + tag_warnings
    # Vocabulary notices are plain per-write warnings, not dismissible advisories:
    # "registered 'field-notebook'" reports what the write did, so routing it
    # through the suppression channel would let a dismissal hide a fact.
    warnings.extend(
        _vocabulary_warnings(kind, domain_resolution, taxonomy_plan, project_plan)
    )
    # Cap-50 trim is recorded in log.md per SKILL.md trim discipline; no need
    # to also surface it as a per-write warning.

    # The binary and its Source page share the held batch. No canonical byte
    # path becomes visible before the page (and any adoption receipt it owns)
    # can publish in the same rollback set.
    artifact_stream = None
    if artifact is not None and artifact_path is not None:
        assert artifact_digest is not None and artifact_size is not None
        artifact_stream = artifact.staged_path.open("rb")
        writes.append(
            PlannedWrite(
                path=artifact_path,
                content=PreparedBinaryContent(
                    artifact_stream,
                    artifact_size,
                    artifact_digest,
                ),
                create_only=True,
                expected_hash=MISSING_CONTENT_HASH,
            )
        )

    # The registry the domain resolved against must still be the one on disk.
    # A registration rewrites it in this batch under its own content guard.
    required_guards = (
        (domain_binding.registry_guard,)
        if domain_binding is not None and not taxonomy_plan.writes
        else ()
    )
    try:
        if defer_fanout_to_terminal:
            publication_intents: list[object] = []
            created_paths = [write.path for write in writes if not os.path.lexists(write.path)]
            committed = batch_atomic_write(
                writes,
                vault_root=vault_root,
                required_guards=required_guards,
                post_commit_fanout=False,
                publication_intents_out=publication_intents,
            )
        else:
            batch_atomic_write(writes, vault_root=vault_root, required_guards=required_guards)
    except Exception as e:
        log.exception("partial write during add(); some files may be updated")
        warnings.append(f"partial write — reconcile on desktop: {e}")
        raise
    finally:
        if artifact_stream is not None:
            artifact_stream.close()

    if defer_fanout_to_terminal:
        from . import deferred_index, file_watcher, writer_lease
        from . import vault as vault_module

        assert isinstance(committed, list)
        replaced = list(dict.fromkeys(committed))

        def abort_intents() -> None:
            try:
                file_watcher.abort_publication_intents(
                    publication_intents, force_paths=replaced
                )
            except Exception as error:  # noqa: BLE001 - the Source already committed
                log.warning("episode publication cleanup failed: %s", type(error).__name__)

        if writer_lease.active_derived_batch_custody(vault_root):
            # The fast-ack receipt already carries this batch's derived work.
            abort_intents()
        else:
            fanout_succeeded = [False]

            def run_fanout() -> list[object]:
                from . import index_sync

                reports: list[object] = []
                try:
                    completed = vault_module.post_commit_batch_fanout(
                        vault_root,
                        replaced,
                        reports,
                        None,
                        created_paths=created_paths,
                        publication_intents=publication_intents,
                    )
                    fanout_succeeded[0] = completed is True
                    if completed is not True:
                        abort_intents()
                except Exception as error:  # noqa: BLE001 - the Source already committed
                    abort_intents()
                    log.warning("episode derived fanout failed after commit: %s", type(error).__name__)
                if not reports:
                    reports.append(
                        index_sync.unverified_upsert_report(vault_root, replaced)
                        if fanout_succeeded[0]
                        else index_sync.failed_upsert_report(vault_root, replaced)
                    )
                return reports

            try:
                # Keep this receipt after a successful inline drain too: a
                # same-path ABA write may have replaced its queued revision.
                # The background drain owns exact-revision retirement.
                receipts = deferred_index.add_full_receipts(
                    vault_root,
                    [path.relative_to(vault_root).as_posix() for path in replaced],
                )
            except Exception as error:  # noqa: BLE001 - a committed Source cannot be retried safely
                log.warning("episode durable derived demand failed after commit: %s", type(error).__name__)
                run_fanout()
                if not fanout_succeeded[0]:
                    raise writer_lease._PostCommitOutcomeUncertain() from None
            else:
                def run_terminal_fanout() -> list[object]:
                    from . import epistemic_graph

                    # This callback has a durable parent and a terminal that
                    # can report pending. A direct caller below must still
                    # converge, even though it also retained full receipts.
                    coordinator = writer_lease.active_manager()._mutation_coordinator_for(
                        vault_root
                    )
                    with epistemic_graph.parent_receipted_graph_handoff(
                        vault_root,
                        state_root=coordinator.state_root,
                        receipts=tuple(receipts),
                    ):
                        return run_fanout()

                if not writer_lease.defer_until_terminal_persisted(run_terminal_fanout):
                    run_fanout()

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
        artifact_path=(
            artifact_path.relative_to(vault_root).as_posix()
            if artifact_path is not None
            else None
        ),
        artifact_hash=artifact_digest,
        artifact_size=artifact_size,
        slug=filename_slug,
        structure_suggestion=_classification_suggestion(
            vault_root, taxonomy, post_counts
        ) if not count_errors else None,
        adoption=adoption_receipt,
        vocabulary_resolution=domain_binding.as_dict() if domain_binding is not None else None,
    )


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
    vault_root: Path,
    taxonomy: source_taxonomy.SourceTaxonomy,
    counts: Mapping[str, int],
) -> dict | None:
    """Advisory: this vault holds sources nobody has given a kind yet.

    Reads the per-folder counts this capture already took for the source
    index, so it adds no scan, model call or persistent state. A caller other
    than the owner gets no advisory, because those counts include pages it may
    not see. Wrapped so a fault here can never fail a committed capture.
    """
    try:
        from .governance import principal, raw_protection

        who = principal.effective_principal()
        if not (
            raw_protection.is_owner(who)
            and raw_protection.has_unrestricted_access(vault_root, who)
        ):
            return None
        by_folder = {name.casefold(): count for name, count in counts.items()}
        folders = {
            name: by_folder[name.casefold()]
            for name in unclassified_folders(taxonomy)
            if by_folder.get(name.casefold(), 0) > 0
        }
        if not folders:
            return None
        return {
            "kind": _SUGGESTION_KIND,
            "strength": "moderate",
            "reasons": ["unclassified_sources_present"],
            "unclassified_sources": sum(folders.values()),
            "folders": sorted(folders),
        }
    except Exception:  # noqa: BLE001 — advisory only; never fail a capture
        log.debug("source-classification advisory failed (non-fatal)", exc_info=True)
        return None


def unclassified_folders(taxonomy: source_taxonomy.SourceTaxonomy) -> tuple[str, ...]:
    """The `Sources/` folders whose pages carry no chosen kind."""
    return (
        *(taxonomy.kinds[key].path_label for key in sorted(source_taxonomy.UNCHOSEN_KINDS)),
        source_taxonomy.IMPORTED_PATH_LABEL,
    )


#: At most this many known kinds ride on a refusal, most used first.
KNOWN_KINDS_SHOWN = 30
#: What each count on a kind-required refusal measures, and where it comes from.
KNOWN_KINDS_COUNTED = (
    "sources: the source pages filed under the kind's Sources/ folder that this "
    "caller may see, counted from this vault when the capture was refused"
)


def capture_kind(vault_root: Path, supplied: str | None, *, unattended: bool) -> str:
    """The kind a capture is filed under, or a refusal raised before any write.

    A capture with no agent in the loop (`unattended`) records a missing kind
    as `unclassified`, the visible classification debt. An agent is refused
    instead, and so is any caller naming a kind that records no choice: the
    agent is the one who reads the material, so the choice is its own. Nothing
    here guesses a kind from the content.
    """
    if supplied is None or not supplied.strip():
        if unattended:
            return source_taxonomy.UNCLASSIFIED_KIND
        raise kind_required(vault_root, supplied=None)
    try:
        key = source_taxonomy.load_taxonomy(vault_root).resolve_kind(supplied).key
    except source_taxonomy.TaxonomyError:
        # `add` refuses it with the near-miss or validity reason it already gives.
        return supplied
    if key in source_taxonomy.UNCHOSEN_KINDS:
        raise kind_required(vault_root, supplied=supplied)
    return supplied


def kind_required(vault_root: Path, *, supplied: str | None) -> cli_ops.OpError:
    """The refusal for a capture whose kind an agent still has to choose."""
    taxonomy = source_taxonomy.load_taxonomy(vault_root)
    known = known_kind_counts(vault_root, taxonomy)
    if supplied is None:
        message = "capture_source needs source_kind: what this material IS"
    else:
        message = f"source_kind {supplied!r} records no choice; name what this material IS"
    return cli_ops.OpError(
        "SOURCE_KIND_REQUIRED",
        message,
        f"{source_taxonomy.CAPTURE_KIND_RULE} {source_taxonomy.CAPTURE_KIND_MIGRATION}",
        details={
            "known_source_kinds": known[:KNOWN_KINDS_SHOWN],
            "known_source_kinds_total": len(known),
            "known_source_kinds_counted": KNOWN_KINDS_COUNTED,
            "known_source_kinds_state": (
                "complete" if all(row["sources"] is not None for row in known)
                else "incomplete" if any(row["sources"] is not None for row in known)
                else "failed"
            ),
        },
    )


def known_kind_counts(
    vault_root: Path, taxonomy: source_taxonomy.SourceTaxonomy
) -> list[dict[str, object]]:
    """Every kind an agent may choose, with its use count, most used first.

    A count is the source pages filed under the kind's folder, counted only
    over the pages this caller may see.
    """
    from .governance import egress

    keep = egress.restricted_release_filter(vault_root)

    def visible(path: str) -> bool:
        return bool(keep(Path(path).relative_to(vault_root).as_posix()))

    # Walks the folders itself rather than calling `indexes._count_sources`,
    # which `_compute_updates_with_counts` swaps out process-wide while a
    # capture in another thread computes its index.
    by_folder: dict[str, int | None] = {}
    sources = kb_root(vault_root) / source_taxonomy.SOURCES_ROOT
    listing_complete = False
    try:
        # iterdir distinguishes proven absence from a failed directory read.
        folders = list(sources.iterdir())
    except FileNotFoundError:
        listing_complete = True
    except OSError:
        pass
    else:
        listing_complete = True
        for folder in folders:
            if folder.name.startswith("_"):
                continue
            errors: list[OSError] = []
            try:
                # stat raises on inaccessible entries; is_dir can suppress errors.
                if not stat.S_ISDIR(folder.stat().st_mode):
                    continue
                count = indexes._count_markdown_pages(
                    folder,
                    skip_underscore_dirs=True,
                    keep=None if keep is None else visible,
                    on_error=errors.append,
                )
            except OSError:
                by_folder[folder.name.casefold()] = None
            else:
                by_folder[folder.name.casefold()] = None if errors else count
    choosable = [
        definition
        for key, definition in taxonomy.kinds.items()
        if key not in source_taxonomy.UNCHOSEN_KINDS
        and key != source_taxonomy.EPISODE_KIND
        and definition.status != "deprecated"
    ]
    rows = [
        (by_folder.get(definition.path_label.casefold(), 0 if listing_complete else None), definition.key)
        for definition in choosable
    ]
    rows.sort(key=lambda row: (row[0] is None, -(row[0] or 0), row[1]))
    return [{"kind": key, "sources": count} for count, key in rows]


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


def _episode_frontmatter_lines(
    kind_key: str,
    fields: Mapping[str, object] | None,
    supersede: Sequence[tuple[str, str]],
) -> tuple[str, ...] | None:
    """The episode kind's extra frontmatter lines, or `None` for any other kind.

    Refuses before anything is planned: the kind without its fields (a plain
    capture filed as a recap nobody bound), the fields or `supersede` on any
    other kind, an unknown or missing field, and a value that is not one line.
    """
    is_episode = kind_key == source_taxonomy.EPISODE_KIND
    if not is_episode:
        if fields is not None or supersede:
            raise AddError(
                code="INVALID_SOURCE",
                missing=["extra_frontmatter"],
                reason=f"episode fields apply to the {source_taxonomy.EPISODE_KIND!r} kind only",
            )
        return None
    if fields is None:
        raise AddError(
            code="EPISODE_KIND_RESERVED",
            missing=["extra_frontmatter"],
            reason=(
                f"the {source_taxonomy.EPISODE_KIND!r} kind is reserved for "
                "conversation recaps; record one with episode_memory"
            ),
        )
    allowed = {name for name, _required in EPISODE_FRONTMATTER_FIELDS}
    unknown = sorted(set(fields) - allowed)
    missing = [
        name
        for name, required in EPISODE_FRONTMATTER_FIELDS
        if required and fields.get(name) in (None, "")
    ]
    if unknown or missing:
        raise AddError(
            code="INVALID_SOURCE",
            missing=missing or unknown,
            reason="episode frontmatter is a closed set: "
            + ", ".join(name for name, _required in EPISODE_FRONTMATTER_FIELDS),
        )

    def _line(value: object, name: str) -> str:
        if not isinstance(value, str) or not value.strip() or value != " ".join(value.split()):
            raise AddError(
                code="INVALID_SOURCE",
                missing=[name],
                reason=f"episode {name} must be one non-empty line",
            )
        return value

    lines: list[str] = []
    for name, _required in EPISODE_FRONTMATTER_FIELDS:
        value = fields.get(name)
        if value in (None, "", [], ()):
            continue
        lines.append(f"{name}: {yaml_scalar(_line(value, name))}")
    return tuple(lines)


def _supersede_targets(
    vault_root: Path, folder_path: Path, pairs: Sequence[tuple[str, str]]
) -> tuple[tuple[Path, str], ...]:
    """The earlier revisions `supersede` names, confined to the episode folder,
    each with the content hash its caller read.

    Checked before anything is created, so a refused call leaves no folder.
    """
    targets: dict[Path, str] = {}
    for pair in pairs:
        rel, expected = pair if isinstance(pair, tuple) and len(pair) == 2 else (None, None)
        target = (Path(vault_root) / rel).resolve() if isinstance(rel, str) else None
        if (
            target is None
            or not isinstance(expected, str)
            or target.parent != folder_path.resolve()
            or target.suffix != ".md"
            or not target.is_file()
        ):
            raise AddError(
                code="INVALID_SOURCE",
                missing=["supersede"],
                reason="only an earlier recap revision in the episode folder can be superseded",
            )
        targets.setdefault(target, expected)
    return tuple(targets.items())


def _supersede_writes(
    vault_root: Path,
    targets: Sequence[tuple[Path, str]],
    *,
    new_path: Path,
    stamp_iso: str,
) -> list[PlannedWrite]:
    """Frontmatter-only supersession of earlier recap revisions, as CAS writes.

    Guarded by the hash the CALLER read, not by this read: a revision another
    writer changed after the caller listed it (a concurrent record retiring it,
    say) fails the whole call here, and the batch carries the same hash, so a
    change landing after this read fails it at commit. A revision already
    superseded is not marked a second time.
    """
    if not targets:
        return []
    from .replace import _mark_superseded

    rel_new = new_path.relative_to(vault_root).with_suffix("").as_posix()
    link = render_wikilink_target(rel_new, vault_root)
    writes: list[PlannedWrite] = []
    for target, expected in targets:
        text = target.read_text(encoding="utf-8")
        actual = content_hash(text)
        if actual != expected:
            raise ContentHashMismatchError(target, expected, actual)
        frontmatter, _body, _raw = parse_frontmatter(text)
        if str(frontmatter.get("status") or "").casefold() == "superseded":
            continue
        updated = _mark_superseded(text, link, stamp_iso)
        if updated != text:
            writes.append(PlannedWrite(path=target, content=updated, expected_hash=expected))
    return writes


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


def _media_type_for(name: str) -> str | None:
    from . import media_types

    return media_types.media_type_for(name)


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
    artifact_name: str | None = None,
    artifact_rel: str | None = None,
    artifact_digest: str | None = None,
    artifact_size: int | None = None,
    adoption_receipt: Mapping[str, object] | None = None,
    extra_lines: Sequence[str] = (),
) -> str:
    """Emit the source page markdown matching frontmatter.md's example shape.

    When the source *is* an artifact, the page also points at the bytes and
    records their identity. The field names are the ones the media pipeline
    already writes and checks — `evidence_file`, `original_filename`,
    `binary_sha256`, `binary_size` — so one vocabulary describes an artifact
    whether its page was written at capture or refreshed by reconciliation.
    `evidence_file` is a misnomer in this tree; it is read in roughly fifteen
    places, and renaming it would cost the whole media pipeline for a better
    word.
    """
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
    lines.extend(extra_lines)
    if artifact_rel:
        media_type = _media_type_for(artifact_name or artifact_rel)
        if media_type:
            lines.append(f"media_type: {media_type}")
        lines.append(f"evidence_file: {artifact_rel}")
        if media_type:
            # `pending` is what invites the extraction worker. It is safe to
            # write on a classified Source now that reconciliation edits the
            # fields it owns instead of re-authoring the page.
            lines.append("extracted_by: pending")
        if artifact_name:
            lines.append(f"original_filename: {yaml_scalar(artifact_name)}")
        if artifact_digest:
            lines.append(f"binary_sha256: {artifact_digest}")
        if artifact_size is not None:
            lines.append(f"binary_size: {artifact_size}")
    if adoption_receipt is not None:
        from .preserve import _render_adoption_receipt_lines

        lines.extend(_render_adoption_receipt_lines(adoption_receipt))
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
