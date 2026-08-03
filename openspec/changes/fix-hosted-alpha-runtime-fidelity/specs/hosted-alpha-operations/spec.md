## ADDED Requirements

### Requirement: Hosted cells deliver semantic recall
A hosted cell SHALL provide the same *retrieval* capability as the local runtime. The
rendered cell environment MUST NOT disable embeddings, and the cell worker limit SHALL be
greater than zero with the `embeddings` feature granted. Readiness SHALL report the
embeddings worker as ready rather than `HOSTED_WORKER_LIMIT_ZERO`. A cell that cannot run
semantic recall SHALL NOT be presented as a paid or invited tenant.

Server-side media extraction is explicitly OUT of scope for the alpha and its grant SHALL
remain withheld. It needs Whisper weights an order of magnitude larger than the
bi-encoder, plus Tesseract, and CPU-only transcription on a shared node approaches
real-time — one tenant's hour of audio would occupy a core for an hour. The
model-driven upload path that supplies text directly is unaffected. Enabling media SHALL
require its own capacity analysis rather than riding on this change.

#### Scenario: Cell renders with workers disabled
- **WHEN** the cell chart would render a worker limit of zero or omit the embeddings grant
- **THEN** rendering fails rather than producing a cell that silently serves keyword-only recall

#### Scenario: Tenant performs semantic recall
- **WHEN** an invited tenant captures notes and then queries by meaning rather than exact wording
- **THEN** the cell returns semantically ranked results, matching the local runtime's behaviour

### Requirement: The hosted image carries the weights the grant promises
The hosted image SHALL contain the embedding runtime and the model weights resolved at
build time, and the hub client SHALL be pinned offline. A cell runs under a default-deny
NetworkPolicy with no egress rules and a read-only root filesystem, so it can neither
download a model nor cache one. Granting `embeddings` against an image built without them
would advertise semantic recall and fail on first use.

The build SHALL prove the offline load rather than assume it, and SHALL NOT carry weights
for a capability whose grant is withheld.

#### Scenario: Grant is enabled against a lean image
- **WHEN** the cell environment grants embeddings but the image has no embedding runtime or weights
- **THEN** this is a release defect, because the cell reports ready and fails only when a tenant first queries by meaning

#### Scenario: Model load attempts a fetch at runtime
- **WHEN** a code path tries to resolve a model from the hub inside a cell
- **THEN** it fails immediately against the pinned offline client rather than hanging against a blocked NetworkPolicy until the caller's deadline

### Requirement: The cell cap is derived from measured memory on the node in service
The USER cell cap SHALL be justified against the memory an embedding-capable cell
actually uses, measured at the encode batch the runtime uses, against the node actually
in service. It SHALL NOT be carried over from the pre-embedding envelope.

The alpha node cannot be resized: as of 2026-08-03 the provider lists no `cx` type as
available or available_for_migration in any datacenter, so the existing instance is a
retired type that can be neither replaced nor grown. The fleet is therefore sized to the
node rather than the node to the fleet, and the cap is four USER cells rather than six.

Raising the cap SHALL require a fresh soak and reviewed cost sheet. Moving to a successor
instance family SHALL be treated as a pricing decision, because the available successors
cost roughly four times as much for equivalent memory and would not be covered by the
subsidised friends price.

#### Scenario: Cost basis names a node that is no longer in service
- **WHEN** the capacity contract's server cost does not match the provisioned server type
- **THEN** the cost evidence is rejected and provisioning admission fails closed

#### Scenario: Cap is raised without measuring the cell
- **WHEN** the USER cap would exceed what measured peak cell memory and platform overhead fit
- **THEN** it is rejected, because exceeding it evicts or OOM-kills tenants rather than queueing them

### Requirement: Encode batch size is chosen for the device
The embedding batch size SHALL follow the device the model runs on rather than a single
global constant. Batch size sets peak resident memory, because activations scale with it
while weights do not, and peak per cell is what determines how many tenants a node
carries.

On CPU a large batch is worse on both axes — measured with the shipped model, batch 32
peaks at 1332 MiB for 1.8 chunks/s while batch 8 peaks at 918 MiB for 2.3 chunks/s — so
the CPU default SHALL be the smaller batch. Accelerators SHALL keep the larger batch,
where it does buy parallelism.

#### Scenario: A CPU cell inherits an accelerator-sized batch
- **WHEN** a hosted cell encodes on CPU using the batch size chosen for a GPU
- **THEN** it costs roughly 400 MiB of additional peak for lower throughput, halving tenant density for nothing
