"""Existing-vault adoption report.

``adopt`` is the product-facing companion to ``overview``: it says what Exomem
found, what remains untouched, which packs look relevant, and which safe next
actions are available. The default mode is read-only and must work before
``Knowledge Base/`` exists. Write modes are explicit and only write under the
governed Knowledge Base layer.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    adoption_plan,
    capabilities,
    context_refs,
    held_fs,
    indexes,
    knowledge_packs,
    reserved_paths,
    semantic_census,
    source_taxonomy,
)
from . import compile_proposal as compile_proposal_module
from . import overview as overview_module
from .kbdir import kb_dirname, kb_prefix
from .vault import (
    MISSING_CONTENT_HASH,
    PlannedWrite,
    VaultPathError,
    batch_atomic_write,
    kb_root,
    prepend_log_entry,
    resolve_under_vault,
    unique_path,
    yaml_scalar,
)

DEFAULT_MODE = "scan-only"
SUPPORTED_MODES = ("scan-only", "save-manifest", "copy-as-sources", "compile-selected")
PLANNED_MODES: tuple[str, ...] = ()
ADOPTION_DIR = "_Adoption"
IMPORTED_SOURCE_FOLDER = source_taxonomy.IMPORTED_PATH_LABEL
_APPLIED_OUTCOME_STATUSES = frozenset({"applied", "already-applied"})
_TEXT_IMPORT_SUFFIXES = frozenset(
    {".md", ".markdown", ".txt", ".text", ".csv", ".tsv", ".json", ".yaml", ".yml", ".rst", ".log"}
)


class AdoptError(Exception):
    """Structured failure: ``code`` is machine-readable, ``reason`` human-readable."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def _safe_next_actions(kb_present: bool) -> list[dict]:
    actions = [
        {
            "action": "scan-only",
            "status": "done",
            "description": "Read-only structure and pack report. No files changed.",
        }
    ]
    if kb_present:
        actions.extend(
            [
                {
                    "action": "save-manifest",
                    "status": "available",
                    "description": (
                        f"Save this adoption report under {kb_dirname()}/{ADOPTION_DIR}/ "
                        "without touching sibling folders."
                    ),
                },
                {
                    "action": "copy-as-sources",
                    "status": "available",
                    "description": (
                        f"Copy explicitly selected legacy text files into governed "
                        f"{kb_dirname()}/Sources/{IMPORTED_SOURCE_FOLDER}/ with original "
                        "path/hash provenance. Originals remain unchanged."
                    ),
                },
                {
                    "action": "compile-selected",
                    "status": "available",
                    "description": (
                        "Copy explicitly selected legacy text files as governed Sources "
                        "when needed, then return a compile plan. No compiled note is "
                        "created until the user deliberately calls note()."
                    ),
                },
            ]
        )
    else:
        actions.append(
            {
                "action": "initialize-kb",
                "status": "available",
                "description": (
                    f"Run setup/init to create {kb_dirname()}/ beside existing files "
                    "before saving manifests or compiled knowledge."
                ),
            }
        )
    return actions


def _governance(scan: dict) -> dict:
    kb = scan.get("kb") or {}
    present = bool(kb.get("present"))
    return {
        "kb_present": present,
        "governed_path": kb.get("path") if present else None,
        "read_only_input": (
            "All existing files outside the governed Knowledge Base are searchable "
            "read-only input. Exomem does not rewrite, move, delete, or add "
            "frontmatter to them by default."
        ),
    }


def _require_kb(root: Path) -> None:
    if not (root / kb_dirname()).is_dir():
        raise AdoptError(
            "KB_NOT_INITIALIZED",
            f"{kb_dirname()}/ is required for this adopt mode; run setup/init first",
        )


def _today(today: dt.date | None) -> dt.date:
    return today or dt.date.today()


def _resolve_manifest_path(root: Path, manifest_path: str | None, today: dt.date) -> tuple[Path, str]:
    raw = (manifest_path or "").strip().replace("\\", "/")
    defaulted = not raw
    if defaulted:
        raw = f"{kb_prefix()}{ADOPTION_DIR}/{today.isoformat()}-adoption-manifest.md"
    elif "/" not in raw:
        raw = f"{kb_prefix()}{ADOPTION_DIR}/{raw}"
    if raw.endswith("/"):
        raise AdoptError("INVALID_MANIFEST_PATH", "manifest_path must name a markdown file")
    if not raw.startswith(kb_prefix()):
        raise AdoptError(
            "INVALID_MANIFEST_PATH",
            f"manifest_path must be under {kb_dirname()}/",
        )
    if not raw.lower().endswith(".md"):
        raw += ".md"
    try:
        target, rel = resolve_under_vault(root, raw)
    except VaultPathError as e:
        raise AdoptError(e.code, e.reason) from e
    if not rel.startswith(kb_prefix()):
        raise AdoptError(
            "INVALID_MANIFEST_PATH",
            f"manifest_path must be under {kb_dirname()}/",
        )
    from .governance import connector_boundary

    try:
        connector_boundary.require_create(root, rel)
    except ValueError as error:
        raise AdoptError("WRITE_REFUSED", "target is unavailable") from error
    if defaulted:
        target = unique_path(target.parent, target.stem, target.suffix, vault_root=root)
        rel = target.relative_to(root).as_posix()
    elif target.exists():
        raise AdoptError("MANIFEST_EXISTS", f"manifest already exists: {rel}")
    return target, rel


