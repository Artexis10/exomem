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

Every engine model, OCR model and decoder that a Cloud cell uses SHALL be part of the cell image, and SHALL load with network access off. The engines and their dependencies SHALL be installed in a build stage that only the Cloud image uses. The image build SHALL fail unless each shipped engine loads offline and extracts a real sample of each format it serves; that build gate SHALL be the proof that Cloud supports the format. Each engine's model SHALL meet the rule of the `shared-model-runtime` capability. The Cloud image SHALL carry every script model that the OCR engine publishes, plus the language packs that the image build configures. That set SHALL be an image build parameter, never a list in code.

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

### Requirement: Media memory brakes derive from the cell's own cgroup

Media work in a Cloud cell SHALL be bounded by three brakes. Each brake SHALL be derived at run time from the cell's own cgroup, never from a fixed byte value, and each SHALL use the measure named here:

- **Admission** reads the cell's anonymous memory (`memory.stat` anon). The worker SHALL claim a job only while that memory plus the engine's measured anonymous-memory budget stays below the lower of two values: the cell's `memory.high`, when one is set, and the admission fraction of `memory.max` that the cell's service profile allows.
- **Pressure stop** reads the cell's memory pressure stall information (`memory.pressure`). The supervisor SHALL stop the media child when the `some` or `full` 10-second average crosses its stall threshold in deployment configuration. Where `memory.pressure` is unavailable, the supervisor SHALL instead stop the child when anonymous memory reaches the admission ceiling: the lower of `memory.high`, when one is set, and the profile's admission fraction of `memory.max`.
- **Hard limit** is a backstop against runaway allocation, not the main brake. The media child SHALL run under a data-segment limit (VmData, `RLIMIT_DATA`) equal to the engine's measured VmData budget plus a pinned margin. That budget SHALL include the engine's mapped weights. Native code that the child starts SHALL inherit the limit.

Neither admission nor the pressure stop SHALL read `memory.current`, which counts reclaimable page cache and model weights charged to whichever cell faulted them first. Each engine's anonymous-memory budget and VmData budget SHALL be measured at its acceptance and pinned in deployment configuration.

A pressure stop, and an allocation failure under the hard limit, SHALL each be a typed memory stop. The job SHALL return to pending and SHALL NOT be recorded as an artifact failure. After a bounded number of consecutive memory stops, the job SHALL leave the cycle in one of two typed states:

- **Exceeds this deployment's processing budget**, only when each of those stops was an allocation failure under the child's own VmData hard limit. A child that hits its hard limit SHALL exit, so each counted failure starts in a fresh child that carries no growth from earlier jobs. That failure is the only evidence about the file itself: anonymous demand above the engine's budget also raises VmData, so the hard limit catches an oversized file. The tenant SHALL see that the file exceeds this deployment's processing budget, with no install wording and no wording that calls the file corrupt. The job SHALL return to pending only when the cell's memory limit or that engine's budget changes.
- **Memory-blocked**, in every other case, including every case with a pressure stop. Pressure can come from the whole cell, such as backfill encoding in the serving process, a sensor child or an import checkpoint, so a pressure stop SHALL never lead to the over-budget state. The job SHALL return to pending without a human retry: when a supervisor starts, when the cell's limit or the engine's budget changes, and periodically with a bounded backoff while memory pressure is low. A tenant SHALL see it only as waiting, with no action to take, and its memory reason SHALL appear only on operator surfaces, such as doctor.

A job that runs longer than the configured job timeout SHALL be stopped by the supervisor. That stop SHALL count separately from memory stops. After the same bounded number of timeout stops, each in a fresh child, the job SHALL take the state "exceeds this deployment's processing budget", with the timeout it exceeded recorded for operators. It SHALL return to pending only when the configured job timeout becomes larger. A file that never finishes within the timeout therefore holds the queue for at most that bound, then lets later jobs run.

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

#### Scenario: A job that always outruns the timeout stops holding the queue

- **WHEN** a scanned book always runs longer than the configured job timeout, and a quick job is queued behind it
- **THEN** after the bounded number of timeout stops the book takes the state "exceeds this deployment's processing budget"
- **AND** the quick job then runs and completes
- **AND** the book returns to pending only after the configured job timeout is raised

#### Scenario: A memory-blocked job recovers without a human

- **WHEN** a job is memory-blocked and memory pressure later stays low
- **THEN** the supervisor returns the job to pending after its bounded backoff, without a human retry
- **AND** the tenant saw the job only as waiting, with no action to take

