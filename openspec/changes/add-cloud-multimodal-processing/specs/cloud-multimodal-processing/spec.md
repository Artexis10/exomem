## ADDED Requirements

### Requirement: A Cloud cell extracts media inside the cell

An Exomem Cloud cell SHALL extract documents, images, audio and video only in its serialized, disposable media worker, inside the cell's own container. The media bytes and the extraction process SHALL stay in the cell, and no third party SHALL receive media for extraction. Later reads of the stored extracted text SHALL follow the capabilities that own them, including an instrument placement that a tenant opts into under the `sensed-epistemic-model` capability. The serving process SHALL run no extraction engine. It MAY hold the image query encoder that the image lane needs, and the idle reaper SHALL unload that encoder.

#### Scenario: An image is extracted in its own cell

- **WHEN** a Cloud cell processes an uploaded image
- **THEN** extraction runs in that cell's media worker child
- **AND** no third party receives the image or its bytes for extraction

#### Scenario: The serving process holds no extraction engine

- **WHEN** media jobs are running in a Cloud cell
- **THEN** the serving process has loaded no OCR, speech, document or caption engine

### Requirement: Cloud engines are pre-baked in a Cloud-only build stage and load offline

Every engine model, OCR model and decoder that a Cloud cell uses SHALL be part of the cell image, and SHALL load with network access off. The engines and their dependencies SHALL be installed in a build stage that only the Cloud image uses, so that the Hosted image carries only the runtime it serves with. The image build SHALL fail unless each shipped engine loads offline and extracts a real sample of each format it serves; that build gate SHALL be the proof that Cloud supports the format. Each engine's model SHALL meet the rule of the `shared-model-runtime` capability. The Cloud image SHALL carry every script model that the OCR engine publishes, plus the language packs that the image build configures. That set SHALL be an image build parameter, never a list in code.

#### Scenario: A missing model fails the build

- **WHEN** an engine's model or OCR model is absent from the image, or loads only with network access
- **THEN** the image build fails before any cell runs the image

#### Scenario: A format that does not extract fails the build

- **WHEN** the Cloud image build cannot extract the real sample of a format that an engine serves
- **THEN** the image build fails, and no Cloud image claims that format

#### Scenario: A language pack is added without a code change

- **WHEN** the image build parameter adds an OCR language pack
- **THEN** the built image carries that pack and OCR uses it
- **AND** no source file changes

#### Scenario: The Hosted image gains no media engine

- **WHEN** the Hosted and Cloud images are built from one source revision
- **THEN** only the Cloud image carries the media engines and their dependencies

### Requirement: Media memory brakes derive from the cell's own cgroup

Media work in a Cloud cell SHALL be bounded by three brakes. Each brake SHALL be derived at run time from the cell's own cgroup, never from a fixed byte value, and each SHALL use the measure named here:

- **Admission** reads the cell's anonymous memory (`memory.stat` anon). The worker SHALL claim a job only while that memory plus the engine's measured anonymous-memory budget stays below the lower of two values: the cell's `memory.high`, when one is set, and the admission fraction of `memory.max` that the cell's service profile allows.
- **Pressure stop** reads the cell's memory pressure stall information (`memory.pressure`). The supervisor SHALL stop the media child when the `some` or `full` 10-second average crosses its stall threshold in deployment configuration. Where `memory.pressure` is unavailable, the supervisor SHALL instead stop the child when anonymous memory reaches the admission ceiling: the lower of `memory.high`, when one is set, and the profile's admission fraction of `memory.max`.
- **Hard limit** is a backstop against runaway allocation, not the main brake. The media child SHALL run under a data-segment limit (VmData, `RLIMIT_DATA`) equal to the engine's measured VmData budget plus a pinned margin. That budget SHALL include the engine's mapped weights. Native code that the child starts SHALL inherit the limit.

Neither admission nor the pressure stop SHALL read `memory.current`, which counts reclaimable page cache and model weights charged to whichever cell faulted them first. Each engine's anonymous-memory budget and VmData budget SHALL be measured at its acceptance and pinned in deployment configuration.

