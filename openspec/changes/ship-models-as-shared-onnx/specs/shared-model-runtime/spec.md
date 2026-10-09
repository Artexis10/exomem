## ADDED Requirements

### Requirement: Every model the Cloud image ships is a pinned artifact whose weights load file-backed

Every model that the Exomem Cloud cell image ships SHALL be a pinned, pre-baked artifact in ONNX format, or in another format whose weights the runtime loads file-backed. It SHALL load with network access off. A Cloud cell SHALL load it with every load-time private copy of the weights disabled, such as ONNX Runtime prepacking, so that the cells on one node map one read-only copy. An artifact SHALL be int8 only where its int8 build passes its parity gate. A model with no consumer fixture to gate an int8 build SHALL ship at its reference precision. A model whose runtime copies its weights into private memory SHALL ship only with a measured reason, recorded in its selection, why no shareable candidate serves. Personal installs MAY keep the prepacked path.

The recall encoder already meets this rule. Its runtime contract stays with the change `swap-embedding-runtime-to-onnx` and the `multilingual-recall` capability, and this capability does not restate it.

#### Scenario: Two cells map one copy

- **WHEN** two Cloud cells on one node load the same shipped artifact
- **THEN** both map the same read-only weights file
- **AND** the node holds those weights once, not once per cell

#### Scenario: A model without a consumer fixture ships at reference precision

- **WHEN** a model has no consumer fixture that could gate an int8 build
- **THEN** it ships at its reference precision

#### Scenario: A private-copy runtime needs a recorded reason

- **WHEN** the best candidate for a job runs on a runtime that copies its weights into private memory
- **THEN** it ships only if its selection record holds the measurement that justifies it over every shareable candidate

#### Scenario: A personal server keeps the fast path

- **WHEN** a personal server loads a served artifact without an explicit sharing override
- **THEN** it keeps the runtime's prepacked path

### Requirement: The Cloud image carries no PyTorch

The Exomem Cloud cell image SHALL NOT contain PyTorch, and its build SHALL fail if PyTorch is importable in it. Development environments and the personal GPU paths MAY keep PyTorch.

#### Scenario: PyTorch reaches the Cloud image

- **WHEN** a dependency change makes PyTorch importable in the Cloud image
- **THEN** the image build fails

### Requirement: Cells share only immutable model bytes

Cells SHALL share only read-only, file-backed model weights. The model runtime, meaning the loaders and inference sessions that run shipped artifacts in a Cloud cell, SHALL NOT share writable memory, caches, tokenizer state or processes between tenants, so tenant isolation is unchanged. Local instruments on Cloud SHALL run per cell, in that cell's own processes, on the shared weights.

#### Scenario: A cell's working memory stays its own

- **WHEN** two cells on one node run the same model at the same time
- **THEN** each runs it in its own process, with its own writable memory and caches
- **AND** only the read-only weights file is shared between them

#### Scenario: A local instrument runs in its tenant's cell

- **WHEN** a Cloud tenant's local instrument senses
- **THEN** it runs in that tenant's cell, on the weights the node shares
- **AND** no other tenant's text reaches its process

### Requirement: An artifact ships only after it matches its reference

An artifact SHALL ship only after it passes the parity gate for its kind:

- **Encoder, at its reference precision.** Its outputs SHALL reach a cosine of at least 0.9999 against its reference implementation on a fixed input set, which is the existing runtime-substitution bound.
- **Quantised encoder.** The verdicts of each consumer's pinned fixture SHALL equal the verdicts under the unquantised reference.
- **Scoring model or transducer.** It SHALL agree with its reference on a pinned sample, at an agreement bound that is recorded before the measurement and never relaxed after it.
- **Instrument.** It SHALL be admitted only by its instrument fixture set under the `frozen-verifiers` and `sensed-epistemic-model` capabilities. This capability adds no separate bound for instruments.

A build that fails its gate SHALL NOT ship. A failed int8 build SHALL leave the reference-precision artifact as the only candidate.

#### Scenario: A broken conversion is caught

- **WHEN** an ONNX conversion of an encoder pools or tokenizes differently from its reference
- **THEN** its minimum cosine falls below 0.9999 and the artifact does not ship

