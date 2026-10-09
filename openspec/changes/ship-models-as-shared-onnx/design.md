## Context

- **The Cloud cell runs one model today.** The `cloud` image stage (`Dockerfile`) installs only the `embeddings-onnx` extra, gates its build on PyTorch being absent, sets `EXOMEM_DISABLE_RANKING=1`, and carries one pre-baked model: the pinned bge-m3 int8 artifact (`embedding_backend._SERVED`). Recall and activation share that one instance (`multilingual-recall`).
- **The encoder already shares its weights.**
  - `runtime_resources.onnx_share_weights_enabled()` is on by default in Cloud mode, and off for personal servers and Hosted cells. `EXOMEM_ONNX_SHARE_WEIGHTS=0|1` overrides it.
  - When it is on, `_OnnxEncoder` sets `session.disable_prepacking=1`. The weights then stay in the file-backed mapping of `model.onnx.data`, which every process on the node maps from one page cache.
- **The production measurement.** On 2026-10-09, on `exomem-alpha-01`, three cells mapped the bge-m3 int8 `model.onnx.data`:
  - Rss 290, 509 and 290 MB;
  - Pss 96, 315 and 96 MB.

  So the node held one copy of about 509 MB, not three. This is also evidence for tasks 5.1–5.3 of `swap-embedding-runtime-to-onnx`, which size the cell envelope and the cell cap. This change does not edit that change.
- **What prepacking costs.** The Exomem note "ONNX Runtime prepacking doubles bge-m3 weights per process" (2026-09-30), and task 5.4 of `swap-embedding-runtime-to-onnx`, record the trade:
  - prepacking makes a private copy of about 290 MiB per process;
  - disabling it costs about 12% on short queries (24.2 ms against 27.1 ms) and about 2% on long chunks (514.5 ms against 524.7 ms);
  - every variant gave bit-identical vectors on the probe texts.

  Task 5.5 of that change measured two production cells on 2026-10-06. Of 74,106 resident pages of the file, 74,102 were the same physical frames in both. The page cache was charged to `system.slice/k3s.service`, which unpacked the image, not to either cell.
- **Why PyTorch stays out.** On the same model, PyTorch imports 399 MiB against ONNX Runtime's 40 MiB (`swap-embedding-runtime-to-onnx` proposal). The `ml` and `cuda` images and development environments keep it.
- **The models that Cloud lacks.**
  - **Reranker.** `BAAI/bge-reranker-base` through the sentence-transformers `CrossEncoder` on PyTorch (`embeddings.py`); `EXOMEM_RERANKER_MODEL` overrides it. `find_policy` declares script coverage for `bge-reranker-base` and `bge-reranker-v2-m3`.
  - **NLI stance verifier.** `claims.VERIFIER_PINS` admits `MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7` at revision `b5113eb3…`, as safetensors, label map `v2`, fixture set `stance-v2-multilingual`. The `nli` extra installs sentence-transformers and PyTorch. `docs/hosted-inference-boundary.md` records that the upstream quantised ONNX export (about 323 MiB) failed a genuine-contradiction fixture.
  - **Named entities, and small language-model instruments.** Nothing ships today.

## Goals / Non-Goals

**Goals:**
- One rule decides how any model reaches the Cloud image, so each new model costs one copy per node.
- No artifact ships without a parity gate, and no consumer silently runs on changed bytes.
- Cloud can reach parity with a personal install, model by model, each behind its own switch.

**Non-goals:**
- Specifying the recall encoder's runtime again. `swap-embedding-runtime-to-onnx` owns it.
- Deciding what an instrument asks, when it runs, or how its readings are used. `add-sensed-epistemic-model` owns that.
- A shared inference process across tenants inside this runtime.
- GPUs on Cloud.

## Decisions

### D1. One runtime rule

Every model the Cloud image ships is a pinned, pre-baked artifact in ONNX, or in another format whose weights load file-backed. It loads offline, as int8 where its parity gate passes, with prepacking or any other load-time private copy disabled on Cloud. Personal servers keep the prepacked fast path.

