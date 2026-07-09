---
name: exomem-review
description: Use Exomem attention and audit queues to surface stale conclusions, contradictions, and unprocessed sources safely.
version: 0.1.0
---

# exomem-review

## Purpose
Drain Exomem review queues into safe next actions.

## When to use
Use when the user asks to review the KB, see what needs attention, drain backlog, or inspect stale/contradictory/unprocessed material.

## Workflow
1. Start with `attention` for the ranked review queue.
2. Use `audit` when the user asks for broader health checks or specific categories.
3. For unprocessed sources, use `propose_compilation` before writing a compiled note.
4. For stale or contradictory compiled notes, read the relevant pages and decide keep, edit, replace, reconcile, or leave alone.
5. Propose risky actions before mutating.

## Output contract
Return a prioritized review list, recommended safe actions, and any completed saves or edits.

## Save rules
Compile unprocessed sources only when they form a coherent note. Use supersession for changed conclusions.

## Mistakes to avoid
Do not auto-delete. Do not treat queue presence as proof something is wrong. Do not batch risky mutations without user confirmation.
