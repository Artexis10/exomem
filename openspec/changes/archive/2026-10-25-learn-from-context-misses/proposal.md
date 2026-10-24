# Proposal

## Why

Recording a context failure does not establish which source data needs correction or whether a later answer improves.
Agents need a repeatable path from an observed miss to a source-backed correction and verified ordinary use.

## What Changes

- Define an agent-led workflow that links an observed miss, source evidence, a proposed correction, its write receipts, and a recheck.
- Reuse existing incident collections, review surfaces, governed content writers, and curation plans; add no parallel queue or storage system.
- Diagnose the first failed boundary before changing data. Prefer supported summaries, topics, aliases, relations, and corrected knowledge when evidence supports them.
- Preserve unsuccessful checks and unresolved boundaries. A successful write alone never closes a miss.
- Exclude held-out evaluation material from learning and keep source-derived expectations separate from generated answers.
- Keep background learning and model backfill default-off. Missing evidence or unavailable optional work leaves the miss open without blocking ordinary recall.

## Capabilities

### New Capabilities

- `context-miss-learning`: Source-backed, agent-led data correction with linked evidence and bounded verification of context failures.

### Modified Capabilities

None. Existing authority ceilings, incident routing, content writes, and evaluation rules remain the boundaries this workflow composes.

## Impact

The implementation adds portable workflow guidance and an optional generic incident-collection example.
Collection manifests own field names and lifecycle vocabulary; product code does not fix a miss taxonomy.
Existing `working_set_learning` naming advisories remain a possible input, not a replacement for diagnosis or proof.

The active agent supplies semantic judgment through existing tools. The server gains no model call, scheduler, automatic correction, or new command.
Verification uses isolated public examples before any separately authorized private replay.
This proposal is a distinct future workflow contract; it does not change the current packet-mode experiment.
