## Context

- **The Cloud cell runs one model today.** The `cloud` image stage (`Dockerfile`) installs only the `embeddings-onnx` extra, gates its build on PyTorch being absent, sets `EXOMEM_DISABLE_RANKING=1`, and carries one pre-baked model: the pinned bge-m3 int8 artifact (`embedding_backend._SERVED`). Recall and activation share that one instance (`multilingual-recall`).
- **The encoder already shares its weights.**
  - `runtime_resources.onnx_share_weights_enabled()` is on by default in Cloud mode, and off for personal servers and Hosted cells. `EXOMEM_ONNX_SHARE_WEIGHTS=0|1` overrides it.
  - When it is on, `_OnnxEncoder` sets `session.disable_prepacking=1`. The weights then stay in the file mapping of `model.onnx.data`, which every process on the node maps from one page cache.
  - ONNX Runtime maps that file private and writable (`rw-p`), so VmData counts it (reproduced on ORT 1.27.0 in the review of this change). Nothing writes the pages, so they stay clean and shared, and Pss shows the sharing. The media hard limit of `add-cloud-multimodal-processing` (design D2) budgets VmData for this reason.
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
- A shared inference process across tenants.
- GPUs on Cloud.

## Decisions

### D1. One runtime rule

Every model the Cloud image ships is a pinned, pre-baked artifact in ONNX, or in another format whose weights load file-backed. It loads offline, with prepacking or any other load-time private copy disabled on Cloud. Personal servers keep the prepacked fast path.

- A model ships as int8 only where its D3 int8 gate can run and passes.
  - An encoder's int8 parity can be proved only by consumer fixtures. While an encoder has none, it ships at its reference precision. The image lane of `add-cloud-multimodal-processing` is the first case: no image relevance fixture exists, so its model ships as fp32 ONNX.
  - A scorer or transducer has its own int8 gate, the pinned agreement sample, so it may ship as int8 without a consumer fixture. That keeps int8 speech candidates, and the 1.5 GB routing cap of the speech rule, workable.
- A runtime that copies its weights into private memory pays a full copy per cell. It is chosen only with a measured reason. CTranslate2 (faster-whisper) and Tesseract may be such runtimes (unverified); acceptance measures each one.
- PyTorch is not in the Cloud image.

### D2. Only immutable bytes are shared, and local instruments run per cell

Cells share read-only, file-backed weights through the page cache. Each cell keeps its own process, writable memory, caches and tokenizer state. So sharing costs no isolation: no tenant's text or state enters another tenant's process. This holds with no exception.

The owner of `add-sensed-epistemic-model` ruled on 2026-10-09 that Cloud instruments run per cell on these shared, read-only weights. That drops the in-cluster shared sensing plane of its ruling R4 and design D9 (slice 5). The owner amends that change; this change does not edit it. Until the amendment lands, the two active changes disagree on where Cloud instruments run, so this change archives only after it (proposal, "Archive dependency").

### D3. The parity gate

| Kind | Gate | Source of the bound |
|---|---|---|
| Encoder at its reference precision | minimum cosine ≥ 0.9999 on a fixed input set | `MIN_COSINE` in `tests/test_embedding_backend.py` |
| Quantised encoder | each consumer's pinned fixture verdicts equal the unquantised reference's | how the bge-m3 int8 artifact was admitted (`embedding_backend._SERVED` comment) |
| Scorer or transducer (reranker, captioner, speech) | agreement with the reference on a pinned sample | a bound recorded before the measurement |
| Instrument (NLI, named entities, small language model) | its instrument fixture set, at the new pin | `frozen-verifiers`, `sensed-epistemic-model` |

**Why int8 is gated on verdicts, not on cosine.** A cosine of 0.9999 is the bound for a runtime substitution at the same precision. An int8 build cannot meet it against fp32: a shared int8 batch alone moves a vector by up to 0.02 cosine (archived `make-recall-multilingual` design). So a quantised encoder is gated on its consumers' verdicts. Where an encoder has no consumer fixture, there is no int8 gate, and it ships at reference precision (D1). A scorer or transducer is not affected: its pinned agreement sample gates its int8 build. This is the one place these changes explain it.

**A converted instrument is a new pin.** `add-sensed-epistemic-model` defines `instrument_id = sha256(model, revision, weights, runtime, runtime_version, template_version)`. An ONNX or int8 build of the NLI pin changes the weights and the runtime.

- The owner ruled on 2026-10-09 that a frozen-verifier pin may name an Exomem-built derived artifact by its digest. The precedent is `embedding_backend.ensure_artifact`, which builds and checks the bge-m3 int8 artifact. The pin records:
  - the upstream revision the artifact was built from;
  - the conversion recipe and its version;
  - the digest of the output manifest.
