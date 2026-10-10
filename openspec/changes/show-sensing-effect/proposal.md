## Why

The owner cannot tell whether sensing or the Dreamer has any effect. No record says, per family, what was surfaced and what became of it. `review_memory(mode="upkeep")` answers only `status: "unavailable"`, so nobody can tell a worker that never started from one that its gate holds. Acting on one due item runs whole-vault audits, so no agent finishes one before its client times out.

## What Changes

- `review_memory(mode="upkeep")` adds a closed `reason` to `status: "unavailable"`: `worker_not_running`, `no_tick_yet` (with the waiting reason, since when, and whether it is live or recorded), `schema_mismatch`, `locked` or `unreadable`.
- The dispositions view adds an `effect` block: per family, over a stated seven-day window, the counts `surfaced`, `dismissed`, `snoozed`, `cleared` and `open`, each with its source. `cleared` and `open` are `unknown` for a family whose current set only an audit can list. Rows without a family count under `unattributed`.
- Both surfacing calls (the due-state carrier and the attention surface) stamp the family on the first-surfaced ledger.
- Each due-state row carries its stored fingerprint. A caller that supplies it resolves the due signal from its stored entry and a re-check of its own page, without a whole-vault audit. A reference without it resolves as it does today.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `command-surface`: the upkeep review mode states why it is unavailable; the dispositions view reports each family's effect.
- `attention-queue`: a due-state row's fingerprint addresses its own signal, and resolves it without a whole-vault audit.

## Impact

- `src/exomem/upkeep.py`, `dreamer.py`, `dreamer_store.py`: the reason and the recorded wait.
- `src/exomem/review_state.py`, `commands.py`: the effect aggregation and the dispositions view.
- `src/exomem/due_state.py`, `attention.py`: family stamping and the bounded resolver.
- No tool input parameter changes. No new storage file; the wait is recorded in the Dreamer's own sidecar.
