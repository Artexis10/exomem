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
import hashlib
import io
import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import indexes, memory_refs, privacy_log
from .kbdir import kb_prefix
from .vault import (
    ContentHashMismatchError,
    PlannedWrite,
    batch_atomic_write,
    escape_wikilinks_for_log,
    kb_root,
    yaml_scalar,
)

log = logging.getLogger(__name__)


def _extract_module():
    from . import extract

    return extract

# Cap extracted text written into a sidecar. `find` rebuilds BM25 over the whole
# corpus per query, so one oversized document (e.g. a 7 MB HTML export rendered to
# markdown) re-tokenizes on every search and degrades ALL lookups. 512 KB sits well
# above any real document yet kills pathological machine-dumps. The original binary
# is always preserved — pull the full content via /download. Env-overridable.
_MAX_EXTRACT_BYTES = int(os.environ.get("EXOMEM_MAX_EXTRACT_BYTES", 512 * 1024))


def _cap_extracted_text(text: str) -> str:
    """Truncate extracted text to `_MAX_EXTRACT_BYTES` (UTF-8), keeping the start.

    No-op for normal documents. A truncated doc stays findable by its start + title;
    only content past the cap isn't text-indexed (the binary still holds it all).
    """
    raw = text.encode("utf-8")
    if len(raw) <= _MAX_EXTRACT_BYTES:
        return text
    kept = raw[:_MAX_EXTRACT_BYTES].decode("utf-8", errors="ignore").rstrip()
    log.warning(
        "extracted text %d bytes exceeds cap %d — truncated; full content stays in the binary",
        len(raw), _MAX_EXTRACT_BYTES,
    )
    return (
        f"{kept}\n\n[... truncated: kept first {_MAX_EXTRACT_BYTES // 1024} KB of "
        f"{len(raw) // 1024} KB; full content is in the original binary — pull it via /download ...]"
    )

MAX_DECODED_BYTES = 5 * 1024 * 1024  # 5 MB — base64-via-model path (tokens cost real money)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB — HTTP /upload path (raw bytes, no token cost).
# Aligns with the Cloudflare free-plan ~100 MB edge cap, so the public path and this app cap
# agree. A deployment that wants larger uploads over a non-Cloudflare route (LAN/Tailscale
# direct to the origin) raises EXOMEM_UPLOAD_MAX_BYTES in its .env.


