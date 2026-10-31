## Why

Under steady governed writes, a full lexical catalogue rebuild can finish its
build and then throw the result away. Two checks fail on writes that the
freshness registry observed and that the build already handles:

- The replay of a completed temporary catalogue demands that the registry still
  sits at the replay's target checkpoint. Any observed write to another page
  during the replay voids it, although each replayed page still proves its own
  signature or absence.
- The final source census compares the whole walk with the rebased target. A
  write that the walk saw on one page and a write that it missed on another
  cannot both match one checkpoint, so the census reports `source_changed`.

On a 30,000-note vault, the first full pass after an injected repair finished
with `source_changed`, and the catalogue stayed behind the live projection until
a later pass found a quiet moment.

## What Changes

- A replay into a detached temporary catalogue keeps its per-path signature,
  membership and absence proofs, but no longer requires the registry to stay at
  the replay's target checkpoint. A temporary catalogue that publishes behind
  the registry is caught up by the next repair. The live catalogue keeps the
  exact registry binding.
- The final census takes one registry snapshot after its walk. When the walk
  disagrees with the rebased target, the census excuses exactly the paths in the
  census replay's delta, which the replay re-proved one by one. Every other path
  must match the registry snapshot. The census stays strict when the replay's
  delta needs a source proof, when the checkpoint lineage does not line up, or
  when the live projection is unavailable.
- Each repair pass emits two cataloged, content-free events,
  `lexical_repair_started` and `lexical_repair_finished`. They carry the pass
  kind, the cause tokens that armed it, the result, one closed stage token, the
  duration, the excused path count and whether the pass was promoted.

## Capabilities

### Modified Capabilities

- `live-index-freshness`: managed lexical repair converges when the walk or the
  replay meets writes that the registry observed, and still aborts on drift that
  no event reported.

## Impact

- `src/exomem/lexstore.py`: detached replay proof, final census excusal, repair
  pass events and closed cause and stage tokens.
- `src/exomem/log_events.py`: two event catalog entries.
- `tests/test_lexstore_atomic_rebuild.py`: census and replay cases.
