## Why

A client's checkpoint state root lists its entries in a fixed-size prune catalog of 512 slots, and sessions stay for the 30-day retention. A home with many parallel sessions and lanes fills the catalog well inside 30 days. The catalog then refuses every new session, so every `PreCompact` and `SessionEnd` write fails and `SessionStart` restores nothing. On 1 November 2026, five Claude homes on one machine held 512 sessions each, and their last 300 hook events were all errors or empty restores. Pruning ran only after a successful write, so a full home never freed a slot.

## What Changes

- When a new session needs catalog slots that are not free, the hook drops catalog entries whose root entry is gone, then prunes the least recently changed sessions through the same tombstone path as an expired session. Only the age checks are lifted; the session must still be this root's own recognized, unlocked session, and the current session is never evicted.
- Each `PreCompact` and `SessionEnd` prunes expired state before it writes, so a failed write still prunes.
- Retention stays 30 days by default; a full state root shortens it for its oldest sessions.

## Impact

- Affected spec: `compaction-continuation-checkpoints`.
- Affected code: `src/exomem/_hooks/exomem_continuation_checkpoint.py` and its plugin copies.
