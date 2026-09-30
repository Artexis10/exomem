## Why

The 0.81.0 preference record saves one engagement level per identity and vault. A user who wants Balanced engagement while coding but Maximal engagement in a conversational client cannot express that: saving Maximal for the identity also makes every coding client Maximal, and the client-derived defaults that already distinguish those surfaces are overridden by the single saved value. The evolving-vocabulary change (`activate-agent-led-vocabulary-evolution`, task 4.3h) depends on this capability owning that split rather than introducing a vocabulary-only setting.

## What Changes

- Add an engagement context dimension, `coding` or `conversation`, derived from the client surface the request already detects, to the saved preference record.
- Let `configure_memory` set or clear a context-specific level while keeping the 0.81.0 identity-wide set unchanged for existing clients.
- Resolve effective prominence from the context-specific value before the identity-wide value, and report which one applied.
- Upgrade existing records in place on first write; readers of the previous schema degrade to configuration or defaults without erasing the newer record.
- Keep context as an eagerness selector only: it never selects a principal, vault, storage path or authority ceiling.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `agent-prominence-control`: preference records gain a context dimension under one revision; precedence gains a context tier; the canonical operation gains a context argument and a clear action; client context is constrained to eagerness.

## Impact

Prominence resolution and preference storage; the canonical `configure_memory` command and its derived MCP/REST/CLI schema and generated metadata; bootstrap and workflow projections that report the effective source; public prominence guidance. Hosted agent profiles keep their pinned membership and are not changed. No identity federation, models, dependencies or background workers are added.