#### Scenario: An int8 build that changes a verdict does not ship

- **WHEN** an int8 build changes any verdict of a consumer's pinned fixture
- **THEN** the int8 build does not ship, and the reference-precision artifact remains the candidate

#### Scenario: A converted instrument is a new pin

- **WHEN** an ONNX or int8 conversion of an admitted instrument is built
- **THEN** it is a new instrument pin, admitted only when its instrument fixture set passes at that pin
- **AND** the existing pin stays admitted until the new pin passes
- **AND** stored readings move to the new pin only through the existing instrument migration

#### Scenario: An int8 instrument that misses a fixture falls back

- **WHEN** the int8 conversion of an instrument misses any fixture of its set
- **THEN** it is not admitted, and the fp32 ONNX conversion becomes the candidate pin

### Requirement: An artifact's identity is recorded, and only a same-precision substitution keeps a vector space

Each shipped artifact SHALL record its artifact identity: its model, its upstream revision, its quantisation, its file format and the digest of the bytes it loads. An artifact that Exomem builds SHALL also record its conversion recipe and the recipe's version. It SHALL be published immutably, checked against its digest when it loads, and loaded from local files only. A local rebuild whose digest differs SHALL be a different artifact, and a pin or calibration made for the published digest SHALL NOT apply to it.

A value calibrated on an artifact SHALL key by its artifact identity, and SHALL apply to nothing after that identity changes until it is calibrated again. An instrument with a new artifact identity SHALL be a new pin. A runtime option that leaves the loaded bytes unchanged, such as disabling prepacking, SHALL NOT change the artifact identity.

For the encoders that this capability covers, a same-precision substitution that passes the encoder parity bound SHALL keep the vector space, and the new artifact identity SHALL be recorded. Another model, another precision, or other pooling, prefixes or sequence limit SHALL be another vector space. The recall encoder keeps the stricter space record of the `multilingual-recall` capability, in which another artifact digest is another space.

#### Scenario: A rebuilt artifact is a different artifact

- **WHEN** a host rebuilds an artifact and its bytes differ from the published digest
- **THEN** its artifact identity differs from the published artifact's
- **AND** a threshold calibrated on the published identity applies to nothing until it is calibrated again

#### Scenario: Sharing keeps the identity

- **WHEN** a Cloud cell loads the pinned recall-encoder artifact with prepacking disabled
- **THEN** its recorded identity equals a personal server's for the same artifact
- **AND** values calibrated on that identity still apply

#### Scenario: A same-precision runtime substitution keeps the space

- **WHEN** an fp32 ONNX build of an encoder replaces its fp32 reference implementation and passes the parity bound
- **THEN** stored vectors stay in the same vector space and are not re-encoded
- **AND** the new artifact identity is recorded

### Requirement: Sharing is measured on a node with at least two cells

The acceptance of each shipped model SHALL read the proportional set size (Pss) of its weights mapping in at least two cells on one node, and SHALL show that the node holds one copy. A model whose weights that measurement does not show as shared SHALL be treated as a private-copy runtime.

#### Scenario: The measurement shows one copy

- **WHEN** a model's weights file is mapped by several cells on one node
- **THEN** the sum of the cells' Pss for that mapping is about the largest single cell's Rss for it
- **AND** it is well below the sum of their Rss

#### Scenario: Assumed sharing is not accepted

- **WHEN** a model's acceptance has no Pss reading from at least two cells on one node
- **THEN** the model's acceptance does not pass

### Requirement: Models are selected by published accuracy and measured cost

Model accuracy SHALL come from published benchmarks, leaderboards, model cards and papers, each cited with its date. Exomem SHALL measure the CPU speed at pinned threads, the peak memory and the shareability on its own hardware, and SHALL run a small sanity check that catches a broken conversion. A capability that selects a model MAY add terms for its own kind of model. Model choices and language sets SHALL be deployment data or selection records, never lists in code.

#### Scenario: A selection cites dated evidence

- **WHEN** a model is selected for the Cloud image
- **THEN** its selection record cites the published accuracy results with their dates
- **AND** it records the measured speed, peak memory and sharing on Exomem's hardware

