## Decision

Track one boolean, `landed_since_ask`, in the existing per-session episode state
file. A successful landing (`_successful_landing`, the detector the capture
reminder uses, for Claude `Bash` and Codex `exec_command` / `exec` shapes) sets
it. An ask, a successful record, or the REST re-base clearing the count resets
it. The ask is due only when the flag is set and the existing K-turn and
cooldown conditions hold.

## Shape

- A landing before the cadence is due is remembered, so the ask fires on the
  first Stop that satisfies the turn count and cooldown.
- The `stop_hook_active` continuation re-reads the whole turn, landing included,
  so it records a successful `episode_memory` record (as before) but never a
  landing; otherwise the landing the ask just spent would re-arm it.
- State files from before this change lack the field and read as "no landing";
  an in-flight session is asked again after its next landing.
- Candidate coverage asks keep their own ledger-driven cadence. They repeat once
  per cooldown while the ledger reports actionable work; that is real pending
  work rather than a conversation heuristic, and is not changed here.

## Risk

A decision made in conversation in a session that never lands anything is no
longer prompted. The alternative, a block at every cooldown for the life of the
session, cost a full-context turn each time and was seen as the hook blocking
the session. The agent can still record on its own at any point.
