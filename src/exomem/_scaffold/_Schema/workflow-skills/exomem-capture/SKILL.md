---
name: exomem-capture
description: Preserve a durable conclusion or recurring entity from a conversation without dumping transcripts into compiled memory.
version: 0.1.0
---

# exomem-capture

## Purpose
Save durable conclusions and recurring entities at the right epistemic layer.

## When to use
Use when the user asks to save or the session lands on durable reusable knowledge, including stable context about a recurring entity.

## Workflow
1. Decide whether the material is raw evidence or a compiled conclusion.
2. Use `capture_source` for raw captured text or source material.
3. Use `preserve_evidence` for factual text artifacts and `transfer_artifact` for binary evidence.
4. Use `remember` for distilled conclusions: `research-note`, `insight`, `failure`, or `pattern`.
5. For an entity, consult the active entity registry and selected knowledge packs,
   then call `connect_memory(operation="resolve-entity", name=...)` before writing.
6. When one active entity matches, use `edit_memory` for a small stable-fact
   correction or the canonical relation workflow for a new connection.
7. Only when no entity matches and the identity is stable, recurring, central,
   and useful beyond this source, use `connect_memory(operation="create-entity")`.
8. Run `connect_memory(operation="suggest-links")` before writing compiled notes;
   prefer `edit_memory` or `replace_memory` for near-duplicates.

## Output contract
After writing, report exactly `Saved -> <path>` plus one short phrase if needed. If nothing durable was saved, say why.

## Save rules
Keep raw source verbatim when it matters. Keep compiled notes concise, attributed, linked, and written as conclusions, not transcripts.

## Mistakes to avoid
Do not save unresolved brainstorming as a conclusion. Do not bury raw evidence
inside a compiled note. A single incidental mention, unresolved identity, or
transient participant stays in source/note context. Do not create a parallel
page when an existing active note or entity should be edited, linked, or
superseded.
