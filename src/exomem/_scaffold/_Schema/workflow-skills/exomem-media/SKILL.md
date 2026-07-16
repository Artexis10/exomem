---
name: exomem-media
description: Search, inspect, cite, and preserve Exomem media artifacts such as PDFs, images, audio, and video.
version: 0.1.0
---

# exomem-media

## Purpose
Use Exomem's multimodal substrate without flattening every artifact into text.

## When to use
Use when the user asks to find media evidence, look inside a recording, inspect a PDF/image/audio/video, or recall where something appeared.

## Workflow
1. Search with media-aware filters or artifact terms using `ask_memory`.
2. Use artifact-specific product tools: extracted text/OCR/transcripts, `process_media` status/retry, `read_media`, `transfer_artifact`, upload metadata, or preserved evidence paths.
3. Cite raw artifact paths and timestamps/pages/frames when available.
4. Compile textual conclusions with `remember` only when there is a durable finding.
5. Preserve new raw artifacts with `capture_source`, `preserve_evidence`, or `transfer_artifact` before analyzing them.

## Output contract
Return matching artifacts, what was inspected, citations, and any compiled conclusion path.

## Save rules
Raw media belongs in `Sources/` or `Evidence/`; textual notes should summarize conclusions and link back to artifacts.

## Mistakes to avoid
Do not claim visual/audio evidence from filenames alone. Do not discard raw media after extracting text. Do not over-transcribe when a cited artifact path is the durable evidence.
