"""Bulk media back-fill — make pre-existing Evidence binaries searchable.

`kb-mcp backfill-media` walks Evidence/, and for every media file (image/audio/video/pdf):
  1. writes a `.md` sidecar if missing — so `find()` can surface it (a CLIP/text match maps
     to `<file>.md`, which must exist);
  2. extracts text (OCR / ASR / PDF) if not already done — text-searchable;
  3. CLIP-embeds images — searchable by visual content.

Idempotent: re-running only does outstanding work. Runs on CPU or GPU (engines auto-detect).
The *incremental* path (new uploads) is handled live by the server; this is the deliberate
one-shot pass over content that predates the feature — or for a friend's existing vault.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from . import embeddings, extract, preserve

log = logging.getLogger(__name__)

_EXTRACTED_BY_RE = re.compile(r"(?m)^extracted_by:\s*(.+?)\s*$")
_NOT_DONE = {"none", "pending"}


def _sidecar_for(binary: Path) -> Path:
    name = binary.name
    if name.lower().endswith(".md"):
        return binary.with_name(name[:-3] + "-notes.md")
    return binary.with_name(name + ".md")


def _ocr_done(sidecar: Path) -> bool:
    """True if the sidecar already has extracted text (a real engine in extracted_by)."""
    try:
        head = sidecar.read_text("utf-8")[:800]
    except OSError:
        return False
    m = _EXTRACTED_BY_RE.search(head)
    if not m:
        return False
    v = m.group(1).strip()
    return v not in _NOT_DONE and not v.startswith("failed:")


@dataclass
class BackfillStats:
    scanned: int = 0
    sidecars_created: int = 0
    extracted: int = 0
    extract_failed: int = 0
    clip_indexed: int = 0
    skipped: int = 0


def backfill_media(
    vault_root: Path,
    *,
    do_ocr: bool = True,
    do_clip: bool = True,
    dry_run: bool = False,
    log_fn=log.info,
) -> BackfillStats:
    """Back-fill sidecars + text + CLIP for every media file under Evidence/. Idempotent."""
    stats = BackfillStats()
    evidence = vault_root / "Knowledge Base" / "Evidence"
    if not evidence.is_dir():
        log_fn("no Knowledge Base/Evidence/ directory; nothing to back-fill")
        return stats
    clip_index = embeddings.ClipIndex(vault_root) if do_clip else None
    files = sorted(p for p in evidence.rglob("*") if p.is_file() and extract.media_type_for(p))
    stats.scanned = len(files)
    log_fn(f"scanning {len(files)} media file(s) under Evidence/ (dry_run={dry_run})")

    for i, f in enumerate(files, 1):
        media_type = extract.media_type_for(f)
        try:
            rel = f.resolve().relative_to(vault_root.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        sidecar = _sidecar_for(f)
        need_sidecar = not sidecar.exists()
        need_ocr = do_ocr and not _ocr_done(sidecar)
        need_clip = (
            do_clip and clip_index is not None and media_type == "image" and not clip_index.has(rel)
        )
        if not (need_sidecar or need_ocr or need_clip):
            stats.skipped += 1
            continue
        if dry_run:
            todo = " ".join(t for t, on in
                            (("sidecar", need_sidecar), ("ocr", need_ocr), ("clip", need_clip)) if on)
            log_fn(f"  [{i}/{len(files)}] {rel} -> {todo}")
            stats.sidecars_created += need_sidecar
            stats.extracted += need_ocr
            stats.clip_indexed += need_clip
            continue

        if need_sidecar:
            sidecar, created = preserve.ensure_media_sidecar(vault_root, f)
            stats.sidecars_created += int(created)
        if need_ocr:
            try:
                res = extract.extract_text(f, media_type=media_type)
                preserve.update_sidecar_extraction(
                    vault_root, sidecar, text=res.text.strip() or "(no text detected)", engine=res.engine
                )
                stats.extracted += 1
            except extract.ExtractionUnavailable as e:
                log_fn(f"  ! extraction engine unavailable ({e}); skipping OCR for the rest")
                do_ocr = False
            except Exception:  # noqa: BLE001 — one bad file shouldn't abort the pass
                log.exception("backfill: extraction failed for %s", f.name)
                stats.extract_failed += 1
        if need_clip:
            try:
                clip_index.upsert(rel, embeddings.embed_image(f), f.stat().st_mtime)
                stats.clip_indexed += 1
            except embeddings.ClipUnavailable as e:
                log_fn(f"  ! CLIP unavailable ({e}); skipping CLIP for the rest")
                do_clip = False
            except Exception:  # noqa: BLE001
                log.exception("backfill: CLIP failed for %s", f.name)
        if i % 25 == 0:
            log_fn(f"  …{i}/{len(files)} processed")

    log_fn(f"backfill done: {asdict(stats)}")
    return stats
