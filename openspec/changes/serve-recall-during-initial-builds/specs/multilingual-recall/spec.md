## MODIFIED Requirements

### Requirement: A recall sidecar records its vector space and nothing mixes spaces

Every recall sidecar SHALL record the encoder that wrote it: the model, the encoder's fingerprint when a profiled encoder was resident (for a served model this names its revision, quantisation, file format and artefact digest), and the vector width. The width SHALL be read from that record wherever vectors are read, stored, mirrored into vec0 or reused, including the claims sidecar and warm-up. A sidecar with rows and no record SHALL be read as `BAAI/bge-base-en-v1.5` at 768 dimensions.

A write of another width SHALL be refused and leave the sidecar unchanged. A query SHALL be encoded for the sidecar the vector lane reads: the serving sidecar, or the build's sidecar while a build serves recall. It SHALL be encoded after that sidecar's encoder is checked against its record and before anything is encoded; when no encoder here serves that space, or a query vector encoded for one sidecar meets another, the vector lane SHALL be reported `unavailable` with reason `vector_space_mismatch`, and the lexical, keyword, graph and temporal lanes SHALL serve. Two fingerprints SHALL be compared exactly when both are known. The claims sidecar SHALL serve nothing from another space and SHALL re-encode every claim under the new one.

#### Scenario: A 1024-dimension sidecar round-trips

- **WHEN** bge-m3 vectors are written to an empty sidecar and read back by a new index
- **THEN** its record names bge-m3 at 1024 dimensions, and search, the matrix, vec0 and stored-vector reuse all read 1024-dimension rows

#### Scenario: Another build of the same model is refused

- **WHEN** the resident encoder is the sidecar's model but runs bytes with another artefact digest, and no build in the resident encoder's space serves recall
- **THEN** the vector lane is `unavailable` with `vector_space_mismatch`, the query is never encoded, and lexical recall answers

### Requirement: A new vector space is built beside the serving sidecar and cut over atomically

The serving recall sidecar SHALL be named by an active pointer beside it; with no pointer, `.embeddings.sqlite` serves. When another model wrote the serving sidecar, the serving sidecar SHALL keep serving on a personal server, with its queries and writes encoded by the encoder that wrote it. That encoder SHALL be loaded by warm-up before writes are admitted, or by the re-embed job, and never by a request; a query that finds it cold SHALL report the vector lane `warming`. A hosted or cloud cell SHALL hold one encoder: it SHALL NOT load the encoder that wrote the serving sidecar, a query SHALL never be encoded for that sidecar, its vector lane SHALL be reported `unavailable` with reason `vector_space_mismatch` while no build serves, and a write's encode for that sidecar SHALL fail soft and leave the page to the build.

A background job SHALL build a sidecar for the recall encoder's space beside the serving one, on a personal server and a cloud cell, in committed batches, through the chunking used by live writes, one text per encode, as bulk model work. It SHALL be resumable: a page is built when the new sidecar's rows carry its current mtime, and a restart SHALL NOT re-encode a built page. A page written during the build SHALL be encoded again before cutover. The cutover SHALL replace the active pointer in one atomic write, after catch-up passes, and SHALL take any write that landed in between; a failed cutover SHALL leave the old sidecar serving. The old sidecar SHALL be removed only by a later start of the job, never by the process that cut over, and only after one pass has caught the serving sidecar up with every page's current mtime. `EXOMEM_RECALL_REEMBED=off` SHALL build nothing: a personal server keeps the old sidecar serving with its own encoder, and a cell keeps its vector lane `unavailable` until the switch is lifted.

While a sidecar the job built in this process, completely or until it failed, is not named by the active pointer, the vector lane SHALL read it whenever the recall encoder cannot answer from the serving sidecar: during an initial build, on a hosted or cloud cell, and on a personal server whose serving sidecar another build of the same model wrote. The query SHALL then be encoded for the build's sidecar. The lane SHALL also read the serving sidecar when that sidecar records exactly the build's space (model, fingerprint and width), so that writes since the build began count. A page's rows in the build's sidecar SHALL answer only while they carry the page's current mtime: a page changed since the build encoded it SHALL answer from the serving sidecar alone, or from the lexical lanes, until a catch-up pass encodes it again. A page current in both sidecars SHALL keep its best score. Sidecars of different spaces SHALL never meet one query vector. Such a response SHALL add `embeddings` to `warming.components`, with `warming.since_s` null, and SHALL carry no other field about the build; a semantic-unit recall during the build SHALL carry the same mark. After the cutover the vector lane SHALL read only the serving sidecar and SHALL NOT mark recall warming. On a service cell, a query's encode SHALL wait for at most the one build passage in flight; on every install, a request SHALL take no model-slot turn to find a resident encoder.