def _render_manifest(report: dict[str, Any], *, rel_path: str, today: dt.date) -> str:
    suggested = report.get("pack_suggestions") or []
    actions = report.get("next_actions") or []
    totals = ((report.get("summary") or {}).get("totals") or {})
    lines = [
        "---",
        "type: adoption-manifest",
        f"created: {today.isoformat()}",
        "status: active",
        "tags: [adoption, onboarding]",
        "---",
        "",
        "# Adoption Manifest",
        "",
        f"Saved at `{rel_path}` by `adopt(mode=\"save-manifest\")`.",
        "",
        "## Write Contract",
        "",
        str(report.get("write_contract", "")),
        "",
        "## Scan Summary",
        "",
        f"- Files: {totals.get('files', 0)}",
        f"- Directories: {totals.get('dirs', 0)}",
        f"- Markdown: {totals.get('markdown', 0)}",
        f"- Binary/media-like files: {totals.get('binary', 0)}",
        "",
        "## Suggested Knowledge Packs",
        "",
    ]
    if suggested:
        for pack in suggested:
            signals = ", ".join(pack.get("matched_signals") or [])
            suffix = f" (signals: {signals})" if signals else ""
            lines.append(f"- {pack.get('name', pack.get('id'))}{suffix}")
    else:
        lines.append("- None suggested by structural signals.")
    lines.extend(["", "## Safe Next Actions", ""])
    for action in actions:
        lines.append(
            f"- {action.get('action')} [{action.get('status')}]: {action.get('description')}"
        )
    # No machine-readable dump here, for the same reason `_render_run_manifest`
    # carries counts only: this manifest is a released page, a full-level release
    # is byte-identical to the ungoverned response, and page BODIES are never
    # scanned by the artifact-reference gate (it only treats `handoff`/`prompt`/
    # `resource` keys as free text). A `json.dumps(_compact_report(...))` fence
    # embeds `overview`, `refs`, `copy` and `compile_plan` — every scanned path
    # and target — so one read of this page would release the whole inventory
    # even where each individual path is withheld. The sections above carry the
    # human-readable summary; the full report stays available live from
    # `adopt(mode="scan-only")`, where a disclosure decision applies.
    lines.append("")
    return "\n".join(lines)


def _add_manifest_index_writes(
    *,
    root: Path,
    writes: list[PlannedWrite],
    rel_path: str,
    today: dt.date,
) -> list[str]:
    warnings: list[str] = []
    kb = kb_root(root)
    rel_no_ext = rel_path[:-3] if rel_path.endswith(".md") else rel_path
    top_index = kb / "index.md"
    if top_index.exists():
        top_text, _trim = indexes._prepend_recent_activity(
            top_index.read_text(encoding="utf-8"),
            date_iso=today.isoformat(),
            summary=f"`{rel_no_ext.removeprefix(kb_prefix())}` (adoption manifest) — saved scan-first existing-vault report",
        )
        sub_writes, top_text = indexes.compute_subindex_writes(
            root,
            top_index_text=top_text,
        )
        writes.append(PlannedWrite(path=top_index, content=top_text or top_index.read_text(encoding="utf-8")))
        writes.extend(sub_writes)
    else:
        warnings.append(f"{kb_prefix()}index.md missing; skipped Recent activity bump")
    log_file = kb / "log.md"
    if log_file.exists():
        writes.append(
            PlannedWrite(
                path=log_file,
                content=prepend_log_entry(
                    log_file.read_text(encoding="utf-8"),
                    date_iso=today.isoformat(),
                    op="adopt",
                    rel_path_no_ext=rel_no_ext,
                    body="Saved existing-vault adoption manifest. Originals remain untouched.",
                ),
            )
        )
    else:
        warnings.append(f"{kb_prefix()}log.md missing; skipped log entry")
    return warnings


def _save_manifest(
    root: Path,
    report: dict[str, Any],
    *,
    manifest_path: str | None,
    today: dt.date,
) -> dict:
    _require_kb(root)
    target, rel = _resolve_manifest_path(root, manifest_path, today)
    content = _render_manifest(report, rel_path=rel, today=today)
    writes = [PlannedWrite(path=target, content=content)]
    warnings = _add_manifest_index_writes(root=root, writes=writes, rel_path=rel, today=today)
    batch_atomic_write(writes, vault_root=root)
    return {"path": rel, "ref": context_refs.manifest_ref(rel), "warnings": warnings}


def _fence_for(text: str) -> str:
    run = 3
    while "`" * run in text:
        run += 1
    return "`" * run


def _clean_inline(value: str) -> str:
    """A value for an inline-code span: no backtick or line break can end it early."""
    return " ".join(value.replace("`", "'").split())


