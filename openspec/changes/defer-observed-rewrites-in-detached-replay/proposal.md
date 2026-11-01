## Why

A full lexical repair cannot publish while a large import runs. In one Cloud rehearsal of a 70,000-file import, 14 of 15 full passes declined as `source_changed` with `retargets_exhausted`. The published catalogue then stayed unchanged for about 82 minutes, for the whole import.

The cause is the retarget loop in the off-barrier detached replay:
- An import rewrites the same index pages on every call.
- When a replayed page changes after the replay's target, the replay waits for the newer event, then applies it and proves the whole delta again.
- Each round outlasts the interval between import calls, so some page has always changed again.
- After 3 retargets the pass declines.
- The gap to live then exceeds the barrier replay cap, so only another full pass can close it, and that pass fails the same way.

A background catch-up fails for a related reason. It records a checkpoint only when the registry stops moving during the round, which never happens under a sustained import.

## What Changes

- The detached replay stops chasing rewritten pages. It waits once for the registry to observe the newer write, then records its original target and leaves those pages as ordinary lag for the next replay. Under the publication barrier it never waits, and it defers only pages that the registry already names after its target.
- A delta that needs a source proof still defers nothing. Drift that no event reports still declines the pass.
- An off-barrier catch-up that ran out of steps records, per lagging scope, the newest checkpoint it fully applied, and it queues a lag-only catch-up for newer events instead of a full repair. A round that moves no stored checkpoint still declines as outpaced.
- Telemetry: `retargets_exhausted` disappears from `declined_for`. `lexical_repair_finished` gains `deferred`, the count of pages that a pass published behind a newer observed write.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `live-index-freshness`: the requirement "Managed Lexical Repair Converges Under Live Traffic" replaces bounded retargeting with deferral, and adds the catch-up rule.

## Impact

- `src/exomem/lexstore.py`: `_rebase_detached_catalog`, `_await_observed_rewrites`, the barrier refusal path, the background catch-up stamp, and the repair telemetry.
- Tests that pin the retarget behaviour are rewritten for deferral.
- No change to the stored catalogue format, to the readiness contract or to recall answers.