Doctor SHALL report the serving space and a build's progress from the sidecars on disk. It SHALL report an initial build as in progress also when a live write has given the serving sidecar the build's space, but only while the job's rule would resume that build; a build sidecar the job will not resume SHALL be reported as left over, neither as a build in progress nor as a pass. It SHALL report a cell's refused sidecar as not serving, with dense recall limited to the pages built so far. The runtime status SHALL report a failed build as failed; a restart SHALL retry it. The runtime status SHALL report the running job's progress, rate and estimate. Progress SHALL appear only on these operator surfaces.

#### Scenario: Recall serves from the old sidecar until the cutover

- **WHEN** an installed vault's sidecar holds English vectors and a personal server upgrades
- **THEN** dense recall keeps answering from that sidecar with the English encoder while the bge-m3 sidecar builds, and answers from the bge-m3 sidecar after the cutover

#### Scenario: A cloud cell re-embeds with its one encoder

- **WHEN** a cloud cell's sidecar holds English vectors and the cell starts on bge-m3
- **THEN** the English encoder is never loaded, the vector lane is `unavailable` with `vector_space_mismatch` until the build starts and then reads the bge-m3 sidecar with recall marked warming, doctor warns that dense recall covers only the built pages, a page written meanwhile is in the bge-m3 sidecar after the cutover, and dense recall answers from the bge-m3 sidecar after the cutover without the mark

#### Scenario: A vault present before the first start is embedded without an operator

- **WHEN** a personal server or cloud cell starts over pages and semantic units already present in its vault, with no embedding sidecar or an empty one and no write receipts
- **THEN** its background job builds every eligible chunk and semantic unit in the recall encoder's space using committed batches, resumes without re-encoding committed work after interruption, catches up and atomically publishes the active pointer, and dense recall participates after cutover without an operator or a second encoder; during the build dense recall answers from the pages built so far with recall marked warming, and doctor reports the pending build or its progress rather than recommending CLI reconcile

#### Scenario: An initial build answers with its built pages and live writes

- **WHEN** an initial build has embedded part of the vault, a live write has given the serving sidecar the build's space, and no pointer names the build
- **THEN** a vector query answers from the build's sidecar and the serving sidecar together, a page only the build holds and the live-written page are both found, the response adds `embeddings` to `warming.components` and no other field, and after the cutover the same query answers without the mark

#### Scenario: A page changed after the build encoded it does not answer from its old rows

- **WHEN** the build has encoded a page, the page is rewritten and a live write puts its new text in the serving sidecar, and no catch-up pass has run
- **THEN** a vector query for the removed text does not return the removed text, and a query for the new text returns the page with its new text

#### Scenario: A personal server reads a build of its own model

- **WHEN** a personal server's serving sidecar was written by another build of the recall model and the job builds a sidecar with the resident build
- **THEN** the query is encoded for the build's sidecar, the vector lane reads it with recall marked warming, and the serving sidecar is never searched with that vector

#### Scenario: A failed build keeps serving what it built

- **WHEN** the build fails before its cutover
- **THEN** the vector lane keeps reading the pages it built, recall stays marked warming, the runtime status reports the build failed, and the next start retries it

#### Scenario: A leftover build sidecar is not a build in progress

- **WHEN** a build sidecar in the serving sidecar's space sits beside a serving sidecar that covers the vault, so the job will not resume the build
- **THEN** doctor reports the build sidecar as left over and not as a build in progress

#### Scenario: A query waits for at most one build passage

- **WHEN** a query arrives on a service cell while the build is encoding
- **THEN** its encode runs after the build passage in flight and before the build's next passage

#### Scenario: A live write does not orphan an interrupted initial build

- **WHEN** an initial build is interrupted, including before its first batch commits, and a live write has given the legacy sidecar the recall encoder's identity, with incomplete corpus coverage, no published active pointer and a separate target-space shadow sidecar beside the active one
- **THEN** the next start resumes the initial shadow build without re-encoding committed batches, covers every eligible page and semantic unit, and reports current only after atomic cutover

#### Scenario: A matching serving sidecar needs no startup coverage scan

- **WHEN** the serving sidecar accepts the recall encoder's identity and either an active pointer is published or no separate target-space shadow sidecar exists
- **THEN** planning enumerates no pages and performs no per-page semantic coverage checks, and ordinary drift in a legacy sidecar does not trigger a full shadow build

#### Scenario: Disabled or unavailable embeddings do not start a build

- **WHEN** embeddings are disabled or the optional serving stack is unavailable
- **THEN** the job reports a non-failure disabled or unavailable state with the existing serving-space details without loading an encoder or fetching its artifact; a served artifact requires ONNX Runtime and tokenizers even when Torch is the preferred backend

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
