---
name: exomem-defrag
description: Reconcile duplicate, stale, or conflicting Exomem memory while preserving history through review, merge, or supersession.
version: 0.1.0
---

# exomem-defrag

## Purpose
Reduce duplicate, stale, or conflicting memory without losing provenance.

## When to use
Use when the user asks to defrag a topic, reconcile notes, resolve contradictions, merge duplicates, or inspect what is stale.

## Workflow
1. Search the topic with `ask_memory`, using `ask_memory(deep=true)`, `connect_memory`, or `review_memory` when graph, stale, audit, or evolution context matters.
2. Group findings into keep, merge, supersede, or leave alone.
3. Read candidate pages with `read_memory`, including history when needed.
4. Use `replace_memory` for changed conclusions and `edit_memory` only for minor corrections.
5. Preserve raw sources and evidence; keep superseded history visible.

## Output contract
Return the reconciliation decision for each candidate and the paths changed or left alone.

## Save rules
Only mutate when the correct action is clear. Use supersession for meaningful changes.

## Mistakes to avoid
Do not auto-delete memory. Do not treat semantic proximity as contradiction by itself. Do not merge notes that answer different questions.
