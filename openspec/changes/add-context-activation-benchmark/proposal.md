# Proposal: add-context-activation-benchmark

## Why

The context compiler (`add-context-activation`) is only worth shipping if it makes a
fresh agent behave better because the right durable knowledge arrived at the right
time, not because recall rank improved. That claim has to be falsifiable before the
mechanism exists, on cases that can make it fail: vague turns with frequency-matched
negative twins, turns where no memory is relevant, superseded knowledge, resource state
that makes the obvious recommendation impossible, ambiguous domains and distractor
overload. The repository already has the substrate for this — the paired utility
episodes of `epistemic-utility-regression` (released family `f32`) and the real-agent
replay journey — so the benchmark is pre-registered here as a bounded instrument on
that substrate rather than a new amendment family that would be withheld until
acknowledged.

## What Changes

The `close-memory-loop` programme makes product-shaped deterministic capture/index/compiler acceptance and observed ordinary-use evidence the required delivery path. Paid multi-arm comparisons remain available, explicitly deferred and separately opt-in. Passing scorer/oracle-packet tests alone does not establish real compiler acceptance; the corpus must use canonical entity, hub, Records and Planning shapes through normal writers and publication.

Derived current-state references retain their own scoring identity and earn relevance
through frozen canonical-source bindings. Subscription agent runs preserve usage
telemetry separately from metered API charges or optional price estimates.

- A **deterministic activation audit** instrument: a seeded synthetic corpus shaped
  like nine real cold-start cases (C1–C9) with nine negative twins (T1–T9), gold and
  poison anchor lists authored before any retrieval runs, a model-free scorer reporting
  per-case, per-anchor-kind activation recall and precision, `resolved` false
  activation on twins, `partial`/`ambiguous` twin rates, abstention correctness,
  supersession marking, packet tokens and latency — each with its dual, no aggregate,
  and a mechanism-removal test. It runs in CI, publishes no comparative claim and
  carries no epistemic-bench row.
- **Agent-in-the-loop variants** under the released `f32 utility_action_episode`
  family (a code-tuple extension, no new receipt): arms A1 control (Exomem absent), A2
  raw recall, A3 compiler, A4 nudged recall (search-first instruction) and A5 oracle
  packet (instrument-only ceiling), graded by observed environment state, with a
  reminder-turn test, blind fact extraction intersected model-free with gold/poison, a
  per-case usefulness rubric blind to arm, and counted recovery searches, injected
  tokens, latency and cost.
- **Pre-registered thresholds, stopping criteria and run protocol** (n = 1 for
  pre-implementation baselines, n = 5 for the A3 comparison; rotated arm order; harness
  faults blocked, never scored; hard cost cap), frozen in this change with digests that
  every run manifest must carry.
- A **private real-vault instrument**: the same scorer over a digest-pinned local
  snapshot of the owner's vault (meta-notes about the benchmark excluded), run locally
  on a quiesced cell with nonces; results are preserved in Exomem as Evidence and never
  committed.
- An optional comparative runbook that measures the naive path, the A5 ceilings, the A1 floor and
  the A2/A4 arms before the compiler exists, so the compiler has a number to beat.

## Capabilities

### New Capabilities
- `context-activation-benchmark`: the deterministic activation audit instrument, its
  fixtures, scorer, thresholds, stopping criteria, run protocol and the private
  real-vault instrument.

### Modified Capabilities
- `epistemic-utility-regression`: adds the context-activation variants and arms under
  the released utility family, keeping its paired, adverse-inclusive, opt-in-paid rules.

## Impact

- New fixtures and scorer under `benchmarks/membench/utility/` (variants, oracle
  packets, gold/poison manifests) and a synthetic corpus generator beside
  `benchmarks/epistemic/corpora/`; CI test for the deterministic layer; a runbook under
  `docs/benchmarks/`.
- Fixture generation writes only to isolated synthetic state through product APIs; no live vault writes. `claude -p` replays consume the owner's
  subscription window and are opt-in per the existing paid-probe rule.
- The A3 arm depends on `add-context-activation`; arms A1, A2, A4, A5 and the
  deterministic baseline run without it.
