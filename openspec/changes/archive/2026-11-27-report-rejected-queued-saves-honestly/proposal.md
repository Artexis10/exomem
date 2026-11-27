## Why

On 2026-11-27 a 36-row bulk Records write was queued twice behind a busy vault. Both replays rejected every row and committed nothing, yet the intake stored them as `applied`, and the status read answered "Saved: the queued save was applied." The rejections were argument faults (bare rows, no source) that the intake could have refused before queueing. Each status check also returned the full 22 KB payload while the save waited.

## What Changes

- A replay that returns `committed: false` with rejected rows ends `failed`, with the first row's code and the rejected rows, not `applied`.
- A save stored `applied` although it committed nothing reads "Not saved".
- Under `on_reject: abort`, a bulk write whose rows are malformed or have no source is refused before queueing, naming every such row. A direct call keeps its per-row report.
- A status read returns a save's arguments only when it failed.
- `read_instructions` for `records.bulk_upsert` shows the row shape with a worked example.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `busy-write-intake`: what counts as a failed replay, which argument checks run before queueing, and what a status read returns.

## Impact

`write_intake` replay classification and status view, `record_memory` argument rules, `records` bulk row checks shared by both writers, and the operation help. No change to the store schema, the receipt or the replay order.