A pressure stop, and an allocation failure under the hard limit, SHALL each be a typed memory stop. The job SHALL return to pending and SHALL NOT be recorded as an artifact failure. After a bounded number of consecutive memory stops, the job SHALL leave the cycle in one of two typed states:

- **Exceeds this deployment's processing budget**, only when each of those stops was an allocation failure under the child's own VmData hard limit. A child that hits its hard limit SHALL exit, so each counted failure starts in a fresh child that carries no growth from earlier jobs. That failure is the only evidence about the file itself: anonymous demand above the engine's budget also raises VmData, so the hard limit catches an oversized file. The tenant SHALL see that the file exceeds this deployment's processing budget, with no install wording and no wording that calls the file corrupt. The job SHALL return to pending only when the cell's memory limit or that engine's budget changes.
- **Memory-blocked**, in every other case, including every case with a pressure stop. Pressure can come from the whole cell, such as backfill encoding in the serving process, a sensor child or an import checkpoint, so a pressure stop SHALL never lead to the over-budget state. The job SHALL return to pending without a human retry: when a supervisor starts, when the cell's limit or the engine's budget changes, and periodically with a bounded backoff while memory pressure is low. A tenant SHALL see it only as waiting, with no action to take, and its memory reason SHALL appear only on operator surfaces, such as doctor.

#### Scenario: No room means no claim

- **WHEN** the cell's anonymous memory plus the next engine's anonymous-memory budget reaches the lower of `memory.high` and the profile's admission fraction
- **THEN** the worker claims no job
- **AND** the queued jobs stay pending

#### Scenario: A lower memory.high governs admission

- **WHEN** the cell's `memory.high` is below the profile's admission fraction of `memory.max`
- **THEN** admission compares against `memory.high`

#### Scenario: Page cache does not block admission

- **WHEN** the cell's `memory.current` is high because of page cache or model weights it faulted first, while its anonymous memory leaves room for the engine's budget
- **THEN** the worker claims the job

#### Scenario: Memory pressure stops the child, and search keeps serving

- **WHEN** the cell's memory stall average crosses its threshold while a media job runs
- **THEN** the supervisor stops the media child and the job returns to pending
- **AND** the serving process keeps answering queries without a restart

#### Scenario: A cell without pressure information falls back to anonymous memory

- **WHEN** `memory.pressure` cannot be read and the cell's anonymous memory reaches the admission ceiling while a media job runs
- **THEN** the supervisor stops the media child and the job returns to pending

#### Scenario: An allocation failure under the hard limit is a memory stop

- **WHEN** the media child's allocation fails under its VmData limit, such as an ONNX Runtime `bad_alloc`
- **THEN** the job returns to pending with a typed memory reason
- **AND** neither the job nor its sidecar reports the artifact as failed or corrupt

#### Scenario: A file too large for an idle cell stops cycling

- **WHEN** a huge panorama fails allocation under the hard limit the bounded number of consecutive times
- **THEN** the job takes the state "exceeds this deployment's processing budget"
- **AND** the tenant sees that state, without install or corruption wording
- **AND** the job is not claimed again until the cell's memory limit or that engine's budget changes

#### Scenario: Pressure stops never mark a file over budget

- **WHEN** a job's consecutive stops include pressure stops, such as stops while an import checkpoint runs
- **THEN** the job becomes memory-blocked and recovers without a human
- **AND** it never takes the state "exceeds this deployment's processing budget"

#### Scenario: A memory-blocked job recovers without a human

- **WHEN** a job is memory-blocked and memory pressure later stays low
- **THEN** the supervisor returns the job to pending after its bounded backoff, without a human retry
- **AND** the tenant saw the job only as waiting, with no action to take

#### Scenario: The brakes follow a changed cell limit

- **WHEN** an operator changes a cell's memory limit
- **THEN** the admission ceiling, and the fallback stop that uses it, follow the new limit without a configuration change
- **AND** every memory-blocked or over-budget job in that cell returns to pending

### Requirement: Cloud reads every document type a personal install reads