### Requirement: Each converted model ships behind its own switch

Converted artifacts MAY be built for the cross-encoder reranker, the NLI stance verifier, a multilingual named-entity model and small language-model instruments. Each SHALL have its own switch, off by default. With a switch off, the model SHALL NOT load, and every product surface SHALL behave as it does without the artifact.

- The reranker switch SHALL turn on in a Cloud cell only after the reranker's acceptance passes on a real cell: its parity gate, its measured sharing, and the Cloud service profile's outcome and capacity gates with reranking on.
- Activation of the stance verifier, of any instrument, and of anything that consumes an instrument's readings SHALL stay with the `frozen-verifiers` and `sensed-epistemic-model` capabilities.
- A named-entity or small language-model artifact SHALL ship in an image only when an admitted instrument question of the `sensed-epistemic-model` capability uses it. The named-entity model SHALL be available only as an instrument runtime, and its outputs SHALL be instrument readings.

#### Scenario: A shipped artifact with its switch off is invisible

- **WHEN** the image ships the reranker artifact and its switch is off
- **THEN** no reranker loads
- **AND** recall results are the same as on an image without the artifact

#### Scenario: Unused instrument weights do not ship

- **WHEN** no admitted instrument question uses the named-entity model
- **THEN** no image carries its artifact

### Requirement: The small language-model runtime scores closed label sets

The runtime path for small language-model instruments SHALL score a closed label set. For a versioned template whose vault text sits only in delimited data slots, it SHALL return the logits of the label tokens at one fixed position, which the instrument's label map reads under the output rule of the `frozen-verifiers` capability. Sampled or generated text SHALL never be an instrument output, and this capability SHALL add no path by which generated text reaches a reading, a sidecar or a response. Templates, label maps, the readings ledger and their governance stay with the `sensed-epistemic-model` capability.

#### Scenario: An instrument receives label scores

- **WHEN** an admitted instrument runs a question through the small language-model runtime
- **THEN** it receives the label-token logits that its label map reads
- **AND** no generated text is returned to it

### Requirement: A tenant's device may prepare derived data with the same artifacts

The same pinned artifacts MAY run on a tenant's own device, in the browser through a WebAssembly ONNX runtime or in the CLI, to prepare vectors and extraction results before upload. A device runtime MAY be used only after its outputs meet the same-precision parity bound against the cell's runtime for that artifact. An int8 artifact MAY run on a device only at one text per encode.

Device-produced data SHALL carry a device-provenance mark. A cell MAY accept device-produced vectors, and a device-produced extraction for an artifact that has none, only under all of these conditions:

- the recorded artifact identity exactly equals the identity the cell runs;
- the data has the shape that identity produces;
- the cell re-encodes a random sample of the batch, and every sampled item stays within the same-precision parity bound.

Data that fails any condition is invalid. One invalid item SHALL reject the whole batch, and the cell SHALL compute that data itself. Accepted data SHALL apply only to the uploading tenant's own vault. Accepted extraction results SHALL keep their device-provenance mark, and the cell SHALL be able to recompute them. A cell SHALL never require device-produced data. Canonical sidecars that arrive as vault content keep their existing rules. How device-produced data travels with an import is owned by the change `add-exomem-cloud-vault-import`, which this capability only references.

#### Scenario: A matching batch is accepted

- **WHEN** a tenant uploads vectors whose artifact identity and shape match the cell's, and every re-encoded sample stays within the bound
- **THEN** the cell stores them for that tenant's vault with their device-provenance mark, without encoding those passages again

#### Scenario: One deviating sample rejects the batch

- **WHEN** one re-encoded sample of an uploaded batch deviates beyond the bound
- **THEN** the cell rejects the whole batch and encodes those passages itself

#### Scenario: A mismatched identity is recomputed

- **WHEN** a tenant uploads vectors produced by another artifact identity
- **THEN** the cell discards them and encodes those passages itself

#### Scenario: Device data never crosses tenants

- **WHEN** a tenant's device prepares derived data
- **THEN** only that tenant's cell receives it, and only that tenant's vault uses it
