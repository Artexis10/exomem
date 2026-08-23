# Proposal: bound-cross-platform-session

## Why

`main`'s cross-platform matrix reported Windows failures that contained no
failing test. The log reads:

```
2929 passed, 226 skipped, 9778 deselected, 3 warnings in 2702.40s (0:45:02)
##[error]Process completed with exit code 1.
```

That is `--session-timeout=2700` firing. Measured across two runs on `main`, the
four Windows shards took 38:40, 38:57, 42:58 and 45:44 — every one of them
against a 45:00 cap, inside a job that allows 60 minutes. The cap had drifted
below normal runtime instead of above it, so whichever shard drew the slowest
runner reported a green suite as a red lane. Between zero and two Windows shards
failed this way per run, which is exactly the shape of a variance crossing a
threshold rather than a defect.

`install-readiness` already requires the *lean* lane to be time-bounded and
diagnosable, and that lane is healthy: 1,500s inside a 30 minute job, with Linux
shards measured at 12:31, 15:40, 17:16 and 15:06. The cross-platform lane has no
equivalent requirement, so nothing noticed when its margin went negative.

## What Changes

- The cross-platform session bound claims the job budget it was leaving unused:
  55 minutes inside a 60 minute job, the same five-minute margin the lean lane
  keeps. It stays below the job deadline deliberately — that is what buys the
  diagnosis, because pytest prints a summary where a killed job prints nothing.
- `install-readiness` gains the requirement that was missing, stated as a
  relationship between the cap, the job deadline and measured runtime rather
  than as a number to restate.
- A second requirement, and a test, for the trap found while writing this: a
  `#` inside a folded `run: >-` scalar is part of the command and comments out
  every flag after it. The first attempt at documenting the new value inline did
  exactly that and would have silently disabled `--session-timeout`,
  `--durations` and `--junitxml` for the whole matrix.

## Capabilities

### Modified Capabilities

- `install-readiness` — the cross-platform lane gets the time-bound guarantee
  the lean lane already has.

## Impact

- `.github/workflows/cross-platform.yml` — one flag value, and the explanation
  moved outside the folded scalar.
- `tests/test_ci_reliability_contract.py` — two tests, both shown to fail
  against the previous state.
- No source change, no tool-surface movement.

## What this does not do

It does not make the suite faster. Windows shards taking 38–46 minutes is the
measurement that motivated this, and raising the cap accepts it rather than
addressing it. `.test_durations.json`, which drives `least_duration` shard
balancing, has not been regenerated since `042d2330 perf(ci): shard the full
test matrix (#425)` and nothing in CI updates it; the shards are nonetheless
close in duration, so rebalancing would not have prevented these failures.
Both are worth their own look and neither is in scope here.
