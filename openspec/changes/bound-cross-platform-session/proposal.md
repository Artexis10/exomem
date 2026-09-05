# Proposal: bound-cross-platform-session

## Why

The nightly cross-platform lane reported Windows failures containing no failing
test:

```
2929 passed, 226 skipped, 9778 deselected, 3 warnings in 2702.40s (0:45:02)
##[error]Process completed with exit code 1.
```

That is `--session-timeout=2700` firing. Measured Windows sessions on this lane
ran 2481s, 2449s and 2595s against that 2700s cap, so whichever shard drew the
slowest runner reported a green suite as a red lane, and between zero and two
did per night.

`install-readiness` already requires the pull-request tiers to hold a 1.5x
headroom over their predicted busiest shard, derived from `.test_durations.json`
rather than restated. That rule cannot be applied to this lane as written, and
the reason is the point of this change: **the durations file records Linux
times, and this is the lane that does not run on Linux.** Uncorrected, the rule
says the busiest four-way shard takes 1385s and a 2700s cap is comfortable —
while the shards were really taking upwards of 2400s and crossing it.

Correcting for the platform is what makes the fix obvious. Windows sessions
measured against that 1385s prediction give a factor of **1.81**. At four shards
a 1.5x headroom therefore needs 3763s, which does not fit the 60-minute job at
all: the cap was never the adjustable part. At six shards the corrected
prediction is ~1672s, 1.5x is 2509s, and **the existing 2700s cap already
holds** at 1.61x.

The model is worth trusting here because it was checked: it predicts a busiest
Windows shard of 2507s and the three measured sessions average 2509s.

## What Changes

- The lane splits six ways instead of four. `--session-timeout` stays at 2700s,
  because with six shards it is already correct — the defect was the split, not
  the timeout.
- `install-readiness` gains the headroom requirement for this lane, stated with
  the platform correction, plus the rule that a split count which cannot hold
  the headroom inside the job deadline is fixed by splitting rather than by
  extending the bound past it.
- A second requirement, and a test, for the trap found while writing this: a
  `#` inside a folded `run: >-` scalar is part of the command and comments out
  every flag after it. The first attempt at documenting a value inline did
  exactly that and would have silently disabled `--session-timeout`,
  `--durations` and `--junitxml` for the whole matrix.

## Capabilities

### Modified Capabilities

- `install-readiness` — the cross-platform lane gets a headroom guarantee stated
  in terms of the platform it runs on.

## Impact

- `.github/workflows/cross-platform.yml` — split count, shard matrix, job name,
  and the explanation moved outside the folded scalar.
- `tests/test_ci_reliability_contract.py` — two tests, both shown to fail
  against the previous state.
- Runner cost: two extra runners per platform per night. This lane has been
  nightly and manual only since `e85a88c1 chore(ci): move expensive gates off
  every PR (#787)`, so the spend is once a night rather than per pull request,
  which is what that change was protecting.
- No source change, no tool-surface movement.

## What this does not do

It does not make the suite faster, and it does not address the failures the lane
is now free to show. With the clock noise gone, the nightly Windows shards still
carry real failures concentrated in `tests/test_governance_active_tuple.py` from
the v4 catalog wave (#800–#818). Those are a separate finding and a separate
change.
