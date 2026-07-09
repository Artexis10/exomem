---
name: exomem-capture
description: Preserve a durable conclusion from a conversation or session without dumping transcripts into compiled memory.
version: 0.1.0
---

# exomem-capture

## Purpose
Save durable conclusions at the right epistemic layer.

## When to use
Use when the user says to save, remember, file, capture, or when the session lands on a decision, solved problem, diagnosed failure, or reusable pattern.

## Workflow
1. Decide whether the material is raw evidence or a compiled conclusion.
2. Use `capture_source` for raw captured text or source material.
3. Use `preserve_evidence` for factual text artifacts and `transfer_artifact` for binary evidence.
4. Use `remember` for distilled conclusions: `research-note`, `insight`, `failure`, or `pattern`.
5. Run `connect_memory(operation="suggest-links")` before writing compiled notes; prefer `edit_memory` or `replace_memory` for near-duplicates.

## Output contract
After writing, report exactly `Saved -> <path>` plus one short phrase if needed. If nothing durable was saved, say why.

## Save rules
Keep raw source verbatim when it matters. Keep compiled notes concise, attributed, linked, and written as conclusions, not transcripts.

## Mistakes to avoid
Do not save unresolved brainstorming as a conclusion. Do not bury raw evidence inside a compiled note. Do not create a parallel page when an existing active note should be edited or superseded.