- The artifact is published immutably. A host fetches it by digest from that publication, checks the digest at load, and loads it from local files only.
- Only bytes that match the published digest load, and hosts do not build the artifact locally. `onnxruntime` is not pinned, and the quantiser's output can differ by host, so a local build may not reproduce the published bytes. A host without the published bytes refuses the verifier until it fetches them, as the `frozen-verifiers` refusal scenario states. This is deliberately stricter than `ensure_artifact`, which builds the recall encoder locally when no download is available; that path stays with `swap-embedding-runtime-to-onnx`.
- This needs a MODIFIED delta to the canonical `frozen-verifiers` requirement "A frozen verifier runs only under a pinned identity", which today requires the "exact repository-pinned upstream revision" to be resident. The delta adds "or a derived artifact named by its digest and recipe and built from that revision", and the same allowance where the constructor receives the artifact. It adds one scenario for a rebuilt artifact, and changes nothing else.
- `add-sensed-epistemic-model` also modifies that requirement. Whichever change archives second refreshes its MODIFIED block onto the other's result and keeps both changes' additions.
- Admission is the existing fixture gate at the new pin: `stance-v2-multilingual`, and `relation-v1-multilingual` at 20 of 20 including the negative twins, in the dedicated `nli` CI lane.
- If the int8 build misses any fixture, the fp32 ONNX conversion becomes the candidate pin. The upstream quantised export already failed a contradiction fixture (`docs/hosted-inference-boundary.md`), so this fallback is likely to be used.
- The PyTorch pin stays admitted until the new pin passes. Stored readings move through the existing instrument migration and lazy re-sense (`add-sensed-epistemic-model` D3).
- This change adds no separate label-agreement bound for instruments.

### D4. The recorded identity and the vector space

Each artifact records its artifact identity: model, upstream revision, quantisation, file format and the digest of the bytes it loads. An artifact that Exomem builds also records its conversion recipe and version. The pattern exists:

- `embedding_backend.ServedArtifact` holds revision, source files, quantisation, file format, artifact digest and asset digest, and `ensure_artifact` checks the digest on every load.
- `EncoderProfile.fingerprint()` hashes model, pooling, prefixes, sequence limit and normalisation, plus revision, quantisation, format and digest for a served model. It leaves out the backend, so a runtime swap is not a model change.
- `claims.VerifierPin` holds model, revision, artifact files, weights digest, label-map version and fixture set.

Two things key on this record:

- **The vector space follows one rule.** A same-precision substitution that passes the encoder parity bound keeps the vector space, and its new artifact identity is recorded. This is the principle of `swap-embedding-runtime-to-onnx`: a backend swap is not a model change. Another model, precision, pooling, prefix set or sequence limit is another space.
  - The recall encoder keeps its stricter canonical rule. `multilingual-recall` scenario "Another build of the same model is refused" makes another artifact digest another space. This change covers the other encoders, and does not relax that rule.
- **Calibrated values follow the space.** A value calibrated on an encoder's output keys by its vector space. A space-keeping substitution carries it; a space change voids it until it is calibrated again. A value calibrated on another model's output keys by its artifact identity.
  - The image-tags threshold (`EXOMEM_IMAGE_TAGS_THRESHOLD`) carries across an fp32 ONNX build of the image model that passes the bound.
  - The sensed model's cosine proposer keys θ = 0.72 by the recall encoder's fingerprint, `BAAI/bge-m3|cls|l2|74068c180d6514e8` in `sensed_model.COSINE_THETA`. That fingerprint includes the artifact digest, so any rebuilt artifact voids θ until it is calibrated again. This change keeps the bge-m3 bytes. Disabling prepacking is a session option outside the fingerprint, and the task 5.4 probe gave bit-identical vectors with it, so θ still applies on Cloud.

### D5. Sharing is measured, not assumed

Each model's acceptance reads the Pss of its weights mapping in at least two cells on one node. When the weights are shared, the sum of the cells' Pss is about the largest single Rss, and far below the sum of their Rss, as the 2026-10-09 measurement in Context shows. Linux charges the page cache to the first cgroup that faults it, so a cell's own memory statistics do not show the sharing; Pss does.

### D6. Selection

Accuracy comes from published benchmarks, leaderboards, model cards and papers, each cited with its date in the model's selection record. Exomem measures what no benchmark covers:

- CPU speed at pinned threads on its hardware;
- peak memory;
- whether the weights are shared.

A small sanity check catches a broken conversion. A capability that selects a model may add terms for its kind; the speech terms of `add-cloud-multimodal-processing` are an example. Model choices and language sets are deployment data or selection records, never lists in code.

