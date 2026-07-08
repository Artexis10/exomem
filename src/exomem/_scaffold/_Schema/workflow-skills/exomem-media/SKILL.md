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
1. Search with media-aware filters or artifact terms using `find`.
2. Use artifact-specific tools: extracted text/OCR/transcripts, `get_video_frames`, upload metadata, or preserved evidence paths.
3. Cite raw artifact paths and timestamps/pages/frames when available.
4. Compile textual conclusions only when there is a durable finding.
5. Preserve new raw artifacts before analyzing them.

## Output contract
Return matching artifacts, what was inspected, citations, and any compiled conclusion path.

## Save rules
Raw media belongs in `Sources/` or `Evidence/`; textual notes should summarize conclusions and link back to artifacts.

## Mistakes to avoid
Do not claim visual/audio evidence from filenames alone. Do not discard raw media after extracting text. Do not over-transcribe when a cited artifact path is the durable evidence.
