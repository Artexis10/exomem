---
name: exomem-reflect
description: Distill a session or project episode into decisions, failures, patterns, open questions, and next actions.
version: 0.1.0
---

# exomem-reflect

## Purpose
Convert experience into reusable memory.

## When to use
Use when the user asks what was learned, asks to reflect, closes a project episode, or wants patterns extracted from a session.

## Workflow
1. Search Exomem with `ask_memory` for the project/topic so reflection builds on existing memory.
2. Extract durable decisions, solved problems, diagnosed failures, reusable patterns, open questions, and next actions.
3. Save each durable conclusion with `remember` at the right type: `insight`, `failure`, `pattern`, or `research-note`.
4. Prefer one strong note over many weak notes unless the conclusions are genuinely different.
5. Link related pages with `connect_memory(operation="suggest-links")`.

## Output contract
Return what was saved, what was intentionally not saved, and remaining open questions.

## Save rules
Save conclusions, not a transcript. Keep enough context that the lesson is reusable later.

## Mistakes to avoid
Do not capture every turn. Do not flatten failures and patterns into generic summaries. Do not erase uncertainty or next actions.