def _links_section(planned: Mapping[str, Any]) -> list[str]:
    """The generated `## Links` section, which sits outside the verbatim fence.

    A wikilink in a Source body becomes a graph edge, so each resolved target is
    one plain wikilink to that target's frozen Source page, keeping the fragment
    the original link named. An unresolved target is listed as text: a guessed
    edge would be worse than a missing one.
    """
    links = planned.get("links") or {}
    resolved = links.get("resolved") or []
    unresolved = links.get("unresolved") or []
    if not resolved and not unresolved:
        return []
    lines = ["## Links", ""]
    seen: set[str] = set()
    for link in resolved:
        target = adoption_plan.wikilink_for(link["target_path"])
        fragment = link.get("fragment")
        # A fragment decoded from a Markdown link may hold the characters that end a
        # wikilink (the wikilink grammar's own delimiters); such a fragment is dropped.
        if fragment and not any(ch in fragment for ch in "[]|\r\n"):
            target = f"{target}#{fragment}"
        if target not in seen:
            seen.add(target)
            lines.append(f"- [[{target}]]")
    for link in unresolved:
        lines.append(f"- unresolved ({link['reason']}): `{_clean_inline(link['raw'])}`")
    lines.append("")
    return lines


def _provenance_lines(planned: Mapping[str, Any], collection: str | None) -> list[str]:
    """Front-matter lines that say where an imported Source came from."""
    lines = [f"imported_from: {yaml_scalar(planned['original_path'])}"]
    if collection is not None:
        lines.append(f"import_collection: {yaml_scalar(adoption_plan.nfc(collection))}")
        lines.append(f"collection_path: {yaml_scalar(planned['collection_path'])}")
    lines.append(f"original_sha256: {planned['original_sha256']}")
    lines.append(f"original_bytes: {planned['original_bytes']}")
    if planned.get("previous_import"):
        lines.append(f"previous_import: {yaml_scalar(planned['previous_import'])}")
    return lines


def _kind_lines(planned: Mapping[str, Any]) -> tuple[str, list[str]]:
    """``(source_type, domain lines)`` from the item's classification."""
    classification = planned.get("classification") or {}
    kind = classification.get("source_kind") or source_taxonomy.UNCLASSIFIED_KIND
    domain = classification.get("domain")
    return kind, [f"domain: {domain}"] if domain else []


def render_text_source(
    planned: Mapping[str, Any], text: str, *, date_iso: str, collection: str | None
) -> str:
    """The imported-Source page of a text original.

    The original text sits in its fence exactly as read: the opening fence, a
    newline, the text, a newline, the closing fence. Nothing is trimmed, so the
    text between the fences encodes to bytes whose SHA-256 is ``original_sha256``.
    """
    title = planned["title"]
    rel_original = planned["original_path"]
    kind, domain_lines = _kind_lines(planned)
    lines = [
        "---",
        "type: source",
        f"title: {yaml_scalar(title)}",
        f"source_type: {kind}",
        *domain_lines,
        f"captured: {date_iso}",
        *_provenance_lines(planned, collection),
    ]
    if planned.get("aliases"):
        lines.append("aliases: [" + ", ".join(yaml_scalar(a) for a in planned["aliases"]) + "]")
    tags = ["imported", *planned.get("tags", [])]
    lines += [
        "tags: [" + ", ".join(yaml_scalar(t) for t in tags) + "]",
        "ingested_into: []",
        "---",
        "",
        f"# {title}",
        "",
        f"> Copied from `{_clean_inline(rel_original)}` by Exomem adoption. The original remains unchanged.",
        "",
        "## Original Metadata",
        "",
        f"- Original path: `{_clean_inline(rel_original)}`",
        f"- SHA-256: `{planned['original_sha256']}`",
        f"- Bytes: {planned['original_bytes']}",
        "",
        *_links_section(planned),
        "## Capture",
        "",
    ]
    fence = _fence_for(text)
    return "\n".join([*lines, fence, text, fence, ""])


def render_artifact_source(
    planned: Mapping[str, Any], *, date_iso: str, collection: str | None
) -> str:
    """The Source page of an artifact original, in capture's own page format."""
    from . import add as add_module
    from . import memory_refs
    from .preserve import _sanitize_filename

    kind, domain_lines = _kind_lines(planned)
    name = _sanitize_filename(Path(planned["original_path"]).name)
    return add_module._render_source(
        title=planned["title"],
        source_type=kind,
        date_iso=date_iso,
        url=None,
        tags=["imported"],
        why_captured=(
            f"Copied from `{_clean_inline(planned['original_path'])}` by Exomem adoption. "
            "The original remains unchanged."
        ),
        content=add_module._describe_artifact(
            name, planned["original_sha256"], planned["original_bytes"]
        ),
        exomem_id=memory_refs.new_id(),
        domain=(planned.get("classification") or {}).get("domain"),
        artifact_name=name,
        artifact_rel=planned["artifact_path"],
        artifact_digest=planned["original_sha256"],
        artifact_size=planned["original_bytes"],
        extra_lines=_provenance_lines(planned, collection),
    )


