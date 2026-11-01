## Why

A detached lexical repair waits up to 30 s for the registry to observe a page
that differs from the disk. That is right while a write is publishing. A page
that differs with no write in flight is drift that no event will report, and
the repair waited the full 30 s before it refused. In CI, 13 tests that edit a
file outside any event spent 25 to 30 s each in teardown on that wait, against
under 5 s before the wait existed. A cell pays the same delay for such drift.

## What Changes

- The wait for the registry's observation continues only while a write in the
  process holds a mutation boundary. One poll after the last write released, it
  ends, and the replay or the census refuses at once, as it did before the wait.
- The 30 s stays as the outer bound. One deadline now covers every path of a
  round, so a replay call waits at most 90 s, and the census waits once more,
  up to 30 s, before each of its replays.
- `lexical_repair_finished` carries `declined_for` for a full pass that declined
  for a moving source: `observation_timed_out`, `retargets_exhausted` or
  `unobserved_drift`.

## Capabilities

### Modified Capabilities

- `live-index-freshness`: the wait for an observed write ends when no write can
  still be publishing, and a declined pass names the bound that ended it.

## Impact

- `src/exomem/lexstore.py`: the wait, the census wait and the new field.
- A write in another process holds no boundary that this process sees. A path it
  changes reaches the registry only as an event, so a replay that meets it
  refuses at once instead of waiting.
