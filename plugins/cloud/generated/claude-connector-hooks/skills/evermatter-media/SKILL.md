---
name: evermatter-media
description: 'Find, view, cite and keep media in Evermatter: PDFs, images, audio and video, with their transcripts, pages and frames. Use when the user asks about a recording, photo, scan or video, wants to look inside one, or shares media to keep.'
metadata:
  skill_contract: 6eefb82e104f7d0910acdf8a799a959e3227acc8d824bfcdb3b3264a8108fc11
  version: "0.2.0"
---

# evermatter-media

This workflow builds on the `evermatter` skill. Its rules apply here and are not
repeated. If that skill is not loaded, load it; a client that cannot load it calls
`read_instructions(profile="compact")` for the portable rules. Before the first
compiled-note write of the session, read [the authoring contract](references/semantic-authoring.md);
the same file sits in the `evermatter` skill's folder, and `read_instructions(section="authoring")` serves it too.
Each call's fields are in the tool's description and in `read_instructions(topic=<tool or operation>)`.

## Purpose
Work from what media actually shows or says, and cite it precisely.

## When to use
The user asks about, views or shares a PDF, image, audio recording or video.

## Workflow
1. Check the `skills` that `recall_context` returned for an owner skill that covers this task. If one does, read it with `read_page` and follow its method through the steps below: it decides what to look for and where it goes.
2. Find media with `search_memory(query=..., file_types=["image", "video"])` or the artifact's terms.
3. Look inside: `inspect_memory(operation="media.image", arguments={...})` for an image, `media.video` for frames of a video, `media.preview` for a page or excerpt, and `read_page` for the extracted text, OCR or transcript. Check processing with `media.status`; `manage_memory(operation="media.retry", ...)` or `media.reprocess` fixes a failed or faulty run.
4. Get the original bytes with `download_files` when the user needs the file itself.
5. Keep new media before you analyse it: `save_source(files=...)`, or `save_evidence(files=...)` for proof; `upload_files` when you hold no file handle.
6. Cite the artifact with its timestamps, pages or frames. Write a note only for a durable finding, with the artifact in `sources`.
7. For a set of recordings and documents to compile, follow the multi-source steps of the source compilation pass in [writing](references/writing.md).

## Declaring the language
When a recording's languages are unknown at ingest, ask the user once: "One language or several?" Several is slower, because the engine detects the language in every window. Declare it on the first processing with `manage_memory(operation="media.process", arguments={"path": ..., "language": <code>})`, or `"multilingual": true` for several.

## Faulty transcripts
Check the sidecar's `asr_language`, `asr_language_probability`, `asr_language_source`, `asr_language_mode` and `asr_speech_seconds` before trusting a transcript. A transcript in the wrong language, or one that repeats a line, is a processing fault, not content: fix it with `manage_memory(operation="media.reprocess", arguments={"path": ..., "language": <code>})`, or `"multilingual": true`. The old extraction stays on a linked prior page.
## Output contract
What the media shows or says, cited by timestamp, page or frame, and what was kept or written.

## Save rules
Media stays in Sources or Evidence. Notes cite it instead of pasting whole transcripts.

## Mistakes to avoid
Do not claim visual or audio content from a file name. Do not discard media after extracting text. Read an extracted text in full before concluding from it.
