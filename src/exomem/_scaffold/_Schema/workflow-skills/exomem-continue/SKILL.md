---
name: exomem-continue
description: Resume prior project or session context from Exomem when the user wants to continue, pick something back up, or remember what was happening on a topic.
version: 0.1.0
---

# exomem-continue

## Purpose
Resume prior context without guessing from chat history alone.

## When to use
Use when the user asks to continue, resume, pick up a project, recover prior state, or asks what they were doing on a topic.

## Workflow
1. Search first with `ask_memory(detail="compact", rerank=false)`, preferring active compiled notes and project-relevant terms from the prompt.
2. Use `ask_memory(deep=true)` when several hits matter or the state needs synthesis.
3. Read only the top relevant pages with `read_memory` when excerpts are not enough.
4. Prefer recent active compiled notes, then linked sources/evidence when provenance is needed.
5. Summarize current state, decisions already made, blockers, and likely next actions.

## Output contract
Return a compact continuation brief with cited Exomem pages and a short next-action list. Say when the search missed or was thin.

## Save rules
Save only if the resumed work produces a new durable decision, solved problem, failure, pattern, or research finding. Use `remember`, `edit_memory`, or `replace_memory` as appropriate.

## Mistakes to avoid
Do not invent prior context from memory. Do not treat a scoped miss as absence. Do not dump long note bodies when a state brief is enough.