@dataclass
class PreserveResult:
    path: str                 # vault-relative path of the artifact
    sidecar_path: str | None  # vault-relative path of the .md sidecar (if any)
    warnings: list[str]
    ref: str | None = None
    size: int | None = None
    hash: str | None = None
    hash_algorithm: str | None = None
    media_id: str | None = None
    content_type: str | None = None

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "stored_path": self.path,
            "sidecar_path": self.sidecar_path,
            "ref": self.ref,
            "warnings": self.warnings,
            "size": self.size,
            "hash": self.hash,
            "hash_algorithm": self.hash_algorithm,
            "media_id": self.media_id,
            "content_type": self.content_type,
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
    content_type: str | None = None,
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
    sidecar_ref: str | None = None
    artifact_size: int | None = None
    artifact_hash: str | None = None
    content_type_effective = (
        content_type.strip()
        if content_type and content_type.strip()
        else mimetypes.guess_type(filename_safe)[0]
    )

    try:
        if content_stream is not None:
            # Stream the upload to disk in chunks — peak memory is one chunk, not
            # the whole file. Enforces the byte cap mid-copy (defense in depth; the
            # HTTP layer's max_part_size already bounds the part during parsing).
            artifact_size, artifact_hash = _copy_stream_to_file(
                content_stream, artifact_path, limit=max_stream_bytes
            )
            written_artifact = True
        elif artifact_bytes is not None:
            # Binary: write directly, no atomic batch (batch_atomic_write is text-only).
            artifact_path.write_bytes(artifact_bytes)
            artifact_size = len(artifact_bytes)
            artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
            written_artifact = True
        else:
            artifact_text_bytes = (artifact_text or "").encode("utf-8")
            artifact_path.write_text(artifact_text or "", encoding="utf-8", newline="\n")
            artifact_size = len(artifact_text_bytes)
            artifact_hash = hashlib.sha256(artifact_text_bytes).hexdigest()
            written_artifact = True

        writes: list[PlannedWrite] = []
        rel_artifact = artifact_path.relative_to(vault_root).as_posix()

        # Optional sidecar. Written when there's a short `description` caption
        # and/or full extracted `text` (the OCR companion that makes a binary
        # searchable). For binary artifacts the convention is `<filename>.md`
        # next to the artifact. For .md artifacts that would produce
        # `<stem>.md.md` (cosmetic but ugly), so use `<stem>-notes.md` instead —
        # the sidecar belongs alongside, not as a double-ext twin of the artifact.
        desc_clean = description.strip() if description and description.strip() else None
        text_clean = text.strip() if text and text.strip() else None
        # A media binary (audio/video/image/pdf) with no provided text gets a STUB
        # sidecar — pointer + media_type + `extracted_by: pending` — so it's a
        # first-class find() result immediately and the extraction worker fills the
        # text later. The actionable stub is independent of worker availability:
        # a disabled/unavailable worker can be remediated and retried later.
        extract = _extract_module()
        media_type = extract.media_type_for(filename_safe)
        want_stub = media_type is not None and artifact_text is None and not text_clean
        if desc_clean or text_clean or want_stub:
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
                if text_clean:
                    extracted_by = "upload"   # the uploader/sandbox supplied the text
                elif want_stub:
                    extracted_by = "pending"  # the worker will fill it
                else:
                    extracted_by = None
                sidecar_md = _render_sidecar(
                    artifact_name=filename_safe,
                    scope=scope_safe,
                    category=category_safe,
                    date_iso=date_iso,
                    description=desc_clean,
                    text=text_clean,
                    media_type=media_type,
                    evidence_file=rel_artifact if media_type else None,
                    extracted_by=extracted_by,
                )
                sidecar_ref = memory_refs.ref_from_markdown(sidecar_md)
                writes.append(PlannedWrite(path=sidecar_path, content=sidecar_md))
                sidecar_rel = sidecar_path.relative_to(vault_root).as_posix()

        # Index + log updates.
        rel_artifact_for_summary = rel_artifact.replace(kb_prefix(), "")
        activity_summary = (
            f"`{rel_artifact_for_summary}` (evidence, {scope_safe}/{category_safe}, "
            f"mobile via exomem)"
        )
        log_body = (
            f"Mobile preserve via exomem. scope={scope_safe}, "
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
            warnings.append(f"{kb_prefix()}index.md missing; skipped Recent activity bump")

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
            warnings.append(f"{kb_prefix()}log.md missing; skipped log entry")

        if writes:
            # Pass vault_root so the sidecar (a real `.md`) is embedded on write
            # — that's what makes the binary's extracted text immediately
            # findable. index.md / log.md in the batch are skipped by name.
            batch_atomic_write(writes, vault_root=vault_root)

    except Exception as e:
        if privacy_log.content_private_logging_enabled():
            log.error(
                "hosted preserve failed code=HOSTED_PRESERVE_FAILED artifact_written=%s",
                written_artifact,
            )
        else:
            log.exception("preserve() failed mid-write; artifact_written=%s", written_artifact)
        warnings.append(f"partial write — reconcile on desktop: {e}")
        raise

    return PreserveResult(
        path=artifact_path.relative_to(vault_root).as_posix(),
        sidecar_path=sidecar_rel,
        warnings=warnings,
        ref=sidecar_ref,
        size=artifact_size,
        hash=artifact_hash,
        hash_algorithm="sha256" if artifact_hash else None,
        media_id=f"sha256:{artifact_hash}" if artifact_hash else None,
        content_type=content_type_effective,
    )


def preserve_stream(
    vault_root: Path,
    *,
    scope: str,
    category: str,
    filename: str,
    stream: BinaryIO,
    content_type: str | None = None,
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
        content_type=content_type,
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
    content_type: str | None = None,
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
        content_type=content_type,
        description=description,
        text=text,
        today=today,
        max_bytes=max_bytes,
    )


# ---------------- helpers ----------------


_STREAM_CHUNK = 1024 * 1024  # 1 MiB copy buffer


def _copy_stream_to_file(stream: BinaryIO, dest: Path, *, limit: int) -> tuple[int, str]:
    """Copy a binary file-like to `dest` in chunks, enforcing `limit` bytes.

    Peak memory is one chunk regardless of file size. On overflow the partial
    file is removed and TOO_LARGE is raised. Returns bytes written and SHA-256.
    """
    try:
        stream.seek(0)
    except (OSError, AttributeError, ValueError):
        pass  # non-seekable stream: copy from the current position
    written = 0
    digest = hashlib.sha256()
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
            digest.update(buf)
            out.write(buf)
    if overflow:
        dest.unlink(missing_ok=True)
        _raise("TOO_LARGE", ["file"], f"upload exceeds the {limit:,}-byte limit")
    return written, digest.hexdigest()


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
    media_type: str | None = None,
    evidence_file: str | None = None,
    extracted_by: str | None = None,
    parent_media: str | None = None,
    frame_ts: float | None = None,
) -> str:
    """Sidecar .md describing a preserved binary artifact.

    Uses `type: source` (with `source_type: other`) since the page-type
    taxonomy doesn't have a dedicated `evidence` type. Tags surface the
    evidence + scope context.

    `description` is a short human caption; `text` is the full extracted/OCR'd
    content of the artifact. Either or both may be present (the caller only
    writes a sidecar when at least one is). The `## Extracted text` body is what
    makes an otherwise-opaque binary findable once the sidecar is embedded.

    For a media binary the frontmatter also carries `media_type`
    (audio/video/image/pdf), `evidence_file` (a vault-relative pointer to the
    original), and `extracted_by` (`pending` until the extraction worker fills
    the text, then the engine string; `upload` when the uploader supplied it).
    These make the binary a first-class `find()` result that points at the file.

    `parent_media` + `frame_ts` mark a derived artifact — a scene frame extracted
    from a video at `frame_ts` seconds. `find` groups such children under the
    parent's hit, and CLIP-enqueue points skip them (the parent's per-scene
    vectors own visual search).
    """
    lines = ["---"]
    lines.append("type: source")
    lines.append(f"exomem_id: {memory_refs.new_id()}")
    display_title = f"Evidence: {artifact_name}"
    lines.append(f"title: {yaml_scalar(display_title)}")
    lines.append("source_type: other")
    lines.append(f"captured: {date_iso}")
    if media_type:
        lines.append(f"media_type: {media_type}")
    if evidence_file:
        lines.append(f"evidence_file: {evidence_file}")
    if extracted_by:
        lines.append(f"extracted_by: {extracted_by}")
    if parent_media:
        lines.append(f"parent_media: {parent_media}")
    if frame_ts is not None:
        lines.append(f"frame_ts: {frame_ts}")
    extra_tag = ", scene-frame" if parent_media else ""
    lines.append(
        f"tags: [evidence, {scope.lower().replace(' ', '-')}, "
        f"{category.lower().replace(' ', '-')}{extra_tag}]"
    )
    lines.append("ingested_into: []")
    lines.append("---")
    lines.append("")
    lines.append(f"# {display_title}")
    lines.append("")
    lines.append(f"Preserved under `Evidence/{scope}/{category}/`.")
    lines.append("")
    if description:
        lines.append("## Description")
        lines.append("")
        lines.append(description)
        lines.append("")
    # Emit the section when there's extracted text, or as an empty anchor for a
    # media stub the worker will fill. A pure-description (non-media) sidecar omits it.
    if text or media_type:
        lines.append("## Extracted text")
        lines.append("")
        if text:
            lines.append(_cap_extracted_text(text))
            lines.append("")
    return "\n".join(lines)


