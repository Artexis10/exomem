# Rescope the benchmark: internal instrument + borrowed-neutrality public suite

## Why

On 2026-08-08 an independent adversarial audit (commissioned by this project,
run read-only by an outside-model critic with no stake) returned **REJECT:
every reported dimension of the Track B head-to-head is unsafe** — six
critical and six major findings. Both headline results are withdrawn: the
original "exomem substantially ahead of basic-memory" (the harness never
built the competitor's vector index — ledger 4b.41) and the corrected
"basic-memory at parity or ahead" (the harness's own renderer fed
basic-memory 376 oracle-normalised fact lines phrased in the query
vocabulary, among five further critical defects pointing both ways). No
publishable product comparison exists, in either direction.

The decisive observation is structural, not a defect count: **every
competitor number requires this project to author that competitor's
renderer, adapter, and capability profile, and that class of defect recurred
in both directions across two full repair rounds.** A benchmark's author
cannot certify its own fairness; residual bias runs toward the author; and
the external governance an "industry standard" claim requires — outside
ownership of taxonomy, renderer approval, release sign-off — has no owner
and none should be assumed. Meanwhile the bespoke suite proved blind on the
one dimension it uniquely claimed: repairing compiled supersession took the
vault from 0 to 37 superseded conclusions and moved the score by a single
row (4b.43), because every gate reads retrieved text and declared citations,
never lifecycle state.

This change therefore retires the cross-product comparison ambition
permanently, narrows Track B to an internal instrument, and obtains the
public number from an external suite instead — exomem-only, beside numbers
other vendors published for themselves.

Relation to existing changes: **supersedes the cross-product comparison
scope** of `add-memory-proof-benchmark` and `expand-memory-proof-benchmark`
(their corpus, oracle, gate, and reference-bound machinery remain live under
the internal-instrument contract; their packaging/industry-standard framing
does not). **Reverses the archived decision** of
`2026-07-03-publish-retrieval-benchmark` (design.md: "No adoption of a
third-party benchmark harness (e.g. LongMemEval)") and the 2026-08-06
"industry standard v0.2" direction-lock: both rested on the premise that the
in-house suite could carry neutral comparison, which is the premise the
audit destroyed. Two of the audit's critical findings argue for the format
swap on technical grounds: external-suite session timestamps live in
ingested content (neutralising the knowledge-time-never-transmitted defect),
and LLM-judged natural-language answers force a real answerer (eliminating
the harness-authored abstention column).

## What Changes

- **New capability `public-suite-eval`**: an exomem-only evaluation lane for
  LongMemEval-S (cleaned variant; MIT; official judge protocol unmodified),
  under `benchmarks/lme/` — a new namespace that does not modify
  `benchmarks/membench/`. Reuses the proven seams: the adapter contract and
  `exomem_local` product-surface core (minus governance translation), the
  `judge/` backends for the reader seam, `environment.py` capture, and the
  run-directory discipline. Re-derives cheap bounds (gold-evidence ceiling
  from `answer_session_ids`, null-abstain floor). Pilot-first: a stratified
  20-question pilot with measured wall-time and API cost precedes any full
  run; metered API spend and the full-500 run each require explicit founder
  approval.
- **Publication policy (harness)**: no cross-contender comparative table
  authored in this repository is publishable; competitor integrations are
  never authored here again, for any suite. Published comparisons place
  exomem's externally-judged number beside figures published elsewhere by
  their owners, cited as such. Latency and harness-mode abstention columns
  are withheld permanently (structurally incomparable / answerer-authored).
- **Internal-instrument contract (Track B)**: purpose narrows to product
  regression and compiled-path correctness against real product surfaces,
  bounded by the oracle-retrieval ceiling and null floor. No new scenario
  families without a stated internal question. Release-byte pinning,
  provider onboarding, and judge–human agreement packaging remain stood
  down. Tracks C and D (corpus-blind, exomem-specific) become the
  single-product capability evidence, published as documentation, never as
  a leaderboard.
- **Docs**: every cross-contender figure in `docs/memory-proof-benchmark*.md`
  is marked WITHDRAWN with the audit rationale; the defect ledger's
  comparison-only items (4b.31, 4b.33, 4b.40, the cross-contender half of
  4b.29) are closed as retired-with-rationale; truth-labelling guards
  (4b.26, 4b.30) and instrument-correctness items (4b.44, 4b.34, 4b.28)
  stay open and routed.

## Capabilities

### New Capabilities
- `public-suite-eval`: exomem-only external-suite evaluation (LongMemEval-S
  cleaned first; MemoryAgentBench's conflict-resolution track is a watch
  item for a second adapter), with official-protocol fidelity, bounds,
  product-default retrieval, artifact preservation, and pilot/spend gates.

### Modified Capabilities
- `memory-proof-harness`: additive narrowing — cross-contender comparison
  retirement, permanently withheld columns, and the internal-instrument
  contract. (Additive requirements; the underlying v0.1/v0.2 requirements
  remain as shipped, with their comparison-publication ambition superseded
  by this change's policy requirements.)

## Impact

- All additive within `benchmarks/`, `docs/`, `openspec/`, and
  `tests/test_lme_*.py`; no product-runtime changes. `benchmarks/membench/`
  is not modified by the new lane (the compile-plan ordering fix 4b.44 and
  Track D clock injection 4b.34 land as their own ledgered tasks).
- The public number's credibility shifts from self-certified fairness (now
  known unreachable) to borrowed neutrality: an external corpus, an external
  judge, and competitor figures owned by their publishers.
- Guarded paths unchanged: `tests/golden/`, `tests/test_latency_gate.py`,
  `tests/test_retrieval_golden.py`, `.github/`, `src/exomem/**` untouched
  by benchmark code. No latency claims from any run on the current dev
  machine (GPU unusable for torch under WSL).
