## Context

Two caps bound a matrix lane and they do different jobs. `--session-timeout`
asks pytest to stop between test items, so the lane still prints a summary, the
slowest-test durations, and a JUnit file. `timeout-minutes` is the runner killing
the process, which produces none of that. The session bound therefore has to stay
under the job deadline — that gap is the diagnosis.

`install-readiness` already pins the pull-request tiers at 1.5x their predicted
busiest shard, derived from `.test_durations.json` by running pytest-split's
`least_duration` over the recorded node times. That rule is sound where the
prediction and the runtime come from the same platform. This lane is the one
place they do not.

## Goals / Non-Goals

**Goals.** A healthy shard on the slowest platform finishes without the cap
firing. A genuine hang is still caught by pytest rather than the runner. The
relationship is pinned so the margin cannot silently go negative again.

**Non-Goals.** Making the suite faster. Changing the pull-request tiers, which
are measured healthy. Fixing the real Windows failures this lane reports once the
clock noise is gone.

## Decisions

**Correct the prediction, do not abandon it.** The alternative was to pin the
observed runtime as a constant, which is what the first draft of this change did
(`the cap must be at least 46 minutes, because a shard was measured at 45:44`).
That restates an observation and goes stale silently. Deriving from the durations
file and applying one measured platform factor keeps the canonical input
canonical, and the factor is the only thing that has to be measured because it is
a property of the runners rather than of the tests.

**The factor is 1.81 and it is checked, not asserted.** Three Windows sessions —
2481s, 2449s, 2595s — against a predicted busiest four-way shard of 1385s. The
corrected model then predicts 2507s for that same configuration against a
measured mean of 2509s, so the correction is doing real work rather than
absorbing the error into a fudge.

**Six shards, not a longer cap.** At four, 1.5x of the corrected prediction is
3763s against a 60-minute job: there is no timeout that satisfies the rule, so
the split is the variable. At six it is 2509s and the existing 2700s cap holds at
1.61x. The test asserts this directly — a split count that cannot hold the
headroom inside the job deadline fails with "this lane needs more shards, not a
longer timeout", so the next person meets the real choice instead of raising a
number until it goes green.

**Guard the folded scalar.** The first attempt put the explanation inline, and
`yaml.safe_load` showed the `#` landing inside the command string, where folding
would have commented out `--session-timeout`, `--durations`, `--durations-min`
and `--junitxml` for every shard on every platform. The test reads the raw file
rather than the parsed tree, because `safe_load` discards the scalar style that
is the entire distinction: a `#` is a shell comment in a literal `|` block and a
command-killer in a folded `>` one. Existing literal-block steps use it correctly
and must not be flagged.

## Risks / Trade-offs

**Two more runners per platform, per night.** Accepted because the lane is
nightly and manual only since #787; that change was protecting the per-PR
allowance, which this does not touch.

**The factor will drift.** It is a property of the runner images and of how much
of the suite is I/O bound, and nothing recomputes it. It is written down with its
measurements so the next person can redo the division rather than guess, and the
headroom assertion fails if it drifts far enough to matter.

**A real hang is still bounded at 45 minutes rather than the job's 60.** That is
deliberate and unchanged.