def _resolve_selected_text_file(root: Path, raw: str) -> tuple[Path, str] | dict:
    try:
        abs_path, rel = resolve_under_vault(root, raw, must_exist=True, must_be_file=True)
    except VaultPathError as e:
        return {"path": raw, "code": e.code, "reason": e.reason}
    if rel.startswith(kb_prefix()):
        return {
            "path": rel,
            "code": "ALREADY_GOVERNED",
            "reason": f"copy-as-sources expects legacy files outside {kb_dirname()}/",
            "ref": context_refs.vault_ref(rel),
        }
    if abs_path.suffix.lower() not in _TEXT_IMPORT_SUFFIXES:
        return {
            "path": rel,
            "code": "UNSUPPORTED_IMPORT_TYPE",
            "reason": "copy-as-sources currently imports text/markdown-like files only",
            "ref": context_refs.vault_ref(rel),
        }
    return abs_path, rel


@dataclass
class ImportArtifact:
    """The original bytes an artifact Source keeps, and where they are published."""

    target: Path  # absolute destination beside the page
    digest: str
    size: int


@dataclass
class ImportItem:
    """One resolved legacy file staged for a governed Sources import.

    Built from a plan item once its bytes are verified, and consumed by
    ``commit_import_items`` (the single batch write). The stateless
    ``copy-as-sources`` mode and the durable adoption run share that commit.
    """

    original_path: str  # vault-relative path of the untouched original
    target_rel: str  # vault-relative path of the governed Sources page
    target: Path  # absolute page destination
    sha256: str
    bytes: int
    title: str
    content: str  # fully rendered imported-Source markdown
    slug_warning: str | None = None
    artifact: ImportArtifact | None = None  # set for an artifact original


def import_item(
    root: Path,
    planned: Mapping[str, Any],
    *,
    text: str | None,
    date_iso: str,
    collection: str | None,
) -> ImportItem:
    """Render one plan item. ``text`` is the verified original for a text item."""
    artifact = None
    if planned["form"] == "artifact":
        content = render_artifact_source(planned, date_iso=date_iso, collection=collection)
        artifact = ImportArtifact(
            target=root / planned["artifact_path"],
            digest=planned["original_sha256"],
            size=planned["original_bytes"],
        )
    else:
        assert text is not None
        content = render_text_source(planned, text, date_iso=date_iso, collection=collection)
    return ImportItem(
        original_path=planned["original_path"],
        target_rel=planned["target_path"],
        target=root / planned["target_path"],
        sha256=planned["original_sha256"],
        bytes=planned["original_bytes"],
        title=planned["title"],
        content=content,
        slug_warning=planned.get("slug_warning"),
        artifact=artifact,
    )


def plan_import_items(
    root: Path,
    selected_paths: list[str] | None,
    *,
    today: dt.date,
) -> tuple[list[ImportItem], list[dict]]:
    """Resolve, read, hash, link and render each selected legacy text file. No writes.

    Returns ``(items, skipped)``. ``skipped`` carries per-path refusals
    (``UNSUPPORTED_IMPORT_TYPE``, ``ALREADY_GOVERNED``, vault-path errors,
    ``NOT_VALID_UTF8``) and an ``ALREADY_IMPORTED`` row, with the earlier
    Source's ref, for a file whose bytes an earlier import already holds.
    """
    from . import client_artifacts

    date_iso = today.isoformat()
    skipped: list[dict] = []
    index = adoption_plan.read_import_index(root)
    destinations = adoption_plan.Destinations(root)
    ceiling = client_artifacts.fetch_budget().max_file_bytes
    planned: list[dict] = []
    texts: dict[str, str] = {}

    for raw in selected_paths or []:
        resolved = _resolve_selected_text_file(root, raw)
        if isinstance(resolved, dict):
            skipped.append(resolved)
            continue
        _abs_path, rel = resolved
        result = adoption_plan.plan_item(
            root, rel, collection=None, collection_root="", classification=None,
            taxonomy=None, text_hint=True, date_iso=date_iso, destinations=destinations,
            index=index, ceiling=ceiling,
        )
        if isinstance(result, dict):
            skipped.append(result)
            continue
        item, text = result
        if item["form"] != "text":
            skipped.append(
                {"path": rel, "code": "NOT_VALID_UTF8",
                 "reason": "copy-as-sources imports valid UTF-8 text only; Adoption Studio imports other files as artifacts"}
            )
            continue
        planned.append(item)
        if text is not None:
            texts[rel] = text

    adoption_plan.resolve_links(planned, index, collection=None)
    items: list[ImportItem] = []
    for item in planned:
        if item["action"] == "already-imported":
            skipped.append(
                {
                    "path": item["original_path"],
                    "code": "ALREADY_IMPORTED",
                    "reason": "an earlier import already holds these bytes",
                    "source_path": item["target_path"],
                    "source_ref": item["existing_ref"],
                    "original_sha256": item["original_sha256"],
                    "original_bytes": item["original_bytes"],
                }
            )
            continue
        items.append(
            import_item(
                root, item, text=texts[item["original_path"]], date_iso=date_iso, collection=None
            )
        )
        if item.get("slug_warning"):
            skipped.append(
                {"path": item["original_path"], "code": "SLUG_TRUNCATED", "reason": item["slug_warning"]}
            )
    return items, skipped


