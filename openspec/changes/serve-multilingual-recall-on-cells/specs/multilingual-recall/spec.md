## ADDED Requirements

### Requirement: A personal server and a cloud cell encode recall with one multilingual model

A personal server and a cloud cell SHALL encode recall with `BAAI/bge-m3`, served from its pinned ONNX Runtime int8 artefact on CPU, and activation SHALL share that one resident instance. A hosted cell SHALL keep `BAAI/bge-base-en-v1.5`; a malformed hosted-cell flag SHALL be read as a hosted cell. `EXOMEM_RECALL_MODEL` SHALL name another model explicitly on any of them. The hosted image SHALL carry the English model and the cloud image SHALL carry bge-m3, and each model's build SHALL fail unless it loads with the network closed and encodes at its declared width. The encoder SHALL read at most 512 tokens of a passage or query.

#### Scenario: A personal server loads one encoder

- **WHEN** a personal server has warmed recall and activation
- **THEN** one bge-m3 instance is resident and both encode with it

#### Scenario: A cloud cell encodes with the multilingual model

- **WHEN** `EXOMEM_CLOUD_CELL` is set and `EXOMEM_RECALL_MODEL` is not
- **THEN** recall encodes with `BAAI/bge-m3`

#### Scenario: A hosted cell keeps the English model

- **WHEN** `EXOMEM_HOSTED_CELL` is set to anything but a false value and `EXOMEM_RECALL_MODEL` is not
- **THEN** recall encodes with `BAAI/bge-base-en-v1.5`

#### Scenario: Each cell image carries the model its cells load

- **WHEN** the hosted and cloud images are built
- **THEN** each names the model its cells resolve, and the build fails unless that model loads offline and encodes at its declared width

## MODIFIED Requirements

### Requirement: A new vector space is built beside the serving sidecar and cut over atomically

The serving recall sidecar SHALL be named by an active pointer beside it; with no pointer, `.embeddings.sqlite` serves. When the recall encoder is not the one that wrote the serving sidecar, the serving sidecar SHALL keep serving on a personal server, with its queries and writes encoded by the encoder that wrote it. That encoder SHALL be loaded by warm-up before writes are admitted, or by the re-embed job, and never by a request; a query that finds it cold SHALL report the vector lane `warming`. A hosted or cloud cell SHALL hold one encoder: it SHALL NOT load the encoder that wrote the serving sidecar, its vector lane SHALL be reported `unavailable` with reason `vector_space_mismatch` while the other lanes serve, and a write's encode for that sidecar SHALL fail soft and leave the page to the build.

A background job SHALL build a sidecar for the recall encoder's space beside the serving one, on a personal server and a cloud cell, in committed batches, through the chunking used by live writes, one text per encode. It SHALL be resumable: a page is built when the new sidecar's rows carry its current mtime, and a restart SHALL NOT re-encode a built page. A page written during the build SHALL be encoded again before cutover. The cutover SHALL replace the active pointer in one atomic write, after catch-up passes, and SHALL take any write that landed in between; a failed cutover SHALL leave the old sidecar serving. The old sidecar SHALL be removed only by a later start of the job, never by the process that cut over, and only after one pass has caught the serving sidecar up with every page's current mtime. `EXOMEM_RECALL_REEMBED=off` SHALL build nothing: a personal server keeps the old sidecar serving with its own encoder, and a cell keeps its vector lane `unavailable` until the switch is lifted. Doctor SHALL report the serving space and a build's progress from the sidecars on disk, and SHALL report a cell's refused sidecar as dense recall off rather than as serving; the runtime status SHALL report the running job's progress, rate and estimate.

#### Scenario: Recall serves from the old sidecar until the cutover

- **WHEN** an installed vault's sidecar holds English vectors and a personal server upgrades
- **THEN** dense recall keeps answering from that sidecar with the English encoder while the bge-m3 sidecar builds, and answers from the bge-m3 sidecar after the cutover

#### Scenario: A cloud cell re-embeds with its one encoder

- **WHEN** a cloud cell's sidecar holds English vectors and the cell starts on bge-m3
- **THEN** the English encoder is never loaded, the vector lane is `unavailable` with `vector_space_mismatch` while lexical recall answers, doctor warns that dense recall is off until the cutover, a page written meanwhile is in the bge-m3 sidecar after the cutover, and dense recall answers from the bge-m3 sidecar after the cutover

#### Scenario: A vault present before the first start is embedded without an operator

- **WHEN** a personal server or cloud cell starts over pages and semantic units already present in its vault, with no embedding sidecar or an empty one and no write receipts
- **THEN** its background job builds every eligible chunk and semantic unit in the recall encoder's space using committed batches, resumes without re-encoding committed work after interruption, catches up and atomically publishes the active pointer, and dense recall participates after cutover without an operator or a second encoder; lexical recall serves during the build and doctor reports the pending build or its progress rather than recommending CLI reconcile

#### Scenario: A live write does not orphan an interrupted initial build

- **WHEN** an initial build is interrupted after a live write has given the legacy sidecar the recall encoder's identity, with incomplete corpus coverage and no published active pointer
- **THEN** the next start resumes the initial shadow build without re-encoding committed batches, covers every eligible page and semantic unit, and reports current only after atomic cutover

#### Scenario: Disabled or unavailable embeddings do not start a build

- **WHEN** embeddings are disabled or the optional serving stack is unavailable
- **THEN** the job reports a non-failure disabled or unavailable state without loading an encoder or fetching its artifact

#### Scenario: An empty embedding corpus needs no encoder

- **WHEN** a vault contains no eligible chunk-bearing pages
- **THEN** the job is current without loading an encoder, repeated starts plan no build, and doctor emits no initial-build warning

#### Scenario: A failed cutover changes nothing

- **WHEN** writing the active pointer fails
- **THEN** the old sidecar keeps serving with its encoder, and the next cutover attempt succeeds from the built sidecar

#### Scenario: The kill switch holds the old space

- **WHEN** `EXOMEM_RECALL_REEMBED=off` is set on a personal server
- **THEN** no sidecar is built and the old sidecar keeps serving with its own encoder

#### Scenario: The kill switch on a cell keeps dense recall off

- **WHEN** `EXOMEM_RECALL_REEMBED=off` is set on a cell whose sidecar another model wrote
- **THEN** no sidecar is built, the vector lane stays `unavailable` with `vector_space_mismatch`, and doctor says the switch keeps dense recall off

## REMOVED Requirements

### Requirement: A personal server encodes recall with one multilingual model

**Reason**: Cloud cells now encode with the same model, so the requirement covers personal servers and cloud cells and names what hosted cells keep.

**Migration**: Replaced by "A personal server and a cloud cell encode recall with one multilingual model".
