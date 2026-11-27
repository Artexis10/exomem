## ADDED Requirements

### Requirement: Adoption pairs a recording with its completed Evermatter page

Adoption SHALL pair a staged audio or video original with its sidecar-named page: the staged page named `<original name>.md` in the same folder. It SHALL pair them only when the page holds a kept extraction, with all of these:

- `type: source` and the original's `media_type`;
- a completed extraction with text under `## Extracted text`;
- `binary_sha256` and `binary_size` equal to the original's;
- a part folder that holds every part that its `extraction_parts` count names.

#### Scenario: A matching page is paired

- **WHEN** a staged folder holds a recording and its completed page, whose `binary_sha256` and `binary_size` match the recording
- **THEN** the plan shows the page paired with the recording

#### Scenario: A page for other bytes is not paired

- **WHEN** a sidecar-named page records a `binary_sha256` that differs from its staged original
- **THEN** the plan does not pair the page

#### Scenario: A pending page is not paired

- **WHEN** a sidecar-named page records a pending extraction
- **THEN** the plan does not pair the page

#### Scenario: A page with missing parts is not paired

- **WHEN** a sidecar-named page lists three parts and its part folder holds one
- **THEN** the plan does not pair the page

### Requirement: A paired recording's Source keeps the page's speech result

Apply SHALL settle a paired page's result on the Source that adoption stored for its recording, by the same route as for a Source stored earlier. That Source SHALL keep the extracted text with its timed lines, `speakers`, `speaker_verification`, `diarize_requested`, `extracted_by`, the `asr_` facts, and the part pages with their text unchanged. The Source SHALL keep the provenance that adoption set for it. The kept text SHALL pass the card-number and credential removal.

#### Scenario: A recording keeps its transcript and labels

- **WHEN** adoption files a recording with a paired page that carries speaker labels
- **THEN** the recording's Source holds the page's extracted text, timed lines, `speakers` and `speaker_verification`
- **AND** the Source carries `imported_from`, `import_collection`, `collection_path` and `original_sha256`

#### Scenario: A long transcript keeps its part pages

- **WHEN** the paired page lists three parts and its part folder holds them, and adoption runs without including hidden files
- **THEN** the new Source lists three parts, and its whole text equals the old page's whole text

### Requirement: A kept speech result starts no speech job

The extraction request that follows the commit of a paired original SHALL admit no speech stage. For a video it SHALL admit a frame-vector job alone, so that visual search does not wait for a worker restart. Custody acknowledgement for a paired audio original SHALL wait for no extraction job, as for an original whose extraction stage is off.

#### Scenario: A kept recording is not transcribed again

- **WHEN** adoption files a recording with a paired page while the speech engine is on
- **THEN** no speech job exists for the recording

#### Scenario: A kept recording needs no job before acknowledgement

- **WHEN** default apply imports an audio recording with a paired page
- **THEN** the original, the copied bytes and the Source page are durable before the reply, and no extraction job exists for the recording

#### Scenario: A kept video gets its frame vectors

- **WHEN** adoption files a video with a paired page
- **THEN** no speech job exists for the video, a frame-vector job exists for it, and its frame vectors are indexed without a worker restart

### Requirement: A paired page is not filed on its own

A paired page and its part pages SHALL NOT be filed as Sources of their own. Apply SHALL repeat the pairing checks with the bytes it writes. A page or part that changed after the plan SHALL give the page's item `SOURCE_CHANGED`, and its recording is stored as usual with a pending extraction.

#### Scenario: No text Source holds a paired page

- **WHEN** adoption files a recording with a paired page
- **THEN** no text Source holds the page or its part pages

#### Scenario: A second run writes nothing

- **WHEN** the same folder with a paired page is planned and applied again unchanged
- **THEN** the recording reports `already-applied`, the page is not filed again, and no file in the vault changes

#### Scenario: A page changed after the plan

- **WHEN** a paired page changes between `plan` and `apply`
- **THEN** the page's item records `SOURCE_CHANGED`, and nothing is written for the page

### Requirement: A page whose original is already stored pairs with that Source

A sidecar-named page SHALL also pair with a stored Source whose original matches the page's `binary_sha256` and `binary_size`. The original MAY come from an earlier window or run. When that Source's extraction or one of its prior extraction pages holds the page's kept result, apply SHALL write nothing. Apply SHALL decide that by a digest over the whole extraction text, part pages included, with the page's text taken after the card-number and credential removal.

#### Scenario: A page in a later window keeps its result

- **WHEN** a series of windows files a recording in an artifact window, and its paired page in a later text window
- **THEN** the recording's Source holds the page's extraction, and no text Source holds the page

#### Scenario: A Source that already holds the page's result

- **WHEN** a paired page matches a stored Source whose extraction is that page's kept result
- **THEN** apply writes nothing for the page, and the recording reports `already-applied`

#### Scenario: Parts decide whether the result is the same

- **WHEN** a paired page and its stored Source hold the same first part, and their third parts differ
- **THEN** apply does not treat the page's result as already stored

### Requirement: A stored Source takes a paired page's result by the speech replacement rule