def _top_folder(rel: str) -> str:
    """The Sources folder a page is counted under: its first segment below `Sources/`."""
    return Path(rel).relative_to(Path(kb_dirname()) / "Sources").parts[0]


def commit_import_items(
    root: Path,
    items: list[ImportItem],
    *,
    today: dt.date,
    extra_writes: Sequence[PlannedWrite] = (),
    source_counts: Mapping[str, int] | None = None,
) -> dict:
    """Write every planned import plus the Sources/top indexes and log in ONE batch.

    Index counts and the log come from where each item actually lands, so a
    classified item counts under its own kind. ``extra_writes`` ride in the same
    batch (the run object, so outcomes commit with the items they describe).
    ``source_counts`` is the Sources census before this batch, for a caller that
    commits many batches and keeps it current from the returned ``source_counts``
    (the census after this batch); without it the Sources tree is walked.
    Returns ``{"copied_sources": [...], "warnings": [...], "source_counts": {...}}``.
    """
    if not items:
        if extra_writes:
            batch_atomic_write(list(extra_writes), vault_root=root)
        return {"copied_sources": [], "warnings": [], "source_counts": source_counts}

    from .add import AddError, artifact_binary_write, artifact_identity

    kb = kb_root(root)
    date_iso = today.isoformat()
    writes: list[PlannedWrite] = []
    copied: list[dict] = []
    held_sources: list[reserved_paths.GenericBinarySource] = []
    with ExitStack() as originals:
        for item in items:
            source = originals.enter_context(
                reserved_paths.open_generic_binary(root, item.original_path, physical=True)
            )
            held_sources.append(source)
            if source.version.size != item.bytes:
                raise AdoptError("SOURCE_CHANGED", "the original changed before import")
            if item.artifact is None:
                try:
                    digest, size = artifact_identity(source.stream, ceiling=item.bytes)
                except AddError as error:
                    raise AdoptError("SOURCE_CHANGED", "the original changed before import") from error
                if digest != item.sha256 or size != item.bytes:
                    raise AdoptError("SOURCE_CHANGED", "the original changed before import")
            writes.append(
                PlannedWrite(
                    path=item.target, content=item.content,
                    create_only=True, expected_hash=MISSING_CONTENT_HASH,
                )
            )
            if item.artifact is not None:
                binary, _stream = artifact_binary_write(
                    source.stream, item.artifact.target,
                    item.artifact.digest, item.artifact.size,
                )
                writes.append(binary)
            copied.append(
                {
                    "original_path": item.original_path,
                    "original_ref": context_refs.vault_ref(item.original_path),
                    "source_path": item.target_rel,
                    "source_ref": context_refs.source_ref(item.target_rel),
                    "artifact_path": (
                        item.artifact.target.relative_to(root).as_posix() if item.artifact else None
                    ),
                    "original_sha256": item.sha256,
                    "original_bytes": item.bytes,
                }
            )

        sources_dir = kb / "Sources"
        pre_counts = (
            indexes._count_sources(sources_dir) if source_counts is None else dict(source_counts)
        )
        post_counts = dict(pre_counts)
        folders: list[str] = []
        for row in copied:
            folder = _top_folder(row["source_path"])
            post_counts[folder] = post_counts.get(folder, 0) + 1
            if folder not in folders:
                folders.append(folder)

        sources_index = sources_dir / "index.md"
        warnings: list[str] = []
        if sources_index.exists():
            sources_text = indexes._replace_by_type_section(
                sources_index.read_text(encoding="utf-8"),
                folder_title=folders[0],
                folder_description="copied legacy-vault material with provenance",
                counts=post_counts,
                vault_root=root,
            )
            for row in reversed(copied):
                sources_text = indexes._prepend_recent_capture(
                    sources_text, date_iso=date_iso, rel_source_path=row["source_path"][:-3]
                )
            writes.append(PlannedWrite(path=sources_index, content=sources_text))
        else:
            warnings.append(f"{kb_prefix()}Sources/index.md missing; skipped Sources index update")

        where = ", ".join(f"Sources/{folder}/" for folder in folders)
        top_index = kb / "index.md"
        if top_index.exists():
            summary = (
                f"{where} (imported sources) - copied "
                f"{len(copied)} selected file(s) with original path/hash provenance"
            )
            top_text, trim_note = indexes._update_top_index(
                top_index.read_text(encoding="utf-8"),
                counts=post_counts,
                date_iso=date_iso,
                activity_summary=summary,
            )
            sub_writes, top_text = indexes.compute_subindex_writes(
                root,
                top_index_text=top_text,
                pending_paths=[row["source_path"] for row in copied],
                sources_counts=pre_counts,
            )
            sub_writes = [write for write in sub_writes if write.path != sources_index]
            writes.append(
                PlannedWrite(path=top_index, content=top_text or top_index.read_text(encoding="utf-8"))
            )
            writes.extend(sub_writes)
            if trim_note:
                warnings.append(trim_note)
        else:
            warnings.append(f"{kb_prefix()}index.md missing; skipped Recent activity bump")

        log_file = kb / "log.md"
        if log_file.exists():
            body_lines = [
                f"Copied {len(copied)} selected file(s) into {where}. Originals remain unchanged.",
                "",
            ]
            for row in copied:
                body_lines.append(
                    f"- {row['original_path']} -> {row['source_path']} sha256={row['original_sha256']}"
                )
            writes.append(
                PlannedWrite(
                    path=log_file,
                    content=prepend_log_entry(
                        log_file.read_text(encoding="utf-8"),
                        date_iso=date_iso,
                        op="adopt-copy",
                        rel_path_no_ext=f"{kb_prefix()}Sources",
                        body="\n".join(body_lines),
                    ),
                )
            )
        else:
            warnings.append(f"{kb_prefix()}log.md missing; skipped log entry")

        writes.extend(extra_writes)

        def require_sources_current() -> None:
            for source in held_sources:
                source.require_current()

        batch_atomic_write(
            writes, vault_root=root, _validate_prepared_bindings=require_sources_current
        )
    return {"copied_sources": copied, "warnings": warnings, "source_counts": post_counts}


