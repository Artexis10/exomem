---
name: exomem-ingest
description: Ingest an external article, PDF, pasted note, dataset, image, audio, or video into Exomem while preserving raw evidence before compiling conclusions.
version: 0.1.0
---

# exomem-ingest

## Purpose
Turn external artifacts into preserved evidence plus useful compiled memory.

## When to use
Use when the user asks to ingest, add, import, process, or preserve an external source or artifact.

## Workflow
1. Identify the artifact type: text, article, PDF, dataset, image, audio, video, or mixed media.
2. Preserve the raw source first with `capture_source`, `preserve_evidence`, or `transfer_artifact`.
3. For media, use `read_media`, extracted text/OCR/transcripts, or media-aware `ask_memory`; do not pretend the artifact is plain text.
4. If the source is worth distilling, use `compile_source` for planning and `remember` for the compiled note.
5. Link related prior notes with `connect_memory(operation="suggest-links")`.

## Output contract
Report the stored source/evidence path, any compiled note path, and what remains unprocessed.

## Save rules
Raw artifacts stay in `Sources/` or `Evidence/`. Distilled conclusions go in typed compiled notes with source links.

## Mistakes to avoid
Do not skip raw preservation. Do not paste large raw artifacts into compiled notes. Do not claim media content was inspected unless an extraction, frame, transcript, OCR, or artifact view was actually used.
