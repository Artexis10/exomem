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
import json
from pathlib import Path
from typing import Any

from . import compile_proposal as compile_proposal_module
from . import context_refs, indexes, knowledge_packs
from . import overview as overview_module
from .kbdir import kb_dirname, kb_prefix
from .vault import (
    PlannedWrite,
    VaultPathError,
    batch_atomic_write,
    kb_root,
    prepend_log_entry,
    resolve_under_vault,
    slugify_with_truncation_check,
    unique_path,
    yaml_scalar,
)

DEFAULT_MODE = "scan-only"
SUPPORTED_MODES = ("scan-only", "save-manifest", "copy-as-sources", "compile-selected")
PLANNED_MODES: tuple[str, ...] = ()
ADOPTION_DIR = "_Adoption"
IMPORTED_SOURCE_FOLDER = "Imported"
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
    if defaulted:
        target = unique_path(target.parent, target.stem, target.suffix)
        rel = target.relative_to(root).as_posix()
    elif target.exists():
        raise AdoptError("MANIFEST_EXISTS", f"manifest already exists: {rel}")
    return target, rel


def _compact_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": report.get("mode"),
        "write_contract": report.get("write_contract"),
        "governance": report.get("governance"),
        "summary": report.get("summary"),
        "pack_suggestions": report.get("pack_suggestions"),
        "next_actions": report.get("next_actions"),
        "refs": report.get("refs"),
        "copy": report.get("copy"),
        "compile_plan": report.get("compile_plan"),
        "overview": report.get("overview"),
    }


def _render_manifest(report: dict[str, Any], *, rel_path: str, today: dt.date) -> str:
    compact = _compact_report(report)
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
    lines.extend(
        [
            "",
            "## Machine-Readable Report",
            "",
            "```json",
            json.dumps(compact, indent=2, sort_keys=True, ensure_ascii=False, default=str),
            "```",
            "",
        ]
    )
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


def _title_from_text(path: Path, text: str) -> str:
    for line in text.splitlines()[:40]:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or path.stem
    return path.stem.replace("-", " ").replace("_", " ").strip() or path.stem


def _fence_for(text: str) -> str:
    run = 3
    while "`" * run in text:
        run += 1
    return "`" * run


