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

If `description` and/or `text` is supplied, a sidecar `<filename>.md` is
written alongside the artifact: `description` is a short human caption,
`text` is the full extracted/OCR'd text of the artifact. The sidecar is a
real `.md` and gets embedded on write, so a binary that is otherwise opaque
to search becomes findable by its content (the OCR companion). The bytes
themselves never get an LLM — extraction happens in the caller (the sandbox).
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import indexes
from .vault import PlannedWrite, batch_atomic_write, escape_wikilinks_for_log, kb_root


log = logging.getLogger(__name__)

MAX_DECODED_BYTES = 5 * 1024 * 1024  # 5 MB — base64-via-model path (tokens cost real money)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB — HTTP /upload path (raw bytes, no token cost).
# Aligns with the Cloudflare free-plan ~100 MB edge cap, so the public path and this app cap
# agree. A deployment that wants larger uploads over a non-Cloudflare route (LAN/Tailscale
# direct to the origin) raises KB_MCP_UPLOAD_MAX_BYTES in its .env.


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
    content_stream: BinaryIO | None = None,
    description: str | None = None,
    text: str | None = None,
    today: dt.date | None = None,
    max_decoded_bytes: int = MAX_DECODED_BYTES,
    max_stream_bytes: int = MAX_UPLOAD_BYTES,
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

    if sum(x is not None for x in (content_base64, content, content_stream)) != 1:
        return _raise(
            "INVALID_PRESERVE",
            ["content"],
            "Exactly one of `content_base64`, `content`, or `content_stream` "
            "must be supplied.",
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
        if len(decoded) > max_decoded_bytes:
            return _raise(
                "TOO_LARGE",
                ["content_base64"],
                (
                    f"decoded size {len(decoded):,} bytes exceeds the "
                    f"{max_decoded_bytes:,}-byte limit. Don't push large binaries "
                    "through the model as base64 — it's billed as output tokens. "
                    "Use the POST /upload endpoint (out-of-band, no token cost) or "
                    "land it desk-side via Obsidian Sync instead."
                ),
            )
        artifact_bytes = decoded
        artifact_text = None
    elif content_stream is not None:
        # Large binary: streamed straight to disk in the write block below, so the
        # file never materializes fully in RAM. Size is enforced during the copy.
        artifact_bytes = None
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
        if content_stream is not None:
            # Stream the upload to disk in chunks — peak memory is one chunk, not
            # the whole file. Enforces the byte cap mid-copy (defense in depth; the
            # HTTP layer's max_part_size already bounds the part during parsing).
            _copy_stream_to_file(content_stream, artifact_path, limit=max_stream_bytes)
            written_artifact = True
        elif artifact_bytes is not None:
            # Binary: write directly, no atomic batch (batch_atomic_write is text-only).
            artifact_path.write_bytes(artifact_bytes)
            written_artifact = True
        else:
            artifact_path.write_text(artifact_text or "", encoding="utf-8", newline="\n")
            written_artifact = True

        writes: list[PlannedWrite] = []

        # Optional sidecar. Written when there's a short `description` caption
        # and/or full extracted `text` (the OCR companion that makes a binary
        # searchable). For binary artifacts the convention is `<filename>.md`
        # next to the artifact. For .md artifacts that would produce
        # `<stem>.md.md` (cosmetic but ugly), so use `<stem>-notes.md` instead —
        # the sidecar belongs alongside, not as a double-ext twin of the artifact.
        desc_clean = description.strip() if description and description.strip() else None
        text_clean = text.strip() if text and text.strip() else None
        if desc_clean or text_clean:
            if filename_safe.lower().endswith(".md"):
                stem = filename_safe[:-3]
                sidecar_path = folder / f"{stem}-notes.md"
            else:
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
                    description=desc_clean,
                    text=text_clean,
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
            new_top, _trim_note = indexes._prepend_recent_activity(
                top_index.read_text(encoding="utf-8"),
                date_iso=date_iso,
                summary=activity_summary,
            )
            # Evidence writes don't change Notes/Entities counts, but keeping
            # the sub-index refresh in the path makes drift impossible to
            # accumulate from any write op (cheap walk; no-op if already-fresh).
            sub_writes, new_top_with_counts = indexes.compute_subindex_writes(
                vault_root, top_index_text=new_top
            )
            if new_top_with_counts is not None:
                new_top = new_top_with_counts
            # Cap-50 trim is recorded in log.md; no per-write warning needed.
            writes.append(PlannedWrite(path=top_index, content=new_top))
            writes.extend(sub_writes)
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
            # Pass vault_root so the sidecar (a real `.md`) is embedded on write
            # — that's what makes the binary's extracted text immediately
            # findable. index.md / log.md in the batch are skipped by name.
            batch_atomic_write(writes, vault_root=vault_root)

    except Exception as e:
        log.exception("preserve() failed mid-write; artifact_written=%s", written_artifact)
        warnings.append(f"partial write — reconcile on desktop: {e}")
        raise

    return PreserveResult(
        path=artifact_path.relative_to(vault_root).as_posix(),
        sidecar_path=sidecar_rel,
        warnings=warnings,
    )


def preserve_stream(
    vault_root: Path,
    *,
    scope: str,
    category: str,
    filename: str,
    stream: BinaryIO,
    description: str | None = None,
    text: str | None = None,
    today: dt.date | None = None,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> PreserveResult:
    """Capture a binary STREAM to Evidence/ — the entrypoint for HTTP /upload.

    The bytes arrive out-of-band (multipart over HTTP) and are copied to disk in
    chunks, so a multi-hundred-MB upload never materializes fully in RAM — peak
    memory is one chunk. Funnels through `preserve()` so there is exactly ONE write
    path with identical sanitization, append-only overwrite refusal, sidecar, and
    index/log behavior. The byte cap is enforced during the copy.

    `text` is the artifact's extracted/OCR'd text — it becomes the embedded sidecar
    body so the binary is findable by its content (the OCR companion).
    """
    return preserve(
        vault_root,
        scope=scope,
        category=category,
        filename=filename,
        content_stream=stream,
        description=description,
        text=text,
        today=today,
        max_stream_bytes=max_bytes,
    )


def preserve_bytes(
    vault_root: Path,
    *,
    scope: str,
    category: str,
    filename: str,
    data: bytes,
    description: str | None = None,
    text: str | None = None,
    today: dt.date | None = None,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> PreserveResult:
    """Capture an in-memory `bytes` artifact to Evidence/ (back-compat wrapper).

    Routes through the streaming path (no base64 round-trip) by wrapping the bytes
    in a BytesIO. Prefer `preserve_stream` when the source is already a file-like
    (e.g. an HTTP upload's spooled temp file) so nothing is buffered whole.
    """
    return preserve_stream(
        vault_root,
        scope=scope,
        category=category,
        filename=filename,
        stream=io.BytesIO(data),
        description=description,
        text=text,
        today=today,
        max_bytes=max_bytes,
    )


# ---------------- helpers ----------------


_STREAM_CHUNK = 1024 * 1024  # 1 MiB copy buffer


def _copy_stream_to_file(stream: BinaryIO, dest: Path, *, limit: int) -> int:
    """Copy a binary file-like to `dest` in chunks, enforcing `limit` bytes.

    Peak memory is one chunk regardless of file size. On overflow the partial
    file is removed and TOO_LARGE is raised. Returns the bytes written.
    """
    try:
        stream.seek(0)
    except (OSError, AttributeError, ValueError):
        pass  # non-seekable stream: copy from the current position
    written = 0
    overflow = False
    with dest.open("wb") as out:
        while True:
            buf = stream.read(_STREAM_CHUNK)
            if not buf:
                break
            written += len(buf)
            if written > limit:
                overflow = True
                break
            out.write(buf)
    if overflow:
        dest.unlink(missing_ok=True)
        _raise("TOO_LARGE", ["file"], f"upload exceeds the {limit:,}-byte limit")
    return written


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
    description: str | None = None,
    text: str | None = None,
) -> str:
    """Sidecar .md describing a preserved binary artifact.

    Uses `type: source` (with `source_type: other`) since the page-type
    taxonomy doesn't have a dedicated `evidence` type. Tags surface the
    evidence + scope context.

    `description` is a short human caption; `text` is the full extracted/OCR'd
    content of the artifact. Either or both may be present (the caller only
    writes a sidecar when at least one is). The `## Extracted text` body is what
    makes an otherwise-opaque binary findable once the sidecar is embedded.
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
    if description:
        lines.append("## Description")
        lines.append("")
        lines.append(description)
        lines.append("")
    if text:
        lines.append("## Extracted text")
        lines.append("")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def _prepend_log_entry(
    text: str, *, date_iso: str, rel_path: str, body: str
) -> str:
    """Insert `## [<date>] preserve | <kb-relative-path>` after the `---` separator."""
    title = rel_path.replace("Knowledge Base/", "", 1)
    new_entry = f"## [{date_iso}] preserve | {title}\n\n{escape_wikilinks_for_log(body)}\n"
    sep_idx = text.find(indexes.LOG_SEPARATOR)
    if sep_idx == -1:
        return text.rstrip() + "\n\n" + new_entry + "\n"
    insertion_point = sep_idx + len(indexes.LOG_SEPARATOR)
    return text[:insertion_point] + "\n" + new_entry + "\n" + text[insertion_point:]


def _raise(code: str, missing: list[str], reason: str):
    raise PreserveError(code=code, missing=missing, reason=reason)
