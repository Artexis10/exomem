# Add the competitive benchmark programme: guest lanes, protocol substrate, and epistemic proof

## Why

Every historical Exomem-vs-competitor figure is void or withdrawn. The
2026-08-08 independent audit rejected the in-house head-to-head because its
author configured the competitor — a defect class that recurred across two
repair rounds and flipped direction each time — and
`rescope-benchmark-instrument-and-public-suite` retired self-authored
comparison permanently. That retirement protects credibility but cannot
answer the product question this programme exists to answer: where Exomem is
below parity, at parity, or genuinely differentiated — and whether the
user-owned epistemic-workspace thesis survives adversarial comparison against
Supermemory (the automatic context/memory engine) and Basic Memory (the
owned-Markdown simplicity competitor).

The 2026-08-09 audit (`docs/benchmarks/competitive-eval-audit-2026-08.md`)
found the structural opening: **both competitors ship their own benchmark
harnesses.** Basic Memory's `bm-bench` carries providers for itself, Mem0,
Zep, two negative-control baselines, and a Basic-Memory-authored Supermemory
provider; an Exomem guest provider already exists in a fork. Supermemory's
MemoryBench (MIT) takes guest providers by design. Comparison is therefore
possible without this repository ever configuring a competitor: Exomem enters
their harnesses as a guest, and the residue this repository must still author
is minimized, provenance-cited, pre-registered, and externally reviewed.

The audit also found three defects that make the substrate lane urgent rather
than hygienic: the existing direct LongMemEval lane leaks gold-evidence
labels into provider state on real data (`answer_`-prefixed session IDs and
`question_type` tags — undetectable by any committed fixture); the three
harnesses pin two different dataset variants; and bm-bench's Supermemory row
is a document-RAG row mislabeled as a memory row.

## What Changes

- **New capability `benchmark-protocol`**: a provider-neutral substrate under
  `benchmarks/protocol/` — canonical events with structural gold quarantine,
  three-scope outbound leakage scanning, per-case namespace isolation with
  canary probes, fail-closed readiness plus known-answer probes, manifests
  written before the first provider call, trace-complete regenerable reports
  with an offline guard, a reservation-based budget ledger shared across
  Python and TypeScript, and a never-collapse provider-variant registry.
- **New guest lanes** (`benchmarks/bmbench/`, `benchmarks/memorybench/`):
  Exomem as a guest provider inside pinned checkouts of the competitors' own
  harnesses; adversarial audits of their provider configurations as
  deliverable artifacts; exporters into the protocol schema; a committed
  25-case equivalence gate (blocking for Exomem's paths, report-mode across
  competitor harnesses); upstream-PR wedge for the Basic Memory provider and
  an upstream issue (never a local fix) for defects found in their
  Supermemory provider.
- **Direct lane extension** (`benchmarks/lme/`): the gold-label leak fixed
  red-first; provider-pluggable runners where competitor providers are
  competitor-authored classes wrapped via a sidecar, each configuration value
  provenance-cited; a transparent hybrid-RAG control; the exomem-only
  official row unchanged.
- **New capability `epistemic-state-bench`** (`benchmarks/epistemic/`):
  fourteen pre-registered state-trajectory scenario families with registered
  deterministic assertions over neutral state snapshots, five-valued scoring
  with N/A-poisoning, catastrophic integrity failures that suppress
  aggregates, per-scenario fairness packets, negative controls, and judge
  use hard-gated behind a structural-blinding fix.
- **New capability `native-lifecycle-bench`** (`benchmarks/native/`): eight
  journeys in controlled and native modes with declared asymmetries, a
  future-blind write agent under an explicit budget envelope, a fresh answer
  agent with own-declared citations, and metered competitor extraction so
  cost envelopes are symmetric.
- **New capability `operational-quality-bench`** (`benchmarks/opsq/`):
  automated install/readiness/recovery/freshness/cost measurements, fault
  injection for failure transparency, labeled heuristics, no aggregates.
- **New capability `benchmark-fairness-contract`**: the cross-cutting rules —
  configuration provenance, glue disclosure with size accounting, harness
  fault never a contender loss, historical-untrusted refusal, and a
  mandatory independent adversarial review before any comparative
  publication.
- **MODIFIED `public-suite-eval`**: "External Suite Evaluation Is
  Exomem-Only" becomes "Competitor Rows Come From Competitor-Authored
  Configuration" — the official exomem-only published row survives
  unchanged; competitor rows become possible strictly under the fairness
  contract. All other `public-suite-eval` requirements stay as shipped.
- **Reports** (`benchmarks/reports/`): per-ability × per-variant rendering,
  fairness and compatibility matrices, an auto-generated adversarial-review
  packet, artifact-only consolidation. One additive CI job
  (`benchmark-protocol`, offline-only); the guarded `retrieval-eval` job is
  untouched.
- **Strategy deliverable**: `docs/strategy/exomem-competitive-strategy-2026-08.md`
  with pre-registered continue/narrow/stop decision gates and evidence slots
  wired to lane outputs.

## Capabilities

### New Capabilities
- `benchmark-protocol`
- `epistemic-state-bench`
- `native-lifecycle-bench`
- `operational-quality-bench`
- `benchmark-fairness-contract`

### Modified Capabilities
- `public-suite-eval`: the exomem-only restriction is narrowed to the
  official published row; competitor rows are admitted only under
  competitor-authored configuration, provenance, paired-row publication, and
  external adversarial review.

## Impact

- Additive within `benchmarks/`, `docs/`, `openspec/`, `tests/`, plus one
  additive CI job. No product-runtime changes. `benchmarks/membench/` is not
  modified (imported as libraries); `benchmarks/lme/` changes are limited to
  the named leak fix and provider-pluggable extension.
- Guarded paths unchanged: `tests/golden/`, `tests/test_latency_gate.py`,
  `tests/test_retrieval_golden.py`, `src/exomem/**` (benchmark code never
  mentions competitors there). All committed artifacts pass the repository
  privacy gate — no absolute local paths, no personal tokens.
- Sibling checkouts stay read-only; the Basic Memory fork work happens on a
  dedicated fork branch; run artifacts remain gitignored; historical run
  directories are labelled `historical-untrusted` and refused by the new
  renderer.
- Spend: metered API use stays founder-gated per `public-suite-eval` (a
  ≤$25 session cap is recorded for the current validation window);
  subscription-billed runners remain the default; the budget ledger stops
  before spend, and a run cannot raise its own cap.