#### Scenario: The brakes follow a changed cell limit

- **WHEN** an operator changes a cell's memory limit
- **THEN** the admission ceiling, and the fallback stop that uses it, follow the new limit without a configuration change
- **AND** every memory-blocked or over-budget job in that cell returns to pending

### Requirement: A node media-slot lease caps heavy media work across cells

Where a node media-slot lease is switched on for a cell, the cell's supervisor SHALL hold a slot from the lease service before it starts a media child, and SHALL release the slot when the child exits. The service SHALL grant slots so that the anonymous-memory bytes it has granted add up to at most the node media pool, in strict first-in first-out order across cells, with at most one queued or held entry for each cell and for each source address.

The cell SHALL send the byte count and the job timeout of the job its child will claim, computed with the same function the child uses, which covers every engine the child loads. The service SHALL hold no copy of the cell's budgets or timeout. It SHALL refuse a byte count above the pool and a timeout above its configured maximum, with a logged reason, and it SHALL NOT shorten a timeout. A lease SHALL last the stated timeout plus a margin, counted from the first reply that tells the cell about the grant. After a restart the service SHALL grant nothing for the longest possible lease.

The child SHALL claim no job that needs more anonymous memory than its slot reserves. It SHALL exit with a code that counts no stop, and the supervisor SHALL release the slot and ask again for the size it now sees.

A job whose slot is not granted, or whose service does not answer, SHALL wait as a memory-blocked job waits: no stop is counted and the tenant sees nothing. A cell that is not switched on SHALL behave as it did before the lease existed.

#### Scenario: Granted budgets never exceed the pool

- **WHEN** a cell holds a slot that fills most of the pool and another cell asks for a slot that does not fit beside it
- **THEN** the second cell waits until the first releases or its lease ends

#### Scenario: A lease outlasts the child it was granted for

- **WHEN** a cell learns of its grant up to the queue window after the grant, and starts its child then
- **THEN** its slot lasts the stated job timeout plus the margin from that moment
- **AND** the next cell is granted nothing while the child may still run

#### Scenario: An unreachable service leaves the job pending

- **WHEN** the lease service does not answer
- **THEN** the job stays pending and no child starts
- **AND** no stop is counted

#### Scenario: An interrupted heavier job asks for its own slot

- **WHEN** a cell restarts, and the child it starts under a slot sized for a lighter job recovers a heavier interrupted job
- **THEN** the child claims nothing and exits without counting a stop
- **AND** the supervisor releases the slot and the heavier job runs under a slot sized for it

#### Scenario: A job that loads the image tower reserves the tower

- **WHEN** the next job loads the CLIP tower beside its stage engine
- **THEN** the cell asks for the anonymous bytes of both

#### Scenario: A restart grants nothing for one longest lease

- **WHEN** the lease service restarts while a child holds a slot from the earlier service
- **THEN** it grants nothing until the longest possible lease has ended

#### Scenario: One address cannot fill the queue

- **WHEN** a source address that already has an entry asks under another cell ID
- **THEN** the service refuses the request

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

The initial Cloud speech default SHALL use the pinned Whisper large-v3-turbo int8 ONNX artifact in the media model registry. It SHALL detect languages automatically and publish transcripts through the existing ingest worker without an agent or a per-file request. Its declared coverage SHALL include the publisher's 100 language tokens. Japanese, English and Estonian SHALL verify quality, not limit supported languages.

The initial selection record SHALL separate diagnostic quality, conversion agreement, declared coverage, failures and unfinished comparisons. Initial delivery SHALL NOT wait for the full FLEURS comparison or the wider quality survey. Those measurements SHALL remain explicit upgrade work. Eligibility screening SHALL check redistribution terms and automatic offline operation. An eligibility exclusion SHALL NOT be reported as a measured quality failure.

A later model or language-routing switch SHALL follow the shared model rule and a frozen comparison protocol:

1. Use WER for verification languages with word boundaries and CER for Japanese.
2. Compare every arm on the same source cases and normalization.
3. Report per-case differences and 95% confidence intervals clustered by source sentence.
4. Confirm the switch once on untouched source units reserved before generation.
5. Keep each verification language's upper loss bound within 2.0 percentage points of the best candidate.
6. Prefer broader coverage among candidates that meet quality and cell resource bounds, then lower private memory.
7. Show that any routing layer improves results through an ablation against its component engines.

