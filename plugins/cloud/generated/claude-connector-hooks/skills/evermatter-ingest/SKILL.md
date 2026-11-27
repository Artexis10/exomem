---
name: evermatter-ingest
description: 'Add an external article, PDF, pasted text, dataset, image, audio, video or a whole folder to Evermatter: keep the original, then compile what matters. Use when the user says ingest, import, add this, process this source, or shares a file to keep.'
metadata:
  skill_contract: 3bffe9f8efafea9e44462619083dd09d06541e27c3f401cac8127c42fb54304a
  version: "0.2.0"
---

# evermatter-ingest

This workflow builds on the `evermatter` skill. Its rules apply here and are not
repeated. If that skill is not loaded, load it; a client that cannot load it calls
`read_instructions(profile="compact")` for the portable rules. Before the first
compiled-note write of the session, read [the authoring contract](references/semantic-authoring.md);
the same file sits in the `evermatter` skill's folder, and `read_instructions(section="authoring")` serves it too.
Each call's fields are in the tool's description and in `read_instructions(topic=<tool or operation>)`.

## Purpose
Keep the original intact and turn what will be reused into compiled memory.

## When to use
The user asks to ingest, add, import, process or keep an external source, a file or a folder.

## Workflow
1. Check the `skills` that `recall_context` returned for an owner skill that covers this task. If one does, read it with `read_page` and follow its method through the steps below: it decides what to extract and where it goes.
2. Keep the original. Call `save_source` with `content` for text or `files` for attachments, and with `source_kind` and `domain` (open vocabularies: name the label that fits). Proof-bearing material goes to `save_evidence`. With no file handle, `upload_files(lane="source", ...)` sends the bytes and saves them on arrival. Never retype a file.
3. For a whole folder or archive, follow [importing](references/importing.md): `manage_memory(operation="imports.file", arguments={"path": ...})`, repeated with its `continuation` until filing ends.
4. Read what you will compile from: the extracted text with `read_page`, and for media the views in the evermatter-media workflow. Record the scope you actually read; media extraction runs on its own and may still be pending.
5. Decide whether the Source's claims will be reused. If not, pass `options={"custody_only": true}` on the save and report it as kept for custody. If so, prepare the note with `inspect_memory(operation="sources.compile", arguments={"sources": [...]})` and follow the [source compilation pass](references/writing.md#source-compilation-pass). Keeping a Source verifies none of its claims, and the adjacent pages and suggested connections that preparation returns are unreviewed candidates, never accepted edges.
6. Write the conclusions with `create_note`, listing the Source in `sources` so it leaves the unprocessed queue.
7. Fix a wrong title, URL, kind or form with `save_source(..., options={"correction": ...})` and no `files`; the stored original is reused.

## Output contract
Report the originals kept, the notes written and the scope actually read. Name unread, unavailable or uncompiled material.

## Save rules
Originals stay in Sources or Evidence; conclusions go in compiled notes that cite them.

## Mistakes to avoid
Do not skip keeping the original. Do not paste a large source into a note. Do not claim you inspected media without an extraction, frame, transcript or page view you actually used.
