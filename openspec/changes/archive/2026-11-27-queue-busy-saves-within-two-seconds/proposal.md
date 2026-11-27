## Why

A busy save kept its caller waiting about 15 s before the queued receipt. On the personal service between 2026-11-25 and 2026-11-27, every one of 86 saves that arrived behind queued saves waited the full 15 s, because the queue did not empty in time. Of 730 saves that got the boundary directly, 7 waited more than 2 s and none more than 5 s. The long wait delayed receipts and bought almost no direct results.

## What Changes

- A save that arrives while saves are queued is queued at once, without waiting for the queue to empty.
- The default intake wait drops from 15 s to 2 s. `EXOMEM_BUSY_INTAKE_AFTER_SECONDS` still sets it.
- A few more saves answer queued instead of saved: about 7 in the same two days.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `busy-write-intake`: the intake wait default and the wait behind a non-empty queue.

## Impact

`write_intake.Admission` and the intake wait default. No change to the store, the receipt or the replay.