- A runtime that copies its weights into private memory pays a full copy per cell. It is chosen only with a measured reason. CTranslate2 (faster-whisper) and Tesseract may be such runtimes (unverified); acceptance measures each one.
- PyTorch is not in the Cloud image.

### D2. Only immutable bytes are shared

Cells share read-only, file-backed weights through the page cache. Each cell keeps its own process, writable memory, caches and tokenizer state. So sharing costs no isolation: no tenant's text or state enters another tenant's process.

This rule covers models that run inside the cells. `add-sensed-epistemic-model` specifies a different placement for instruments on Cloud: an in-cluster shared plane (its ruling R4, design D9). That plane has its own isolation contract and acceptance, and this change neither adopts nor changes it.

### D3. The parity gate

| Kind | Gate | Source of the bound |
|---|---|---|
| Encoder at its reference precision | minimum cosine ≥ 0.9999 on a fixed input set | `MIN_COSINE` in `tests/test_embedding_backend.py` |
| Quantised encoder | each consumer's pinned fixture verdicts equal the unquantised reference's | how the bge-m3 int8 artifact was admitted (`embedding_backend._SERVED` comment) |
| Scorer or transducer (reranker, captioner, speech) | agreement with the reference on a pinned sample | a bound recorded before the measurement |
| Instrument (NLI, named entities, small language model) | its instrument fixture set | `frozen-verifiers`, `sensed-epistemic-model` |

- A cosine of 0.9999 is the bound for a runtime substitution at the same precision. An int8 build cannot meet it against fp32: a shared int8 batch alone moves a vector by up to 0.02 cosine (archived `make-recall-multilingual` design). So a quantised encoder is gated on its consumers' verdicts instead, and it carries its own identity.
- **A converted instrument is a new pin.** `add-sensed-epistemic-model` defines `instrument_id = sha256(model, revision, weights, runtime, runtime_version, template_version)`. An ONNX or int8 build of the NLI pin changes the weights and the runtime.
  - Its gate is the existing fixture admission: `stance-v2-multilingual`, and `relation-v1-multilingual` at 20 of 20 including the negative twins, in the dedicated `nli` CI lane.
  - The PyTorch pin stays admitted until the new pin passes.
  - Stored readings move through the existing instrument migration and lazy re-sense (`add-sensed-epistemic-model` D3).
  - This change adds no separate label-agreement bound for instruments.

### D4. The recorded identity

Each artifact records its model, upstream revision, quantisation, file format and the digest of the bytes it loads. The pattern exists:

- `embedding_backend.ServedArtifact` holds revision, source files, quantisation, file format, artifact digest and asset digest.
- `EncoderProfile.fingerprint()` hashes model, pooling, prefixes, sequence limit and normalisation, plus revision, quantisation, format and digest for a served model. It leaves out the backend, so a runtime swap is not a model change.
- `claims.VerifierPin` holds model, revision, artifact files, weights digest, label-map version and fixture set.

A rebuilt artifact, another precision or another sequence limit is a new identity. The live example is the sensed model's cosine proposer: `sensed_model.COSINE_THETA` holds θ = 0.72 under the fingerprint `BAAI/bge-m3|cls|l2|74068c180d6514e8`, the pinned int8 artifact, and any other fingerprint proposes nothing until it is calibrated. This change keeps the bge-m3 bytes. Disabling prepacking is a session option outside the fingerprint, and the task 5.4 probe gave bit-identical vectors with it, so θ still applies on Cloud.

### D5. Sharing is measured, not assumed

Each model's acceptance reads the Pss of its weights mapping in at least two cells on one node. The Pss values sum to about one copy when the weights are shared. Linux charges the page cache to the first cgroup that faults it, so a cell's own memory statistics do not show the sharing; Pss does.

### D6. Selection

Accuracy comes from published benchmarks, leaderboards, model cards and papers, each cited with its date in the model's selection record. Exomem measures only what no benchmark covers:

- CPU speed at pinned threads on its hardware;
- peak memory;
- whether the weights are shared.

A small sanity check catches a broken conversion. Model choices and language sets are deployment data or selection records, never lists in code.

