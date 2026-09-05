## Why

The frozen stance verifier has a safe admission boundary but no admitted model, and
its untested v1 label map asks ordinary NLI to distinguish semantic relatedness from
neutrality. Real checkpoints correctly refuse that contract. We should admit a real,
multilingual verifier only after making its identity reproducible, its labels truthful,
and its Hosted cost boundary explicit.

## What Changes

- Admit one reviewed multilingual NLI checkpoint at an exact upstream revision and
  content-bound artifact manifest; runtime configuration still cannot select a model.
- Replace the unadmitted v1 aggregation with a fixture-verified v2 relation map:
  symmetric contradiction, mutual entailment (`duplicate`), one-way entailment
  (`refine`), and otherwise `neutral`.
- **BREAKING**: the admitted label vocabulary uses `neutral`, not `unrelated`;
  NLI neutrality does not prove that two claims are topically unrelated. No real v1
  pin has shipped, so this corrects the public contract before model labels exist.
- Expand admission fixtures beyond English to same-language and mixed-language pairs,
  while documenting that verified fixture coverage is narrower than the model card's
  broad language claim.
- Pin the upstream revision and exact files as well as their digest, so a cache with
  extra files or revisions cannot change what is verified or loaded.
- Add a real-model CI lane that resolves the exact revision and proves the production
  admission path and multilingual fixtures, alongside the existing injected tests.
- Retire the production-dead `EXOMEM_CLAIM_POLARITY_MAX_PAIRS` knob; the asynchronous
  queue remains bounded by `EXOMEM_CONTRADICTION_TOP_N`.
- Keep Hosted activation withheld. The verifier remains default-off, soft-failing,
  local queue enrichment; a Hosted grant or image payload requires separate measured
  memory/latency/capacity evidence. The rejected compact and quantized candidates are
  not admitted merely because they fit the cell envelope.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `frozen-verifiers`: Require an exact revision/file manifest, truthful v2 NLI
  semantics, multilingual real-model fixtures, and reproducible real-model admission.
- `contradiction-queue`: Change the admitted queue label vocabulary from `unrelated`
  to `neutral` and preserve provenance-only asynchronous enrichment.
- `hosted-tenant-cell`: Make explicit that no Hosted verifier capability or model
  payload is granted until its resource envelope is measured and admitted.

## Impact

The change affects `src/exomem/claims.py`, verifier diagnostics, contradiction-queue
rendering, the optional `nli` dependency path, frozen-verifier/doctor/claims tests,
real-model CI, and the Hosted inference boundary documentation. It does not change
write behavior, ranking, retrieval, authority, Planning/Records workflows, or Hosted
runtime grants and images.