When a stored Source does not hold a paired page's kept result, apply SHALL offer that result to the Source under the replacement rule of `speech-result-attachment`, without `replace_labelled`. When the rule accepts it, apply SHALL write the kept extraction to it, guarded by the Source page's content hash, and SHALL NOT file the page. When the rule refuses it, apply SHALL write the result, with its part pages, as a prior extraction page that the Source lists in `prior_extractions`.

#### Scenario: A pending Source takes the page's result

- **WHEN** a paired page matches a stored Source whose extraction is pending
- **THEN** the Source holds the page's extraction, no speech job waits for it, and no text Source holds the page

#### Scenario: An unlabelled transcript is replaced by a labelled page

- **WHEN** a paired page with speaker labels matches a stored Source whose completed transcript has no labels and no `speaker_names`
- **THEN** the Source holds the page's extraction, and the earlier transcript stays on a linked prior extraction page

#### Scenario: A labelled or named Source keeps its result

- **WHEN** a paired page with three parts matches a stored Source whose transcript carries speaker labels or `speaker_names`
- **THEN** the Source keeps its transcript, and a prior extraction page that the Source lists holds the page's result with its three parts
- **AND** no text Source holds the page, and the plan and the apply result count the page under its reason

#### Scenario: A second run after a kept-out page writes nothing

- **WHEN** the same folder is applied again after a labelled Source kept a page's result out
- **THEN** apply finds the result on the Source's prior extraction page, and writes nothing

#### Scenario: A running job does not replace a kept result

- **WHEN** a speech job for a pending Source is running when adoption writes the kept extraction to that Source
- **THEN** the job's later result is not published, and the Source keeps the kept extraction

### Requirement: Import selection carries a selected page's part folder

The selection rules that `exomem import` and `exomem cloud import` share SHALL select the part folder of each selected page, as an exception to the hidden-entry rule of local staging. A part folder SHALL be the folder that the extraction's part layout names for a page, such as `.X.parts/` for the page `X.md`. The other selection rules SHALL still apply to its files. The report before staging or archiving SHALL count the part folders that the selection carries.

#### Scenario: A long transcript's part pages travel with it

- **WHEN** the folder holds a page with a part folder, and the user does not pass `--include-hidden`
- **THEN** the selection holds the part folder's pages, and the report counts the part folder

#### Scenario: Other hidden entries still stay behind

- **WHEN** the folder also holds a hidden folder that is no page's part folder
- **THEN** the selection holds nothing from that folder, and the report counts it under the hidden reason

### Requirement: A page waits for a recording that another window has yet to file

A complete page whose recording no Source holds yet SHALL wait while another window of the same import series still holds that recording and its filing is unfinished. It SHALL wait as a retryable item with the code `KEPT_EXTRACTION_RECORDING_PENDING`, and apply SHALL write no Source and record no outcome for it. A later `apply` or `file` call SHALL pair the page once the recording is stored.

#### Scenario: A page whose window is filed before its recording's window

- **WHEN** a series files the window that holds a page before the window that holds its recording
- **THEN** the page's window stops with `KEPT_EXTRACTION_RECORDING_PENDING` and files no text Source
- **AND** after the recording's window files, the next call settles the page on the recording's Source

### Requirement: A page stops waiting when no window will file its recording

A page SHALL wait in the same way, under `KEPT_EXTRACTION_LOOKUP_UNAVAILABLE`, while the duplicate index fails for now. When no window is still to file the recording, or the configuration turns the index off, the page SHALL file as a text Source and the result SHALL count it under `KEPT_EXTRACTION_NO_RECORDING`, which is not a mismatch.

#### Scenario: The recording's window is discarded

- **WHEN** a page waits and the person discards the window that held its recording
- **THEN** the next call files the page as a text Source and counts it under `KEPT_EXTRACTION_NO_RECORDING`

#### Scenario: The recording's window is cancelled

- **WHEN** a page waits and the person cancels the run of the window that held its recording
- **THEN** the next call files the page as a text Source and counts it under `KEPT_EXTRACTION_NO_RECORDING` once

#### Scenario: The configuration turns the duplicate index off

- **WHEN** a page's recording is stored nowhere and the configuration turns the duplicate index off
- **THEN** the page files as a text Source and no call waits for it

#### Scenario: The duplicate index cannot answer

- **WHEN** the duplicate index cannot answer for a page, and then can
- **THEN** the page waits under `KEPT_EXTRACTION_LOOKUP_UNAVAILABLE`, and the next call pairs it

### Requirement: An unpaired page is filed as before and counted

A sidecar-named page that fails a pairing check SHALL keep today's behaviour: adoption files it as a text Source, and its original gets a pending extraction. The plan and the apply result SHALL count the originals stored with a kept extraction. They SHALL also count, by reason, the sidecar-named pages whose extraction was not kept, and the unpaired pages whose part pages were not filed with them.

#### Scenario: A mismatched page is filed as text and counted

- **WHEN** a sidecar-named page records a `binary_size` that differs from its staged original
- **THEN** adoption files the page as a text Source and the original with a pending extraction
- **AND** the plan and the apply result count the page under its reason

#### Scenario: An unpaired long page is counted without its parts

- **WHEN** a sidecar-named page with three parts records a pending extraction
- **THEN** adoption files the page as a text Source without its part pages
- **AND** the plan and the apply result count the page among the unpaired pages whose part pages were not filed