def _copy_as_sources(
    root: Path,
    *,
    selected_paths: list[str] | None,
    today: dt.date,
) -> dict:
    _require_kb(root)
    if not selected_paths:
        raise AdoptError(
            "MISSING_SELECTION",
            "copy-as-sources requires selected_paths; scan first, then pass explicit files",
        )

    items, skipped = plan_import_items(root, selected_paths, today=today)
    if not items:
        return {"copied_sources": [], "skipped": skipped, "warnings": ["no importable files copied"]}

    try:
        result = commit_import_items(root, items, today=today)
    except (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError) as error:
        raise AdoptError(**adoption_plan.source_failure(error)) from error
    return {
        "copied_sources": result["copied_sources"],
        "skipped": skipped,
        "warnings": result["warnings"],
    }


def _run_manifest_counts(summary: dict) -> list[str]:
    """Count lines for the run manifest — never a path, title, hash or item code.

    Mirrors ``adoption_run._APPLIED_STATUSES``; importing it would be a cycle
    (``adoption_run`` imports this module).
    """
    outcomes = summary.get("outcomes") or {}
    applied = sum(
        1
        for outcome in outcomes.values()
        if isinstance(outcome, dict) and outcome.get("status") in _APPLIED_OUTCOME_STATUSES
    )
    recall_ok = bool((summary.get("recall_check") or {}).get("ok"))
    return [
        f"- Selected: {len(summary.get('selection') or [])}",
        f"- Applied: {applied}",
        f"- Not applied: {len(outcomes) - applied}",
        f"- Verified unchanged: {summary.get('verified_unchanged', 0)} of "
        f"{summary.get('verified_total', 0)}",
        f"- Recall check: {'ok' if recall_ok else 'needs attention'}",
    ]


def _render_run_manifest(*, run_id: str, summary: dict, today: dt.date, rel_path: str) -> str:
    """Render the run manifest body: counts and the run reference only.

    The manifest is an ordinary page. A page released at full level is returned
    byte-identically and only named frontmatter/page provenance fields are
    stripped — the body is never scanned. So a machine-readable dump of the run
    here would hand a caller every source path, target path and content hash in
    the run even when each individual item is withheld. Per-item detail stays in
    the run object, read through ``adoption_studio(action="status")`` where a
    disclosure decision applies.
    """
    lines = [
        "---",
        "type: adoption-run-manifest",
        f"created: {today.isoformat()}",
        f"run_id: {run_id}",
        "status: active",
        "tags: [adoption, onboarding]",
        "---",
        "",
        "# Adoption Run Manifest",
        "",
        f"Saved at `{rel_path}` by `{capabilities.active_call_text('adoption_studio', {'action': 'finish', 'run_id': run_id})}`. Originals remain untouched.",
        "",
        "## Run",
        "",
        f"- Run ref: `{summary.get('run_ref', '')}`",
        f"- Phase: {summary.get('phase', '')}",
        "",
        "## Counts",
        "",
        *_run_manifest_counts(summary),
        "",
        "Per-item detail (sources, targets, hashes) stays in the run object. Read it with",
        f"`{capabilities.active_call_text('adoption_studio', {'action': 'status', 'run_id': run_id})}`.",
        "",
    ]
    return "\n".join(lines)


