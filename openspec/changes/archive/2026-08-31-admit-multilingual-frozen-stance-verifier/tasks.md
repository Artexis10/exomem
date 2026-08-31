## 1. Truthful NLI relation map

- [x] 1.1 Add red unit tests for v2 column order, symmetric contradiction,
  mutual-entailment duplicate, one-way-entailment refine, neutral fallback,
  threshold edges, invalid shapes, and non-finite refusal.
- [x] 1.2 Implement label map v2 and the closed
  `contradict`/`duplicate`/`refine`/`neutral` vocabulary; remove the unadmitted v1
  map and update rendered queue semantics.

## 2. Reproducible artifact identity

- [x] 2.1 Add red tests that a pin requires an exact revision and artifact
  manifest, ignores extra cache files/revisions, refuses a missing declared file,
  and loads only the exact pinned revision locally.
- [x] 2.2 Extend `VerifierPin`, hashing, admission, diagnostics, and the model loader
  to bind the exact revision plus declared-file digest without any runtime fetch.

## 3. Real multilingual admission

- [x] 3.1 Add the `stance-v2-multilingual` fixture set with English, German,
  French, Estonian, and mixed English/Estonian pairs covering every v2 label and
  record each fixture's language shape.
- [x] 3.2 Add the exact reviewed multilingual checkpoint pin and prove its
  artifact manifest digest against revision
  `b5113eb38ab63efdd7f280f8c144ea8b13f978ce`.
- [x] 3.3 Add a real-model test that enables the production gate and requires the
  exact pinned bytes to admit and pass every multilingual fixture.

## 4. Queue and knob cleanup

- [x] 4.1 Update contradiction-queue tests and documentation so `neutral` never
  renders as or implies `unrelated`, while provenance, ranking, signal version,
  write-path absence, and failure isolation remain unchanged.
- [x] 4.2 Remove production-dead `EXOMEM_CLAIM_POLARITY_MAX_PAIRS`, its helper and
  tests; retain `EXOMEM_CONTRADICTION_TOP_N` as the sole surfaced/enriched bound.

## 5. CI and Hosted boundary

- [x] 5.1 Add a path-scoped real-model workflow with revision-keyed Hugging Face
  caching, exact-file download, the `nli` extra, production admission, and the real
  multilingual fixture test.
- [x] 5.2 Add regression coverage proving the Hosted image/config receives no
  verifier extra, artifact, grant, or gate in this change, and update the Hosted
  inference boundary with the measured rejection of the available quantized
  candidate.

## 6. Verification and closure

- [x] 6.1 Run focused verifier, claims, doctor, queue, dependency-absent, real-model,
  Hosted-boundary, and lint checks; record red-first and green evidence.
- [x] 6.2 Run the completion-boundary full suite, strict OpenSpec validation, and
  attribution against the current `origin/main` baseline for any unrelated red.
- [x] 6.3 Sync the three delta specs, archive the completed change through
  `openspec archive`, and re-run strict validation before delivery.
