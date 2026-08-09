# Design — competitive benchmark programme

## Governing decisions

1. **Inverted configuration authorship is the fairness answer.** The rescope
   retired comparison because the author configured the competitor. This
   programme makes competitor-side configuration competitor-authored:
   guest lanes run their harnesses with their providers; controlled-direct
   rows wrap their provider classes via a sidecar under their own project
   environment; every remaining knob carries a provenance row (their
   file:line or docs URL) and a knob without provenance refuses to run.
   Residual Exomem-authored glue (projectors, scenario drivers) is size-
   accounted in the fairness matrix, and any comparative publication is
   gated on an independent adversarial review of the same shape that
   produced the 2026-08-08 REJECT.
2. **Lane G (guest) is the primary comparative vehicle; MVP if scope
   collapses = guest lanes + epistemic families 5/7/11/12/14.** Guest rows
   carry borrowed neutrality; the custom suites carry the wedge question.
3. **One reader, one judge, one ledger.** MemoryBench contributes
   checkpointed INGEST/SEARCH artifacts only; ANSWER/EVALUATE re-run in the
   direct lane. Its aggregate (MemScore) and its exclusion of failed
   questions from accuracy are thereby never consumed. bm-bench accuracy
   numbers are likewise re-derived; competitor-harness native numbers are
   never republished as ours.
4. **Equivalence gates.** A committed 25-case LongMemEval-S subset
   (3 answerable × 6 types + 7 abstention, hash-ordered selection, recorded
   dataset sha; changes require an OpenSpec task). Twelve diff keys with
   explicit normalizers; `null` never equals a value. Blocking for
   Exomem-direct vs Exomem-in-MemoryBench; report-mode with mandatory
   written explanation across competitor-authored harnesses (two of their
   harnesses may legitimately disagree — that is a finding, not a block).
   Exceptions register: weaker predicate only, evidence-cited, expiring.
5. **Fail-closed readiness with product-honest statuses.** Positive
   verification only (exit-code-0 evidence rejected): Basic Memory =
   vector-chunk count > 0 + config + log line; Supermemory = terminal
   document status plus a memories-mode canary (status `done` never implies
   extraction; irrecoverable failures auto-delete ≈2 min → `reaped_404`);
   Exomem = doctor checks with refusal-not-degradation. Dynamic-dreaming
   Supermemory gets `readiness-unverifiable` as a disclosed first-class
   status — INVALID would make the product's default mode structurally
   unmeasurable, which is its own bias. Near-zero retrieval is a harness
   fault, never a contender loss.
6. **Variants never collapse** (registry-enforced, disclosure text per row):
   `exomem-{source-only,controlled,native}`,
   `basic-memory-{controlled,native-git,native-nogit}`,
   `supermemory-{hosted-memorybench,hosted-native,local-controlled,local-native,local-documents-v3}`,
   `hybrid-rag-control` (RRF k=60 over BM25 + bge-base — the embedding
   family Exomem and local Supermemory already share), `grep-markdown`,
   `no-memory`. Hosted and local Supermemory run different extraction
   models and are never presented as one product. Both our control and
   MemoryBench's own `rag` provider run — their delta inside one harness
   measures harness/prompt effects.
7. **Epistemic State Bench is a new package importing membench as
   libraries** — membench's oracle is the wrong shape (week-keyed bitemporal
   timelines, not phase-keyed trajectories), its contract forbids new
   cross-product families, and its gates read text while this bench reads
   lifecycle state. One trajectory format whose op vocabulary includes
   out-of-band actions (`external_edit`, `stop_engine`, `fresh_agent`,
   `export`, `snapshot`) so corpus-shaped families (1–9) and operational
   families (10–14) share one runner. Coverage subtraction is executed and
   published: families overlapping public suites report state metrics only.
   Assertions run against a neutral state snapshot produced by per-product
   read-only projectors whose every field mapping cites competitor-authored
   evidence; projector size asymmetry is itself a reportable finding.
   Scoring is five-valued; `not_applicable` (capability-declared) poisons
   the family for comparative claims; a marketed-but-missing property is
   `fail` with the marketing citation; acceptance predicates enumerate ≥2
   structurally different ways to satisfy each invariant (Basic Memory may
   satisfy history retention via VCS in a disclosed `native-git` row).
   Catastrophic integrity failures render `INTEGRITY FAIL` and suppress
   every aggregate. The LLM judge is confined to semantic task success and
   continuation narrative, runs in a final phase (deterministic scores are
   byte-identical without it), and is hard-blocked until the structural
   blinding fix passes a structure-swap test; judge–human agreement (κ)
   precedes any judged number.
8. **Pre-registration.** Scenario families, the assertion registry,
   acceptance predicates, and the strategy decision gates are committed and
   content-hashed before any competitor run; the hash lands in every run
   manifest; later changes are dated amendments. Negative controls
   (ripgrep-over-markdown, no-memory) run in every epistemic table so
   totals are interpretable.
9. **Cost-envelope symmetry.** A local metering proxy fronts every
   provider's model endpoint so Supermemory's server-side extraction tokens
   land in the same envelope as Exomem's write-agent tokens. The budget
   ledger uses reservation semantics (refusal happens on the estimate,
   before the call), a cross-process STOP sentinel, priced models only
   (unknown model refuses), founder-approval records in the ledger, and no
   self-raised caps.
10. **Native and operational lanes stay honest about this machine.** No
    cross-provider latency is publishable from the current host (GPU
    unusable; standing 4b.40 policy) — latency renders indicative-only and
    the report refuses comparative latency columns. Providers run strictly
    sequentially under the RAM budget. Blocked rows (hosted keys, cloud
    modes) render as `blocked: <reason>`, never as losses.

## Execution

fable-delegate discipline: Stage-0 packet → Codex/Claude implementer lanes in
isolated worktrees with red-first evidence → fresh independent reviewer over
the actual diff → orchestrator acceptance quoting verdict and diff stat.
Routing per the repository Codex protocol: mechanical, well-specified modules
to Codex lanes; design-sensitive pieces (TypeScript providers, Supermemory
adapter, assertion engine) to Claude executors; adversarial reviews to Sol
xhigh read-only; OpenSpec/docs/KB writes Claude-side. Concurrency capped;
benchmark runs only on a quiesced machine. The red leak test opens the first
implementation lane (observed failing before the normalizer exists).

## Alternatives rejected

- **Keep the retirement absolute** (no competitor rows ever): answers no
  product question; the marginal cost of testing the wedge is now lower
  than the cost of not knowing (rescope already concedes the suite-format
  argument).
- **Extend membench into the epistemic bench**: schema changes would re-pin
  the frozen corpus release, violate the internal-instrument contract, and
  inherit text-reading gates blind to lifecycle state (the 4b.43 lesson).
- **Vendor MemoryBench into this repo**: MIT permits it, but it invites
  drift-by-local-edit and bloats the tree; a pinned sibling checkout with a
  lockfile, hash-verified provider sync, and a single registration patch is
  strictly more auditable.
- **Author competitor adapters directly** (the pre-audit approach): the
  defect class that voided every prior result; excluded by requirement.