A Cloud cell SHALL extract every document type that a personal install extracts: PDF, Word, Excel, PowerPoint, HTML, plain text, email and calendar files. It SHALL also extract the formats that every install gains under the `automatic-media-processing` capability. Formats that extract without a shipped dependency (plain text, email and calendar files) SHALL stay on and SHALL NOT depend on the documents switch, so they do not regress.

#### Scenario: An office document is searchable on Cloud

- **WHEN** a tenant uploads a Word document to a Cloud cell whose document engine is on
- **THEN** its text is extracted into the canonical sidecar and is found by search

#### Scenario: Plain text needs no switch

- **WHEN** a tenant uploads a plain text, email or calendar file to a Cloud cell whose documents switch is off
- **THEN** its text is still extracted and found by search

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

A Cloud cell MAY add one caption per image, written by a pinned-weight, frozen captioner that is chosen by the same selection rule as the image model. An instruction-following model SHALL NOT write captions. Captioning SHALL have its own engine switch. A caption SHALL be machine-owned extracted text in the image's sidecar, and search SHALL read it as it reads the image's OCR text. With captioning off, an image's extracted text SHALL be unchanged: its OCR text plus any enabled image tags.

#### Scenario: A caption makes a photo without text findable

- **WHEN** captioning is on and a photo with no legible text is processed
- **THEN** its sidecar holds one caption sentence as extracted text
- **AND** a query that describes the photo finds it through ordinary search

#### Scenario: Captioning off changes nothing

- **WHEN** the caption switch is off
- **THEN** no caption model is loaded
- **AND** each image's extracted text is its OCR text plus any enabled image tags, as before

### Requirement: The Cloud speech model is selected by the shared rule with speech terms

The Cloud speech engine and model SHALL be selected under the selection rule of the `shared-model-runtime` capability, with these terms fixed before any measurement:

1. Each language in the deployment's required speech language set SHALL be scored with its own metric: word error rate for a language written with spaces between words, and character error rate for a language written without them.
2. Candidates SHALL be compared on the same published benchmark wherever one covers them all.
3. When a published result is missing for any candidate in a required language, every candidate SHALL be measured for that language on the full FLEURS test split. That language SHALL be judged on those measurements only, and a measurement SHALL never be compared with a published result. Each difference from the best candidate SHALL be reported with its 95% bootstrap confidence interval.
4. The CPU speed measure SHALL be the int8 real-time factor at pinned CPU threads on the cell's hardware. The sanity check SHALL be ten utterances per required language.
5. The rule SHALL pick the lowest-memory candidate whose error rate is within 2.0 points of the best candidate in every required language. On a measured language, a candidate SHALL pass only when the upper end of its interval is within 2.0 points. Shareable weights SHALL count once per node.
6. Routing by language SHALL be allowed only when no single candidate passes, and only when the routed candidates together stay under 1.5 GB.

#### Scenario: The lightest candidate within the bound wins

- **WHEN** two candidates are within 2.0 points of the best error rate in every required language
- **THEN** the candidate with the lower measured memory is chosen

#### Scenario: A missing published result means every candidate is measured

- **WHEN** one candidate has no published result for a required language
- **THEN** every candidate is measured for that language on the full FLEURS test split
- **AND** no candidate's published result for that language enters the comparison

#### Scenario: Routing is a fallback, not a preference

- **WHEN** one candidate passes the error bound in every required language
- **THEN** speech is not routed by language

#### Scenario: A broken conversion is caught

- **WHEN** a candidate's int8 or ONNX conversion fails the ten-utterance sanity check in any required language
- **THEN** that candidate is not selected

### Requirement: Each Cloud engine is off by default and turns on after acceptance in a canary cell

Each engine (documents, OCR, image search, captions and speech) SHALL have its own switch. cellctl SHALL render the switch into each cell's environment, per cell. Every switch SHALL default to off. Personal installs SHALL keep their current defaults. Under the current cellctl render, changing a cell's switch re-renders that cell and restarts its pod, a brief outage. While a switch change restarts the pod, the canary and the rollout SHALL change switches outside the cell backup window.

An operator SHALL turn a switch on first in the acceptance cell, which is the owner's cell acting as the canary, and SHALL run that engine's acceptance there with the engine's backlog active. Acceptance SHALL require all of these:

