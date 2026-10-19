# Proposal

## Why

The global five-item due list can omit an overdue item on material that activation just served.
A bounded association lets the active agent review that item when it matters to the current conversation.

## What Changes

- Add at most one optional `relevant_due_refs` candidate to a resolved activation packet.
- Select existing due projection buckets for released current material before audience recomposition and ordering.
- Keep the candidate only while its matching material survives final filtering.
- Teach the agent to judge relevance, raise at most one due item per client conversation, and use one existing writer or dismissal.

The carrier runs on ordinary activation and fails softly when its projection is unavailable.
It runs no model, creates no notification service, and changes no public input schema.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `context-activation`: Carry a bounded due candidate associated with released current material.
- `agent-bootstrap-contract`: Explain conversation scope and the existing review and resolution operations accurately.

## Impact

The change affects the existing due projection reader, artifact-role projection reader, activation wrapper, final egress filter, hook renderer, and skill scaffold.
Maintained producers refresh generated guidance and schema contracts.
The existing due-state block, recall emission ledger, mutation authorities, and authored resolution rules retain their contracts.
