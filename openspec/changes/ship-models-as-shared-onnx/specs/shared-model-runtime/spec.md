## ADDED Requirements

### Requirement: Every model the Cloud image ships is a pinned artifact whose weights load file-backed

Every model that the Exomem Cloud cell image ships SHALL be a pinned, pre-baked artifact in ONNX format, or in another format whose weights the runtime loads file-backed. It SHALL load with network access off. A Cloud cell SHALL load it with every load-time private copy of the weights disabled, such as ONNX Runtime prepacking, so that the cells on one node map one read-only copy. An artifact SHALL be int8 only where its int8 build passes its parity gate. A model whose runtime copies its weights into private memory SHALL ship only with a measured reason, recorded in its selection, why no shareable candidate serves. Personal installs MAY keep the prepacked path.

The recall encoder already meets this rule. Its runtime contract stays with the change `swap-embedding-runtime-to-onnx` and the `multilingual-recall` capability, and this capability does not restate it.

#### Scenario: Two cells map one copy

- **WHEN** two Cloud cells on one node load the same shipped artifact
- **THEN** both map the same read-only weights file
- **AND** the node holds those weights once, not once per cell

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

Cells SHALL share only read-only, file-backed model weights. This runtime SHALL NOT share writable memory, caches, tokenizer state or processes between tenants, so tenant isolation is unchanged. A placement that runs a model outside the cells, such as an instrument plane specified by the `sensed-epistemic-model` capability, is governed by its own capability and not by this requirement.

#### Scenario: A cell's working memory stays its own

- **WHEN** two cells on one node run the same model at the same time
- **THEN** each runs it in its own process, with its own writable memory and caches
- **AND** only the read-only weights file is shared between them

### Requirement: An artifact ships only after it matches its reference

An artifact SHALL ship only after it passes the parity gate for its kind:

- **Encoder, at its reference precision.** Its outputs SHALL reach a cosine of at least 0.9999 against its reference implementation on a fixed input set, which is the existing runtime-substitution bound.
- **Quantised encoder.** The verdicts of each consumer's pinned fixture SHALL equal the verdicts under the unquantised reference. The quantised artifact SHALL carry its own artifact identity.
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
- **THEN** it is a new instrument pin, admitted only when its instrument fixture set passes
- **AND** the existing pin stays admitted until the new pin passes
- **AND** stored readings move to the new pin only through the existing instrument migration

### Requirement: An artifact's identity is recorded, and a changed identity is a new artifact

Each shipped artifact SHALL record its model, its upstream revision, its quantisation, its file format and the digest of the bytes it loads. A value calibrated on an artifact SHALL key by that identity. A rebuilt artifact, another precision or another sequence limit SHALL be a new identity: an encoder's vectors form a new space, an instrument becomes a new pin, and a value calibrated on the old identity SHALL apply to nothing until it is calibrated again. A runtime option that leaves the loaded bytes unchanged, such as disabling prepacking, SHALL NOT change the identity. This change SHALL NOT alter the bytes of the pinned recall-encoder artifact.

#### Scenario: A rebuilt artifact is a new identity

- **WHEN** a host rebuilds an artifact and its bytes differ from the pinned digest
- **THEN** its identity differs from the pinned artifact's
- **AND** a threshold calibrated on the pinned identity applies to nothing until it is calibrated again

#### Scenario: Sharing keeps the identity

- **WHEN** a Cloud cell loads the pinned recall-encoder artifact with prepacking disabled
- **THEN** its recorded identity equals a personal server's for the same artifact
- **AND** values calibrated on that identity still apply

### Requirement: Sharing is measured on a node with at least two cells

The acceptance of each shipped model SHALL read the proportional set size (Pss) of its weights mapping in at least two cells on one node, and SHALL show that the node holds one copy. A model whose weights that measurement does not show as shared SHALL be treated as a private-copy runtime.

#### Scenario: The measurement shows one copy

- **WHEN** a model's weights file is mapped by several cells on one node
- **THEN** the cells' Pss values for that mapping sum to about one copy of the file

#### Scenario: Assumed sharing is not accepted