def ensure_media_sidecar(
    vault_root: Path, binary_path: Path, *, today: dt.date | None = None
) -> tuple[Path, bool]:
    """Ensure an Evidence media binary has a sidecar — the back-fill of pre-feature files.

    Images/PDFs/audio dropped into Evidence/ before server-side extraction existed have no
    `.md` sidecar, so `find()` can't surface them (a CLIP/text match maps to `<file>.md`,
    which doesn't exist). This writes a minimal stub (`media_type` + `evidence_file` pointer,
    `extracted_by: none`) and embeds it. Idempotent: returns (existing_sidecar, False) if one
    already exists, else (new_sidecar, True). Used by `exomem backfill-media`.
    """
    extract = _extract_module()
    media_type = extract.media_type_for(binary_path)
    if media_type is None:
        raise ValueError(f"not an extractable media file: {binary_path.name!r}")
    name = binary_path.name
    if name.lower().endswith(".md"):
        sidecar = binary_path.with_name(name[:-3] + "-notes.md")
    else:
        sidecar = binary_path.with_name(name + ".md")
    if sidecar.exists():
        return sidecar, False
    rel = binary_path.resolve().relative_to(vault_root.resolve()).as_posix()
    # Derive scope/category from Knowledge Base/Evidence/<scope>/<category>/… for the tags.
    parts = rel.split("/")
    scope = parts[2] if len(parts) > 2 else "evidence"
    category = parts[3] if len(parts) > 3 else "uncategorized"
    md = _render_sidecar(
        artifact_name=name,
        scope=scope,
        category=category,
        date_iso=(today or dt.date.today()).isoformat(),
        media_type=media_type,
        evidence_file=rel,
        extracted_by="none",  # not pending → the auto OCR scan ignores it; backfill OCRs it
    )
    batch_atomic_write([PlannedWrite(path=sidecar, content=md)], vault_root=vault_root)
    return sidecar, True


