## ADDED Requirements

### Requirement: Every deployment encodes recall with one multilingual model

A personal server and a hosted or cloud cell SHALL encode recall with `BAAI/bge-m3`, served from its pinned ONNX Runtime int8 artefact on CPU, and activation SHALL share that one resident instance. `EXOMEM_RECALL_MODEL` SHALL name another model explicitly on either. The hosted and cloud images SHALL carry that artefact and its tokenizer, and their build SHALL fail unless the model loads with the network closed and encodes at its declared width. The encoder SHALL read at most 512 tokens of a passage or query.

#### Scenario: A personal server loads one encoder

- **WHEN** a personal server has warmed recall and activation
- **THEN** one bge-m3 instance is resident and both encode with it

#### Scenario: A cell encodes with the multilingual model

- **WHEN** `EXOMEM_HOSTED_CELL` or `EXOMEM_CLOUD_CELL` is set and `EXOMEM_RECALL_MODEL` is not
- **THEN** recall encodes with `BAAI/bge-m3`

#### Scenario: The cell image carries the model its cells load

- **WHEN** the hosted or cloud image is built
- **THEN** its build stage and runtime name the model a cell resolves, and the build fails unless that model loads offline and encodes at its declared width

## MODIFIED Requirements

### Requirement: A new vector space is built beside the serving sidecar and cut over atomically

The serving recall sidecar SHALL be named by an active pointer beside it; with no pointer, `.embeddings.sqlite` serves. When the recall encoder is not the one that wrote the serving sidecar, the serving sidecar SHALL keep serving on a personal server, with its queries and writes encoded by the encoder that wrote it. That encoder SHALL be loaded by warm-up before writes are admitted, or by the re-embed job, and never by a request; a query that finds it cold SHALL report the vector lane `warming`. A hosted or cloud cell SHALL hold one encoder: it SHALL NOT load the encoder that wrote the serving sidecar, its vector lane SHALL be reported `unavailable` with reason `vector_space_mismatch` while the other lanes serve, and a write's encode for that sidecar SHALL fail soft and leave the page to the build.

A background job SHALL build a sidecar for the recall encoder's space beside the serving one, on a personal server and a cell alike, in committed batches, through the chunking used by live writes, one text per encode. It SHALL be resumable: a page is built when the new sidecar's rows carry its current mtime, and a restart SHALL NOT re-encode a built page. A page written during the build SHALL be encoded again before cutover. The cutover SHALL replace the active pointer in one atomic write, after catch-up passes, and SHALL take any write that landed in between; a failed cutover SHALL leave the old sidecar serving. The old sidecar SHALL be removed only by a later start of the job, never by the process that cut over, and only after one pass has caught the serving sidecar up with every page's current mtime. `EXOMEM_RECALL_REEMBED=off` SHALL build nothing and keep the old sidecar serving with its own encoder on a personal server. Doctor SHALL report the serving space and a build's progress from the sidecars on disk, and the runtime status SHALL report the running job's progress, rate and estimate.

#### Scenario: Recall serves from the old sidecar until the cutover

- **WHEN** an installed vault's sidecar holds English vectors and a personal server upgrades
- **THEN** dense recall keeps answering from that sidecar with the English encoder while the bge-m3 sidecar builds, and answers from the bge-m3 sidecar after the cutover

#### Scenario: A cell re-embeds with its one encoder

- **WHEN** a cell's sidecar holds English vectors and the cell starts on bge-m3
- **THEN** the English encoder is never loaded, the vector lane is `unavailable` with `vector_space_mismatch` while lexical recall answers, a page written meanwhile is in the bge-m3 sidecar after the cutover, and dense recall answers from the bge-m3 sidecar after the cutover

#### Scenario: A failed cutover changes nothing

- **WHEN** writing the active pointer fails
- **THEN** the old sidecar keeps serving with its encoder, and the next cutover attempt succeeds from the built sidecar

#### Scenario: The kill switch holds the old space

- **WHEN** `EXOMEM_RECALL_REEMBED=off` is set
- **THEN** no sidecar is built and the old sidecar keeps serving with its own encoder

## REMOVED Requirements

### Requirement: A personal server encodes recall with one multilingual model

**Reason**: Cells now encode with the same model, so the requirement covers every deployment and its cell scenario inverts.

**Migration**: Replaced by "Every deployment encodes recall with one multilingual model".