- **WHEN** a model's acceptance has no Pss reading from at least two cells on one node
- **THEN** the model's acceptance does not pass

### Requirement: Models are selected by published accuracy and measured cost

Model accuracy SHALL come from published benchmarks, leaderboards, model cards and papers, each cited with its date. Exomem SHALL measure only the CPU speed at pinned threads, the peak memory and the shareability on its own hardware, plus a small sanity check that catches a broken conversion. Model choices and language sets SHALL be deployment data or selection records, never lists in code.

#### Scenario: A selection cites dated evidence

- **WHEN** a model is selected for the Cloud image
- **THEN** its selection record cites the published accuracy results with their dates
- **AND** it records the measured speed, peak memory and sharing on Exomem's hardware

### Requirement: Each converted model ships behind its own switch

Converted artifacts SHALL be available for the cross-encoder reranker, the NLI stance verifier, a multilingual named-entity model and small language-model instruments. Each SHALL have its own switch, off by default. With a switch off, the model SHALL NOT load, and every product surface SHALL behave as it does without the artifact.

- The reranker switch SHALL turn on in a Cloud cell only after the reranker's acceptance passes on a real cell: its parity gate, its measured sharing, and the Cloud service profile's outcome and capacity gates with reranking on.
- Activation of the stance verifier, of any instrument, and of anything that consumes an instrument's readings SHALL stay with the `frozen-verifiers` and `sensed-epistemic-model` capabilities.
- The named-entity model SHALL be available only as an instrument runtime. Its outputs SHALL be instrument readings under the `sensed-epistemic-model` capability.

#### Scenario: A shipped artifact with its switch off is invisible

- **WHEN** the image ships the reranker artifact and its switch is off
- **THEN** no reranker loads
- **AND** recall results are the same as on an image without the artifact

#### Scenario: A named-entity artifact senses nothing by itself

- **WHEN** the named-entity artifact is present and no admitted instrument question uses it
- **THEN** it is not loaded and no reading is recorded

### Requirement: The small language-model runtime scores closed label sets

The runtime path for small language-model instruments SHALL score a closed label set. For a versioned template whose vault text sits only in delimited data slots, it SHALL return the logits or probabilities of the label tokens at one fixed position, normalised over the closed set plus abstain. Sampled or generated text SHALL never be an instrument output, and this capability SHALL add no path by which generated text reaches a reading, a sidecar or a response. Templates, label maps, the readings ledger and their governance stay with the `sensed-epistemic-model` capability.

#### Scenario: An instrument receives probabilities

- **WHEN** an admitted instrument runs a question through the small language-model runtime
- **THEN** it receives probabilities over the question's closed label set plus abstain
- **AND** no generated text is returned to it

### Requirement: A tenant's device may prepare derived data with the same artifacts

The same pinned artifacts MAY run on a tenant's own device, in the browser through a WebAssembly ONNX runtime or in the CLI, to prepare vectors and extraction results before upload. A device runtime SHALL be supported only after its outputs meet the same-precision parity bound against the cell's runtime for that artifact. A cell SHALL accept device-produced vectors, and a device-produced extraction for an artifact that has none, only when the recorded artifact identity exactly equals the identity the cell runs. It SHALL apply that data only to the uploading tenant's own vault, and SHALL recompute whatever is absent, mismatched or invalid. A cell SHALL never require device-produced data. Canonical sidecars that arrive as vault content keep their existing rules. How device-produced data travels with an import is owned by the change `add-exomem-cloud-vault-import`.

#### Scenario: Matching identity is accepted

- **WHEN** a tenant uploads vectors whose recorded artifact identity equals the cell's
- **THEN** the cell stores them for that tenant's vault without encoding those passages again

#### Scenario: A mismatched identity is recomputed

- **WHEN** a tenant uploads vectors produced by another artifact identity
- **THEN** the cell discards them and encodes those passages itself

#### Scenario: Device data never crosses tenants

- **WHEN** a tenant's device prepares derived data
- **THEN** only that tenant's cell receives it, and only that tenant's vault uses it