_EXTRACTED_HEADING = "## Extracted text"


def update_sidecar_extraction(
    vault_root: Path, sidecar_path: Path, *, text: str, engine: str,
    speakers: list[dict] | None = None,
    speaker_verification: str | None = None,
    attempts: int | None = None,
) -> None:
    """Fill a pending media sidecar with extracted text + engine, and re-embed.

    Called by the extraction worker once ASR/OCR/PDF text is ready: sets
    `extracted_by: <engine>` in the frontmatter and replaces the `## Extracted text`
    body, then writes via `batch_atomic_write(vault_root=)` so the sidecar is
    re-embedded and immediately findable by its content. `engine` may be a
    `failed: …` marker so a permanent failure stops the restart re-enqueue loop.

    `speakers` (optional, from opt-in ASR diarization) carries the structured turn
    list; when present its distinct speaker labels are recorded as a `speakers:`
    frontmatter list so the transcript's `[Speaker A]: …` turns are also queryable
    structurally. Omitted (None/empty) leaves the frontmatter unchanged — every
    existing call site is unaffected.
    """
    content = sidecar_path.read_text(encoding="utf-8")
    content = _set_frontmatter_field(content, "extracted_by", engine)
    content = _set_frontmatter_field(content, "processing_state", "completed")
    content = _set_frontmatter_field(content, "processing_error", "null")
    content = _set_frontmatter_field(content, "processing_retryable", "false")
    content = _set_frontmatter_field(content, "processing_next_action", "null")
    if attempts is not None:
        content = _set_frontmatter_field(content, "processing_attempts", str(attempts))
    current_verification = re.search(r"(?m)^speaker_verification:\s*(\S+)\s*$", content)
    if speaker_verification and not (
        current_verification and current_verification.group(1) == "human-verified"
    ):
        content = _set_frontmatter_field(
            content, "speaker_verification", yaml_scalar(speaker_verification)
        )
    if speakers:
        labels: list[str] = []
        for turn in speakers:
            label = str(turn.get("speaker", "")).strip()
            if label and label not in labels:
                labels.append(label)
        if labels:
            content = _set_frontmatter_field(content, "speakers", f"[{', '.join(labels)}]")
    content = _set_extracted_text(content, _cap_extracted_text(text))
    batch_atomic_write([PlannedWrite(path=sidecar_path, content=content)], vault_root=vault_root)


