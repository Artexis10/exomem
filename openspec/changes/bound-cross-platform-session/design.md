## Context

Two caps bound a matrix lane, and they do different jobs. `--session-timeout`
asks pytest to stop between test items, so the lane still prints a summary, the
slowest-test durations, and a JUnit file. `timeout-minutes` is the runner
killing the process, which produces none of that.

The lean lane encodes the relationship: 1,500s inside 30 minutes. The
cross-platform lane had 2,700s inside 60 minutes — the same idea with a much
larger unused remainder, and no requirement pinning it, so when Windows runtime
grew into the cap nothing objected.

## Goals / Non-Goals

**Goals.** A healthy shard on the slowest platform finishes without the cap
firing. A genuine hang is still caught by pytest rather than the runner. The
relationship is pinned so the margin cannot silently go negative again.

**Non-Goals.** Making the suite faster. Rebalancing shards. Changing the lean
lane, which is measured healthy.

## Decisions

**Claim the budget, keep the margin.** 3,300s inside a 60 minute job leaves the
same five minutes the lean lane leaves. The margin exists for collection,
teardown, and artifact upload, which happen outside the session and still need
to fit.

**Pin the relationship, not the number.** The test asserts three things: the cap
is under the job deadline, the unclaimed remainder is at most five minutes, and
the cap is above the slowest healthy shard yet measured (45:44). A future
slowdown fails that last assertion, which is the signal — restating `3300`
would only prove the file says what the file says.

**Guard the folded scalar.** The first attempt put the explanation inline, and
`yaml.safe_load` showed the `#` landing inside the command string, where folding
would have commented out `--session-timeout`, `--durations`, `--durations-min`
and `--junitxml` for every shard on every platform. The test reads the raw file
rather than the parsed tree, because `safe_load` discards the scalar style that
is the entire distinction: a `#` is a shell comment in a literal `|` block and a
command-killer in a folded `>` one. Five existing literal-block steps use `#`
correctly and must not be flagged.

## Risks / Trade-offs

**A real hang now costs ten more minutes of runner time.** Accepted: the
alternative was failing healthy runs, and the job deadline is unchanged.

**The measured-runtime assertion will need revisiting.** It is written against
45:44, the slowest healthy shard observed. If Windows runtime keeps growing, that
assertion fails and someone has to decide between more shards, a faster suite,
and a longer job — which is the decision this failure mode was hiding.