def run_manifest_writes(
    root: Path,
    *,
    run_id: str,
    summary: dict,
    today: dt.date,
) -> tuple[list[PlannedWrite], str, list[str]]:
    """Planned writes for a durable adoption-run manifest under ``_Adoption/``.

    Reuses ``_add_manifest_index_writes`` (the shared manifest index/log helper)
    so the run manifest bumps Recent activity and the log exactly like the
    stateless ``save-manifest`` mode. Returns ``(writes, rel_path, warnings)``;
    the caller folds these into (or commits alongside) its own batch.
    """
    rel = f"{kb_prefix()}{ADOPTION_DIR}/{today.isoformat()}-adoption-run-{run_id}.md"
    target = root / rel
    writes: list[PlannedWrite] = [
        PlannedWrite(
            path=target,
            content=_render_run_manifest(run_id=run_id, summary=summary, today=today, rel_path=rel),
        )
    ]
    warnings = _add_manifest_index_writes(root=root, writes=writes, rel_path=rel, today=today)
    return writes, rel, warnings


def _source_wikilink_path(rel_path: str) -> str:
    return rel_path[:-3] if rel_path.lower().endswith(".md") else rel_path


def _resolve_compile_selection(root: Path, raw: str) -> dict:
    try:
        abs_path, rel = resolve_under_vault(root, raw, must_exist=True, must_be_file=True)
    except VaultPathError as e:
        return {"kind": "skip", "path": raw, "code": e.code, "reason": e.reason}

    if rel.startswith(kb_prefix()):
        if rel.startswith(f"{kb_prefix()}Sources/") and rel.lower().endswith(".md"):
            return {"kind": "governed_source", "abs_path": abs_path, "rel": rel}
        return {
            "kind": "skip",
            "path": rel,
            "code": "NOT_ADOPTION_SOURCE",
            "reason": "compile-selected expects legacy files or governed Sources",
            "ref": context_refs.vault_ref(rel),
        }

    if abs_path.suffix.lower() not in _TEXT_IMPORT_SUFFIXES:
        return {
            "kind": "skip",
            "path": rel,
            "code": "UNSUPPORTED_IMPORT_TYPE",
            "reason": "compile-selected currently imports text/markdown-like files only",
            "ref": context_refs.vault_ref(rel),
        }
    return {"kind": "legacy_text", "abs_path": abs_path, "rel": rel}


def _compile_selected(
    root: Path,
    *,
    selected_paths: list[str] | None,
    today: dt.date,
) -> dict:
    _require_kb(root)
    if not selected_paths:
        raise AdoptError(
            "MISSING_SELECTION",
            "compile-selected requires selected_paths; scan first, then pass explicit files",
        )

    legacy_paths: list[str] = []
    planned_sources: list[dict] = []
    skipped: list[dict] = []
    warnings: list[str] = []
    copied_sources: list[dict] = []

    for raw in selected_paths:
        resolved = _resolve_compile_selection(root, raw)
        kind = resolved.get("kind")
        if kind == "governed_source":
            rel = resolved["rel"]
            data = resolved["abs_path"].read_bytes()
            planned_sources.append(
                {
                    "source_path": rel,
                    "source_wikilink": _source_wikilink_path(rel),
                    "source_ref": context_refs.source_ref(rel),
                    "source_sha256": hashlib.sha256(data).hexdigest(),
                    "source_bytes": len(data),
                    "already_governed": True,
                }
            )
        elif kind == "legacy_text":
            legacy_paths.append(resolved["rel"])
        else:
            item = dict(resolved)
            item.pop("kind", None)
            skipped.append(item)

    if legacy_paths:
        copy_result = _copy_as_sources(root, selected_paths=legacy_paths, today=today)
        copied_sources = copy_result.get("copied_sources") or []
        for row in copy_result.get("skipped") or []:
            if row.get("code") == "ALREADY_IMPORTED":
                # The earlier Source is the material to compile; nothing is copied again.
                copied_sources.append(
                    {
                        "original_path": row["path"],
                        "source_path": row["source_path"],
                        "source_ref": row["source_ref"],
                        "original_sha256": row["original_sha256"],
                        "original_bytes": row["original_bytes"],
                    }
                )
            else:
                skipped.append(row)
        warnings.extend(copy_result.get("warnings") or [])
        for item in copied_sources:
            source_path = item["source_path"]
            planned_sources.append(
                {
                    "original_path": item["original_path"],
                    "original_ref": item.get("original_ref") or context_refs.vault_ref(item["original_path"]),
                    "source_path": source_path,
                    "source_wikilink": _source_wikilink_path(source_path),
                    "source_ref": item.get("source_ref") or context_refs.source_ref(source_path),
                    "original_sha256": item["original_sha256"],
                    "original_bytes": item["original_bytes"],
                    "already_governed": False,
                }
            )

    if not planned_sources:
        return {
            "status": "empty",
            "proposal_ref": None,
            "sources": [],
            "copied_sources": copied_sources,
            "skipped": skipped,
            "warnings": warnings + ["no source material available for compilation plan"],
            "proposal": None,
        }

    source_wikilinks = [item["source_wikilink"] for item in planned_sources]
    try:
        proposal = compile_proposal_module.propose_compilation(root, sources=source_wikilinks)
    except compile_proposal_module.ProposeError as e:
        raise AdoptError(e.code, e.reason) from e

    proposal_sources = proposal.get("suggested_sources") or source_wikilinks
    proposal_ref = context_refs.proposal_ref(proposal_sources)
    proposal["proposal_ref"] = proposal_ref
    return {
        "status": "ready",
        "proposal_ref": proposal_ref,
        "sources": planned_sources,
        "copied_sources": copied_sources,
        "skipped": skipped,
        "warnings": warnings + list(proposal.get("warnings") or []),
        "proposal": proposal,
        "next_step": "Review outline_markdown, then call note() with suggested_sources when ready.",
    }

