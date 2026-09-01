## Context

The previous change built a sound verifier boundary but intentionally shipped an
empty pin registry. Running three English checkpoints and two multilingual
checkpoints against its nine fixtures exposed two distinct issues:

- label map v1 treats a one-way entailment as `unrelated` because mean neutrality
  is tested before asymmetric entailment; and
- ordinary NLI cannot distinguish topically unrelated text from compatible but
  non-entailing evidence. Calling either state `unrelated` overstates the model.

The selected candidate is
`MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7` at revision
`b5113eb38ab63efdd7f280f8c144ea8b13f978ce` (MIT). It was trained on XNLI plus a
27-language NLI corpus that includes mixed English/non-English pairs. The model
card's broader language reach is useful prior evidence, not Exomem's acceptance
claim; Exomem claims only the checked fixture set.

Hosted cells are a separate constraint. The selected safetensors checkpoint is
about 532 MiB before runtime overhead. The upstream full ONNX export is about
1,064 MiB, while its 323 MiB quantized export failed a genuine-contradiction
fixture. Adding Torch or a red quantization to every memory-capped tenant cell is
not an acceptable way to make the feature look Hosted-ready.

## Goals / Non-Goals

**Goals:**

- admit one exact real checkpoint through the production admission path;
- make the logit-to-product-label contract faithful to NLI semantics;
- verify English, non-English, and mixed-language pairs against the real bytes;
- make cache identity reproducible across machines and independent of unrelated
  cached files;
- keep default-off, soft-fail, queue-only, pure-substrate behavior unchanged;
- add real-model CI and retire the dead pair-limit knob; and
- leave a hard, testable Hosted non-activation boundary.

**Non-Goals:**

- claiming equal quality across every language named by the model card;
- detecting the language of a note or refusing unlisted languages;
- changing semantic retrieval's English BGE model;
- using verifier output to select, rank, create, suppress, or resolve queue items;
- enabling the verifier in Hosted, adding its weights to the Hosted image, or
  designing a cross-tenant inference service; and
- changing Planning, Records, workflow contracts, or OpenSpec integration.

## Decisions

### D1. Label map v2 represents NLI relations, not topical relatedness

The selected head declares columns `(entailment, neutral, contradiction)`. Exomem
runs both text directions and applies the following reviewed map in order:

1. `contradict` when the minimum directional contradiction probability is at
   least `0.93`;
2. `duplicate` when the minimum directional entailment probability is at least
   `0.95`;
3. `refine` when either directional entailment probability is at least `0.95`;
4. `neutral` otherwise.

Contradiction is symmetric, mutual entailment is an equivalence signal, and
one-way entailment is the added-detail shape. The fallback is `neutral`, not
`unrelated`: it includes unrelated text, compatible evidence, and any uncertain
pair. V1 is removed rather than retained as a selectable map because no real pin
ever used it and its public semantics are the defect being corrected.

Alternatives rejected: lowering v1 thresholds until a model passed; treating
high neutrality as `refine`; and combining the NLI output with the existing BGE
cosine. The first two encode false semantics. The last would make a purported
multilingual verifier depend on an English embedding model and a second unpinned
measurement identity.

### D2. The pin names revision and files, not an incidental cache directory

`VerifierPin` gains an exact upstream revision and an ordered artifact-file
manifest. Admission resolves `snapshots/<revision>`, requires every declared
relative file, and hashes only those names and bytes. Extra files and extra
revisions in the cache neither change the digest nor make the pin ambiguous.
Missing, unreadable, or digest-mismatched declared files refuse admission.

The production constructor receives that exact revision directory and remains
`local_files_only=True`. Runtime never fetches. This closes the reproducibility
gap where two users could download different subsets of one Hugging Face repo and
obtain different whole-directory digests despite loading the same weights.

### D3. The real fixture set is multilingual but its claim is bounded

`stance-v2-multilingual` contains the corrected English corpus shapes plus
same-language German, French, and Estonian examples and mixed English/Estonian
pairs. It covers all four v2 labels, reordered restatements, added detail,
negation/concordance that remains honestly neutral, shared-vocabulary neutral
pairs, and cross-language contradiction/equivalence/refinement/neutrality.

Every pair must pass exactly. The fixture metadata records its language shape so
CI and diagnostics can state what was checked. This is admission evidence for
those examples, not a general multilingual benchmark claim.

### D4. Real-model CI exercises the same production admission path

A dedicated workflow runs when verifier code, pin, fixtures, dependencies, or the
workflow itself changes, and on schedule/manual dispatch. It caches Hugging Face
artifacts, explicitly downloads only the pin's files at the pin's revision,
installs the `nli` extra, enables the gate, and asserts that
`verifier_admission()` is admitted before running the real fixture test.
Ordinary CI keeps the dependency-absent and injected-predictor tests.

The download step is CI setup, not runtime behavior. A changed model, revision,
file manifest, digest, map, or fixture set cannot pass by updating only a fake.

### D5. Hosted activation remains withheld

This change adds neither the `nli` extra nor model artifacts to the Hosted image,
sets no Hosted gate, and grants no verifier capability. The existing Hosted
optional-compute rule applies: activation needs a trusted capability grant plus
measured peak RSS, latency, concurrent-cell capacity, idle reclamation, image
size, and failure isolation on the actual node/runtime.

The rejected upstream quantized ONNX artifact is recorded as evidence, not used.
A future Hosted proposal may produce a better calibrated quantization, a bounded
per-cell worker, or another isolated serving shape, but it must pass the exact
fixture set and the Hosted capacity gate before activation.

### D6. The queue cap has one owner

`EXOMEM_CLAIM_POLARITY_MAX_PAIRS` and `_max_polarity_pairs()` are removed. Their
only production caller disappeared with synchronous polarity. The asynchronous
enrichment already operates after `EXOMEM_CONTRADICTION_TOP_N` caps the surfaced
queue, so a second dead cap would only mislead operators.

## Risks / Trade-offs

- **A broad multilingual model is still uneven across languages.** → Claim only
  fixture-backed coverage, keep labels advisory, and preserve soft failure.
- **A 532 MiB optional model is heavy locally.** → Keep the extra and gate
  default-off, load lazily, and run only over the capped asynchronous queue.
- **A conservative symmetric contradiction threshold can miss conflicts.** →
  Prefer absence/neutrality to false contradiction; no label affects surfacing or
  rank, and future map changes require a new version plus re-verification.
- **Upstream cache layout could change.** → Bind exact revision and relative file
  manifest; refusal is safer than guessing another path.
- **A real-model workflow consumes bandwidth.** → Use a revision-keyed cache and a
  path-scoped dedicated workflow while retaining scheduled verification.

## Migration Plan

1. Land v2, the exact pin, and the real fixtures while the gate remains default-off.
2. Run ordinary dependency-absent tests and the real-model lane against the exact
   revision.
3. Remove the dead pair-limit knob and document `EXOMEM_CONTRADICTION_TOP_N` as the
   only queue bound.
4. Confirm the Hosted image and rendered cell environment still contain no verifier
   grant, gate, dependency, or weight payload.
5. Rollback is code-only: removing the pin returns every runtime to `no-pin`; no
   vault or sidecar migration is involved.

## Open Questions

None block local admission. Hosted activation remains a separate, evidence-gated
future change rather than an open implementation choice in this one.