### D7. Converted models, each behind its own switch

Each switch is off by default. A shipped artifact with its switch off does not load.

- **(a) Reranker.** The reranker model is chosen under D6, and its script coverage stays declared for the `multilingual-recall` rerank gate. Its Cloud switch turns on only after its acceptance on a real cell: the parity gate, measured sharing, and the Cloud service profile's outcome and capacity gates with reranking on.
- **(b) NLI stance verifier.** The artifact is a new pin under D3. Activation stays with `EXOMEM_CLAIM_POLARITY_NLI` and the no-nudge programme's S9. Hosted keeps its own resource admission (`hosted-tenant-cell`).
- **(c) Multilingual named entities, GLiNER class.** The artifact and its runtime are available as an instrument runtime. Its outputs are instrument readings recorded in the ledger under rulings R1 and R2 of `add-sensed-epistemic-model`; "sensors" stay the deterministic families only. Whether and how entity recurrence (S5) consumes those readings belongs to `add-sensed-epistemic-model`.
- **(d) Small language-model instruments, ONNX Runtime GenAI class.** The runtime path scores a closed label set: it reads the logits of the label tokens at one fixed position and normalises them over the closed set plus abstain (R1). Sampled text is never an instrument output. Templates, label maps, the ledger and governance stay with `add-sensed-epistemic-model`.
- **(e) Device-side processing.** The same artifacts may run on the tenant's device: in the browser through onnxruntime-web, or in the CLI. That lets OCR and vectors be prepared before upload.
  - A device runtime is supported only after its outputs meet the same-precision parity bound against the cell's runtime.
  - The cell accepts device-produced vectors, and a device-produced extraction for an artifact that has none, only when the recorded artifact identity exactly equals the cell's.
  - The data applies only to that tenant's vault, and the cell recomputes anything absent, mismatched or invalid.
  - Identity equality proves that the claim matches the cell's artifact, not that the device ran it honestly. The harm is bounded to the tenant's own vault, which the tenant can already write.
  - Canonical sidecars that arrive as vault content keep their existing rules. How device-produced data travels belongs to `add-exomem-cloud-vault-import`, which this change does not modify.

## Risks / Trade-offs

- **Disabling prepacking costs latency.** About 12% on short queries and 2% on long chunks, for one copy per node instead of one per cell. Personal servers keep prepacking.
- **int8 can change verdicts.** The upstream quantised NLI export failed a contradiction fixture. The gate catches that, and the reference-precision artifact stays the candidate.
- **A cell's memory statistics hide the sharing.** The page cache is charged to the cgroup that unpacked the image. Acceptance reads Pss, not the cell's `memory.stat`.
- **Newer conversions are less proven.** GLiNER-class and GenAI-class ONNX paths are younger than the encoder path. Each ships only behind its switch and its gate.
- **Browser numerics differ.** WebAssembly kernels can differ slightly from native ones. The device runtime meets the same-precision bound before it is supported.

## Migration Plan

1. Land the rule, the identity record and the gates. The encoder already conforms.
2. Convert the reranker behind its switch, and run its Cloud acceptance.
3. Build the ONNX NLI pin when S9 needs it on Cloud. The PyTorch pin stays admitted until the new pin passes.
4. Ship the named-entity and small language-model runtimes when `add-sensed-epistemic-model` admits a question that uses them.
5. Add device-side processing after `add-exomem-cloud-vault-import` can carry the data.

Rollback: turn the switch off. The model stops loading, and the product behaves as it does without it.

## Open Questions

- Whether this change owns the reranker's Cloud activation, as D7 (a) assumes, or a later change does.
- The `frozen-verifiers` requirement asks that the pin's "exact repository-pinned upstream revision is resident". `docs/hosted-inference-boundary.md` records an available full ONNX export of about 1,064 MiB. If that export is published at an upstream revision, an fp32 ONNX pin fits the requirement as written (unverified). An int8 build made by Exomem is a derived manifest; whether a pin may name one is for the owner of `add-sensed-epistemic-model`.
- The agreement bound for the reranker, recorded before its measurement.
