"""The `link` MCP tool: create a typed entity under Entities/.

Four entity types per page-types.md:
- person   → Entities/People/<Name>.md
- concept  → Entities/Concepts/<Name>.md
- library  → Entities/Libraries/<Name>.md
- decision → Entities/Decisions/<Name>.md

Name is **Title Case**, not slugified — entities are named after the thing
they are (e.g., `Ada Lovelace.md`, `Agentic RAG.md`, `pgvector.md`).

v1 is create-only. If the entity file already exists, this raises
`ENTITY_EXISTS` — use `replace` to supersede an existing entity.

Sub-folder index maintenance (e.g. categorizing concepts by domain in
`Entities/Concepts/index.md`) is deferred — handled by audit follow-up.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import indexes, memory_refs, semantic_writes
from .kbdir import kb_prefix
from .vault import (
    InvalidSlugError,
    PlannedWrite,
    WikilinkResolver,
    batch_atomic_write,
    escape_wikilinks_for_log,
    kb_root,
    normalize_body_wikilinks,
    normalize_wikilink,
    plan_log_writes,
    read_guarded_text,
    render_wikilink_target,
    resolve_filename_slug,
    rotate_log_if_needed,
    yaml_scalar,
)

log = logging.getLogger(__name__)


ENTITY_TYPES = ("person", "concept", "library", "decision")

ENTITY_TYPE_TO_FOLDER: dict[str, str] = {
    "person": "People",
    "concept": "Concepts",
    "library": "Libraries",
    "decision": "Decisions",
}

DECISION_STATUS_VALUES = ("proposed", "accepted", "superseded")


@dataclass
class LinkResult:
    path: str  # vault-relative
    ref: str
    warnings: list[str]
    creation: dict | None = None

    def as_dict(self) -> dict:
        value = {"path": self.path, "ref": self.ref, "warnings": self.warnings}
        if self.creation is not None:
            value["creation"] = self.creation
        return value


@dataclass
class LinkError(Exception):
    code: str
    missing: list[str]
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "missing": self.missing, "reason": self.reason}


def _legacy_link(
    vault_root: Path,
    *,
    entity_type: str,
    name: str,
    slug: str | None = None,
    summary: str,
    why_in_kb: str | None = None,
    tags: list[str] | None = None,
    connections: list[str] | None = None,
    # person
    affiliation: str | None = None,
    relationship: str | None = None,
    # concept
    domain: str | None = None,
    # library
    language: str | None = None,
    repo: str | None = None,
    license: str | None = None,
    used_in: list[str] | None = None,
    # decision
    decided: str | None = None,
    project: str | None = None,
    decision_status: str | None = None,
    today: dt.date | None = None,
) -> LinkResult:
    """Create a typed entity page + update top index + log."""
    slug_warnings: list[str] = []
    filename_slug: str | None = None
    if slug is not None:
        try:
            filename_slug, slug_warnings = resolve_filename_slug(name, slug)
        except InvalidSlugError as e:
            raise LinkError(code="INVALID_SLUG", missing=["slug"], reason=str(e)) from e
    err = _validate(
        entity_type=entity_type,
        name=name,
        summary=summary,
        decision_status=decision_status,
    )
    if err is not None:
        raise LinkError(code=err.code, missing=err.missing, reason=err.reason)

    # Decision entities carry a `project:` field — route it through the
    # same auto-register + typo-distance guard the other writers use.
    # Without this, `link(entity_type="decision", project="helath")` would
    # land a broken decision page silently.
    if entity_type == "decision" and project:
        from . import project_keys as project_keys_module
        registry = project_keys_module.load_project_registry(vault_root)
        if project not in registry.project_to_folder:
            try:
                project_keys_module.register_project_key(vault_root, project)
            except project_keys_module.ProjectKeyTypoError as e:
                raise LinkError(
                    code="PROJECT_KEY_TYPO",
                    missing=["project"],
                    reason=str(e),
                ) from e
            except ValueError:
                # Invalid slug — let it land; downstream audit will flag via
                # unregistered_project_key.
                pass

    today = today or dt.date.today()
    date_iso = today.isoformat()
    tags_clean = _clean_tags(tags)
    exomem_id = memory_refs.new_id()

    display_name = name.strip()
    name_safe = _sanitize_name(name)
    folder = kb_root(vault_root) / "Entities" / ENTITY_TYPE_TO_FOLDER[entity_type]
    entity_path = folder / f"{filename_slug or name_safe}.md"

    if entity_path.exists():
        raise LinkError(
            code="ENTITY_EXISTS",
            missing=["name"],
            reason=(
                f"{entity_path.relative_to(vault_root).as_posix()!r} already exists. "
                "Entities are create-only via `link`; use `replace` to supersede."
            ),
        )

    folder.mkdir(parents=True, exist_ok=True)

    rel_entity_no_ext = entity_path.relative_to(vault_root).with_suffix("").as_posix()
    # Shared, freshness-checked resolver (see find.shared_resolver) — never a
    # fresh O(vault) build per write. Pending entry re-synced by index_sync
    # post-write; purged in the except-path below on failure.
    from . import find as find_module
    resolver = find_module.shared_resolver(vault_root)
    resolver.add_pending(rel_entity_no_ext, title=display_name)

    connections_norm, conn_warnings = _normalize_connections(
        connections, vault_root=vault_root, resolver=resolver
    )

    # Normalize wikilinks inside the summary and why_in_kb prose so the
    # entity body lands in canonical form even when written via the bare
    # `link` API.
    summary_clean, summary_warnings = normalize_body_wikilinks(
        summary, vault_root, resolver=resolver
    )
    why_clean: str | None = None
    why_warnings: list[str] = []
    if why_in_kb:
        why_clean, why_warnings = normalize_body_wikilinks(
            why_in_kb, vault_root, resolver=resolver
        )

    entity_md = _render_entity(
        entity_type=entity_type,
        name=display_name,
        summary=summary_clean,
        why_in_kb=why_clean,
        date_iso=date_iso,
        tags=tags_clean,
        connections=[
            render_wikilink_target(connection, vault_root)
            for connection in connections_norm
        ],
        affiliation=affiliation,
        relationship=relationship,
        domain=domain,
        language=language,
        repo=repo,
        license=license,
        used_in=used_in,
        decided=decided,
        project=project,
        decision_status=decision_status,
        exomem_id=exomem_id,
    )

    rel_entity = entity_path.relative_to(vault_root).as_posix()

    writes: list[PlannedWrite] = [PlannedWrite(path=entity_path, content=entity_md)]
    warnings: list[str] = (
        list(slug_warnings)
        + list(conn_warnings)
        + list(summary_warnings)
        + list(why_warnings)
    )

    # Index + log updates.
    kb = kb_root(vault_root)
    activity_summary = _activity_summary(
        rel_entity_no_ext=rel_entity_no_ext,
        name=display_name,
        entity_type=entity_type,
        domain=domain,
        project=project,
    )
    log_body = _log_entry_body(
        entity_type=entity_type,
        name=display_name,
        domain=domain,
        project=project,
        decision_status=decision_status,
        tags=tags_clean,
    )

    top_index = kb / "index.md"
    if top_index.exists():
        new_top, _trim_note = indexes._prepend_recent_activity(
            top_index.read_text(encoding="utf-8"),
            date_iso=date_iso,
            summary=activity_summary,
        )
        # Refresh Entities sub-index + top-index Counts. Pass the new
        # entity's path so counts reflect post-write state.
        sub_writes, new_top_with_counts = indexes.compute_subindex_writes(
            vault_root,
            top_index_text=new_top,
            pending_paths=[rel_entity_no_ext],
        )
        if new_top_with_counts is not None:
            new_top = new_top_with_counts
        # Cap-50 trim is recorded in log.md; no per-write warning needed.
        writes.append(PlannedWrite(path=top_index, content=new_top))
        writes.extend(sub_writes)
    else:
        warnings.append(f"{kb_prefix()}index.md missing; skipped Recent activity bump")

    log_file = kb / "log.md"
    if log_file.exists():
        new_log = _prepend_log_entry(
            log_file.read_text(encoding="utf-8"),
            date_iso=date_iso,
            rel_path=rel_entity_no_ext,
            body=log_body,
        )
        writes.append(PlannedWrite(path=log_file, content=new_log))
    else:
        warnings.append(f"{kb_prefix()}log.md missing; skipped log entry")

    try:
        batch_atomic_write(writes, vault_root=vault_root)
    except Exception as e:
        log.exception("partial write during link(); some files may be updated")
        warnings.append(f"partial write — reconcile on desktop: {e}")
        try:  # purge the add_pending phantom — the entity never landed
            find_module.on_resolver_files_changed(
                vault_root, [rel_entity_no_ext + ".md"], []
            )
        except Exception:  # noqa: BLE001 — purge is best-effort cleanup
            log.debug("resolver pending-purge failed", exc_info=True)
        raise

    rotate_note = rotate_log_if_needed(vault_root)
    if rotate_note:
        warnings.append(rotate_note)

    return LinkResult(
        path=rel_entity,
        ref=memory_refs.memory_ref(exomem_id),
        warnings=warnings,
    )


# ---------------- validation ----------------


@dataclass
class _Err:
    code: str
    missing: list[str]
    reason: str


def _validate(
    *, entity_type: str, name: str, summary: str, decision_status: str | None
) -> _Err | None:
    if entity_type not in ENTITY_TYPES:
        return _Err(
            code="INVALID_LINK",
            missing=["entity_type"],
            reason=(
                f"entity_type {entity_type!r} not valid. "
                f"Valid: {list(ENTITY_TYPES)}"
            ),
        )
    missing: list[str] = []
    reasons: list[str] = []
    if not name or not name.strip():
        missing.append("name")
        reasons.append("name is empty")
    if not summary or not summary.strip():
        missing.append("summary")
        reasons.append("summary is empty")
    if entity_type == "decision" and decision_status is not None:
        if decision_status not in DECISION_STATUS_VALUES:
            return _Err(
                code="INVALID_LINK",
                missing=["decision_status"],
                reason=(
                    f"decision_status {decision_status!r} not valid. "
                    f"Valid: {list(DECISION_STATUS_VALUES)}"
                ),
            )
    if missing:
        return _Err(code="INVALID_LINK", missing=missing, reason="; ".join(reasons))
    return None


# ---------------- name + path sanitization ----------------


_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_name(name: str) -> str:
    """Strip filesystem-reserved chars from an entity name while preserving
    Title Case and spaces (which Obsidian filenames allow on Windows)."""
    cleaned = _INVALID_NAME_CHARS.sub("", name.strip())
    # Collapse repeated whitespace
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "Unnamed"


# ---------------- render ----------------


def _render_entity(
    *,
    entity_type: str,
    name: str,
    summary: str,
    why_in_kb: str | None,
    date_iso: str,
    tags: list[str],
    connections: list[str],
    affiliation: str | None,
    relationship: str | None,
    domain: str | None,
    language: str | None,
    repo: str | None,
    license: str | None,
    used_in: list[str] | None,
    decided: str | None,
    project: str | None,
    decision_status: str | None,
    exomem_id: str,
) -> str:
    lines = ["---"]
    lines.append("type: entity")
    lines.append(f"exomem_id: {exomem_id}")
    lines.append(f"title: {yaml_scalar(name)}")
    lines.append(f"entity_type: {entity_type}")
    lines.append("status: active")
    lines.append(f"created: {date_iso}")
    lines.append(f"updated: {date_iso}")

    if entity_type == "person":
        if affiliation:
            lines.append(f"affiliation: {affiliation}")
        if relationship:
            lines.append(f"relationship: {relationship}")
    elif entity_type == "concept":
        if domain:
            lines.append(f"domain: {domain}")
    elif entity_type == "library":
        if language:
            lines.append(f"language: {language}")
        if repo:
            lines.append(f"repo: {repo}")
        if license:
            lines.append(f"license: {license}")
        if used_in:
            lines.append("used_in: [" + ", ".join(used_in) + "]")
    elif entity_type == "decision":
        if decided:
            lines.append(f"decided: {decided}")
        if project:
            lines.append(f"project: {project}")
        if decision_status:
            lines.append(f"decision_status: {decision_status}")

    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    else:
        lines.append("tags: []")
    lines.append("---")
    lines.append("")
    lines.append(f"# {name}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(summary.strip())
    if why_in_kb and why_in_kb.strip():
        lines.append("")
        lines.append("## Why in the KB")
        lines.append("")
        lines.append(why_in_kb.strip())
    if connections:
        lines.append("")
        lines.append("## Relations")
        lines.append("")
        for c in connections:
            lines.append(f"- relates_to [[{c}]]")
    lines.append("")
    return "\n".join(lines)


# ---------------- helpers ----------------


def _normalize_connections(
    connections: list[str] | None,
    *,
    vault_root: Path,
    resolver: WikilinkResolver,
) -> tuple[list[str], list[str]]:
    """Canonicalize each connection wikilink to full vault-rooted form.

    Returns (canonical_connections, warnings). Same fall-through behaviour
    as `note._normalize_sources`: unresolved targets pass through with a
    warning so forward refs aren't blocked.
    """
    if not connections:
        return [], []
    out: list[str] = []
    seen: set[str] = set()
    warnings: list[str] = []
    for c in connections:
        c = (c or "").strip()
        if not c:
            continue
        canonical, warning = normalize_wikilink(
            c, vault_root, resolver=resolver, strict=False
        )
        if warning:
            warnings.append(warning)
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out, warnings


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


def _activity_summary(
    *,
    rel_entity_no_ext: str,
    name: str,
    entity_type: str,
    domain: str | None,
    project: str | None,
) -> str:
    path_part = rel_entity_no_ext.replace(kb_prefix(), "")
    modifier_parts: list[str] = [entity_type]
    if entity_type == "concept" and domain:
        modifier_parts.append(domain)
    if entity_type == "decision" and project:
        modifier_parts.append(project)
    modifier = ", ".join(modifier_parts)
    return (
        f"`{path_part}` ({modifier}, mobile via exomem) — \"{name}\""
    )


def _log_entry_body(
    *,
    entity_type: str,
    name: str,
    domain: str | None,
    project: str | None,
    decision_status: str | None,
    tags: list[str],
) -> str:
    parts: list[str] = []
    parts.append(
        f"Mobile link via exomem. entity_type={entity_type}. \"{name}\"."
    )
    if entity_type == "concept" and domain:
        parts.append(f"domain={domain}.")
    if entity_type == "decision":
        if project:
            parts.append(f"project={project}.")
        if decision_status:
            parts.append(f"decision_status={decision_status}.")
    if tags:
        parts.append(f"tags: {tags}.")
    return " ".join(parts)


def _prepend_log_entry(
    text: str, *, date_iso: str, rel_path: str, body: str
) -> str:
    """Insert `## [<date>] link | <kb-relative-path>` after the `---` separator."""
    title = rel_path.replace(kb_prefix(), "", 1)
    new_entry = f"## [{date_iso}] link | {title}\n\n{escape_wikilinks_for_log(body)}\n"
    if new_entry in text:
        return text
    sep_idx = text.find(indexes.LOG_SEPARATOR)
    if sep_idx == -1:
        return text.rstrip() + "\n\n" + new_entry + "\n"
    insertion_point = sep_idx + len(indexes.LOG_SEPARATOR)
    return text[:insertion_point] + "\n" + new_entry + "\n" + text[insertion_point:]


def link(
    vault_root: Path,
    *,
    entity_type: str,
    name: str,
    slug: str | None = None,
    summary: str,
    why_in_kb: str | None = None,
    tags: list[str] | None = None,
    connections: list[str] | None = None,
    affiliation: str | None = None,
    relationship: str | None = None,
    domain: str | None = None,
    language: str | None = None,
    repo: str | None = None,
    license: str | None = None,
    used_in: list[str] | None = None,
    decided: str | None = None,
    project: str | None = None,
    decision_status: str | None = None,
    today: dt.date | None = None,
) -> LinkResult:
    """Create an entity through detached structural preflight."""
    slug_warnings: list[str] = []
    filename_slug: str | None = None
    if slug is not None:
        try:
            filename_slug, slug_warnings = resolve_filename_slug(name, slug)
        except InvalidSlugError as error:
            raise LinkError("INVALID_SLUG", ["slug"], str(error)) from error
    err = _validate(
        entity_type=entity_type,
        name=name,
        summary=summary,
        decision_status=decision_status,
    )
    if err is not None:
        raise LinkError(err.code, err.missing, err.reason)
    from . import find as find_module
    from . import project_keys as project_keys_module

    try:
        key_plan = project_keys_module.plan_project_keys(
            vault_root,
            [project] if entity_type == "decision" and project else [],
        )
    except project_keys_module.ProjectKeyTypoError as error:
        raise LinkError("PROJECT_KEY_TYPO", ["project"], str(error)) from error
    except ValueError as error:
        raise LinkError("INVALID_LINK", ["project"], str(error)) from error
    date_iso = (today or dt.date.today()).isoformat()
    identity = memory_refs.new_id()
    display_name = name.strip()
    folder = kb_root(vault_root) / "Entities" / ENTITY_TYPE_TO_FOLDER[entity_type]
    entity_path = folder / f"{filename_slug or _sanitize_name(name)}.md"
    rel_entity = entity_path.relative_to(vault_root).as_posix()
    if entity_path.exists():
        raise LinkError(
            "ENTITY_EXISTS",
            ["name"],
            f"{rel_entity!r} already exists. Entities are create-only via `link`; "
            "use `replace` to supersede.",
        )
    rel_entity_no_ext = rel_entity.removesuffix(".md")
    resolver = find_module.writer_resolver_snapshot(vault_root)
    resolver.add_pending(rel_entity_no_ext, title=display_name)
    connections_norm, connection_warnings = _normalize_connections(
        connections, vault_root=vault_root, resolver=resolver
    )
    summary_clean, summary_warnings = normalize_body_wikilinks(
        summary, vault_root, resolver=resolver
    )
    why_clean: str | None = None
    why_warnings: list[str] = []
    if why_in_kb:
        why_clean, why_warnings = normalize_body_wikilinks(
            why_in_kb, vault_root, resolver=resolver
        )
    source = _render_entity(
        entity_type=entity_type,
        name=display_name,
        summary=summary_clean,
        why_in_kb=why_clean,
        date_iso=date_iso,
        tags=_clean_tags(tags),
        connections=[
            render_wikilink_target(item, vault_root) for item in connections_norm
        ],
        affiliation=affiliation,
        relationship=relationship,
        domain=domain,
        language=language,
        repo=repo,
        license=license,
        used_in=used_in,
        decided=decided,
        project=project,
        decision_status=decision_status,
        exomem_id=identity,
    )
    registrations = tuple(
        semantic_writes.DraftRegistration(item.key, item.category, item.folder)
        for item in key_plan.introductions
    )
    token = semantic_writes.DraftToken(
        "link",
        "create",
        rel_entity,
        date_iso,
        registrations,
    ).encode()
    try:
        preflight = semantic_writes.preflight_creation(
            vault_root,
            path=rel_entity,
            source=source,
            operation="create",
            writer="link",
            draft_id=identity,
            draft_token=token,
            registrations=registrations,
        )
    except semantic_writes.SemanticWriteError as error:
        raise LinkError(error.code, [], error.reason) from error
    warnings = (
        list(slug_warnings)
        + list(connection_warnings)
        + list(summary_warnings)
        + list(why_warnings)
    )
    auxiliary: list[PlannedWrite] = list(key_plan.writes)
    kb = kb_root(vault_root)
    activity = _activity_summary(
        rel_entity_no_ext=rel_entity_no_ext,
        name=display_name,
        entity_type=entity_type,
        domain=domain,
        project=project,
    )
    top_index = kb / "index.md"
    if top_index.is_file():
        top_text, top_guard = read_guarded_text(vault_root, top_index)
        new_top, _ = indexes._prepend_recent_activity(
            top_text, date_iso=date_iso, summary=activity
        )
        sub_writes, counted_top = indexes.compute_subindex_writes(
            vault_root,
            top_index_text=new_top,
            pending_paths=[rel_entity_no_ext],
            include_unchanged=True,
        )
        auxiliary.append(
            PlannedWrite(top_index, counted_top or new_top, guard=top_guard)
        )
        auxiliary.extend(sub_writes)
    else:
        warnings.append(f"{kb_prefix()}index.md missing; skipped Recent activity bump")
    try:
        log_plan = plan_log_writes(
            vault_root,
            date_iso=date_iso,
            op="link",
            rel_path_no_ext=rel_entity_no_ext,
            body=_log_entry_body(
                entity_type=entity_type,
                name=display_name,
                domain=domain,
                project=project,
                decision_status=decision_status,
                tags=_clean_tags(tags),
            ),
            operation_token=token,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise LinkError("LOG_PLAN_CONFLICT", [], str(error)) from error
    auxiliary.extend(log_plan.writes)
    if log_plan.warning is not None:
        warnings.append(log_plan.warning)
    if log_plan.rotation_note is not None:
        warnings.append(log_plan.rotation_note)
    committed = semantic_writes.commit_creation(
        vault_root,
        preflight=preflight,
        auxiliary_writes=tuple(auxiliary),
        operation="create",
    )
    return LinkResult(
        rel_entity,
        memory_refs.memory_ref(identity),
        warnings,
        committed.as_dict(),
    )
