## Why

The intake can treat a matching resend as a new write after an earlier queued save fails. When it cannot establish the earlier effect, a new write can duplicate it.

## What Changes

- Keep the original receipt on a matching resend of a failed save within the retry window.
- Tell the caller to check the recorded outcome when an earlier save fails.

## Capabilities

### Modified Capabilities

- `busy-write-intake` retains terminal failures under their original identity and keeps unproven outcomes visible.

## Impact

The change uses the existing intake database and lookup. The dispatcher and older stored receipts keep their format.
