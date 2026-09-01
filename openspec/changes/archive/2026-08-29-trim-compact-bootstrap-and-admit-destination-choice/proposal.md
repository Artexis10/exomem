# Trim the compact bootstrap and admit the destination-choice clause

## Why

Compact bootstrap is 61,376 bytes against its 61,400 ceiling — 24 bytes of
headroom, which the budget test's own history pre-commits is "not headroom: the
next addition trims or argues". Two deliveries have already recorded deferrals
against this queue: route-lifecycle ("compact-bootstrap trim is still its own
queued change") and the semantic scope-divergence sensor (design D8: the
destination-choice teaching landed in the FULL contract only, with compact
carriage "that work's acceptance concern"). The canonical
`agent-bootstrap-contract` spec states the hook explicitly: "the compact payload
remains byte-identical until the queued compact-bootstrap trim admits the
clause." This is that change.

## What Changes

- **Trim compact below the warning line.** Recover bytes from the compact
  payload by the method the budget test's log already established: redundancy
  first — text that repeats what the payload says elsewhere — never a rule, a
  landing, a consequence, or a named non-outcome. Every trimmed passage is
  argued in the budget test's running log, following its convention. The
  ceiling (61,400) does not move.
- **Admit the destination-choice clause into compact.** Compact then carries
  the pre-write destination-choice teaching (route an emerging out-of-scope
  durable thread at write time; post-write advisories are the safety net),
  condensed wording allowed, same rule as full.
- **Land with real headroom.** After admission, compact finishes at or below
  61,400 − 512 bytes — under `HEADROOM_WARNING_BYTES`, the line the test
  already codifies as where the budget becomes a cliff — so the next contract
  change starts with spendable budget instead of inheriting this queue.

## Non-goals

- No ceiling movement, no weakening of `MINIMUM_SAVING_RATIO`, and no change
  to the full or diagnostics TEACHING beyond what clause admission requires.
  Because the method cuts redundancy in shared prose, full and diagnostics
  lose the same redundant bytes — as both precedent trims did — but no
  teaching leaves any profile, and full's destination-choice wording stays
  byte-identical.
- No new teaching content beyond the already-specified destination-choice
  clause; no tool-surface changes.

## Impact

- Affected specs: `agent-bootstrap-contract` (MODIFIED: destination-choice
  requirement gains compact carriage).
- Affected code: `src/exomem/commands.py` (`op_bootstrap` compact prose),
  `tests/test_bootstrap_compact_budget.py` (log entry; pins that move with
  text), `tests/test_epistemic_bootstrap_contract.py` (the compact
  byte-identical pin flips to a compact-carriage pin).