- the latency, freshness and node headroom gates of the Cloud service profile hold;
- the cell's peak memory, read as anonymous memory plus non-reclaimable kernel and shared memory from `memory.stat` sampled at least once per second, stays within the service profile's peak fraction of the limit. The no-OOM gate below is the backstop for spikes shorter than the sampling interval;
- no process is killed for memory and the serving process does not restart, and the cell's `memory.oom.group` value is read and recorded;
- the owner-sized backlog drains within a drain-time bound stated before the run, and no job is left memory-blocked. Jobs that exceed the deployment's processing budget SHALL be excluded from the drain, and the acceptance report SHALL list each one with its file size and type;
- a labelled known-content subset for each format and engine agrees with a personal install's extraction of the same files, under an agreement bound stated before the run: documents and OCR images with known text, and speech clips with reference transcripts. Silent media SHALL produce its no-speech, no-audio or no-text marker;
- stored image vectors have the recorded model, width and precision, and meet the parity bound of the `shared-model-runtime` capability;
- the engine's weights are measured as shared on the node;
- the engine's anonymous-memory budget and VmData budget are recorded and pinned in deployment configuration.

When acceptance passes, the switch SHALL turn on in the other cells one cell at a time. When any gate misses, the switch SHALL be turned off in the acceptance cell and SHALL stay off in every other cell. Turning a switch off SHALL stop new jobs for that engine, and SHALL leave stored sidecars and vectors valid.

#### Scenario: A missed gate keeps the engine off

- **WHEN** an engine's acceptance run in the owner's cell exceeds the profile's peak fraction on anonymous and non-reclaimable memory
- **THEN** the switch is turned off in the owner's cell
- **AND** it stays off in every other Cloud cell

#### Scenario: Page cache alone does not fail acceptance

- **WHEN** large media reads fill the cell's page cache up to its limit while anonymous and non-reclaimable memory stay within the peak fraction
- **THEN** the memory gate passes

#### Scenario: A file over budget does not block acceptance

- **WHEN** one file of the owner's backlog exceeds the deployment's processing budget
- **THEN** the drain gate excludes that job
- **AND** the acceptance report lists it with its file size and type

#### Scenario: Silent media is expected to be silent

- **WHEN** the known-content subset includes a video without an audio stream
- **THEN** acceptance expects its no-audio marker, not a transcript

#### Scenario: A passed gate rolls out one cell at a time

- **WHEN** an engine passes its acceptance in the owner's cell
- **THEN** its switch turns on in the other cells one cell at a time, each outside the backup window

#### Scenario: Turning an engine off is a safe rollback

- **WHEN** an operator turns an engine's switch off in a cell
- **THEN** no new job for that engine starts in that cell
- **AND** sidecars and vectors that the engine already wrote keep serving search

### Requirement: A disabled Cloud engine is reported as disabled

An engine whose switch is off, or which the deployment does not ship, SHALL be reported as disabled on runtime status and doctor. It SHALL NOT be reported to a tenant as missing software, and no tenant-facing surface SHALL tell a tenant to install anything. Its media SHALL keep its pending sidecar without a job, as the `automatic-media-processing` capability states, and queries SHALL treat its lane as the `instant-start` capability states for a disabled lane.

#### Scenario: Status names the engine as disabled

- **WHEN** a tenant reads runtime status on a cell whose speech switch is off
- **THEN** speech is reported as disabled
- **AND** no install instruction appears

### Requirement: Existing media is backfilled one cell at a time

When an engine's switch turns on in a cell, the media already in that cell SHALL be queued for that engine automatically, with no operator action per file. Because switches turn on one cell at a time, backfill SHALL never run in every cell at once. A change of image space SHALL re-encode the stored image vectors once, through the same backfill.

#### Scenario: Turning on OCR processes the images already stored

- **WHEN** the OCR switch turns on in a cell that already stores images without OCR text
- **THEN** each of those images is queued once and its OCR text is extracted

#### Scenario: Backfill is not fleet-wide

- **WHEN** an engine is turned on across several cells
- **THEN** the cells never start that engine's backfill all at once