### D7. Converted models, each behind its own switch

Each switch is off by default. A shipped artifact with its switch off does not load.

- **(a) Reranker.** The reranker model is chosen under D6, and its script coverage stays declared for the `multilingual-recall` rerank gate. Its Cloud switch turns on only after its acceptance on a real cell: the parity gate, measured sharing, and the Cloud service profile's outcome and capacity gates with reranking on.
- **(b) NLI stance verifier.** The artifact is a new derived pin under D3. Activation stays with `EXOMEM_CLAIM_POLARITY_NLI` and the no-nudge programme's S9. Hosted keeps its own resource admission (`hosted-tenant-cell`).
- **(c) Multilingual named entities, GLiNER class.** The artifact and its runtime are available as an instrument runtime. Its outputs are instrument readings recorded in the ledger under rulings R1 and R2 of `add-sensed-epistemic-model`; "sensors" stay the deterministic families only. Whether and how entity recurrence (S5) consumes those readings belongs to `add-sensed-epistemic-model`. The artifact ships in an image only when an admitted instrument question uses it.
- **(d) Small language-model instruments, ONNX Runtime GenAI class.** The runtime path returns the label-token logits at one fixed position. The instrument's label map reads them under the `frozen-verifiers` output rule. Sampled text is never an instrument output. Templates, label maps, the ledger and governance stay with `add-sensed-epistemic-model`. The artifact ships in an image only when an admitted instrument question uses it.
- **(e) Device-side preparation.** The owner approved it. The same artifacts may run on the tenant's device, in the browser through onnxruntime-web or in the CLI, so that OCR and vectors can be prepared before upload. The cell trusts nothing it cannot check cheaply:
  - A device runtime may be used only after its outputs meet the same-precision parity bound against the cell's runtime. WebAssembly kernels can differ slightly from native ones.
  - An int8 artifact runs on a device only at one text per encode, because batching moves int8 vectors (D3).
  - Device data carries a device-provenance mark.
  - A batch is invalid when any item has the wrong artifact identity or the wrong shape, or when a random sample fails the cell's own check:
    - for vectors, the cell re-encodes the sample, and each vector must stay within the same-precision parity bound;
    - for extraction results such as OCR text, the cell re-extracts the sample, and each text must equal the cell's exactly after Unicode NFC normalisation and the collapse of each whitespace run to one space.
  - One invalid item rejects the whole batch, and the cell computes that data itself.
  - **Wrong-firing cost.** A wrong rejection makes the cell do the computation it would have done without device data. The tenant pays only delay, and no data is lost.
  - Accepted data applies only to that tenant's vault. Accepted extraction results keep their mark and stay recomputable.
  - Identity equality and the sample prove that the batch matches the cell's artifact, not that every item was computed honestly. The harm is bounded to the tenant's own vault, which the tenant can already write.
  - Canonical sidecars that arrive as vault content keep their existing rules. How device-produced data travels, and how an import archive carries it, belong to `add-exomem-cloud-vault-import`, which this change only references.

## Risks / Trade-offs

- **Disabling prepacking costs latency.** About 12% on short queries and 2% on long chunks, for one copy per node instead of one per cell. Personal servers keep prepacking.
- **int8 can change verdicts.** The upstream quantised NLI export failed a contradiction fixture. The gate catches that, and the fp32 conversion stays the candidate.
- **A cell's memory statistics hide the sharing.** The page cache is charged to the cgroup that unpacked the image. Acceptance reads Pss, not the cell's `memory.stat`.
- **Newer conversions are less proven.** GLiNER-class and GenAI-class ONNX paths are younger than the encoder path. Each ships only behind its switch and its gate, and only when a question uses it.
- **Device data is a new input.** The identity check, the shape check and the re-encoded sample bound it, and the harm stays in the uploading tenant's vault.

## Migration Plan

1. Land the rule, the identity record, the gates and the `frozen-verifiers` allowance. The encoder already conforms.
2. Convert the reranker behind its switch, and run its Cloud acceptance.
3. Build the derived NLI pin when S9 needs it on Cloud. The PyTorch pin stays admitted until the new pin passes.
4. Ship the named-entity and small language-model runtimes when `add-sensed-epistemic-model` admits a question that uses them.
5. Add device-side preparation after `add-exomem-cloud-vault-import` can carry the data.

Rollback: turn the switch off. The model stops loading, and the product behaves as it does without it.

## Open Questions

- Whether this change owns the reranker's Cloud activation, as D7 (a) assumes, or a later change does.
- The agreement bound for the reranker, recorded before its measurement.
- The size of the device batch sample, and how the device-provenance mark is stored, are set at implementation with `add-exomem-cloud-vault-import`.
