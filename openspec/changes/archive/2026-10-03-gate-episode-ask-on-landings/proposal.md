## Why

The capture Stop hook's episode ask fires every K substantive turns once its
cooldown has elapsed, and only a successful `episode_memory` record resets it.
A long autonomous session that never records an episode is therefore blocked at
every cooldown for as long as it runs: one Codex session got the check 24 times
in a day, each a full extra model turn. The capture reminder already fires only
on landings; the episode ask should too.

## What Changes

- The episode ask additionally requires a landing (the capture reminder's
  detector) since the last episode ask or, if newer, the last successful record.
  The turn and cooldown cadence still applies on top.
- A session that never lands work is never asked, and an ask answered without a
  record is not repeated until work lands again.
- Unchanged: the `stop_hook_active` self-disarm, the REST `inspect` re-base,
  candidate-coverage asks, `prominence=off` and the env overrides.

## Impact

- `src/exomem/_hooks/exomem_capture_nudge.py` and its generated copies
  (`plugins/claude-code/hooks/`, `plugins/cloud/generated/`).
- `QUICKSTART.md`, `docs/prominence.md`.
- Capability: `agent-bootstrap-contract`.
