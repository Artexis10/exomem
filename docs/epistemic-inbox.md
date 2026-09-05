<!-- authority:non-specification -->

# Epistemic Inbox

The Epistemic Inbox is Exomem's daily, review-only answer to "what deserves my
attention?" It composes deterministic measurements into one ranked list:

- close active conclusions that may restate, refine, or contradict each other;
- conclusions that are old, rarely surfaced, and weakly linked;
- raw sources that have never been compiled;
- active compiled notes with no durable outbound Markdown connections;
- open Planning items that recorded events already join to.

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

## Unreflected outcomes

`unreflected_outcomes` is the one family that reads structured collections
rather than pages. It fires where two authored facts disagree by omission: a
Records collection holds events that join to a Planning item, and that item is
still open (`lifecycle: active`, status not `completed` or `cancelled`).

The binding is authored, never inferred. A Records manifest declares it on its
Planning link:

```yaml
links:
  plans:
    - reference: exomem://memory/<planning-collection-id>
      query: {limit: 50}
      join:
        title: title
```

`join` maps one to four declared record fields to plan field names, matched
exactly. Records itself never resolves the Planning side: the link stays opaque
on every Records operation, and this family is its only consumer.

What resolves a finding: the item leaves the open state, the binding is removed
from the manifest, or the joined records are gone. Never time, and never the
runtime — no command, sweep or write-path advisory edits either side. You move
the item yourself with `plan_memory(action="triage")`.

The fingerprint is the item reference plus the joined record keys, so a new
event on a dismissed item resurfaces it under the ordinary material-change rule
while the dismissal record stands. A record append or a plan transition applies
its own bounded delta, so the response to the write that opens the gap is the
response that reports it. A Planning reference that cannot be resolved produces
no finding and is reported as unevaluated in the audit's own metadata — never
silently skipped.

## Triage

Triage is explicit write-capable state, separate from read-only review:

```bash
exomem review snooze exomem://review/0123456789abcdef01234567 --until 2026-08-01
exomem review dismiss exomem://review/0123456789abcdef01234567 --why "intentionally standalone"
exomem review reopen exomem://review/0123456789abcdef01234567
```

Agents call `triage_memory` with the same `dismiss`, `snooze`, or `reopen`
action. Decisions live in `.review-state.json` under the external per-vault
machine-local state root. The file is portable JSON and stores no note content.

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

## Reason codes

Every triage decision records *why*, as a closed code. Lead the free text with
one of `intentional:`, `false_positive:`, `handled:`, `deferred:`, or
`too_frequent:`; anything else records `unspecified` and is never an error for
an item decision. The CLI composes the token for you:

```bash
exomem review dismiss exomem://review/0123456789abcdef01234567 \
  --reason handled --why "closed in the incident review"
```

The code rides inside the existing free-text `why` rather than a parameter of
its own, so no tool input schema moves and a client that only ever sends `why`
keeps working unchanged. The `why` is stored verbatim either way: a closed code
alone does not say what you actually meant.

Each record also carries an `origin`: `manual` when a person decided through
the triage surface, `automatic` when the runtime wrote it itself. Records
migrated from the older schema carry `manual`, because that schema could only
be written by the triage surface.

## Silencing a whole signal family

Triage is per item. When a *kind* of signal is noise for your corpus, set that
family's disposition instead of lowering prominence, which silences everything:

```bash
exomem review quiet prediction_window --reason too_frequent --why "fires more than it helps"
exomem review off near-duplicate --reason false_positive
exomem review normal prediction_window
```

Agents use the same `triage_memory` command with a family reference,
`exomem://review/family/<family>`, and the `quiet`, `off`, or `normal` action.
`quiet` and `off` require a reason code.

- `quiet` drops the family from the default review union, from every due-state
  carrier, and from write-path advisories, while it stays reachable on an
  explicit category request.
- `off` additionally drops it from explicit category review; only the
  all-states view still shows it.
- `normal` restores it.