def update_sidecar_processing_failure(
    vault_root: Path,
    sidecar_path: Path,
    *,
    state: str,
    attempts: int,
    error: str,
    retryable: bool,
    next_action: str,
) -> None:
    """Persist actionable worker state without replacing a pending transcript."""
    content = sidecar_path.read_text(encoding="utf-8")
    fields = (
        ("processing_state", state),
        ("processing_attempts", str(attempts)),
        ("processing_error", yaml_scalar(error)),
        ("processing_retryable", "true" if retryable else "false"),
        ("processing_next_action", yaml_scalar(next_action)),
    )
    if state == "failed":
        error_type = error.split(":", 1)[0].strip() or "Error"
        content = _set_frontmatter_field(content, "extracted_by", f"failed:{error_type}")
    for field, value in fields:
        content = _set_frontmatter_field(content, field, value)
    batch_atomic_write([PlannedWrite(path=sidecar_path, content=content)], vault_root=vault_root)


def update_sidecar_processing_pending(
    vault_root: Path,
    sidecar_path: Path,
    *,
    attempts: int,
    expected_hash: str | None = None,
) -> bool:
    """Keep changed-in-flight media automatic and actionable until reconciliation."""
    content = sidecar_path.read_text(encoding="utf-8")
    fields = (
        ("extracted_by", "pending"),
        ("processing_state", "pending"),
        ("processing_attempts", str(attempts)),
        ("processing_error", "null"),
        ("processing_retryable", "true"),
        ("processing_next_action", "wait for media reconciliation"),
    )
    for field, value in fields:
        content = _set_frontmatter_field(content, field, value)
    try:
        batch_atomic_write(
            [
                PlannedWrite(
                    path=sidecar_path,
                    content=content,
                    expected_hash=expected_hash,
                )
            ],
            vault_root=vault_root,
        )
    except ContentHashMismatchError:
        return False
    return True


def _set_frontmatter_field(content: str, field: str, value: str) -> str:
    """Set `field: value` in the leading `---` frontmatter (replace or insert)."""
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    head, body = content[:end], content[end:]
    pattern = re.compile(rf"(?m)^{re.escape(field)}:.*$")
    if pattern.search(head):
        head = pattern.sub(f"{field}: {value}", head, count=1)
    else:
        head = head.rstrip("\n") + f"\n{field}: {value}"
    return head + body


def _set_extracted_text(content: str, text: str) -> str:
    """Replace the body under `## Extracted text` (to the next `## ` or EOF), or append."""
    block = f"{_EXTRACTED_HEADING}\n\n{text}\n"
    idx = content.find(_EXTRACTED_HEADING)
    if idx == -1:
        return content.rstrip("\n") + "\n\n" + block
    after = content.find("\n## ", idx + len(_EXTRACTED_HEADING))
    if after == -1:
        return content[:idx] + block
    return content[:idx] + block + "\n" + content[after + 1 :]


def _prepend_log_entry(
    text: str, *, date_iso: str, rel_path: str, body: str
) -> str:
    """Insert `## [<date>] preserve | <kb-relative-path>` after the `---` separator."""
    title = rel_path.replace(kb_prefix(), "", 1)
    new_entry = f"## [{date_iso}] preserve | {title}\n\n{escape_wikilinks_for_log(body)}\n"
    sep_idx = text.find(indexes.LOG_SEPARATOR)
    if sep_idx == -1:
        return text.rstrip() + "\n\n" + new_entry + "\n"
    insertion_point = sep_idx + len(indexes.LOG_SEPARATOR)
    return text[:insertion_point] + "\n" + new_entry + "\n" + text[insertion_point:]


def _raise(code: str, missing: list[str], reason: str):
    raise PreserveError(code=code, missing=missing, reason=reason)