def _render_imported_source(
    *,
    title: str,
    rel_original: str,
    sha256: str,
    size: int,
    date_iso: str,
    content: str,
) -> str:
    fence = _fence_for(content)
    return "\n".join(
        [
            "---",
            "type: source",
            f"title: {yaml_scalar(title)}",
            "source_type: other",
            f"captured: {date_iso}",
            f"imported_from: {yaml_scalar(rel_original)}",
            f"original_sha256: {sha256}",
            f"original_bytes: {size}",
            "tags: [imported]",
            "ingested_into: []",
            "---",
            "",
            f"# {title}",
            "",
            f"> Copied from `{rel_original}` by `adopt(mode=\"copy-as-sources\")`. The original remains unchanged.",
            "",
            "## Original Metadata",
            "",
            f"- Original path: `{rel_original}`",
            f"- SHA-256: `{sha256}`",
            f"- Bytes: {size}",
            "",
            "## Capture",
            "",
            fence,
            content.rstrip(),
            fence,
            "",
        ]
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


def _unique_import_path(folder: Path, stem: str, reserved: set[Path]) -> Path:
    """Return a path unique on disk and within the pending batch."""
    i = 1
    while True:
        suffix = "" if i == 1 else f"-{i}"
        candidate = folder / f"{stem}{suffix}.md"
        if not candidate.exists() and candidate not in reserved:
            reserved.add(candidate)
            return candidate
        i += 1



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

    kb = kb_root(root)
    folder = kb / "Sources" / IMPORTED_SOURCE_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    date_iso = today.isoformat()
    copied: list[dict] = []
    skipped: list[dict] = []
    writes: list[PlannedWrite] = []
    reserved_targets: set[Path] = set()

    for raw in selected_paths:
        resolved = _resolve_selected_text_file(root, raw)
        if isinstance(resolved, dict):
            skipped.append(resolved)
            continue
        abs_path, rel = resolved
        data = abs_path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        text = data.decode("utf-8", errors="replace")
        title = _title_from_text(abs_path, text)
        slug, slug_warning = slugify_with_truncation_check(title)
        target = _unique_import_path(folder, f"{date_iso}-{slug}", reserved_targets)
        rel_target = target.relative_to(root).as_posix()
        if slug_warning:
            skipped.append({"path": rel, "code": "SLUG_TRUNCATED", "reason": slug_warning})
        writes.append(
            PlannedWrite(
                path=target,
                content=_render_imported_source(
                    title=title,
                    rel_original=rel,
                    sha256=sha,
                    size=len(data),
                    date_iso=date_iso,
                    content=text,
                ),
            )
        )
        copied.append(
            {
                "original_path": rel,
                "original_ref": context_refs.vault_ref(rel),
                "source_path": rel_target,
                "source_ref": context_refs.source_ref(rel_target),
                "original_sha256": sha,
                "original_bytes": len(data),
            }
        )

    if not copied:
        return {"copied_sources": [], "skipped": skipped, "warnings": ["no importable files copied"]}

    sources_dir = kb / "Sources"
    post_counts = indexes._count_sources(sources_dir)
    post_counts[IMPORTED_SOURCE_FOLDER] = post_counts.get(IMPORTED_SOURCE_FOLDER, 0) + len(copied)

    sources_index = sources_dir / "index.md"
    warnings: list[str] = []
    if sources_index.exists():
        sources_text = indexes._replace_by_type_section(
            sources_index.read_text(encoding="utf-8"),
            folder_title=IMPORTED_SOURCE_FOLDER,
            folder_description="copied legacy-vault material with provenance",
            counts=post_counts,
        )
        for item in reversed(copied):
            rel_no_ext = item["source_path"][:-3]
            sources_text = indexes._prepend_recent_capture(
                sources_text,
                date_iso=date_iso,
                rel_source_path=rel_no_ext,
            )
        writes.append(PlannedWrite(path=sources_index, content=sources_text))
    else:
        warnings.append(f"{kb_prefix()}Sources/index.md missing; skipped Sources index update")

    top_index = kb / "index.md"
    if top_index.exists():
        summary = (
            f"`Sources/{IMPORTED_SOURCE_FOLDER}/` (imported sources) - copied "
            f"{len(copied)} selected legacy file(s) with original path/hash provenance"
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
            pending_paths=[item["source_path"] for item in copied],
        )
        sub_writes = [
            write for write in sub_writes
            if write.path != sources_index
        ]
        writes.append(PlannedWrite(path=top_index, content=top_text or top_index.read_text(encoding="utf-8")))
        writes.extend(sub_writes)
        if trim_note:
            warnings.append(trim_note)
    else:
        warnings.append(f"{kb_prefix()}index.md missing; skipped Recent activity bump")

    log_file = kb / "log.md"
    if log_file.exists():
        body_lines = [
            f"Copied {len(copied)} selected legacy file(s) into Sources/{IMPORTED_SOURCE_FOLDER}/. Originals remain unchanged.",
            "",
        ]
        for item in copied:
            body_lines.append(
                f"- {item['original_path']} -> {item['source_path']} sha256={item['original_sha256']}"
            )
        writes.append(
            PlannedWrite(
                path=log_file,
                content=prepend_log_entry(
                    log_file.read_text(encoding="utf-8"),
                    date_iso=date_iso,
                    op="adopt-copy",
                    rel_path_no_ext=f"{kb_prefix()}Sources/{IMPORTED_SOURCE_FOLDER}",
                    body="\n".join(body_lines),
                ),
            )
        )
    else:
        warnings.append(f"{kb_prefix()}log.md missing; skipped log entry")

    batch_atomic_write(writes, vault_root=root)
    return {"copied_sources": copied, "skipped": skipped, "warnings": warnings}


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
        skipped.extend(copy_result.get("skipped") or [])
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
    if mode == "save-manifest":
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