Language sets, mappings and thresholds SHALL remain deployment data. Wider quality measurements SHALL distinguish attempted languages, completed cases, failures and measured quality across at least 100 languages. Declared coverage SHALL NOT imply equal quality in every language.

#### Scenario: Ingest produces a transcript automatically

- **WHEN** speech is enabled and a supported audio or video file is ingested
- **THEN** the existing worker creates its transcript without an agent or another request
- **AND** the worker uses the pinned offline default and automatic language detection

#### Scenario: A later switch keeps the quality bound

- **WHEN** a candidate replaces the default model or adds language routing
- **THEN** its frozen comparison and fresh confirmation meet the 2.0-point bound
- **AND** any routing layer has a useful ablation

#### Scenario: Silent input does not fabricate a transcript

- **WHEN** the activity detector finds no speech in a decoded chunk
- **THEN** that chunk adds no transcript text
- **AND** the completed artifact uses the existing silent-media outcome

#### Scenario: A repeating window is decoded again

- **WHEN** a window's decode exceeds the registry's compression-ratio threshold or falls below its log-probability threshold
- **THEN** the engine decodes that window again at the next registry temperature
- **AND** the transcript keeps the first attempt that meets both thresholds, or else the most probable plausible attempt

#### Scenario: A long recording continues after a stop

- **WHEN** a speech job stops after some windows complete, and the same engine processes the same file again
- **THEN** transcription continues from the first unfinished window, in the language detected before the stop
- **AND** the completed transcript equals the transcript of a run without the stop
- **AND** a run by another engine label starts from the first window

#### Scenario: A removed recording leaves no partial transcript

- **WHEN** a recording with a partial transcript is removed from the vault
- **THEN** the deletion fan-out discards its progress record

### Requirement: Each Cloud engine is off by default and turns on after acceptance in a canary cell

Each engine (documents, OCR, image search, captions and speech) SHALL have its own switch. cellctl SHALL render the switch into each cell's environment, per cell. Every switch SHALL default to off: a deployment that sets no per-cell selection and no fleet default SHALL run no engine in any cell. Personal installs SHALL keep their current defaults. Under the current cellctl render, changing a cell's switch re-renders that cell and restarts its pod, a brief outage. While a switch change restarts the pod, the canary and the rollout SHALL change switches outside the cell backup window.

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

An engine whose switch is on in every existing cell MAY be made the fleet default. A fleet default SHALL apply to every Cloud cell, including a cell created later, so a new cell runs the engine with no per-cell edit. A cell's engines SHALL be the union of the fleet default and its own per-cell entries. The operator SHALL turn the engine on in each existing cell through the per-cell selection before setting the default, so that the default changes no existing cell's rendered environment, restarts no cell and starts no backfill. The operator, not cellctl, SHALL check this precondition: cellctl cannot tell an existing cell from a new one, and a cell created later is the case the default exists for.

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

#### Scenario: A new cell under a fleet default runs the engine

- **WHEN** the fleet default names an engine and a cell is created afterwards, with no per-cell entry for it
- **THEN** the new cell runs that engine
- **AND** no per-cell edit was needed

#### Scenario: Setting the default after per-cell rollout restarts no cell

- **WHEN** an engine is on in every existing cell through the per-cell selection, and the operator then adds the engine to the fleet default
- **THEN** every existing cell keeps the same rendered environment
- **AND** no cell restarts and no backfill starts

#### Scenario: A deployment with no default keeps every engine off

- **WHEN** a deployment sets no fleet default and no per-cell selection
- **THEN** every engine is off in every cell, including a cell created later

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

When an engine's switch turns on in a cell, the media already in that cell SHALL be queued for that engine automatically, with no operator action per file. Because switches turn on one cell at a time, backfill SHALL never run in every cell at once. This SHALL hold when an engine is made the fleet default under the precondition of the requirement "Each Cloud engine is off by default and turns on after acceptance in a canary cell", and for a cell created later, which backfills only its own media. A change of image space SHALL re-encode the stored image vectors once, through the same backfill.

#### Scenario: Turning on OCR processes the images already stored

- **WHEN** the OCR switch turns on in a cell that already stores images without OCR text
- **THEN** each of those images is queued once and its OCR text is extracted

#### Scenario: Backfill is not fleet-wide

- **WHEN** an engine is turned on across several cells
- **THEN** the cells never start that engine's backfill all at once
