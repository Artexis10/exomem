---
name: exomem-curate
description: Improve Exomem note quality by adding links, clarifying compiled notes, and organizing safely without editing raw Sources or Evidence.
version: 0.1.0
---

# exomem-curate

## Purpose
Improve the KB graph and compiled-note quality without damaging provenance.

## When to use
Use when the user asks to clean up, organize, link, tidy, or improve a set of Exomem notes.

## Workflow
1. Search related notes with `ask_memory`; use `connect_memory` for graph context, inbound links, or link suggestions when graph shape matters.
2. Identify safe improvements: missing links, stale wording, weak titles, duplicate tags, or unlinked entities.
3. Use `edit_memory` for small compiled-note fixes.
4. Use `replace_memory` for substantial rewrites or changed conclusions.
5. Leave raw `Sources/` and `Evidence/` untouched except for metadata the core contract explicitly allows.

## Output contract
Summarize changes made or proposed, citing affected paths. Flag risky changes instead of applying them silently.

## Save rules
Preserve history. Prefer supersession when meaning changes. Keep links useful, not decorative.

## Mistakes to avoid
Do not rewrite raw sources or evidence. Do not mass-edit without reading the relevant notes. Do not collapse distinct conclusions just because they share keywords.
