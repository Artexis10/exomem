# Epistemic Inbox

The Epistemic Inbox is Exomem's daily, review-only answer to "what deserves my
attention?" It composes deterministic measurements into one ranked list:

- close active conclusions that may restate, refine, or contradict each other;
- conclusions that are old, rarely surfaced, and weakly linked;
- raw sources that have never been compiled;
- active compiled notes with no durable outbound Markdown connections.

None of these signals is a judgment. Exomem does not decide that a conclusion
is wrong, infer a relationship as fact, decay memory, or edit a note. The agent
or user inspects the evidence and chooses what to do.

## Daily command

```bash
exomem review
```

Human output shows a compact ranked inbox. Automation keeps the shared JSON
envelope:

```bash
exomem review --json
exomem review --state all --json
exomem review --category relation_debt --json
```

The MCP/REST product route is `review_memory(mode="attention")`. Every item
includes its current path, canonical target reference, reasons, stable
`exomem://review/<id>` reference, signal fingerprint, and review state.

## Triage

Triage is explicit write-capable state, separate from read-only review:

```bash
exomem review snooze exomem://review/0123456789abcdef01234567 --until 2026-08-01
exomem review dismiss exomem://review/0123456789abcdef01234567 --why "intentionally standalone"
exomem review reopen exomem://review/0123456789abcdef01234567
```

Agents call `triage_memory` with the same `dismiss`, `snooze`, or `reopen`
action. Decisions live in `Knowledge Base/.review-state.json`. The file is
portable JSON and stores no note content.

A decision binds to the exact signal fingerprint reviewed. Ranking changes and
age counters do not change that fingerprint. A material note edit, new review
reason, changed contradiction partner, or changed source state does, so the item
automatically resurfaces instead of being hidden forever. Expired snoozes also
return to the open inbox.

## Repairing relation debt

Relation debt means an active compiled note has no outbound wikilinks or typed
relations. It does not mean Exomem should invent edges.

1. Inspect the note and nearby context.
2. Run `connect_memory(operation="suggest-relations")` or `suggest-links`.
3. Accept only relationships whose meaning is defensible.
4. Write note-level edges under `## Relations` as
   `- relation_type [[Target]]`; use semantic-block `relations:` metadata for
   claim/finding/evidence-level edges.
5. Re-run review. The repaired note leaves the relation-debt queue.

For batch repair, `review_memory(mode="relation-queue")` presents the same
deterministic suggestions as a fingerprint-guarded accept/reject queue
(see the Review Studio's Relations worklist): accept via
`connect_memory(operation="accept-relation")`, reject via `triage_memory`.
Rejections are fingerprint-bound and resurface when the signal materially
changes.

Existing vaults are repaired incrementally through this loop. There is no
automatic bulk rewrite, and semantic similarity alone never becomes a durable
typed relation.