**A quiet family is silent, not clean.** The full audit still measures it, and a
due-state block that omits a family is never evidence that family has nothing
due. `review_memory(mode="dispositions")` lists every non-default family with
its reason, `why`, timestamp, origin, and the number of items of that family
you had already dismissed by hand before quieting it.

## What the review store records, and how it stays small

The external per-vault `.review-state.json` holds three things: the triage
decisions, the family dispositions, and a first-surfaced ledger recording when
each signal first reached a served surface — the review list, a due-state carrier, or a
write advisory. "Reached" means DELIVERED, not produced: a carrier block that a
batch, the change-only rule, or a `legacy` response dropped was shown to nobody
and is not recorded. Neither is anything withheld by governance, filtered by a
disposition, seen only by the audit, or merely looked up — resolving one
reference scans every queue to find it, and a scan is not a surface. The ledger
is never backfilled, and a store that cannot be written changes nothing a
reader acts on.

Reading the store is the other way round. The ledger write is best effort, but
the DECISIONS are not: a `.review-state.json` that cannot be read or parsed
makes the review surfaces refuse with `REVIEW_STATE_INVALID` rather than answer
as though nothing had ever been decided, because the second one silently
resurrects every dismissal in the vault. `maintain_memory(mode="reconcile")` is
the way back — it reads at a raised limit precisely so a store that outgrew the
ordinary one can still be compacted, and it reports `review_state_compaction:
{"error": ...}` when it cannot, because a healer that fails silently is
indistinguishable from one that found nothing to do.

The due-state carriers fail closed too, in the direction that suits a carrier:
they serve **no block** rather than refusing. Every count they report is
filtered by a triage decision and a family disposition read out of the same
store, so an unreadable one would otherwise put every dismissed item back in
front of you. Silence costs an advisory you can still ask for by name; the
alternative costs the trust that dismissing works. The write itself is
unaffected — this is the advisory attached to the response, not the mutation.

The store compacts itself, within limits worth stating plainly. Expired snoozes
older than 90 days and ledger entries older than 400 days are dropped once the
file passes its size or record threshold; a standing decision is never dropped,
whatever its age. So compaction bounds the ledger and it does not bound the
store: a vault that accumulates dismissals grows, and no retention rule brings
it back down. The drop is reported under `stats.compaction` with
`origin: automatic`, and `maintain_memory(mode="reconcile")` returns the same
report so a compaction is never silent.

## Emission counters

The external per-vault `.due-state.json` carries the maintained due-state
projection and an `emission` section: `writes` counts the governed writes the
projection has absorbed and `emissions` counts the due-state blocks actually delivered. A
command that writes many pages at once is one batch, and a batch delivers at
most one block rather than one per write — the counters are how that is
checkable rather than merely claimed, and checking it is the point of writing
them down.

Successive single `record_memory` or `plan_memory` calls each carry a block, and
that is the accepted behaviour rather than a leak: the governor suppresses an
identical total, and each of those writes genuinely changes the total. Turning
that into one block per session would be a change to the governance rule, not to
the carrier.

The section also carries `due_total`: how many items were in the last block a
caller was actually handed. It has exactly one writer — the delivery path — so a
vault that has delivered nothing reports `0`. It is informational, and
deliberately not a measure of any particular batch: it describes the last
delivery whenever that happened, and survives every batch after it.

Telling "governance suppressed eleven repeats" from "this batch delivered
nothing" therefore needs the DELTA, not the totals. `writes` and `emissions` are
cumulative over the vault's whole life, so the batch under test is the
difference between two readings — which is what the bench's counter assertion
compares, and why a batch that delivered nothing reports `unsupported` instead
of inheriting a `pass` from a block emitted before it started.

Which is what it reports today. No product command commits more than one
governed write through the write carrier: measured on `adoption_studio` apply,
`maintain_memory(mode="fix")`, `preserve_artifacts` and `process_media`, each
of which writes eight to twelve pages and reaches the carrier zero times. The
batch scope those leaves hold is therefore a guard for a route nothing takes
yet, not a mechanism doing work today; where the carrier IS reached — a single
governed write, and the recall and bootstrap carriers — one scope over twelve
runs still delivers at most one block, and that is where the requirement is
proven.
