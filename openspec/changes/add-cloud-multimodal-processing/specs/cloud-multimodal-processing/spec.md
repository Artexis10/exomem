## ADDED Requirements

### Requirement: A Cloud cell processes media inside the cell

An Exomem Cloud cell SHALL extract documents, images, audio and video only in its serialized, disposable media worker, inside the cell's own container. Media and the text extracted from it SHALL NOT leave the cell for processing, and no third-party service SHALL receive them. The serving process SHALL run no extraction engine. It MAY hold the image query encoder that the image lane needs, and the idle reaper SHALL unload that encoder.

#### Scenario: An image is processed in its own cell

- **WHEN** a Cloud cell processes an uploaded image
- **THEN** extraction runs in that cell's media worker child
- **AND** no request carrying the image or its text leaves the cell

#### Scenario: The serving process holds no extraction engine

- **WHEN** media jobs are running in a Cloud cell
- **THEN** the serving process has loaded no OCR, speech, document or caption engine

### Requirement: Cloud engines are pre-baked and load offline

Every engine model, OCR model and decoder that a Cloud cell uses SHALL be part of the cell image, and SHALL load with network access off. The image build SHALL fail unless each shipped engine loads offline and processes a real sample. Each engine's model SHALL meet the rule of the `shared-model-runtime` capability: a pinned artifact whose weights cells on one node share read-only. The Cloud image SHALL carry every script model that the OCR engine publishes, plus the language packs that the image build configures. That set SHALL be an image build parameter, never a list in code.

#### Scenario: A missing model fails the build

- **WHEN** an engine's model or OCR model is absent from the image, or loads only with network access
- **THEN** the image build fails before any cell runs the image

#### Scenario: A language pack is added without a code change

- **WHEN** the image build parameter adds an OCR language pack
- **THEN** the built image carries that pack and OCR uses it
- **AND** no source file changes

### Requirement: Media memory brakes derive from the cell's own limits

Media work in a Cloud cell SHALL be bounded by three brakes. Each brake SHALL be derived at run time from the cell's own cgroup memory limit and current usage, never from a fixed byte value:

- **Admission.** The worker SHALL claim a job only while the cell's current memory plus the engine's measured budget stays at or below the peak fraction that the cell's service profile allows.
- **Hard limit.** The media child SHALL run under a per-process limit on private memory equal to the engine's budget, and native code it starts SHALL inherit that limit. File-backed shared weights SHALL NOT count against it.
- **Pressure stop.** The supervisor SHALL stop the child when the cell's memory crosses a high-water mark, set as a fraction of the cell's limit in deployment configuration.

Each engine's budget SHALL be measured at its acceptance and pinned in deployment configuration. A job that a brake stops SHALL return to pending and SHALL NOT be recorded as an artifact failure. After a bounded number of consecutive stops, the job SHALL become blocked with a typed memory reason, and SHALL never become failed for that reason.

#### Scenario: No room means no claim

- **WHEN** the cell's current memory plus the next engine's budget exceeds the allowed peak fraction
- **THEN** the worker claims no job
- **AND** the queued jobs stay pending

#### Scenario: Memory pressure stops the child, and search keeps serving

- **WHEN** cell memory crosses the high-water mark while a media job runs
- **THEN** the supervisor stops the media child and the job returns to pending
- **AND** the serving process keeps answering queries without a restart

#### Scenario: The brakes follow a changed cell limit

- **WHEN** an operator changes a cell's memory limit
- **THEN** admission, the hard limit and the high-water mark follow the new limit without a configuration change

#### Scenario: Repeated stops block the job instead of looping

- **WHEN** a job is stopped by a brake the bounded number of consecutive times
- **THEN** the job becomes blocked with a typed memory reason
- **AND** it is not recorded as failed, and its artifact is not reported as corrupt

### Requirement: Cloud reads every document type a personal install reads

A Cloud cell SHALL extract every document type that a personal install extracts: PDF, Word, Excel, PowerPoint, HTML, plain text, email and calendar files. It SHALL also extract the formats that every install gains under the `automatic-media-processing` capability.

#### Scenario: An office document is searchable on Cloud

- **WHEN** a tenant uploads a Word document to a Cloud cell whose document engine is on
- **THEN** its text is extracted into the canonical sidecar and is found by search

### Requirement: The Cloud image model is chosen by published retrieval results within the media budget

The image model SHALL be selected under the selection rule of the `shared-model-runtime` capability. Its accuracy evidence SHALL be published multilingual image–text retrieval results for the deployment's required languages. Among the candidates whose measured CPU speed and peak memory on the cell's hardware fit the media budget, and whose weights cells can share, the candidate with the best published results SHALL be chosen.

#### Scenario: A stronger model that does not fit is not chosen

- **WHEN** the candidate with the best published retrieval results exceeds the media budget on the cell's hardware
- **THEN** the best candidate that fits the budget is chosen instead

#### Scenario: The choice records its evidence