def adopt(
    root: Path | str,
    *,
    path: str = "",
    mode: str = DEFAULT_MODE,
    max_depth: int = overview_module.DEFAULT_MAX_DEPTH,
    include_hidden: bool = False,
    samples: int = 5,
    pack_limit: int = 6,
    manifest_path: str | None = None,
    selected_paths: list[str] | None = None,
    semantic_max_files: int = semantic_census.DEFAULT_MAX_FILES,
    semantic_max_bytes: int = semantic_census.DEFAULT_MAX_BYTES,
    semantic_example_limit: int = semantic_census.DEFAULT_EXAMPLE_LIMIT,
    today: dt.date | None = None,
) -> dict:
    """Return an adoption report for an existing vault.

    Args:
        path: Optional vault subtree to scan. Defaults to the vault root.
        mode: Adoption mode. ``scan-only`` is read-only; write modes are
            explicit and only write under ``Knowledge Base/``.
        max_depth: Folder-tree depth cap passed to ``overview``.
        include_hidden: Include hidden files/directories in the scan.
        samples: Sample filename count per folder.
        pack_limit: Maximum pack suggestions to return.
        manifest_path: Optional markdown destination for ``save-manifest``.
        selected_paths: Explicit vault-relative files for ``copy-as-sources`` or ``compile-selected``.
        semantic_max_files: Maximum Markdown files read by the scan-only semantic census.
        semantic_max_bytes: Maximum total Markdown bytes read by the scan-only semantic census.
        semantic_example_limit: Maximum examples per semantic census diagnostic grouping.

    Scan-only never writes. Unsupported modes fail explicitly instead of
    silently pretending to migrate content.
    """
    if mode not in SUPPORTED_MODES:
        supported = ", ".join(SUPPORTED_MODES)
        raise AdoptError(
            "UNSUPPORTED_MODE",
            f"adopt mode {mode!r} is not supported; supported modes: {supported}",
        )
    root_path = Path(root)
    run_date = _today(today)
    try:
        scan = overview_module.overview(
            root_path,
            path=path,
            max_depth=max_depth,
            include_hidden=include_hidden,
            samples=samples,
        )
    except overview_module.OverviewError as e:
        raise AdoptError(e.code, e.reason) from e

    governance = _governance(scan)
    kb_present = bool(governance["kb_present"])
    report: dict[str, Any] = {
        "mode": mode,
        "implemented_modes": list(SUPPORTED_MODES),
        "planned_modes": list(PLANNED_MODES),
        "scope_note": overview_module.SCOPE_NOTE,
        "write_contract": (
            "Default adoption is read-only. Originals stay where they are. "
            "Governed Exomem writes happen only under Knowledge Base/ after an "
            "explicit save/copy/compile action."
        ),
        "governance": governance,
        "summary": {
            "root": scan.get("root", ""),
            "totals": scan.get("totals", {}),
            "kb": scan.get("kb", {}),
            "junk_counts": (scan.get("junk") or {}).get("counts", {}),
            "skipped": scan.get("skipped", {}),
        },
        "pack_suggestions": knowledge_packs.suggest_packs(scan, limit=pack_limit),
        "available_packs": knowledge_packs.list_builtin_packs(),
        "pack_schema": knowledge_packs.pack_schema(),
        "next_actions": _safe_next_actions(kb_present),
        "refs": {"root": context_refs.vault_ref(path or "")},
        "overview": scan,
    }
    if mode == "scan-only":
        try:
            report["semantic_census"] = semantic_census.scan(
                root_path,
                path=path,
                include_hidden=include_hidden,
                max_files=semantic_max_files,
                max_bytes=semantic_max_bytes,
                example_limit=semantic_example_limit,
            )
        except (overview_module.OverviewError, ValueError) as e:
            if isinstance(e, overview_module.OverviewError):
                raise AdoptError(e.code, e.reason) from e
            raise AdoptError("INVALID_SEMANTIC_CENSUS_LIMIT", str(e)) from e
    elif mode == "save-manifest":
        report["manifest"] = _save_manifest(
            root_path,
            report,
            manifest_path=manifest_path,
            today=run_date,
        )
    elif mode == "copy-as-sources":
        report["copy"] = _copy_as_sources(
            root_path,
            selected_paths=selected_paths,
            today=run_date,
        )
    elif mode == "compile-selected":
        report["compile_plan"] = _compile_selected(
            root_path,
            selected_paths=selected_paths,
            today=run_date,
        )
    return report
