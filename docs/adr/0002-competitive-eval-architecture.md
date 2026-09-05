<!-- authority:non-specification -->

# ADR 0002 — Competitive evaluation architecture: inverted configuration authorship

Historical status: accepted 2026-08-09. The OpenSpec change
`add-competitive-benchmark-programme` is the current durable authority.

## Context

The 2026-08-08 independent adversarial audit returned REJECT on the in-house
cross-product head-to-head: the recurring, direction-flipping defect class was
"the benchmark's author configures the competitor," and no engineering fix
self-certifies fairness. `rescope-benchmark-instrument-and-public-suite`
responded by retiring self-authored competitor comparison permanently and
moving the public number to an exomem-only external-suite lane beside
competitor-published figures.

The product question that retirement cannot answer: where Exomem is genuinely
below parity, at parity, or differentiated — across standard long-term memory,
native lifecycle quality, epistemic-state integrity, and operational quality —
under a protocol an adversarial reviewer accepts. The audit preceding this ADR
(`docs/benchmarks/competitive-eval-audit-2026-08.md`) additionally found that
both principal competitors ship their own benchmark harnesses, one of which
already contains a competitor-authored provider for the other, and that an
Exomem guest provider already exists inside Basic Memory's harness fork.

## Historical decision record

Four coordinated splits, one fairness inversion:

1. **Official runner (unchanged).** The exomem-only LongMemEval-S lane
   (`public-suite-eval`) remains the flagship published number: official
   dataset, official judge, bounds, no aggregate, beside owners' published
   figures.
2. **Guest lanes (primary comparative vehicle).** Exomem runs as a guest
   provider inside competitor-authored harnesses — Basic Memory's
   `bm-bench` and Supermemory's MemoryBench. The competitor configured the
   competitor and chose the metric; this repository authors only Exomem's own
   integration, the same posture every vendor takes on a public suite.
   MemoryBench supplies checkpointed INGEST and SEARCH stages only; answering
   and judging are re-derived in the direct lane from exported artifacts, so
   one reader, one judge, and one budget ledger cover every path (this also
   neutralizes MemoryBench's aggregate score and its exclusion of failed
   questions from accuracy).
3. **Provider-pluggable direct lane with imported competitor configuration.**
   For controlled rows, competitor providers are not written here — the
   competitor-authored provider classes are wrapped via a subprocess sidecar
   running under their own project environment. Every configuration value
   carries a provenance row (competitor file:line or docs URL); a knob
   without provenance refuses to run; a competitor's controlled number is
   publishable only beside its competitor-harness row. A committed 25-case
   equivalence gate (blocking for Exomem's own two paths, report-mode across
   competitor-authored harnesses) plus per-case diff artifacts tie the lanes
   together.
4. **Custom deterministic suites only where public suites cannot reach.**
   The Epistemic State Bench (state-trajectory scenarios with registered
   deterministic assertions over neutral state snapshots), native-lifecycle
   journeys, and operational measurements are new packages that import
   membench modules as libraries and never modify it; membench itself stays
   the internal instrument under its existing contract.

The fairness inversion, stated once: **who authors competitor-side
configuration moves from this repository to the competitors themselves**, and
whatever residue this repository must still author is minimized, size-
accounted, provenance-cited, pre-registered, and independently adversarially
reviewed before any comparative claim is published. A result showing a
competitor ahead is a valid output of the programme.

## Consequences

- `rescope-…`'s requirement "External Suite Evaluation Is Exomem-Only" is
  MODIFIED (not deleted) by the new change: exomem-only remains the shape of
  the *official published* row; competitor rows become possible strictly
  under the provenance/paired-row/review regime. The retirement's rationale
  is preserved — self-certified fairness stays dead.
- All comparative machinery is additive under `benchmarks/`, `docs/`,
  `openspec/`, `tests/`; product runtime untouched; guarded paths untouched;
  competitor names never appear under `src/exomem/`.
- Historical run artifacts stay `historical-untrusted`; the new report
  renderer refuses them.
- Failure economics change: a broken competitor environment is a harness
  fault that invalidates the run (retrieval-floor guard), never a competitor
  loss — the exact inversion of the defect that produced the void 2026-08
  results.
