"""The `preserve` MCP tool: capture binary or text artifact to Evidence/.

Per SKILL.md rule 2, Evidence is append-only — never edited after creation.
The artifact's value is "this is what we received." No analysis at this
layer; analytical takes go in compiled notes that link to the evidence file.

Path: `Evidence/<scope>/<category>/<filename>`. Folders auto-created.

Two input modes:
- `content_base64`: file bytes for binary artifacts (PDF, images, .docx).
  5MB decoded size limit (base64 inflates ~33% — keeps the MCP request
  comfortably under claude.ai's transport limits).
- `content`: UTF-8 text for markdown/plain-text artifacts.

If `description` is supplied, a sidecar `<filename>.md` is written alongside
the artifact with frontmatter explaining what it is.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import indexes
from .vault import PlannedWrite, batch_atomic_write, kb_root


log = logging.getLogger(__name__)

MAX_DECODED_BYTES = 5 * 1024 * 1024  # 5 MB


@dataclass
class PreserveResult:
    path: str                 # vault-relative path of the artifact
    sidecar_path: str | None  # vault-relative path of the .md sidecar (if any)
    warnings: list[str]

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "sidecar_path": self.sidecar_path,
            "warnings": self.warnings,
        }


@dataclass
class PreserveError(Exception):
    code: str
    missing: list[str]
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "missing": self.missing, "reason": self.reason}


def preserve(
    vault_root: Path,
    *,
    scope: str,
    category: str,
    filename: str,
    content_base64: str | None = None,
    content: str | None = None,
    description: str | None = None,
    today: dt.date | None = None,
) -> PreserveResult:
    """Capture an artifact to Evidence/<scope>/<category>/<filename>."""
    missing: list[str] = []
    reasons: list[str] = []

    scope_safe = _sanitize_segment(scope)
    if not scope_safe:
        missing.append("scope")
        reasons.append("scope is empty or only invalid characters")
    category_safe = _sanitize_segment(category)
    if not category_safe:
        missing.append("category")
        reasons.append("category is empty or only invalid characters")
    filename_safe = _sanitize_filename(filename)
    if not filename_safe:
        missing.append("filename")
        reasons.append("filename is empty or only invalid characters")

    if (content_base64 is None) == (content is None):
        return _raise(
            "INVALID_PRESERVE",
            ["content_base64" if content is None else "content"],
            "Exactly one of `content_base64` or `content` must be supplied.",
        )

    if missing:
        return _raise("INVALID_PRESERVE", missing, "; ".join(reasons))

    today = today or dt.date.today()
    date_iso = today.isoformat()
    kb = kb_root(vault_root)
    folder = kb / "Evidence" / scope_safe / category_safe
    artifact_path = folder / filename_safe

    if artifact_path.exists():
        return _raise(
            "ARTIFACT_EXISTS",
            ["filename"],
            (
                f"{artifact_path.relative_to(vault_root).as_posix()!r} already "
                "exists. Evidence is append-only; pick a new filename or rename."
            ),
        )

    if content_base64 is not None:
        try:
            decoded = base64.b64decode(content_base64, validate=True)
        except (ValueError, base64.binascii.Error) as e:
            return _raise(
                "INVALID_PRESERVE",
                ["content_base64"],
                f"could not decode base64: {e}",
            )
        if len(decoded) > MAX_DECODED_BYTES:
            return _raise(
                "TOO_LARGE",
                ["content_base64"],
                (
                    f"decoded size {len(decoded):,} bytes exceeds the "
                    f"{MAX_DECODED_BYTES:,}-byte preserve limit. Land it "
                    "desk-side via Obsidian Sync instead."
                ),
            )
        artifact_bytes = decoded
        artifact_text = None
    else:
        # content is UTF-8 text; write as-is.
        artifact_bytes = None
        artifact_text = content

    folder.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    written_artifact = False
    sidecar_rel: str | None = None

    try:
        if artifact_bytes is not None:
            # Binary: write directly, no atomic batch (batch_atomic_write is text-only).
            artifact_path.write_bytes(artifact_bytes)
            written_artifact = True
        else:
            artifact_path.write_text(artifact_text or "", encoding="utf-8", newline="\n")
            written_artifact = True

        writes: list[PlannedWrite] = []

        # Optional sidecar
        if description and description.strip():
            sidecar_path = folder / f"{filename_safe}.md"
            if sidecar_path.exists():
                warnings.append(
                    f"sidecar {sidecar_path.name!r} already exists; skipped."
                )
            else:
                sidecar_md = _render_sidecar(
                    artifact_name=filename_safe,
                    scope=scope_safe,
                    category=category_safe,
                    date_iso=date_iso,
                    description=description.strip(),
                )
                writes.append(PlannedWrite(path=sidecar_path, content=sidecar_md))
                sidecar_rel = sidecar_path.relative_to(vault_root).as_posix()

        # Index + log updates.
        rel_artifact = artifact_path.relative_to(vault_root).as_posix()
        rel_artifact_for_summary = rel_artifact.replace("Knowledge Base/", "")
        activity_summary = (
            f"`{rel_artifact_for_summary}` (evidence, {scope_safe}/{category_safe}, "
            f"mobile via kb-mcp)"
        )
        log_body = (
            f"Mobile preserve via kb-mcp. scope={scope_safe}, "
            f"category={category_safe}, filename={filename_safe}."
        )
        if description and description.strip():
            desc_one_line = description.strip().replace("\n", " ")
            if len(desc_one_line) > 280:
                desc_one_line = desc_one_line[:277] + "…"
            log_body += f" Description: {desc_one_line}"

        top_index = kb / "index.md"
        if top_index.exists():
            new_top, trim_note = indexes._prepend_recent_activity(
                top_index.read_text(encoding="utf-8"),
                date_iso=date_iso,
                summary=activity_summary,
            )
            if trim_note:
                warnings.append(f"Recent activity trimmed at cap-50: {trim_note}")
            writes.append(PlannedWrite(path=top_index, content=new_top))
        else:
            warnings.append("Knowledge Base/index.md missing; skipped Recent activity bump")

        log_file = kb / "log.md"
        if log_file.exists():
            new_log = _prepend_log_entry(
                log_file.read_text(encoding="utf-8"),
                date_iso=date_iso,
                rel_path=rel_artifact,
                body=log_body,
            )
            writes.append(PlannedWrite(path=log_file, content=new_log))
        else:
            warnings.append("Knowledge Base/log.md missing; skipped log entry")

        if writes:
            batch_atomic_write(writes)

    except Exception as e:
        log.exception("preserve() failed mid-write; artifact_written=%s", written_artifact)
        warnings.append(f"partial write — reconcile on desktop: {e}")
        raise

    return PreserveResult(
        path=artifact_path.relative_to(vault_root).as_posix(),
        sidecar_path=sidecar_rel,
        warnings=warnings,
    )


# ---------------- helpers ----------------


_INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_segment(s: str | None) -> str:
    """Strip path separators and reserved chars from a path segment.

    Used for scope/category — must not allow `..` traversal or absolute paths.
    """
    if not s:
        return ""
    cleaned = _INVALID_PATH_CHARS.sub("", s.strip())
    # Reject anything that's just dots (could be `.` or `..`)
    if cleaned and cleaned.replace(".", "") == "":
        return ""
    return cleaned


def _sanitize_filename(s: str | None) -> str:
    """Same as _sanitize_segment but allows the trailing extension dot."""
    if not s:
        return ""
    cleaned = _INVALID_PATH_CHARS.sub("", s.strip())
    if cleaned and cleaned.replace(".", "") == "":
        return ""
    return cleaned


def _render_sidecar(
    *,
    artifact_name: str,
    scope: str,
    category: str,
    date_iso: str,
    description: str,
) -> str:
    """Sidecar .md describing a preserved binary artifact.

    Uses `type: source` (with `source_type: other`) since the page-type
    taxonomy doesn't have a dedicated `evidence` type. Tags surface the
    evidence + scope context.
    """
    lines = ["---"]
    lines.append("type: source")
    lines.append("source_type: other")
    lines.append(f"captured: {date_iso}")
    lines.append(f"tags: [evidence, {scope.lower().replace(' ', '-')}, {category.lower().replace(' ', '-')}]")
    lines.append("ingested_into: []")
    lines.append("---")
    lines.append("")
    lines.append(f"# Evidence: {artifact_name}")
    lines.append("")
    lines.append(f"Preserved under `Evidence/{scope}/{category}/`.")
    lines.append("")
    lines.append("## Description")
    lines.append("")
    lines.append(description)
    lines.append("")
    return "\n".join(lines)


def _prepend_log_entry(
    text: str, *, date_iso: str, rel_path: str, body: str
) -> str:
    """Insert `## [<date>] preserve | <kb-relative-path>` after the `---` separator."""
    title = rel_path.replace("Knowledge Base/", "", 1)
    new_entry = f"## [{date_iso}] preserve | {title}\n\n{body}\n"
    sep_idx = text.find(indexes.LOG_SEPARATOR)
    if sep_idx == -1:
        return text.rstrip() + "\n\n" + new_entry + "\n"
    insertion_point = sep_idx + len(indexes.LOG_SEPARATOR)
    return text[:insertion_point] + "\n" + new_entry + "\n" + text[insertion_point:]


def _raise(code: str, missing: list[str], reason: str):
    raise PreserveError(code=code, missing=missing, reason=reason)
