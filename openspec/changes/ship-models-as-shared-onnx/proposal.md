## Why

An Exomem Cloud cell runs in 2 CPU and 3 GiB, and its image carries no PyTorch. So a cell runs only one model, the recall encoder. A personal install also runs a reranker, image search, OCR, speech and the optional NLI stance verifier. Each of those must reach Cloud without multiplying memory by the number of cells on a node.

The recall encoder already shows how. It runs as a pinned int8 ONNX artifact, and Cloud cells load it with ONNX Runtime prepacking disabled. A production measurement on 2026-10-09 showed three cells on one node holding a single copy of its weights (design, Context).

Today that is a property of one model. Each new model would otherwise be chosen case by case, and a runtime that copies its weights into private memory would cost a full copy per cell. This change makes the encoder's pattern the rule for every model the Cloud image ships.

## What Changes

- **One runtime rule.** Every model the Cloud image ships is a pinned, pre-baked artifact in ONNX, or in another format whose weights load file-backed. It loads offline, with prepacking disabled on Cloud. So all cells on a node share one read-only copy.
  - It is int8 only where its int8 gate passes. An encoder without a consumer fixture ships at reference precision; a scorer or transducer is gated by its pinned agreement sample.
  - PyTorch is not in the Cloud image. It stays for development and the personal GPU paths.
  - A runtime that copies weights into private memory needs a measured reason.
  - Only immutable model bytes are shared, never writable memory, caches or processes. Local instruments on Cloud run per cell on the shared weights. So tenant isolation is unchanged.
- **A parity gate.** An artifact ships only after it matches its reference:
  - an encoder at its reference precision, at a cosine of at least 0.9999 (the existing `MIN_COSINE`);
  - a quantised encoder, when its consumers' fixture verdicts are unchanged;
  - a scorer or transducer, at an agreement bound stated before the measurement;
  - an instrument, only through its existing fixture admission, at its new pin.
- **A recorded identity.** Each artifact records its model, revision, quantisation, format and digest. An artifact that Exomem builds also records its conversion recipe and version, and is published immutably.
  - A host fetches a built artifact by digest from that publication. A local rebuild never substitutes for it, and a host without the published bytes refuses that model until it fetches them.
  - A same-precision substitution that passes the parity bound keeps an encoder's vector space and the values calibrated on it; the recall encoder keeps its stricter rule. A space change voids those values until they are calibrated again.
- **Measured sharing.** Each model's acceptance reads Pss on a node with at least two cells.
- **Selection by published accuracy.** Accuracy comes from dated published benchmarks. Exomem measures CPU speed, peak memory and shareability on its own hardware, plus a sanity check for a broken conversion.
- **Converted models, each behind its own switch, off by default:**
  - the cross-encoder reranker;
  - the NLI stance verifier, as a new pin on an Exomem-built derived artifact, with an fp32 ONNX fallback if int8 misses a fixture;
  - a multilingual named-entity model of the GLiNER class, available as an instrument runtime;
  - a runtime path for small language-model instruments, of the ONNX Runtime GenAI class, which scores closed label sets and never returns sampled text.

  Named-entity and small language-model artifacts ship only when an admitted instrument question uses them.
- **Device-side preparation, allowed and bounded.** The same artifacts may run on the tenant's device, in the browser or the CLI, to prepare vectors and OCR before upload. The cell accepts a batch only on an exact identity match, the right shape and a passing sample that the cell re-encodes or re-extracts itself. Device data stays marked and recomputable. A wrong rejection costs the tenant only delay.

## Ownership boundary

This change owns the model runtime and the artifacts. It does not own what the models are used for:

- **The recall encoder's runtime** stays with `swap-embedding-runtime-to-onnx`. This change does not specify it again, and does not alter the bytes of the pinned recall-encoder artifact.
- **Instruments** stay with `add-sensed-epistemic-model`: questions, templates, label maps, the readings ledger and activation. That includes stance-verifier activation (no-nudge programme S9), and whether entity recurrence (S5) consumes named-entity readings.
  - This change states one placement fact, by the owner's ruling: local instruments on Cloud run per cell on shared weights. The owner amends R4 and D9 of `add-sensed-epistemic-model` to drop the in-cluster shared plane.
- **Hosted verifier admission** stays with the `hosted-tenant-cell` requirement "Hosted frozen-verifier activation is separately resource-admitted".
- **Import transport** for device-prepared data stays with `add-exomem-cloud-vault-import`.

**Archive dependency.** This change archives only after `add-sensed-epistemic-model` has amended its ruling R4 and design D9 to run Cloud instruments per cell. Until then, the two active changes disagree on where Cloud instruments run.

## Pure substrate and soft-fail

- The encoders, the reranker and the media transducers rank, represent or transcribe. They decide nothing.
- Instruments label closed questions under `add-sensed-epistemic-model`. This change adds no path by which generated text reaches a reading, a sidecar or a response.
- Every converted model is off by default. With its switch off, it does not load, and the product behaves as it does without it.

## Capabilities

### New Capabilities

- `shared-model-runtime` covers:
  - the runtime rule for every model the Cloud image ships;
  - the parity gate, the recorded artifact identity and the vector-space rule;
  - measured sharing and selection;
  - the switches for the converted models;
  - the closed-label scoring path for small language-model instruments;
  - device-side preparation with the same artifacts.

### Modified Capabilities

- `frozen-verifiers`: a pin may name a derived artifact by its digest and recipe, built from the pinned upstream revision. The fixture admission is unchanged.

No other canonical requirement changes:

- `multilingual-recall` already pins the recall encoder's artifact, and this change keeps those bytes.
- `hosted-tenant-cell` keeps its separate Hosted verifier admission.

## Impact

- **Image:** the Cloud image gains converted artifacts only as each owning change turns one on. The rule keeps PyTorch out.
- **Code:** a shared loader for pinned artifacts, the parity gates as tests, and the artifact identity record for each model kind. The pattern is `embedding_backend.ServedArtifact`, `ensure_artifact` and `EncoderProfile.fingerprint()`.
- **Capacity:** each model adds its weights once per node, plus each cell's working memory while it runs.
