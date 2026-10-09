## Why

An Exomem Cloud cell runs in 2 CPU and 3 GiB, and its image carries no PyTorch. So a cell runs only one model, the recall encoder. A personal install also runs a reranker, image search, OCR, speech and the optional NLI stance verifier. Each of those must reach Cloud without multiplying memory by the number of cells on a node.

The recall encoder already shows how. It runs as a pinned int8 ONNX artifact, and Cloud cells load it with ONNX Runtime prepacking disabled. On 2026-10-09, on the production node `exomem-alpha-01`, three cells mapped the same bge-m3 `model.onnx.data`:

- Rss 290, 509 and 290 MB;
- Pss 96, 315 and 96 MB.

So the node held one copy of about 509 MB, not three.

Today that is a property of one model. Each new model would otherwise be chosen case by case, and a runtime that copies its weights into private memory would cost a full copy per cell. This change makes the encoder's pattern the rule for every model the Cloud image ships.

## What Changes

- **One runtime rule.** Every model the Cloud image ships is a pinned, pre-baked artifact in ONNX, or in another format whose weights load file-backed. It loads offline, as int8 where its parity gate passes, with prepacking disabled on Cloud. So all cells on a node share one read-only copy.
  - PyTorch is not in the Cloud image. It stays for development and the personal GPU paths.
  - A runtime that copies weights into private memory needs a measured reason.
  - Only immutable model bytes are shared, never writable memory, caches or processes. So tenant isolation is unchanged.
- **A parity gate.** An artifact ships only after it matches its reference:
  - an encoder at its reference precision, at a cosine of at least 0.9999 (the existing `MIN_COSINE`);
  - a quantised encoder, when its consumers' fixture verdicts are unchanged;
  - a scorer or transducer, at an agreement bound stated before the measurement;
  - an instrument, only through its existing fixture admission.
- **A recorded identity.** Each artifact records its model, revision, quantisation, format and digest. A changed identity is a new artifact, and anything calibrated on the old one is calibrated again.
- **Measured sharing.** Each model's acceptance reads Pss on a node with at least two cells.
- **Selection by published accuracy.** Accuracy comes from dated published benchmarks. Exomem measures only CPU speed, peak memory and shareability on its own hardware, plus a sanity check for a broken conversion.
- **Converted models, each behind its own switch, off by default:**
  - the cross-encoder reranker;
  - the NLI stance verifier, as a new instrument pin;
  - a multilingual named-entity model of the GLiNER class, available as an instrument runtime;
  - a runtime path for small language-model instruments, of the ONNX Runtime GenAI class, which scores closed label sets and never returns sampled text;
  - device-side processing: the same artifacts may run on the tenant's device, in the browser or the CLI, to prepare vectors and OCR before upload.

## Ownership boundary

This change owns the model runtime and the artifacts. It does not own what the models are used for:

- **The recall encoder's runtime** stays with `swap-embedding-runtime-to-onnx`. This change does not specify it again.
- **Instruments** stay with `add-sensed-epistemic-model`: questions, templates, label maps, the readings ledger, placement and activation. That includes stance-verifier activation (no-nudge programme S9), and whether entity recurrence (S5) consumes named-entity readings.
- **Hosted verifier admission** stays with the `hosted-tenant-cell` requirement "Hosted frozen-verifier activation is separately resource-admitted".
- **Import transport** for device-prepared data stays with `add-exomem-cloud-vault-import`.

## Pure substrate and soft-fail

- The encoders, the reranker and the media transducers rank, represent or transcribe. They decide nothing.
- Instruments label closed questions under `add-sensed-epistemic-model`. This change adds no path by which generated text reaches a reading, a sidecar or a response.
- Every converted model is off by default. With its switch off, it does not load, and the product behaves as it does without it.

## Capabilities

### New Capabilities

- `shared-model-runtime` covers:
  - the runtime rule for every model the Cloud image ships;
  - the parity gate and the recorded artifact identity;
  - measured sharing and selection;
  - the switches for the converted models;
  - the closed-label scoring path for small language-model instruments;
  - device-side processing with the same artifacts.

### Modified Capabilities

None. No canonical requirement changes:

- `multilingual-recall` already pins the recall encoder's artifact, and this change keeps those bytes.
- `frozen-verifiers` already admits a pin only by its fixture set. A converted NLI artifact is a new pin under that requirement.
- `hosted-tenant-cell` keeps its separate Hosted verifier admission.

## Impact

- **Image:** the Cloud image gains converted artifacts only as each owning change turns one on. The rule keeps PyTorch out.
- **Code:** a shared loader for pinned artifacts, the parity gates as tests, and the artifact identity record for each model kind. The pattern is `embedding_backend.ServedArtifact` and `EncoderProfile.fingerprint()`.
- **Capacity:** each model adds its weights once per node, plus each cell's working memory while it runs.
- **Evidence:** the 2026-10-09 node measurement is also evidence for tasks 5.1–5.3 of `swap-embedding-runtime-to-onnx`. This change does not edit that change.