- **WHEN** an image model is selected
- **THEN** the selection record cites the published results with their dates
- **AND** it records the measured speed, peak memory and sharing on the cell's hardware

### Requirement: Image captions are optional and have their own switch

A Cloud cell MAY add one caption per image, written by a pinned-weight, frozen captioner that is chosen by the same selection rule as the image model. An instruction-following model SHALL NOT write captions. Captioning SHALL have its own deployment switch, which SHALL stay off until the caption acceptance passes. A caption SHALL be machine-owned extracted text in the image's sidecar, and search SHALL read it as it reads the image's OCR text. With captioning off, image extraction SHALL be byte-identical to OCR-only extraction.

#### Scenario: A caption makes a photo without text findable

- **WHEN** captioning is on and a photo with no legible text is processed
- **THEN** its sidecar holds one caption sentence as extracted text
- **AND** a query that describes the photo finds it through ordinary search

#### Scenario: Captioning off changes nothing

- **WHEN** the caption switch is off
- **THEN** no caption model is loaded
- **AND** each image's extracted text equals its OCR-only extraction

### Requirement: The Cloud speech model follows a frozen selection rule

The Cloud speech engine and model SHALL be selected by this rule, which is fixed before any measurement:

1. Accuracy SHALL come from published results for each language in the deployment's required speech language set.
2. Exomem SHALL measure only the int8 real-time factor at pinned CPU threads on the cell's hardware, the peak memory, and whether the weights can be shared.
3. A sanity check of ten utterances per required language SHALL catch a broken int8 or ONNX conversion.
4. The rule SHALL pick the lowest-memory candidate whose published error rate is within 2.0 points of the best candidate in every required language. Shareable weights SHALL count once per node.
5. Routing by language SHALL be allowed only when no single candidate passes, and only when the routed candidates together stay under 1.5 GB.

#### Scenario: The lightest candidate within the bound wins

- **WHEN** two candidates are within 2.0 points of the best published error rate in every required language
- **THEN** the candidate with the lower measured memory is chosen

#### Scenario: Routing is a fallback, not a preference

- **WHEN** one candidate passes the error bound in every required language
- **THEN** speech is not routed by language

#### Scenario: A broken conversion is caught

- **WHEN** a candidate's int8 or ONNX conversion fails the ten-utterance sanity check in any required language
- **THEN** that candidate is not selected

### Requirement: Each Cloud engine stays off until its acceptance passes on a real cell

Each engine (documents, OCR, image search, captions and speech) SHALL have its own deployment switch. On Cloud, each switch SHALL stay off until that engine's acceptance passes on a real cell. Personal installs SHALL keep their current defaults. Acceptance SHALL run on the owner-sized cell with that engine's backlog active, and SHALL require all of these:

- the outcome and capacity gates of the Cloud service profile hold;
- no process is killed for memory and the serving process does not restart, and the cell's `memory.oom.group` value is read and recorded;
- outputs match a personal install's outputs in shape, and image vectors meet the parity bound of the `shared-model-runtime` capability;
- the engine's weights are measured as shared on the node;
- the engine's measured memory budget is recorded and pinned in deployment configuration.

A missed gate SHALL keep the switch off. Turning a switch off SHALL stop new jobs for that engine, and SHALL leave stored sidecars and vectors valid.

#### Scenario: A missed gate keeps the engine off

- **WHEN** an engine's acceptance run exceeds the service profile's memory gate
- **THEN** that engine's switch stays off in every Cloud cell

#### Scenario: Turning an engine off is a safe rollback

- **WHEN** an operator turns an engine's switch off
- **THEN** no new job for that engine starts
- **AND** sidecars and vectors that the engine already wrote keep serving search

### Requirement: A disabled Cloud engine is reported as disabled

An engine whose switch is off, or which the deployment does not ship, SHALL be reported as disabled on runtime status and doctor. It SHALL NOT be reported to a tenant as missing software, and no tenant-facing surface SHALL tell a tenant to install anything. Its media SHALL keep its pending sidecar without a job, as the `automatic-media-processing` capability states, and queries SHALL treat its lane as the `instant-start` capability states for a disabled lane.

#### Scenario: Status names the engine as disabled

- **WHEN** a tenant reads runtime status on a cell whose speech switch is off
- **THEN** speech is reported as disabled
- **AND** no install instruction appears

### Requirement: Existing media is backfilled one cell at a time

When an engine's switch turns on in a cell, the media already in that cell SHALL be queued for that engine automatically, with no operator action per file. Rollout SHALL turn an engine on one cell at a time, so that backfill never runs in every cell at once. A change of image model SHALL re-encode the stored image vectors once, through the same backfill.

#### Scenario: Turning on OCR processes the images already stored

- **WHEN** the OCR switch turns on in a cell that already stores images without OCR text
- **THEN** each of those images is queued once and its OCR text is extracted

#### Scenario: Backfill is not fleet-wide

- **WHEN** a release turns an engine on across several cells
- **THEN** the switch changes in one cell at a time
- **AND** the cells never start that engine's backfill all at once
