# Design: add-context-activation-benchmark

## Context

Audit of the benchmark substrate at `main` 19762189 (2026-09-16): a new §7 amendment
family is withheld at scenario loading until the founder acknowledges it
(`benchmarks/epistemic/amendments.py:95-107`, `epistemic-state-bench` "Registration is
not release"), and acknowledgment is bound by the byte chain and revision binding in
`benchmarks/protocol/contracts.py:350-366,616-661`, which is why sequences 2, 4 and 5
remain pending. The released `f32 utility_action_episode` family (sequence 6,
acknowledged 2026-09-13) grades by observed environment state, its variants are a
code tuple (`benchmarks/membench/utility/schema.py` `VARIANTS`), and
`benchmarks/epistemic/journeys/f27_replay.py` already drives a real `claude -p` in
isolation with env stripping, strict MCP config and harness-fault handling. The
repository's own worst benchmark-defect class is cases that are unwinnable by
construction; the referent benchmark pinned false resolution at zero
(`openspec/specs/referent-resolution/spec.md:119`). Live reproduction on 2026-09-16
showed the insight note that quotes a fixture turn ranking first for that turn —
fixture contamination is real.

## Goals / Non-Goals

Goals: a benchmark that can falsify the compiler before it exists; per-class results
with duals; zero tolerance for confident false activation; a ceiling arm that strikes
unwinnable cases; a run that is cheap enough to repeat; no private data in the
repository.

Non-goals: a new amendment family in v0; a model judge inside `f32`; coding-task cases
(the adverse coding prior is recorded and kept separate); any aggregate score.

## Decisions

- **D1 — Two layers, no new family.** Layer A (deterministic audit) is an
  unregistered instrument with no claim standing so it can run today; Layer B rides
  `f32` as new variants (code tuple) so it runs and scores today. Judged conversational
  cases (C1, C7) are labelled findings, never claims, per the f27 precedent. A sequence-7
  amendment converting findings into claims may be filed later and is expected to sit
  pending.
- **D2 — Five arms.** A4 (nudged recall) is mandatory: without it any A3 gain is
  attributable to "we told it to look". A5 (oracle packet) is mandatory: a case A5
  cannot win against A1 is void and is never scored against the compiler; A5 is
  instrument evidence, never product performance.
- **D3 — Fixtures.** C1 AI-usage complaint (gold: the AI subscriptions collection, the
  weekly-limit insight, the capacity-ceilings pattern); C2 "planning to cook this"
  (grill/equipment page and cooking-method insights; twin: photographing); C3 an
  Exomem implementation turn worded to avoid self-contamination (Planning items +
  OpenSpec pointer; twin: the same shape for another project's Planning collection); C4
  named/unnamed person (entity + failure notes; twins: no name, shared first name →
  ambiguous); C5 resource whose latest Records state makes it unavailable (twin: an
  available resource must not be qualified); C6 no-memory turn (0 tokens; twin: a
  unit-conversion turn that does carry a domain cue); C7 ambiguous domain ("AI search";
  twin: a scoped variant that resolves); C8 supersession chain (active head + marked
  ancestors; twin: unchained active note → no marking); C9 C2's own turn scored on a
  tree padded with ~200 adjacent evidence/transcript pages (twin: C2's twin's own
  turn, on that same padded tree — an ordinary negative control again; padding
  robustness compares C9 against C2's own unpadded-tree score, round-two revision).
  The synthetic corpus mirrors
  these shapes with generated names; the private instrument uses the real pages.
- **D4 — Scoring.** Deterministic layer: per case × anchor kind, recall/precision with
  poison, twin false activation split by status, abstention, supersession, tokens,
  latency; duals always; mechanism-removal test. Agent layer: reminder test as primary
  (deterministic, no grader), blind extraction + model-free intersection, blind rubric,
  counted costs. No aggregate.
- **D5 — Thresholds and stopping criteria** as pinned in the spec; the latency bound
  is replaced by a measured constant from the naive-path baseline before freezing.
- **D6 — Protocol.** Reuse `f27_replay.py` wholesale; pin model, provider, effort, CLI
  and exomem versions, prominence, corpus and fixture digests on the manifest; n = 1
  for pre-implementation baselines, n = 5 for the A3 comparison; rotated arm order; the
  same person authors fixtures and gold but does not grade; hard cost cap with
  per-episode reservation.
- **D7 — Privacy.** CI uses only the synthetic corpus; the real-vault snapshot is local,
  digest-pinned, contamination-filtered, and its report is preserved as Evidence in the
  owner's knowledge base.
- **D8 — Order of measurement.** Naive-path latency → A5 ceilings (strike unwinnable
  cases) → A1 floor → A2/A4 (if A4 already clears the bar, that is the cheapest
  falsification) → deterministic baseline from `ask_memory` output labelled against
  gold/poison.

## Risks / Trade-offs

- The variant loophole: fixtures added under a released family could enter a scored
  table without a receipt; mitigated by freezing digests in this change and voiding
  manifests without them.
- Agent variance dwarfs single readings (measured in the no-nudge dev runs); mitigated
  by n = 5 with individual and modal outcomes and by refusing means across cases.
- The premise correction about receipt ordering is inferred from the spec and call
  sites, not reproduced; a probe (acknowledge a copy of the sequence-2 receipt naming
  its introduction commit) precedes any reliance on it.

## Migration Plan

Additive: new fixtures, scorer, variants and a runbook. No product code, no vault
writes. Existing variants and receipts are untouched, asserted by a byte-identity test.

## Open Questions

- Whether a sequence-7 amendment should be filed in parallel to give the judged cases
  claim standing later (expected to sit pending).
- The exact wording of C3 that avoids contaminating the Planning collection with the
  benchmark's own vocabulary.
